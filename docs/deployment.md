# Guide de déploiement — EVARuntime local et multi-nœuds

Ce document couvre deux parcours distincts : le produit **local mono-nœud**,
sûr et choisi par défaut, et l'**orchestrateur cluster**, explicitement opt-in.
Le premier lance `llama-server` sur l'hôte gateway; le second ne requiert aucun
GPU local et pilote des node-agents installés séparément.

## Déploiement local — premier token

Ce parcours s'applique à une machine Linux équipée d'au moins un GPU NVIDIA :
la gateway et `llama-server` tournent sur le même hôte. Il permet de vérifier
un premier token local avant d'ajouter un certificat TLS, un reverse proxy ou
un déploiement multi-nœuds. Le parcours de production épinglé de
[§4](#parcours-production-complet--mode-local) reste utile ensuite ; il ne
remplace pas cette première installation.

Les chemins ne changent pas d'une étape à l'autre :

| Élément | Chemin |
|---|---|
| Checkout EVARuntime | `/opt/src/EVARuntime` |
| Sources llama.cpp | `/opt/src/llama.cpp` |
| Binaire lu par la gateway | `/opt/llama.cpp/current/llama-server` |
| GGUF | `/models/` |
| Configuration | `/etc/llm-gateway/env` |
| Registre actif | `/var/lib/llm-gateway/models.yaml` |

### Étape 1 — vérifier le GPU et le compilateur CUDA

```bash
uname -m
nvidia-smi --query-gpu=index,name,memory.total,driver_version,compute_cap \
  --format=csv,noheader
command -v nvcc
nvcc --version
```

`nvidia-smi` doit lister le ou les GPU destinés à l'inférence, et `nvcc` doit
provenir du toolkit CUDA compatible avec leur pilote. La [section 2](#2-installation-de-llamacpp)
calcule ensuite l'architecture CUDA à transmettre à CMake : ne la fixez pas à
une valeur propre à un autre GPU.

### Étape 2 — préparer les dépendances et le checkout

   ```bash
   sudo apt update
   sudo apt install -y \
     build-essential cmake ninja-build git libcurl4-openssl-dev \
     python3 python3-venv python3-pip nginx sqlite3

   sudo install -d -m 0755 /opt/src
   sudo chown "$USER":"$(id -gn)" /opt/src

   # Uniquement si votre réseau impose un proxy sortant :
   # export http_proxy=http://proxy.example.net:3128
   # export https_proxy=http://proxy.example.net:3128
   # `sudo -E` à l'étape 4 transmet ces deux variables à pip.

   git clone https://github.com/Tutanka01/EVARuntime.git /opt/src/EVARuntime
   ```

### Étape 3 — compiler `llama.cpp` avant d’installer la gateway

`install.sh` installe la gateway, mais ne télécharge ni ne compile
`llama-server`. Ne passez pas à l’étape suivante tant que le dernier `test`
ci-dessous ne réussit pas.

```bash
export CUDACXX="$(command -v nvcc)"
test -x "$CUDACXX"

CUDA_ARCHITECTURES="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader \
  | awk '{gsub(/\./, ""); print}' | sort -u | paste -sd ';' -)"
test -n "$CUDA_ARCHITECTURES"

git clone https://github.com/ggml-org/llama.cpp.git /opt/src/llama.cpp
cd /opt/src/llama.cpp

cmake -S . -B build -G Ninja \
  -DGGML_CUDA=ON \
  -DCMAKE_CUDA_COMPILER="$CUDACXX" \
  -DCMAKE_CUDA_ARCHITECTURES="$CUDA_ARCHITECTURES" \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_SHARED_LIBS=OFF \
  -DLLAMA_BUILD_UI=OFF \
  -DLLAMA_USE_PREBUILT_UI=OFF
cmake --build build --target llama-server --parallel "$(nproc)"

RELEASE="manual-$(git rev-parse --short HEAD)"
sudo install -d -m 0755 "/opt/llama.cpp/releases/$RELEASE"
sudo install -m 0755 build/bin/llama-server \
  "/opt/llama.cpp/releases/$RELEASE/llama-server"
sudo ln -sfn "releases/$RELEASE" /opt/llama.cpp/.current-new
sudo mv -Tf /opt/llama.cpp/.current-new /opt/llama.cpp/current

test -x /opt/llama.cpp/current/llama-server
/opt/llama.cpp/current/llama-server --version
```

La [section 2](#2-installation-de-llamacpp) détaille les cas de build avancés,
mais cette étape suffit pour l’installation locale initiale.

### Étape 4 — installer le socle gateway

L'installateur crée le service et ses secrets, mais ne démarre pas encore la
gateway :

   ```bash
   export EVA_SRC=/opt/src/EVARuntime
   bash "$EVA_SRC/gateway/deploy/install.sh" --mode local --dry-run
   sudo -E bash "$EVA_SRC/gateway/deploy/install.sh" --mode local

   sudo systemctl is-enabled llm-gateway
   sudo test -x /opt/llama.cpp/current/llama-server
   sudo test -f /etc/llm-gateway/env
   sudo test -f /var/lib/llm-gateway/models.yaml
   ```

### Étape 5 — télécharger un petit modèle de recette

Déclarez uniquement ce modèle comme activé pour le premier test :

   ```bash
   sudo python3 -m venv /opt/src/hf-venv
   sudo -E /opt/src/hf-venv/bin/python -m pip install --upgrade pip huggingface-hub

   sudo install -d -m 0750 -o llmservice -g llmservice \
     /var/lib/llm-gateway/huggingface /models/qwen2.5-0.5b

   sudo -u llmservice env HF_HOME=/var/lib/llm-gateway/huggingface \
     http_proxy="${http_proxy:-}" https_proxy="${https_proxy:-}" \
     /opt/src/hf-venv/bin/hf download \
     Qwen/Qwen2.5-0.5B-Instruct-GGUF \
     --include '*q4_k_m*' \
     --local-dir /models/qwen2.5-0.5b

   sudo find /models/qwen2.5-0.5b -maxdepth 1 -type f -name '*.gguf' -print
   ```

   Ouvrez `/etc/llm-gateway/env` avec `sudoedit`. Renseignez
   `TOTAL_VRAM_GB` avec la valeur `memory.total` du GPU choisi ; si le pilote
   affiche `N/A`, utilisez la capacité annoncée par le constructeur. Vérifiez
   ensuite :

   ```ini
   LLAMA_SERVER_BIN=/opt/llama.cpp/current/llama-server
   TOTAL_VRAM_GB=<capacité VRAM de la machine>
   VRAM_OVERHEAD_GB=2.0
   VRAM_SAFETY_MARGIN=0.05
   ALLOWED_MODEL_DIRS=/models
   DEFAULT_MODEL_ID=qwen2.5-0.5b-instruct-q4_k_m
   CUDA_VISIBLE_DEVICES=0
   ```

   Remplacez le contenu de `/var/lib/llm-gateway/models.yaml` par le registre
   minimal suivant. Le nom dans `path` doit être exactement celui affiché par
   `find` :

   ```yaml
   models:
     - id: "qwen2.5-0.5b-instruct-q4_k_m"
       path: "/models/qwen2.5-0.5b/qwen2.5-0.5b-instruct-q4_k_m.gguf"
       description: "Modèle de recette"
       vram_gb: 1.0
       enabled: true
       capabilities: [text_generation, streaming]
       llama_params:
         n_gpu_layers: 999
         ctx_size: 4096
         parallel: 1
         batch_size: 512
         ubatch_size: 256
         cache_type_k: "q8_0"
         cache_type_v: "q8_0"
         flash_attn: true
         threads: 8
         threads_http: 2
   ```

### Étape 6 — démarrer et prouver le premier token

   ```bash
   sudo systemctl start llm-gateway
   curl -fsS http://127.0.0.1:8000/health
   curl -i http://127.0.0.1:8000/ready

   sudo bash /opt/llm-gateway/deploy/smoke_test.sh \
     --admin-url http://127.0.0.1:8000 \
     --base-url http://127.0.0.1:8000 \
     --model qwen2.5-0.5b-instruct-q4_k_m
   ```

`/ready` contrôle la structure ; seul `smoke_test.sh` charge le GGUF et prouve
le premier token SSE. Ajoutez TLS/nginx uniquement après ce succès local.

---

## Table des matières

0. [Déploiement local — premier token](#déploiement-local--premier-token)
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
13. [Déploiement multi-nœuds (optionnel)](#13-déploiement-multi-nœuds-optionnel--avancé)
14. [Sauvegardes SQLite et restauration](#14-sauvegardes-sqlite-et-restauration)
15. [Durcissement systemd et profils mémoire](#15-durcissement-systemd-et-profils-mémoire)
16. [Durcissement nginx](#16-durcissement-nginx-anti-slowloris-et-concurrence)
17. [Rotation des logs journald](#17-rotation-des-logs-journald)
18. [Référence de configuration (variables d'environnement)](#18-référence-de-configuration-variables-denvironnement)

---

## 1. Prérequis

### Système

| Composant | Version minimale | Notes |
|-----------|-----------------|-------|
| Ubuntu | 22.04 LTS | 24.04 aussi supporté |
| Python | 3.11+ | `python3 --version` |
| CUDA toolkit | 12.x | Mode local et chaque node-agent; inutile sur l'orchestrateur cluster |
| Pilotes NVIDIA | 535+ | `nvidia-smi` sur les hôtes d'inférence, pas sur l'orchestrateur |
| nginx | 1.18+ | `apt install nginx`. La conf livrée est valide de 1.18 à 1.29 ; **HTTP/2 est activé automatiquement** par `install.sh`/`update.sh` dans la forme qu'accepte la version installée — [§8](#8-configuration-nginx) |
| Espace disque | 100 GB+ sur nœud GPU | À dimensionner sur la somme des GGUF activés (le seul profil MiniMax pèse ~248 GB). L'orchestrateur ne stocke que code, DB et registre |
| RAM hôte | 64 GB+ sur nœud GPU (128 GB = hôte de référence) | **Dépend des modèles activés** : un modèle `cpu_moe: true` garde ses experts FFN en RAM hôte. Table de dimensionnement en [§15](#15-durcissement-systemd-et-profils-mémoire). 4 GB suffisent sur l'orchestrateur cluster |

### Commandes exigées par les scripts de déploiement

Les préflights refusent de continuer si l'une de ces commandes manque. La liste
est **exhaustive** et **tenue à jour mécaniquement** : chaque script déclare ses
dépendances dans un tableau bash unique (`INSTALL_REQUIRED_COMMANDS`,
`AGENT_REQUIRED_COMMANDS`, …) et `gateway/tests/test_deploy_required_commands.py`
dérive de ces tableaux la liste qui doit figurer ci-dessous. Une dépendance
ajoutée à un script sans être documentée ici fait échouer la CI.

| Commande | Exigée par | Paquet Debian/Ubuntu |
|---|---|---|
| `awk` | `install.sh`, `update.sh` | `mawk` ou `gawk` (présent) |
| `chmod`, `chown`, `cp`, `id`, `mkdir`, `mktemp`, `mv` | `install.sh`, `update.sh` | `coreutils` (présent) |
| `find` | `install.sh`, `update.sh` | `findutils` (présent) |
| `python3` | tous | `python3` (≥ 3.11) |
| `systemctl` | tous | `systemd` (présent) |
| `useradd` | `install.sh` | `passwd` (présent) |
| `usermod` | `install.sh` **mode local** | `passwd` (présent) |
| `nvidia-smi` | `install.sh` / `update.sh` **mode local** | pilote NVIDIA — contournable par `--allow-no-gpu`, voir [§4](#4-installation-du-gateway) |
| `curl` | `update.sh` | `curl` — **à installer** |
| `git` | `update.sh`, `update-agent.sh` (sans `--no-pull`) | `git` — **à installer** |
| `rsync` | `install-agent.sh`, `update-agent.sh` | `rsync` — **à installer**, absent d'une Debian 13 minimale |
| `openssl` | `install-agent.sh` | `openssl` — **à installer** si absent |

Commandes **optionnelles** : absentes, elles ne bloquent pas l'installation mais
désactivent une fonction, et le script le dit.

| Commande | Fonction perdue si absente | Paquet |
|---|---|---|
| `nginx` | reverse-proxy TLS non configuré ([§8](#8-configuration-nginx)) | `nginx` |
| `sqlite3` | timer de sauvegarde quotidienne **non armé** ([§11](#11-mise-à-jour)) | `sqlite3` |
| `ufw` | aucune règle firewall créée sur le nœud ; à faire dans le firewall réseau | `ufw` |

```bash
# Orchestrateur / gateway mono-nœud
sudo apt install -y curl git sqlite3 nginx

# CHAQUE nœud GPU, avant install-agent.sh
sudo apt install -y rsync openssl
```

### Vérifications initiales

```bash
# Sur le serveur local ou CHAQUE node-agent (pas sur l'orchestrateur cluster)
# vérifier que le GPU est reconnu
nvidia-smi

# Résultat attendu : au moins un GPU NVIDIA apparaît sans erreur.

# Vérifier Python
python3 --version   # doit afficher 3.11 ou supérieur

# Si Python 3.11 absent :
sudo apt install python3.11 python3.11-venv python3.11-dev
```

### Planifier avant d'installer

Trois couches, volontairement séparées. Deux écrivent, mais pas au même moment
ni avec la même autorité :

| Couche | Commande | Privilèges | Effet sur l'hôte |
|---|---|---|---|
| Planification | `python cli.py bootstrap-plan` | aucun | **aucun** — produit un document |
| Installation du socle | `bash gateway/deploy/install.sh` | `root` | utilisateur système, venv, systemd, nginx, secrets |
| Application relue | `python cli.py bootstrap-apply … --apply` | `root` sur le parcours supporté | runtime, modèles, registre, preuves et raccord de l'EnvironmentFile |

`install.sh` reste la seule voie supportée pour poser le **service**. Sur une
machine neuve il accepte que `llama-server` soit encore absent : `/health` et
les routes d'administration démarrent, tandis que `/ready` reste honnêtement
rouge jusqu'à l'application du plan. `bootstrap-apply` pose ensuite le runtime
sous `/opt/llama.cpp/current/llama-server`, télécharge le modèle, le calibre et
prouve le premier token à travers le chemin public.

`bootstrap-plan` inventorie l'hôte, résout quel `llama-server` conviendrait,
filtre le catalogue de modèles approuvés et rend cette séquence — **sans rien
télécharger, compiler ni écrire**. Le seul sous-processus qu'il puisse lancer
est `llama-server --version`, et seulement si on lui fournit `--llama-bin`.

L'intérêt pratique : le plan se lit, se discute et se colle dans un ticket avant
qu'une machine ne soit touchée, et il dit *pourquoi* chaque choix a été fait —
là où un installeur ne dit que ce qu'il a fait.

```bash
# Sur l'hôte cible, après installation du socle (§4). Le compte de service peut
# lire le venv et le catalogue sans recevoir de privilège système supplémentaire.
cd /opt/llm-gateway
sudo -u llmservice ./venv/bin/python cli.py bootstrap-plan --models-dir /models
```

Sans `--pin-version`/`--pin-commit`, le plan sort **bloqué** et ne propose aucune
étape : le planificateur refuse d'inventer un numéro de build. Référence complète
des options et des exit codes :
[guide administrateur, section 9](admin.md#9-planificateur-damorçage--bootstrap-plan).

Épingler la version ne suffit pas à rendre le plan **applicable** : la matrice
d'artefacts livrée avec EVARuntime ne porte aucune empreinte et aucune URL
d'archive, de sorte que seule la voie du build local y est éligible. Pour que
`bootstrap-apply` installe réellement un runtime, fournissez votre propre matrice
avec `--runtime-variants`, à partir du modèle commenté installé avec le service :

```bash
sudo install -d -m 0755 /etc/evaruntime
sudo install -m 0644 \
  /opt/llm-gateway/deploy/runtime-variants.yaml.example \
  /etc/evaruntime/runtime-variants.yaml
sudoedit /etc/evaruntime/runtime-variants.yaml
# relever les vraies empreintes — le fichier refuse de se charger tant que les
# marqueurs REMPLACER y figurent
sudo -u llmservice ./venv/bin/python cli.py bootstrap-plan --json --strict \
    --pin-version b6210 --pin-commit <sha_git> --min-build 6120 \
    --runtime-variants /etc/evaruntime/runtime-variants.yaml \
    --models-dir /models > /tmp/plan.json
```

Le fichier **remplace** la matrice livrée : redéclarez-y la variante
`local-build` si vous voulez conserver cette voie. Champs, contrôles et méthode
de relevé : [guide administrateur, section 9](admin.md#épingler-son-runtime----runtime-variants).

Si `--models-dir` contient déjà les GGUF du catalogue et qu'un manifeste de
provenance les atteste, le plan **ne propose pas de les retélécharger** : l'étape
`download_model` disparaît, le volume annoncé est décompté, et seule la
vérification d'empreinte subsiste. Un fichier présent dont la **taille** diffère
de celle épinglée bloque le plan au lieu d'être écrasé. Le détail des trois cas —
et pourquoi le plan n'a jamais à hacher les fichiers pour conclure — est dans le
[guide administrateur, section 9](admin.md#artefacts-déjà-présents-sur-lhôte).

Les sections 2 et 3 qui suivent décrivent la procédure manuelle de secours
(compilation de llama.cpp, téléchargement des GGUF). Le parcours de production
automatisé et copiable se trouve en [§4](#parcours-production-complet--mode-local).

---

## 2. Installation de llama.cpp

Cette procédure compile `llama-server` depuis les sources avec le toolkit CUDA
et l'architecture détectés sur l'hôte. Elle publie ensuite le binaire sous le
chemin atomique lu par la gateway.

```bash
# Dépendances de compilation
sudo apt install -y build-essential cmake ninja-build git libcurl4-openssl-dev

# CUDACXX doit désigner le toolkit CUDA adapté au pilote et au GPU.
export CUDACXX="$(command -v nvcc)"
test -x "$CUDACXX"

# CMake accepte une liste séparée par des points-virgules pour des GPU variés.
CUDA_ARCHITECTURES="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader \
  | awk '{gsub(/\./, ""); print}' | sort -u | paste -sd ';' -)"
test -n "$CUDA_ARCHITECTURES"
printf 'Architectures CUDA : %s\n' "$CUDA_ARCHITECTURES"

sudo install -d -m 0755 /opt/src
sudo chown "$USER":"$(id -gn)" /opt/src
git clone https://github.com/ggml-org/llama.cpp.git /opt/src/llama.cpp
cd /opt/src/llama.cpp

# Compiler et ne construire que le serveur.
cmake -S . -B build -G Ninja \
  -DGGML_CUDA=ON \
  -DCMAKE_CUDA_COMPILER="$CUDACXX" \
  -DCMAKE_CUDA_ARCHITECTURES="$CUDA_ARCHITECTURES" \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_SHARED_LIBS=OFF \
  -DLLAMA_BUILD_UI=OFF \
  -DLLAMA_USE_PREBUILT_UI=OFF

cmake --build build --target llama-server --parallel "$(nproc)"

# Publier une release immuable derrière le même lien que bootstrap-apply.
RELEASE="manual-$(git rev-parse --short HEAD)"
sudo install -d -m 0755 "/opt/llama.cpp/releases/$RELEASE"
sudo install -m 0755 build/bin/llama-server \
  "/opt/llama.cpp/releases/$RELEASE/llama-server"
sudo ln -sfn "releases/$RELEASE" /opt/llama.cpp/.current-new
sudo mv -Tf /opt/llama.cpp/.current-new /opt/llama.cpp/current

# Vérifier l'installation
/opt/llama.cpp/current/llama-server --version
```

> **Note :** La compilation prend 5 à 15 minutes selon la machine.
> La commande déduit `CMAKE_CUDA_ARCHITECTURES` depuis `nvidia-smi`. Si le
> toolkit choisi ne reconnaît pas l'architecture annoncée, installez un toolkit
> CUDA plus récent, définissez `CUDACXX` vers son `nvcc`, puis reconfigurez.
>
> La voie manuelle ne fabrique pas le manifeste de provenance de
> `bootstrap-apply`. Elle exige donc une attestation et une mise à jour manuelles
> de `LLAMA_SERVER_MIN_BUILD` avant production.

---

## 3. Téléchargement des modèles

Le gateway supporte plusieurs modèles simultanément. Chaque modèle est un fichier
`.gguf` indépendant. Télécharger ceux que vous souhaitez proposer, puis les déclarer
dans le [registre des modèles](#6-registre-des-modèles-modelsyaml).

En mode local, exécuter ces commandes sur le gateway. En mode cluster, les
exécuter sur chaque nœud GPU éligible (ou monter un stockage partagé) et conserver
strictement les mêmes chemins absolus; l'orchestrateur ne télécharge aucun GGUF.

```bash
# Installer huggingface-cli
pip3 install huggingface-hub

# Créer le répertoire des modèles
sudo mkdir -p /models
```

### Modèle principal — Llama 3.3 70B Q4_K_M (~42 GB)

Qualité élevée ; vérifier qu'environ 42 Go de VRAM sont disponibles avant de
l'activer.

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

### Modèles vision (multimodaux)

Les modèles capables de traiter des images (LLaVA, Qwen2-VL, InternVL, etc.)
nécessitent **deux fichiers GGUF** : le modèle principal et le **projecteur multimodal**
(`mmproj`). Sans le fichier mmproj, llama-server retourne HTTP 500 sur toute
requête contenant une image, même si la capability `vision` est déclarée.

```bash
# Exemple avec LLaVA 1.6 Mistral 7B
huggingface-cli download bartowski/llava-v1.6-mistral-7b-GGUF \
  --include "*Q4_K_M*" "*mmproj*" \
  --local-dir /models/

ls -lh /models/llava*
# → llava-v1.6-mistral-7b-Q4_K_M.gguf          ~4.4G   ← modèle principal
# → llava-v1.6-mistral-7b-mmproj-f16.gguf       ~0.6G   ← projecteur CLIP (requis)
```

Le `mmproj_path` est déclaré dans `models.yaml` — voir section 6 pour la structure complète.

> **Note :** Avec llama.cpp ≥ mai 2025, le flag `-hf org/repo` télécharge le mmproj
> automatiquement depuis HuggingFace. Pour les fichiers locaux (notre cas), le champ
> `mmproj_path` est **toujours obligatoire**.

### Budget VRAM — planification

| Modèle | Fichier | Poids VRAM | KV cache | Total estimé |
|--------|---------|------------|----------|--------------|
| Llama 3.3 70B Q4_K_M | `Llama-3.3-70B-Instruct-Q4_K_M.gguf` | ~38–40 GB | ~2.5 GB (4 slots × 8K, Q8) | ~42 GB |
| Llama 3.1 8B Q4_K_M | `Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf` | ~4.5 GB | ~1 GB (8 slots × 8K, Q8) | ~5.5 GB |
| LLaVA 1.6 Mistral 7B Q4_K_M | `llava-v1.6-mistral-7b-Q4_K_M.gguf` + mmproj | ~4.4 GB + 0.6 GB mmproj | ~1 GB | ~6 GB |

**Exemple pour un GPU de 48 Go :**
- Budget net disponible = 48 − 2 (overhead) − 2.4 (marge 5%) = **43.6 GB**
- 70B seul : 42 GB ≤ 43.6 GB ✓
- 70B + 8B simultanément : 42 + 5.5 = 47.5 GB > 43.6 GB → éviction LRU automatique

> **La VRAM n'est pas la seule contrainte.** Les modèles MoE déclarés avec
> `cpu_moe: true` (`--cpu-moe`) laissent leurs experts FFN **en RAM hôte** : ils
> ne consomment presque pas de VRAM, mais exigent des dizaines — voire des
> centaines — de gigaoctets de RAM, et cette RAM est comptée dans le cgroup
> systemd du service. Avant d'activer un modèle, consulter la table
> « modèle → RAM hôte requise → VRAM requise » de
> [§15](#15-durcissement-systemd-et-profils-mémoire).

### Catalogue de modèles approuvés (amorçage)

Les téléchargements ci-dessus sont manuels et libres. Le planificateur
d'amorçage, lui, ne propose que ce qui figure dans un **catalogue de modèles
approuvés** : `gateway/bootstrap/catalog.yaml`.

Ce fichier **n'est pas le registre opérationnel**. `models.yaml` décrit ce que
*cette* installation sert, avec des chemins locaux et des `vram_gb` calibrés ; le
catalogue décrit ce que le bootstrap a le **droit** de proposer au téléchargement.
Les deux ne se recouvrent jamais et n'ont pas le même cycle de vie : le registre
est une conséquence du catalogue, jamais l'inverse.

Chaque entrée distingue explicitement :

| Champ | Ce qu'il fixe |
|---|---|
| `license.base_model` | licence du **modèle de base** (dépôt d'origine) |
| `license.fine_tune` | licence du **fine-tune / de la quantisation GGUF** publiée |
| `license.redistribution_allowed` | droit de recopier le GGUF vers un miroir interne |
| `license.gated` | dépôt exigeant une acceptation nominative et un jeton |
| `license.operator_acceptance_required` | acceptation explicite avant tout téléchargement |
| `source.revision` | SHA de commit **de 40 caractères** — une branche n'est pas une révision, elle bouge |
| `source.files[].sha256` | empreinte de chaque fichier de l'ensemble |
| `resources` | **estimations** conservatrices, jamais des mesures |

La licence du **logiciel de téléchargement** (`huggingface_hub`, Apache-2.0) est
commune à toutes les entrées et vit au niveau du catalogue. Une licence dont
l'identifiant n'est pas connu du chargeur fait **échouer le chargement** : on ne
devine pas une licence.

**Règle fail-closed.** Une entrée dont la révision ou l'un des SHA-256 vaut `null`
est chargée, listée et visible — mais marquée `pending` et **écartée de toute
planification de téléchargement**, avec un constat bloquant qui dit comment
l'épingler. L'entrée reste dans le catalogue à dessein : la faire disparaître
priverait l'opérateur de l'information qui lui permet de la corriger. Une entrée
bloquée est un désagrément ; un SHA inventé transforme la vérification
d'intégrité en théâtre.

Deux entrées sont livrées, toutes deux Apache-2.0 de bout en bout, non gated, et
destinées à la recette du premier token — **pas à la production** :

| Identifiant | Dépôt | Quantisation | Taille |
|---|---|---|---|
| `qwen2.5-0.5b-instruct-q4_k_m` | `Qwen/Qwen2.5-0.5B-Instruct-GGUF` | Q4_K_M | ~0,46 Go |
| `smollm2-360m-instruct-q8_0` | `HuggingFaceTB/SmolLM2-360M-Instruct-GGUF` | Q8_0 | ~0,36 Go |

La seconde vient d'un éditeur distinct pour que la recette du premier token ne
dépende pas de la disponibilité d'un unique dépôt.

Leurs révisions et empreintes sont **réelles** : relevées le 2026-07-31 sur l'API
publique Hugging Face et recoupées avec l'en-tête `X-Linked-Etag`. Pour les
revérifier — lecture publique, aucun jeton requis :

```bash
# Révision (champ `sha`) et empreinte de chaque fichier (`siblings[].lfs.sha256`)
curl -s "https://huggingface.co/api/models/<repo_id>?blobs=true" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); \
      print(d["sha"]); \
      [print(s["rfilename"], s.get("lfs",{}).get("sha256"), s.get("size")) \
       for s in d["siblings"]]'

# Recoupement : les deux endpoints doivent donner le même SHA-256
curl -sI "https://huggingface.co/<repo_id>/resolve/<revision>/<fichier>" | grep -i x-linked-etag
```

Les fichiers d'une entrée forment un **ensemble indivisible** : shards GGUF et
projecteur `mmproj` se téléchargent ensemble ou pas du tout. Le chargeur refuse
une série de shards incomplète ou non contiguë, et un `mmproj` manquant quand le
runtime le déclare nécessaire.

Enfin, les valeurs de `resources` ne doivent jamais être recopiées telles quelles
dans le `vram_gb` du registre : ce sont des estimations, que seule une
calibration par chargement réel remplace par des pics observés — voir
[Estimé contre mesuré](architecture.md#estimé-contre-mesuré).

---

## 4. Installation du gateway

### Choisir le parcours

| Mode | Quand l'utiliser | Hôte gateway | Nœuds GPU |
|------|-------------------|--------------|-----------|
| `local` | un serveur GPU autonome | gateway + nginx + SQLite + `llama-server` | aucun agent |
| `cluster` | plusieurs serveurs GPU | orchestrateur + nginx + SQLite, sans accès GPU | un `node_agent` par serveur |

Le mode local est le seul défaut implicite d'une installation neuve. En
production, toujours écrire le mode dans la commande et commencer par le plan
non destructif :

```bash
# Cloner le projet
git clone https://github.com/Tutanka01/EVARuntime.git /tmp/llm-gateway-src

# Parcours A — mono-nœud
bash /tmp/llm-gateway-src/gateway/deploy/install.sh --mode local --dry-run
sudo bash /tmp/llm-gateway-src/gateway/deploy/install.sh --mode local

# Parcours B — orchestrateur multi-nœuds
bash /tmp/llm-gateway-src/gateway/deploy/install.sh --mode cluster --dry-run
sudo bash /tmp/llm-gateway-src/gateway/deploy/install.sh --mode cluster
```

`--cluster` reste accepté comme alias de compatibilité. `--dry-run` ne requiert
pas root et n'exécute ni écriture, ni `pip`, ni `git`, ni `systemctl`.

#### Installer en mode local sur un hôte sans GPU — `--allow-no-gpu`

En mode local, le préflight exige `nvidia-smi` et **refuse** l'installation s'il
est absent. Ce refus est délibéré : un gateway local sans GPU ne peut rien
offloader. Il existe pourtant des cas légitimes — banc CPU, recette de bout en
bout, maquette d'intégration — et le contournement pratiqué jusqu'ici consistait
à éditer le script, ce qui ne laissait **aucune trace** : ni dans la
configuration, ni dans le diagnostic.

```bash
# Refusé : l'hôte n'a pas de GPU et rien ne l'assume
sudo bash install.sh --mode local
# → le message liste les trois conduites à tenir possibles

# Accepté : l'absence de GPU est ASSUMÉE, et tracée
sudo bash install.sh --mode local --allow-no-gpu
```

L'option écrit `ALLOW_NO_GPU=true` dans `/etc/llm-gateway/env`. Conséquences :

- `evaruntime doctor` rapporte `gpu_inventory` en **`skip` / `gpu_absence_declared`**
  — « pas de GPU, par décision » — au lieu de **`warn` / `nvidia_smi_unavailable`**
  — « GPU attendu mais absent ». Deux diagnostics distincts, deux verdicts
  distincts : le premier ne colore pas le rapport, le second reste un signal ;
- si un GPU apparaît plus tard alors que la clé est encore là, doctor le signale
  en `warn` / `gpu_waiver_stale` : une renonciation ne doit pas survivre au
  matériel qui la justifiait. Retirez la clé, ou relancez `install.sh` sans
  l'option — l'installateur remet `ALLOW_NO_GPU=false` dès qu'il détecte un GPU ;
- rien d'autre ne change : aucun offload GPU, `TOTAL_VRAM_GB` et les attentes de
  latence sont à adapter à la main.

`update.sh` relit cette décision avant son préflight : en mode local,
`nvidia-smi` reste obligatoire par défaut, mais une installation portant déjà
`ALLOW_NO_GPU=true` reste mise à jour sans nouvelle option ni modification du
script. Sans cette trace persistée, la mise à jour conserve le refus historique
et explique comment déclarer le banc CPU par `install.sh --allow-no-gpu`.

En mode cluster, l'option est sans objet — l'orchestrateur n'a jamais de GPU
local — et le script le dit.

Le script effectue automatiquement :

1. Création de l'utilisateur système `llmservice` (sans shell, sans home)
2. Ajout au groupe `render,video` pour l'accès GPU
3. Création des répertoires (`/opt/llm-gateway`, `/var/lib/llm-gateway`, `/var/log/llm-gateway`, `/etc/llm-gateway`)
4. Copie du code source (`gateway`, `cluster/` et `bootstrap/` avec son
   catalogue) et création du virtualenv Python
5. Installation des dépendances Python
6. **Génération automatique des secrets** (`INTERNAL_API_KEY`, `ADMIN_SECRET`) dans `/etc/llm-gateway/env`
7. Copie du registre dans `/var/lib/llm-gateway/models.yaml` (writable par le
   service pour les mutations admin atomiques; les secrets restent sous `/etc`)
8. Enregistrement du service systemd et activation
9. Initialisation de la base de données SQLite

À la fin du script, les prochaines étapes sont affichées avec les valeurs générées.

> **Important :** Noter l'`ADMIN_SECRET` affiché à la fin du script.
> Il ne sera plus visible ensuite (stocké dans `/etc/llm-gateway/env`).

L'installateur conserve tout fichier `env`, `models.yaml` ou `nodes.yaml`
existant. Il déduplique uniquement les clés de mode qu'il gère et ne régénère
jamais un secret déjà configuré. Une ancienne installation dont le registre
est sous `/etc/llm-gateway/models.yaml` est copiée sans suppression vers
`/var/lib/llm-gateway/models.yaml`; le reste de `env` demeure inchangé.

### Parcours production complet — mode local

Ce parcours est la référence « machine neuve → premier token ». Il n'emploie
aucun fichier secret intermédiaire et ne demande aucune édition manuelle de
`models.yaml`. Remplacez les valeurs entre chevrons ; ne recopiez jamais une
empreinte d'exemple.

1. Poser le socle, sans exiger un runtime déjà présent :

   ```bash
   sudo install -d -m 0755 /opt/src
   sudo chown "$USER":"$(id -gn)" /opt/src
   git clone https://github.com/Tutanka01/EVARuntime.git /opt/src/EVARuntime
   sudo bash /opt/src/EVARuntime/gateway/deploy/install.sh --mode local
   ```

   Une installation neuve livre un registre dont tous les modèles sont
   désactivés. `/health` peut donc démarrer avant les artefacts ; `/ready` reste
   non prêt tant que le runtime et un modèle n'ont pas été prouvés.

2. Épingler l'archive runtime et produire un plan strict :

   ```bash
   sudo install -d -m 0755 /etc/evaruntime
   sudo install -m 0644 \
     /opt/llm-gateway/deploy/runtime-variants.yaml.example \
     /etc/evaruntime/runtime-variants.yaml
   sudoedit /etc/evaruntime/runtime-variants.yaml

   cd /opt/llm-gateway
   sudo -u llmservice ./venv/bin/python cli.py bootstrap-plan --json --strict \
     --pin-version <bNNNNN> \
     --pin-commit <sha_git_du_tag> \
     --min-build <premier_build_corrige> \
     --runtime-variants /etc/evaruntime/runtime-variants.yaml \
     --models-dir /models \
     --model qwen2.5-0.5b-instruct-q4_k_m \
     > /tmp/evaruntime-plan.json
   ```

   Le code attendu est `0`. Un code `1`, `2`, `3` ou `4` doit être traité avant
   l'application ; avec `--strict`, un avertissement n'est pas accepté comme
   dette implicite de production.

3. Configurer TLS et nginx ([§7](#7-certificat-tls),
   [§8](#8-configuration-nginx)), puis démarrer le mono-worker :

   ```bash
   sudo systemctl start llm-gateway
   curl -fsS http://127.0.0.1:8000/health
   ```

   À ce stade, un `/ready` non-200 est normal : le lien runtime et les GGUF ne
   sont pas encore publiés.

4. Déclarer une seule fois les arguments relus, simuler, puis appliquer :

   ```bash
   BOOTSTRAP_ARGS=(
     --allowed-root /opt/llama.cpp
     --allowed-root /models
     --allowed-root /var/lib/llm-gateway
     --allowed-root /etc/llm-gateway
     --models-dir /models
     --registry /var/lib/llm-gateway/models.yaml
     --runtime-root /opt/llama.cpp
     --calibration-report-dir /var/lib/llm-gateway/calibration
     --base-url https://<fqdn-production>
     --admin-url http://127.0.0.1:8000
     --env-file /etc/llm-gateway/env
     --vram-budget-gb <budget_vram_net>
     --accept-license qwen2.5-0.5b-instruct-q4_k_m
     --license-reference <ticket-technique>
     --ttft-threshold-ms <seuil_valide>
     --ttft-gate
   )

   # Simulation : aucune mutation ; le code 3 est attendu par contrat.
   sudo ./venv/bin/python cli.py bootstrap-apply \
     /tmp/evaruntime-plan.json "${BOOTSTRAP_ARGS[@]}"

   # Application réelle et rapport archivable, sans secret sur argv/stdout.
   sudo ./venv/bin/python cli.py bootstrap-apply \
     /tmp/evaruntime-plan.json --apply --json "${BOOTSTRAP_ARGS[@]}" \
     > /tmp/evaruntime-install-report.json
   ```

   Avant toute mutation, la CLI recoupe trois chemins : `--registry` doit être
   le `MODELS_CONFIG_PATH` du service, le runtime publié doit être son
   `LLAMA_SERVER_BIN`, et `--min-build` doit être strictement positif. Le secret
   admin est lu depuis `--env-file`; `/etc/llm-gateway/admin.secret` n'existe pas
   et n'est pas nécessaire. Après succès complet, la CLI publie atomiquement
   `LLAMA_SERVER_BIN` et `LLAMA_SERVER_MIN_BUILD` dans le même EnvironmentFile,
   sans changer ni afficher les secrets. Une mutation observée avant la bascule
   fait échouer l'écriture au lieu d'être écrasée.

5. Archiver, redémarrer sous le plancher désormais persistant et revalider :

   ```bash
   sudo install -o root -g llmservice -m 0640 \
     /tmp/evaruntime-install-report.json \
     /var/lib/llm-gateway/evaruntime-install-report.json
   sudo systemctl restart llm-gateway

   sudo /opt/llm-gateway/venv/bin/python /opt/llm-gateway/cli.py doctor \
     --env-file /etc/llm-gateway/env --strict
   sudo bash /opt/llm-gateway/deploy/smoke_test.sh \
     --base-url https://<fqdn-production> --json
   ```

Le rapport JSON, le fichier `runtime-variants.yaml`, la révision Git déployée et
les sorties `doctor`/smoke constituent ensemble la preuve de déploiement. Ne
prononcez pas la production sur le seul code de sortie de `install.sh`.

---

## 5. Configuration

Le fichier de configuration se trouve dans `/etc/llm-gateway/env`.
C'est là que vivent **tous les secrets et paramètres globaux** — jamais dans le code source.
Les paramètres spécifiques à chaque modèle (taille de contexte, nombre de slots, etc.)
se trouvent dans `models.yaml` (voir section 6).

```bash
sudo nano /etc/llm-gateway/env
```

### Les trois durcissements posés par l'installateur

`install.sh` écrit trois réglages de sécurité dans le fichier généré. Ils y sont
**visibles et commentés** : un réglage absent du fichier n'existe pas pour
l'exploitant.

| Clé | Valeur posée | Ce qu'elle protège |
|---|---|---|
| `ALLOWED_MODEL_DIRS` | le répertoire de modèles créé par l'installateur **plus** les répertoires déclarés par le registre livré | seuls des `.gguf` de ces répertoires peuvent être déclarés dans `models.yaml` — barrière contre un chemin arbitraire injecté par l'API admin |
| `CORS_ALLOW_ORIGINS` | **vide** = aucune origine navigateur | `*` autoriserait n'importe quelle page web à parler à la gateway. L'API est consommée par des clients serveur, que CORS ne concerne pas, et le dashboard admin est servi depuis la même origine |
| `LLAMA_SERVER_MIN_BUILD` | `0` (aucun enforcement) | plancher de version `llama-server` contre GHSA-8947-pfff-2f3c. **À relever** : `doctor` le signale à chaque exécution tant qu'il vaut 0 |

Deux pièges d'exploitation :

- **`ALLOWED_MODEL_DIRS` est validée sur TOUTES les entrées de `models.yaml`,
  activées ou non.** Une seule entrée hors allowlist et le service **refuse de
  démarrer**. C'est pour cela que la valeur posée est dérivée du registre que
  l'installateur pose, et non écrite en dur. Restreignez-la dès que vos chemins
  réels sont fixés, puis validez avec `evaruntime doctor` avant de démarrer.
- **`update.sh` ne pose pas ces clés sur une installation antérieure.** Il les
  **signale** en préflight : ajouter `CORS_ALLOW_ORIGINS=` d'autorité couperait
  un client navigateur existant au milieu d'une mise à jour. À vous de trancher,
  clé par clé.

### Paramètres critiques à vérifier

```bash
# ── Registre des modèles ──────────────────────────────────────────────────────
# Chemin vers le fichier YAML listant tous les modèles disponibles
MODELS_CONFIG_PATH=/var/lib/llm-gateway/models.yaml

# Lien atomique publié par bootstrap-apply. Ne pointez pas le service vers une
# release horodatée : `current` est le contrat de bascule et de rollback.
LLAMA_SERVER_BIN=/opt/llama.cpp/current/llama-server

# ── Budget VRAM ────────────────────────────────────────────────────────────────
# Renseigner la capacité réellement disponible sur l'hôte.
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
# Liste séparée par des virgules, ou tableau JSON ["/models","/data/models"].
# Vide = pas de restriction — à ne pas laisser tel quel en production.
ALLOWED_MODEL_DIRS=/models

# ── GPU ────────────────────────────────────────────────────────────────────────
CUDA_VISIBLE_DEVICES=0          # index du GPU (0 = premier)

# ── Comportement idle (commun à tous les modèles) ────────────────────────────
IDLE_TIMEOUT_SECONDS=300        # décharger après 5 min sans requête
# ↑ Augmenter si les utilisateurs reviennent souvent (ex: 600 pour 10 min)
# ↓ Diminuer pour économiser l'électricité (ex: 120 pour 2 min)

# ── Queue d'admission VRAM ────────────────────────────────────────────────────
# Attend une libération VRAM/port au lieu de retourner 503 immédiatement.
CAPACITY_QUEUE_ENABLED=true
CAPACITY_QUEUE_TIMEOUT_SECONDS=120
CAPACITY_QUEUE_MAX_WAITERS=100
CAPACITY_QUEUE_RETRY_AFTER_SECONDS=10

# ── Secrets (générés par install.sh — ne pas modifier manuellement) ───────────
# IMPORTANT : les routes /admin répondent 503 tant qu'ADMIN_SECRET est vide ou
# laissé à une valeur d'exemple CHANGE_ME_*. La clé interne est transmise à
# llama-server via la variable d'environnement LLAMA_API_KEY (jamais en argument
# de commande, qui serait visible via ps).
INTERNAL_API_KEY=<généré>
ADMIN_SECRET=<généré>

# ── CORS ──────────────────────────────────────────────────────────────────────
# Origines navigateur autorisées (séparées par des virgules, ou tableau JSON).
# "*" par défaut ; en production, restreindre aux domaines clients connus.
# Une valeur vide n'autorise aucune origine — ce n'est pas un joker implicite.
# CORS_ALLOW_ORIGINS=https://app.example.com
```

---

## 6. Registre des modèles (models.yaml)

Le fichier `models.yaml` est la **source de vérité** pour tous les modèles disponibles.
Il est installé dans `/var/lib/llm-gateway/models.yaml`.

```bash
sudoedit /var/lib/llm-gateway/models.yaml
```

### Structure

```yaml
models:
  - id: "llama-3.3-70b-instruct"          # identifiant unique (regex: ^[a-z0-9][a-z0-9._-]{0,62}$)
    path: "/models/Llama-3.3-70B-Instruct-Q4_K_M.gguf"   # chemin absolu vers le .gguf
    description: "Llama 3.3 70B Instruct, Q4_K_M — modèle principal"
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
      flash_attn: true                     # Flash Attention 2 si le build le prend en charge
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

  # Exemple de modèle vision — deux fichiers requis
  - id: "llava-7b"
    path: "/models/llava-v1.6-mistral-7b-Q4_K_M.gguf"
    mmproj_path: "/models/llava-v1.6-mistral-7b-mmproj-f16.gguf"   # OBLIGATOIRE si vision
    description: "LLaVA 1.6 Mistral 7B — vision + texte"
    vram_gb: 6.0
    enabled: false
    capabilities:
      - text_generation
      - vision
      - streaming
    llama_params:
      n_gpu_layers: 999
      ctx_size: 8192
      parallel: 4
      batch_size: 2048
      ubatch_size: 512
      cache_type_k: "q8_0"
      cache_type_v: "q8_0"
      flash_attn: true
      threads: 4
      threads_http: 2
```

> **Avant de passer un modèle à `enabled: true` :** vérifier la RAM **hôte**
> requise, pas seulement la VRAM. Un modèle avec `cpu_moe: true` garde ses experts
> FFN résidents en RAM et cette RAM est comptée dans le cgroup systemd du service.
> Table de dimensionnement et procédure d'activation :
> [§15.1](#151-couple-modelsyaml--limites-mémoire-systemd). `minimax-m2.7` est
> livré `enabled: false` pour cette raison (~236 Go de RAM hôte requis).

> **Modèles vision — règle absolue :** si `vision` est dans `capabilities`, le champ
> `mmproj_path` doit pointer vers le fichier projecteur CLIP (`.gguf`). Sans lui,
> llama-server démarre correctement mais retourne **HTTP 500** sur toute requête avec
> image. La gateway émet un avertissement dans les logs au démarrage si `mmproj_path`
> est absent pour un modèle vision.

### Vérification d'intégrité `sha256` (optionnel — supply-chain)

Champ optionnel par modèle : l'empreinte SHA-256 attendue du fichier GGUF.

```yaml
  - id: "llama-3.3-70b-instruct"
    path: "/models/Llama-3.3-70B-Instruct-Q4_K_M.gguf"
    sha256: "3f2a...<64 hex>"   # empreinte attendue (64 caractères hexadécimaux)
    # ...
```

Calculer l'empreinte d'un GGUF :

```bash
sha256sum /models/Llama-3.3-70B-Instruct-Q4_K_M.gguf
```

Comportement :

- **absent** (défaut) → aucune vérification (rétro-compatible) ;
- **présent** → au démarrage (gateway) et avant chaque chargement (node-agent),
  le SHA-256 réel du fichier est recalculé et comparé. Un écart bloque le
  chargement du modèle (log critical côté gateway, HTTP 422 côté node-agent) —
  protection contre un GGUF substitué ou corrompu.

> **Coût :** hacher un GGUF de plusieurs Go prend plusieurs secondes. La
> vérification n'a lieu qu'au démarrage/chargement, jamais dans le chemin de
> requête. À réserver aux modèles critiques.

### Activer un modèle

```bash
# 1. Vérifier que le fichier .gguf est présent
ls -lh /models/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf

# 2. Éditer le registre
sudoedit /var/lib/llm-gateway/models.yaml
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
sudoedit /var/lib/llm-gateway/models.yaml

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
- Les `id` doivent correspondre à `^[a-z0-9][a-z0-9._-]{0,62}$` (pas de `/`, `..`, etc.)
- Les `path` doivent être absolus et se terminer par `.gguf`
- Le `mmproj_path`, s'il est fourni, subit les mêmes validations que `path`
- Si `ALLOWED_MODEL_DIRS` est configuré, les chemins (`path` et `mmproj_path`) doivent être sous ces répertoires

---

## 7. Certificat TLS

L'accès HTTPS est **obligatoire** — les clés API transitent dans les headers.
Utilisez un nom DNS dont vous contrôlez le certificat, par une autorité de
certification publique (ACME) ou par la PKI de votre organisation.

```bash
# Installer le certificat et la clé émis pour votre nom DNS.
sudo install -m 0644 /chemin/vers/certificat.pem /etc/ssl/certs/llm-gateway.crt
sudo install -m 0600 /chemin/vers/cle-privee.pem /etc/ssl/private/llm-gateway.key
sudo chmod 600 /etc/ssl/private/llm-gateway.key
sudo chmod 644 /etc/ssl/certs/llm-gateway.crt
```

Renouvelez le certificat avant son expiration selon la procédure de votre AC ou
de votre PKI.

---

## 8. Configuration nginx

```bash
# Adapter le nom de domaine dans la config
sudo nano /etc/nginx/sites-available/llm-gateway

# Remplacer gateway.example.com par votre domaine réel.
# Vérifier les plages IP autorisées dans la section /admin/ :
#   allow 10.0.0.0/8;      ← adapter si besoin
#   allow 192.168.0.0/16;

# Tester la configuration
sudo nginx -t

# Activer et recharger
sudo ln -sf /etc/nginx/sites-available/llm-gateway \
            /etc/nginx/sites-enabled/llm-gateway
sudo nginx -s reload
```

### HTTP/2 : activé automatiquement, dans la forme qu'accepte votre nginx

Aucune écriture de la directive HTTP/2 n'est acceptée par l'ensemble du socle
supporté :

| Version nginx | `listen … ssl http2` | `http2 on;` |
|---------------|----------------------|-------------|
| 1.18 (Ubuntu 22.04 LTS) | fonctionne, sans avertissement | **`unknown directive` — nginx refuse de démarrer** |
| 1.24 (Ubuntu 24.04 LTS) | fonctionne, sans avertissement | **`unknown directive` — nginx refuse de démarrer** |
| ≥ 1.25.1 (Debian 13, nginx.org) | **déprécié — warning à chaque `nginx -t` et à chaque reload** | forme recommandée |

Le fichier `deploy/nginx.conf` est donc livré dans la seule forme valide partout
— un `listen` nu, sans HTTP/2 — pour qu'une copie manuelle ne casse jamais rien.

Mais `install.sh` et `update.sh` ne s'en tiennent pas là : ils lisent `nginx -v`
et **écrivent la forme adaptée à la version installée** (`deploy/nginx-lib.sh`).
Vous conservez donc HTTP/2 sur les deux LTS supportées *et* un `nginx -t`
silencieux sur les versions récentes. Les scripts annoncent leur choix :

```
[INFO] nginx 1.24.0 — HTTP/2 : listen … ssl http2
```

Sur un doute (version illisible, nginx absent), les scripts retombent
délibérément sur la forme sans HTTP/2 : un avertissement cosmétique est
préférable à un nginx qui refuse de démarrer.

**Copie manuelle de la conf** — si vous déployez `deploy/nginx.conf` à la main
plutôt que par les scripts, vous obtenez HTTP/1.1. Sur nginx ≥ 1.25.1,
décommentez `http2 on;` ; en dessous, ajoutez `http2` aux deux lignes `listen`.
Le streaming SSE, `proxy_buffering off` et les timeouts longs se comportent à
l'identique dans les deux cas — HTTP/2 n'apporte ici que le multiplexage de
plusieurs requêtes sur une même connexion cliente.

`nginx -t` ne doit produire **aucun** avertissement, quelle que soit la version :
un warning de dépréciation à chaque reload finit par masquer les vrais.

---

## 9. Démarrage et vérification

### Démarrer le service

```bash
sudo systemctl start llm-gateway
sudo systemctl status llm-gateway
```

Résultat attendu :

```
● llm-gateway.service - LLM Inference Gateway (FastAPI)
     Loaded: loaded (/etc/systemd/system/llm-gateway.service; enabled)
     Active: active (running) since ...
    Process: ExecStart=/opt/llm-gateway/venv/bin/uvicorn main:app ...
   Main PID: 12345 (uvicorn)
```

### Valider que le service SERT, pas seulement qu'il répond

`systemctl status` et `/health` ne prouvent que la liveness, `/ready` que la
readiness structurelle. Pour prouver qu'un token sort réellement du chemin
public, lancer la recette du premier token :

```bash
sudo bash /opt/llm-gateway/deploy/smoke_test.sh --base-url https://gateway.example.com
```

Elle crée une identité éphémère, charge le modèle, génère, vérifie le log
d'usage puis retire l'identité. Détails, options et exit codes :
[Recette du premier token](#recette-du-premier-token-smoke_testsh).

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
sudo -u llmservice ./venv/bin/python cli.py add-user test --email test@example.com
sudo -u llmservice ./venv/bin/python cli.py create-key test --name "test"
# → Copier la clé affichée : llmgw-XXXX...

# Tester (le modèle 70B va charger, attendre ~60-90s à la première requête)
curl -s https://gateway.example.com/v1/chat/completions \
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
# NVIDIA GPU, faible mémoire utilisée, mémoire libre élevée   ← GPU libéré ✓
```

### Consulter les logs

```bash
# Logs de la gateway (temps réel)
sudo journalctl -u llm-gateway -f

# Logs de llama-server (chaque modèle est préfixé dans les logs)
tail -f /var/log/llm-gateway/llama-server.log

# Filtrer les erreurs uniquement
sudo journalctl -u llm-gateway -p err --since "1 hour ago"

# Journal d'accès nginx — fichier DÉDIÉ, format rédigé (§16)
tail -f /var/log/nginx/llm-gateway-access.log
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
https://gateway.example.com/admin/dashboard
```

> **Prérequis réseau :** la route `/admin/` est restreinte au réseau campus par nginx
> (plages `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`). Le dashboard n'est donc
> pas accessible depuis Internet.

### Première connexion

1. Ouvrir `https://gateway.example.com/admin/dashboard` dans un navigateur
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
# Charger d'abord le script de mise à jour de la release visée. Un script bash
# déjà en cours conserve son ancienne version même si son propre git pull réussit.
git pull --ff-only

# Conserve automatiquement le mode installé
bash gateway/deploy/update.sh --dry-run
sudo bash gateway/deploy/update.sh

# Équivalents explicites
sudo bash gateway/deploy/update.sh --mode local
sudo bash gateway/deploy/update.sh --mode cluster
```

En cluster, cette commande met à jour **l'orchestrateur uniquement**. Exécuter
ensuite `node_agent/deploy/update-agent.sh` sur chaque nœud GPU; aucun code n'est
poussé à distance par l'orchestrateur.

Le `git pull` explicite avant l'invocation est important quand la release modifie
`update.sh` lui-même : le processus déjà lancé ne peut pas remplacer son propre
code en mémoire. Le script conserve tout de même son `git pull --ff-only` interne
comme garde-fou contre une révision arrivée entre la vérification et l'exécution.

Ce que fait le script :

1. Refuse un checkout sale puis exécute `git pull --ff-only`
2. Synchronise les modules racine, `cluster/`, `bootstrap/` avec son catalogue,
   les contraintes `requirements.txt` et le `requirements.lock` vérifié vers
   `/opt/llm-gateway/`
3. Synchronise le répertoire `static/` (dashboard HTML…)
4. Construit un **nouveau venv**, installe uniquement les versions et empreintes
   de `requirements.lock`, puis exige `pip check`; l'ancien venv reste intact
   jusqu'au redémarrage
5. **Sauvegarde la base SQLite** avant redémarrage (voir ci-dessous)
6. Installe la recette du premier token dans `/opt/llm-gateway/deploy/`
7. Exécute **`evaruntime doctor` avant la bascule**, avec le venv neuf et le code
   déjà synchronisé — le service tourne encore l'ancienne version, donc un hôte
   inapte est détecté sans aucune coupure
8. Choisit l'unité systemd GPU (`local`) ou sans GPU (`cluster`)
9. Redémarre le service et exige la readiness `/ready`, pas seulement `/health`
10. Exige la **recette du premier token** : une génération réelle de bout en bout
    (voir ci-dessous). C'est le gate qui distingue « le service répond » de
    « le service **sert** »
11. Repasse `doctor` sur l'hôte basculé, en constat non bloquant
12. **Rollback automatique** du code déployé, du venv, du mode et de l'unité si la
    readiness ou la recette échoue
13. **Purge les venvs de release excédentaires** une fois la version validée, en
    conservant l'actif et le précédent (voir ci-dessous)

> **Conservation :** les secrets, `nodes.yaml`, le contenu du registre, la DB et
> les GGUF ne sont jamais remplacés. Le script ne modifie dans `env` que les clés
> nécessaires au mode explicitement confirmé. Il peut copier l'ancien registre
> `/etc/llm-gateway/models.yaml` vers `/var/lib/llm-gateway/models.yaml` pour
> corriger les permissions d'écriture atomique; l'original reste en place.

#### Rétention des venvs de release

`update.sh` ne remplace jamais un venv : il en construit un neuf à son
emplacement définitif (`/opt/llm-gateway/venv-release-<commit>-<horodatage>`)
puis fait basculer le symlink `/opt/llm-gateway/venv`, le chemin figé dans
l'unité systemd. C'est ce qui rend le rollback instantané — mais aussi ce qui
ferait grossir `/opt` indéfiniment, une arborescence complète par mise à jour.

La rétention est donc **bornée et explicite** : une fois la version validée par
la recette du premier token, le script conserve **la release active et la
précédente**, et purge les plus anciennes. La cible courante du symlink n'est
jamais supprimée, quel que soit son âge — y compris après un retour arrière
manuel vers une vieille release. Le `venv-pre-update-*` laissé par la migration
depuis l'ancien schéma est traité comme une release ordinaire.

- Le nombre de releases conservées se règle par `EVA_GATEWAY_VENV_KEEP`
  (défaut : `2`, minimum : `1`).
- `--dry-run` annonce ce que la purge emporterait, sans rien supprimer.
- Un échec de purge n'échoue **jamais** la mise à jour : la gateway sert, seul
  l'espace disque manque. Le script le signale en avertissement.
- La purge n'a lieu **qu'après** la validation. Avant, la release précédente est
  encore ce vers quoi le rollback rebascule.

Retour arrière manuel, tant que la release est conservée :

```bash
ls -dt /opt/llm-gateway/venv-release-*
sudo ln -sfn /opt/llm-gateway/venv-release-<…> /opt/llm-gateway/venv
sudo systemctl restart llm-gateway
```

Le node-agent applique la même politique sur ses propres releases — voir
« Stratégie de venv du node-agent ».

#### Sauvegarde automatique avant mise à jour

Juste avant le redémarrage, `update.sh` crée une copie horodatée de la base :

```
/var/lib/llm-gateway/backups/gateway-pre-update-AAAAMMJJ-HHMMSS.db
```

La copie utilise `sqlite3 "$DB" ".backup '$DEST'"` — sûr sur une base **WAL
active** (un simple `cp` pourrait capturer un WAL incohérent). Si `sqlite3` est
absent, l'étape est ignorée avec un avertissement (la mise à jour continue).

#### Recette du premier token (`smoke_test.sh`)

`/ready` prouve que l'installation est **structurellement** saine : registre
lisible, binaire `llama-server` exécutable, GGUF des modèles activés présents,
base inscriptible, budget VRAM cohérent. Elle ne prouve **pas** qu'un token
sort. Une version applicative cassée dans le proxy d'inférence répondait donc
200 sur `/ready` et était acceptée puis déployée.

`gateway/deploy/smoke_test.sh` ferme ce trou en exerçant le **vrai chemin
public** :

```text
client → nginx si configuré → authentification → quota/rate limit
       → résolution du modèle → llama-server → chunk SSE AVEC du contenu
       → log d'usage
```

Séquence exécutée :

1. liveness `GET /health` sur le chemin public ;
2. readiness structurelle `GET /ready` sur le plan de contrôle ;
3. création d'un **utilisateur et d'une clé éphémères** ;
4. chargement **explicite** du modèle (`POST /admin/models/{id}/load`) ;
5. `POST /v1/chat/completions` avec `stream: true` ;
6. mesure du temps jusqu'aux en-têtes ;
7. mesure du **TTFT** = temps jusqu'au premier delta portant réellement du
   contenu — pas le premier octet SSE, qui n'est qu'un chunk de rôle ;
8. attente de `[DONE]` ;
9. contrôle de l'enveloppe (`chat.completion.chunk`), du modèle annoncé et de
   l'`usage` ;
10. vérification de l'écriture du log d'usage (`GET /admin/usage`) ;
11. révocation de la clé et **anonymisation** de l'utilisateur éphémère ;
12. rapport, sans aucune clé ni contenu généré.

##### Usage manuel

```bash
# Défauts : gateway en direct, modèle dérivé de la configuration
sudo bash /opt/llm-gateway/deploy/smoke_test.sh

# Exercer le vrai chemin client, TLS et non-buffering nginx compris
sudo bash /opt/llm-gateway/deploy/smoke_test.sh \
     --base-url https://gateway.example.com \
     --model llama-3.1-8b-instruct \
     --ttft-threshold-ms 5000

# Rapport machine
sudo bash /opt/llm-gateway/deploy/smoke_test.sh --json
```

> Le script est copié dans `/opt/llm-gateway/deploy/` par `install.sh` et
> `update.sh`. Sur une machine issue d'une ancienne version, le lancer depuis le
> checkout : `sudo bash gateway/deploy/smoke_test.sh`.

##### Recette opt-in avec un vrai petit GGUF

La suite ordinaire reste hors ligne et n'embarque pas de poids de modèle. Une
recette dédiée permet néanmoins d'attester séparément un vrai binaire
`llama-server` et un petit GGUF déjà téléchargé, avec empreinte obligatoire :

```bash
cd /chemin/vers/EVARuntime/gateway
EVARUNTIME_REAL_LLAMA_BIN=/opt/llama.cpp/current/llama-server \
EVARUNTIME_REAL_GGUF=/models/approved-small-model.gguf \
EVARUNTIME_REAL_GGUF_SHA256=<64-caracteres-hexadecimaux> \
.venv/bin/python -m pytest tests/test_real_small_gguf.py -v
```

Le test vérifie le SHA-256 avant exécution, démarre le runtime sur loopback avec
offload CPU par défaut, attend `/health`, exige un delta SSE avec contenu puis
`[DONE]`, et termine toujours le processus. Pour exercer le GPU, ajouter
`EVARUNTIME_REAL_N_GPU_LAYERS=99`. Cette recette runtime/GGUF complète mais ne
remplace pas `smoke_test.sh`, qui traverse la gateway, nginx, l'authentification
et la comptabilisation.

##### Options

| Option | Effet |
|---|---|
| `--base-url URL` | Chemin **public** exercé par la génération. Défaut : `http://GATEWAY_HOST:GATEWAY_PORT`. |
| `--admin-url URL` | Plan de contrôle et `/ready`. Défaut : la gateway **en direct**. |
| `--env-file PATH` | EnvironmentFile lu pour `ADMIN_SECRET`, le port et le modèle. Défaut : `/etc/llm-gateway/env`. |
| `--admin-secret-file PATH` | Lit `ADMIN_SECRET` dans un fichier root-only. |
| `--model ID` | Modèle exercé. Défaut : `DEFAULT_MODEL_ID`, sinon le plus petit modèle activé. |
| `--prompt TEXT` | Prompt du smoke test. |
| `--max-tokens N` | Plafond de génération (défaut 16). |
| `--ttft-threshold-ms N` | Seuil d'alerte sur le TTFT. `0` = désactivé (défaut). |
| `--fail-on-ttft` | Transforme le dépassement de seuil en **échec** (exit 4). |
| `--load-timeout SEC` | Attente du chargement explicite (défaut 330). |
| `--stream-timeout SEC` | Durée totale maximale du stream (défaut 120). |
| `--ready-timeout`, `--connect-timeout`, `--usage-timeout`, `--admin-timeout` | Autres bornes, toutes explicites. |
| `--ca-cert PATH`, `--insecure-tls` | Contrôle de la validation TLS. |
| `--json` | Rapport JSON sur `stdout` (les traces restent sur `stderr`). |

##### Exit codes

| Code | Sens | Effet dans `update.sh` |
|---:|---|---|
| 0 | Un delta SSE avec du contenu a traversé tout le chemin public | Version conservée |
| 1 | **Échec fonctionnel** : aucun contenu, erreur upstream, stream sans `[DONE]`, enveloppe/modèle/usage invalides, chargement impossible, log d'usage absent | **Rollback** |
| 2 | Erreur d'usage (option inconnue, dépendance manquante, configuration inexploitable) | **Rollback** |
| 3 | Préflight : liveness ou readiness structurelle en échec | **Rollback** |
| 4 | TTFT au-dessus du seuil **avec** `--fail-on-ttft` | **Rollback** (demandé explicitement) |
| 5 | Identité éphémère non créée, ou non nettoyée | Version **conservée**, `update.sh` sort en erreur |

##### Ce que chaque URL de base couvre — et ne couvre pas

- `--base-url` sur **nginx** (`https://…`) couvre en plus TLS, `limit_req`,
  `limit_conn`, l'anti-slowloris et surtout `proxy_buffering off` /
  `X-Accel-Buffering` : c'est la seule configuration qui prouve que le SSE n'est
  pas bufferisé par le reverse-proxy.
- `--base-url` sur la **gateway en direct** (défaut) ne couvre **rien** de tout
  cela : un premier token vert en direct peut rester invisible côté client
  derrière un nginx mal configuré.
- `--admin-url` vise la gateway **en direct** par défaut, volontairement : les
  routes `/admin/*` sont restreintes au réseau campus par nginx, et la recette
  doit rester exécutable depuis l'hôte lui-même. Depuis COR-009, viser nginx
  fonctionne aussi : `/ready` y est proxifiée et `location /admin/` laisse 900 s,
  soit au-delà des ~310 s qu'un `POST /admin/models/{id}/load` peut légitimement
  attendre (`load_timeout_seconds + 10`, pire modèle activé). Avant COR-009 ce
  même appel renvoyait 504 à travers nginx (`proxy_read_timeout 30s`) alors qu'il
  **réussissait** côté serveur, et la recette concluait à tort à une régression.

##### Sécurité de la recette

- Aucun secret n'est passé en argument : `ADMIN_SECRET` et la clé éphémère
  transitent par des fichiers de configuration `curl` en mode 600, dans un
  répertoire temporaire 700. Ils n'apparaissent ni dans `ps`, ni dans
  `/proc/*/cmdline`, ni dans le rapport.
- L'identité éphémère est nettoyée par un `trap` sur `EXIT`/`INT`/`TERM`/`HUP` :
  échec, erreur ou `Ctrl-C`, elle est toujours révoquée puis anonymisée. Le
  nettoyage est idempotent (un 404 est toléré).
- Le rapport ne contient ni clé, ni préfixe de clé, ni contenu généré :
  seulement des compteurs et des durées.

#### Succès fonctionnel : hard gate — TTFT : alerte

Ces deux notions sont **délibérément séparées** :

- **Succès fonctionnel** (un token sort, l'usage est comptabilisé) : hard gate.
  Un échec restaure la version précédente, sans exception.
- **Régression de TTFT** : par défaut une simple **alerte**. La version reste
  déployée et `update.sh` sort en succès. Un rollback automatique sur une
  latence dégradée provoquerait des boucles de restauration sur une machine
  momentanément chargée, sans qu'aucun défaut applicatif soit en cause.

Pour en faire un gate, il faut le demander explicitement :

```bash
# Alerte seulement (défaut) : le TTFT est mesuré et signalé, jamais bloquant
sudo bash gateway/deploy/update.sh --ttft-threshold-ms 5000

# Gate : un TTFT > 5 s restaure la version précédente
sudo bash gateway/deploy/update.sh --ttft-threshold-ms 5000 --ttft-gate

# Équivalents par variables d'environnement (automatisation)
EVA_SMOKE_TTFT_THRESHOLD_MS=5000 EVA_SMOKE_TTFT_GATE=1 \
  sudo -E bash gateway/deploy/update.sh
```

Sans `--ttft-threshold-ms`, aucun seuil n'est appliqué : le TTFT est mesuré et
rapporté, rien de plus. Les seuils par modèle ne sont pas encore stabilisés —
les fixer trop tôt ferait plus de mal que de bien.

#### Options de mise à jour liées à la recette

| Option `update.sh` | Variable | Effet |
|---|---|---|
| `--smoke-base-url URL` | `EVA_SMOKE_BASE_URL` | Chemin public exercé (viser nginx pour couvrir TLS et le non-buffering). |
| `--smoke-model ID` | `EVA_SMOKE_MODEL` | Modèle exercé. |
| `--ttft-threshold-ms N` | `EVA_SMOKE_TTFT_THRESHOLD_MS` | Seuil d'alerte TTFT (0 = désactivé). |
| `--ttft-gate` | `EVA_SMOKE_TTFT_GATE=1` | Le dépassement de seuil devient une cause de rollback. |
| `--skip-smoke-test` | — | **Dangereux** : revient au comportement d'avant COR-006 (validation sur `/ready` seul). Dépannage uniquement. |
| `--skip-doctor` | — | Désactive les deux préflights `doctor`. |

`bash gateway/deploy/update.sh --dry-run` affiche la politique retenue (chemin
exercé, seuil, alerte ou gate) avant toute modification.

#### Quand la recette échoue

`update.sh` a déjà restauré la version précédente : le service en cours est
**l'ancien**, sain. Le checkout Git n'a pas été touché.

1. Lire le rapport de la recette dans la trace de `update.sh` : la ligne
   `Cause` donne le code exact (`generation:no_content`,
   `generation:upstream_error`, `usage_log_missing`, `model_load_failed:503`…).
2. Rejouer la recette à la main contre la version restaurée pour distinguer une
   régression applicative d'un incident d'hôte :

   ```bash
   sudo bash /opt/llm-gateway/deploy/smoke_test.sh --json
   ```

3. Exécuter le préflight complet, qui ne demande aucun service en marche :

   ```bash
   /opt/llm-gateway/venv/bin/python /opt/llm-gateway/cli.py doctor \
       --env-file /etc/llm-gateway/env \
       --nginx-conf /etc/nginx/sites-available/llm-gateway \
       --systemd-unit /etc/systemd/system/llm-gateway.service
   ```

   Exit codes : `0` conforme, `1` échec bloquant, `2` erreur d'usage CLI,
   `3` avertissements seulement, `4` erreur interne. `update.sh` traite `0` et
   `3` comme un succès. Ne jamais ajouter `--verify-hashes` dans un contexte de
   déploiement : il relit intégralement les GGUF, soit plusieurs centaines de Go.

4. Consulter les journaux de la tentative :
   `sudo journalctl -u llm-gateway --since "30 min ago"`.
5. Corriger, puis relancer `sudo bash gateway/deploy/update.sh`.

Cas particuliers :

| Cause rapportée | Interprétation |
|---|---|
| `readiness_failed:model_file_missing` | Un GGUF d'un modèle `enabled: true` manque. Le télécharger, corriger son `path`, ou passer le modèle à `enabled: false`. |
| `model_load_failed:504` | Un reverse-proxy a coupé avant la fin du chargement. La conf livrée laisse 900 s sur `/admin/` depuis COR-009 : vérifier que l'hôte n'a pas gardé une conf antérieure (`doctor` le signale, contrôle `nginx_timeouts`), ou viser `--admin-url` en direct. |
| `generation:no_content` | Le stream s'ouvre en 200 mais n'émet aucun token : régression du proxy d'inférence ou backend muet. |
| `generation:no_done` | Le stream est tronqué : coupure réseau, timeout nginx trop court, ou `llama-server` tué (`MemoryMax`). |
| `usage_log_missing` | La génération réussit mais n'est pas comptabilisée : la facturation serait fausse. |
| exit 5 | La version est déployée et fonctionnelle, mais un compte de smoke test subsiste. Le retirer immédiatement avec la commande affichée dans le rapport. |

#### Rollback automatique

Avant toute synchronisation, le script copie le code réellement déployé dans
`/var/lib/llm-gateway/backups/code-pre-update-*`. Si `/ready` ne devient pas
200, **ou si la recette du premier token échoue**, il restaure ce snapshot et
l'ancien venv. L'ancienne version est donc conservée jusqu'à la fin de la
recette, pas seulement jusqu'au redémarrage. Une migration restaure aussi
`CLUSTER_MODE` et l'unité systemd précédents. Le checkout Git n'est jamais
modifié par le rollback et ne finit donc pas en `detached HEAD`. Un rollback
réussi fait quand même sortir `update.sh` en erreur afin que l'automatisation
ne confonde pas restauration et déploiement réussi.

Un échec bloquant de `doctor` **avant** la bascule restaure lui aussi le code
synchronisé, mais **sans jamais arrêter le service** : la version en production
continue de servir pendant toute la détection.

#### Redémarrages et start-limit systemd (COR-017)

Une unité qui a échoué plusieurs fois de suite atteint son **start-limit** :
systemd refuse alors tout démarrage (`Start request repeated too quickly`), y
compris celui du rollback. Tous les démarrages d'`install.sh` et `update.sh`
sont donc précédés d'un `systemctl reset-failed` — un no-op sur une unité saine.

Sur un chemin de rollback, un démarrage refusé n'est plus un avertissement mais
une **indisponibilité** : `update.sh` sort en erreur avec un bloc
`[INDISPONIBILITÉ]` qui rappelle que la gateway est à terre et donne la
commande de rétablissement. La restauration (mode, code, venv, unité) est
toujours menée à son terme **avant** que l'échec soit signalé.

```
[INDISPONIBILITÉ] Rollback vers /var/lib/llm-gateway/backups/code-pre-update-… : démarrage refusé par systemd.
[INDISPONIBILITÉ] llm-gateway n'a PAS redémarré : la gateway est À TERRE.
  Rétablissement manuel immédiat :
    sudo systemctl reset-failed llm-gateway
    sudo systemctl start llm-gateway
```

Sur le chemin **nominal**, un démarrage en échec reste délibérément non fatal :
la sonde `/ready` qui suit enchaîne sur le rollback, qu'un arrêt immédiat
court-circuiterait.

Le rollback **ne restaure pas** la base de données automatiquement : le schéma
peut avoir évolué et écraser la base sans arbitrage humain serait plus risqué
qu'utile. La sauvegarde `gateway-pre-update-*.db` reste disponible pour une
restauration manuelle (voir [Sauvegardes SQLite](#14-sauvegardes-sqlite-et-restauration)).

### Mettre à jour llama.cpp

```bash
cd /opt/llama.cpp-src
sudo git pull --ff-only

sudo cmake --build build --config Release -j$(nproc)
sudo install -d -m 0755 /opt/llama.cpp/releases/<nouveau-bNNNNN>
sudo install -m 0755 build/bin/llama-server \
  /opt/llama.cpp/releases/<nouveau-bNNNNN>/llama-server
sudo ln -sfn releases/<nouveau-bNNNNN> /opt/llama.cpp/.current-new
sudo mv -Tf /opt/llama.cpp/.current-new /opt/llama.cpp/current

# Mettre LLAMA_SERVER_MIN_BUILD au premier build corrigé connu, puis valider.
sudoedit /etc/llm-gateway/env
sudo systemctl restart llm-gateway
sudo /opt/llm-gateway/venv/bin/python /opt/llm-gateway/cli.py doctor \
  --env-file /etc/llm-gateway/env --strict
```

#### Politique de version (mitigation supply-chain)

`llama-server` a fait l'objet de CVEs 2025-2026 (écriture hors-bornes non
authentifiée via `n_discard`/context-shift — GHSA-8947-pfff-2f3c —, overflows de
parsing GGUF menant au RCE). Suivez les advisories llama.cpp et, après avoir
installé un build patché, épinglez-le :

```bash
# Vérifier le build installé
/opt/llama.cpp/current/llama-server --version   # → version: <build> (<hash>)

# Fixer le minimum accepté dans /etc/llm-gateway/env (et sur chaque node-agent)
LLAMA_SERVER_MIN_BUILD=<build_patché>
```

Au démarrage, la gateway (et chaque node-agent) sonde `llama-server --version` :

- build lu `<` `LLAMA_SERVER_MIN_BUILD` (si > 0) → **démarrage refusé** (log critical) ;
- version illisible / binaire absent **et** `LLAMA_SERVER_MIN_BUILD > 0` →
  **démarrage refusé** (log critical). Politique fail-closed (SEC-009) : un
  binaire qui ne sait pas dire ce qu'il est ne peut pas être attesté patché, et
  `doctor` refusait déjà dans ce cas. Les deux chemins rendent le même verdict ;
- version illisible / binaire absent **et** `LLAMA_SERVER_MIN_BUILD=0` → simple
  avertissement, démarrage poursuivi ;
- `LLAMA_SERVER_MIN_BUILD=0` (défaut) → aucun enforcement.

> Le durcissement inclut aussi l'absence délibérée du flag `--context-shift`
> (vecteur de la CVE `n_discard`) dans la commande de lancement.

Cette valeur est le **plancher de sécurité** — le premier build corrigé connu —
et non le build effectivement installé : exiger le build épinglé reviendrait à
refuser de démarrer après toute mise à jour manuelle vers un build plus récent.
C'est la même grandeur que l'option `--min-build` de
[`bootstrap-plan`](admin.md#9-planificateur-damorçage--bootstrap-plan), qui la
dérive de la politique de release et refuse une politique épinglant un build
inférieur à son propre plancher.

### Ajouter ou modifier des modèles

Les modèles se gèrent via `models.yaml` — aucun redémarrage requis si vous utilisez l'API REST.

**Via models.yaml (redémarrage requis) :**

```bash
# 1. Télécharger le fichier .gguf
huggingface-cli download bartowski/Qwen2.5-32B-Instruct-GGUF \
  --include "*Q4_K_M*" --local-dir /models/

# 2. Ajouter l'entrée dans le registre
sudoedit /var/lib/llm-gateway/models.yaml

# 3. Redémarrer
sudo systemctl restart llm-gateway
```

**Via API REST (sans redémarrage) :**

```bash
export ADMIN_SECRET=$(sudo grep ADMIN_SECRET /etc/llm-gateway/env | cut -d= -f2)

# Enregistrer un nouveau modèle
curl -s -X POST "https://gateway.example.com/admin/models" \
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
curl -s -X PATCH "https://gateway.example.com/admin/models/llama-3.1-8b-instruct" \
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
| `ValueError: model_id invalide` | ID dans models.yaml non conforme | L'ID doit correspondre à `^[a-z0-9][a-z0-9._-]{0,62}$` |

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
| HTTP 500 sur requête avec image | `mmproj_path` absent dans `models.yaml` | Ajouter `mmproj_path` pointant vers le fichier projecteur CLIP (`.gguf`) |
| Warning `mmproj_path absent` dans les logs | Modèle vision sans projecteur | Télécharger le fichier `mmproj` sur HuggingFace et configurer `mmproj_path` |
| Génération très lente, TTFT de plusieurs dizaines de secondes, **aucune erreur** ; `pgmajfault` grimpe dans `/sys/fs/cgroup/system.slice/llm-gateway.service/memory.stat` ; disque à 100 % | Le working set RAM hôte d'un modèle `cpu_moe: true` dépasse `MemoryHigh`/`MemoryMax` (ou la RAM installée) : le noyau recycle en boucle les pages `mmap` propres des experts FFN → refault NVMe à chaque token | Désactiver le modèle ou l'exécuter sur un hôte plus grand. Vérifier la table de dimensionnement [§15.1](#151-couple-modelsyaml--limites-mémoire-systemd). Ne pas se contenter de relever `MemoryMax` au-delà de la RAM physique |
| `Failed to set up mount namespacing`, l'unité ne démarre pas | Un chemin de `ReadWritePaths` n'existe pas (ex. `/data/models` non provisionné) | Créer le répertoire, ou le préfixer par `-` dans l'unité (déjà fait pour les répertoires de modèles) |

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

---

## 13. Déploiement multi-nœuds (Optionnel — avancé)

> Le multi-nœud est une fonctionnalité **opt-in**. Le mode local reste le
> défaut. Cette section s'adresse aux opérateurs souhaitant piloter plusieurs
> hôtes GPU.

### Architecture

```
Client OpenAI-compatible
        │ HTTPS public (nginx)
        ▼
┌────────────────────────────────┐
│   Orchestrateur (cette gateway) │
│   ClusterManager               │
└─────────────┬──────────────────┘
              │ HTTPS :9443 (Bearer agent_secret)
    ┌─────────┴─────────┐
    ▼                   ▼
┌──────────┐     ┌──────────┐
│ Agent A  │     │ Agent B  │
│ Nœud GPU │     │ Nœud GPU │
└────┬─────┘     └────┬─────┘
     ▼                ▼
 llama-server    llama-server
```

Deux flux séparés :
- **Plan de contrôle** : orchestrateur ↔ agent (load/unload/health) — HTTPS
- **Plan de données** : orchestrateur ↔ llama-server (inférence SSE) — HTTP interne LAN

### Pré-requis réseau

- Ports ouverts sur chaque nœud :
  - `9443` (TCP) — agent, accessible uniquement depuis l'orchestrateur
  - `8081-8085` (TCP) — llama-server, accessible uniquement depuis l'orchestrateur
- L'orchestrateur expose seulement `443` publiquement via nginx; son port
  FastAPI `8000` reste sur `127.0.0.1`.
- Autoriser **les deux flux** depuis l'IP orchestrateur et rien d'autre :

```bash
sudo ufw allow from <IP_orchestrateur> to any port 9443 proto tcp
sudo ufw allow from <IP_orchestrateur> to any port 8081:8085 proto tcp
```

### Installation — orchestrateur

```bash
# Sur la machine orchestrateur :
bash gateway/deploy/install.sh --mode cluster --dry-run
sudo bash gateway/deploy/install.sh --mode cluster
# → unité systemd orchestrateur sans GPU ni /models local
# → crée AGENT_SECRET une seule fois s'il est absent
# → crée nodes.yaml uniquement s'il est absent

# Éditer la topologie des nœuds
sudo nano /etc/llm-gateway/nodes.yaml
```

### Installation — chaque nœud GPU

Voir [la procédure de compilation CUDA](#2-installation-de-llamacpp) pour
compiler `llama-server` sur chaque nœud.

```bash
# Depuis l'orchestrateur, transférer le secret via stdin (jamais en argv/log) :
sudo awk -F= '$1 == "AGENT_SECRET" {sub(/^[^=]*=/, ""); print; exit}' \
  /etc/llm-gateway/env | \
  ssh root@gpu-node-a 'umask 077; cat > /root/evaruntime-agent-secret'

# Sur chaque nœud GPU (répéter pour gpu-node-a et gpu-node-b) :
git clone https://github.com/Tutanka01/EVARuntime.git /opt/llm-gateway-src
cd /opt/llm-gateway-src

sudo bash node_agent/deploy/install-agent.sh \
  --node-id gpu-node-a \
  --llama-min-build <premier_build_corrige> \
  --agent-secret-file /root/evaruntime-agent-secret \
  --orchestrator-cidr <IP_orchestrateur>/32
# → crée /etc/llm-gateway-agent/env, génère le certificat TLS, installe le service

sudo rm /root/evaruntime-agent-secret  # après validation de l'installation

sudo systemctl start llm-gateway-agent
sudo journalctl -fu llm-gateway-agent
```

Le plan de données doit être joignable depuis l'orchestrateur : chaque agent
doit lancer `llama-server` sur son adresse réseau interne (pas `127.0.0.1`) et
le firewall doit limiter `8081-8085` à l'IP orchestrateur.
L'installateur exige un build minimal positif et sonde le binaire canonique
`/opt/llama.cpp/current/llama-server` avant d'activer l'unité. Un autre chemin
doit être déclaré explicitement avec `--llama-server-bin` ; un build illisible
ou inférieur au plancher fait échouer le préflight.

### Registre et fichiers de modèles partagés

Le registre `/var/lib/llm-gateway/models.yaml` vit sur l'orchestrateur, mais les
GGUF ne sont pas transférés par EVARuntime. Pour tout modèle susceptible d'être
placé sur plusieurs nœuds, copier le GGUF, son `mmproj` éventuel et les mêmes
permissions **au même chemin absolu sur chacun de ces nœuds**. Un stockage
partagé monté au même chemin convient aussi, après validation de son débit et de
son comportement en panne. Valider le SHA-256 avant d'activer le modèle.

> **Fail-closed :** un `AGENT_SECRET` vide ou laissé à sa valeur d'exemple
> (`CHANGE_ME_*`) fait refuser toutes les requêtes par l'agent (503), et la
> gateway refuse de démarrer en `CLUSTER_MODE=cluster` tant que ce secret
> n'est pas défini.

### Configuration TLS (certificats auto-signés)

Si vous utilisez des certificats auto-signés générés par `install-agent.sh` :

```bash
# Sur l'orchestrateur — récupérer les certificats des deux nœuds
scp gpu-node-a:/etc/llm-gateway-agent/tls/agent.crt /etc/ssl/certs/gpu-node-a.crt
scp gpu-node-b:/etc/llm-gateway-agent/tls/agent.crt /etc/ssl/certs/gpu-node-b.crt

# Créer le bundle CA
cat /etc/ssl/certs/gpu-node-a.crt \
    /etc/ssl/certs/gpu-node-b.crt \
  > /etc/ssl/certs/llm-gateway-nodes-ca.pem

# Pointer nodes.yaml vers ce bundle :
#   cluster:
#     tls_verify: /etc/ssl/certs/llm-gateway-nodes-ca.pem
sudo nano /etc/llm-gateway/nodes.yaml
```

### Démarrage et vérification

```bash
# Démarrer l'orchestrateur
sudo systemctl start llm-gateway

# Vérifier les nœuds (les deux doivent être online)
curl -s -H "Authorization: Bearer $ADMIN_SECRET" \
  http://localhost:8000/admin/cluster | python3 -m json.tool

# Charger un modèle (le scheduler choisit le meilleur nœud automatiquement)
curl -s -X POST -H "Authorization: Bearer $ADMIN_SECRET" \
  http://localhost:8000/admin/models/llama-3.3-70b-instruct/load

# Vérifier le placement
curl -s -H "Authorization: Bearer $ADMIN_SECRET" \
  http://localhost:8000/admin/cluster | python3 -m json.tool
```

### Comportement en cas de panne

- **Nœud injoignable** : après 3 heartbeats échoués (~30 s), le nœud passe
  `offline`. Les modèles qui y étaient deviennent `unavailable`.
- **Requêtes en cours** : reçoivent une erreur 502 si le nœud s'arrête pendant
  le stream. Les requêtes futures sont routées vers les nœuds disponibles.
- **Retour du nœud** : dès que le heartbeat répond, le nœud repasse `online`.
  L'orchestrateur **ne recharge pas automatiquement** les modèles — rechargement
  manuel via `POST /admin/models/{id}/load` ou à la prochaine requête utilisateur.
- **Tous les nœuds offline** : les requêtes d'inférence reçoivent une 503 explicite.

### Mise à jour orchestrateur et agents

Avant la première mise à jour d'un agent installé avec l'ancien défaut
`LLAMA_SERVER_MIN_BUILD=0`, attestez le runtime puis relevez explicitement son
plancher dans `/etc/llm-gateway-agent/env`. Le préflight de la nouvelle release
refuse une valeur nulle, un binaire illisible ou un build trop ancien **avant**
de toucher au service :

```bash
/opt/llama.cpp/current/llama-server --version
sudoedit /etc/llm-gateway-agent/env
# LLAMA_SERVER_BIN=/opt/llama.cpp/current/llama-server
# LLAMA_SERVER_MIN_BUILD=<premier_build_corrige>
```

```bash
# 1. Orchestrateur uniquement
sudo bash gateway/deploy/update.sh --mode cluster

# 2. Sur CHAQUE nœud, séparément
sudo bash node_agent/deploy/update-agent.sh

# 3. Gate production après retour des nœuds
curl -fsS http://127.0.0.1:8000/ready
sudo bash /opt/llm-gateway/deploy/smoke_test.sh
```

La recette du premier token fonctionne à l'identique en mode cluster : le
chargement explicite du modèle est délégué au node-agent qui l'héberge, et le
verdict porte sur le même chemin public. `/ready` y délègue binaire et GGUF aux
nœuds — raison de plus pour ne pas s'en contenter.

Mettre les agents à jour un par un et attendre leur retour `online` avant de
continuer maintient la capacité si le cluster possède au moins un autre nœud
capable d'héberger les modèles. L'orchestrateur ne fait aucune mise à jour SSH
ou distante implicite.

#### Stratégie de venv du node-agent

`update-agent.sh` applique la même stratégie que `update.sh` : le venv neuf est
construit **à son emplacement définitif**
(`/opt/llm-gateway/venv-agent-release-<horodatage>-<pid>`), puis le symlink
`/opt/llm-gateway/venv-agent` — le chemin figé dans l'unité systemd — est
basculé d'une release à l'autre. Aucun venv n'est jamais déplacé.

Un venv **n'est pas relogeable** : les scripts console de `bin/` portent un
shebang absolu vers `<venv>/bin/python`. La version précédente construisait le
venv dans un staging `mktemp` puis le déplaçait, ce qui laissait `bin/uvicorn`
pointer vers un staging root-only puis supprimé : `203/EXEC Permission denied`,
cinq health-checks en échec et rollback systématique — aucune mise à jour de
node-agent ne pouvait aboutir. `ExecStartPre` passait, lui, car `bin/python` est
un lien vers l'interpréteur système : le symptôme désignait le mauvais coupable.

Conséquences pratiques :

- **Agent installé avant ce correctif** : la première mise à jour migre le venv
  en place. Le venv réel est écarté une seule fois vers
  `venv-agent-pre-update-<horodatage>` et le symlink est créé. Aucune action
  opérateur, et les mises à jour suivantes ne repassent plus par ce cas.
- **Rollback** : le symlink est rebasculé vers la release précédente, restée
  intacte à son emplacement. Un rollback ne déplace donc aucun venv non plus.
- **Retour arrière manuel** possible tant que la release est conservée :

```bash
ls -dt /opt/llm-gateway/venv-agent-release-*
sudo ln -sfn /opt/llm-gateway/venv-agent-release-<…> /opt/llm-gateway/venv-agent
sudo systemctl restart llm-gateway-agent
```

- L'unité conserve `ExecStart=…/venv-agent/bin/python -m uvicorn` : défense en
  profondeur validée en production, insensible par construction au shebang.

**Rétention des releases.** Puisque aucun venv n'est plus écrasé, chaque mise à
jour laisse une arborescence complète (~200 Mo) sur le disque du nœud, plus un
éventuel `venv-agent-pre-update-*` issu de la migration. `update-agent.sh` purge
donc les releases excédentaires **après une mise à jour réussie**, en conservant
**l'active et la précédente** — soit exactement ce que réclame le retour arrière
manuel ci-dessus. La politique est la même que celle de la gateway :

- La cible courante du symlink n'est **jamais** supprimée, quel que soit son
  âge : c'est le venv sur lequel l'agent tourne. Après un retour arrière manuel
  vers une vieille release, celle-ci est donc protégée bien qu'elle soit la plus
  ancienne du répertoire.
- L'ordre de purge est celui des **dates de modification**, pas des noms.
- Le nombre de releases conservées se règle par `EVA_AGENT_VENV_KEEP`
  (défaut : `2`, minimum : `1`).
- `--dry-run` annonce ce que la purge emporterait, sans rien supprimer.
- La purge n'a lieu **qu'après** la validation (health-check passé, rollback
  désarmé). Avant, la release précédente est encore ce vers quoi le rollback
  rebascule — un rollback ne doit jamais trouver une release supprimée.
- Un échec de purge n'échoue **jamais** la mise à jour : l'agent est en service,
  seul l'espace disque manque. Le script le signale en avertissement.

Vérifier ce qui reste sur un nœud :

```bash
du -sh /opt/llm-gateway/venv-agent-release-* /opt/llm-gateway/venv-agent-pre-update-* 2>/dev/null
readlink -f /opt/llm-gateway/venv-agent
sudo bash node_agent/deploy/update-agent.sh --dry-run --no-pull   # annonce la purge
```

#### Start-limit systemd et codes de sortie de `update-agent.sh`

Après plusieurs redémarrages rapprochés, systemd refuse de démarrer une unité
(« Start request repeated too quickly ») et la laisse en `failed`. Un rollback
qui se contente de `systemctl start` laisse alors le **nœud à terre**. Chaque
`systemctl start` des scripts de déploiement node-agent est donc précédé d'un
`systemctl reset-failed` — no-op sur une unité saine, donc sans risque —, y
compris dans le chemin de rollback et après une réinstallation.

Codes de sortie de `update-agent.sh` :

| Code | Signification | Action |
|------|---------------|--------|
| `0` | Mise à jour déployée et health-check passé | aucune |
| `1` | Échec : version précédente restaurée, agent **en service** | investiguer avant de réessayer |
| `9` | **INDISPONIBILITÉ** : rollback effectué mais l'agent ne redémarre pas | intervention immédiate |

Un `9` n'est jamais un simple avertissement : le nœud ne sert plus aucune
requête et l'orchestrateur le passera `offline` après ~30 s.

```bash
sudo systemctl reset-failed llm-gateway-agent
sudo systemctl start llm-gateway-agent
sudo journalctl -u llm-gateway-agent -n 100 --no-pager
```

### Migration explicite local ↔ cluster

Une migration n'est jamais déduite d'un simple rerun. Elle exige `--mode` et
`--allow-mode-change`.

**Local vers cluster :**

1. Installer les node-agents, synchroniser GGUF/mmproj et partager
   `AGENT_SECRET`.
2. Préparer `nodes.yaml`, le bundle CA et les règles `9443`/`8081-8085`.
3. Décharger/drainer les requêtes locales pendant une fenêtre de maintenance.
4. Exécuter :

```bash
bash gateway/deploy/update.sh --mode cluster --dry-run
sudo bash gateway/deploy/update.sh --mode cluster --allow-mode-change
curl -fsS http://127.0.0.1:8000/ready
```

Si `nodes.yaml` ou son bundle CA est invalide, le script restaure le mode local
avant le redémarrage. Si la readiness échoue après bascule, il restaure code,
venv, `CLUSTER_MODE` et unité systemd locaux.

**Cluster vers local :** préinstaller d'abord un `llama-server` CUDA fonctionnel
sur l'orchestrateur, placer les GGUF sous les chemins du registre, dimensionner
la VRAM locale et valider `nvidia-smi`. Puis :

```bash
bash gateway/deploy/update.sh --mode local --dry-run
sudo bash gateway/deploy/update.sh --mode local --allow-mode-change
curl -fsS http://127.0.0.1:8000/ready
```

La bascule ne supprime jamais `nodes.yaml`, `AGENT_SECRET` ni les services agents;
ils restent disponibles pour un retour contrôlé. Après validation locale,
arrêter les agents devenus inutiles selon la politique d'exploitation.

### Vérification de la rétrocompatibilité (déploiement local existant)

```bash
# Sur une installation mono-nœud existante :
bash gateway/deploy/update.sh --dry-run
sudo bash gateway/deploy/update.sh
# Le mode local est auto-détecté et conservé; aucun flag de migration requis.
```

---

## 14. Sauvegardes SQLite et restauration

La base `gateway.db` contient les utilisateurs, clés API (hashées), quotas et
logs d'usage. Deux mécanismes de sauvegarde coexistent :

1. **Ponctuelle** : `update.sh` crée `gateway-pre-update-*.db` avant chaque
   mise à jour (voir [section 11](#11-mise-à-jour)).
2. **Périodique** : une unité systemd `timer` déclenche une sauvegarde
   quotidienne avec rotation.

Toutes les sauvegardes utilisent `sqlite3 .backup` (cohérent sur une base **WAL
active**) et vivent dans `/var/lib/llm-gateway/backups/` (permissions `600`,
propriétaire `llmservice`).

### Installation du timer de sauvegarde quotidienne

**Automatique.** `install.sh` déploie le script + les unités et **arme** le timer
avec `enable --now` (si `sqlite3` est présent). `update.sh` rafraîchit ensuite
ces fichiers à chaque mise à jour, et arme le timer la première fois. Un timer
que vous auriez volontairement désactivé (`systemctl disable`) n'est jamais
réactivé automatiquement. Il n'y a donc normalement **rien à faire
manuellement**.

> **OPS-008 — `--now` est indispensable.** Un `systemctl enable` seul crée le
> lien dans `timers.target` mais laisse l'unité `inactive` : elle n'apparaît pas
> dans `list-timers` et ne déclenche **aucune** sauvegarde jusqu'au prochain
> reboot. Sur un serveur qui ne redémarre pas, cela signifiait zéro sauvegarde
> périodique, sans le moindre message. `update.sh` répare aussi cet état sur les
> hôtes déjà installés : un timer `enabled` mais `inactive` est armé.
>
> Dans `install.sh`, l'armement suit délibérément l'**initialisation de la
> base** (étape 9). Sur une installation neuve, ce premier `start` ne déclenche
> aucune sauvegarde (sans stamp préexistant, systemd pose le stamp sans exécuter
> le job) ; sur une réinstallation avec un stamp périmé, `Persistent=true` peut
> en revanche rattraper l'occurrence manquée immédiatement — elle trouve alors
> une base déjà initialisée.

Vérifier / tester :

```bash
systemctl is-active llm-gateway-backup.timer      # doit répondre « active »
systemctl list-timers llm-gateway-backup.timer    # doit lister la prochaine échéance
sudo systemctl start llm-gateway-backup.service   # test manuel immédiat
journalctl -u llm-gateway-backup.service --no-pager -n 20
```

Installation ou réactivation manuelle (si besoin) :

```bash
sudo cp gateway/deploy/llm-gateway-backup.sh /opt/llm-gateway/deploy/
sudo chmod 750 /opt/llm-gateway/deploy/llm-gateway-backup.sh
sudo cp gateway/deploy/llm-gateway-backup.service /etc/systemd/system/
sudo cp gateway/deploy/llm-gateway-backup.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now llm-gateway-backup.timer
```

### Paramètres

Le script lit les variables d'environnement (surchargées par
`/etc/llm-gateway/env` si présentes) :

| Variable | Défaut | Rôle |
|----------|--------|------|
| `DB_PATH` | `/var/lib/llm-gateway/gateway.db` | Base source |
| `BACKUP_DIR` | `/var/lib/llm-gateway/backups` | Destination |
| `BACKUP_RETENTION_DAYS` | `14` | Rotation : suppression au-delà de N jours |

Après chaque sauvegarde, un `PRAGMA integrity_check` est exécuté ; une copie
corrompue est supprimée et le job échoue (visible dans `journalctl`).

### Restauration depuis une sauvegarde `.backup`

Un fichier `.backup` est une base SQLite complète et autonome — la restauration
est une simple copie, service arrêté :

```bash
# 1. Arrêter la gateway (libère les verrous WAL)
sudo systemctl stop llm-gateway

# 2. (Prudence) archiver la base actuelle avant de l'écraser
sudo mv /var/lib/llm-gateway/gateway.db \
        /var/lib/llm-gateway/gateway.db.corrompue-$(date +%Y%m%d-%H%M%S)

# 3. Restaurer la sauvegarde choisie
sudo cp /var/lib/llm-gateway/backups/gateway-20260706-031500.db \
        /var/lib/llm-gateway/gateway.db

# 4. Rétablir propriétaire/permissions
sudo chown llmservice:llmservice /var/lib/llm-gateway/gateway.db
sudo chmod 640 /var/lib/llm-gateway/gateway.db

# 5. Redémarrer
sudo systemctl start llm-gateway
curl http://127.0.0.1:8000/health
```

> **Note WAL :** un `.backup` est déjà consolidé (pas de `-wal`/`-shm` à copier).
> Supprimer d'éventuels fichiers `gateway.db-wal`/`gateway.db-shm` résiduels
> **avant** l'étape 3 si la base courante était corrompue.

---

## 15. Durcissement systemd et profils mémoire

`gateway/deploy/llm-gateway.service` applique le durcissement systemd maximal,
**à l'exception des directives incompatibles avec l'accès GPU**.
Directives ajoutées : `ProtectKernelTunables`, `ProtectKernelModules`,
`ProtectControlGroups`, `ProtectClock`, `ProtectHostname`, `RestrictNamespaces`,
`RestrictRealtime`, `RestrictSUIDSGID`, `LockPersonality`,
`SystemCallArchitectures=native`, `SystemCallFilter=@system-service` (moins
`@privileged @resources @mount @reboot @swap @debug`), et des limites
`MemoryHigh`/`MemoryMax`/`MemorySwapMax`/`OOMPolicy`/`TasksMax` — ces dernières
étant **dérivées du profil de modèles activés** ([§15.1](#151-couple-modelsyaml--limites-mémoire-systemd)).

### Directives volontairement OMISES (casseraient le GPU)

| Directive | Pourquoi omise |
|-----------|----------------|
| `PrivateDevices=true` | Masquerait `/dev/nvidia*` et `/dev/dri/*` → CUDA indisponible pour les sous-processus `llama-server`. (À réserver aux unités qui ne touchent jamais au GPU, comme l'orchestrateur cluster.) |
| `MemoryDenyWriteExecute=true` | Casse le JIT (CUDA PTX/JIT, certains chemins Python/allocateurs). |
| `CapabilityBoundingSet=` (vide) | Non nécessaire au GPU (l'accès passe par les nœuds devices + groupes `render,video`), laissé au défaut pour rester conservateur. |

### Directives à VALIDER en staging avant prod

- **`DevicePolicy=closed` + `DeviceAllow=...`** : restreint explicitement l'accès
  aux nœuds devices NVIDIA/DRI usuels. Si votre pilote expose des nœuds
  supplémentaires (`nvidia-caps`, MIG…), les ajouter ; en cas de doute,
  **commenter tout le bloc** pour revenir au comportement par défaut (accès régi
  par l'appartenance aux groupes `render,video`, cf. `install.sh`).
- Les **limites mémoire** ne sont plus des valeurs de départ arbitraires : elles
  sont dérivées du profil de modèles activés et documentées en
  [§15.1](#151-couple-modelsyaml--limites-mémoire-systemd).

> Après modification, valider avec `systemd-analyze verify
> /etc/systemd/system/llm-gateway.service` puis un cycle
> `stop`/`start`/inférence de bout en bout **en staging** avant la prod.

### Conflits durcissement ↔ besoins des modèles (conservés, documentés)

Ces points sont laissés **durcis** volontairement. Ils sont listés pour que
personne ne les découvre en production.

| Directive | Conflit potentiel | Décision |
|-----------|-------------------|----------|
| `SystemCallFilter=~@resources` | Bloque `set_mempolicy`, `mbind`, `migrate_pages`, `move_pages`, `sched_setaffinity`. `llama-server` n'est **pas** lancé avec `--numa` (cf. `build_llama_cmd`), donc aucun modèle approuvé n'est bloqué aujourd'hui. Sur un hôte bi-socket, `--numa distribute` améliorerait pourtant nettement les modèles `cpu_moe`. | Filtre **conservé**. Activer `--numa` exige de lever `~@resources` : décision de sécurité explicite, à valider en staging (le refus d'un appel filtré termine le processus par `SIGSYS`, il ne renvoie pas une erreur). |
| `LimitMEMLOCK` (défaut, 8 Mo) | `--mlock` verrouillerait les poids en RAM. | `--mlock` **n'est pas utilisé** : le GGUF est lu via `mmap`, ses pages restent réclamables, ce qui est exactement ce qui évite l'OOM-kill. Ne pas relever `LimitMEMLOCK` « au cas où » : avec `--mlock`, `MemoryMax` redevient un plafond dur et le modèle entier doit tenir en RAM. Un test (`test_local_unit_does_not_grant_memlock`) garde cette décision. |
| `ProtectSystem=strict` + `ReadWritePaths` | Un répertoire listé mais **absent** empêche l'unité de démarrer. | Les répertoires de modèles sont préfixés `-` (`-/models -/data/models`) : ignorés s'ils n'existent pas. `install.sh` ne crée que `/models`. Tout nouveau répertoire déclaré dans `models.yaml` doit être ajouté ici. |
| `PrivateDevices` / `MemoryDenyWriteExecute` | Casseraient CUDA et le JIT. | Omises (cf. table précédente), inchangé. |

### 15.1 Couple `models.yaml` × limites mémoire systemd

C'est le point de dimensionnement le plus souvent manqué, parce qu'il ne produit
**aucune erreur** quand il est faux : seulement un effondrement du TTFT.

**Pourquoi les limites du service concernent les modèles.** `llama-server` n'est
pas un service séparé : c'est un **sous-processus enfant** de la gateway (mode
local) ou du node-agent (mode cluster). Il vit donc dans le **même cgroup**, et
toute sa mémoire — y compris le page cache de son `mmap` GGUF — est comptée dans
le `MemoryHigh`/`MemoryMax` de l'unité.

**Où réside quoi.**

| Ce qui est chargé | Emplacement | Piloté par |
|-------------------|-------------|-----------|
| Poids des couches offloadées | **VRAM** | `n_gpu_layers` |
| KV cache | **VRAM** (le KV suit les couches, qui sont sur GPU) | `ctx_size`, `parallel`, `cache_type_k/v` |
| Poids des experts FFN d'un MoE `cpu_moe: true` | **RAM hôte, résidents** — relus depuis le `mmap` à chaque token | `cpu_moe` |
| Page cache du GGUF pendant le chargement | **RAM hôte, transitoire** (pages propres, jetables ensuite) | taille du fichier |
| Processus (gateway + RSS de chaque `llama-server`) | RAM hôte | `batch_size`, `ubatch_size`, `threads_http` |

**La nuance qui compte.** Une limite cgroup inférieure au working set d'un modèle
`cpu_moe` ne provoque pas forcément d'OOM-kill : les pages `mmap` **propres** sont
réclamables, le noyau les recycle au lieu de tuer le processus. Ce qui est
certain, c'est le **thrashing NVMe** — chaque token refaute des pages d'experts —
donc un TTFT et un débit effondrés, silencieusement, y compris pour les autres
modèles chargés. Autrement dit : `MemoryMax` ne protège pas contre ce cas, il le
provoque. La limite doit être **dérivée du profil de modèles activés**.

**Politique retenue** (`llm-gateway.service` et `node_agent/deploy/llm-gateway-agent.service`) :

```ini
MemoryHigh=80%      # pression douce (reclaim/throttle) — adapté à des pages mmap réclamables
MemoryMax=90%       # garde-fou dur contre une fuite de mémoire ANONYME uniquement
MemorySwapMax=0     # ne jamais swapper : refault d'une page propre ≪ swap-in d'une page anonyme
OOMPolicy=stop      # un llama-server tué ⇒ arrêt de l'unité, Restart=on-failure repart propre
TasksMax=4096       # ~64 tâches par llama-server × MAX_LOADED_MODELS=5
```

> **Prérequis :** les valeurs en pourcentage et `MemorySwapMax=` exigent
> **cgroup v2** (hiérarchie unifiée) et systemd ≥ 231 — c'est le défaut sur Ubuntu
> 22.04 et 24.04. Vérifier avec `stat -fc %T /sys/fs/cgroup` → `cgroup2fs`. Sur un
> hôte encore en cgroup v1, remplacer les pourcentages par les valeurs absolues
> déduites de la table ci-dessous et documenter l'écart.

Les pourcentages sont relatifs à la RAM physique de l'hôte. Ils sont préférés aux
valeurs absolues parce que l'unité systemd est un **artefact versionné** alors que
le dimensionnement mémoire est une **propriété de l'hôte** : `MemoryMax=64G`
codait en dur l'hypothèse d'un hôte donné, et cette hypothèse a dérivé dès qu'un
modèle de 248 Go est entré dans le registre.

**Règle d'arbitrage :** un profil de modèles qui ne tient pas dans 80 % de la RAM
installée se refuse **dans `models.yaml`** (`enabled: false`). Il ne s'absorbe pas
en relevant la limite systemd au-delà de la RAM réellement présente.

#### Table de dimensionnement — modèle → RAM hôte → VRAM

Hôte de référence : **GPU 48 Go / 128 Go de RAM** (mono-socket, GGUF sur NVMe).

| Modèle | `cpu_moe` | GGUF | VRAM requise | RAM hôte **résidente** | RAM hôte **transitoire** (chargement) | RAM physique minimale (`MemoryHigh=80 %`) | Activé par défaut | Verdict |
|--------|-----------|------|--------------|------------------------|----------------------------------------|-------------------------------------------|-------------------|---------|
| `llama-3.3-70b-instruct` | non | ~42.5 Go | 42.0 Go | ~1.5 Go | ~42.5 Go (propre, jetable) | 8 Go (64 Go recommandés pour un chargement rapide) | oui | ✅ compatible |
| `gemma-4-26b-a4b` | non | ~16 Go | 26.9 Go | ~1.5 Go | ~16 Go | 8 Go | oui | ✅ compatible |
| `qwen3.5-9b-q5_k_m` | **oui** | ~19 Go | 7.0 Go | **~16 Go** | ~19 Go | 24 Go | oui | ✅ compatible |
| `llama-3.1-8b-instruct` | non | ~4.9 Go | 5.5 Go | ~0.7 Go | ~4.9 Go | 4 Go | non (GGUF absent) | ✅ compatible si activé |
| `minimax-m2.7` | **oui** | **~248 Go** (4 shards) | 32.0 Go | **~236 Go** | ~248 Go | **~320 Go** | **non** | ❌ **incompatible** avec l'hôte de référence |
| **Profil activé complet** | — | — | ≤ 43.6 Go (borné par l'éviction LRU) | ~19 Go | — | 32 Go (64 Go recommandés, 128 Go = référence) | — | ✅ |

> **Niveau de preuve.** Ces valeurs sont des **estimations d'architecture**, pas
> des mesures : tailles de GGUF déclarées (amont / registre), part des experts
> déduite de la quantisation et du ratio paramètres actifs/totaux, surcoût de
> processus estimé. Elles sont volontairement conservatrices. À remplacer par des
> mesures `nvidia-smi` + `memory.peak` du cgroup dès que la calibration
> (backlog AUT-008 / PERF-006) existe. La colonne « RAM hôte résidente » est
> celle qui décide de la compatibilité ; la colonne « transitoire » n'influence
> que le temps de chargement.

#### Cas MiniMax M2.7 — traité explicitement

`minimax-m2.7` est **désactivé par défaut** dans `gateway/models.yaml`. Ce n'est
pas une contrainte de VRAM (32 Go déclarés, cela tient dans le budget GPU de
référence) mais de RAM
hôte : avec `cpu_moe: true`, les ~248 Go d'experts FFN doivent rester résidents.
Sur l'hôte de référence à 128 Go, aucune valeur de `MemoryMax` ne rend ce modèle
utilisable — l'ancienne valeur `MemoryMax=64G` garantissait le thrashing, et la
retirer ne ferait que déplacer le problème sur la RAM physique.

Pour l'activer :

1. hôte avec **≥ 320 Go de RAM** (working set ~236 Go sous `MemoryHigh=80 %`) et
   ~250 Go d'espace NVMe rapide ;
2. `enabled: true` dans `models.yaml` (ou `POST /admin/models/{id}/enable`) ;
3. mettre à jour la table ci-dessus **et** le profil correspondant dans
   `gateway/tests/test_deploy_memory_profiles.py` (le test échoue sinon) ;
4. vérifier après chargement : `systemctl show llm-gateway -p MemoryCurrent` et
   `cat /sys/fs/cgroup/system.slice/llm-gateway.service/memory.stat` (regarder
   `file`, `pgmajfault` : une croissance continue de `pgmajfault` en génération
   signe le thrashing) ;
5. mesurer le TTFT réel avant d'ouvrir le modèle aux utilisateurs.

#### Procédure — activer un nouveau modèle

À suivre **avant** de passer `enabled: true`, en local comme en cluster :

1. **Mesurer le fichier** : `ls -lh` sur le GGUF (tous les shards).
2. **Classer le modèle** : `cpu_moe: true` → la quasi-totalité du fichier sera
   résidente en RAM hôte ; sinon → seule la VRAM est dimensionnante.
3. **Comparer à la RAM installée** : `free -g`. Le working set + ~2 Go de
   baseline doit tenir dans **80 %** de la RAM (valeur de `MemoryHigh`).
4. **Vérifier le répertoire** : si le GGUF n'est pas sous un chemin déjà listé
   dans `ReadWritePaths` de l'unité, l'ajouter (préfixé `-`) et vérifier
   `ALLOWED_MODEL_DIRS`.
5. **Mettre à jour** la table de cette section et `MEMORY_PROFILES` dans
   `gateway/tests/test_deploy_memory_profiles.py`, puis
   `cd gateway && python -m pytest tests/test_deploy_memory_profiles.py -v`.
6. **Charger et observer** en staging : `MemoryCurrent`, `memory.stat`
   (`pgmajfault`), TTFT. Si `pgmajfault` grimpe pendant la génération, le modèle
   ne tient pas — le désactiver.

#### Topologie cluster — où s'applique la contrainte

| Unité | `llama-server` dans son cgroup ? | Limites mémoire | Dépend de `models.yaml` ? |
|-------|----------------------------------|-----------------|---------------------------|
| `gateway/deploy/llm-gateway.service` (mode local) | **oui** | `MemoryHigh=80%` / `MemoryMax=90%` / `MemorySwapMax=0` | **oui** — table ci-dessus |
| `node_agent/deploy/llm-gateway-agent.service` (nœud GPU, mode cluster) | **oui** | idem | **oui** — la même table s'applique à la RAM du **nœud** |
| `gateway/deploy/llm-gateway-cluster.service` (orchestrateur) | non (pas de GPU, `PrivateDevices=true`, pas de `/models`) | `MemoryHigh=4G` / `MemoryMax=8G`, absolues | non |
| `gateway/deploy/llm-gateway-backup.service` | non (`sqlite3 .backup`) | aucune limite mémoire | non |

En mode cluster, **augmenter la RAM ou la limite de l'orchestrateur ne sert à
rien** : le modèle est chargé par le node-agent, sur le nœud. Un nœud doit avoir
la RAM du plus gros modèle qu'il peut se voir attribuer ; sinon retirer ce modèle
du registre de l'orchestrateur.

#### Test de non-régression

`gateway/tests/test_deploy_memory_profiles.py` lit les unités systemd livrées et
`gateway/models.yaml`, et échoue si :

- un modèle du registre n'a pas de profil mémoire déclaré (force la revue de
  déploiement à chaque ajout de modèle) ;
- un `vram_gb` diverge entre `models.yaml` et la table ;
- un modèle **activé** exige plus de RAM hôte que la limite de l'unité locale ou
  de l'unité node-agent ne l'autorise sur l'hôte de référence ;
- le profil activé complet dépasse cette limite ;
- une unité qui héberge des `llama-server` ne déclare pas explicitement sa
  politique mémoire (`MemoryHigh`, `MemoryMax`, `MemorySwapMax=0`, `OOMPolicy`,
  `TasksMax`) ;
- l'orchestrateur cluster gagne un accès GPU ou aux répertoires de modèles (son
  dimensionnement bas ne serait plus valide) ;
- un modèle activé pointe vers un répertoire absent de `ReadWritePaths`.

C'est une validation **par inspection outillée** : elle ne remplace pas un essai
en staging, elle empêche la dérive entre le registre et les unités.

---

## 16. Durcissement nginx (anti-slowloris et concurrence)

`gateway/deploy/nginx.conf` ajoute une défense en profondeur sans toucher aux
réglages SSE existants.

### Anti-slowloris

Timeouts courts sur la **réception** de la requête client (n'affectent PAS le
streaming de la réponse) :

```nginx
client_header_timeout   15s;
client_body_timeout     15s;
client_body_buffer_size 128k;
large_client_header_buffers 4 16k;
```

`client_max_body_size 10m` reste **inchangé** (prompts longs).

### Granularité du rate-limiting

La `location /v1/` est scindée :

- **`= /v1/models`** — route légère non-streamante : rate-limit souple, **pas de
  `limit_conn`** (un GET rapide ne doit pas consommer un slot de concurrence GPU) ;
- **`~ ^(/v1/(chat/completions|completions|completion|embeddings)|/completion)$`**
  — routes d'inférence : `limit_req` + **`limit_conn api_conn 4`** qui borne les
  streams SSE concurrents par IP (protection contre le DoS GPU). `/completion`
  sans préfixe `/v1/` y figure explicitement : c'est l'endpoint natif llama.cpp
  documenté en `docs/api.md` §7, qui tombait sinon dans le repli `location /`
  (404) ;
- **`/v1/`** (fallback) — conserve par prudence le comportement streaming
  d'origine, borné en concurrence ;
- **`= /ready`** — sonde de readiness non authentifiée, également documentée
  côté utilisateur ; rate-limitée, sans `limit_conn`, `access_log off`.

> **Réglages SSE préservés à l'identique** sur les routes streamantes :
> `proxy_buffering off`, `proxy_cache off`, `chunked_transfer_encoding on`,
> `add_header X-Accel-Buffering no`, `proxy_read_timeout 900s`,
> `proxy_send_timeout 900s`. Ne pas réduire ces timeouts : cela casserait les
> générations longues et les premiers appels à froid (COR-009 — la valeur est
> dérivée du `load_timeout_seconds` maximal du registre, voir l'en-tête de
> `gateway/deploy/nginx.conf`).

### Journal d'accès rédigé — aucun nom d'utilisateur en clair (SEC-016)

**Le problème.** La règle du projet est catégorique : ne jamais journaliser un
`username`, un email ou le champ libre `notes`. L'anonymisation RGPD
(`DELETE /admin/users/{username}`, §DEC-001) est vide de sens si un journal en
garde copie. Le filtre posé sur `uvicorn.access` couvre la gateway, mais **nginx
est devant et tient son propre journal** : son format `combined` par défaut écrit
`$request`, c'est-à-dire la ligne de requête brute — méthode, URI complète *et*
query string. Deux appels d'administration parfaitement normaux écrivaient donc
le nom en clair dans `/var/log/nginx/access.log` :

```
10.1.2.3 - - [03/Aug/2026:11:22:33 +0200] "GET /admin/users/Jean-Dupont HTTP/1.1" 200 412 "-" "curl/8.4.0"
10.1.2.3 - - [03/Aug/2026:11:22:34 +0200] "GET /admin/usage?username=Jean-Dupont HTTP/1.1" 200 8104 "-" "curl/8.4.0"
```

**La réponse.** *Pas* `access_log off` : couper le journal fermerait la fuite et
rendrait tout diagnostic d'incident impossible. `nginx.conf` remplace `$request`
par une reconstruction rédigée, via deux `map` et un `log_format eva_redacted` :

```
10.1.2.3 [03/Aug/2026:11:22:33 +0200] "GET /admin/users/<redacted> HTTP/1.1" 200 412 0.042 "curl/8.4.0"
10.1.2.3 [03/Aug/2026:11:22:34 +0200] "GET /admin/usage?<redacted> HTTP/1.1" 200 8104 0.310 "curl/8.4.0"
```

Ce qui **reste** : adresse cliente, horodatage, méthode, forme de la route,
protocole, statut, volume, durée (`$request_time`, absent de `combined` et
précieux sur une passerelle d'inférence), user-agent. Ce qui **disparaît** :
le seul segment de chemin et les seules valeurs de paramètres susceptibles de
porter un nom.

| Élément | Règle |
|---------|-------|
| Chemin | `map $uri $eva_log_path` — `/admin/users/<nom>` et `/admin/users/<nom>/keys` deviennent `/admin/users/<redacted>[/keys]`. Dérivé de `$uri` (pourcent-décodé), donc `/admin/users/%4Aean` est rédigé lui aussi. |
| Query string | `map $args $eva_log_args` — **liste d'autorisation** : `from_date`, `to_date`, `limit`, `force`, `period` restent lisibles, **tout le reste est rédigé**, y compris un paramètre ajouté demain. |
| Corps | Jamais journalisé. `$request_body` ne doit pas être ajouté : `email` et `notes` y transitent. |
| `$http_referer`, `$remote_user` | Retirés de `combined` : le premier peut recopier l'URI d'une page admin, le second serait un identifiant si un `auth_basic` était ajouté. |

La liste d'autorisation est **la même** que celle du filtre uvicorn
(`_LOGGABLE_QUERY_PARAMS` dans `gateway/main.py`, SEC-010) — une seule politique,
pas deux. Une seule divergence, assumée et toujours dans le sens conservateur :
nginx ne sait pas itérer sur les paramètres dans un `map`, il décide donc sur la
query string **entière**. Sur `?username=X&limit=50`, uvicorn journalise
`username=<redacted>&limit=50`, nginx journalise `<redacted>` — nginx en dit
moins, jamais plus. Si vous modifiez la liste d'un côté, modifiez l'autre :
`gateway/tests/test_nginx_access_log_redaction.py` compare les deux et rougit
sinon.

**Fichier dédié.** Le journal part dans `/var/log/nginx/llm-gateway-access.log`
et non dans `access.log`, pour ne pas mélanger deux formats dans un même fichier.
Aucune configuration de rotation à ajouter : le logrotate nginx de Debian/Ubuntu
porte sur `/var/log/nginx/*.log`. Les `access_log off` de `/health` et `/ready`
sont **conservés** (sondes appelées en boucle).

**Journal d'erreur.** nginx n'a pas d'équivalent de `log_format` pour son journal
d'erreur : le format est figé dans le code et chaque ligne de niveau `error`
traîne un contexte `request: "GET /admin/users/<nom> HTTP/1.1"` — l'URI brute,
hors de portée des `map`. La seule parade native est le seuil : `location /admin/`
porte `error_log /var/log/nginx/error.log crit;`. Conséquence à connaître —
sur `/admin/` uniquement, trois messages disparaissent du journal d'erreur :
`access forbidden by rule` (le filtrage IP campus), `upstream timed out` et
`connect() failed`. Les événements correspondants restent visibles dans le
journal d'accès rédigé sous leur statut (403, 502, 504), avec l'adresse cliente
et la durée ; seul le commentaire de nginx est perdu. Les routes `/v1/*` gardent
un journal d'erreur complet : leurs URI sont fixes et ne portent aucune donnée
personnelle, et c'est là que se diagnostiquent les incidents d'inférence.

Recharger après modification :

```bash
sudo nginx -t && sudo nginx -s reload
```

Vérification manuelle de la rédaction, après reload :

```bash
curl -sk -H "X-Admin-Secret: $ADMIN_SECRET" \
     "https://gateway.example.com/admin/usage?username=CANARI-Prenom-Nom" >/dev/null
sudo grep -c 'CANARI' /var/log/nginx/llm-gateway-access.log   # doit afficher 0
sudo tail -1 /var/log/nginx/llm-gateway-access.log            # doit montrer « ?<redacted> »
```

---

## 17. Rotation des logs journald

`llm-gateway` et ses `llama-server` peuvent être verbeux (`--access-log`
uvicorn, logs d'inférence). journald n'a **pas** de quota par service ; le
drop-in fixe donc des limites **globales** au journal système.

**Automatique.** `install.sh` et `update.sh` déposent ce drop-in s'il est absent
et redémarrent `systemd-journald`. Comme les réglages sont globaux et dépendent
de l'espace disque local, le fichier existant n'est **jamais écrasé** : un
ajustement manuel de `SystemMaxUse` est donc préservé lors des mises à jour.

Installation ou réinitialisation manuelle (si besoin) :

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
sudo cp gateway/deploy/journald-llm-gateway.conf \
        /etc/systemd/journald.conf.d/llm-gateway.conf
sudo systemctl restart systemd-journald
```

Réglages appliqués (à ajuster selon l'espace disque de `/var/log/journal`) :

| Clé | Valeur | Effet |
|-----|--------|-------|
| `SystemMaxUse` | `500M` | Taille max du journal persistant |
| `SystemKeepFree` | `1G` | Espace libre garanti sur le FS |
| `SystemMaxFileSize` | `100M` | Taille max d'un fichier journal |
| `MaxRetentionSec` | `30day` | Purge des entrées de plus de 30 jours |

Vérifier :

```bash
journalctl --disk-usage
sudo journalctl --vacuum-time=30d   # purge manuelle immédiate si besoin
```

---

## 18. Référence de configuration (variables d'environnement)

Toutes les variables ci-dessous vivent dans `/etc/llm-gateway/env` (gateway
principale/orchestrateur) et, pour le cluster, sur chaque node-agent dans
`/etc/llm-gateway-agent/env`. Les noms sont insensibles à la casse. Cette section
consolide les réglages **récemment ajoutés** ; les paramètres historiques
(`TOTAL_VRAM_GB`, `BASE_LLAMA_PORT`, `MAX_LOADED_MODELS`, `IDLE_TIMEOUT_SECONDS`,
`CAPACITY_QUEUE_*`, secrets…) sont couverts en [section 5](#5-configuration).

### Intégrité et version llama-server (supply-chain)

| Variable | Défaut | Rôle |
|----------|--------|------|
| `LLAMA_SERVER_MIN_BUILD` | `0` | Build minimal accepté du binaire `llama-server`. `0` = aucun enforcement. Si `> 0` et build lu strictement inférieur → **démarrage refusé** ; si `> 0` et version illisible → **démarrage refusé** aussi (fail-closed, SEC-009). Version illisible avec `0` → simple avertissement. À fixer sur le premier build patché (cf. [section 11](#11-mise-à-jour)). |

Le défaut de la classe de configuration reste `0` pour le développement et les
tests. Les chemins de production sont plus stricts : `install-agent.sh` exige
`--llama-min-build > 0`, son préflight et `ExecStartPre` refusent `0`, et
`bootstrap-apply --apply` exige également un plancher positif avant de publier
le runtime de la gateway locale.

> Le champ `sha256` par modèle (intégrité GGUF) se configure dans `models.yaml`,
> pas ici — voir [section 6](#6-registre-des-modèles-modelsyaml).

### Pool de connexions HTTP vers llama-server (chemin chaud)

| Variable | Défaut | Rôle |
|----------|--------|------|
| `HTTPX_MAX_CONNECTIONS` | `200` | Connexions totales max du client partagé (`0` = illimité, déconseillé). |
| `HTTPX_MAX_KEEPALIVE` | `100` | Connexions keep-alive conservées au repos (`0` = illimité). |
| `HTTPX_KEEPALIVE_EXPIRY` | `30.0` | Durée de vie (s) d'une connexion keep-alive inactive. |

### Robustesse du cycle de vie (shutdown, VRAM, orphelins)

| Variable | Défaut | Rôle |
|----------|--------|------|
| `SHUTDOWN_DRAIN_TIMEOUT_SECONDS` | `25.0` | Attente max des requêtes actives (modèles pinnés) au SIGTERM avant déchargement forcé (`0` = pas d'attente). |
| `SHUTDOWN_DRAIN_POLL_SECONDS` | `0.2` | Intervalle de poll pendant le drain. |
| `VRAM_RECONCILE_INTERVAL_SECONDS` | `60.0` | Intervalle entre deux sondes `nvidia-smi` de réconciliation VRAM (`0` = désactivé). |
| `VRAM_RECONCILE_PROBE_TIMEOUT_SECONDS` | `5.0` | Timeout de la sonde `nvidia-smi`. |
| `VRAM_RECONCILE_DRIFT_THRESHOLD` | `0.15` | Seuil de dérive relative déclenchant un warning (0.15 = +15 %). |
| `KILL_ORPHAN_LLAMA_ON_STARTUP` | `false` | Tenter de tuer les `llama-server` orphelins occupant un port du pool au démarrage (best-effort, nécessite `psutil`). Par défaut : LOG seulement. |

La réconciliation VRAM et la détection d'orphelins sont **non fatales** et
inertes sans GPU / `nvidia-smi` / port occupé. Détails :
[architecture.md](architecture.md#robustesse-du-cycle-de-vie-shutdown-vram-orphelins).

### Cluster multi-nœuds (mode `cluster` uniquement)

| Variable | Défaut | Rôle |
|----------|--------|------|
| `CLUSTER_STRICT_TLS_VERIFY` | `false` | Si `true`, refuse au démarrage une config `tls_verify: false` combinée à un nœud `https://` (fail-fast). Par défaut : simple avertissement (compatibilité dev/certs auto-signés). |
| `CLUSTER_LOAD_TIMEOUT` | `300.0` | Timeout (s) dédié au chargement d'un modèle sur un agent — un gros GGUF prend plusieurs minutes, bien au-delà de `CLUSTER_REQUEST_TIMEOUT` (défaut `10.0`, plan de contrôle). |

`tls_verify` lui-même (bundle CA, `true`/`false`) se déclare dans `nodes.yaml`
(section `cluster:`), pas via une variable d'environnement — voir
[section 13](#13-déploiement-multi-nœuds-optionnel--avancé). `CLUSTER_HEALTH_INTERVAL`
(défaut `10`) et `CLUSTER_HEALTH_FAILURES_TO_OFFLINE` (défaut `3`) pilotent le
heartbeat.

> Ces variables s'appliquent à l'**orchestrateur**. Le node-agent lit sa propre
> config (`node_agent/config.py` : `NODE_ID`, `AGENT_PORT`, `AGENT_SECRET`,
> `TOTAL_VRAM_GB`, `LLAMA_SERVER_BIN`, `LLAMA_SERVER_MIN_BUILD`…).
