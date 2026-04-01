# Guide de déploiement — Cluster EVA Inference Gateway

Ce document décrit l'installation complète du gateway sur le serveur hébergeant
le GPU NVIDIA L40S 48GB. Il s'adresse à l'administrateur système responsable
de la mise en production.

---

## Table des matières

1. [Prérequis](#1-prérequis)
2. [Installation de llama.cpp](#2-installation-de-llamacpp)
3. [Téléchargement des modèles](#3-téléchargement-des-modèles)
4. [Installation du gateway](#4-installation-du-gateway)
5. [Configuration](#5-configuration)
6. [Registre des modèles (models.yaml)](#6-registre-des-modèles-modelsyaml)
7. [Certificat TLS](#7-certificat-tls)
8. [Configuration nginx](#8-configuration-nginx)
9. [Démarrage et vérification](#9-démarrage-et-vérification)
10. [Dashboard de monitoring](#10-dashboard-de-monitoring)
11. [Mise à jour](#11-mise-à-jour)
12. [Dépannage](#12-dépannage)

---

## 1. Prérequis

### Système

| Composant | Version minimale | Notes |
|-----------|-----------------|-------|
| Ubuntu | 22.04 LTS | 24.04 aussi supporté |
| Python | 3.11+ | `python3 --version` |
| CUDA toolkit | 12.x | Pour llama.cpp GPU |
| Pilotes NVIDIA | 535+ | `nvidia-smi` doit fonctionner |
| nginx | 1.18+ | `apt install nginx` |
| Espace disque | 100 GB+ | Modèle 70B Q4_K_M ≈ 42 GB, 8B Q4_K_M ≈ 5 GB |

### Vérifications initiales

```bash
# Vérifier que le GPU est reconnu
nvidia-smi

# Résultat attendu :
# +-----------------------------------------------------------------------------------------+
# | NVIDIA-SMI 535.x  Driver Version: 535.x  CUDA Version: 12.x                           |
# +-----------------------+----------------------+----------------------+
# | GPU  Name                 Persistence-M | Bus-Id        Disp.A |
# |   0  NVIDIA L40S                    Off |  00000000:...    Off |
# +-----------------------+----------------------+----------------------+
# | N/A   31C    P8    28W / 350W |    1MiB / 46068MiB |      0%      Default |

# Vérifier Python
python3 --version   # doit afficher 3.11 ou supérieur

# Si Python 3.11 absent :
sudo apt install python3.11 python3.11-venv python3.11-dev
```

---

## 2. Installation de llama.cpp

On compile llama.cpp depuis les sources pour avoir la version la plus récente
avec support CUDA optimisé pour l'Ada Lovelace (L40S, compute capability 8.9).

```bash
# Dépendances de compilation
sudo apt install -y build-essential cmake git libcurl4-openssl-dev

# Cloner le dépôt
git clone https://github.com/ggerganov/llama.cpp /opt/llama.cpp
cd /opt/llama.cpp

# Compiler avec support CUDA
# GGML_CUDA=ON : active le backend CUDA
# CMAKE_CUDA_ARCHITECTURES=89 : Ada Lovelace (L40S compute cap 8.9)
cmake -B build \
  -DGGML_CUDA=ON \
  -DCMAKE_CUDA_ARCHITECTURES="89" \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLAMA_CURL=ON

cmake --build build --config Release -j$(nproc)

# Installer le binaire
sudo cp build/bin/llama-server /usr/local/bin/
sudo chmod +x /usr/local/bin/llama-server

# Vérifier l'installation
llama-server --version
```

> **Note :** La compilation prend 5 à 15 minutes selon la machine.
> Si `nvidia-smi` indique une version CUDA différente, adapter `CMAKE_CUDA_ARCHITECTURES` :
> - A100 → `80`, V100 → `70`, RTX 4090 → `89`, L40S → `89`

---

## 3. Téléchargement des modèles

Le gateway supporte plusieurs modèles simultanément. Chaque modèle est un fichier
`.gguf` indépendant. Télécharger ceux que vous souhaitez proposer, puis les déclarer
dans le [registre des modèles](#6-registre-des-modèles-modelsyaml).

```bash
# Installer huggingface-cli
pip3 install huggingface-hub

# Créer le répertoire des modèles
sudo mkdir -p /models
```

### Modèle principal — Llama 3.3 70B Q4_K_M (~42 GB)

Qualité maximale, utilise la quasi-totalité du budget VRAM du L40S.

```bash
huggingface-cli download bartowski/Llama-3.3-70B-Instruct-GGUF \
  --include "*Q4_K_M*" \
  --local-dir /models/

ls -lh /models/*.gguf
# → Llama-3.3-70B-Instruct-Q4_K_M.gguf  ~42G
```

### Modèle léger — Llama 3.1 8B Q4_K_M (~5 GB)

Idéal en complément du 70B : faible consommation VRAM, démarrage rapide.
Peut tourner en parallèle du 70B si le budget VRAM le permet.

```bash
huggingface-cli download bartowski/Meta-Llama-3.1-8B-Instruct-GGUF \
  --include "*Q4_K_M*" \
  --local-dir /models/

# → Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf  ~5G
```

### Budget VRAM — planification

| Modèle | Fichier | Poids VRAM | KV cache | Total estimé |
|--------|---------|------------|----------|--------------|
| Llama 3.3 70B Q4_K_M | `Llama-3.3-70B-Instruct-Q4_K_M.gguf` | ~38–40 GB | ~2.5 GB (4 slots × 8K, Q8) | ~42 GB |
| Llama 3.1 8B Q4_K_M | `Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf` | ~4.5 GB | ~1 GB (8 slots × 8K, Q8) | ~5.5 GB |

**L40S 48 GB :**
- Budget net disponible = 48 − 2 (overhead) − 2.4 (marge 5%) = **43.6 GB**
- 70B seul : 42 GB ≤ 43.6 GB ✓
- 70B + 8B simultanément : 42 + 5.5 = 47.5 GB > 43.6 GB → éviction LRU automatique

---

## 4. Installation du gateway

```bash
# Cloner le projet
git clone <url-depot-interne> /tmp/llm-gateway-src

# Lancer le script d'installation (en root)
sudo bash /tmp/llm-gateway-src/gateway/deploy/install.sh
```

Le script effectue automatiquement :

1. Création de l'utilisateur système `llmservice` (sans shell, sans home)
2. Ajout au groupe `render,video` pour l'accès GPU
3. Création des répertoires (`/opt/llm-gateway`, `/var/lib/llm-gateway`, `/var/log/llm-gateway`, `/etc/llm-gateway`)
4. Copie du code source et création du virtualenv Python
5. Installation des dépendances Python
6. **Génération automatique des secrets** (`INTERNAL_API_KEY`, `ADMIN_SECRET`) dans `/etc/llm-gateway/env`
7. Copie du fichier `models.yaml` initial dans `/etc/llm-gateway/models.yaml`
8. Enregistrement du service systemd et activation
9. Initialisation de la base de données SQLite

À la fin du script, les prochaines étapes sont affichées avec les valeurs générées.

> **Important :** Noter l'`ADMIN_SECRET` affiché à la fin du script.
> Il ne sera plus visible ensuite (stocké dans `/etc/llm-gateway/env`).

---

## 5. Configuration

Le fichier de configuration se trouve dans `/etc/llm-gateway/env`.
C'est là que vivent **tous les secrets et paramètres globaux** — jamais dans le code source.
Les paramètres spécifiques à chaque modèle (taille de contexte, nombre de slots, etc.)
se trouvent dans `models.yaml` (voir section 6).

```bash
sudo nano /etc/llm-gateway/env
```

### Paramètres critiques à vérifier

```bash
# ── Registre des modèles ──────────────────────────────────────────────────────
# Chemin vers le fichier YAML listant tous les modèles disponibles
MODELS_CONFIG_PATH=/etc/llm-gateway/models.yaml

# ── Budget VRAM (L40S 48 GB) ──────────────────────────────────────────────────
# Ajuster si vous utilisez un GPU différent
TOTAL_VRAM_GB=48.0
VRAM_OVERHEAD_GB=2.0        # réservé pour le contexte CUDA et le framework
VRAM_SAFETY_MARGIN=0.05     # 5% de marge de sécurité supplémentaire

# ── Pool de ports multi-modèles ───────────────────────────────────────────────
# Chaque llama-server chargé occupe un port de ce pool
BASE_LLAMA_PORT=8081
MAX_LOADED_MODELS=5         # taille max du pool (ports 8081–8085)

# ── Modèle par défaut ─────────────────────────────────────────────────────────
# ID du modèle utilisé quand le client ne précise pas de champ "model"
# Laisser vide pour utiliser automatiquement le premier modèle activé
DEFAULT_MODEL_ID=llama-3.3-70b-instruct

# ── Répertoires autorisés pour les fichiers .gguf ─────────────────────────────
# Liste séparée par des virgules. Vide = pas de restriction.
ALLOWED_MODEL_DIRS=/models

# ── GPU ────────────────────────────────────────────────────────────────────────
CUDA_VISIBLE_DEVICES=0          # index du GPU (0 = premier)

# ── Comportement idle (commun à tous les modèles) ────────────────────────────
IDLE_TIMEOUT_SECONDS=300        # décharger après 5 min sans requête
# ↑ Augmenter si les utilisateurs reviennent souvent (ex: 600 pour 10 min)
# ↓ Diminuer pour économiser l'électricité (ex: 120 pour 2 min)

# ── Secrets (générés par install.sh — ne pas modifier manuellement) ───────────
INTERNAL_API_KEY=<généré>
ADMIN_SECRET=<généré>
```

---

## 6. Registre des modèles (models.yaml)

Le fichier `models.yaml` est la **source de vérité** pour tous les modèles disponibles.
Il est installé dans `/etc/llm-gateway/models.yaml`.

```bash
sudo nano /etc/llm-gateway/models.yaml
```

### Structure

```yaml
models:
  - id: "llama-3.3-70b-instruct"          # identifiant unique (regex: ^[a-z0-9][a-z0-9._-]*$)
    path: "/models/Llama-3.3-70B-Instruct-Q4_K_M.gguf"   # chemin absolu vers le .gguf
    description: "Llama 3.3 70B Instruct, Q4_K_M — modèle principal UPPA"
    vram_gb: 42.0                          # estimation VRAM totale (poids + KV cache)
    enabled: true                          # false = invisible aux clients
    capabilities:
      - text_generation
      - tool_calls
      - streaming
    llama_params:
      n_gpu_layers: 999                    # offload toutes les couches sur GPU
      ctx_size: 32768                      # taille de contexte (tokens)
      parallel: 4                          # slots parallèles (utilisateurs simultanés)
      batch_size: 4096
      ubatch_size: 512
      cache_type_k: "q8_0"               # KV cache quantisé : -50% VRAM, qualité identique
      cache_type_v: "q8_0"
      flash_attn: true                     # Flash Attention 2 (supporté sur L40S)
      threads: 8
      threads_http: 4

  - id: "llama-3.1-8b-instruct"
    path: "/models/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
    description: "Llama 3.1 8B Instruct, Q4_K_M — modèle léger"
    vram_gb: 5.5
    enabled: false                         # activer quand le fichier .gguf est disponible
    capabilities:
      - text_generation
      - streaming
    llama_params:
      n_gpu_layers: 999
      ctx_size: 32768
      parallel: 8
      batch_size: 2048
      ubatch_size: 512
      cache_type_k: "q8_0"
      cache_type_v: "q8_0"
      flash_attn: true
      threads: 4
      threads_http: 2
```

### Activer un modèle

```bash
# 1. Vérifier que le fichier .gguf est présent
ls -lh /models/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf

# 2. Éditer le registre
sudo nano /etc/llm-gateway/models.yaml
# → passer enabled: false à enabled: true pour le modèle voulu

# 3. Redémarrer le gateway pour recharger le registre
sudo systemctl restart llm-gateway
```

### Ajouter un nouveau modèle

```bash
# 1. Télécharger le fichier .gguf
huggingface-cli download bartowski/Qwen2.5-32B-Instruct-GGUF \
  --include "*Q4_K_M*" --local-dir /models/

# 2. Ajouter une entrée dans models.yaml (respecter la structure ci-dessus)
sudo nano /etc/llm-gateway/models.yaml

# 3. Redémarrer
sudo systemctl restart llm-gateway

# 4. Vérifier que le modèle est bien détecté
cd /opt/llm-gateway
sudo -u llmservice ./venv/bin/python cli.py status
```

> **Alternative API REST :** Les modèles peuvent aussi être enregistrés à chaud
> via `POST /admin/models` sans redémarrer le service (voir le guide administrateur).

### Sécurité du registre

Le registre est validé au démarrage :
- Les `id` doivent correspondre à `^[a-z0-9][a-z0-9._-]*$` (pas de `/`, `..`, etc.)
- Les `path` doivent être absolus et se terminer par `.gguf`
- Si `ALLOWED_MODEL_DIRS` est configuré, les chemins doivent être sous ces répertoires

---

## 7. Certificat TLS

L'accès HTTPS est **obligatoire** — les clés API transitent dans les headers.

Le domaine utilisé est **`llm.eva.univ-pau.fr`**. Le certificat est fourni par la DSI UPPA (PKI interne) — pas de certbot.

```bash
# Placer les fichiers fournis par la DSI UPPA :
sudo cp uppa-llm.crt /etc/ssl/certs/llm-gateway.crt
sudo cp uppa-llm.key /etc/ssl/private/llm-gateway.key
sudo chmod 600 /etc/ssl/private/llm-gateway.key
sudo chmod 644 /etc/ssl/certs/llm-gateway.crt
```

> **Note :** Le certificat est géré par la DSI UPPA. Contacter le service informatique pour le renouvellement avant expiration.

---

## 8. Configuration nginx

```bash
# Adapter le nom de domaine dans la config
sudo nano /etc/nginx/sites-available/llm-gateway

# Remplacer llm.univ-pau.fr par votre domaine réel
# Vérifier les plages IP campus dans la section /admin/ :
#   allow 10.0.0.0/8;      ← adapter si besoin
#   allow 192.168.0.0/16;

# Tester la configuration
sudo nginx -t

# Activer et recharger
sudo ln -sf /etc/nginx/sites-available/llm-gateway \
            /etc/nginx/sites-enabled/llm-gateway
sudo nginx -s reload
```

---

## 9. Démarrage et vérification

### Démarrer le service

```bash
sudo systemctl start llm-gateway
sudo systemctl status llm-gateway
```

Résultat attendu :

```
● llm-gateway.service - LLM Inference Gateway UPPA (FastAPI)
     Loaded: loaded (/etc/systemd/system/llm-gateway.service; enabled)
     Active: active (running) since ...
    Process: ExecStart=/opt/llm-gateway/venv/bin/uvicorn main:app ...
   Main PID: 12345 (uvicorn)
```

### Vérifier le registre des modèles (CLI)

```bash
cd /opt/llm-gateway
sudo -u llmservice ./venv/bin/python cli.py status
```

Sortie attendue :

```
Configuration VRAM
  Total GPU       : 48.0 GB
  Overhead        : 2.0 GB
  Marge sécurité  : 5%
  Budget net      : 43.6 GB
  Max modèles     : 5
  Pool de ports   : 8081–8085
  Idle timeout    : 300s

┌──────────────────────────┬──────────┬────────┬──────────────────────────────────┬──────────────────────────────────────────────────────────┐
│ ID                       │ VRAM     │ Activé │ Capacités                        │ Chemin                                                   │
├──────────────────────────┼──────────┼────────┼──────────────────────────────────┼──────────────────────────────────────────────────────────┤
│ llama-3.3-70b-instruct   │ 42.0 GB  │ oui    │ text_generation, tool_calls, ... │ /models/Llama-3.3-70B-Instruct-Q4_K_M.gguf               │
│ llama-3.1-8b-instruct    │  5.5 GB  │ non    │ text_generation, streaming       │ /models/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf           │
└──────────────────────────┴──────────┴────────┴──────────────────────────────────┴──────────────────────────────────────────────────────────┘
```

### Vérifier le health check

```bash
curl http://127.0.0.1:8000/health
```

Réponse attendue (aucun modèle chargé au démarrage) :

```json
{
  "status": "ok",
  "models_loaded": [],
  "vram_used_gb": 0.0,
  "vram_available_gb": 43.6
}
```

### Première requête (déclenche le chargement du modèle)

```bash
# Créer d'abord un utilisateur et une clé
cd /opt/llm-gateway
sudo -u llmservice ./venv/bin/python cli.py add-user test --email test@univ-pau.fr
sudo -u llmservice ./venv/bin/python cli.py create-key test --name "test"
# → Copier la clé affichée : llmgw-XXXX...

# Tester (le modèle 70B va charger, attendre ~60-90s à la première requête)
curl -s https://llm.eva.univ-pau.fr/v1/chat/completions \
  -H "Authorization: Bearer llmgw-VOTRE_CLE" \
  -H "Content-Type: application/json" \
  -d '{"model":"llama-3.3-70b-instruct","messages":[{"role":"user","content":"Dis bonjour"}]}' \
  | python3 -m json.tool
```

### Vérifier le statut multi-modèles (API)

```bash
export ADMIN_SECRET=$(sudo grep ADMIN_SECRET /etc/llm-gateway/env | cut -d= -f2)
curl -s http://127.0.0.1:8000/admin/status \
  -H "Authorization: Bearer $ADMIN_SECRET" | python3 -m json.tool
```

Réponse après chargement du 70B :

```json
{
  "status": "ok",
  "vram_budget": {
    "total_gb": 48.0,
    "overhead_gb": 2.0,
    "used_gb": 42.0,
    "available_gb": 1.6
  },
  "models": [
    {
      "id": "llama-3.3-70b-instruct",
      "state": "ready",
      "vram_gb": 42.0,
      "pid": 18432,
      "uptime_seconds": 125.3,
      "idle_seconds": 12.1,
      "path": "/models/Llama-3.3-70B-Instruct-Q4_K_M.gguf"
    }
  ]
}
```

### Vérifier la libération GPU après idle

```bash
# Surveiller la VRAM en temps réel
watch -n 5 'nvidia-smi --query-gpu=name,memory.used,memory.free,power.draw \
  --format=csv,noheader'

# Après IDLE_TIMEOUT_SECONDS sans requête, observer :
# L40S, 500 MiB, 47000 MiB, 28.00 W   ← GPU libéré ✓
```

### Consulter les logs

```bash
# Logs de la gateway (temps réel)
sudo journalctl -u llm-gateway -f

# Logs de llama-server (chaque modèle est préfixé dans les logs)
tail -f /var/log/llm-gateway/llama-server.log

# Filtrer les erreurs uniquement
sudo journalctl -u llm-gateway -p err --since "1 hour ago"
```

---

## 10. Dashboard de monitoring

Le gateway embarque un dashboard d'administration accessible depuis le navigateur.
Il affiche en temps réel les KPIs d'usage, les graphiques de consommation de tokens,
la distribution des erreurs, les statistiques par utilisateur et les métriques GPU
de chaque llama-server chargé.

### Accès

Le dashboard est servi à l'URL :

```
https://llm.eva.univ-pau.fr/admin/dashboard
```

> **Prérequis réseau :** la route `/admin/` est restreinte au réseau campus par nginx
> (plages `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`). Le dashboard n'est donc
> pas accessible depuis Internet.

### Première connexion

1. Ouvrir `https://llm.eva.univ-pau.fr/admin/dashboard` dans un navigateur
2. Un écran de connexion s'affiche — entrer l'`ADMIN_SECRET`
3. Le token est stocké dans `sessionStorage` (durée de vie : onglet du navigateur)
4. À la fermeture du navigateur ou de l'onglet, la session est automatiquement détruite

Pour retrouver l'`ADMIN_SECRET` sur le serveur :

```bash
sudo grep ADMIN_SECRET /etc/llm-gateway/env
```

### Ce que le dashboard affiche

| Section | Contenu |
|---------|---------|
| **KPI cards** | Requêtes aujourd'hui (Δ% vs hier), tokens, utilisateurs actifs (7j), latence moyenne, taux d'erreur |
| **Budget VRAM** | VRAM totale / utilisée / disponible, état de chaque modèle chargé |
| **Requêtes / heure** | Graphique ligne, dernières 24h/7j/30j, avec courbe d'erreurs |
| **Token usage** | Graphique barres empilées (prompt vs completion), dernières 24h/7j/30j |
| **Distribution HTTP** | Graphique donut des codes de statut (200, 429, 503…) |
| **Tableau utilisateurs** | Requêtes, tokens consommés, barre de quota, RPM, dernière activité |
| **Statut système** | État de chaque modèle chargé (READY/LOADING), VRAM par modèle, métriques llama-server en direct |

Le dashboard se rafraîchit automatiquement toutes les **30 secondes**.

### Endpoints metrics (pour l'automatisation)

```bash
# Vue d'ensemble KPI + état multi-modèles
curl -s "$GW/admin/metrics/overview" \
  -H "Authorization: Bearer $ADMIN_SECRET" | python3 -m json.tool

# Métriques llama-server en direct (indexées par model_id)
curl -s "$GW/admin/metrics/llama" \
  -H "Authorization: Bearer $ADMIN_SECRET" | python3 -m json.tool
# Exemple de réponse avec deux modèles chargés :
# {
#   "llama-3.3-70b-instruct": { "kv_cache_usage_ratio": 0.12, "tokens_per_second": 18.4, ... },
#   "llama-3.1-8b-instruct":  { "kv_cache_usage_ratio": 0.05, "tokens_per_second": 62.1, ... }
# }
```

---

## 11. Mise à jour

### Mettre à jour le code de la gateway

```bash
# Sur le serveur GPU, dans le répertoire du dépôt cloné
sudo bash gateway/deploy/update.sh
```

Ce que fait le script :

1. `git pull` dans le dépôt
2. Synchronise les fichiers Python et `requirements.txt` vers `/opt/llm-gateway/`
3. Synchronise le répertoire `static/` (dashboard HTML…)
4. Met à jour les dépendances pip si `requirements.txt` a changé
5. Copie le fichier systemd et recharge `daemon-reload`
6. Redémarre le service proprement et attend le health check

> **Note :** Le script ne touche jamais à `/etc/llm-gateway/env` (secrets),
> à `/etc/llm-gateway/models.yaml` (registre), à la base de données, ni aux modèles GGUF.

### Mettre à jour llama.cpp

```bash
cd /opt/llama.cpp
git pull

cmake --build build --config Release -j$(nproc)
sudo cp build/bin/llama-server /usr/local/bin/

# Redémarrer la gateway pour prendre en compte le nouveau binaire
sudo systemctl restart llm-gateway
```

### Ajouter ou modifier des modèles

Les modèles se gèrent via `models.yaml` — aucun redémarrage requis si vous utilisez l'API REST.

**Via models.yaml (redémarrage requis) :**

```bash
# 1. Télécharger le fichier .gguf
huggingface-cli download bartowski/Qwen2.5-32B-Instruct-GGUF \
  --include "*Q4_K_M*" --local-dir /models/

# 2. Ajouter l'entrée dans le registre
sudo nano /etc/llm-gateway/models.yaml

# 3. Redémarrer
sudo systemctl restart llm-gateway
```

**Via API REST (sans redémarrage) :**

```bash
export ADMIN_SECRET=$(sudo grep ADMIN_SECRET /etc/llm-gateway/env | cut -d= -f2)

# Enregistrer un nouveau modèle
curl -s -X POST "https://llm.eva.univ-pau.fr/admin/models" \
  -H "Authorization: Bearer $ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "qwen2.5-32b-instruct",
    "path": "/models/Qwen2.5-32B-Instruct-Q4_K_M.gguf",
    "description": "Qwen 2.5 32B Instruct Q4_K_M",
    "vram_gb": 20.0,
    "enabled": true,
    "capabilities": ["text_generation", "streaming"],
    "llama_params": {
      "n_gpu_layers": 999,
      "ctx_size": 32768,
      "parallel": 6,
      "batch_size": 2048,
      "ubatch_size": 512,
      "cache_type_k": "q8_0",
      "cache_type_v": "q8_0",
      "flash_attn": true,
      "threads": 6,
      "threads_http": 3
    }
  }'

# Activer / désactiver un modèle existant
curl -s -X PATCH "https://llm.eva.univ-pau.fr/admin/models/llama-3.1-8b-instruct" \
  -H "Authorization: Bearer $ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}'
```

---

## 12. Dépannage

### Le service ne démarre pas

```bash
sudo journalctl -u llm-gateway -n 50 --no-pager
```

Causes fréquentes :

| Symptôme dans les logs | Cause | Solution |
|------------------------|-------|----------|
| `ModuleNotFoundError` | venv corrompu | `sudo bash deploy/install.sh` |
| `FileNotFoundError: models.yaml` | Registre absent | Vérifier `MODELS_CONFIG_PATH` dans `/etc/llm-gateway/env` |
| `Permission denied` sur `/models/` | Droits incorrects | `sudo chown -R root:llmservice /models && chmod -R 750 /models` |
| `Address already in use` | Port 8000 occupé | `sudo ss -tlnp \| grep 8000` |
| `ValidationError` | Config invalide dans `.env` | Vérifier `/etc/llm-gateway/env` |
| `ValueError: model_id invalide` | ID dans models.yaml non conforme | L'ID doit correspondre à `^[a-z0-9][a-z0-9._-]*$` |

### llama-server ne démarre pas (timeout de chargement)

```bash
tail -100 /var/log/llm-gateway/llama-server.log
```

Les logs sont préfixés par le model_id (ex: `[llama-3.3-70b-instruct]`) pour
distinguer les instances quand plusieurs modèles sont chargés.

Causes fréquentes :

| Symptôme | Cause | Solution |
|----------|-------|----------|
| `CUDA error: out of memory` | Modèle trop grand pour le budget VRAM | Réduire `ctx_size` ou `parallel` dans `models.yaml` |
| `failed to load model` | Chemin incorrect | Vérifier `path` dans `models.yaml` |
| `llama-server: command not found` | llama.cpp non installé | Refaire l'étape 2 |
| Timeout après 180s | Modèle trop lent à charger | Augmenter `MODEL_LOAD_TIMEOUT_SECONDS=300` dans `env` |
| `Port already in use` | Deux modèles sur le même port | Vérifier `BASE_LLAMA_PORT` et `MAX_LOADED_MODELS` |

### Vérifier le budget VRAM

```bash
# Snapshot rapide
nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader,nounits

# Après idle timeout, la mémoire GPU doit être quasi-nulle
# Résultat attendu : < 500 MiB utilisés

# Statut détaillé via l'API (VRAM comptabilisée par le gateway)
curl -s http://127.0.0.1:8000/admin/status \
  -H "Authorization: Bearer $ADMIN_SECRET" | python3 -m json.tool

# Si la mémoire n'est pas libérée : vérifier les processus orphelins
sudo fuser /dev/nvidia0
```

### Streaming SSE bloqué (pas de réponse en temps réel)

Vérifier la configuration nginx :

```bash
# S'assurer que proxy_buffering est bien off
grep -n "proxy_buffering" /etc/nginx/sites-available/llm-gateway
# → proxy_buffering        off;

# Recharger nginx
sudo nginx -s reload
```

### Réinitialiser la base de données (⚠ efface tout)

```bash
sudo systemctl stop llm-gateway
sudo rm /var/lib/llm-gateway/gateway.db
sudo systemctl start llm-gateway
# La DB est recréée automatiquement au démarrage
```
