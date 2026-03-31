# Guide de déploiement — Cluster EVA Inference Gateway

Ce document décrit l'installation complète du gateway sur le serveur hébergeant
le GPU NVIDIA L40S 48GB. Il s'adresse à l'administrateur système responsable
de la mise en production.

---

## Table des matières

1. [Prérequis](#1-prérequis)
2. [Installation de llama.cpp](#2-installation-de-llamacpp)
3. [Téléchargement du modèle](#3-téléchargement-du-modèle)
4. [Installation du gateway](#4-installation-du-gateway)
5. [Configuration](#5-configuration)
6. [Certificat TLS](#6-certificat-tls)
7. [Configuration nginx](#7-configuration-nginx)
8. [Démarrage et vérification](#8-démarrage-et-vérification)
9. [Mise à jour](#9-mise-à-jour)
10. [Dépannage](#10-dépannage)

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
| Espace disque | 80 GB+ | Modèle 70B Q4_K_M ≈ 42 GB |

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

## 3. Téléchargement du modèle

### Option A — Llama 3.3 70B (recommandé)

Qualité maximale, tient dans 48GB avec les paramètres configurés.

```bash
# Installer huggingface-cli
pip3 install huggingface-hub

# Télécharger uniquement le fichier Q4_K_M (~42 GB)
huggingface-cli download bartowski/Llama-3.3-70B-Instruct-GGUF \
  --include "*Q4_K_M*" \
  --local-dir /models/

# Vérifier le fichier
ls -lh /models/*.gguf
# → Llama-3.3-70B-Instruct-Q4_K_M.gguf  ~42G
```

### Option B — Qwen 2.5 34B Q8_0 (plus de marge VRAM)

Idéal si vous avez besoin de plus de slots concurrents ou de contextes plus longs.

```bash
huggingface-cli download bartowski/Qwen2.5-34B-Instruct-GGUF \
  --include "*Q8_0*" \
  --local-dir /models/

# Adapter ensuite dans la config :
# MODEL_PATH=/models/Qwen2.5-34B-Instruct-Q8_0.gguf
# MODEL_PUBLIC_NAME=qwen2.5-34b-instruct
# LLAMA_CTX_SIZE=65536
# LLAMA_PARALLEL=8
```

### Option C — Modèle local existant

Copier simplement le fichier `.gguf` dans `/models/` et adapter `MODEL_PATH`.

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
3. Création des répertoires (`/opt/llm-gateway`, `/var/lib/llm-gateway`, `/var/log/llm-gateway`)
4. Copie du code source et création du virtualenv Python
5. Installation des dépendances Python
6. **Génération automatique des secrets** (`INTERNAL_API_KEY`, `ADMIN_SECRET`) dans `/etc/llm-gateway/env`
7. Enregistrement du service systemd et activation
8. Initialisation de la base de données SQLite

À la fin du script, les prochaines étapes sont affichées avec les valeurs générées.

> **Important :** Noter l'`ADMIN_SECRET` affiché à la fin du script.
> Il ne sera plus visible ensuite (stocké dans `/etc/llm-gateway/env`).

---

## 5. Configuration

Le fichier de configuration se trouve dans `/etc/llm-gateway/env`.
C'est là que vivent **tous les secrets et paramètres** — jamais dans le code source.

```bash
sudo nano /etc/llm-gateway/env
```

### Paramètres critiques à vérifier

```bash
# ── Chemin du modèle ──────────────────────────────────────────────────────────
# Adapter au fichier téléchargé à l'étape 3
MODEL_PATH=/models/Llama-3.3-70B-Instruct-Q4_K_M.gguf
MODEL_PUBLIC_NAME=llama-3.3-70b-instruct

# ── Paramètres GPU (déjà optimisés pour L40S 48GB + 70B Q4_K_M) ──────────────
LLAMA_N_GPU_LAYERS=999          # offload toutes les couches sur le GPU
LLAMA_CTX_SIZE=32768            # 4 slots × 8192 tokens ≈ 2.5GB KV cache
LLAMA_PARALLEL=4                # 4 utilisateurs simultanés
LLAMA_FLASH_ATTN=true           # Flash Attention 2 (supporté sur L40S)
LLAMA_CACHE_TYPE_K=q8_0         # KV cache quantisé : -50% VRAM, qualité identique
LLAMA_CACHE_TYPE_V=q8_0
CUDA_VISIBLE_DEVICES=0          # index du GPU (0 = premier)

# ── Comportement idle ─────────────────────────────────────────────────────────
IDLE_TIMEOUT_SECONDS=300        # décharger après 5 min sans requête
# ↑ Augmenter si les utilisateurs reviennent souvent (ex: 600 pour 10 min)
# ↓ Diminuer pour économiser l'électricité (ex: 120 pour 2 min)
```

### Budget VRAM — vérification

Pour `70B Q4_K_M` avec les paramètres par défaut :

```
Poids du modèle (Q4_K_M)  : ~38–40 GB
KV cache (4 slots × 8K, Q8) : ~2.5 GB
─────────────────────────────────────
Total estimé               : ~40.5 GB
Disponible sur L40S        : 48 GB
Marge                      : ~7.5 GB  ✓
```

### Adapter pour un modèle 34B (plus de marge)

```bash
MODEL_PATH=/models/Qwen2.5-34B-Instruct-Q8_0.gguf
MODEL_PUBLIC_NAME=qwen2.5-34b-instruct
LLAMA_CTX_SIZE=65536            # 8 slots × 8192 tokens
LLAMA_PARALLEL=8                # 8 utilisateurs simultanés
# Budget VRAM : ~36GB poids + ~5GB KV ≈ 41GB  ✓
```

---

## 6. Certificat TLS

L'accès HTTPS est **obligatoire** — les clés API transitent dans les headers.

### Option A — Let's Encrypt (domaine public)

```bash
sudo apt install certbot python3-certbot-nginx

# Remplacer par le domaine réel
sudo certbot certonly --nginx -d llm.univ-pau.fr

# Certificat généré dans :
# /etc/letsencrypt/live/llm.univ-pau.fr/fullchain.pem
# /etc/letsencrypt/live/llm.univ-pau.fr/privkey.pem

# Renouvellement automatique (cron déjà configuré par certbot)
sudo certbot renew --dry-run
```

### Option B — PKI interne UPPA

```bash
# Placer les fichiers fournis par la DSI :
sudo cp uppa-llm.crt /etc/ssl/certs/llm-gateway.crt
sudo cp uppa-llm.key /etc/ssl/private/llm-gateway.key
sudo chmod 600 /etc/ssl/private/llm-gateway.key

# Adapter nginx.conf :
# ssl_certificate     /etc/ssl/certs/llm-gateway.crt;
# ssl_certificate_key /etc/ssl/private/llm-gateway.key;
```

---

## 7. Configuration nginx

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

## 8. Démarrage et vérification

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

### Vérifier le health check

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok","model_state":"unloaded"}
# ↑ "unloaded" est normal — le modèle charge à la première requête
```

### Première requête (déclenche le chargement du modèle)

```bash
# Créer d'abord un utilisateur et une clé
cd /opt/llm-gateway
sudo -u llmservice ./venv/bin/python cli.py add-user test --email test@univ-pau.fr
sudo -u llmservice ./venv/bin/python cli.py create-key test --name "test"
# → Copier la clé affichée : llmgw-XXXX...

# Tester (le modèle va charger, attendre ~60-90s)
curl -s https://llm.univ-pau.fr/v1/chat/completions \
  -H "Authorization: Bearer llmgw-VOTRE_CLE" \
  -H "Content-Type: application/json" \
  -d '{"model":"llama-3.3-70b-instruct","messages":[{"role":"user","content":"Dis bonjour"}]}' \
  | python3 -m json.tool
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

# Logs de llama-server (séparés)
tail -f /var/log/llm-gateway/llama-server.log

# Filtrer les erreurs uniquement
sudo journalctl -u llm-gateway -p err --since "1 hour ago"
```

---

## 9. Mise à jour

### Mettre à jour le code de la gateway

```bash
# Sur la machine de développement
git pull
sudo bash gateway/deploy/install.sh   # idempotent — ne touche pas à la config

sudo systemctl restart llm-gateway
sudo journalctl -u llm-gateway -f --since now
```

### Mettre à jour llama.cpp

```bash
cd /opt/llama.cpp
git pull

cmake --build build --config Release -j$(nproc)
sudo cp build/bin/llama-server /usr/local/bin/

# Redémarrer la gateway pour prendre en compte le nouveau binaire
sudo systemctl restart llm-gateway
```

### Changer de modèle

```bash
# 1. Télécharger le nouveau modèle
huggingface-cli download bartowski/Qwen2.5-72B-Instruct-GGUF \
  --include "*Q4_K_M*" --local-dir /models/

# 2. Mettre à jour la config
sudo nano /etc/llm-gateway/env
# → modifier MODEL_PATH et MODEL_PUBLIC_NAME

# 3. Redémarrer
sudo systemctl restart llm-gateway
```

---

## 10. Dépannage

### Le service ne démarre pas

```bash
sudo journalctl -u llm-gateway -n 50 --no-pager
```

Causes fréquentes :

| Symptôme dans les logs | Cause | Solution |
|------------------------|-------|----------|
| `ModuleNotFoundError` | venv corrompu | `sudo bash deploy/install.sh` |
| `Permission denied` sur `/models/` | Droits incorrects | `sudo chown -R root:llmservice /models && chmod -R 750 /models` |
| `Address already in use` | Port 8000 occupé | `sudo ss -tlnp \| grep 8000` |
| `ValidationError` | Config invalide dans `.env` | Vérifier `/etc/llm-gateway/env` |

### llama-server ne démarre pas (timeout de chargement)

```bash
tail -100 /var/log/llm-gateway/llama-server.log
```

Causes fréquentes :

| Symptôme | Cause | Solution |
|----------|-------|----------|
| `CUDA error: out of memory` | Modèle trop grand | Réduire `LLAMA_CTX_SIZE` ou `LLAMA_PARALLEL` |
| `failed to load model` | Chemin incorrect | Vérifier `MODEL_PATH` dans la config |
| `llama-server: command not found` | llama.cpp non installé | Refaire l'étape 2 |
| Timeout après 180s | Modèle trop lent à charger | Augmenter `MODEL_LOAD_TIMEOUT_SECONDS=300` |

### Vérifier que la VRAM est bien libérée

```bash
# Après l'idle timeout, la mémoire GPU doit être quasi-nulle
nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits
# Résultat attendu : < 500 MiB

# Si la mémoire n'est pas libérée : vérifier les processus
sudo fuser /dev/nvidia0
# Tuer les processus orphelins si nécessaire
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
