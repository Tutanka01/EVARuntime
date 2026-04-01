# Architecture technique — Cluster EVA Inference Gateway

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
│  │ openai   │  │ LangChain│   │ curl → /admin/metrics/*          │     │
│  └────┬─────┘  └────┬─────┘   └────────────────┬─────────────────┘     │
│       └─────────────┘                           │                       │
│                         │ HTTPS / TLS 1.3       │ HTTPS (campus only)   │
└─────────────────────────┼─────────────────────────────────────────────┘
                          │                       │
┌─────────────────────────┼───────────────────────┼─────────────────────┐
│  Cluster EVA — hébergé à l'UPPA (GPU L40S)      │                     │
│                         ▼                       ▼                     │
│  ┌──────────────────────────────────────────────────────────────────┐ │
│  │  nginx  (TLS termination, rate limiting, IP filtering /admin)    │ │
│  └──────────────────────────┬─────────────────────────────────────┘ │
│                              │ HTTP/1.1 (127.0.0.1:8000)             │
│  ┌───────────────────────────▼──────────────────────────────────────┐ │
│  │                    FastAPI Gateway                                │ │
│  │                                                                   │ │
│  │  ┌─────────────┐  ┌──────────────┐  ┌──────────────────────────┐ │ │
│  │  │    auth.py  │  │rate_limiter  │  │     proxy.py             │ │ │
│  │  │  Bearer SHA │  │sliding window│  │  forward + SSE streaming  │ │ │
│  │  └─────────────┘  └──────────────┘  └────────────┬─────────────┘ │ │
│  │                                                   │               │ │
│  │  ┌─────────────────────────────┐  ┌──────────────┴─────────────┐ │ │
│  │  │  metrics.py + dashboard.html│  │   server_manager.py        │ │ │
│  │  │  /admin/metrics/*  (JSON)   │  │  UNLOADED→LOADING→READY    │ │ │
│  │  │  /admin/dashboard  (HTML)   │  │  asyncio.Lock + Event      │ │ │
│  │  └─────────────┬───────────────┘  └──────────────┬─────────────┘ │ │
│  │                │ (lit usage_log,                  │               │ │
│  │                │  proxie /metrics)    subprocess  │               │ │
│  │  ┌─────────────▼───────────────┐  ┌──────────────▼─────────────┐ │ │
│  │  │  SQLite WAL (database.py)   │  │  llama-server (llama.cpp)  │ │ │
│  │  │  users | api_keys           │  │  port 8081 — 127.0.0.1     │ │ │
│  │  │  usage_log                  │  │  --metrics (Prometheus)    │ │ │
│  │  └─────────────────────────────┘  └──────────────┬─────────────┘ │ │
│  │                                                   │ CUDA          │ │
│  │                                   ┌──────────────▼─────────────┐ │ │
│  │                                   │  NVIDIA L40S 48GB          │ │ │
│  │                                   │  Chargé : ~40.5GB VRAM     │ │ │
│  │                                   │  Déchargé : ~0.2GB         │ │ │
│  │                                   └────────────────────────────┘ │ │
│  └───────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Flux d'une requête

### 1. Requête authentifiée, modèle prêt

```
Client          nginx           FastAPI         llama-server     GPU
  │               │                │                  │           │
  │─POST /v1/──→  │                │                  │           │
  │  chat/comp.   │─forward──────→ │                  │           │
  │               │                │─check auth       │           │
  │               │                │  (SHA-256 lookup DB)         │
  │               │                │─check rate limit │           │
  │               │                │  (sliding window)            │
  │               │                │─ensure_loaded()  │           │
  │               │                │  state==READY ✓  │           │
  │               │                │─forward POST ──→ │           │
  │               │                │                  │─infer──→  │
  │               │                │                  │←──────────│
  │               │                │←─── response ────│           │
  │               │                │ (log_usage async)│           │
  │←──────────────│←─── response ──│                  │           │
```

**Latence typique (modèle chargé) :**
- Auth + rate limit : < 5ms (in-memory)
- Génération courte (100 tokens) : 2–4s
- Génération longue (1000 tokens) : 15–30s

### 2. Requête avec chargement du modèle

```
Client          nginx           FastAPI         llama-server     GPU
  │               │                │                  │           │
  │─POST /v1/──→  │─forward──────→ │                  │           │
  │               │                │─ensure_loaded()  │           │
  │               │                │  state==UNLOADED │           │
  │               │                │  → acquire Lock  │           │
  │               │                │  → spawn process─────────→  │
  │               │                │  → poll /health  │           │
  │               │                │  [60-120s]       │─load──→   │
  │               │                │                  │←──────────│
  │               │                │  /health → "ok" ✓│           │
  │               │                │  state → READY   │           │
  │               │                │  Event.set()     │           │
  │               │                │─forward POST ──→ │           │
  │←──────────────│←─── response ──│←─── response ────│           │
```

### 3. Requêtes concurrentes pendant le chargement

```
Client A        Client B        FastAPI
    │               │              │
    │─POST ────────────────────→  │
    │               │              │  state=LOADING, Event créé
    │               │─POST ──────→ │  state==LOADING → await Event
    │               │              │  [les deux attendent l'Event]
    │               │              │
    │               │              │  ... chargement ...
    │               │              │
    │               │              │  Event.set() → les deux repartent
    │←── response ──│←── response ─│  en parallèle sur le modèle chargé
```

**Invariant :** aucune requête n'est perdue. Le `asyncio.Lock` garantit
qu'un seul coroutine lance le subprocess. L'`asyncio.Event` débloque tous
les waiters simultanément dès que le modèle est prêt.

---

## Décision clé : subprocess vs service séparé

### Pourquoi llama-server n'est pas un service systemd séparé

Deux approches étaient possibles :

**Option A — Service systemd séparé (rejeté)**
```
systemd → llm-gateway.service (FastAPI)
systemd → llama-server.service (llama.cpp)
```

Problème : si on utilise `--sleep-idle-seconds` pour décharger le modèle,
le processus llama-server reste vivant avec un contexte CUDA actif (~600MB).
Pour libérer 100% la VRAM, il faut tuer le processus. Mais si c'est un service
systemd indépendant, systemd le redémarre immédiatement.

**Option B — Subprocess géré par la gateway (adopté)**
```
systemd → llm-gateway.service (FastAPI)
               └── subprocess → llama-server (llama.cpp)
```

La gateway peut tuer le subprocess à volonté. Le système d'exploitation
récupère toute la mémoire allouée par le processus fils, y compris la VRAM GPU.

**Preuve :** `nvidia-smi` après `os.killpg(pgid, SIGTERM)` montre < 500MB utilisés
(driver NVIDIA uniquement, pas de contexte CUDA).

### Pourquoi `start_new_session=True`

```python
self._process = await asyncio.create_subprocess_exec(
    *cmd,
    start_new_session=True,  # ← crée un nouveau process group
)
```

Sans cette option, `os.killpg()` tuerait aussi la gateway elle-même
(car elle fait partie du même process group). Avec `start_new_session=True`,
llama-server obtient son propre process group, qu'on peut tuer proprement
sans affecter la gateway.

---

## Sécurité

### Séparation des clés

```
Utilisateur ──→ clé_utilisateur (llmgw-xxx) ──→ hash SHA-256 en DB
                                                                │
Gateway ──────→ INTERNAL_API_KEY ────────────→ llama-server    │
                (jamais exposée)              (127.0.0.1 only)  │
                                                                │
DB stocke uniquement : key_hash, key_prefix (8 chars lisibles) ←┘
                       jamais : raw_key
```

**Propriété :** même en accès total à la base de données, un attaquant ne peut
pas retrouver les clés API des utilisateurs (SHA-256 non-inversible sans bruteforce).

### Isolation réseau

```
Internet ──→ nginx :443 ──→ FastAPI :8000 (127.0.0.1 only)
                                    ──→ llama-server :8081 (127.0.0.1 only)

/admin/* : allow 10.0.0.0/8 (campus) + deny all
```

llama-server écoute uniquement sur `127.0.0.1` — il n'est jamais accessible
depuis le réseau, même en cas de mauvaise configuration nginx.

### Pas de stockage de secrets

```python
# Ce qu'on stocke en DB (database.py)
key_hash   = SHA-256(raw_key)   # non-inversible
key_prefix = raw_key[:14]       # pour identification humaine uniquement

# Ce qu'on ne stocke jamais
raw_key    # affiché une seule fois à la création, puis oublié
```

---

## Base de données SQLite WAL

### Pourquoi SQLite et pas PostgreSQL

Pour une centaine d'utilisateurs avec des accès intermittents, SQLite suffit.
PostgreSQL apporterait de la complexité sans bénéfice réel.

Le mode WAL (Write-Ahead Log) est activé car :
- Il permet des lectures concurrentes pendant les écritures (important car
  on lit pour l'auth pendant qu'on log l'usage)
- Il est plus performant pour les workloads en lecture-majorité
- Il évite les corruptions en cas d'arrêt brutal

```python
# Pragmas appliqués (database.py)
PRAGMA journal_mode = WAL;       # concurrent reads + single writer
PRAGMA synchronous  = NORMAL;    # performance sans risque de corruption
PRAGMA cache_size   = -65536;    # 64MB cache mémoire
PRAGMA foreign_keys = ON;        # intégrité référentielle
PRAGMA temp_store   = MEMORY;    # temp tables en RAM
```

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

Index : usage_log(user_id, timestamp), usage_log(timestamp),
        api_keys(key_hash), api_keys(user_id)
```

### Performances d'auth

Le path critique auth (à chaque requête) :
1. Hash SHA-256 du token entrant : ~0.1ms
2. Lookup `api_keys` par `key_hash` (index) : < 1ms
3. JOIN `users` : < 1ms
4. Update `last_used` : fire-and-forget (hors du critical path)

Total auth : < 2ms pour 99% des requêtes.

---

## Rate limiting in-memory

### Pourquoi pas Redis

Redis est sur-dimensionné pour ce cas d'usage. L'état du rate limiter
est en mémoire dans le processus Python :

```python
# rate_limiter.py
_windows: dict[int, deque[float]] = {}
# user_id → deque de timestamps dans la fenêtre d'1 minute
```

**Propriété :** si la gateway redémarre, les compteurs se remettent à zéro.
C'est acceptable — les limites sont par minute, pas par heure.

### Algorithme sliding window log

Versus le token bucket (plus simple), le sliding window offre une fenêtre
glissante précise sans les pics en début de fenêtre fixe.

```
t=0   t=10  t=20  t=30  t=40  t=50  t=60  t=70
 │     │     │     │     │     │     │     │
 R     R     R           R     R     R        ← requêtes (R)
                                     │
                    fenêtre de 60s ──┤
                    [t=10 → t=70]    │
                    3 requêtes dans  │
                    la fenêtre       │
```

---

## Streaming SSE — flux technique

```
Client                nginx               FastAPI             llama-server
  │                     │                    │                     │
  │─POST stream:true──→ │                    │                     │
  │                     │─forward──────────→ │                     │
  │                     │                    │─POST /v1/chat ────→ │
  │                     │                    │                     │─generate
  │                     │                    │←── chunk1 ──────────│
  │                     │←── chunk1 ─────────│                     │─generate
  │←── chunk1 ──────────│                    │                     │
  │                     │                    │←── chunk2 ──────────│
  │←── chunk2 ──────────│                    │                     │
  ...                  ...                  ...                   ...
  │←── data: [DONE] ────│                    │                     │
```

**Points critiques nginx :**
```nginx
proxy_buffering        off;   # ne pas bufferiser côté nginx
add_header X-Accel-Buffering no always;  # signal upstream
chunked_transfer_encoding on;            # HTTP/1.1 chunked
proxy_read_timeout     600s;  # 10min pour les longues générations
```

Sans `proxy_buffering off`, nginx accumule tous les chunks et envoie
la réponse complète en une fois — le streaming est annulé.

---

## Paramètres llama-server — justification

### `-ngl 999` (GPU layers)

999 est un sentinel signifiant "offloader toutes les couches disponibles".
llama-server le plafonne automatiquement au nombre réel de couches du modèle.
On ne met pas le nombre exact de couches (ex: 80 pour 70B) car il est
différent selon les architectures.

### `-c 32768 --parallel 4` (contexte et parallélisme)

```
ctx_size = tokens_per_slot × n_parallel
32768    = 8192             × 4

VRAM KV cache (Q8) ≈ 2 × layers × heads × head_dim × ctx_size × sizeof(q8)
                   ≈ 2 × 80 × 8 × 128 × 32768 × 1 octet
                   ≈ ~2.7 GB
```

4 slots parallèles = 4 utilisateurs peuvent générer simultanément.
Au-delà, les requêtes supplémentaires attendent qu'un slot se libère
(géré nativement par llama-server avec continuous batching).

### `-ctk q8_0 -ctv q8_0` (KV cache quantization)

Le KV cache en FP16 (défaut) utiliserait ~5GB pour ce contexte.
En Q8_0, il tombe à ~2.7GB avec une dégradation de perplexité de +0.003
(imperceptible). C'est le meilleur compromis qualité/VRAM.

### `-fa on` (Flash Attention)

Flash Attention 2 est supporté sur Ada Lovelace (compute capability 8.9).
Il réduit la mémoire d'attention de O(n²) à O(n) et améliore le débit
de préfill de ~15%. Activé inconditionnellement.

### `--cont-batching` (continuous batching)

Sans continuous batching, les slots d'inférence ne commencent un nouveau
token que quand **tous** les slots ont terminé leur génération en cours.
Avec continuous batching, chaque slot avance indépendamment : le GPU
est utilisé de façon optimale même avec des requêtes de longueurs variables.

---

## Couche monitoring (dashboard)

### Composants

| Fichier | Rôle |
|---------|------|
| `metrics.py` | Router FastAPI sous `/admin/metrics/` — agrège les données et les expose en JSON |
| `static/dashboard.html` | SPA auto-contenue (Chart.js CDN + vanilla JS) servie par `GET /admin/dashboard` |

### Flux de données du dashboard

```
Navigateur admin
  │
  ├─ GET /admin/dashboard ──→ HTMLResponse(dashboard.html)   [synchronous file read]
  │
  └─ GET /admin/metrics/overview
     GET /admin/metrics/timeseries?period=24h
     GET /admin/metrics/users?period=30d
     GET /admin/metrics/status-codes
     GET /admin/metrics/llama
            │
            ├─ usage_log / users (aiosqlite, index idx_usage_timestamp)
            │
            └─ llama-server :8081/metrics (Prometheus text → parsing Python → JSON)
                 ↑ retourne {} si état ≠ READY (pas d'erreur pour le dashboard)
```

### Calcul des percentiles de latence

SQLite ne supporte pas `PERCENTILE_CONT`. Les percentiles (P50/P95/P99) sont
calculés en Python depuis des échantillons bruts :

```python
samples = await db.get_latency_samples(period_hours=168, limit=10_000)
samples.sort()
p95 = samples[int(0.95 * len(samples))]
```

À l'échelle universitaire (milliers à dizaines de milliers de requêtes), ce calcul
reste sous 10ms. Si le volume venait à dépasser ~100k requêtes par semaine, envisager
une pré-agrégation horaire dans un job cron.

### Sécurité du dashboard

Le dashboard ne stocke **aucune donnée sensible** :
- Pas de contenu de prompt ou de réponse
- Pas de clé API (ni hash ni préfixe)
- Pas d'adresse IP des utilisateurs

Le token admin est stocké dans `sessionStorage` (effacé à la fermeture de l'onglet,
jamais dans `localStorage`, jamais envoyé à un tiers).

La route `GET /admin/dashboard` ne requiert pas de bearer token — la protection réseau
nginx (campus IP only) est suffisante pour servir le fichier HTML. En revanche, tous
les endpoints `/admin/metrics/*` exigent le bearer token, de sorte qu'un accès direct
à l'URL de la page sans connaissance du secret n'affiche aucune donnée.

---

## Opérations de sécurité

### Rotation de l'ADMIN_SECRET

```bash
# Sur le serveur
NEW_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
sudo sed -i "s/^ADMIN_SECRET=.*/ADMIN_SECRET=$NEW_SECRET/" /etc/llm-gateway/env
sudo systemctl restart llm-gateway
echo "Nouveau secret : $NEW_SECRET"
# Mettre à jour tout script d'automatisation utilisant l'ancien secret
```

### Révocation d'urgence de tous les accès

```bash
# Désactiver tous les utilisateurs sauf l'admin
sqlite3 /var/lib/llm-gateway/gateway.db \
  "UPDATE users SET is_active = 0;"

# Ou révoquer toutes les clés
sqlite3 /var/lib/llm-gateway/gateway.db \
  "UPDATE api_keys SET is_active = 0;"

# Effet immédiat — aucun redémarrage nécessaire
```

### Audit des accès suspects

```bash
# Utilisateurs avec le plus grand volume de tokens ce mois
sqlite3 /var/lib/llm-gateway/gateway.db "
SELECT u.username, COUNT(*) as reqs, SUM(l.total_tokens) as tokens
FROM usage_log l JOIN users u ON u.id = l.user_id
WHERE l.timestamp >= date('now', 'start of month')
GROUP BY u.id ORDER BY tokens DESC LIMIT 10;"

# Requêtes depuis une IP spécifique (dans les logs nginx)
sudo grep "1.2.3.4" /var/log/nginx/access.log | tail -50
```
