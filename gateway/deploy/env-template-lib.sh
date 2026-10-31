#!/usr/bin/env bash
# Rendu du fichier d'environnement initial (/etc/llm-gateway/env) — SEC-002.
#
# Pourquoi cette bibliothèque existe
# ----------------------------------
# Trois durcissements de sécurité ne figuraient pas dans l'environnement généré
# par `install.sh` : l'allowlist des répertoires de modèles, la politique CORS et
# le plancher de version llama-server. Absents du fichier, ils n'existaient pas
# pour l'exploitant : rien ne les nommait, rien ne disait quoi y mettre.
#
# `ALLOWED_MODEL_DIRS` méritait mieux qu'une ligne : jusqu'à COR-014 elle était
# non seulement absente mais **impossible à activer** — une valeur CSV faisait
# échouer le démarrage sur `SettingsError`. Elle est désormais activable, donc
# elle doit être posée.
#
# Le rendu est isolé ici, et non écrit en ligne dans `install.sh`, pour une seule
# raison : `install.sh` vit derrière un contrôle root et n'est pas exerçable en
# test. Sourcée, cette fonction produit le VRAI fichier, que
# `gateway/tests/test_deploy_env_hardening.py` charge ensuite avec la VRAIE
# classe `Settings` — pas avec un `grep`. Un durcissement qui empêcherait le
# service de démarrer se verrait donc immédiatement.

# ── Allowlist des répertoires de modèles ──────────────────────────────────────

# deploy_model_dirs_from_registry <registre.yaml>
#
# Répertoires (un par ligne, dédoublonnés) portant les `path:` / `mmproj_path:`
# déclarés par un registre.
#
# Pourquoi lire le registre plutôt qu'écrire « /models » en dur : `ModelRegistry`
# valide l'allowlist sur TOUTES les entrées, activées ou non. Une seule entrée
# hors allowlist et la gateway REFUSE de démarrer. Le registre livré déclare des
# chemins hors de /models : poser « /models » seul rendrait donc l'installation
# neuve non démarrable — un durcissement qui casse le produit n'est pas un
# durcissement, c'est une panne.
deploy_model_dirs_from_registry() {
    local registry="$1" line value
    [[ -f "$registry" ]] || return 0
    while IFS= read -r line; do
        value="${line#*:}"          # retire la clé « path: »
        value="${value%%#*}"        # retire un commentaire de fin de ligne
        value="${value//\"/}"       # retire les guillemets YAML
        value="${value//\'/}"
        value="${value#"${value%%[![:space:]]*}"}"   # trim gauche
        value="${value%"${value##*[![:space:]]}"}"   # trim droite
        [[ "$value" == /*/* ]] || continue           # chemin absolu avec parent
        printf '%s\n' "${value%/*}"
    done < <(grep -E '^[[:space:]]*(path|mmproj_path):' "$registry" 2>/dev/null) \
        | sort -u
}

# deploy_allowed_model_dirs <repertoire_modeles> <registre.yaml>
#
# Valeur CSV de `ALLOWED_MODEL_DIRS` : le répertoire de modèles que
# l'installateur vient de créer, plus les répertoires déclarés par le registre
# qu'il pose. Aucune de ces deux sources n'est inventée — ce sont deux artefacts
# que le script écrit lui-même sur l'hôte.
deploy_allowed_model_dirs() {
    local models_dir="$1" registry="$2" dir result=""
    result="$models_dir"
    while IFS= read -r dir; do
        [[ -n "$dir" && "$dir" != "$models_dir" ]] || continue
        case ",$result," in
            *",$dir,"*) continue ;;
        esac
        result="$result,$dir"
    done < <(deploy_model_dirs_from_registry "$registry")
    printf '%s\n' "$result"
}

# ── Rendu du fichier ──────────────────────────────────────────────────────────

# deploy_render_env_file <cible> <data_dir> <log_dir> <config_dir> <models_dir>
#                        <registre_template> <internal_key> <admin_secret>
#
# Écrit le fichier d'environnement initial. N'écrase jamais : l'appelant vérifie
# l'absence du fichier au préalable.
deploy_render_env_file() {
    local target="$1" data_dir="$2" log_dir="$3" config_dir="$4"
    local models_dir="$5" registry="$6" internal_key="$7" admin_secret="$8"
    local allowed_dirs

    allowed_dirs="$(deploy_allowed_model_dirs "$models_dir" "$registry")"

    cat > "$target" <<EOF
# EVARuntime — Configuration
# Généré le $(date -u +"%Y-%m-%dT%H:%M:%SZ")
# Modifier selon votre environnement.
# Les modèles (chemins, paramètres llama-server) sont dans ${data_dir}/models.yaml

# ── Chemins ───────────────────────────────────────────────────────────────────
MODELS_CONFIG_PATH=${data_dir}/models.yaml
# Chemin stable publié atomiquement par bootstrap-apply. Le lien peut être
# absent au premier démarrage ; /health reste disponible et /ready explique
# alors que le runtime doit encore être appliqué.
LLAMA_SERVER_BIN=${LLAMA_BIN:-/opt/llama.cpp/current/llama-server}
DB_PATH=${data_dir}/gateway.db
LOG_DIR=${log_dir}

# ── Pool de ports multi-modèles ───────────────────────────────────────────────
BASE_LLAMA_PORT=8081
MAX_LOADED_MODELS=5

# ── Budget VRAM (adapter à la machine) ───────────────────────────────────────
TOTAL_VRAM_GB=48.0
VRAM_OVERHEAD_GB=2.0
VRAM_SAFETY_MARGIN=0.05

# ── Modèle par défaut (vide = premier modèle activé du registre) ─────────────
DEFAULT_MODEL_ID=

# ── Lifecycle ─────────────────────────────────────────────────────────────────
IDLE_TIMEOUT_SECONDS=300
MODEL_LOAD_TIMEOUT_SECONDS=180
IDLE_CHECK_INTERVAL_SECONDS=30

# ── Queue d'admission VRAM ───────────────────────────────────────────────────
CAPACITY_QUEUE_ENABLED=true
CAPACITY_QUEUE_TIMEOUT_SECONDS=120
CAPACITY_QUEUE_MAX_WAITERS=100
CAPACITY_QUEUE_RETRY_AFTER_SECONDS=10

# ── Borne de génération par requête ──────────────────────────────────────────
# max_tokens / max_completion_tokens / n_predict. 0 = aucune borne.
# Un dépassement ou une valeur négative/non entière est refusé en 400.
MAX_NEW_TOKENS_PER_REQUEST=0

# ── DURCISSEMENT 1/3 — allowlist des répertoires de modèles (SEC-002) ────────
# Seuls les .gguf situés sous ces répertoires peuvent être déclarés dans
# models.yaml. C'est la barrière contre un chemin arbitraire injecté par l'API
# admin (lecture de fichier hors des zones prévues).
#
# Valeur posée = le répertoire de modèles créé par install.sh, plus les
# répertoires déclarés par le registre livré. RESTREIGNEZ-LA dès que vos chemins
# réels sont fixés : chaque répertoire en trop élargit la surface.
#
# Format : CSV (« /models,/data/models ») ou tableau JSON. VIDE = AUCUNE
# restriction — ne pas laisser tel quel en production.
# ATTENTION : toute entrée de models.yaml hors de cette liste, même désactivée,
# empêche le DÉMARRAGE du service. Vérifiez avec « evaruntime doctor ».
ALLOWED_MODEL_DIRS=${allowed_dirs}

# ── DURCISSEMENT 2/3 — CORS explicite (SEC-002) ──────────────────────────────
# Origines de navigateur autorisées. VIDE = aucune : c'est le défaut retenu, et
# il est sûr pour l'usage nominal — l'API est consommée par des clients serveur
# (curl, SDK OpenAI) que CORS ne concerne pas, et le dashboard admin est servi
# depuis la MÊME origine que la gateway.
# Ne l'ouvrir que pour une application navigateur tierce, domaine par domaine :
#   CORS_ALLOW_ORIGINS=https://app.example.com,https://chat.example.com
# « * » autoriserait n'importe quelle page web à parler à la gateway avec les
# en-têtes du client : à proscrire en production.
CORS_ALLOW_ORIGINS=

# ── DURCISSEMENT 3/3 — plancher de version llama-server (SEC-002) ────────────
# Build minimal accepté du binaire llama-server. 0 = AUCUN enforcement, et le
# garde-fou supply-chain est alors INERTE : « evaruntime doctor » le signale à
# chaque exécution (code min_build_not_enforced), délibérément.
# À fixer sur le premier build patché contre GHSA-8947-pfff-2f3c (écriture OOB
# via n_discard/context-shift) et les overflows de parsing GGUF. Relevez le build
# réellement installé, puis inscrivez-le ici :
#   llama-server --version
# Si > 0 et que le binaire est plus ancien, le démarrage est REFUSÉ.
LLAMA_SERVER_MIN_BUILD=0

# ── Sécurité (NE PAS PARTAGER) ────────────────────────────────────────────────
INTERNAL_API_KEY=${internal_key}
ADMIN_SECRET=${admin_secret}

# ── Réseau ────────────────────────────────────────────────────────────────────
GATEWAY_HOST=127.0.0.1
GATEWAY_PORT=8000
LLAMA_SERVER_HOST=127.0.0.1
CUDA_VISIBLE_DEVICES=0

# ── Rate limiting par défaut ───────────────────────────────────────────────────
DEFAULT_RPM_LIMIT=20
DEFAULT_MONTHLY_TOKEN_LIMIT=0

# ── Cluster multi-nœuds (désactivé par défaut — activer avec --cluster) ───────
CLUSTER_MODE=local
# CLUSTER_NODES_PATH=${config_dir}/nodes.yaml
# AGENT_SECRET=CHANGE_ME_GENERATE_WITH_python3_-c_import_secrets;_print(secrets.token_urlsafe(32))
# CLUSTER_REQUEST_TIMEOUT=10.0
# CLUSTER_LOAD_TIMEOUT=300.0
# CLUSTER_HEALTH_INTERVAL=10
# CLUSTER_HEALTH_FAILURES_TO_OFFLINE=3
EOF
}

# ── Clés de durcissement attendues dans un environnement existant ─────────────
# `update.sh` ne régénère JAMAIS le fichier d'environnement : sur un hôte
# installé avant SEC-002, ces clés resteraient donc absentes indéfiniment. Elles
# ne sont pas ajoutées d'autorité — écrire « CORS_ALLOW_ORIGINS= » sur une
# installation qui sert un client navigateur le casserait en silence pendant une
# mise à jour. `update.sh` les SIGNALE, l'opérateur tranche.
DEPLOY_HARDENING_KEYS=(ALLOWED_MODEL_DIRS CORS_ALLOW_ORIGINS LLAMA_SERVER_MIN_BUILD)
