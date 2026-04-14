# EVARuntime — Gateway d'Inférence LLM Souveraine

> **Un serveur de modèles de langage puissant, sécurisé et économe en énergie, conçu pour l'enseignement supérieur et la recherche.**

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688.svg)](https://fastapi.tiangolo.com/)
[![llama.cpp](https://img.shields.io/badge/llama.cpp-GPU%20CUDA-orange.svg)](https://github.com/ggml-org/llama.cpp)
[![OpenAI Compatible](https://img.shields.io/badge/API-OpenAI%20Compatible-green.svg)](https://platform.openai.com/docs/api-reference)
[![License](https://img.shields.io/badge/License-UPPA-lightgrey.svg)]()

---

## Pourquoi EVARuntime ?

Les universités et laboratoires de recherche ont un besoin croissant d'accès aux grands modèles de langage (LLMs), mais font face à trois défis majeurs :

| Défi | Solution EVARuntime |
|------|---------------------|
| **Souveraineté des données** | Tout reste sur vos serveurs — aucune donnée ne quitte votre infrastructure |
| **Coût et énergie** | Le GPU est libéré automatiquement quand il n'est pas utilisé : **~30W vs ~350W** |
| **Simplicité d'usage** | API compatible OpenAI — vos outils existants fonctionnent sans modification |

EVARuntime transforme un serveur GPU NVIDIA en une plateforme d'inférence LLM **production-ready**, avec gestion des accès, traçabilité complète et administration simplifiée.

---

## Fonctionnalités clés

### Inference haute performance
- **OpenAI-compatible** — Drop-in replacement : changez juste l'URL et la clé API
- **Streaming temps réel** — Réponse token par token via Server-Sent Events (SSE)
- **Multi-utilisateurs concurrents** — 4 slots parallèles avec continuous batching
- **Flash Attention 2** — Accélération optimisée pour architecture Ada Lovelace

### Gestion intelligente du GPU
- **Multi-modèles simultanés** — Plusieurs modèles en VRAM en même temps, dans la limite du budget
- **Chargement automatique** — Le modèle se charge à la première requête (~60-90s)
- **Déchargement automatique** — Après inactivité, le GPU est entièrement libéré
- **Éviction LRU** — Quand la VRAM ou les ports sont saturés, le modèle le moins utilisé est déchargé pour faire de la place
- **Aucune VRAM résiduelle** — Contrairement au mode sleep, 100% de la mémoire est rendue
- **Zéro requête perdue** — Les requêtes arrivant pendant le chargement sont mises en attente et traitées
- **Support MoE avec `--cpu-moe`** — Les experts FFN des modèles MoE peuvent être déportés sur RAM CPU

### Sécurité renforcée
- **Clés API hachées SHA-256** — Les clés brutes ne sont jamais stockées
- **Authentification double** — Secret admin + filtrage IP réseau campus
- **Isolation réseau** — Le moteur d'inférence n'est accessible qu'en local (127.0.0.1)
- **Hardening système** — Service systemd avec NoNewPrivileges, PrivateTmp, ProtectSystem

### Contrôle d'accès et traçabilité
- **Rate limiting par utilisateur** — Algorithme sliding window, configurable en RPM
- **Quotas mensuels** — Contrôle de la consommation de tokens par utilisateur
- **Journalisation complète** — Chaque requête est enregistrée (tokens, durée, statut)
- **Rapports d'usage** — Vue agrégée et détaillée de la consommation

### Administration simple
- **CLI riche** — Gestion des utilisateurs, clés et rapports en ligne de commande
- **API REST admin** — Automatisation et intégration avec vos outils
- **Hot-reload des paramètres** — `PATCH /admin/models/{id}` modifie `llama_params` à chaud (cpu_moe, ctx_size…) sans redémarrage
- **Diagnostic de crash** — Les dernières lignes stderr de llama-server apparaissent dans l'erreur retournée (CUDA OOM, mauvais chemin…)
- **Installation idempotente** — Un script qui configure tout : venv, systemd, nginx, TLS

---

## Architecture

```
Internet ──→ Nginx (TLS, SSE no-buffer, IP filtering)
                │
                ▼
         FastAPI Gateway (port 8000)
         ├── Authentification & rate limiting
         ├── ModelManager — budget VRAM, éviction LRU, pool de ports
         └── Base SQLite WAL (users, clés, logs)
                │
                ├──→ llama-server :8081  (modèle A — ex: Llama 70B, 42 GB)
                ├──→ llama-server :8082  (modèle B — ex: Qwen MoE, 7 GB)
                └──→ ... (jusqu'à max_loaded_models simultanément)
                     NVIDIA L40S 48GB — 100% VRAM libérée si idle
```

**Choix architectural distinctif :** chaque `llama-server` est géré comme un **sous-processus** de la gateway (pas un service systemd séparé). C'est la seule approche garantissant une libération totale de la VRAM GPU — le mode `--sleep-idle-seconds` laisse un contexte CUDA de ~600 MB. Le `ModelManager` peut charger plusieurs modèles simultanément tant que leur VRAM combinée tient dans le budget, et évinçe automatiquement le moins récemment utilisé (LRU) quand la VRAM ou les slots de ports sont saturés.

---

## Démarrage rapide

### Prérequis
- Serveur Ubuntu avec GPU NVIDIA L40S 48GB (ou équivalent)
- `llama.cpp` compilé avec support CUDA
- Modèle GGUF (ex: Llama-3.3-70B-Instruct Q4_K_M)
- Certificats TLS pour votre domaine

### Installation en 3 commandes

```bash
# 1. Cloner le dépôt
git clone <votre-repo> /tmp/llm-gateway-src

# 2. Lancer le script d'installation (en root)
sudo bash /tmp/llm-gateway-src/gateway/deploy/install.sh

# 3. Suivre les instructions affichées
```

Le script configure automatiquement : utilisateur dédié, environnement Python, service systemd, reverse proxy nginx et certificats TLS.

---

## Utilisation

### Avec le client OpenAI Python

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://llm.univ-pau.fr/v1",
    api_key="llmgw-votre_cle_api",
)

response = client.chat.completions.create(
    model="llama-3.3-70b-instruct",
    messages=[{"role": "user", "content": "Explique le théorème de Bayes."}],
)
print(response.choices[0].message.content)
```

### Avec curl

```bash
curl https://llm.univ-pau.fr/v1/chat/completions \
  -H "Authorization: Bearer llmgw-votre_cle_api" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama-3.3-70b-instruct",
    "messages": [{"role": "user", "content": "Explique le théorème de Bayes."}]
  }'
```

### Streaming

```python
stream = client.chat.completions.create(
    model="llama-3.3-70b-instruct",
    messages=[{"role": "user", "content": "Rédige une introduction sur les LLMs."}],
    stream=True,
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="", flush=True)
```

> Compatible avec **LangChain**, **LiteLLM**, et tout client OpenAI standard.

---

## Administration

### CLI — Gestion des utilisateurs

```bash
cd /opt/llm-gateway

# Créer un utilisateur
sudo -u llmservice ./venv/bin/python cli.py add-user alice \
  --email alice@univ-pau.fr --rpm 30

# Générer une clé API (affichée une seule fois)
sudo -u llmservice ./venv/bin/python cli.py create-key alice --name "these-2025"

# Lister les utilisateurs
sudo -u llmservice ./venv/bin/python cli.py list-users

# Révoquer une clé
sudo -u llmservice ./venv/bin/python cli.py revoke-key llmgw-abc12345

# Rapport d'usage mensuel
sudo -u llmservice ./venv/bin/python cli.py usage-report --month 2025-03 --summary

# État du système
sudo -u llmservice ./venv/bin/python cli.py status
```

### API REST Admin

Protégée par `Authorization: Bearer <ADMIN_SECRET>` + filtrage IP réseau campus.

```bash
# Créer un utilisateur
curl -X POST https://llm.univ-pau.fr/admin/users \
  -H "Authorization: Bearer $ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"username": "bob", "email": "bob@univ-pau.fr", "rpm_limit": 20}'

# Modifier les paramètres llama-server d'un modèle (hot-reload sans redémarrage)
curl -X PATCH https://llm.univ-pau.fr/admin/models/qwen3.5-9b-q5_k_m \
  -H "Authorization: Bearer $ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"llama_params": {"n_gpu_layers": 999, "ctx_size": 32768, "parallel": 4,
       "batch_size": 2048, "ubatch_size": 512, "cache_type_k": "q8_0",
       "cache_type_v": "q8_0", "flash_attn": true, "threads": 8,
       "threads_http": 4, "cpu_moe": true}}'

# Forcer le déchargement de tous les modèles
curl -X POST https://llm.univ-pau.fr/admin/unload \
  -H "Authorization: Bearer $ADMIN_SECRET"
```

---

## Performance et efficacité énergétique

### Consommation GPU

| État | Puissance | Description |
|------|-----------|-------------|
| GPU libre | **~20-30W** | Modèle déchargé, processus arrêté |
| Modèle chargé, inactif | ~100-150W | Modèle en VRAM, sans requête |
| Inférence active (70B) | ~300-350W | Traitement de requêtes |

> **Économie réalisée :** avec un timeout de 5 minutes, un GPU inutilisé la nuit consomme **~85% d'énergie en moins**.

### Paramètres d'inférence (L40S 48GB, 70B Q4_K_M)

| Paramètre | Valeur | Justification |
|-----------|--------|---------------|
| `-ngl` | `999` | Offload complet sur GPU |
| `-c` | `32768` | Contexte total : 4 slots × 8K tokens |
| `--parallel` | `4` | Concurrence adaptée à un usage universitaire |
| `-b / -ub` | `4096 / 512` | Prefill optimisé pour L40S |
| `-ctk/-ctv` | `q8_0` | KV cache : -50% VRAM, qualité identique |
| `-fa` | `on` | Flash Attention 2 (Ada Lovelace) |
| **VRAM utilisée** | **~40.5 GB** | 38GB poids + 2.5GB KV sur 48GB |

---

## Modèles recommandés

### Modèles denses (un seul en VRAM)

| Modèle | Quantisation | VRAM | Profil |
|--------|-------------|------|--------|
| **Llama-3.3-70B-Instruct** | Q4_K_M | ~42 GB | Optimal qualité/taille sur L40S |
| Qwen2.5-72B-Instruct | Q4_K_M | ~38-40 GB | Alternative performante |
| Qwen2.5-34B-Instruct | Q8_0 | ~36 GB | Qualité maximale en 34B |
| Llama-3.1-8B-Instruct | Q8_0 | ~9 GB | Rapide et léger |

### Modèles coexistants (plusieurs simultanément sur L40S 48GB)

Les modèles ci-dessous peuvent tenir ensemble dans le budget de 43.6 GB.

| Modèle A | VRAM (A) | Modèle B | VRAM (B) | Total | Faisable ? |
|----------|----------|----------|----------|-------|-----------|
| Gemma 4 27B MoE (a4b) | 26.9 GB | Qwen 27B MoE Q5_K_M (`cpu_moe`) | 7 GB | 33.9 GB | ✅ |
| Llama 8B Q4_K_M | 5.5 GB | Qwen 9B Q5_K_M | 7 GB | 12.5 GB | ✅ |
| Llama 70B Q4_K_M | 42 GB | n'importe quel autre | > 1.6 GB | > 43.6 GB | ❌ |

> **MoE et `cpu_moe`** : les modèles MoE (Mixture of Experts) doivent avoir
> `cpu_moe: true` dans leurs `llama_params` pour que les experts FFN soient
> déportés sur RAM CPU. Sans ce flag, toute la VRAM est consommée par les
> experts → CUDA OOM si un autre modèle est présent.

---

## Structure du projet

```
gateway/
├── main.py                 # Application FastAPI, routes, lifespan
├── config.py               # Configuration via .env / variables d'environnement
├── database.py             # SQLite WAL — utilisateurs, clés, journal d'usage
├── auth.py                 # Authentification Bearer token
├── rate_limiter.py         # Rate limiter sliding window in-memory
├── server_manager.py       # Cycle de vie llama-server (subprocess asyncio)
├── proxy.py                # Proxy OpenAI-compatible + streaming SSE
├── admin.py                # Routes d'administration REST
├── schemas.py              # Modèles Pydantic pour validation
├── cli.py                  # CLI d'administration (Typer + Rich)
├── requirements.txt        # Dépendances Python
├── .env.example            # Template de configuration
└── deploy/
    ├── install.sh              # Script d'installation idempotent
    ├── llm-gateway.service     # Unité systemd avec hardening
    └── nginx.conf              # Configuration nginx (TLS, SSE, IP filtering)
```

**Documentation complète :**
- [`docs/architecture.md`](docs/architecture.md) — Décisions techniques et flux de données
- [`docs/api.md`](docs/api.md) — Guide utilisateur complet avec exemples
- [`docs/admin.md`](docs/admin.md) — Référence CLI et API admin
- [`docs/deployment.md`](docs/deployment.md) — Guide de déploiement pas à pas

---

## Stack technique

| Couche | Technologie |
|--------|-------------|
| Framework web | [FastAPI](https://fastapi.tiangolo.com/) >= 0.115.0 |
| Serveur ASGI | [uvicorn](https://www.uvicorn.org/) + uvloop + httptools |
| Client HTTP | [httpx](https://www.python-httpx.org/) (async) |
| Base de données | [SQLite](https://www.sqlite.org/wal.html) WAL via [aiosqlite](https://aiosqlite.omnilib.dev/) |
| Configuration | [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) |
| CLI | [Typer](https://typer.tiangolo.com/) + [Rich](https://rich.readthedocs.io/) |
| Reverse proxy | [nginx](https://nginx.org/) |
| Init system | [systemd](https://systemd.io/) |
| Moteur d'inférence | [llama.cpp](https://github.com/ggml-org/llama.cpp) (`llama-server` CUDA) |
| GPU | NVIDIA L40S 48GB (Ada Lovelace, compute 8.9) |

---

## Conçu pour l'UPPA

EVARuntime a été développé pour l'**Université de Pau et des Pays de l'Adour** afin de fournir à ses doctorants, chercheurs et personnels un accès souverain et maîtrisé aux grands modèles de langage.

- **~100 utilisateurs** cible
- **Trafic intermittent** typique d'un environnement académique
- **GPU partagé** entre inférence et entraînement
- **Conformité** avec les exigences de souveraineté des données

---

*Développé pour l'Université de Pau et des Pays de l'Adour (UPPA)*
