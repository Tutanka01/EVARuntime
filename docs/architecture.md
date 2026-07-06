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

Le fichier `models.yaml` est la source de vérité unique pour tous les modèles
disponibles sur la gateway. Il est lu au démarrage et peut être modifié en
direct via l'API admin (écriture atomique : temp + rename).

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
    vram_gb: 6.0
    capabilities: [text_generation, vision, streaming]
    ...
```

Le champ `mmproj_path` est transmis à llama-server via le flag `--mmproj` uniquement
quand `vision` est présent dans `capabilities`. Sans ce fichier, llama-server démarre
normalement mais retourne HTTP 500 sur toute requête contenant une image.

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
2. `model_id` validé par regex `^[a-z0-9][a-z0-9._-]*$` (pas de `/`, `..`, espaces)
3. `path` doit être absolu (`path.is_absolute()`) et pointer vers un `.gguf`
4. `mmproj_path`, si présent, subit les mêmes validations que `path`
5. Si `ALLOWED_MODEL_DIRS` est configuré : `path` et `mmproj_path` doivent être sous un répertoire autorisé
6. `vram_gb > 0` et `ubatch_size ≤ batch_size`
7. Warning si `vision` ∈ capabilities mais `mmproj_path` absent (HTTP 500 garanti sinon)

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

`ServerManager` maintient un buffer circulaire des 30 dernières lignes de stderr
(`deque(maxlen=30)` alimenté par `_drain_logs`). Quand `_wait_for_health` détecte
un returncode non nul, il attend 150ms pour laisser le drain vider le pipe, puis
construit le message d'erreur avec le tail :

```python
raise RuntimeError(
    f"llama-server '{model.id}' s'est terminé prématurément (code {returncode}).\n"
    f"Stderr (dernières {n} lignes) :\n  {tail_text}"
)
```

Ce message est propagé dans les logs gateway au niveau ERROR et dans la réponse
HTTP 503 retournée au client, ce qui rend le diagnostic immédiat sans avoir à
ouvrir le fichier de log du sous-processus.

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
DB stocke uniquement : key_hash, key_prefix (8 chars)  ◄───────┘
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
| Injection model_id | Regex `^[a-z0-9][a-z0-9._-]*$` sur tous les model_id |
| GGUF substitué / corrompu | Champ `sha256` optionnel — vérification d'intégrité au chargement |

### Durcissement de llama.cpp (supply-chain)

`llama-server` est un binaire tiers exposé à des CVEs (2025-2026) : écriture
hors-bornes non authentifiée via `n_discard`/context-shift (GHSA-8947-pfff-2f3c)
et overflows de parsing GGUF menant au RCE. Trois garde-fous :

| Mesure | Mise en œuvre |
|--------|---------------|
| `--context-shift` désactivé | `build_llama_cmd` n'émet **jamais** ce flag — c'est le vecteur de la CVE `n_discard`. |
| Épinglage de version | `LLAMA_SERVER_MIN_BUILD` : au démarrage, `llama-server --version` est sondé ; un build inférieur au minimum refuse le démarrage (0 = désactivé, non fatal si version illisible). |
| Intégrité GGUF | Champ `sha256` par modèle : le hash du fichier est recalculé et comparé avant chargement. |

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
-- Pragmas appliqués (database.py)
PRAGMA journal_mode = WAL;       -- concurrent reads + single writer
PRAGMA synchronous  = NORMAL;    -- performance sans risque de corruption
PRAGMA cache_size   = -65536;    -- 64MB cache mémoire
PRAGMA foreign_keys = ON;        -- intégrité référentielle
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

Le champ `model` dans `usage_log` stocke l'ID du modèle tel que résolu
par le routing (ex : `"llama-3.3-70b-instruct"`), permettant les rapports
d'usage par modèle.

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

**Points critiques nginx :**
```nginx
proxy_buffering        off;
add_header X-Accel-Buffering no always;
chunked_transfer_encoding on;
proxy_read_timeout     600s;
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

### Réconciliation VRAM via `nvidia-smi`

Une tâche périodique (`start_vram_reconcile()`) compare la VRAM réelle mesurée
par `nvidia-smi` à la somme des `vram_gb` déclarés des modèles READY. Une dérive
significative (VRAM réelle > déclaré × (1 + seuil), et > 512 Mo de bruit)
déclenche un **warning** invitant à chercher des `llama-server` orphelins.
Purement diagnostique — aucune éviction n'est déclenchée. Si `nvidia-smi` est
absent, la sonde renvoie `None` et rien n'est fait (aucun champ trompeur dans
`status()`). Après une sonde réussie, `status()` ajoute `gpu_used_mb_measured` et
`vram_drift_mb` au bloc `vram_budget`.

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

### Sécurité du dashboard

- Pas de contenu de prompt ou de réponse
- Pas de clé API (ni hash ni préfixe)
- Token admin dans `sessionStorage` (jamais `localStorage`)
- La page HTML est servie sans auth — les données JSON exigent le bearer token

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
requête sur ce modèle, un `load_model()` **idempotent** (`already_loaded` côté
agent, pas de rechargement réel) récupère la vraie clé et l'URL de confiance.
L'URL `llama-server` est toujours reconstruite à partir de l'hôte du `base_url`
du nœud (source de confiance `nodes.yaml`), jamais d'une valeur renvoyée par
l'agent.

### Déchargement sans purge optimiste

Si un `unload_model` échoue (nœud flaky), l'état local **n'est pas purgé de façon
optimiste** : le `llama-server` tourne peut-être encore et occupe sa VRAM.
`_resync_after_failed_unload` re-synchronise via `health()` et ne libère l'entrée
locale **que** si le nœud confirme l'absence du modèle. Sinon la VRAM reste
comptée comme occupée pour éviter une sur-réservation au placement suivant.

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
