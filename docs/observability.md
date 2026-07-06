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

La gateway expose deux sondes non authentifiées, aux rôles distincts.

| Sonde | Sémantique | Usage | Codes |
|-------|-----------|-------|-------|
| `GET /health` | **Liveness** — le process répond | nginx, `systemd`, redémarrage auto | `200` toujours si le process répond |
| `GET /ready` | **Readiness** — la gateway peut servir une requête d'inférence | load balancer, mise en/hors rotation | `200` prêt / `503` pas prêt |

### /health (liveness)

```bash
curl -s http://127.0.0.1:8000/health
```

```json
{"status": "ok", "models_loaded": ["qwen3.5-9b-q5_k_m"], "vram_used_gb": 6.0, "vram_available_gb": 37.6}
```

Un `ok` **sans modèle chargé** est normal : le modèle charge à la demande. Ne pas
utiliser `/health` pour décider si l'instance doit recevoir du trafic — c'est le
rôle de `/ready`.

### /ready (readiness)

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/ready
```

`200` si au moins un modèle est déjà `ready`, **ou** s'il reste de la capacité
VRAM (mode local) / au moins un nœud online (mode cluster). Sinon `503` avec une
`reason` :

```json
{"status": "not_ready", "models_ready": [], "vram_available_gb": 0.0,
 "reason": "no_model_ready_and_no_capacity"}
```

En mode cluster, le corps ajoute `nodes_online` ; la raison devient
`all_nodes_offline` si tous les nœuds sont tombés. Le corps ne divulgue aucune
infra sensible (ni chemin fichier, ni URL).

**Distinction pour la supervision :**
- Brancher `/health` sur le redémarrage automatique (`systemd`, `Restart=`).
- Brancher `/ready` sur la décision de routage (retirer une instance saturée de
  la rotation sans la tuer).

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
| `eva_models_loaded` | gauge | — | Nombre de modèles à l'état `ready` |
| `eva_llama_kv_cache_usage_ratio` | gauge | `model` (+ `node` en cluster) | Occupation du KV cache (0–1) |
| `eva_llama_tokens_per_second` | gauge | `model` (+ `node`) | Débit de génération |
| `eva_llama_requests_processing` | gauge | `model` (+ `node`) | Requêtes en cours d'inférence |
| `eva_llama_requests_deferred` | gauge | `model` (+ `node`) | Requêtes en attente de slot |

Propriétés importantes :

- **Robuste par construction** : chaque source indisponible (aucun modèle chargé,
  pas de `nvidia-smi`, mode cluster sans agrégation, DB vide) est silencieusement
  omise ou émise à `0` — jamais de `500`.
- **Aucune fuite de prompt** : uniquement des compteurs agrégés. Aucun contenu de
  requête ou de réponse n'est exposé.
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
