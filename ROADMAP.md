# EVARuntime — feuille de route et plan de référence

> Document public de direction et de priorisation.
> Dernière mise à jour : 3 septembre 2026.
>
> Ce document rassemble la vision produit, les résultats de l’audit du dépôt,
> les limites connues, l’architecture cible et le plan de travail. Il ne
> remplace pas la documentation d’exploitation : [architecture](docs/architecture.md),
> [API](docs/api.md), [déploiement](docs/deployment.md) et
> [observabilité](docs/observability.md) décrivent le comportement courant.

## 1. Thèse du projet

EVARuntime est un gateway d’inférence local-first, compatible avec l’API
OpenAI, destiné à des GPU privés et partagés.

La proposition de valeur n’est pas de battre vLLM ou SGLang sur le débit brut.
Elle est de fournir un plan de contrôle compréhensible et vérifiable qui sait :

- charger un modèle uniquement quand il est nécessaire ;
- ne jamais évincer un modèle qui traite une requête ;
- libérer réellement la mémoire GPU lorsqu’elle n’est plus utile ;
- fonctionner en local ou sur plusieurs nœuds sans imposer Kubernetes ;
- garder prompts, modèles, clés, journaux et opérations dans l’organisation ;
- mesurer les temps, la mémoire, la consommation électrique et les échecs ;
- prouver quel artefact et quelle version de runtime ont servi une requête.

La phrase de positionnement recommandée est :

> **Le runtime d’inférence privé pour une flotte de GPU hétérogènes :
> explicite sur ses états, frugal sur ses ressources et vérifiable sur ses
> coûts.**

## 2. Politique de suivi

Il n’existe qu’une source de vérité par type d’information :

| Besoin | Source canonique |
|---|---|
| Fonctionnement livré | `README.md` et `docs/` |
| Direction produit et jalons | `ROADMAP.md` |
| Décision d’architecture | `docs/decisions/ADR-*.md` ou une RFC GitHub |
| Travail actionnable | GitHub Issues et GitHub Project |
| Incident ou vulnérabilité | Issue dédiée ou GitHub Security Advisory |
| Historique d’audit détaillé | `codex-analyse.md` (archive, non canonique) |

Une idée ne devient pas automatiquement une issue. Elle devient une issue
lorsqu’elle possède un périmètre, un responsable possible, des critères
d’acceptation et un plan de vérification. Les sujets encore incertains sont
des RFC/Discussions. Les petites étapes restent une checklist dans une epic.

Le dépôt doit conserver peu d’issues ouvertes mais bien triées. Le nombre
d’issues n’est pas un indicateur de mauvaise qualité ; les indicateurs utiles
sont la clarté des priorités, l’âge des issues critiques, les preuves de test
et la qualité des releases.

## 3. État actuel

### Ce qui est déjà solide

- machine d’états locale `UNLOADED → LOADING → READY → UNLOADING` ;
- chargements concurrents d’un même modèle coalescés ;
- `pin()`/`unpin()` jusqu’à la fin réelle d’un stream ;
- éviction LRU des modèles inactifs ;
- admission combinant budget VRAM, ports et queue bornée ;
- registre YAML validé, chemins contraints et écritures atomiques ;
- SQLite WAL, migrations versionnées et anonymisation des utilisateurs ;
- subprocesses `llama-server` possédés par le gateway ;
- readiness structurelle centralisée par `gateway/readiness.py` ;
- `doctor`, bootstrap plan/apply, smoke test et runbooks de déploiement ;
- mode cluster avec scheduler, health checks, réconciliation et failover ;
- dépendances verrouillées, audit CVE et tests d’invariants nombreux.

### Vérification locale du 3 septembre 2026

| Composant | Résultat |
|---|---:|
| Tests gateway | 2513 réussis, 3 ignorés pour dépendances d’environnement |
| Tests node agent | 67 réussis |
| Ruff gateway et node agent | OK |
| GPU physique / nginx réel / runtime amont épinglé | Non démontré dans cet audit |
| Modèle distribué sur plusieurs nœuds | Non supporté actuellement |

Le projet peut donc revendiquer un socle logiciel et un chemin llama.cpp
local sérieux. Il ne doit pas encore revendiquer une qualification complète
de production GPU ou de serving distribué.

## 4. Défauts à traiter avant l’expansion

Les points ci-dessous sont issus d’une inspection du code et ne sont pas tous
couverts par les tests actuels. Les numéros sont des identifiants de suivi
proposés ; ils peuvent être regroupés dans les epics GitHub.

| ID | Priorité | Constat | Conséquence | Acceptation minimale |
|---|:---:|---|---|---|
| `CLU-001` | P0 | `ClusterManager.unload_model()` ignore l’échec booléen de `_do_unload()` | L’admin peut annoncer une VRAM libérée alors que l’ancien serveur tourne toujours | Erreur typée/503, placement conservé et état `unload_uncertain` |
| `SEC-ART-001` ✅ | P0 | Le SHA-256 est vérifié au démarrage, mais pas à chaque transition vers `LOADING` | Un GGUF peut être remplacé après le démarrage puis chargé sans nouvelle attestation | Vérification fail-closed juste avant chaque chargement |
| `CLU-002` ✅ | P0 | Le hash d'un gros GGUF est synchrone dans le seul event loop du node agent | `/health`, unload et heartbeat peuvent être bloqués pendant plusieurs minutes | Hash hors event loop, single-flight et cache attesté |
| `COR-013` ✅ | P0 | Une réponse upstream 4xx/5xx est relayée comme stream HTTP 200 | Les clients OpenAI reçoivent une enveloppe invalide ou du JSON brut dans du SSE | Statut et enveloppe d’erreur traités avant le premier octet |
| `ACC-001` ✅ | P0 | La journalisation d’usage est après le `finally` du générateur SSE | Une déconnexion peut libérer le pin sans enregistrer l’usage partiel | Un seul résultat terminal, y compris `client_cancelled` |
| `ACC-002` ✅ | P0 | Les tâches `fire_and_forget` ne sont pas drainées au shutdown | Les derniers usages peuvent être perdus lors d’un redémarrage | Queue bornée et flush avec deadline |

✅ = corrigé avec tests. Lot 1 de l'épic #39 : pré-flight du statut upstream, résultat
terminal unique protégé par bouclier anyio, drain borné `SHUTDOWN_BACKGROUND_FLUSH_SECONDS`.
Lot 2 : attestation GGUF fail-closed à chaque transition `LOADING` (SEC-ART-001) et
hachage hors event loop avec single-flight et cache attesté (CLU-002) — module partagé
`gateway/integrity.py`, utilisé par le gateway et le node agent.
| `CLU-003` | P0 | L’idempotence du node agent dépend seulement de `model.id` | Une nouvelle définition peut continuer à servir une ancienne génération | `deployment_digest` et `generation_id` obligatoires |
| `CLU-005` | P0 | Le timeout cluster fixe est inférieur aux chargements de certains modèles | L’orchestrateur abandonne alors que l’agent continue, créant des doublons | `operation_id`, progression et deadline négociée |
| `CFG-001` | P0 | VRAM, ports, quotas et timeouts peuvent prendre des valeurs incohérentes | Échecs tardifs et interprétation accidentelle d’un quota négatif | Validation de bornes et invariants croisés au démarrage |
| `SEC-006` | P0 | Le data-plane cluster reconstruit des URLs HTTP | Prompts et secret interne peuvent transiter en clair | mTLS/WireGuard ou profil production fail-closed |
| `TST-004` | P0 | La recette sur vrai runtime/GPU/GGUF/nginx n’est pas archivée | Le chemin installé jusqu’au premier token reste une hypothèse | Rapport reproductible signé par environnement |
| `TST-005` | P0 | Les tests cluster utilisent surtout des fakes in-process | Les sockets, délais, streams, pannes et processus réels ne sont pas prouvés | E2E avec vrais agents/processus et injections de panne |
| `REG-001` | P1 | Le parseur YAML accepte des types invalides ou trop permissifs | Le contrat YAML diffère du contrat admin | Schémas stricts et erreurs identiques |
| `REG-002` | P1 | `vision` n’exige pas structurellement un projector | Une configuration apparemment valide échoue à la première image | `vision` exige `mmproj` et son intégrité |
| `PORT-001` | P1 | Les ports occupés sont détectés mais restent réallouables | Échecs répétés et réutilisation d’un port orphelin | États `available/owned/quarantined` |
| `GPU-002` | P1 | La sonde VRAM agrège tous les GPU de l’hôte | Une charge hors `CUDA_VISIBLE_DEVICES` fausse la capacité EVA | Mesure par UUID et périmètre configuré |
| `OBS-001` | P1 | La latence persistée exclut queue et cold start | Les SLO et rapports sont optimistes | `total_ms`, `queue_ms`, `load_ms`, `backend_ms` séparés |
| `OBS-002` | P1 | `usage_log` ne compte pas tous les résultats terminaux | Le taux d’erreur paraît meilleur qu’il ne l’est | Un outcome par requête, indépendant de la facturation |
| `QUOTA-001` | P1 | Le quota est lu puis crédité après coup | Des requêtes concurrentes peuvent dépasser la limite | Réservation atomique, remboursement/ajustement documenté |
| `API-001` | P1 | Le champ `stream` n’est pas strictement booléen | `"false"` peut sélectionner le chemin SSE | Booléen uniquement, erreur 400 avant chargement |
| `API-002` | P1 | `stream_options` du client est écrasé | Incompatibilité avec certains SDK OpenAI | Fusion contrôlée et tests de compatibilité |
| `PERF-002` | P1 | Des métriques `*_total` sont des fenêtres glissantes | Elles peuvent diminuer, ce qui viole Prometheus | Counters monotones ou renommage en gauges 24 h |
| `PERF-008` | P2 | Le nettoyage du rate limiter n’est pas planifié | Accumulation possible d’identités inactives | Tâche périodique bornée et testée |

Les issues GitHub historiques déjà ouvertes sont à conserver :

- [#30 — Gérer la priorité CPU des processus llama-server](https://github.com/Tutanka01/EVARuntime/issues/30) ;
- [#32 — Purger les identités éphémères de smoke test](https://github.com/Tutanka01/EVARuntime/issues/32).

### Backlog déjà identifié dans l’ancien tracker

Ces éléments existaient déjà dans `codex-analyse.md`. Ils restent dans la
roadmap, mais doivent être traités comme des sous-tâches d’une epic plutôt que
comme une nouvelle forêt d’issues :

| Référence | Sujet | Epic cible |
|---|---|---|
| `COR-003` | Formaliser une lease atomique entre admission, pin et réponse | Reliability |
| `COR-008` | Enveloppe d’erreur OpenAI stable sur tous les chemins | Reliability |
| `COR-010` | Révocation par préfixe sans interprétation wildcard SQL | Reliability |
| `COR-012` | Réservation atomique des quotas | Reliability |
| `COR-018` | Ne pas annoncer READY pour un modèle d’un nœud offline/stale | Cluster |
| `COR-019` | Queue d’admission cohérente en mode cluster | Cluster |
| `PERF-002` | Corriger la sémantique des Counters 24 h | Observabilité |
| `PERF-003` | Benchmarker les performances SQLite WAL et la rétention | Observabilité |
| `PERF-004` | Contrat tools/reasoning/streaming entre client et backend | Runtime abstraction |
| `PERF-005` | Déporter les mutations admin longues en jobs asynchrones | Lifecycle |
| `PERF-006` | Profiler le temps de chargement et le pic VRAM par modèle | Cold starts |
| `PERF-007` | Documenter et tester les limites réseau/NAT du cluster | Cluster |
| `PERF-008` | Planifier le nettoyage des entrées rate limiter obsolètes | Reliability |
| `SEC-005` | Manifeste d’intégrité et provenance pour tous les artefacts | Reliability |
| `SEC-006` | Protection du data-plane cluster | Cluster |
| `SEC-007` | SBOM, provenance des releases et runtime immuable | Lifecycle |
| `TST-002` | Seuil de couverture et mutation testing ciblé | Reliability |
| `TST-003` | Tests CLI et lifespan/shutdown | Lifecycle |
| `TST-004` | Petit GGUF et vrai runtime sur matériel de staging | Preuve terrain |
| `TST-005` | E2E cluster avec vrais sockets/processus | Cluster |
| `OPS-001` | Profils matériels officiellement supportés | GPU/énergie |
| `OPS-002` | Exercice de restauration off-host et rétention des backups | Lifecycle |
| `OPS-003` | Releases immuables et rollback vérifiable | Lifecycle |
| `OPS-004` | Request ID propagé et corrélé aux métriques | Observabilité |

### Compléments cluster à ne pas perdre

Le cluster doit aussi rendre opérables les capacités actuellement seulement
présentes dans les modèles de données ou dans les tests :

- `draining` pour préparer la maintenance d’un nœud ;
- drain d’un modèle cohérent avec le mode local ;
- distinction entre panne de nœud, artefact absent, incompatibilité et manque
  de capacité ;
- inventaire des artefacts présents par digest avant placement ;
- plusieurs réplicas et routage selon charge, queue et cache ;
- epoch/lease/fencing contre le split-brain ;
- négociation de version et de capabilities du protocole agent ;
- agrégation des métriques par `(node, deployment, model)` sans écrasement ;
- câblage réel de `pin_to_node` ou retrait de cette promesse documentaire.

## 5. Vision produit

### Pilier A — Warm Fleet

Le projet peut rendre compatible deux comportements qui semblent opposés :
libérer la VRAM et éviter de recalculer tout le contexte.

Objectifs :

- snapshot KV optionnel au déchargement ;
- restore refusé si la signature modèle/runtime ne correspond pas ;
- budget disque, TTL et LRU des snapshots ;
- routage vers le nœud qui possède déjà un préfixe chaud ;
- session pinning explicite ;
- comparaison publique TTFT cold, warm et restored.

Ce chantier est à traiter comme une optimisation expérimentale. Il nécessite
un format de snapshot stable, des limites de confidentialité par tenant et
une stratégie de purge. Les API de slots et de sauvegarde de llama.cpp sont
évolutives : le build doit être épinglé et testé à chaque mise à jour.

### Pilier B — Tokens par watt

EVARuntime doit exposer non seulement « combien de tokens », mais aussi :

- combien de watts pendant l’inférence ;
- combien de joules par runtime et par modèle ;
- combien de joules par requête réussie, avec un niveau de confiance ;
- quelle puissance est consommée à l’idle ;
- quels GPU sont limités par température ou puissance.

L’énergie attribuée à une requête reste une estimation lorsque plusieurs
requêtes sont batchées. Les compteurs bruts par GPU doivent toujours être
conservés à côté de l’allocation estimée.

### Pilier C — Local-first explicite

Une éventuelle escalade vers un moteur ou un fournisseur externe doit être :

- désactivée par défaut ;
- autorisée par politique de clé ou de groupe ;
- plafonnée en coût ;
- expliquée dans l’audit ;
- impossible lorsque la politique est `local-only`.

Ce chantier est secondaire. Il ne faut pas transformer EVARuntime en proxy
universel multi-fournisseurs.

## 6. État de l’art des moteurs

| Moteur | Rôle recommandé | Artefacts | Cycle de vie | Distribution | Décision |
|---|---|---|---|---|---|
| **llama.cpp** | Backend universel, GGUF, machines hétérogènes, CPU/GPU, Apple Silicon | GGUF | Arrêt complet fiable ; mmap ; cache local | Split layer/row ; tensor expérimental ; RPC expérimental | Backend par défaut |
| **vLLM** | Haut débit, batching continu, modèles HF/Safetensors, GPU dédiés | Safetensors, AWQ, GPTQ, FP8 ; GGUF expérimental | Sleep niveau 1 ou 2, wake, process group | TP, PP, DP, EP et multi-nœud | Premier backend secondaire |
| **SGLang** | Agentique, préfixes réutilisés, structured output, MoE | HF/Safetensors et formats supportés par version | HiCache, warmup et chargement avancé | TP/PP/DP/EP, prefill/decode désagrégé | Backend expérimental |
| **TensorRT-LLM/Triton** | Parc NVIDIA homogène et optimisation spécialisée | Artefacts NVIDIA | Très performant mais plus lourd à opérer | TP/EP/MPI | Spike séparé, pas le socle |

Le support GGUF dans vLLM reste décrit comme expérimental et potentiellement
plus lent que ses formats natifs : prévoir un artefact GGUF pour llama.cpp et
un artefact Safetensors/HF pour vLLM.

La documentation vLLM actuelle décrit un **Sleep Mode** : le niveau 1 déporte
les poids en RAM et supprime le KV cache ; le niveau 2 supprime poids et KV de
la mémoire GPU. Les endpoints HTTP nécessitent toutefois le mode développement
et ne doivent pas être exposés directement :
[vLLM Sleep Mode](https://docs.vllm.ai/en/latest/features/sleep_mode/).

La conclusion historique de `docs/recherche/veille-technique.md` selon laquelle
vLLM ne peut pas rendre sa VRAM n’est donc plus valide. Elle doit être lue
comme une note historique et non comme une décision actuelle.

Sources moteur :

- [llama.cpp server README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md) ;
- [llama.cpp multi-GPU](https://github.com/ggml-org/llama.cpp/blob/master/docs/multi-gpu.md) ;
- [llama.cpp RPC](https://github.com/ggml-org/llama.cpp/blob/master/tools/rpc/README.md) ;
- [vLLM data parallel](https://docs.vllm.ai/en/latest/serving/data_parallel_deployment/) ;
- [vLLM GGUF](https://docs.vllm.ai/en/latest/features/quantization/gguf/) ;
- [SGLang installation et multi-nœud](https://docs.sglang.io/docs/get-started/install) ;
- [SGLang expert parallelism](https://docs.sglang.io/docs/advanced_features/expert_parallelism).

Le RPC llama.cpp est explicitement présenté comme fragile et non sécurisé.
Il doit rester limité à un laboratoire sur réseau isolé, en particulier à la
lumière de l’[avis de sécurité associé](https://github.com/ggml-org/llama.cpp/security/advisories/GHSA-j8rj-fmpv-wcxw).

## 7. Architecture cible

### 7.1 Contrat de backend

Le domaine ne doit plus connaître `llama_url()` ni construire directement une
commande propre à un moteur.

```python
class BackendDriver(Protocol):
    def capabilities(self) -> BackendCapabilities: ...
    def validate(self, deployment, allocation) -> ValidationResult: ...
    def estimate_resources(self, deployment, profile) -> ResourceEstimate: ...
    async def prepare(self, instance) -> PreparedArtifact: ...
    async def start(self, instance, allocation) -> BackendHandle: ...
    async def wait_ready(self, handle) -> ReadinessResult: ...
    async def drain(self, handle, deadline) -> None: ...
    async def sleep(self, handle, level) -> None: ...
    async def wake(self, handle) -> None: ...
    async def stop(self, handle) -> None: ...
    async def metrics(self, handle) -> BackendMetrics: ...
```

Les capacités doivent être versionnées et négociées : streaming, tools,
reasoning, JSON schema, embeddings, vision, speculative decoding, sleep,
TP, PP, EP, cache KV et métriques disponibles.

### 7.2 Registre backend-neutral

```yaml
id: qwen-72b

artifacts:
  llamacpp:
    format: gguf
    path: /models/qwen-72b-q4.gguf
    sha256: ...
  vllm:
    format: safetensors
    path: /models/qwen-72b-hf
    revision: immutable-digest

runtime_profiles:
  - name: llamacpp-local
    engine: llamacpp
    resources:
      gpu_count: 1
  - name: vllm-tp4
    engine: vllm
    parallelism:
      tensor_parallel: 4
      pipeline_parallel: 1
    resources:
      gpu_count: 4
      min_vram_gb_per_gpu: 70

lifecycle:
  idle_action: sleep_cpu
  idle_ttl_seconds: 900
  min_residency_seconds: 300

placement:
  replicas: 1
  min_ready: 0
```

Les paramètres propres à un moteur doivent être namespacés et allowlistés.
Les tableaux arbitraires de flags CLI ne sont pas acceptables dans une API
admin.

### 7.3 Desired state et observed state

La vérité durable doit distinguer :

- ce que l’opérateur demande ;
- ce que les nœuds observent réellement ;
- la génération exacte à laquelle un endpoint appartient.

États recommandés :

```text
PENDING → PREFETCHING → VERIFYING → RESERVING → LOADING → WARMING → READY
READY → DRAINING → SLEEPING → WAKING → READY
READY → DRAINING → UNLOADING → UNLOADED
Tout état peut devenir FAILED ou UNKNOWN.
```

Chaque opération doit porter `operation_id`, `deployment_id`, `generation_id`,
`deployment_digest`, `controller_epoch` et une deadline. Le résultat d’une
opération arrivée en retard ne doit jamais écraser une génération plus récente.

### 7.4 ReplicaGroup et gang scheduling

L’unité de placement future n’est plus `model → node`, mais :

```text
ModelDeployment
 ├── ReplicaGroup A
 │    ├── rank 0 → node-1 / GPU-0,1
 │    └── rank 1 → node-2 / GPU-0,1
 └── ReplicaGroup B
      └── node-3 / GPU-0,1,2,3
```

La réservation doit être atomique : sélectionner tous les GPU, réserver avec
TTL, préparer les artefacts, lancer tous les rangs, attendre leur readiness,
publier le leader, puis rollback collectif si un rang échoue.

## 8. Stratégie multi-GPU et multi-nœud

### Étape 1 — llama.cpp intra-nœud

Ajouter une allocation par GPU UUID et les paramètres `device`, `split_mode`,
`tensor_split` et `main_gpu`. Qualifier d’abord le mode `layer`. Le mode
`tensor` reste expérimental et ne doit jamais être choisi silencieusement.

### Étape 2 — vLLM TP mono-nœud

Réserver plusieurs GPU sur un seul nœud, laisser vLLM posséder ses workers et
présenter le groupe comme une seule instance au gateway.

### Étape 3 — réplicas TP

Ajouter `min_replicas`, `max_replicas`, anti-affinité, routage par charge,
drain et failover.

### Étape 4 — TP/PP entre nœuds

Privilégier TP à l’intérieur d’un hôte NVLink/NVSwitch, puis PP entre hôtes.
Ne valider TP inter-nœud qu’avec une fabric réseau adaptée, typiquement
InfiniBand/RDMA. Une panne d’un seul rang doit rendre tout le groupe non
routable et déclencher un remplacement collectif.

### Ce qu’il ne faut pas faire maintenant

Ne pas utiliser `--rpc` de llama.cpp comme mécanisme de production pour
« additionner » des GPU de machines différentes. Il faut d’abord un protocole
authentifié, une transaction de groupe, un cache d’artefacts cohérent, du
fencing et des tests de panne réseau.

## 9. Cold start et gestion mémoire

Une durée unique de chargement ne suffit pas. Mesurer séparément :

```text
capacity_wait
artifact_prefetch
artifact_verify
disk_read
cpu_to_gpu
process_start
distributed_rendezvous
jit_compile
cuda_graph_capture
warmup
functional_probe
time_to_first_token
```

Fonctionnalités prioritaires :

- cache local NVMe adressé par digest ;
- prefetch sans réservation GPU ;
- vérification single-flight ;
- checkpoints shardés par profil ;
- préchauffage planifié ;
- hystérésis `min_residency` ;
- historique du coût de chargement par modèle/nœud ;
- jobs admin asynchrones avec progression ;
- politiques `kill`, `sleep_cpu`, `sleep_discard`, `keep_warm`.

Le scheduler peut ensuite minimiser :

```text
queue_delay + cold_start_cost + eviction_cost + topology_penalty + energy_cost
```

## 10. Énergie et observabilité

### Télémétrie énergétique

Pour NVIDIA, utiliser DCGM comme source brute lorsque disponible :

- `DCGM_FI_DEV_POWER_USAGE` ;
- `DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION` ;
- température, clocks, utilisation mémoire/GPU ;
- ECC, Xid et causes de throttling.

Source : [métriques DCGM Exporter](https://docs.nvidia.com/datacenter/dcgm/latest/reference/dcgm-exporter-metrics.html).

Métriques EVARuntime proposées :

```text
eva_gpu_power_watts{node,gpu_uuid}
eva_gpu_energy_joules_total{node,gpu_uuid}
eva_runtime_energy_joules_total{deployment,generation}
eva_runtime_idle_baseline_watts
eva_energy_per_output_token_joules
eva_energy_per_successful_request_joules
eva_power_throttling_seconds_total
```

Les labels ne doivent jamais contenir prompt, utilisateur, email ou request ID.

### SLI/SLO

Par modèle, backend, profil matériel et classe de charge :

- TTFT p50/p95/p99 ;
- ITL/TPOT et latence bout en bout ;
- temps de queue ;
- tokens entrée/sortie ;
- requêtes actives et en attente ;
- cause d’attente et préemptions ;
- utilisation et hit ratio du KV cache ;
- acceptance speculative ;
- phases de cold start et wake ;
- OOM, crash, timeout, annulation et disconnect ;
- goodput sous SLO ;
- joules par requête/token.

Séparer les SLO `warm`, `wake` et `cold`. Ne pas comparer une requête
interactive courte à un batch long sans publier la classe de charge.

## 11. Plan GitHub maintenable

Le projet public devrait exposer environ sept epics, et non une quarantaine de
micro-issues :

1. [**Reliability hardening**](https://github.com/Tutanka01/EVARuntime/issues/39) — exactitude des états, intégrité, streaming,
   quotas et configuration ;
2. [**GPU inventory & energy**](https://github.com/Tutanka01/EVARuntime/issues/40) — UUID, VRAM réelle, DCGM et coûts ;
3. [**Cold starts & lifecycle**](https://github.com/Tutanka01/EVARuntime/issues/41) — prefetch, warmup, sleep/wake et cache ;
4. [**Runtime abstraction**](https://github.com/Tutanka01/EVARuntime/issues/42) — `BackendDriver`, capabilities et registre v2 ;
5. [**vLLM single-node**](https://github.com/Tutanka01/EVARuntime/issues/43) — Safetensors, TP local, process group et métriques ;
6. [**Replicas & cluster scheduling**](https://github.com/Tutanka01/EVARuntime/issues/44) — ReplicaGroup, réplicas, drain et
   failover ;
7. [**SGLang/distributed experiments**](https://github.com/Tutanka01/EVARuntime/issues/45) — HiCache, TP/PP/EP et prefill/decode.

Une epic doit contenir : objectif, hors périmètre, liens vers la roadmap,
critères de sortie et checklist. Les sous-issues sont créées uniquement au
moment où elles sont prêtes à être développées.

Labels recommandés :

```text
area:lifecycle  area:cluster  area:engine  area:energy
area:security   area:observability  area:docs
priority:critical  priority:high  priority:medium  priority:low
type:bug  type:feature  type:rfc  type:ops
```

Definition of Done commune : code, tests positifs/négatifs/concurrents,
documentation, métriques, rollback sûr, CI verte et cohérence local/cluster.

## 12. Jalons

### R0 — vérité opérationnelle

Corriger CLU-001, SEC-ART-001, CLU-002, COR-013, ACC-001, ACC-002, CFG-001 et
CLU-003. Aucune opération ne doit annoncer un état non confirmé.

Progression (épic #39) : COR-013, ACC-001 et ACC-002 sont corrigés (lot 1 —
erreurs OpenAI avant premier octet, résultat terminal unique sous déconnexion,
drain borné au shutdown) ; SEC-ART-001 et CLU-002 le sont (lot 2 — attestation
GGUF fail-closed à chaque chargement, hachage hors event loop, single-flight,
cache attesté). Restent : CLU-001, CLU-003, CFG-001
(+ CLU-005, REG-001, REG-002).

### R1 — preuve terrain et cluster qualifié

Exécuter TST-004/TST-005, mesurer le matériel par UUID, sécuriser le data-plane,
versionner le protocole et séparer les erreurs de capacité, artefact et santé.

### R2 — qualité de service

Ajouter queue cluster, préfetch, drain opérable, réplicas, routage conscient de
la charge et métriques de goodput.

### R3 — abstraction runtime

Extraire `LlamaCppDriver`, publier le registre backend-neutral et ajouter un
fake backend contractuel.

### R4 — vLLM et énergie

Livrer vLLM mono-nœud, TP local, sleep/wake privé, DCGM, benchmarks cold/warm
et coûts énergétiques.

### R5 — modèles distribués

Livrer ReplicaGroup, gang scheduling, TP/PP/EP, rendez-vous sécurisé, rollback
collectif et chaos tests réseau.

### R6 — SGLang et optimisations avancées

Qualifier SGLang, HiCache, speculative decoding et éventuellement
prefill/decode désagrégé. Chaque optimisation doit être activée uniquement
pour un profil mesuré.

## 13. Calendrier recommandé

### 0–30 jours

- fermer les défauts P0 de vérité opérationnelle ;
- produire la preuve GPU/nginx/GGUF ;
- mettre en place les epics GitHub ;
- corriger le tracker historique et les textes vLLM obsolètes.

### 30–90 jours

- `BackendDriver` et extraction llama.cpp ;
- inventaire GPU réel ;
- opérations longues ;
- registre v2 ;
- backend vLLM externe mono-GPU ;
- contrat OpenAI exécuté sur chaque backend.

### 3–6 mois

- vLLM TP mono-nœud ;
- réplicas ;
- sleep/wake ;
- cache NVMe ;
- energy telemetry ;
- benchmark public reproductible.

### 6–12 mois

- ReplicaGroup ;
- TP/PP multi-nœud ;
- EP MoE ;
- fencing et HA du control-plane ;
- SGLang qualifié ;
- cache hiérarchique et prefill/decode uniquement si les mesures le justifient.

## 14. Hors périmètre volontaire

EVARuntime ne doit pas essayer de devenir :

- un proxy universel de dizaines de fournisseurs cloud ;
- un concurrent direct de vLLM/SGLang sur leurs kernels ;
- une plateforme Kubernetes complète ;
- un produit desktop grand public ;
- une solution SaaS qui contredit le local-first ;
- un système distribué multi-hôte avant d’avoir des opérations mono-nœud
  attestées, idempotentes et observables.

## 15. Documents à maintenir

- `README.md` : promesse publique, quick start et statut de maturité ;
- `ROADMAP.md` : vision, limites, jalons et priorités ;
- `docs/vision.md` : thèse produit courte et différenciation ;
- `docs/architecture.md` : architecture livrée et invariants ;
- `docs/api.md` : contrat OpenAI ;
- `docs/deployment.md` : installation et rollback ;
- `docs/observability.md` : sondes et métriques ;
- `docs/decisions/ADR-*.md` : décisions stables ;
- `docs/recherche/veille-technique.md` : notes de veille, toujours datées.

`codex-analyse.md` reste une archive d’implémentation historique. Il ne doit
plus être utilisé pour connaître le statut courant ; tout futur agent doit
commencer par `ROADMAP.md`.

## 16. Références techniques

- [vLLM Sleep Mode](https://docs.vllm.ai/en/latest/features/sleep_mode/) ;
- [vLLM data parallel deployment](https://docs.vllm.ai/en/latest/serving/data_parallel_deployment/) ;
- [vLLM disaggregated prefill](https://docs.vllm.ai/en/latest/features/disagg_prefill/) ;
- [llama.cpp server](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md) ;
- [llama.cpp multi-GPU](https://github.com/ggml-org/llama.cpp/blob/master/docs/multi-gpu.md) ;
- [llama.cpp RPC](https://github.com/ggml-org/llama.cpp/blob/master/tools/rpc/README.md) ;
- [SGLang documentation](https://docs.sglang.io/) ;
- [DCGM Exporter metrics](https://docs.nvidia.com/datacenter/dcgm/latest/reference/dcgm-exporter-metrics.html) ;
- [Zeus energy measurement](https://github.com/ml-energy/zeus) ;
- [MLPerf Power methodology](https://docs.mlcommons.org/inference/power/).
