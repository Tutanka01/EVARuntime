# Architecture technique — Cluster EVA Inference Gateway

> Ce document décrit l’architecture actuellement livrée. Pour les limites
> connues, l’intégration multi-runtime, les modèles multi-GPU et les jalons à
> venir, consulter la [roadmap](../ROADMAP.md) et la [vision produit](vision.md).

Ce document explique les décisions de conception, les flux de données et
les invariants de sécurité du gateway. Il s'adresse aux développeurs et
aux administrateurs souhaitant comprendre ou modifier le système.

---

## Vue d'ensemble

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Réseau UPPA / Internet                                                 │
│                                                                         │
│  Clients inférence            Admin (réseau campus uniquement)          │
│  ┌──────────┐  ┌──────────┐   ┌──────────────────────────────────┐     │
│  │ Python   │  │ curl     │   │ Navigateur → /admin/dashboard    │     │
│  │ openai   │  │ LangChain│   │ curl → /admin/models/*           │     │
│  └────┬─────┘  └────┬─────┘   └────────────────┬─────────────────┘     │
│       └─────────────┘                           │                       │
│              │ HTTPS / TLS 1.3                  │ HTTPS (campus only)   │
└──────────────┼──────────────────────────────────┼─────────────────────┘
               │                                  │
┌──────────────┼──────────────────────────────────┼─────────────────────┐
│  Cluster EVA — hébergé à l'UPPA (GPU L40S)       │                     │
│              ▼                                  ▼                     │
│  ┌──────────────────────────────────────────────────────────────────┐ │
│  │  nginx  (TLS termination, rate limiting, IP filtering /admin)    │ │
│  └──────────────────────────┬─────────────────────────────────────┘ │
│                              │ HTTP/1.1 (127.0.0.1:8000)             │
│  ┌───────────────────────────▼──────────────────────────────────────┐ │
│  │                    FastAPI Gateway (main.py)                      │ │
│  │                                                                   │ │
│  │  ┌─────────────┐  ┌──────────────┐  ┌──────────────────────────┐ │ │
│  │  │    auth.py  │  │rate_limiter  │  │     proxy.py             │ │ │
│  │  │  Bearer SHA │  │sliding window│  │  routing model_id        │ │ │
│  │  └─────────────┘  └──────────────┘  │  forward + SSE streaming  │ │ │
│  │                                      └────────────┬─────────────┘ │ │
│  │                                                   │               │ │
│  │  ┌──────────────────────────────┐  ┌─────────────▼─────────────┐ │ │
│  │  │  metrics.py + dashboard.html │  │   ModelManager            │ │ │
│  │  │  /admin/metrics/*  (JSON)    │  │   model_manager.py        │ │ │
│  │  │  /admin/dashboard  (HTML)    │  │  ┌─────────────────────┐  │ │ │
│  │  └──────────────┬───────────────┘  │  │ Budget VRAM + LRU   │  │ │ │
│  │                 │                  │  │ Pool de ports        │  │ │ │
│  │  ┌──────────────▼───────────────┐  │  └─────────────────────┘  │ │ │
│  │  │  ModelRegistry               │  │  ServerManager[70B] :8081  │ │ │
│  │  │  model_registry.py           │◄─┤  ServerManager[8B]  :8082  │ │ │
│  │  │  models.yaml (source vérité) │  └──────────┬────────────────┘ │ │
│  │  └──────────────────────────────┘             │ subprocesses      │ │
│  │                                               │                   │ │
│  │  ┌───────────────────────────┐  ┌─────────────▼────────────────┐ │ │
│  │  │  SQLite WAL (database.py) │  │  llama-server (llama.cpp)    │ │ │
│  │  │  users | api_keys         │  │  :8081 llama-3.3-70b (~42GB) │ │ │
│  │  │  usage_log                │  │  :8082 llama-3.1-8b (~5.5GB) │ │ │
│  │  └───────────────────────────┘  │  (pool de ports dynamique)   │ │ │
│  └──────────────────────────────────└────────────┬─────────────────┘ │ │
│                                                   │ CUDA              │ │
│                                   ┌──────────────▼─────────────┐ │ │
│                                   │  NVIDIA L40S 48GB           │ │ │
│                                   │  Budget net : ~43.6 GB      │ │ │
│                                   │  (48 - 2 overhead - 5%)     │ │ │
│                                   └────────────────────────────┘ │ │
│  └──────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘
```

---
 
## Registre des modèles (models.yaml)

### Source de vérité

Le fichier `/var/lib/llm-gateway/models.yaml` est la source de vérité
**persistante** pour tous les modèles disponibles sur la gateway. Il est lu au
démarrage et peut être modifié en direct via l'API admin. Il vit sous
`/var/lib`, writable par `llmservice`, tandis que secrets et topologie restent
sous `/etc/llm-gateway`, non writable par le service.

**Une mutation admin ne resérialise pas le fichier** (COR-020). Elle le
**retouche textuellement** : ajout en fin de document, retouche des seules lignes
de champ qui changent, retrait du seul bloc d'une entrée supprimée. Le texte
candidat est reparsé et comparé au document attendu ; s'il ne signifie pas
exactement ce qui était prévu, l'écriture est **refusée** plutôt que remplacée
par une réécriture globale — celle-ci détruirait les commentaires
d'exploitation du fichier, qui sont de la documentation. C'est la politique
d'AUT-007 (`bootstrap/registry_writer.py`), réutilisée par `model_registry.py`
pour qu'il n'existe qu'**une** politique d'écriture de `models.yaml`.

La **localisation** d'une entrée dans ce texte suit la même règle depuis COR-028 :
elle vit dans `model_registry._entry_bounds`, et `registry_writer` s'y adosse au
lieu d'entretenir un second localisateur. Le premier ancrait sur une ligne
`- id: <model_id>`, donc sur les seules entrées dont `id` est la première clé ;
`yaml.safe_dump` trie les clés et met `capabilities` en tête, si bien que sur
tout hôte dont le `models.yaml` était déjà passé par une mutation admin, l'étape
`enable_model` de l'amorçage refusait un fichier pourtant valide. Le refus reste
la bonne issue quand l'entrée est réellement introuvable ou ambiguë : le
localisateur voit plus large, il ne refuse pas moins.

S'y ajoutent une sauvegarde horodatée et bornée
(`models.yaml.pre-admin.<stamp>.bak`, 5 conservées, motif distinct des
`*.pre-bootstrap.*` du bootstrap), une écriture atomique validée par le chargeur
du registre lui-même, le `fsync` du fichier **et** du répertoire parent — sans
lequel le renommage n'est pas durable —, et la restauration du mode, du
propriétaire et du groupe après `os.replace`. Un refus restaure l'état mémoire :
la gateway ne reste jamais en avance sur son disque. Un verrou optimiste compare
en outre le document relu au snapshot qui précédait la mutation : toute édition
sémantique concurrente, scalaire comprise, provoque un refus explicite plutôt
qu'un écrasement ; une retouche de commentaire seule reste permise et préservée.
Ce garde ne remplace pas un verrou inter-processus entre deux gateways écrivant
le même fichier. Détails opérationnels et liste des refus : `docs/admin.md`.

La seule superposition à cette vérité est l'activation provisoire de
`bootstrap-apply` : un snapshot validé peut être publié **en mémoire** dans le
worker unique, sous un bail borné, pendant la recette du premier token. Le YAML
reste désactivé jusqu'à la confirmation ; un redémarrage ou l'expiration du bail
revient donc à l'état persistant sûr.

```yaml
models:
  - id: "llama-3.3-70b-instruct"    # identifiant OpenAI-compatible
    path: "/models/Llama-3.3-70B-Instruct-Q4_K_M.gguf"
    description: "Modèle principal UPPA"
    vram_gb: 42.0                    # poids + KV cache à charge nominale
    enabled: true
    capabilities: [text_generation, tool_calls, streaming]
    llama_params:                    # paramètres par modèle — remplace l'ancienne config globale
      n_gpu_layers: 999
      ctx_size: 32768
      parallel: 4
      cache_type_k: "q8_0"
      ...

  - id: "llava-7b"
    path: "/models/llava-v1.6-mistral-7b-Q4_K_M.gguf"
    mmproj_path: "/models/llava-v1.6-mistral-7b-mmproj-f16.gguf"  # projecteur CLIP — requis pour vision
    mmproj_sha256: "<64 caractères hexadécimaux>"                 # requis pour vision
    vram_gb: 6.0
    capabilities: [text_generation, vision, streaming]
    ...
```

Les champs `mmproj_path` et `mmproj_sha256` sont obligatoires quand `vision` est
présent dans `capabilities`. Le registre refuse sinon de démarrer. Le projecteur
est attesté avant chaque chargement, puis transmis à llama-server via `--mmproj`.

### Speculative decoding MTP

Un bloc optionnel `speculative:` active le **Multi-Token Prediction** (MTP) sur les
modèles dont la tête MTP est intégrée au GGUF (DeepSeek-V3, GLM, etc.) :

```yaml
  - id: "deepseek-v3-mtp"
    path: "/models/DeepSeek-V3-Q4_K_M.gguf"
    vram_gb: 42.0
    capabilities: [text_generation, streaming]
    speculative:
      type: mtp        # seul type supporté actuellement
      draft_max: 16    # --spec-draft-n-max : nb de tokens draftés
      draft_min: 0     # --spec-draft-n-min (optionnel)
      draft_p_min: 0.0 # --spec-draft-p-min, proba min greedy (optionnel)
```

`build_llama_cmd` traduit ce bloc en flags `--spec-type draft-mtp --spec-draft-n-max …`.
**Invariant VRAM :** la tête MTP est dans le même GGUF, donc MTP **n'ajoute pas de
VRAM** — `vram_gb` reste l'empreinte du modèle seul, et la logique de capacité/éviction
est inchangée. Absent, le bloc ne produit aucun flag : comportement strictement
identique à avant (rétrocompatible).

**Local et cluster :** la définition transite vers les `node_agent` sous forme de dict
(`to_dict()` → `LoadRequest.model` → `_parse_entry`), donc le bloc `speculative` est
re-validé et appliqué à l'identique sur les nodes — aucun changement de protocole. Le
binaire `llama-server` de chaque node doit supporter `--spec-type` (vérifier avec
`llama-server --help | grep spec`).

### Validation à la charge

`ModelRegistry._load()` applique plusieurs couches de validation avant d'accepter une entrée :

1. `yaml.safe_load()` — jamais `yaml.load()` (protège contre l'injection YAML)
2. `model_id` validé par regex `^[a-z0-9][a-z0-9._-]{0,62}$` (63 caractères max ; pas de `/`, `..`, espaces)
3. `path` doit être absolu (`path.is_absolute()`) et pointer vers un `.gguf`
4. `mmproj_path`, si présent, subit les mêmes validations que `path`
5. Si `ALLOWED_MODEL_DIRS` est configuré : `path` et `mmproj_path` doivent être sous un répertoire autorisé
6. Les clés inconnues, les conversions implicites et les capabilities hors de
   `text_generation`, `streaming`, `tool_calls`, `vision`, `embeddings` sont refusées
7. `enabled`, s'il est présent, doit être un vrai booléen YAML (`true` ou
   `false`) ; les chaînes telles que `"false"` sont refusées au lieu d'être
   coercées en vrai
8. `vram_gb > 0` et `ubatch_size ≤ batch_size`
9. `vision` exige `mmproj_path` et son `mmproj_sha256` valide

---

## Planificateur d'amorçage (`bootstrap`)

Le registre décrit ce que **cette** installation sert. Le paquet
`gateway/bootstrap/` répond à la question d'avant : sur un hôte encore nu, que
faudrait-il installer pour atteindre le premier token, et pourquoi ? Il produit
un **plan** — un document — et n'applique rien.

### Trois étapes, et pourquoi elles sont séparées

| Couche | Artefact | Privilèges | Écrit sur l'hôte |
|---|---|---|---|
| Installation du socle | `gateway/deploy/install.sh` | `root` | oui — utilisateur système, venv, systemd, nginx, secrets |
| Planification | `python cli.py bootstrap-plan` | aucun | **non** |
| Application relue | `python cli.py bootstrap-apply … --apply` | selon les chemins du plan | oui — runtime, modèles, registre et preuves |

`install.sh` reste la voie supportée pour poser le **service** : utilisateur,
venv, systemd, nginx et secrets. Il ne remplace ni `env` ni `models.yaml` (cf.
[guide de déploiement](deployment.md#4-installation-du-gateway)). Une
installation locale neuve pointe déjà `LLAMA_SERVER_BIN` sur le lien canonique
`/opt/llama.cpp/current/llama-server`, mais tolère que ce lien n'existe pas
encore : `/health` et l'administration restent disponibles, tandis que `/ready`
reste rouge. Le registre livré ne contient aucun modèle actif dont le GGUF est
absent. Une fois cette gateway mono-worker joignable sur loopback,
`bootstrap-apply` exécute le plan relu pour poser le runtime et les modèles puis
prouver le premier token.
Le caractère mono-worker est un invariant du protocole : le snapshot
provisoire, son verrou et son bail vivent dans la mémoire du processus. L'unité
systemd officielle respecte cet invariant (`--workers 1`) ; un lancement manuel
avec plusieurs workers n'est pas supporté.

`bootstrap-plan` est la couche non privilégiée. Elle inventorie, calcule, explique
— et s'arrête là. Aucun téléchargement, aucune compilation, aucune écriture de
registre, aucun `systemctl`. Le seul sous-processus que la chaîne puisse lancer
est `llama-server --version`, et seulement si l'opérateur fournit `--llama-bin`.

La séparation existe parce que ces trois étapes n'ont ni le même risque ni le
même public. Un plan se lit, se discute, se colle dans un ticket et se rejoue à
l'identique sans conséquence ; l'installation du socle et l'application relue
modifient l'hôte, mais pas les mêmes artefacts. Les confondre reviendrait à
demander `root` pour obtenir un avis, et à faire d'une lecture de diagnostic une
modification du système.

L'application de production reçoit explicitement l'`EnvironmentFile` systemd.
Elle refuse un registre différent de `MODELS_CONFIG_PATH`, un binaire publié
différent de `LLAMA_SERVER_BIN` et un plancher de sécurité nul. Après — et
seulement après — un parcours runtime entièrement réussi, elle remplace
atomiquement `LLAMA_SERVER_BIN` et `LLAMA_SERVER_MIN_BUILD` dans ce fichier sans
réécrire les secrets ni les autres réglages. Le service redémarré charge donc
exactement le runtime dont l'installation vient d'être prouvée.

### Pipeline

```text
inventaire matériel  (bootstrap/inventory.py, AUT-002)
      ↓
résolution runtime   (bootstrap/runtime_resolver.py, AUT-003)
      ↓
recommandation       (bootstrap/llmfit.py, AUT-004 — optionnelle)
      ↓
catalogue approuvé   (bootstrap/catalog.py + catalog.yaml, AUT-005)
      ↓
sélection + budget   (bootstrap/planner.py, AUT-001)
      ↓
séquence d'étapes    → schema.BootstrapPlan (bootstrap/schema.py)
```

Les producteurs ne se connaissent pas : ils se projettent vers `schema`, jamais
l'inverse. C'est ce qui permet à l'un d'échouer sans entraîner les autres — un
`nvidia-smi` cassé dégrade la section `hardware` sans empêcher le catalogue
d'être lu, ni le plan d'exister pour dire où est le trou. `planner.py` est le
seul module qui importe tous les autres, et le seul à connaître l'ordre global.

Un GGUF déjà présent dans le volume des modèles est inspecté par
`bootstrap/gguf_meta.py` (AUT-013) : lecture bornée de l'en-tête, sans
matérialiser les tenseurs ni le vocabulaire, pour affiner l'estimation à partir
du fichier réel plutôt que de la valeur déclarée.

### Contrat du plan

`schema.BootstrapPlan` tient quatre promesses, chacune vérifiable dans le module :

| Promesse | Mécanisme |
|---|---|
| versionné | `schema_version` (entier stable), comme le `SCHEMA_VERSION` de `doctor` |
| validé | `validate_plan_dict()` — un plan mal formé n'est jamais « appliqué au mieux » |
| sans secret | `find_secret_leaks()` sur le document rendu, noms de champs **et** valeurs |
| lisible | `render_human()` produit la même information que le JSON, en français |

Chaque section porte un statut (`ok`, `warn`, `fail`, `skip`) et des constats.
Un constat de niveau `fail` bloque **quel que soit le statut de sa section** :
la première écriture ne collectait les bloqueurs que dans les sections déjà en
`fail`, et un producteur émettant un `fail` dans une section `warn` aurait vu son
bloqueur disparaître en silence.

### Non-divulgation

Le plan est conçu pour être copié dans un ticket. `render_json()` comme
`render_human()` passent par `assert_no_secrets()` : si le document contient une
valeur ressemblant à un secret, **rien n'est rendu** — la commande échoue.

Deux filets indépendants, parce qu'aucun ne suffit seul :

- **par nom de champ** — `token`, `secret`, `api_key`, `password`… Un tel champ
  n'a le droit de porter qu'un booléen ou `null` ; un booléen de présence
  (`"token_present": true`) est la façon recommandée de signaler un secret sans
  le dire ;
- **par forme de valeur** — un `hf_…`, un `sk-…`, une clé `llmgw-…`, un en-tête
  `Bearer`, un bloc PEM ou une URL `user:password@` rangés sous un nom anodin.

Le message de refus cite le **chemin** et le motif, jamais la valeur : un rapport
de fuite qui recopie le secret est lui-même une fuite.

### Téléchargements sortants

Les artefacts runtime et modèles partagent `bootstrap/public_https.py`. Cette
frontière refuse tout schéma autre que HTTPS, les identifiants dans l'URL, les
noms locaux et les adresses littérales non publiques. Pour chaque requête et
chaque redirection, le hostname est résolu une seule fois ; une réponse vide,
invalide, privée ou mixte est refusée en bloc. La connexion TCP utilise ensuite
exactement les adresses validées, tandis que le certificat et SNI restent liés
au hostname initial. Il n'existe donc pas de seconde résolution exploitable par
DNS rebinding, et `http.client` n'hérite pas des proxies d'environnement.

Ce contrôle ne remplace pas la preuve supply-chain : l'archive runtime et chaque
fichier du catalogue sont toujours confrontés à leur SHA-256 avant promotion.
Inversement, le SHA ne suffirait pas à fermer un SSRF, car une requête vers le
réseau privé a déjà produit son effet même si son corps est ensuite rejeté.

### Ordre des étapes

```text
accept_license → download_model → verify_artifact → write_registry (désactivé)
  → calibrate_model → enable_model (provisoire) → smoke_test → warmup_model
```

Trois inversions seraient des défauts, pas des goûts :

- **vérifier après avoir posé l'artefact** ne protège de rien — le contrôle
  SHA-256 précède la mise en service, jamais l'inverse ;
- **activer avant d'avoir calibré** publie une capacité supposée : `write_registry`
  écrit délibérément `enabled: false`, et `enable_model` est la seule étape qui
  rend le modèle servable — provisoirement dans la mémoire de la gateway, pas
  encore dans le fichier ;
- **séparer l'activation provisoire de sa recette** rend le plan non compensable :
  le pré-vol exige le triplet adjacent `calibrate_model → enable_model → smoke_test`
  pour le même modèle ;
- **préchauffer avant la recette** conserverait en mémoire un modèle dont aucun
  token n'a encore été prouvé.

`verify_artifact` est une action à **deux domaines**, et un seul l'emploie. Pour
les GGUF du catalogue, elle suit `download_model` et relit réellement les octets.
Pour l'archive de `llama-server`, elle **n'existe pas** : le plan ne l'émet pas
(COR-030). La raison est qu'à ce numéro d'étape l'archive n'est pas encore
téléchargée — c'est `install_runtime` qui la récupère puis confronte son empreinte
avant d'extraire quoi que ce soit. Une étape qui ne peut rien vérifier était
sautée par l'applicateur, et une étape sautée ne prouve rien : la condition n°1 du
jalon M2 retombait `unsatisfied` sur une installation pourtant réussie, et
`bootstrap-apply` sortait en code 3. L'empreinte attendue est donc inscrite dans le
détail de l'étape `install_runtime`, qui est celle qui la contrôle.

`smoke_test` est exécutée **pour chaque modèle**, immédiatement après son
activation provisoire. Elle traverse le chemin public réel (nginx → gateway →
`llama-server`) et ferme la transition DEC-010 : succès, la preuve complète
persiste `enabled: true` ; échec, preuve illisible, exception ou annulation,
l'applicateur ferme l'admission live puis décharge sans forcer les requêtes déjà
actives. Pendant toute cette fenêtre, le fichier reste `enabled: false` : un
redémarrage revient donc à l'état sûr. L'état live porte en plus un bail borné :
si l'applicateur est tué sans pouvoir compenser, la gateway expire elle-même
l'activation provisoire, ferme l'admission et tente le déchargement. Une entrée qui
était déjà active avant l'exécution n'est jamais qualifiée de provisoire et ne
peut donc pas être désactivée par cette compensation. La recette reprend le
parcours du premier token déjà outillé par
[`smoke_test.sh`](deployment.md#recette-du-premier-token-smoke_testsh) : TTFT
mesuré, rapport sans secret.

Le bail n'est pas `MODEL_LOAD_TIMEOUT + constante` : il additionne le pire
chemin séquentiel autorisé par les timeouts de readiness, d'identité, de load,
de stream, de log d'usage, de nettoyage et de confirmation, puis une marge. Une
configuration qui demanderait plus que le maximum contractuel de 3600 s est
refusée ; elle n'est jamais tronquée silencieusement.

Le raccord passe par `POST /admin/models/{id}/bootstrap-sync`, uniquement sur
l'origin admin loopback et sous `ADMIN_SECRET`. Les trois transitions
`activate`, `confirm` et `rollback` portent l'empreinte SHA-256 exacte du YAML ;
`activate` porte aussi la VRAM calibrée et la durée du bail. Une mutation
concurrente du fichier, d'un autre modèle ou de l'état mémoire est refusée. Les
mutations admin ordinaires du registre sont également refusées tant que la
transition est ouverte. Les mutations disque déportées par `asyncio.to_thread`
sont attendues jusqu'à leur terminaison même sous annulation : la compensation
ne peut donc pas être doublée par un thread qui republierait `enabled: true`
après le rollback.

### Estimé contre mesuré

Les ressources du catalogue et l'estimation tirée de l'en-tête GGUF sont des
**estimations conservatrices**, jamais des mesures. La hiérarchie est explicite :

```text
en-tête GGUF + paramètres  →  ESTIMATION conservatrice
                           →  chargement réel (étape calibrate_model)
                           →  mesure des pics
                           →  valeur de capacité approuvée
```

Aucune valeur estimée ne doit être recopiée telle quelle dans le `vram_gb` du
registre. `calibrate_model` est l'étape qui remplace l'estimation par des pics
observés et **propose** un `vram_gb` sans l'appliquer silencieusement ; tant
qu'elle n'a pas eu lieu, l'entrée reste `enabled: false`.

Les « paramètres » de cette chaîne sont ceux du modèle inspecté, et de lui seul :
`ctx_size`, `parallel` et les types de cache KV viennent des `runtime.defaults` de
**son** entrée de catalogue (COR-027). Le poste dominant de l'estimation est le
cache KV, proportionnel au produit `ctx_size × parallel` : appliquer à un modèle
les réglages d'un autre — ce que faisait le planificateur au-delà du premier
modèle retenu — produit un chiffre faux de plusieurs ordres de grandeur, et faux
dans le sens de la sous-estimation quand le premier modèle est le plus modeste.

### LLMfit : conseiller, jamais autorité

La règle d'activation, littéralement :

```text
recommandation LLMfit                        ← simple ordonnancement
  + modèle approuvé par le catalogue         ← filtre dur
  + estimation conservatrice                 ← filtre dur
  + chargement réel de calibration           ← étape du plan, pas une évaluation
  = modèle activable
```

Absent, LLMfit fait sortir la section `recommendation` en `skip` et le plan reste
valide : c'est le cas par défaut sur une machine nue comme en CI. Présent, il ne
peut qu'**ordonner** des candidats déjà approuvés par le catalogue et déjà
retenus par le budget de l'hôte. Il ne peut ni ajouter un modèle absent du
catalogue, ni ressusciter une entrée non épinglée, ni relever un budget.

La subordination est structurelle et pas seulement déclarative :

- `llmfit.py` ne construit ni n'importe `schema.PlanStep` et ne nomme aucune
  constante `ACTION_*` : il lui est impossible d'émettre `enable_model` ;
- les identifiants qui en sortent sont publiés sous la clé `candidate`, jamais
  `model` ou `model_id` : rien de ce qu'il écrit n'a la forme d'une entrée de
  registre ;
- chaque entrée porte `catalog_approved: null` — « non statué ici » ;
- le rapprochement avec le catalogue exige une correspondance exacte
  (identifiant d'entrée ou `repo_id`) : un rapprochement flou ferait entrer par
  la petite porte le pouvoir refusé par la grande.

Deux asymétries volontaires dans les statuts émis. **Absence n'est pas échec** :
un conseiller optionnel manquant ne doit jamais empêcher un plan d'exister. Mais
**ce que l'opérateur a déclaré doit tenir** : une empreinte épinglée qui ne
correspond plus, ou un profil manuel désigné et illisible, sont des `fail` — non
parce que le conseil manque, mais parce que la machine n'est pas dans l'état
déclaré.

### Limites connues, écrites noir sur blanc

Trois zones où le code est en avance sur la preuve. Elles sont énoncées ici
plutôt que découvertes en production :

- **Les fixtures de test LLMfit sont synthétiques.** Le dépôt amont ne publie ni
  les noms de champs ni un exemple de sortie de `recommend --json` ; la forme
  validée par l'adaptateur est dérivée de la spécification. Tous les fichiers de
  `gateway/tests/fixtures/llmfit/` sont préfixés `synthetic-`, et un test échoue
  si ce préfixe disparaît. À remplacer par de vraies captures avant de s'appuyer
  sur LLMfit en production.
- **La matrice d'artefacts `llama-server` mélange constats et hypothèses.**
  Chaque variante de `DEFAULT_VARIANTS` porte un champ `evidence` :
  `constat-§6` pour ce que la spécification affirme, `hypothèse-à-confirmer` pour
  ce qui est plausible mais non vérifié (le résolveur n'a pas d'accès réseau et
  ne consulte aucune page de release). Sont des **constats** : les images GHCR
  officielles `server-cuda` et `server-cuda13` sur `linux-x86_64`, les builds
  locaux CUDA 12/13 sur `linux-x86_64`, et le build local CPU `linux-x86_64`
  (réellement exercé lors du déploiement du 2026-07-30). Tout le reste — archives
  natives de release, image CPU GHCR, builds ROCm, Vulkan, arm64 et Metal — est
  une hypothèse. Une variante retenue sur hypothèse déclenche un `warn` : le plan
  ne présente jamais une supposition comme un fait.
- **Aucun test n'a été exécuté contre un GPU réel.** Les chemins GPU sont
  couverts par des sondes injectées ; la VRAM reste déclarative tant qu'une
  calibration n'a pas eu lieu sur la machine cible.

Par ailleurs, `DEFAULT_VARIANTS` ne porte **aucun digest** : rien n'est épinglé
dans ce dépôt à ce jour, et inventer une empreinte serait pire que de ne pas en
avoir. Avec la politique par défaut, seules les variantes `local-build` sont donc
éligibles ; les autres apparaissent dans les motifs de rejet avec la mention
« non épinglé », qui est l'information utile.

Ses `reference` d'archive ne valent pas davantage : elles désignent la **page de
releases** du projet, pas un artefact. Même munie d'une empreinte, la matrice
livrée ne donnerait à `runtime_installer` aucune URL téléchargeable. L'opérateur
fournit donc la sienne — `bootstrap-plan --runtime-variants`, chargée par
`gateway/bootstrap/runtime_variants.py`, modèle dans
`gateway/deploy/runtime-variants.yaml.example`. Trois propriétés structurent ce
chargeur :

- il **remplace** la matrice livrée au lieu de s'y ajouter. En union, une faute
  de frappe dans `platform` rendrait l'entrée épinglée invisible et un
  `local-build` livré l'emporterait en silence : l'opérateur lirait un plan
  réussi qui ignore son épinglage ;
- il est **fail-closed** : un fichier malformé refuse en bloc et ne se replie
  jamais sur `DEFAULT_VARIANTS` ni sur ses seules entrées valides ;
- il impose le niveau de preuve `constat-opérateur` — troisième valeur d'`evidence`.
  Un fichier ne peut pas se réclamer de `constat-§6` : §6 ne connaît aucune
  empreinte, et lui laisser l'autorité de la spécification annulerait la
  distinction constat/hypothèse dont vit le rapport d'installation. Le plan porte
  un constat `info` nommant l'origine des variantes employées.

La politique d'URL vit dans ce chargeur et non dans le résolveur : elle réutilise
`bootstrap/public_https.py`, donc `socket` et `http.client`, que le garde-fou
d'isolation de `runtime_resolver` interdit précisément d'y faire entrer. Elle
ajoute à la politique publique HTTPS trois contraintes — le chemin doit désigner
une archive extractible, sans chaîne de requête ni fragment — et
`production.runtime_installer_from_plan` l'applique désormais aussi, pour refuser
une URL inexploitable **avant** le téléchargement plutôt qu'après, au contrôle
d'empreinte.

Usage opérationnel de la commande : [guide administrateur](admin.md#9-planificateur-damorçage--bootstrap-plan).
Catalogue de modèles approuvés : [guide de déploiement](deployment.md#catalogue-de-modèles-approuvés-amorçage).

---

## Budget VRAM et éviction LRU

### Calcul du budget

```
budget_net = total_vram_gb - vram_overhead_gb - (total_vram_gb × vram_safety_margin)
           = 48.0          - 2.0              - (48.0 × 0.05)
           = 48.0          - 2.0              - 2.4
           = 43.6 GB disponibles pour les modèles
```

| Couche | GB réservés | Raison |
|--------|-------------|--------|
| Driver NVIDIA + contexte CUDA | ~0.2–0.5 GB | Toujours présent |
| Framework (llama.cpp allocateurs) | ~1.5 GB | Par instance llama-server |
| Marge de sécurité (5%) | 2.4 GB | Pics imprévus / quantisation incomplète |

### Machine à états d'un modèle

```
UNLOADED ──► LOADING ──► READY ──► UNLOADING ──► UNLOADED
               │                        ▲
               │ (erreur)               │
               └──── UNLOADED ──────────┘
```

### Flux de décision avant chargement (`_ensure_capacity`)

```
ensure_model_loaded("llama-3.1-8b")
        │
        ▼
Modèle dans le registre ? ──non──► LookupError → 404
        │
       oui
        ▼
Modèle enabled ? ──non──► PermissionError → 403
        │
       oui
        ▼
Déjà READY ? ──oui──► retourner le manager (fast path, sans lock)
        │
       non
        ▼
[LOCK acquis]
┌─ available_vram ≥ model.vram_gb ? ─┐
│  ET                                 │
└─ pool de ports non vide ?          ─┘
    ──oui──► allouer port, créer ServerManager, lancer
    ──non──► éviction LRU (modèle READY le plus ancien, non pinné)
                    │
                    ├─ modèle idle trouvé → unload → recommencer la vérification
                    ├─ aucun idle mais capacité temporaire possible
                    │  → queue FIFO bornée (défaut : 120s, 100 waiters)
                    ├─ queue expirée ou pleine → 503 + Retry-After
                    └─ modèle > budget VRAM net → RuntimeError 503 immédiat
```

**Point critique** : les deux contraintes (VRAM **et** pool de ports) déclenchent
l'éviction LRU. Si aucune éviction sûre n'est possible parce que les modèles sont
actifs ou en chargement, la requête attend dans la queue d'admission VRAM au lieu
de recevoir immédiatement un 503. La queue est volontairement bornée pour éviter
l'épuisement de connexions en cas d'abus ou de saturation prolongée.

Variables d'environnement :

| Variable | Défaut | Rôle |
|---|---:|---|
| `CAPACITY_QUEUE_ENABLED` | `true` | Active l'attente bornée avant chargement |
| `CAPACITY_QUEUE_TIMEOUT_SECONDS` | `120` | Temps maximal d'attente d'une requête |
| `CAPACITY_QUEUE_MAX_WAITERS` | `100` | Nombre maximal de requêtes en attente |
| `CAPACITY_QUEUE_RETRY_AFTER_SECONDS` | `10` | Valeur de l'en-tête `Retry-After` en cas de 503 queue |

### Éviction LRU

L'algorithme évinçe uniquement les modèles en état `READY` et non pinnés
(aucune requête active en cours). Le modèle avec le `_last_request_time` le plus
ancien est choisi.

**Propriété de sécurité :** une inférence en cours ne peut jamais être
interrompue par l'éviction. `is_pinned` (compteur `_active_requests > 0`) protège
le modèle entre `manager.pin()` (avant proxy) et `manager.unpin()` (dans le finally).
Quand `unpin()` fait retomber ce compteur à zéro, les requêtes en attente sont
réveillées et peuvent retenter l'éviction LRU.

Le moniteur d'inactivité respecte le même invariant : un modèle pinné n'est
jamais déchargé pour idle timeout, même si la génération en cours dure plus
longtemps qu'`IDLE_TIMEOUT_SECONDS` (cas des streams longs). `unpin()` repart
d'une fenêtre idle fraîche à la fin de chaque requête. Pour le streaming, un
pin de garde couvre aussi la fenêtre entre la création de la réponse SSE et le
démarrage effectif du générateur (relâché au premier chunk, ou après 30 s si
le client se déconnecte avant).

### Requête pendant un déchargement (`UNLOADING`)

Le déchargement pour inactivité et la détection de crash partent du
`ServerManager` lui-même : ils ne tiennent ni le verrou par modèle ni
`_capacity_cond`. Pendant la fenêtre `UNLOADING` (SIGTERM, jusqu'à 10 s, puis
SIGKILL), le processus tient encore son port et sa VRAM. Deux invariants
protègent le pool :

- **Un seul manager par modèle.** Une requête qui trouve son modèle en
  `UNLOADING` attend, sous `_capacity_cond`, que le déchargement ait rendu
  port et entrée de pool, puis recharge normalement. L'attente est bornée par
  `CAPACITY_QUEUE_TIMEOUT_SECONDS` (503 + `Retry-After` à l'expiration) ; avec
  `CAPACITY_QUEUE_ENABLED=false`, le refus est immédiat. Un unload qui ne
  converge jamais laisse le modèle refusé plutôt que de lancer un second
  `llama-server` à côté d'un processus qui tient encore sa VRAM.
- **Libération par identité.** `_allocated_ports` est indexé par modèle : le
  callback `on_unload` de chaque `ServerManager` est lié à son instance, et
  `_release_manager()` ne libère que si cette instance est encore le manager
  courant. Un callback tardif d'une instance remplacée est un no-op journalisé
  — même motif que `node_agent._release_manager()`.

Sans ces deux règles, une requête arrivée pendant `UNLOADING` créait un second
manager ; le callback du premier rendait ensuite au pool le port du second,
encore en service (`EADDRINUSE`, ou sonde de santé validant le serveur d'un
autre modèle), retirait le second du pool (VRAM non comptée, budget
sur-engagé) et l'ancien port fuyait définitivement (audit P0-1, 2026-10-19).

---

## Flux d'une requête multi-modèle

### 1. Requête vers un modèle chargé (fast path)

```
Client          nginx           FastAPI/proxy       ModelManager      llama-server[70B]
  │               │                  │                   │                 │
  │─POST /v1/ ──► │                  │                   │                 │
  │ model:"70b"   │─forward ────────►│                   │                 │
  │               │                  │─check auth        │                 │
  │               │                  │─check rate limit  │                 │
  │               │                  │─extract model_id  │                 │
  │               │                  │─ensure_loaded ───►│                 │
  │               │                  │  state==READY ✓   │                 │
  │               │                  │◄─ manager ────────│                 │
  │               │                  │─ POST :8081 ──────────────────────►│
  │               │                  │◄──────────────────────── response ──│
  │               │                  │ (log_usage async)                   │
  │◄──────────────│◄── response ─────│                                     │
```

### 2. Requête avec chargement + éviction LRU

```
Client          FastAPI/proxy     ModelManager     ServerManager[8B]    GPU
  │                  │                 │                  │              │
  │─POST model:8b──►│                 │                  │              │
  │                  │─ensure_loaded─►│                  │              │
  │                  │                │ budget < 5.5 GB  │              │
  │                  │                │ → évict LRU(70B) │              │
  │                  │                │   [70B déchargé] │              │
  │                  │                │ → allouer port   │              │
  │                  │                │ → créer manager ►│              │
  │                  │                │                  │─spawn llama-►│
  │                  │                │                  │─poll /health │
  │                  │                │                  │  [60-90s]    │
  │                  │                │                  │◄─ "ok" ──────│
  │                  │                │◄─ READY ─────────│              │
  │                  │◄─ manager ─────│                  │              │
  │                  │─ POST :8082 ───────────────────────────────────►│
  │◄── response ─────│                │                  │              │
```

### 3. Requêtes concurrentes pendant le chargement

```
Client A        Client B        FastAPI         ModelManager
    │               │              │                  │
    │─POST(70b) ────────────────►  │                  │
    │               │              │─ensure_loaded ──►│  state=LOADING, Event créé
    │               │─POST(70b)──► │                  │
    │               │              │─ensure_loaded ──►│  state==LOADING → await Event
    │               │              │                  │  [les deux attendent]
    │               │              │                  │  ... chargement ...
    │               │              │                  │  Event.set()
    │◄── response ──│◄── response ─│◄─────────────────│  → les deux repartent
```

**Invariant :** aucune requête n'est perdue. Un seul coroutine lance le
subprocess (`asyncio.Lock`), tous les waiters repartent ensemble
(`asyncio.Event`).

La sonde `/health` du chargement est progressive : première sonde immédiate,
puis 100 ms, intervalle ×1,5 plafonné à 1 s
(`server_manager.HEALTH_POLL_*`). L'ancien intervalle fixe de 2 s ajoutait
jusqu'à 2 s à chaque démarrage à froid. La détection de crash et les messages
d'erreur assainis sont inchangés.

---

## Pool de ports dynamique

Chaque modèle chargé consomme un port du pool (`base_llama_port` à
`base_llama_port + max_loaded_models - 1`, défaut : 8081–8085).

```python
# Allocation à la création du ServerManager
port = self._port_pool.pop(0)          # 8081

# Libération via callback on_unload
def _on_model_unloaded(self, model_id):
    port = self._allocated_ports.pop(model_id)
    self._port_pool.append(port)        # 8081 retourné au pool
```

Le callback `on_unload` est appelé par `ServerManager.unload()` après
déchargement complet — quelle que soit la cause (idle timeout, admin, LRU
eviction, shutdown). Cette conception garantit qu'aucun port ne fuit.

**Interaction avec l'éviction VRAM :** le pool de ports est une contrainte
indépendante de la VRAM. `_ensure_capacity` les vérifie ensemble : si tous les
slots sont occupés mais que la VRAM permettrait un modèle supplémentaire, une
éviction LRU est quand même déclenchée pour libérer un port.

---

## Modèles MoE et `--cpu-moe`

Les architectures MoE (Mixture of Experts) ont un volume de poids total bien
supérieur au nombre de paramètres actifs par token. Sans `--cpu-moe`, llama-server
alloue **l'intégralité des experts FFN** en VRAM au démarrage.

```
Modèle MoE 27B — sans --cpu-moe
  Poids GPU = 27B paramètres × 5.5 bits ÷ 8 ≈ 18.6 GB
  → trop pour coexister avec un autre modèle de 26.9 GB (total ≈ 45.5 GB > 43.6 budget)
  → exit code 1 (CUDA OOM) au chargement

Modèle MoE 27B — avec --cpu-moe
  GPU = couches attention + embeddings ≈ 5–8 GB
  CPU = experts FFN (RAM système)
  → coexistence possible sur L40S 48GB
```

**Règle de dimensionnement** : le `vram_gb` déclaré dans `models.yaml` doit
correspondre à la consommation **avec** `cpu_moe` si le flag est activé :

```yaml
- id: "qwen3.5-9b-q5_k_m"    # 9b = 9B paramètres actifs, 27B total
  vram_gb: 7.0                # CORRECT avec cpu_moe: true (attention + KV cache seulement)
  llama_params:
    cpu_moe: true             # experts FFN → RAM CPU
```

Sans `cpu_moe`, `vram_gb: 7.0` serait faux (réalité ≈ 18-28 GB selon le modèle),
le budget VRAM sous-estimerait la consommation, et le processus planterait en cours
de chargement plutôt que d'être refusé par l'éviction LRU.

Le flag `cpu_moe` peut être activé à chaud via
`PATCH /admin/models/{id}` → `{"llama_params": {..., "cpu_moe": true}}`.
Le hot-reload décharge le modèle et le relance avec `--cpu-moe` à la prochaine requête.

---

## Diagnostic de crash — buffer stderr

Quand llama-server s'arrête prématurément (exit code ≠ 0), la raison est dans
son stderr : CUDA OOM, mauvais chemin de modèle, driver incompatible, etc.

`ServerManager` maintient un buffer circulaire des 30 dernières lignes de sortie
(`deque(maxlen=30)` alimenté par `_drain_stream`, lignes tronquées à 8 Kio). Quand
`_wait_for_health` détecte un returncode non nul, il attend 150 ms pour laisser le
drain vider le pipe.

Le tail complet part dans le journal de la gateway au niveau ERROR. Le client
reçoit un `503` au message générique, sans stderr ni chemin de fichier : le
diagnostic se fait donc dans `journalctl -u llm-gateway`.

---

## Décision clé : subprocess vs service séparé

### Pourquoi llama-server n'est pas un service systemd séparé

**Option A — Service systemd (rejeté)**
```
systemd → llm-gateway.service (FastAPI)
systemd → llama-server.service (llama.cpp)  ← redémarré automatiquement
```

Problème : `--sleep-idle-seconds` laisse un contexte CUDA actif (~600 MB).
Pour libérer 100% la VRAM, il faut tuer le processus. Mais systemd le
redémarre immédiatement.

**Option B — Subprocess géré (adopté)**
```
systemd → llm-gateway.service (FastAPI)
               └── subprocesses → llama-server[70B] PID A
               └── subprocesses → llama-server[8B]  PID B
```

La gateway peut tuer et créer des subprocesses à volonté. Le pool de ports
+ le callback `on_unload` garantissent un nettoyage propre.

### Pourquoi `start_new_session=True`

```python
self._process = await asyncio.create_subprocess_exec(
    *cmd,
    start_new_session=True,  # nouveau process group
)
# ...
os.killpg(pgid, signal.SIGTERM)  # tue llama-server + ses enfants, pas la gateway
```

Sans cette option, `os.killpg()` tuerait aussi la gateway elle-même.

---

## Sécurité

### Séparation des clés

```
Utilisateur ──► clé_utilisateur (llmgw-xxx) ──► hash SHA-256 en DB
                                                                │
Gateway ──────► INTERNAL_API_KEY ─────────────► llama-server   │
                (injectée dans chaque                           │
                 llama-server du pool)         (127.0.0.1 only) │
                                                                │
DB stocke uniquement : key_hash, key_prefix (14 chars) ◄───────┘
                       jamais : raw_key
```

### Sécurité du registre des modèles

| Vecteur | Protection |
|---------|-----------|
| Injection YAML | `yaml.safe_load()` obligatoire — jamais `yaml.load()` |
| Path traversal | `path.is_absolute()` + regex model_id sans `/` ni `..` |
| Path traversal via mmproj | `mmproj_path` validé identiquement à `path` (absolu, `.gguf`, `ALLOWED_MODEL_DIRS`) |
| Modèles non autorisés | `ALLOWED_MODEL_DIRS` (liste blanche) si configuré |
| OOM GPU | Budget VRAM strict avec marge 5% avant chaque chargement |
| DoS via modèles | `MAX_LOADED_MODELS` = taille du pool de ports |
| Accès non autorisé | `require_admin` sur tous les endpoints `/admin/models/*` |
| Injection model_id | Regex `^[a-z0-9][a-z0-9._-]{0,62}$` sur tous les model_id |
| GGUF substitué / corrompu | Champ `sha256` optionnel — attestation fail-closed au démarrage puis à chaque transition vers LOADING (SEC-ART-001) |

### Isolation de llama-server

`llama-server` parse des GGUF : il reçoit donc le moins possible de la gateway.

| Mesure | Mise en œuvre |
|--------|---------------|
| Environnement minimal | `child_env.CHILD_ENV_ALLOWLIST`, plus `CUDA_VISIBLE_DEVICES` et `LLAMA_API_KEY`. Aucun secret de la gateway n'est transmis, y compris pendant la calibration du bootstrap. |
| Clé interne jamais prévisible | En mode local, un `INTERNAL_API_KEY` vide ou `CHANGE_ME_*` est remplacé au démarrage par une clé aléatoire, valable pour ce processus. |
| Appels internes hors proxy | `trust_env=False` sur les clients d'inférence, de sonde, de métriques et de plan de contrôle : un `HTTP_PROXY` ne voit passer ni prompt ni secret. |
| Sortie toujours drainée | `_drain_stream` lit par blocs et tronque les lignes démesurées : un pipe plein gèlerait le backend. |

**Limite : même utilisateur système.** `llama-server` tourne sous le même UID que
la gateway. L'allowlist retire les secrets de son propre environnement, mais un
processus compromis peut encore lire `/proc/<gateway>/environ` et l'EnvironmentFile
(640 `root:llmservice`), donc `ADMIN_SECRET` et `AGENT_SECRET`. La fermeture de ce
chemin est suivie dans la [roadmap](../ROADMAP.md) (`SEC-018`).

### Durcissement de llama.cpp (supply-chain)

`llama-server` est un binaire tiers exposé à des CVEs (2025-2026) : écriture
hors-bornes non authentifiée via `n_discard`/context-shift (GHSA-8947-pfff-2f3c)
et overflows de parsing GGUF menant au RCE. Trois garde-fous :

| Mesure | Mise en œuvre |
|--------|---------------|
| `--context-shift` désactivé | `build_llama_cmd` n'émet **jamais** ce flag — c'est le vecteur de la CVE `n_discard`. |
| Épinglage de version | `LLAMA_SERVER_MIN_BUILD` : au démarrage, `llama-server --version` est sondé ; **fail-closed** dès que le plancher est `> 0` — un build inférieur **ou une version illisible** refuse le démarrage (0 = désactivé, la sonde se contente alors d'un avertissement). Même verdict que `doctor`, quel que soit le chemin de démarrage (SEC-009). |
| Intégrité des artefacts | Champ `sha256` optionnel pour le GGUF principal ; `mmproj_sha256` obligatoire avec `vision`. Chaque artefact déclaré est attesté **à chaque transition vers LOADING**, juste avant le lancement du sous-processus — hors event loop (`asyncio.to_thread`), cache attesté clé sur l'identité fichier (taille, mtime, inode + empreinte déclarée), re-stat après hachage et single-flight par clé dans la boucle courante. Le détail reste en journal ; le message client est sanitisé. Le node agent applique les mêmes contrôles avant réservation de port (CLU-002, REG-002). |
| Manifeste recoupé | Un manifeste de provenance §6 posé à côté du binaire ne vaut attestation qu'après confrontation **au binaire lui-même** : version et commit rendus par `--version`, puis empreinte SHA-256 du binaire face à celle consignée dans `install.binary_sha256`. Voir ci-dessous. |

#### Un manifeste non recoupé n'est pas une attestation (SEC-009)

Le manifeste de provenance est un fichier texte posé à côté d'un exécutable.
Rien n'empêche de le recopier d'un autre hôte, ni de le laisser survivre au
remplacement manuel du binaire qu'il décrit. `bootstrap/runtime_installer` le
savait déjà : son contrôle d'idempotence recalcule l'empreinte du binaire posé à
chaque passe. `bootstrap/runtime_resolver._judge_existing_binary` ne le faisait
pas et accordait `reuse_existing` sur parole.

Un binaire en place n'est désormais conservé qu'après quatre confrontations,
toutes nécessaires, du moins cher au plus cher :

| Constat | Ce qui est confronté | Verdict |
|---|---|---|
| `runtime_manifest_build_mismatch` | `version:` du manifeste ↔ build rendu par `--version` | remplacé |
| `runtime_manifest_commit_mismatch` | `commit:` du manifeste ↔ SHA court rendu par `--version` (comparaison par préfixe, le binaire n'en rend que 7 caractères) | remplacé |
| `runtime_backend_mismatch` | backend déclaré ↔ candidats de l'hôte | remplacé |
| `runtime_binary_unattested` | aucun `install.binary_sha256` dans le manifeste | remplacé |
| `runtime_binary_unreadable` | empreinte du binaire incalculable | remplacé |
| `runtime_binary_tampered` | empreinte observée ↔ empreinte consignée | remplacé |

Le refus est toujours **nommé** : la réinstallation depuis la variante épinglée
est le remède, jamais un repli silencieux. Le binaire n'est lu — donc haché — que
si un manifeste est fourni : sans attestation à confronter, lire des centaines de
Mo ne prouverait rien.

Quand `--version` ne rend pas de commit, ce recoupement-là est simplement omis :
on ne peut pas le faire, on ne l'invente pas. Les autres restent.

##### D'où vient le manifeste confronté (SEC-017)

Ces six confrontations n'ont de valeur que si un manifeste leur est réellement
fourni. Elles ne l'étaient pas : `planner._resolve_runtime` ne passait au
résolveur que le chemin du binaire, jamais son manifeste, et le recoupement
n'avait donc lieu que dans les tests. Le planificateur relit désormais le
`provenance.yaml` que `runtime_installer` pose **à côté du binaire**, et en tire
trois issues dégradées, chacune portant son constat dans le plan :

| Constat | Cause | Conséquence |
|---|---|---|
| `runtime_manifest_absent` (`info`) | aucun fichier au chemin attendu — cas nominal d'un binaire compilé à la main | pas d'attestation ; le constat dit **où** le manifeste était attendu |
| `runtime_manifest_unreadable` (`warn`) | droits insuffisants, fichier tronqué, YAML invalide | pas d'attestation |
| `runtime_manifest_invalid` (`warn`) | relu, mais refusé par les règles de §6 | pas d'attestation |

Fail-closed dans les trois cas : **l'absence de manifeste ne vaut jamais
attestation**. Un manifeste approximatif vaut d'ailleurs moins que pas de
manifeste, précisément parce qu'on lui fait confiance.

#### Aucun invariant de production porté par un `assert` (COR-021)

`python -O` retire toutes les instructions `assert` du bytecode, et rien
n'interdit `-O` dans une unité systemd (`ExecStart=… python -O …`, ou
`PYTHONOPTIMIZE=1` dans l'`EnvironmentFile`). Un garde-fou écrit
`assert x is not None` disparaît alors en silence, et le refus explicite se
transforme en `AttributeError` opaque quelques lignes plus loin.

Le code de production des deux composants n'en contient donc aucun : un invariant
dont la violation doit produire une erreur lève une exception nommée
(`ProvenanceError`, `LLMfitError`) ou retourne un refus explicite. Un test balaie
l'AST de tous les modules de `gateway/` et `node_agent/` — tests exclus — et
échoue à la première occurrence. Il porte deux contrôles positifs : le scanner
sait voir un `assert` imbriqué, et son périmètre couvre bien les deux composants.

### Isolation réseau

```
Internet ──► nginx :443 ──► FastAPI :8000 (127.0.0.1 only)
                                    ──► llama-server :8081 (127.0.0.1 only)
                                    ──► llama-server :8082 (127.0.0.1 only)
                                    ...

/admin/* : allow 10.0.0.0/8 (campus) + deny all
```

Tous les llama-server du pool écoutent uniquement sur `127.0.0.1` —
ils ne sont jamais accessibles depuis le réseau, même en cas de
mauvaise configuration nginx.

---

## Base de données SQLite WAL

### Pourquoi SQLite

Pour une centaine d'utilisateurs avec des accès intermittents, SQLite suffit.
Le mode WAL permet des lectures concurrentes pendant les écritures (auth
pendant le log d'usage).

```sql
-- Persistant, posé à l'initialisation (database.py, _PRAGMAS)
PRAGMA journal_mode = WAL;       -- concurrent reads + single writer
-- Par connexion, posés à CHAQUE ouverture (_SESSION_PRAGMAS)
PRAGMA synchronous  = NORMAL;    -- un fsync au checkpoint, pas à chaque COMMIT
PRAGMA cache_size   = -65536;    -- 64MB cache mémoire
PRAGMA temp_store   = MEMORY;
PRAGMA foreign_keys = ON;        -- intégrité référentielle
PRAGMA busy_timeout = 5000;      -- attend le verrou au lieu d'échouer
PRAGMA secure_delete = ON;       -- effacement réel (anonymisation, rétention)
```

`synchronous`, `cache_size` et `temp_store` sont des réglages de connexion :
jusqu'à 2026-11 ils n'étaient posés que sur la connexion de migration, et tout
le trafic tournait en `synchronous=FULL` avec un cache de 2 Mo. En WAL,
`NORMAL` ne peut pas corrompre la base ; une coupure de courant peut perdre
les dernières transactions validées (lignes d'usage récentes).

**Connexions du chemin chaud.** Trois connexions aiosqlite longues, une par
rôle : `read` (`lookup_key`), `quota` (`tokens_used_last_30_days`) et `write`
(`touch_key_last_used`, `log_usage`). Elles sont ouvertes par le lifespan et
fermées au shutdown **après** le flush des tâches d'arrière-plan. Elles sont en
autocommit et chaque lecture est consommée en un seul aller-retour : aucune
transaction ne reste ouverte, donc une révocation écrite par la CLI (autre
processus) est vue dès la requête suivante — il n'y a pas de cache
d'authentification. Les rôles sont séparés parce qu'aiosqlite exécute tout sur
un thread par connexion. Une écriture qui attend le verrou (purge de
rétention, CLI) ne retarde donc pas l'authentification. Une somme de quota
(15 ms pour 800 000 lignes sur 30 jours) ne retarde pas non plus les
authentifications concurrentes, qui passaient sinon de 0,1 ms à 15 ms. La fermeture attend
les opérations en cours sur chaque connexion (bornée, avec un avertissement
au-delà) ; une tâche qui survit au flush passe ensuite par une connexion
dédiée, refermée aussitôt. Tout travail multi-instructions (anonymisation,
migrations, rapports, `VACUUM`) garde une connexion dédiée (`get_db()`).
Remplacer `gateway.db` exige d'arrêter le service, comme la procédure de
restauration le prévoit déjà. Plancher : aiosqlite 0.22 (`Connection.stop()`).

`api_keys.last_used` est écrit au plus une fois par clé et par minute
(`auth.LAST_USED_DEBOUNCE_SECONDS`) : sa précision est d'environ une minute.
L'historique exact des appels est dans `usage_log`.

### Schéma

```
users
  id, username (UNIQUE), email (UNIQUE), created_at
  is_active, rpm_limit, monthly_token_limit, notes

api_keys
  id, user_id (FK → users), key_hash (UNIQUE), key_prefix
  name, created_at, last_used, is_active, expires_at

usage_log
  id, user_id (FK), api_key_id (FK), timestamp
  model, prompt_tokens, completion_tokens, total_tokens
  duration_ms, status_code, request_id

Index : usage_log(user_id, timestamp, total_tokens), usage_log(timestamp),
        api_keys(key_hash), api_keys(user_id)
```

Le champ `model` dans `usage_log` stocke l'ID du modèle tel que résolu
par le routing (ex : `"llama-3.3-70b-instruct"`), permettant les rapports
d'usage par modèle.

L'index `idx_usage_user_time_tokens` (migration 3) **couvre** le contrôle de
quota mensuel, exécuté à chaque requête d'un compte limité : la somme des
`total_tokens` sur 30 jours est lue dans l'index, sans accès à la table
(21 ms → 3,6 ms pour 200 000 lignes sur la fenêtre). Il remplace
`idx_usage_user_time`, son préfixe strict. L'aperçu du dashboard
(`get_overview_stats`, rafraîchi toutes les 30 s par onglet) borne son
parcours à sa fenêtre la plus large, 7 jours, par un prédicat sur
`idx_usage_timestamp` au lieu de lire toute la table.

### Migrations versionnées

Un `CREATE TABLE IF NOT EXISTS` n'atteint **jamais** une base déjà créée : sans
mécanisme de migration, une base déployée conserve indéfiniment son schéma
d'origine et aucun changement de contrainte ne l'atteint. `gateway/database.py`
embarque donc un moteur de migration versionné.

#### Versionnement

La version du schéma est portée par `PRAGMA user_version`, un entier stocké
dans l'en-tête du fichier SQLite :

| `user_version` | Signification |
|---|---|
| `0` | base neuve, ou base déployée avant l'introduction du mécanisme |
| `N` | les migrations `1..N` ont été appliquées |

Le code déclare sa version cible dans `SCHEMA_VERSION`, dérivée de la dernière
entrée du tuple `MIGRATIONS`. `init_db()` amène la base à cette version puis
n'a plus aucun effet : il est idempotent et peut être appelé à chaque démarrage
comme à chaque commande CLI.

#### Structure d'une migration

```python
@dataclass(frozen=True)
class Migration:
    version: int                  # user_version atteint après application
    description: str              # journalisée ; jamais de donnée personnelle
    statements: tuple[str, ...]   # SQL exécuté instruction par instruction
    apply: Callable | None        # hook Python, même transaction
    check_foreign_keys: bool      # PRAGMA foreign_key_check avant COMMIT
```

Règles :

- **Ajouter** une entrée en fin de tuple `MIGRATIONS`, avec `version` égale à la
  précédente + 1. Ne **jamais** réécrire une entrée déjà livrée : une base en
  production l'a déjà appliquée et ne la rejouera pas.
- `statements` est un tuple d'instructions, pas un script. `executescript()` de
  `sqlite3` valide implicitement la transaction en cours : l'utiliser
  romprait l'atomicité de la migration.
- `apply` est nécessaire dès que la migration doit inspecter l'état réel de la
  base (plutôt que de le supposer) ou recréer une table.
- `check_foreign_keys=True` sur toute migration qui recrée une table. Il est à
  `False` par défaut car une base historique peut porter des violations
  préexistantes, sans lien avec la migration : le démarrage du service ne doit
  pas en dépendre.

#### Transactionnalité et clés étrangères

Chaque migration s'exécute dans un `BEGIN IMMEDIATE` qui englobe son SQL, son
hook Python **et** l'écriture de `user_version` : `user_version` n'avance
qu'avec la migration, et un échec annule tout. Les migrations déjà validées ne
sont pas perdues — la base s'arrête simplement à la dernière version appliquée.

`PRAGMA foreign_keys` est **silencieusement ignoré à l'intérieur d'une
transaction**. Le moteur le positionne donc à `OFF` **avant** d'ouvrir la
première transaction, pour toute la série de migrations, et le restaure à `ON`
dans un `finally`. C'est une nécessité du motif de recréation de table : le
`DROP TABLE` de l'ancienne table casserait sinon les références. Les connexions
applicatives (`get_db()`) rétablissent `foreign_keys = ON` à chaque ouverture.

`user_version` est relue **sous le verrou d'écriture**, après le
`BEGIN IMMEDIATE` : si un autre processus (service et CLI démarrés en parallèle)
a appliqué la migration entre-temps, elle est ignorée au lieu d'être rejouée.
Une migration de recréation de table n'est donc jamais exécutée deux fois.

#### Recréer une table pour changer une contrainte

SQLite ne sait pas modifier une clé étrangère par `ALTER TABLE`. Le seul motif
possible est *create new → copy → drop old → rename*, encapsulé par le helper
`_rebuild_table()`. Exemple complet — passer `usage_log.user_id` en
`ON DELETE CASCADE` :

```python
async def _migration_usage_log_cascade(db: aiosqlite.Connection) -> None:
    """Recrée usage_log pour supprimer les lignes d'un utilisateur supprimé."""
    await _rebuild_table(
        db,
        table="usage_log",
        create_new_sql="""
            CREATE TABLE usage_log__new (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id             INTEGER NOT NULL
                                    REFERENCES users(id) ON DELETE CASCADE,
                api_key_id          INTEGER
                                    REFERENCES api_keys(id) ON DELETE SET NULL,
                timestamp           TEXT    NOT NULL DEFAULT (datetime('now')),
                model               TEXT    NOT NULL,
                prompt_tokens       INTEGER NOT NULL DEFAULT 0,
                completion_tokens   INTEGER NOT NULL DEFAULT 0,
                total_tokens        INTEGER NOT NULL DEFAULT 0,
                duration_ms         INTEGER,
                status_code         INTEGER,
                request_id          TEXT
            )
        """,
        copy_columns=(
            "id", "user_id", "api_key_id", "timestamp", "model",
            "prompt_tokens", "completion_tokens", "total_tokens",
            "duration_ms", "status_code", "request_id",
        ),
        index_statements=(
            "CREATE INDEX idx_usage_user_time ON usage_log(user_id, timestamp)",
            "CREATE INDEX idx_usage_timestamp ON usage_log(timestamp)",
        ),
    )


MIGRATIONS: tuple[Migration, ...] = (
    # … migrations existantes, inchangées …
    Migration(
        version=2,
        description="usage_log.user_id en ON DELETE CASCADE",
        apply=_migration_usage_log_cascade,
        check_foreign_keys=True,
    ),
)
```

Points d'attention pour ce motif :

- `create_new_sql` doit créer la table sous le nom `<table>__new`.
- Les index de l'ancienne table disparaissent avec le `DROP` :
  `index_statements` doit les redéclarer (mêmes noms, la table étant supprimée).
- `copy_columns` liste explicitement les colonnes copiées ; c'est aussi le point
  d'entrée pour transformer une valeur (`SELECT` implicite colonne par colonne).
- Si des lignes orphelines préexistent, `check_foreign_keys=True` fera échouer
  la migration : nettoyer les orphelins dans la même migration, avant l'appel.

#### Sauvegarde préalable

Avant la **première migration réellement applicable**, le moteur produit une
sauvegarde. Aucune copie n'est faite si la base est déjà à jour, ni si elle est
neuve (rien à protéger).

- Chemin : `<db_path>.pre-migration.v<version_origine>.<horodatage UTC>.bak`,
  par exemple `gateway.db.pre-migration.v0.20260730T101500Z.bak`.
- Méthode : API de sauvegarde SQLite (`Connection.backup()`), pas une copie de
  fichier — en mode WAL, le fichier principal seul est incomplet.
- Permissions : fichier créé en `0600` avant toute écriture, puis restreint à
  l'intersection avec le mode de la base. Une sauvegarde n'est jamais plus
  largement accessible que la base elle-même.
- `O_EXCL` : une sauvegarde existante n'est jamais écrasée.

Ces sauvegardes contiennent une copie complète de la base (users, api_keys,
usage_log) : leur rétention est **bornée** à `MIGRATION_BACKUPS_TO_KEEP`
(2 par défaut, minimum 1). La purge suit chaque migration réussie et chaque
passe de rétention du lifespan — sans elle, une copie des données personnelles
survivrait indéfiniment à `anonymize_user` (DEC-001). Seul le motif
`<db>.pre-migration.` est purgé : jamais les sauvegardes `.pre-admin.` /
`.pre-bootstrap.` du registre ni les archives du script de sauvegarde. La plus
récente est toujours conservée : la procédure de rollback ci-dessous s'appuie
sur elle.

#### Comportement en cas d'échec — fail-closed

Toute erreur remonte sous forme de `MigrationError` depuis `init_db()`, donc
depuis le lifespan FastAPI : **le service ne démarre pas** sur une base à moitié
migrée. Deux cas explicites :

| Situation | Comportement |
|---|---|
| Migration qui échoue | `ROLLBACK`, `user_version` inchangée, `MigrationError` |
| `user_version` > `SCHEMA_VERSION` | refus immédiat, aucune écriture, message désignant le rollback applicatif |

Le second cas correspond à un retour arrière de version du code sur une base
déjà migrée. Il produit une erreur claire plutôt qu'une corruption silencieuse.

Les journaux tracent la version d'origine, la version cible, la description de
chaque migration et le chemin de la sauvegarde — jamais de donnée personnelle ni
de secret.

#### Procédure opérateur

Migration normale (déploiement) :

1. Arrêter le service (`systemctl stop llm-gateway`) — la migration s'exécute au
   démarrage et ne doit pas concurrencer un service actif.
2. Déployer le nouveau code, puis démarrer le service.
3. Vérifier le journal : `journalctl -u llm-gateway | grep "Migration SQLite"`.
4. Contrôler la version atteinte :
   `sqlite3 /var/lib/llm-gateway/gateway.db "PRAGMA user_version;"`.

En cas d'échec de migration :

1. Le service ne démarre pas ; lire la `MigrationError` dans le journal.
2. La base est restée dans son état d'avant la migration fautive — elle est
   exploitable par la version précédente du code si celle-ci acceptait cette
   `user_version`.
3. Sinon, restaurer la sauvegarde produite juste avant :
   ```bash
   systemctl stop llm-gateway
   cp gateway.db.pre-migration.v0.20260730T101500Z.bak gateway.db
   rm -f gateway.db-wal gateway.db-shm
   ```
4. Redéployer la version de code correspondant au schéma restauré.

---

## Rate limiting in-memory

```python
# rate_limiter.py — sliding window log
_windows: dict[int, deque[float]] = {}
# user_id → deque de timestamps dans la fenêtre d'1 minute
```

**Propriété :** si la gateway redémarre, les compteurs se remettent à zéro.
Acceptable — les limites sont par minute.

---

## Streaming SSE — flux technique

```
Client             nginx               FastAPI             llama-server
  │                  │                    │                      │
  │─POST stream:true►│                    │                      │
  │                  │─forward ──────────►│                      │
  │                  │                    │─routing model_id     │
  │                  │                    │─POST :808X ─────────►│
  │                  │                    │                      │─generate
  │                  │                    │◄── chunk1 ───────────│
  │                  │◄── chunk1 ─────────│                      │─generate
  │◄── chunk1 ────── │                    │                      │
  ...               ...                  ...                    ...
  │◄── data: [DONE] ─│                    │                      │
```

**Cadrage.** Chaque événement SSE du backend (ses lignes non vides, jusqu'à la
ligne vide) est relayé en **un seul** chunk et un seul envoi ASGI : lignes
jointes par `\n`, terminateur `\n\n`. Le relais ligne à ligne émettait deux
envois par token (la ligne, puis un `\n` isolé), soit trois sauts de ligne
côté client, et aurait coupé un événement multi-lignes (`event:` + `data:`).
Seules les lignes `data:` JSON sont réécrites (`model` public, capture de
`usage`, TTFT) ; un dernier événement sans ligne vide finale est relayé tel
quel.

**Échecs du data-plane.** Un timeout renvoie `504`, une autre erreur de
transport `502`, comme avant. Mais seul un échec qui **prouve** un backend
injoignable — connexion refusée ou trop lente (`ConnectError`,
`ConnectTimeout`), socket coupée (`ReadError`, `WriteError`), protocole rompu
(`RemoteProtocolError`) — est signalé au gestionnaire
(`report_backend_failure`) : en mode cluster, ce signal invalide le placement
et déclenche évictions et rechargement à froid. Un `ReadTimeout` (génération
plus longue que `read=600s`) ou un `PoolTimeout` (pool de la gateway saturé)
ne le déclenchent plus.

**Points critiques nginx :**
```nginx
proxy_buffering        off;
add_header X-Accel-Buffering no always;
chunked_transfer_encoding on;
proxy_read_timeout     900s;   # dérivé du load_timeout_seconds max du registre
```

---

## Paramètres llama-server — justification

Les paramètres sont maintenant **par modèle** (dans `models.yaml`),
non plus globaux. Les valeurs ci-dessous correspondent au modèle 70B par défaut.

### `-ngl 999` (GPU layers)

Sentinel signifiant "tout en GPU". Plafonné automatiquement au nombre
réel de couches du modèle par llama.cpp.

### `-c 32768 --parallel 4` (contexte et parallélisme pour 70B)

```
ctx_size = tokens_per_slot × n_parallel
32768    = 8192             × 4

VRAM KV cache (Q8) ≈ 2 × 80 couches × 8 têtes × 128 dim × 32768 × 1 octet
                   ≈ ~2.7 GB
```

Pour le modèle 8B, on peut passer à `parallel: 8` car le budget VRAM
restant est bien plus large.

### `-ctk q8_0 -ctv q8_0` (KV cache quantization)

KV cache FP16 (défaut) → ~5 GB pour ce contexte.
En Q8_0 → ~2.7 GB. Dégradation perplexité : +0.003 (imperceptible).

### `-fa on` (Flash Attention)

Supporté sur Ada Lovelace (compute capability 8.9). Réduit la mémoire
d'attention de O(n²) à O(n), améliore le débit prefill de ~15%.

### `--cont-batching` (continuous batching)

Permet à chaque slot d'avancer indépendamment — GPU utilisé de façon
optimale même avec des requêtes de longueurs variables.

---

## Performance du chemin chaud — client HTTP partagé

Toutes les requêtes d'inférence sont proxifiées vers les sous-processus
`llama-server` par un **unique `httpx.AsyncClient` partagé par processus**
(`gateway/proxy.py`). Ce client est créé une fois au démarrage (`lifespan` →
`init_http_client()`) et fermé au shutdown (`aclose_http_client()`) — **jamais**
recréé par requête. Un client par requête rouvrirait le pool de connexions à
chaque appel, exactement le gaspillage qu'on élimine.

Le pool keep-alive évite un handshake TCP par requête vers
`127.0.0.1:<port_llama>`. Le mode non-streaming emprunte une connexion via
`client.post(...)` ; le mode streaming l'emprunte via `client.stream(...)` le
temps du stream puis la rend au context-exit. Seule la connexion empruntée est
libérée — le client partagé reste ouvert.

**Déconnexion du client en non-streaming.** uvicorn n'annule pas un handler
non streamant quand le client raccroche : la génération allait à son terme,
slot GPU occupé et modèle pinné, pour une réponse que personne ne lirait
(sans borne si `MAX_NEW_TOKENS_PER_REQUEST=0`). L'appel upstream court donc
contre l'arrivée de `http.disconnect` sur le canal `receive` de la requête
(le corps est déjà lu : c'est le seul message possible). Si la déconnexion
gagne, la requête httpx est annulée — la connexion fermée libère le slot
llama.cpp —, une ligne d'usage `499` est écrite comme en streaming, et le pin
est relâché par le `finally` habituel. Vérifié sous un vrai uvicorn (h11 et
httptools) avec le middleware HTTP de `main.py` (`BaseHTTPMiddleware`), en
FastAPI 0.136/Starlette 1.2 comme aux versions verrouillées (0.141/1.3) ;
le mécanisme dépend du relais de `http.disconnect` par ce middleware, couvert
par `tests/test_non_stream_proxy.py`.

**Grosses réponses JSON.** Au-delà de 256 Kio (embeddings), le parse, la
réécriture de `model` et la sérialisation ne se font plus d'un seul tenant.
Le premier niveau du document est parcouru à la main, et chaque tableau de
premier niveau est traité en appels C bornés. Un tableau de petits éléments
(`/v1/tokenize`) est découpé en fenêtres d'environ 64 Kio. Un tableau de gros
éléments (vecteurs d'embeddings) est traité élément par élément. La main est
rendue à la boucle toutes les 2 ms. Le coût CPU total reste proche de celui
des codecs C, à 10–20 % près. `asyncio.to_thread` n'aurait rien changé : les
codecs C du module `json` gardent le GIL pendant tout l'appel (mesuré : même
gel de 84 ms pour 5,5 Mo). Les octets renvoyés sont identiques à ceux de
`JSONResponse`. Limite connue : un gros tableau imbriqué plus bas que le
premier niveau (embeddings sans pooling, `{"data":[{"embedding":[[…]]}]}`)
reste traité d'un bloc.

Dimensionnement (bornes `httpx.Limits`, réglables par variable d'environnement) :

| Variable | Défaut | Rôle |
|----------|--------|------|
| `HTTPX_MAX_CONNECTIONS` | `200` | Connexions totales max du pool (`0` = illimité, déconseillé en prod) |
| `HTTPX_MAX_KEEPALIVE` | `100` | Connexions keep-alive conservées au repos (`0` = illimité) |
| `HTTPX_KEEPALIVE_EXPIRY` | `30.0` | Durée de vie (s) d'une connexion keep-alive inactive |

Les défauts laissent une marge large au-dessus de `MAX_LOADED_MODELS` pour
absorber le parallélisme par modèle (`parallel` slots × plusieurs modèles).
Le timeout d'inférence reste distinct (`connect=10s`, `read=600s`, `write=60s`,
`pool=5s`) pour tolérer les générations longues.

Les configurations gateway et node-agent sont validées avant le démarrage :
valeurs numériques finies, budget VRAM net positif, ports dans `1..65535` sans
collision avec le pool llama-server, quotas représentables par SQLite, timeouts
positifs et invariants croisés des pools et des drains. Les booléens ne sont pas
acceptés comme nombres par conversion implicite (CFG-001).

---

## Robustesse du cycle de vie (shutdown, VRAM, orphelins)

Trois mécanismes best-effort durcissent le cycle de vie du `LocalModelManager`
sans impacter le chemin chaud. Tous sont **non fatals** et inertes en test
(pas de GPU, pas de `nvidia-smi`, ports libres).

### Drain borné au SIGTERM

`shutdown()` appelle `_drain_pinned(timeout)` avant de décharger les modèles :
il attend (borné) que les modèles **pinnés** (requêtes actives, y compris
streams) libèrent leurs requêtes, par polling court. Si rien n'est pinné, retour
immédiat. Au-delà du délai, déchargement forcé avec un warning.

| Variable | Défaut | Rôle |
|----------|--------|------|
| `SHUTDOWN_DRAIN_TIMEOUT_SECONDS` | `25.0` | Attente max des requêtes actives avant déchargement forcé (`0` = pas d'attente) |
| `SHUTDOWN_DRAIN_POLL_SECONDS` | `0.2` | Intervalle de poll pendant le drain |
| `SHUTDOWN_BACKGROUND_FLUSH_SECONDS` | `5.0` | Deadline du flush des usages fire-and-forget au SIGTERM (`0` = pas d'attente) |

En complément, les lignes d'usage journalisées en fire-and-forget juste avant
l'arrêt peuvent encore être en vol au SIGTERM : après `model_manager.shutdown()`
et **avant** la fermeture du client HTTP, le lifespan attend au plus
`SHUTDOWN_BACKGROUND_FLUSH_SECONDS` la fin des tâches d'arrière-plan
(`background.drain_pending`). Les tâches non terminées à l'expiration ne sont
pas annulées — un warning non fatal liste celles qui restent.

### Réconciliation VRAM via `nvidia-smi`

Une tâche périodique (`start_vram_reconcile()`) compare la VRAM réelle mesurée
par `nvidia-smi` à la somme des `vram_gb` déclarés des modèles READY. La sonde
conserve l'UUID de chaque GPU et agrège uniquement les devices sélectionnés par
`CUDA_VISIBLE_DEVICES`, dans l'ordre déclaré. Une charge située sur un GPU
masqué ne fausse donc plus la dérive EVA. Une dérive significative (VRAM réelle
> déclaré × (1 + seuil), et > 512 Mo de bruit) déclenche un **warning** invitant
à chercher des `llama-server` orphelins. La mesure ne déclenche aucune éviction,
mais sa VRAM libre borne désormais la capacité d'admission : EVA retient le
minimum entre le budget configuré restant et la mémoire physique libre. Si la
sonde est indisponible, le comportement historique fondé sur la configuration
reste actif afin de ne pas rendre un environnement CPU-only inutilisable.

La mesure périodique compte encore la mémoire d'un modèle évincé ou déchargé
depuis la dernière sonde. Elle ne décide donc jamais seule : quand le budget
configuré admettrait le modèle mais pas la mesure en mémoire, EVA re-sonde
`nvidia-smi` avant d'évincer ou de mettre la requête en attente. Si le GPU est
réellement occupé (processus tiers, pilote qui tarde à rendre la mémoire d'un
processus tué), la requête en tête de queue re-sonde chaque seconde jusqu'à
l'admission ou au timeout de la queue. Aucune sonde n'est lancée sur le chemin
nominal, où mesure et budget s'accordent.

Le bloc admin `vram_budget.gpu_measurement` publie l'inventaire par UUID et un
état `measured` ou `unavailable` avec une raison stable. Une sonde absente,
échouée, expirée ou partielle n'est jamais convertie en zéro et remplace tout
ancien snapshot devenu obsolète. Les anciens champs
`gpu_used_mb_measured`/`vram_drift_mb` restent présents après une mesure valide.
Les UUID MIG et un GPU parent dont `mig.mode.current=Enabled` sont refusés avec
`mig_unsupported` tant qu'une sonde dédiée ne permet pas d'éviter le double
comptage parent/instance.

| Variable | Défaut | Rôle |
|----------|--------|------|
| `VRAM_RECONCILE_INTERVAL_SECONDS` | `60.0` | Intervalle entre deux sondes (`0` = désactivé) |
| `VRAM_RECONCILE_PROBE_TIMEOUT_SECONDS` | `5.0` | Timeout de la sonde `nvidia-smi` |
| `VRAM_RECONCILE_DRIFT_THRESHOLD` | `0.15` | Seuil de dérive relative (0.15 = +15 %) |

### Détection des `llama-server` orphelins au démarrage

`detect_orphan_ports()` teste, au démarrage, si des ports du pool sont déjà
occupés (survivants d'un crash gateway). Par défaut, il **LOG seulement** —
aucun processus n'est tué. Le passage à `KILL_ORPHAN_LLAMA_ON_STARTUP=true`
active un kill best-effort strictement borné : port du pool **et** ligne de
commande contenant `llama-server`, via `psutil` (dépendance optionnelle ; si
absente, l'orphelin n'est pas nettoyé et un warning invite au nettoyage manuel).

| Variable | Défaut | Rôle |
|----------|--------|------|
| `KILL_ORPHAN_LLAMA_ON_STARTUP` | `false` | Tenter de tuer les `llama-server` orphelins sur les ports du pool (nécessite `psutil`) |

---

## Couche monitoring (dashboard)

### Flux de données — multi-modèles

```
Navigateur admin
  │
  ├─ GET /admin/dashboard ──► HTMLResponse(dashboard.html)
  │
  └─ GET /admin/metrics/overview
     GET /admin/metrics/llama         ← interroge TOUS les modèles READY
            │
            ├─ usage_log / users (SQLite)
            │
            └─ pour chaque ServerManager READY :
                 GET http://127.0.0.1:{port}/metrics
                 → résultat indexé par model_id
```

### Calcul des percentiles de latence

SQLite ne supporte pas `PERCENTILE_CONT`. Calcul en Python :

```python
samples = await db.get_latency_samples(period_hours=168, limit=10_000)
samples.sort()
p95 = samples[int(0.95 * len(samples))]
```

Le chemin chaud ajoute aussi trois histogrammes en mémoire exposés par
`/admin/metrics/prometheus` : TTFT du premier delta SSE significatif (contenu,
raisonnement ou appel d'outil), durée de chargement du modèle et attente de
capacité locale. Le TTFT démarre avant la lecture du body et inclut donc
l'attente VRAM ainsi que le chargement à la demande ; un chunk de rôle ou
d'usage sans delta significatif ne le clôt pas. En cluster, le chargement
et le TTFT portent le `node_id` du placement. Les labels sont volontairement
limités à `model`, `node` et un résultat technique, avec une borne stricte de
512 séries par histogramme.

### Sécurité du dashboard

- Pas de contenu de prompt ou de réponse
- Pas de clé API (ni hash ni préfixe)
- Token admin dans `sessionStorage` (jamais `localStorage`)
- Chart.js est vendored et servi par la gateway : le dashboard d'incident ne
  dépend d'aucun CDN, font provider ou accès Internet.
- Une CSP et des headers anti-framing/nosniff protègent la page. Le HTML est
  servi sans auth — les données JSON exigent toujours le bearer token.

---

## Opérations de sécurité

### Rotation de l'ADMIN_SECRET

```bash
NEW_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
sudo sed -i "s/^ADMIN_SECRET=.*/ADMIN_SECRET=$NEW_SECRET/" /etc/llm-gateway/env
sudo systemctl restart llm-gateway
```

### Révocation d'urgence de tous les accès

```bash
sqlite3 /var/lib/llm-gateway/gateway.db \
  "UPDATE api_keys SET is_active = 0;"
# Effet immédiat — aucun redémarrage nécessaire
```

### Audit des accès suspects

```bash
# Consommation par modèle ce mois
sqlite3 /var/lib/llm-gateway/gateway.db "
SELECT model, COUNT(*) as reqs, SUM(total_tokens) as tokens
FROM usage_log
WHERE timestamp >= date('now', 'start of month')
GROUP BY model ORDER BY tokens DESC;"

# Top 10 utilisateurs par tokens
sqlite3 /var/lib/llm-gateway/gateway.db "
SELECT u.username, l.model, COUNT(*) as reqs, SUM(l.total_tokens) as tokens
FROM usage_log l JOIN users u ON u.id = l.user_id
WHERE l.timestamp >= date('now', 'start of month')
GROUP BY u.id, l.model ORDER BY tokens DESC LIMIT 10;"
```

---

## Architecture cluster multi-nœuds (opt-in avancé)

> Activé par `CLUSTER_MODE=cluster`. Le mode `local` (défaut) est inchangé.

### Frontière entre les deux produits

La même API et le même registre alimentent deux backends exclusifs, choisis au
démarrage. Le déploiement matérialise cette frontière avec deux profils systemd :

| Invariant | `local` | `cluster` |
|-----------|---------|-----------|
| `llama-server` | sous-processus du gateway | sous-processus des agents seulement |
| GPU sur l'hôte gateway | requis, devices NVIDIA autorisés | non requis, `PrivateDevices=true` |
| `/models` sur le gateway | requis | inutile |
| Plan de contrôle | in-process | HTTPS `:9443` vers les agents |
| Plan de données | loopback `:8081-8085` | LAN `:8081-8085`, limité à l'orchestrateur |

`install.sh` choisit `local` par défaut uniquement pour une installation
neuve. `update.sh` relit et conserve le mode existant. Une transition demande
`--mode` et `--allow-mode-change`; le rollback restaure simultanément code,
venv, mode et unité. Les agents ont un cycle de déploiement séparé : aucune
mise à jour orchestrateur ne pousse du code sur les nœuds.

### Vue d'ensemble

```
                  Client OpenAI-compatible
                          │ HTTPS public (TLS 1.3, nginx)
                          ▼
              ┌───────────────────────────────────┐
              │   Orchestrateur (FastAPI)          │
              │   auth, rate limit, DB SQLite      │
              │   ClusterManager                   │
              │   Routes /v1/*, /admin/*           │
              └─────────────────┬─────────────────┘
                                │ HTTPS :9443 (Bearer agent_secret)
              ┌─────────────────┴─────────────────┐
              ▼                                   ▼
     ┌──────────────────┐                ┌──────────────────┐
     │  Node Agent A    │                │  Node Agent B    │
     │  FastAPI :9443   │                │  FastAPI :9443   │
     │  load / unload   │                │  load / unload   │
     │  health (VRAM)   │                │  health (VRAM)   │
     └────────┬─────────┘                └────────┬─────────┘
              │                                   │
              ▼  subprocess local                 ▼  subprocess local
     ┌──────────────────┐                ┌──────────────────┐
     │ llama-server     │                │ llama-server     │
     │ :8081  :8082 …   │                │ :8081  :8082 …   │
     │ GB10 — 128 GB    │                │ GB10 — 128 GB    │
     │ unifiée CPU/GPU  │                │ unifiée CPU/GPU  │
     └──────────────────┘                └──────────────────┘
                        ▲
         Trafic d'inférence SSE direct (orchestrateur → llama-server)
         L'agent retourne llama_url + internal_api_key dans LoadResponse.
         L'orchestrateur ouvre une connexion HTTP directe vers llama-server
         pour éviter un double-hop sur les flux SSE longs.
```

### Deux plans séparés

| Plan | Participants | Protocole | Volume |
|------|-------------|-----------|--------|
| Contrôle | orchestrateur ↔ agent | HTTPS + Bearer | Faible (load/unload/health) |
| Données | orchestrateur ↔ llama-server | HTTP LAN | Élevé (tokens SSE) |

Chaque node-agent rafraîchit en arrière-plan le même inventaire GPU par UUID.
Le heartbeat reste une lecture mémoire sans sous-processus ; son champ optionnel
`gpu_measurement` permet une mise à jour progressive des agents et de
l'orchestrateur. L'endpoint protégé `GET /agent/gpus` force au besoin une
actualisation et expose le diagnostic détaillé.

Le registre est central mais les artefacts ne le sont pas : `path` et
`mmproj_path` doivent résoudre vers des fichiers identiques, aux mêmes chemins
absolus, sur chaque nœud éligible. Le scheduler ne transfère pas les GGUF.

### Scheduler (placement automatique)

`gateway/cluster/scheduler.py` contient la logique **pure** de placement (pas d'I/O) :

1. **Best-fit immédiat** : nœuds avec VRAM libre suffisante + port libre →
   on choisit celui avec le moins de résidu (optimise le packing).
2. **Éviction LRU simulée** : si aucun nœud n'a assez de VRAM libre, on simule
   l'éviction des modèles les moins récemment utilisés et on choisit le nœud
   qui doit évincer le moins de VRAM (moins de churn).
3. **Contrainte de pin** : `pin_to_node` force le placement sur un nœud précis.

### Heartbeat robuste & dégradation gracieuse

- Toutes les `CLUSTER_HEALTH_INTERVAL` secondes (défaut 10 s) :
  `GET /agent/health` vers chaque nœud.
- ≥ `CLUSTER_HEALTH_FAILURES_TO_OFFLINE` (défaut 3) échecs consécutifs →
  nœud marqué `offline`. Plus aucune requête routée vers lui.
- **Heartbeat robuste** : toute exception inattendue du client (pas seulement
  `NodeUnreachableError`) compte comme un échec de heartbeat. Sans cela, un nœud
  réellement KO mais qui lève une erreur non prévue resterait `online`
  indéfiniment.
- Retour à `online` dès qu'un health répond OK (les échecs consécutifs sont
  remis à zéro).
- L'orchestrateur **ne recharge pas proactivement** un modèle après le retour
  d'un nœud : le rechargement se fait à la demande (prochaine requête ou
  `POST /admin/models/{id}/load`).

### Failover rapide sur nœud offline

Le rechargement paresseux est complété par un **failover à la requête** dans
`ensure_model_loaded` : si le placement enregistré pour un modèle pointe vers un
nœud devenu `offline` (crash entre deux heartbeats), l'entrée est invalidée et un
**placement frais** est calculé, qui choisira un autre nœud `online`. On ne sert
donc jamais un handle vers un nœud offline. Un stream déjà en cours au moment du
crash reçoit une erreur réseau ; la requête suivante est replacée automatiquement.

### Réconciliation d'état au démarrage

Après un redémarrage de l'orchestrateur, des `llama-server` peuvent encore
tourner sur les nœuds. `start_health_monitor()` effectue un premier
`_check_all_nodes()` puis `_reconcile_state()` : pour chaque nœud `online`, il
lit `GET /agent/status` et reconstruit `_placement` / `loaded` à partir des
modèles réellement `ready`. Cela évite les placements fantômes et les
rechargements redondants.

L'`internal_api_key` du `llama-server` n'étant pas exposée par `status()`, une
entrée réconciliée porte une clé vide (sentinelle « à rafraîchir »). À la première
requête sur ce modèle, un chargement **idempotent** (`start_load_model`,
`already_loaded` côté agent, pas de rechargement réel) récupère la vraie clé et
l'URL de confiance. L'entrée existante est mise à jour **en place** : les
handles déjà distribués gardent leurs pins.
L'URL `llama-server` est toujours reconstruite à partir de l'hôte du `base_url`
du nœud (source de confiance `nodes.yaml`), jamais d'une valeur renvoyée par
l'agent.

### Déchargement sans purge optimiste

Si un `unload_model` échoue (nœud flaky), l'état local **n'est pas purgé de façon
optimiste** : le `llama-server` tourne peut-être encore et occupe sa VRAM.
`_resync_after_failed_unload` re-synchronise via `health()` et ne libère l'entrée
locale **que** si le nœud confirme l'absence du modèle. Sinon la VRAM reste
comptée comme occupée pour éviter une sur-réservation au placement suivant
(CLU-001).

Tant que la disparition n'est pas confirmée, le modèle passe à l'état
`unload_uncertain` (`loaded[...].unload_uncertain = True`, visible dans le statut
cluster) :

- `DELETE /admin/models/{id}/unload` répond **503** avec l'erreur typée
  `ClusterUnloadUncertainError` au lieu d'annoncer une VRAM libérée à tort ;
- toute nouvelle tentative de chargement échoue immédiatement — pas de routage
  vers une URL potentiellement disparue, pas de copie du modèle sur un autre
  nœud — jusqu'à ce que l'incertitude soit levée.

Le heartbeat la lève dans les deux sens. Si le nœud ne rapporte plus le
modèle, la disparition est confirmée et l'entrée purgée. S'il le rapporte
encore, le heartbeat déclenche un `GET /agent/status` : un modèle `ready` sous
la **même** `generation_id` prouve que l'unload n'a pas eu lieu, le drapeau est
retiré et le modèle redevient routable. Une autre génération ne lève pas le
drapeau.

`POST /admin/models/{id}/unload` décharge **toutes** les copies suivies du
modèle sur les nœuds online (route primaire, doublon revenu d'un nœud offline,
copie suspecte ou ancienne génération retirée du placement), et refuse en bloc
(409) si l'une d'elles traite encore une requête. Les nœuds offline sont
sautés et journalisés ; leur copie est réconciliée comme doublon évictable à
leur retour. Avant, seul `_placement` était consulté : une copie suspecte ou
d'ancienne génération restait en VRAM alors que la route annonçait « VRAM
libérée ».

### Inventaire, pins et comptabilité VRAM (audit cluster 2026-11)

- **Pins préservés.** Un replacement sur le nœud qui détient déjà l'entrée
  (réponse `already_loaded`, refresh d'une entrée suspecte ou réconciliée) met
  à jour le `_LoadedInfo` existant au lieu d'en créer un neuf : un stream en
  cours reste compté et son modèle n'est jamais évincé.
- **Heartbeat périmé ignoré.** La révision d'inventaire est capturée avant
  `health()` ; si un load, un unload ou une invalidation a eu lieu pendant le
  round-trip, la réponse n'est appliquée ni à l'inventaire ni à
  `last_health`. Une entrée qu'un inventaire ne rapporte plus mais qui a des
  requêtes actives n'est jamais jetée : elle perd sa route primaire, devient
  suspecte, puis est réadoptée en place ou purgée une fois les requêtes
  terminées.
- **Copie existante réutilisée.** Après un échec data-plane, le replacement
  tente d'abord un chargement idempotent sur un nœud qui sert déjà la même
  définition (aucune VRAM supplémentaire), avant le scheduler. Une alternative
  saine capable d'accueillir le modèle reste prioritaire sur le nœud suspect.
  Avant, sur un nœud unique, une seule erreur réseau provoquait un 503 (copie
  pinnée) ou deux évictions et un rechargement à froid.
- **Pas de double comptage.** L'agent compte un modèle LOADING dans sa VRAM,
  ses ports et `loaded_model_ids`. Le scheduler ne retranche une réservation
  `pending_loads` que si le dernier health ne la publie pas encore, et les
  deltas locaux appliqués après un load ou un unload sont ignorés quand le
  health courant reflète déjà le changement.
- **Réservations orphelines purgées.** Une réservation laissée par un load
  annulé est retirée dès que `status()` rapporte le modèle `ready` ou absent
  (un POST jamais acquitté reste couvert par la grâce de 30 s). Le heartbeat
  déclenche ce `status()` dès qu'une réservation n'apparaît pas dans
  `loaded_model_ids` ou que sa grâce est échue : un load terminé en `failed`
  n'y apparaît jamais, et sa réservation restait sinon à vie. Un unload
  confirmé du modèle sur ce nœud la purge aussi, de même qu'un échec de
  finalisation (unload global concurrent, réponse READY incomplète). Un
  unload-all ne la jette **pas** à l'aveugle : l'agent ne décharge que les
  managers existants, et une opération encore en validation lui survit ; c'est
  l'inventaire suivant qui tranche.
- **`evicting` jamais orphelin.** `_do_unload` remet `evicting=False` sur toute
  issue, annulation comprise (y compris pendant la re-synchronisation
  `health()` d'un unload admin), et une série d'évictions interrompue le remet
  sur toutes ses victimes. La boucle de placement cède la main à chaque replan
  et s'arrête après 64 tentatives.

### Protocole de chargement identifié (CLU-003, CLU-005)

Chaque `POST /agent/models/load` transporte désormais une identité complète, pas
seulement `model.id` :

| Champ | Rôle |
|-------|------|
| `deployment_digest` | SHA-256 du JSON canonique (clés triées) de la définition runtime complète. Deux définitions successives du même `model.id` ont des digests différents. |
| `generation_id` | Identifiant stable du couple `(model.id, deployment_digest)` côté agent : une définition inchangée conserve sa génération, une définition modifiée en ouvre une nouvelle. |
| `operation_id` | Identifiant de l'opération de chargement elle-même (une exécution, pas un contenu). |
| `deadline_seconds` | Budget relatif négocié : le minimum entre le `load_timeout_seconds` du modèle et le timeout cluster de l'orchestrateur. |

Conséquences opérationnelles :

- **Acceptation puis polling.** Le POST ne bloque plus sur le chargement : il
  retourne `202` (`state="accepted"`) dès que l'agent a réservé le travail.
  L'orchestrateur interroge ensuite `GET /agent/operations/{operation_id}`,
  avec progression (`progress`) et états terminaux `ready`, `failed`,
  `conflict`, `deadline_exceeded`. Un timeout du RPC initial ne crée plus de
  doublon : le même `operation_id` est réinterrogé, jamais relancé sur un autre
  nœud. C'est l'unique chemin de chargement : l'ancien `load_model()` bloquant
  a été retiré du client et de l'agent.
- **Un résultat `ready` reste lié à son llama-server.** L'agent ne répond
  `ready` (au GET comme au rejeu du POST) que tant que le manager qui l'a
  produit est encore le manager READY du modèle. Après un unload, l'opération
  répond `failed` (`error_code="unloaded"`) : son port, rendu au pool, peut
  servir un autre modèle.
- **Un `failed` n'est pas une panne de nœud.** L'agent a répondu : un état
  `failed`/`deadline_exceeded` ne compte jamais vers le seuil offline. Il porte
  `error_status` (champ additif : 422 artefact ou définition, 500 llama-server,
  503 capacité du nœud, 504 échéance). L'orchestrateur tente les autres nœuds.
  Un nœud qui échoue pour une raison propre au modèle (statut connu hors
  409/429/503) est écarté **10 s** du placement de ce digest, nœud par nœud :
  un nœud sain momentanément plein reste candidat. La requête n'échoue
  immédiatement (« échec récent ») que si **tous** les nœuds online sont
  écartés ; les requêtes en file partagent alors l'échec au lieu de rejouer
  le chargement sur tout le cluster. Un `failed` sans `error_status` (agent
  antérieur, en mise à jour progressive) n'est jamais mémorisé : il pourrait
  s'agir d'une capacité. `ClusterManager.forget_load_failures(model_id)` oublie
  ces échecs (chargement admin après correction de l'artefact). Avant, trois
  requêtes pour un GGUF absent passaient tous les nœuds OFFLINE.
- **Changement de définition.** Une ancienne génération du même modèle sur le
  nœud choisi est déchargée avant le chargement de la nouvelle ; le scheduler
  compte sa VRAM et son port comme libérables. Si elle traite encore des
  requêtes, ce nœud est écarté ; sans autre nœud possible, le client reçoit
  `ClusterLoadConflictError` (503) jusqu'à la fin de ces requêtes. Avant, le
  modèle restait bloqué sur un 409 de l'agent jusqu'à un unload manuel, lui-même
  sans effet.
- **Une nouvelle définition ne peut plus servir une ancienne génération.** Si le
  digest reçu diffère de celui du modèle actif (ou de l'opération en cours), le
  node agent refuse le POST en **HTTP 409** (conflit d'identité ; le même code
  signale un déchargement en cours). L'orchestrateur le remonte en
  `ClusterLoadConflictError` (503 côté client, corps de l'agent au seul
  journal), purge sa réservation et n'écrase pas le placement. Un état
  terminal `conflict` publié par l'opération suit le même chemin.
- **Réservation de chargement bornée.** Tant que l'issue d'un load n'est pas
  connue, `pending_loads` réserve VRAM et port sur le nœud et interdit tout
  failover concurrent. Cette réservation ne peut plus bloquer un modèle à vie :

  | Issue du POST ou du polling | Interprétation | Traitement |
  |---|---|---|
  | POST 409 | Conflit / unload en cours côté agent | Réservation purgée, `ClusterLoadConflictError`, pas de failover |
  | POST 4xx définitif (401, 404, 422…) | L'agent valide tout **avant** d'enregistrer l'opération : rien n'a été accepté | Réservation purgée, failover ; ne compte pas vers le seuil offline |
  | POST 5xx, 408, 425, 429, coupure, timeout | Le POST a peut-être été traité | Polling du **même** `operation_id` |
  | GET 404 après que l'agent a reconnu l'opération | L'agent l'a perdue (redémarrage, purge) | Abandon immédiat, réservation purgée, failover |
  | GET 404 sans reconnaissance préalable | Le POST est peut-être encore en vol | Toléré pendant la grâce (30 s), puis abandon |
  | Nœud marqué offline par le heartbeat | Même politique qu'un modèle READY | Abandon, replacement |
  | `created_at + deadline_seconds + 30 s` dépassé | L'échéance appliquée par l'agent est forcément échue | Abandon, réservation purgée |

  Avant ce bornage, un 4xx au POST basculait sur le polling d'une opération
  jamais enregistrée : le GET répondait 404, la réservation restait, et chaque
  requête suivante re-pollait la même opération. Le modèle restait inutilisable
  jusqu'au redémarrage de la gateway, même avec un second nœud sain (audit
  P0-2, 2026-10-19). Lors d'une reprise abandonnée, le couple modèle/nœud est
  marqué suspect : le replacement préfère un autre nœud sain et ne revient à
  celui-ci que faute d'alternative. Une copie que le nœud terminerait malgré
  tout est réconciliée par le heartbeat comme doublon comptabilisé et
  évictable.

  ⚠️ `POST /admin/models/{id}/unload` ne purge **pas** une réservation en
  cours : la purge reste pilotée par les bornes ci-dessus. Une réservation
  orpheline (load annulé) est en revanche purgée par l'inventaire ou par un
  unload confirmé du modèle sur ce nœud (section « Inventaire, pins et
  comptabilité VRAM »).
- **Définition inchangée = chargement idempotent.** Digest et génération
  identiques → l'agent répond `already_loaded=True` sans redémarrer
  `llama-server`.

⚠️ **Changement de protocole cassant** : ces champs sont requis sur le fil.
Gateway et node agent doivent être mis à jour **ensemble** (une version contre
l'autre refuse la requête de chargement). Voir `docs/deployment.md` pour la
procédure.

### Durcissement TLS du plan de contrôle

Le canal orchestrateur ↔ node-agent (`nodes.yaml`, `cluster.tls_verify`) contrôle
la valeur passée à `httpx.AsyncClient(verify=...)` :

- **chemin vers un bundle CA** → vérification stricte contre ce bundle
  (recommandé en production) ;
- **`true`** → vérification système par défaut ;
- **`false`** → vérification désactivée. Un **warning explicite** est émis :
  cette combinaison expose la liaison (et donc les prompts) au MITM sur le LAN
  inter-nœuds. À réserver au dev/LAN isolé.

Une config `false` **avec au moins un nœud en `https://`** est incohérente (coût
TLS payé sans aucune vérification du certificat) : warning renforcé par défaut,
et **refus de chargement** si `CLUSTER_STRICT_TLS_VERIFY=true` est positionné
(fail-fast opt-in). Recommandation : pointer `tls_verify` vers un bundle CA
valide plutôt que de désactiver la vérification.

### Budget VRAM sur GB10 (mémoire unifiée)

Sur les DGX Spark GB10, le concept "VRAM" est en réalité de la **mémoire unifiée**
partagée CPU+GPU via NVLink-C2C (600 GB/s). Conséquences :

- `total_vram_gb` à configurer à ~120 sur les 128 GB physiques (OS+CUDA réserve ~8 GB).
- `--cpu-moe` de llama.cpp est **inutile** : déporter les experts FFN sur CPU ne libère
  rien car c'est la même mémoire physique. Laisser `cpu_moe: false`.
- Un GB10 peut tenir un 70B en Q8_0 (~72 GB) ou un 120B en Q4_K_M (~70 GB) seul.

Voir [build-llama-cpp-dgx-spark.md](build-llama-cpp-dgx-spark.md) pour la compilation
et la configuration complète.

### Nouveaux fichiers du package cluster

| Fichier | Rôle |
|---------|------|
| `gateway/cluster/__init__.py` | Package cluster |
| `gateway/cluster/node_protocol.py` | DTOs Pydantic partagés (LoadRequest/Response, NodeHealth, NodeStatus…) |
| `gateway/cluster/scheduler.py` | Logique pure de placement (best-fit + éviction LRU simulée) |
| `gateway/cluster/nodes_config.py` | Chargement/validation de `nodes.yaml` |
| `gateway/cluster/node_client.py` | `RemoteNodeClient` (HTTPS) + `LocalNodeAdapter` (in-process, tests) |
| `gateway/cluster/cluster_manager.py` | Orchestrateur — heartbeat, placement, état par nœud |
| `node_agent/main.py` | App FastAPI agent (~250 lignes, réutilise ServerManager) |
| `node_agent/config.py` | Settings agent (port, secret, VRAM, bin…) |
| `gateway/deploy/nodes.yaml.example` | Template de topologie cluster |
| `node_agent/deploy/install-agent.sh` | Script d'install agent sur DGX Spark |
| `docs/build-llama-cpp-dgx-spark.md` | Guide de compilation llama.cpp pour GB10/sm_121 |
