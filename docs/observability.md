# Observabilité — Cluster EVA Inference Gateway

Ce document décrit comment superviser la gateway principale (et, en mode cluster,
les node-agents) : exposition Prometheus, sondes de liveness/readiness, et
quelques règles d'alerte pragmatiques.

La philosophie reste celle du projet : **pas de stack lourde**. La gateway expose
un endpoint texte Prometheus généré à la main (aucune dépendance ajoutée) que
scrape un Prometheus mono-binaire local ; le dashboard admin (JSON) couvre le
reste. Rien n'oblige à déployer une pile observabilité complète.

---

## Table des matières

1. [Sondes /health et /ready](#1-sondes-health-et-ready)
2. [Exposition Prometheus](#2-exposition-prometheus)
3. [Scraping local (Prometheus mono-binaire)](#3-scraping-local-prometheus-mono-binaire)
4. [Métriques par nœud (mode cluster)](#4-métriques-par-nœud-mode-cluster)
5. [Règles d'alerte pragmatiques](#5-règles-dalerte-pragmatiques)

---

## 1. Sondes /health et /ready

### Readiness à trois niveaux

La supervision de la gateway repose sur trois niveaux distincts. Les confondre
est ce qui permettait, avant COR-005, de déployer une version incapable de
servir : `update.sh` décide du rollback sur `/ready`, et `/ready` se contentait
alors de « un modèle est chargé **ou** il reste de la VRAM ».

| Niveau | Signification | Où c'est prouvé |
|---|---|---|
| **Liveness** | Le processus répond. | `GET /health` |
| **Structural readiness** | Configuration, DB, binaire, répertoires et au moins un modèle activé sont valides. | `GET /ready` → `200` |
| **Serving readiness** | Un modèle est chargé, ou un smoke load récent a prouvé qu'il peut l'être. | smoke test de mise à jour (`COR-006`) ; **observée** dans `/ready` via `levels.serving`, jamais garantie par elle |

Le déploiement peut utiliser la structural readiness pour décider si le process
est correctement installé ; le feu vert de production doit exiger un smoke test
explicite (génération réelle de tokens).

| Sonde | Sémantique | Usage | Codes |
|-------|-----------|-------|-------|
| `GET /health` | **Liveness** — le process répond | nginx, `systemd`, redémarrage auto | `200` toujours si le process répond |
| `GET /ready` | **Readiness structurelle stricte** | mise en/hors rotation, `update.sh` | `200` prêt / `503` pas prêt |

### /health (liveness)

```bash
curl -s http://127.0.0.1:8000/health
```

```json
{"status": "ok", "models_loaded": ["qwen3.5-9b-q5_k_m"], "vram_used_gb": 6.0, "vram_available_gb": 37.6}
```

Un `ok` **sans modèle chargé** est normal : le modèle charge à la demande. Ne pas
utiliser `/health` pour décider si l'instance doit recevoir du trafic — c'est le
rôle de `/ready`. Le format de `/health` est inchangé et ne fait aucun contrôle
structurel : il ne peut donc jamais contredire `/ready`.

### /ready (readiness structurelle)

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/ready
```

Codes HTTP, exploitables tels quels par `systemd`, nginx et `update.sh` :

| Code | Signification |
|------|---------------|
| `200` | Tous les contrôles structurels critiques passent. |
| `503` | Au moins un contrôle critique échoue ; `reason` porte son code. |

#### Ce que /ready garantit

Contrôles effectués, dans l'ordre d'évaluation (`reason` = code du **premier**
contrôle critique en échec) :

| Contrôle | Vérifie | Critique | Mode cluster |
|---|---|---|---|
| `models_config` | `MODELS_CONFIG_PATH` présent et lisible | oui | identique |
| `enabled_models` | au moins un modèle `enabled: true` dans le registre | oui | identique |
| `secrets` | `ADMIN_SECRET` / `INTERNAL_API_KEY` (ou `AGENT_SECRET`) non laissés à une valeur `CHANGE_ME_*` | **non** (`warn`) | identique, sur `AGENT_SECRET` |
| `admin_secret` | `ADMIN_SECRET` présent, non-placeholder et d'au moins 32 caractères (routes `/admin` fail-closed sinon) | oui | identique |
| `llama_server_binary` | `LLAMA_SERVER_BIN` existe et est exécutable | oui | `skip` — délégué aux node-agents |
| `model_files` | pour chaque modèle **activé** : GGUF présent et lisible, plus `mmproj_path` si la capability `vision` est déclarée | oui | `skip` — délégué aux node-agents |
| `database` | répertoire de la base inscriptible (WAL crée `-wal`/`-shm`) et fichier inscriptible s'il existe | oui | identique |
| `log_dir` | `LOG_DIR` inscriptible | **non** (`warn`) | identique |
| `vram_budget_fit` | au moins un modèle activé tient dans le budget VRAM net | oui | `skip` — budgets lus sur les nœuds |
| `cluster_nodes_config` | `CLUSTER_NODES_PATH` présent et lisible | oui | *cluster uniquement* (`skip` en local) |
| `cluster_nodes_online` | au moins un node-agent en ligne | oui | *cluster uniquement* (`skip` en local) |
| `serving_capacity` | un modèle est déjà prêt, **ou** il reste de la capacité pour en charger un | oui | identique |

#### Ce que /ready ne garantit PAS

- **Qu'un modèle génère effectivement des tokens.** Aucun chargement n'est
  déclenché, aucune inférence n'est tentée. C'est la serving readiness, prouvée
  seulement par le smoke test de mise à jour.
- **Que les GGUF sont intègres.** `/ready` fait un `stat`, jamais une lecture ni
  un hash : un fichier de 0 octet satisfait le contrôle de présence. La
  vérification SHA-256 (`sha256:` dans `models.yaml`) a lieu au démarrage puis
  **à chaque transition vers LOADING** (SEC-ART-001), hors event loop et avec un
  cache attesté — un fichier inchangé n'est haché qu'une fois par processus.
- **Que le binaire llama-server est d'une version patchée.** L'épinglage
  (`LLAMA_SERVER_MIN_BUILD`) est appliqué au démarrage : `/ready` ne lance aucun
  sous-processus.
- **Qu'un modèle désactivé est installé.** Seuls les modèles **activés** sont
  contrôlés : retirer volontairement le GGUF d'un modèle `enabled: false` ne
  rend jamais la gateway non-ready.
- **En mode cluster : que les nœuds possèdent le binaire et les GGUF.** Sur
  l'orchestrateur, ces artefacts n'existent pas ; l'équivalent structurel est
  « inventaire des nœuds lisible **et** au moins un nœud en ligne ». Chaque
  node-agent applique ses propres contrôles (`node_agent/preflight.py`) et
  revalide au chargement, sur le nœud qui possède réellement les fichiers.

#### Forme de la réponse

Corps public (appelant non privilégié) — ni chemin de fichier, ni URL de nœud,
ni secret : uniquement des identifiants de contrôle et des codes stables.

```json
{
  "status": "ready",
  "level": "structural",
  "levels": {"liveness": true, "structural": true, "serving": false},
  "mode": "local",
  "models_ready": [],
  "vram_available_gb": 37.6,
  "checks": {
    "models_config": "pass",
    "enabled_models": "pass",
    "secrets": "pass",
    "llama_server_binary": "pass",
    "model_files": "pass",
    "database": "pass",
    "log_dir": "pass",
    "vram_budget_fit": "pass",
    "cluster_nodes_config": "skip",
    "cluster_nodes_online": "skip",
    "serving_capacity": "pass"
  }
}
```

- `level` : `none` (non-ready) / `structural` / `serving`.
- `status` : `ready` dès que `level != "none"`.
- `reason` : présent **uniquement** en `503`, code du premier contrôle critique
  en échec (`llama_server_missing`, `model_file_missing`, `no_enabled_model`,
  `database_not_writable`, `all_nodes_offline`,
  `no_model_ready_and_no_capacity`…). Ces codes sont stables et destinés à être
  testés par script.
- `nodes_online` : présent en mode cluster seulement.
- Chaque statut de contrôle vaut `pass`, `fail`, `warn` ou `skip`. Un `warn` ne
  bloque jamais : une gateway saine ne doit pas sortir de rotation à cause d'un
  contrôle trop zélé.

Exemple de `503` :

```json
{"status": "not_ready", "level": "none",
 "levels": {"liveness": true, "structural": false, "serving": false},
 "mode": "local", "models_ready": [], "vram_available_gb": 37.6,
 "checks": {"models_config": "pass", "enabled_models": "pass",
            "llama_server_binary": "fail", "model_files": "skip"},
 "reason": "llama_server_missing"}
```

#### Diagnostic détaillé (appelant privilégié)

Les messages actionnables contiennent des chemins de fichiers : ils ne sont
**jamais** dans le corps public. Un appelant présentant `ADMIN_SECRET` — même
niveau de confiance que les routes `/admin/*` — reçoit en plus un tableau
`details` :

```bash
curl -s -H "Authorization: Bearer $ADMIN_SECRET" http://127.0.0.1:8000/ready | jq '.details'
```

```json
[{"name": "llama_server_binary", "status": "fail",
  "code": "llama_server_missing",
  "message": "Binaire llama-server introuvable : /opt/llama.cpp/current/llama-server. Compilez/installez llama.cpp ou corrigez LLAMA_SERVER_BIN.",
  "critical": true}]
```

Un `ADMIN_SECRET` laissé à sa valeur d'exemple ne privilégie personne
(fail-closed). `nginx.conf` proxifie explicitement la forme publique de
`/ready`, sans chemin ni détail d'infrastructure, afin qu'un load balancer
puisse décider du routage. Les détails actionnables restent conditionnés au
bearer admin et le plan de contrôle direct est recommandé pour leur lecture.

#### Coût et chemin chaud

`/ready` est sondée par `systemd`, nginx et les scripts de déploiement. Les
contrôles ne font donc que des `stat`/`access` — aucune lecture de fichier,
aucun hash, aucun sous-processus, aucune connexion SQLite. Les appels système
sont malgré tout exécutés hors de la boucle d'événements (`asyncio.to_thread`)
et mémorisés pendant `READINESS_CACHE_TTL_SECONDS` (défaut `15`, `0` pour
désactiver). Le cache est invalidé automatiquement dès que la configuration ou
la liste des modèles activés change : une mutation via l'API admin est visible
immédiatement. Un pic d'appels concurrents ne déclenche qu'une seule sonde.

L'état de service (`models_ready`, `vram_available_gb`, `nodes_online`) est lu à
chaque appel — il vient de la mémoire du gestionnaire de modèles, coût nul.

#### Réutilisation par la CLI

Les contrôles vivent dans `gateway/readiness.py` et sont réutilisables hors HTTP :

```python
from readiness import evaluate_readiness

report = await evaluate_readiness(manager, config=settings, use_cache=False)
report.structural_ok   # bool
report.reason          # code du premier contrôle critique en échec, ou None
report.checks          # tuple[CheckResult] : name, status, code, message, critical
```

C'est cette API que consomme `evaruntime doctor` (AUT-012) pour son rapport
humain/JSON.

**Distinction pour la supervision :**
- Brancher `/health` sur le redémarrage automatique (`systemd`, `Restart=`).
- Brancher `/ready` sur la décision de routage (retirer une instance saturée ou
  mal installée de la rotation sans la tuer).
- Ne **pas** considérer un `/ready` à `200` comme la preuve qu'une nouvelle
  version sert correctement : exiger un smoke test générant réellement des
  tokens.

---

## 2. Exposition Prometheus

`GET /admin/metrics/prometheus` renvoie l'exposition au **format texte Prometheus
0.0.4**, protégée par `ADMIN_SECRET` (comme toutes les routes `/admin/*`, elle est
aussi restreinte au réseau campus par nginx).

```bash
export ADMIN_SECRET=$(sudo grep ADMIN_SECRET /etc/llm-gateway/env | cut -d= -f2)
curl -s "http://127.0.0.1:8000/admin/metrics/prometheus" \
  -H "Authorization: Bearer $ADMIN_SECRET"
```

Métriques exposées (noms exacts) :

| Métrique | Type | Labels | Description |
|----------|------|--------|-------------|
| `eva_requests_total` | counter | `model`, `status` | Requêtes par modèle et code HTTP (fenêtre 24h) |
| `eva_tokens_total` | counter | `model`, `type` (`prompt`/`completion`) | Tokens par modèle et type (fenêtre 24h) |
| `eva_request_latency_seconds` | gauge | `quantile` (0.5/0.95/0.99) | Percentiles de latence (fenêtre 7j) |
| `eva_vram_used_gb` | gauge | — | VRAM utilisée estimée (budget comptabilisé) |
| `eva_vram_total_gb` | gauge | — | VRAM totale du budget |
| `eva_vram_available_gb` | gauge | — | VRAM disponible estimée |
| `eva_gpu_probe_success` | gauge | `node` | Dernier inventaire GPU exploitable (`1`) ou indisponible (`0`) |
| `eva_gpu_memory_total_bytes` | gauge | `node`, `gpu_uuid` | VRAM totale mesurée du GPU visible |
| `eva_gpu_memory_used_bytes` | gauge | `node`, `gpu_uuid` | VRAM utilisée mesurée du GPU visible |
| `eva_gpu_memory_available_bytes` | gauge | `node`, `gpu_uuid` | VRAM disponible mesurée du GPU visible |
| `eva_models_loaded` | gauge | — | Nombre de modèles à l'état `ready` |
| `eva_inference_ttft_seconds` | histogram | `model`, `node`, `outcome` | Temps entre la réception de la requête et le premier delta SSE significatif (`content`, `reasoning_content` ou `tool_calls`; queue et chargement inclus) |
| `eva_model_load_seconds` | histogram | `model`, `node`, `outcome` | Durée d'un appel réel de chargement local ou distant |
| `eva_capacity_queue_wait_seconds` | histogram | `model`, `node`, `outcome` | Attente dans la queue locale de capacité VRAM/ports |
| `eva_llama_kv_cache_usage_ratio` | gauge | `model` (+ `node` en cluster) | Occupation du KV cache (0–1) |
| `eva_llama_tokens_per_second` | gauge | `model` (+ `node`) | Débit de génération |
| `eva_llama_requests_processing` | gauge | `model` (+ `node`) | Requêtes en cours d'inférence |
| `eva_llama_requests_deferred` | gauge | `model` (+ `node`) | Requêtes en attente de slot |

Propriétés importantes :

- **Robuste par construction** : chaque source indisponible (aucun modèle chargé,
  pas de `nvidia-smi`, mode cluster sans agrégation, DB vide) est silencieusement
  omise ou émise à `0` — jamais de `500`.
- **Périmètre GPU explicite** : seules les cartes sélectionnées par
  `CUDA_VISIBLE_DEVICES` produisent des séries mémoire. Une mesure indisponible
  émet `eva_gpu_probe_success{node}=0`, sans inventer de valeurs mémoire nulles.
- **Aucune fuite de prompt** : uniquement des compteurs agrégés. Aucun contenu de
  requête ou de réponse n'est exposé.
- **Cardinalité bornée** : les trois histogrammes runtime n'acceptent que les
  labels techniques `model`, `node` et `outcome`, tronquent les valeurs, limitent
  chaque métrique à 512 séries et agrègent tout dépassement dans
  `__overflow__`. Aucun identifiant utilisateur, email ou texte libre n'entre
  dans ces séries.
- **État en mémoire** : les histogrammes runtime repartent de zéro à chaque
  redémarrage du processus. Leurs buckets, `_sum` et `_count` suivent le format
  Prometheus standard ; utiliser `rate(..._count[5m])` et
  `histogram_quantile()` sur les buckets pour les alertes de tendance.
- **Fenêtres temporelles** : les compteurs `eva_requests_total` /
  `eva_tokens_total` sont calculés sur une fenêtre glissante de 24h dans
  `usage_log` ; les percentiles de latence sur 7j. Ce ne sont donc pas des
  compteurs monotones classiques — préférer `*_over_time`/gauge côté requêtes
  PromQL plutôt que `rate()`.

> Les endpoints JSON existants (`/admin/metrics/overview`, `/admin/metrics/llama`,
> etc.) restent inchangés et alimentent le dashboard. L'exposition Prometheus est
> **additive**.

---

## 3. Scraping local (Prometheus mono-binaire)

Un Prometheus mono-binaire installé sur le même hôte suffit. Comme l'endpoint
exige le bearer `ADMIN_SECRET`, on le passe via `authorization` dans le job de
scrape.

```yaml
# prometheus.yml
scrape_configs:
  - job_name: eva-gateway
    scrape_interval: 30s
    metrics_path: /admin/metrics/prometheus
    scheme: http                     # localhost ; TLS terminé par nginx en façade
    static_configs:
      - targets: ["127.0.0.1:8000"]
    authorization:
      type: Bearer
      # Éviter de commiter le secret : le lire depuis un fichier hors VCS.
      credentials_file: /etc/prometheus/eva_admin_secret
```

```bash
# Fichier ne contenant QUE le secret, permissions serrées
sudo install -m 600 /dev/null /etc/prometheus/eva_admin_secret
sudo grep ADMIN_SECRET /etc/llm-gateway/env | cut -d= -f2 \
  | sudo tee /etc/prometheus/eva_admin_secret >/dev/null
```

> **Sécurité :** ne pas exposer `/admin/metrics/prometheus` à Internet. Scraper en
> `127.0.0.1` (ou via le réseau campus derrière nginx). Ne jamais mettre
> l'`ADMIN_SECRET` en clair dans un fichier versionné.

Pour une supervision basique sans Prometheus, un simple `curl` périodique de
`/ready` et `/health` (cf. section 1) couvre l'essentiel.

---

## 4. Métriques par nœud (mode cluster)

Chaque node-agent expose `GET /agent/metrics` (protégé par `AGENT_SECRET`),
retournant un JSON compact `{model_id: {…}}` des métriques `llama-server` de CE
nœud. Il ne renvoie **jamais** de contenu de prompt.

L'orchestrateur les agrège automatiquement : `/admin/metrics/llama` (JSON) et
`/admin/metrics/prometheus` interrogent tous les nœuds `online` et taguent chaque
échantillon par un label `node` (le `node_id`) pour éviter les collisions de
`model_id` entre nœuds. Un nœud injoignable est simplement ignoré (best-effort,
hors chemin d'inférence) — le heartbeat reste seul responsable de l'état
online/offline.

L'état des nœuds lui-même se consulte via `GET /admin/cluster` (VRAM par nœud,
`online`, échecs consécutifs, modèles chargés).

---

## 5. Règles d'alerte pragmatiques

Quelques alertes utiles, à adapter aux seuils du site. Exemples PromQL indicatifs
(les fenêtres des compteurs `eva_*` sont déjà glissantes — voir section 2).

### VRAM saturée

```promql
# Moins de 1 GB de budget VRAM disponible pendant 5 min → saturation probable
eva_vram_available_gb < 1
```

Action : vérifier les modèles chargés (`/admin/status`), la file d'admission, et
d'éventuels `llama-server` orphelins. Un warning de **dérive VRAM** (VRAM réelle
`nvidia-smi` > déclaré) apparaît dans les logs de la gateway si la réconciliation
VRAM est active (`VRAM_RECONCILE_INTERVAL_SECONDS > 0`).

### File d'admission pleine

La saturation de la queue VRAM se traduit par des `503` avec `Retry-After` sur
les requêtes d'inférence. À surveiller côté HTTP :

```promql
# Part de requêtes 503 sur 24h
sum(eva_requests_total{status="503"}) / sum(eva_requests_total) > 0.05
```

L'état exact de la queue (waiters/max) est aussi lisible via `/admin/status`
(`capacity_queue`) et `/v1/capacity`.

### Taux d'erreurs 5xx élevé

```promql
# > 5 % de 5xx sur la fenêtre 24h
(
  sum(eva_requests_total{status=~"5.."})
  /
  sum(eva_requests_total)
) > 0.05
```

Action : consulter `journalctl -u llm-gateway -p err`, vérifier `/health` et,
en cluster, l'état des nœuds.

### Nœud cluster offline

```promql
# Au moins un nœud est tombé (dérivé de /ready ou d'une sonde externe)
# À défaut de métrique dédiée, alerter sur la readiness :
```

La façon la plus simple : sonder `/ready`. En mode cluster, un `503` avec
`reason=all_nodes_offline` signale la perte totale des nœuds. Pour un suivi plus
fin par nœud, scripter une vérification de `/admin/cluster` (champ `online` par
nœud) et alerter dès qu'un `online` passe à `false`.

### Latence dégradée

```promql
# P95 de latence > 30 s (fenêtre 7j)
eva_request_latency_seconds{quantile="0.95"} > 30
```

Une latence élevée peut simplement refléter des chargements de modèles à froid
(première requête) ou une file d'admission active ; corréler avec
`eva_models_loaded` et `eva_llama_requests_deferred`.
