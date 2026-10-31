# Audit de sécurité — 30 octobre 2026

> Périmètre : `main` @ `18e4230`. Branche de correction : `fix/audit-securite-2026-10`.
> Méthode : trois audits indépendants (surface d'authentification/persistance,
> data plane, cluster et scripts de déploiement), **chaque finding re-vérifié
> dans le code réel** avant correction. Un finding non reproductible n'entre pas
> dans ce document.

## Ce que cet audit n'est pas

Ce dépôt avait déjà fermé une cinquantaine d'items tracés (`SEC-001`…`SEC-017`,
audit P0 : `ADMIN_SECRET` fail-closed, rédaction des journaux, attestation GGUF,
rétention RGPD, anonymisation DEC-001). Cet audit ne reprend pas ce travail : il
cherche ce qui reste **après** ces corrections, et il documente aussi ce qu'il a
examiné puis écarté, pour que la question ne soit pas rouverte tous les six mois.

Deux modèles de menace restent hors périmètre et sont inchangés :

- **`SEC-006`** — le data plane cluster n'est pas chiffré et aucun profil
  fail-closed de production n'existe. Toujours ouvert, déjà tracé.
- **`SEC-004`** — pas de limite de concurrence par utilisateur. Une limite de
  débit par minute ne borne pas le nombre de générations simultanées ; c'est un
  travail de conception, pas un correctif.

## Findings corrigés

| ID | Gravité | Constat | Correctif |
|---|:---:|---|---|
| `DP-001` | **Élevée** | `env = os.environ.copy()` transmettait à chaque llama-server `ADMIN_SECRET`, `AGENT_SECRET` et `INTERNAL_API_KEY`. llama-server est un binaire C++ qui parse des GGUF non fiables (le dépôt cite lui-même les overflows menant au RCE) : une compromission du parsing donnait `procfs`, donc le contrôle de `/admin` et de tous les nœuds. | `CHILD_ENV_ALLOWLIST` + `build_child_env()` : environnement minimal, toute variable ajoutée est un choix explicite. |
| `DP-002` | **Élevée** | httpx lit `HTTP_PROXY`/`ALL_PROXY` et y route **aussi** le loopback. Sur un hôte avec proxy d'environnement (proxy campus, `Environment=` dans l'unité), chaque prompt, chaque complétion et l'en-tête `Authorization` de la clé interne partaient vers ce proxy. Le téléchargement des modèles appliquait déjà `trust_env=False` (`SEC-014`) ; l'inférence, non. La même classe touchait le client du plan de contrôle cluster (`AGENT_SECRET`), la collecte de métriques du node-agent (`INTERNAL_API_KEY` en HTTP) et la sonde de préflight. | `trust_env=False` sur le client d'inférence, la sonde `/health`, la collecte de métriques de la gateway, le client du plan de contrôle cluster, la collecte de métriques du node-agent et la sonde de préflight. |
| `DP-005` | Moyenne | `_drain_stream` lisait avec `readline()`, qui lève `ValueError` au-delà de 64 Kio, et l'exception était avalée. La tâche de drainage mourait en silence, le pipe se remplissait, llama-server se bloquait sur son écriture et `process.wait()` — qui ne rend qu'une fois les pipes refermés — ne répondait plus. Modèle figé en `UNLOADING`, éviction LRU bloquée sous le verrou de capacité. Déclencheur : un prompt très long journalisé par le backend. | Lecture par blocs, lignes recollées à la main, ligne démesurée tronquée puis tampon vidé, erreur journalisée. `_reap_after_kill` ferme les transports en dernier recours. |
| `SEC-A-01` | Moyenne | `revoke_key` faisait `WHERE key_prefix LIKE ?` : le préfixe venait du chemin d'URL et ses jokers SQL étaient interprétés. `DELETE /admin/keys/%25` produisait `LIKE '%%'` et révoquait **les clés actives de tous les comptes** en une requête, en répondant 200. `_` agissait comme joker mono-caractère. | Égalité stricte (`key_prefix = ?`) + `is_active = 1`. Un préfixe est de longueur fixe et désigne une seule clé. |
| `SEC-A-03` | Moyenne | Sans `PRAGMA secure_delete`, SQLite laissait en clair la cellule de l'index `username TEXT UNIQUE`. La docstring promettait « aucune donnée effacée n'est récupérable » ; `strings gateway.db` retrouvait le nom, et toute sauvegarde produite **après** l'anonymisation le contenait encore — ce qui vidait DEC-001 de son sens. | `PRAGMA secure_delete = ON` dans `_SESSION_PRAGMAS` (chaque connexion), ce qui couvre aussi la purge de rétention. |
| `SEC-A-02` | Moyenne | `INTERNAL_API_KEY` laissé au placeholder ne produisait qu'un `log.critical` et le démarrage continuait avec une constante publiée dans le code source. Tout compte local de l'hôte pouvait interroger les llama-server en direct : contournement de `get_current_user`, du RPM, du quota et de `usage_log`. | Clé aléatoire générée au démarrage en mode local. Fail-closed aurait cassé les déploiements manuels documentés pour un risque limité au loopback. |
| `MY-01` | Moyenne | `GET /health` est publique et traverse le `deny all` de `/admin/` : elle publiait les identifiants des modèles chargés et l'occupation VRAM, alors que `/v1/models/{id}` renvoie le même 404 pour un modèle inconnu et un modèle désactivé, précisément pour ne pas révéler son existence. | `status` seul pour un appelant anonyme ; détail réservé au porteur d'`ADMIN_SECRET` (même mécanisme que `/ready`). |
| `DP-003` | Moyenne | Aucune validation des bornes de génération : `n_predict: -1` (« jusqu'à remplir le contexte »), `max_tokens: true` (booléen = 1 token), valeurs non entières — tout partait au backend. Un porteur de clé pouvait immobiliser les slots d'un modèle pendant le timeout de 600 s. | Typage strict, refus des négatifs, `MAX_NEW_TOKENS_PER_REQUEST` (0 = pas de borne, comportement historique). Refus explicite en 400, jamais d'écrêtage silencieux. |
| `CLU-101` | Moyenne | `ALLOWED_MODEL_DIRS` vide sur le node-agent désactivait silencieusement le contrôle de chemin (`ModelRegistry` ne vérifie que `if self._allowed_dirs`), et `main.py` convertissait le vide en `None`. N'importe quel GGUF absolu lisible par le service pouvait être chargé. | Réglage obligatoire dans `validate_runtime_security()`, `or None` retiré. L'installateur migre les EnvironmentFile existants (`append_env_if_missing ALLOWED_MODEL_DIRS /models`) : sans cette migration, le préflight aurait bloqué toute mise à jour d'un nœud antérieur. |
| `CLU-104` | Faible | La sonde de préflight du node-agent envoyait `AGENT_SECRET` avec `verify=False` : n'importe quel processus capable de se lier au port recevait le secret partagé de tous les nœuds. | Certificat épinglé (`cafile`), `check_hostname=False` assumé — la sonde vise `127.0.0.1` alors que le certificat est émis pour le nom du nœud. |
| `CLU-105` | Faible | `server_tokens` absent des deux confs nginx : chaque réponse, y compris les 403/404, annonçait `Server: nginx/<version>`, donc les CVE de cette version exacte. | `server_tokens off;` dans les deux arbres. |
| `CLU-107` | Faible | L'installeur imprimait `ADMIN_SECRET = …` sur stdout alors que le fichier est en 640 : la sortie d'un installeur finit dans un journal de provisioning, un `tee` ou l'historique de console. | Le secret n'est plus affiché ; l'installeur indique où le lire. Aligné sur `install-agent.sh`. |
| `CLU-108` | Faible | Le node-agent renvoyait `Fichier GGUF introuvable sur le nœud : /chemin/réel` dans ses 422, et `NodeProtocolError` propage ce texte jusqu'aux routes admin — l'arborescence d'un nœud GPU fuyait par un message d'erreur. | Message sans chemin, chemin complet dans le journal de l'agent. |
| `CLU-109` | Faible | `BACKUP_DIR` était interpolé dans une commande sqlite3 délimitée par des quotes simples (`.backup '$DEST'`) : une quote fermait la chaîne et permettait d'y ajouter des commandes sqlite3, le `chmod 600` portant alors sur un autre fichier. | `BACKUP_DIR` validé (absolu, sans quote, sans saut de ligne). |

Aucun de ces correctifs n'ajoute de dépendance.

## Findings examinés puis écartés

Écartés **après vérification**, pas par oubli. Chacun a une raison vérifiable.

| Candidat | Pourquoi il n'est pas un correctif |
|---|---|
| `CORS_ALLOW_ORIGINS` par défaut à `["*"]` | Le défaut est verrouillé par `test_cors_wildcard_default`, et `deploy/env-template-lib.sh` écrit `CORS_ALLOW_ORIGINS=` (donc liste vide) dans **tout** déploiement installé. Le chemin livré est déjà fermé ; changer le défaut casserait un test qui documente un arbitrage assumé (confort de dev) sans améliorer un déploiement réel. |
| Pas de `limit_req` sur `/admin/` | Le bloc est déjà restreint aux plages RFC1918 et le secret fait au moins 32 caractères (2^192 possibilités) : un balayage n'aboutit pas. Ajouter la directive demandait de réécrire un test qui vérifie les timeouts admin par remplacement de chaîne exacte, pour un gain nul. |
| `tls_verify: false` accepté en mode cluster | Politique explicite et testée : avertissement, refus seulement si `CLUSTER_STRICT_TLS_VERIFY=true`, pour ne pas casser les déploiements à certificat auto-signé. **Action d'exploitation** : positionner cette variable en production. |
| `http://` accepté pour le control plane | Même arbitrage, même variable d'échappement. À traiter avec `SEC-006`, pas séparément. |
| XSS dans le dashboard via `onclick='…'` | `esc()` est un échappement HTML, pas JS, et l'entité est décodée avant l'analyse du handler — la faille de raisonnement est réelle. Mais `username` est contraint à `^[a-zA-Z0-9_.-]+$` et `model_id` à `^[a-z0-9][a-z0-9._-]{0,62}$` : aucune quote ne peut atteindre l'attribut. **Non exploitable en l'état.** À réviser le jour où un identifiant devient libre. |
| `/admin/dashboard` et `/admin/assets/*` sans `require_admin` | Ce sont les deux seules routes `/admin` non authentifiées au niveau applicatif ; la barrière est le filtre IP nginx. Servir une SPA statique sans secret est cohérent (le secret est saisi dans le formulaire) et `Depends(require_admin)` est inapplicable à une navigation navigateur. Risque résiduel : un déploiement qui exposerait uvicorn sans nginx publierait la surface d'administration. Documenté ici, non corrigé faute de pouvoir borner les IP sans nouvelle configuration. |

## Limites résiduelles

Ces points ne sont pas des régressions des correctifs, mais ils bornent ce que
cet audit garantit :

- **Clé interne courte mais non-placeholder** : le remplacement automatique ne
  vise que `CHANGE_ME_*`/vide. Une valeur de moins de 32 caractères reste
  acceptée en mode local, alors que le node-agent l'exige. À traiter dans le
  même esprit que SEC-A-02 (générer ou refuser).
- **Anonymisations antérieures** : `secure_delete` protège les écritures
  futures ; un nom déjà effacé avant ce correctif peut subsister dans les pages
  libres de `gateway.db` et dans les sauvegardes antérieures. Un `VACUUM` hors
  ligne (ou la purge documentée) est nécessaire pour nettoyer l'historique ;
  les sauvegardes plus anciennes restent soumises à la rétention.
- **WAL** : avec une connexion concurrente ouverte, l'ancien nom peut rester
  dans le WAL, même avec `secure_delete` — les frames déjà écrites ne sont pas
  réécrites. `.backup` lit l'état courant et reste propre ; une copie brute
  `gateway.db` + `gateway.db-wal` ne l'est pas.
- **`revoke-key` par préfixe partiel** : l'égalité stricte est correcte, mais un
  script qui passait `llmgw-ab` (au lieu du préfixe complet) reçoit désormais
  404. Le dashboard et le CLI utilisent le préfixe complet.
- **`MAX_NEW_TOKENS_PER_REQUEST=0`** : DP-003 reste opt-in pour la borne haute ;
  seule la validation des types/négatifs est inconditionnelle. Poser une valeur
  en production si l'objectif est de borner les slots.
- **Scripts `curl` d'exploitation** : `smoke_test.sh`/`update.sh` n'imposent pas
  `--noproxy` ; si l'environnement d'exploitation définit `http_proxy`, les
  appels loopback peuvent passer par le proxy. À traiter séparément des clients
  Python.

## Vérification

| Contrôle | Avant | Après |
|---|---|---|
| Gateway `pytest` | 2746 réussis, 3 ignorés | **2767 réussis, 3 ignorés** |
| Node agent `pytest` | 109 réussis, 1 échec préexistant (auteur) | **113 réussis** (environnement de revue) |
| `ruff check` (les deux composants) | OK | OK |
| `scripts/export_openapi.py --check` | à jour | à jour (aucun modèle Pydantic modifié) |
| `bash -n` | OK | OK |
| `git diff --check` | propre | propre |

`node_agent/tests/test_update_agent_venv.py::test_legacy_install_is_migrated_in_place_and_can_roll_back`
échouait dans l'environnement de l'auteur (`uv`, shebang du venv hérité) ; il
passe dans le venv de revue (113 réussis). Le défaut est lié à l'outil qui a créé
le venv, pas au code.

La non-régression `SEC-A-03` a été vérifiée dans les deux sens : avec
`PRAGMA secure_delete = ON` retiré temporairement, le test échoue sur le nom
laissé dans le fichier ; avec le pragma, il passe.

## Tests de régression

Les défauts corrigés sont couverts là où un test est pertinent :
`gateway/tests/test_audit_securite_2026_10.py` pour les défauts gateway,
`gateway/tests/test_node_client.py` pour le client du plan de contrôle, et
`node_agent/tests/` pour la configuration et le préflight. Chaque test est nommé
d'après le défaut, avec un contrôle positif partout où une absence est assérée
(règle AGENTS.md). Les correctifs purement statiques (nginx, installeur,
validation shell) sont couverts par `bash -n` et la revue :

- la révocation par joker ne coupe que la clé visée, et la révocation légitime
  fonctionne toujours ;
- l'environnement du sous-processus ne porte aucun secret **et** n'est pas vide ;
- le client d'inférence, le client du plan de contrôle cluster, la collecte de
  métriques du node-agent et la sonde de préflight ignorent les proxies
  d'environnement ;
- la clé interne placeholder est remplacée au démarrage et la clé tirée est bien
  celle transmise à `llama-server` ;
- les bornes de génération invalides sont refusées, les valides passent, et la
  route `proxy_request` applique la validation avant tout chargement ;
- une allowlist `ALLOWED_MODEL_DIRS` vide est refusée par
  `validate_runtime_security()` ;
- le nom anonymisé n'est plus lisible dans le fichier SQLite ni dans son WAL,
  tandis que le pseudonyme y est ; le marqueur de notes est volontairement long
  et unique pour que le test échoue réellement sans `secure_delete` ;
- un vrai sous-processus écrivant une ligne de 300 Kio laisse le drainage vivant
  et `process.wait()` aboutir.
