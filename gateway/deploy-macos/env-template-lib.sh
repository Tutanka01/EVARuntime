#!/usr/bin/env bash
# env-template-lib.sh — Rendu du fichier d'environnement initial sur macOS
#
# Génère ~/.config/evaruntime/env avec les chemins et réglages adaptés à macOS.
# Les valeurs par défaut sont adaptées pour Apple Silicon (Metal, unified memory).

# ── Allowlist des répertoires de modèles ──────────────────────────────────────

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

deploy_render_env_file() {
    local target="$1" data_dir="$2" log_dir="$3" config_dir="$4"
    local models_dir="$5" registry="$6" internal_key="$7" admin_secret="$8"
    local allowed_dirs

    allowed_dirs="$(deploy_allowed_model_dirs "$models_dir" "$registry")"

    # Déterminer le chemin llama-server : Homebrew sur Apple Silicon
    local llama_bin="${LLAMA_BIN:-/opt/homebrew/bin/llama-server}"
    
    # Estimer la RAM unifiée du Mac Studio (en GB)
    local total_ram_gb
    total_ram_gb=$(sysctl -n hw.memsize 2>/dev/null | awk '{printf "%.0f", $1 / 1073741824}')
    total_ram_gb="${total_ram_gb:-64}"  # fallback si sysctl échoue

    cat > "$target" <<EOF
# EVARuntime — Configuration macOS
# Généré le $(date -u +"%Y-%m-%dT%H:%M:%SZ")
# Modifier selon votre environnement.
# Les modèles (chemins, paramètres llama-server) sont dans ${data_dir}/models.yaml

# ── Chemins ───────────────────────────────────────────────────────────────────
MODELS_CONFIG_PATH=${data_dir}/models.yaml
LLAMA_SERVER_BIN=${llama_bin}
DB_PATH=${data_dir}/gateway.db
LOG_DIR=${log_dir}

# ── Pool de ports multi-modèles ───────────────────────────────────────────────
BASE_LLAMA_PORT=8081
MAX_LOADED_MODELS=5

# ── Budget mémoire unifiée (Apple Silicon) ────────────────────────────────────
# RAM totale détectée : ${total_ram_gb} GB
# Sur Apple Silicon, la mémoire est unifiée (CPU + GPU partagent le même pool).
# Réserver ~10% pour le système et les processus macOS.
TOTAL_VRAM_GB=${total_ram_gb}
VRAM_OVERHEAD_GB=4.0
VRAM_SAFETY_MARGIN=0.10

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

# ── DURCISSEMENT 1/3 — allowlist des répertoires de modèles (SEC-002) ────────
ALLOWED_MODEL_DIRS=${allowed_dirs}

# ── DURCISSEMENT 2/3 — CORS explicite (SEC-002) ──────────────────────────────
CORS_ALLOW_ORIGINS=

# ── DURCISSEMENT 3/3 — plancher de version llama-server (SEC-002) ────────────
LLAMA_SERVER_MIN_BUILD=0

# ── Sécurité (NE PAS PARTAGER) ────────────────────────────────────────────────
INTERNAL_API_KEY=${internal_key}
ADMIN_SECRET=${admin_secret}

# ── Réseau ────────────────────────────────────────────────────────────────────
GATEWAY_HOST=127.0.0.1
GATEWAY_PORT=8000
LLAMA_SERVER_HOST=127.0.0.1

# ── Rate limiting par défaut ───────────────────────────────────────────────────
DEFAULT_RPM_LIMIT=20
DEFAULT_MONTHLY_TOKEN_LIMIT=0

# ── Cluster multi-nœuds (désactivé par défaut) ────────────────────────────────
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
# Même contrat que deploy/env-template-lib.sh : update.sh ne régénère JAMAIS le
# fichier d'environnement. Sur une installation antérieure à SEC-002, ces clés
# peuvent manquer ; update.sh les signale, l'opérateur tranche. Elles ne sont
# jamais ajoutées d'autorité — voir la copie Linux pour la justification complète.
DEPLOY_HARDENING_KEYS=(ALLOWED_MODEL_DIRS CORS_ALLOW_ORIGINS LLAMA_SERVER_MIN_BUILD)
