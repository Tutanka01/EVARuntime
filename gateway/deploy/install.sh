#!/usr/bin/env bash
# install.sh — Installation du LLM Gateway UPPA
# Testé sur : Ubuntu 22.04 / 24.04
#
# Usage :
#   sudo bash install.sh --mode local    # mono-nœud (défaut)
#   sudo bash install.sh --mode cluster  # orchestrateur multi-nœuds (opt-in)
#   bash install.sh --mode cluster --dry-run
#
# Ce script est idempotent : le relancer ne casse pas une installation existante.

set -euo pipefail
IFS=$'\n\t'

# ── Arguments ─────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=deploy-mode-lib.sh
source "$SCRIPT_DIR/deploy/deploy-mode-lib.sh"

usage() {
    cat <<EOF
Usage: $0 [--mode local|cluster] [--cluster] [--allow-mode-change] [--dry-run]

  --mode local       Gateway mono-nœud (défaut sur une installation neuve).
  --mode cluster     Orchestrateur multi-nœuds; les agents s'installent à part.
  --cluster          Alias historique de --mode cluster.
  --allow-mode-change
                     Confirme une migration d'une installation existante.
  --dry-run          Affiche le mode et le plan sans modifier l'hôte.

Sans option, install.sh choisit local. Sur une installation existante, indiquez
toujours le mode existant; aucun changement de mode n'est implicite.
EOF
}

REQUESTED_MODE="local"
MODE_WAS_EXPLICIT=false
ALLOW_MODE_CHANGE=false
DRY_RUN=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)
            [[ $# -ge 2 ]] || { echo "--mode requiert local ou cluster" >&2; usage; exit 2; }
            deploy_validate_mode "$2" || { echo "Mode invalide : $2" >&2; usage; exit 2; }
            [[ "$MODE_WAS_EXPLICIT" != true || "$REQUESTED_MODE" == "$2" ]] || { echo "Options de mode contradictoires" >&2; exit 2; }
            REQUESTED_MODE="$2"; MODE_WAS_EXPLICIT=true; shift 2 ;;
        --mode=*)
            value="${1#*=}"
            deploy_validate_mode "$value" || { echo "Mode invalide : $value" >&2; usage; exit 2; }
            [[ "$MODE_WAS_EXPLICIT" != true || "$REQUESTED_MODE" == "$value" ]] || { echo "Options de mode contradictoires" >&2; exit 2; }
            REQUESTED_MODE="$value"
            MODE_WAS_EXPLICIT=true; shift ;;
        --cluster)
            [[ "$MODE_WAS_EXPLICIT" != true || "$REQUESTED_MODE" == "cluster" ]] || { echo "--cluster contredit --mode $REQUESTED_MODE" >&2; exit 2; }
            REQUESTED_MODE="cluster"; MODE_WAS_EXPLICIT=true; shift ;;
        --allow-mode-change) ALLOW_MODE_CHANGE=true; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Option inconnue : $1" >&2; usage; exit 2 ;;
    esac
done

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()    { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── Configuration ─────────────────────────────────────────────────────────────

INSTALL_DIR="${LLM_GATEWAY_INSTALL_DIR:-/opt/llm-gateway}"
DATA_DIR="${LLM_GATEWAY_DATA_DIR:-/var/lib/llm-gateway}"
LOG_DIR="${LLM_GATEWAY_LOG_DIR:-/var/log/llm-gateway}"
MODELS_DIR="${LLM_GATEWAY_MODELS_DIR:-/models}"
CONFIG_DIR="${LLM_GATEWAY_CONFIG_DIR:-/etc/llm-gateway}"
SERVICE_USER="llmservice"
PYTHON="${PYTHON:-python3}"
CONFIG_FILE="$CONFIG_DIR/env"
CURRENT_MODE="$(deploy_env_value "$CONFIG_FILE" CLUSTER_MODE)"
EFFECTIVE_MODE="$REQUESTED_MODE"

if [[ -n "$CURRENT_MODE" ]] && ! deploy_validate_mode "$CURRENT_MODE"; then
    error "Valeur CLUSTER_MODE invalide dans $CONFIG_FILE : '$CURRENT_MODE'"
fi
if [[ -n "$CURRENT_MODE" && "$CURRENT_MODE" != "$EFFECTIVE_MODE" ]]; then
    if [[ "$MODE_WAS_EXPLICIT" != true ]]; then
        error "Installation existante en mode $CURRENT_MODE. Relancez avec --mode $CURRENT_MODE (aucune migration implicite)."
    fi
    if [[ "$ALLOW_MODE_CHANGE" != true && "$DRY_RUN" != true ]]; then
        error "Migration $CURRENT_MODE → $EFFECTIVE_MODE non confirmée. Vérifiez le plan avec --dry-run puis ajoutez --allow-mode-change."
    fi
fi

echo ""
echo "EVARuntime — préflight installation"
echo "  Mode demandé : $EFFECTIVE_MODE"
echo "  Mode existant  : ${CURRENT_MODE:-<aucun>}"
echo "  Configuration  : $CONFIG_FILE"
echo "  Conservation   : env, models.yaml, nodes.yaml et secrets existants"

if [[ "$DRY_RUN" == true ]]; then
    echo "  Action         : aucune (--dry-run)"
    if [[ -n "$CURRENT_MODE" && "$CURRENT_MODE" != "$EFFECTIVE_MODE" ]]; then
        echo "  Migration      : $CURRENT_MODE → $EFFECTIVE_MODE; l'exécution exigera --allow-mode-change"
    fi
    if [[ "$EFFECTIVE_MODE" == "cluster" ]]; then
        echo "  Parcours       : orchestrateur sans GPU local; agents, TLS et ports inter-nœuds à configurer séparément"
    else
        echo "  Parcours       : llama-server et modèles sur cet hôte"
    fi
    exit 0
fi

[[ $EUID -eq 0 ]] || error "Ce script doit être exécuté en root (sudo bash install.sh)"
for required in awk chmod chown cp find id mkdir mktemp mv systemctl useradd "$PYTHON"; do
    command -v "$required" &>/dev/null || error "Préflight : commande requise introuvable : $required"
done
[[ -f "$SCRIPT_DIR/requirements.txt" ]] || error "Préflight : requirements.txt introuvable"
[[ -f "$SCRIPT_DIR/deploy/llm-gateway.service" ]] || error "Préflight : unité systemd locale introuvable"
if [[ "$EFFECTIVE_MODE" == "cluster" ]]; then
    [[ -f "$SCRIPT_DIR/deploy/llm-gateway-cluster.service" ]] || error "Préflight : unité systemd orchestrateur introuvable"
    [[ -f "$SCRIPT_DIR/deploy/nodes.yaml.example" ]] || error "Préflight : template nodes.yaml introuvable"
else
    command -v usermod &>/dev/null || error "Préflight : commande requise introuvable : usermod"
    command -v nvidia-smi &>/dev/null || error "Préflight local : nvidia-smi introuvable"
    LLAMA_BIN="$(deploy_env_value "$CONFIG_FILE" LLAMA_SERVER_BIN)"
    [[ -x "${LLAMA_BIN:-/usr/local/bin/llama-server}" ]] || error "Préflight local : llama-server non exécutable (${LLAMA_BIN:-/usr/local/bin/llama-server})"
fi
info "Préflight validé; installation en mode $EFFECTIVE_MODE."

# ── 1. Création de l'utilisateur système ─────────────────────────────────────

info "Création de l'utilisateur système '$SERVICE_USER'…"
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd \
        --system \
        --shell /sbin/nologin \
        --no-create-home \
        --comment "LLM Gateway Service" \
        "$SERVICE_USER"
    info "Utilisateur '$SERVICE_USER' créé."
else
    info "Utilisateur '$SERVICE_USER' existe déjà."
fi

# L'orchestrateur cluster n'accède jamais au GPU local.
if [[ "$EFFECTIVE_MODE" == "local" ]]; then
    usermod -aG render,video "$SERVICE_USER" 2>/dev/null || true
fi

# ── 2. Création des répertoires ───────────────────────────────────────────────

info "Création des répertoires…"
for dir in "$INSTALL_DIR" "$DATA_DIR" "$LOG_DIR" "$CONFIG_DIR"; do
    mkdir -p "$dir"
done
if [[ "$EFFECTIVE_MODE" == "local" ]]; then
    mkdir -p "$MODELS_DIR"
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR" "$LOG_DIR"
chown -R root:root "$INSTALL_DIR" "$CONFIG_DIR"
chmod 755 "$INSTALL_DIR" "$CONFIG_DIR"
chmod 750 "$DATA_DIR" "$LOG_DIR"

# Modèles locaux : lisibles par le service, pas par les autres.
if [[ "$EFFECTIVE_MODE" == "local" ]]; then
    chown -R root:"$SERVICE_USER" "$MODELS_DIR"
    chmod -R 750 "$MODELS_DIR"
fi

info "Répertoires créés."

# ── 3. Vérification Python ────────────────────────────────────────────────────

info "Vérification de Python…"
PYTHON_VERSION=$("$PYTHON" --version 2>&1 | cut -d' ' -f2)
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d'.' -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d'.' -f2)

if [[ "$PYTHON_MAJOR" -lt 3 ]] || [[ "$PYTHON_MAJOR" -eq 3 && "$PYTHON_MINOR" -lt 11 ]]; then
    error "Python 3.11+ requis. Version trouvée : $PYTHON_VERSION"
fi
info "Python $PYTHON_VERSION OK."

# ── 4. Copie du code source ───────────────────────────────────────────────────

info "Copie du code source vers $INSTALL_DIR…"
# Copier tous les fichiers Python
cp "$SCRIPT_DIR"/*.py "$INSTALL_DIR/"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"

# Package cluster/ — requis en CLUSTER_MODE=cluster (importé par model_manager)
mkdir -p "$INSTALL_DIR/cluster"
cp "$SCRIPT_DIR/cluster"/*.py "$INSTALL_DIR/cluster/"

# Fichiers statiques (dashboard admin servi par /admin/dashboard)
if [[ -d "$SCRIPT_DIR/static" ]]; then
    mkdir -p "$INSTALL_DIR/static"
    cp -r "$SCRIPT_DIR/static/." "$INSTALL_DIR/static/"
fi

chown -R root:"$SERVICE_USER" "$INSTALL_DIR"
chmod -R 640 "$INSTALL_DIR"/*.py
chmod 640 "$INSTALL_DIR/cluster"/*.py
chmod 750 "$INSTALL_DIR/cluster"
if [[ -d "$INSTALL_DIR/static" ]]; then
    find "$INSTALL_DIR/static" -type d -exec chmod 755 {} \;
    find "$INSTALL_DIR/static" -type f -exec chmod 644 {} \;
fi
chmod 644 "$INSTALL_DIR/requirements.txt"

# ── 5. Environnement virtuel Python ──────────────────────────────────────────

info "Création/mise à jour de l'environnement virtuel…"
if [[ ! -d "$INSTALL_DIR/venv" ]]; then
    "$PYTHON" -m venv "$INSTALL_DIR/venv"
fi

"$INSTALL_DIR/venv/bin/pip" install --upgrade pip --quiet
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" --quiet
info "Dépendances installées."

# ── 6. Fichier de configuration ───────────────────────────────────────────────

CONFIG_FILE="$CONFIG_DIR/env"

if [[ ! -f "$CONFIG_FILE" ]]; then
    info "Génération de la configuration initiale…"

    INTERNAL_KEY=$(python3 -c "import secrets; print('llmgw-internal-' + secrets.token_urlsafe(32))")
    ADMIN_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")

    cat > "$CONFIG_FILE" << EOF
# LLM Gateway UPPA — Configuration
# Généré le $(date -u +"%Y-%m-%dT%H:%M:%SZ")
# Modifier selon votre environnement.
# Les modèles (chemins, paramètres llama-server) sont dans $DATA_DIR/models.yaml

# ── Chemins ───────────────────────────────────────────────────────────────────
MODELS_CONFIG_PATH=${DATA_DIR}/models.yaml
LLAMA_SERVER_BIN=/usr/local/bin/llama-server
DB_PATH=${DATA_DIR}/gateway.db
LOG_DIR=${LOG_DIR}

# ── Pool de ports multi-modèles ───────────────────────────────────────────────
BASE_LLAMA_PORT=8081
MAX_LOADED_MODELS=5

# ── Budget VRAM (L40S 48 GB — adapter selon GPU) ─────────────────────────────
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

# ── Sécurité (NE PAS PARTAGER) ────────────────────────────────────────────────
INTERNAL_API_KEY=${INTERNAL_KEY}
ADMIN_SECRET=${ADMIN_SECRET}

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
# CLUSTER_NODES_PATH=${CONFIG_DIR}/nodes.yaml
# AGENT_SECRET=CHANGE_ME_GENERATE_WITH_python3_-c_import_secrets;_print(secrets.token_urlsafe(32))
# CLUSTER_REQUEST_TIMEOUT=10.0
# CLUSTER_LOAD_TIMEOUT=300.0
# CLUSTER_HEALTH_INTERVAL=10
# CLUSTER_HEALTH_FAILURES_TO_OFFLINE=3
EOF

    chmod 640 "$CONFIG_FILE"
    chown root:"$SERVICE_USER" "$CONFIG_FILE"

    warn "Configuration générée dans $CONFIG_FILE"
    warn "ADMIN_SECRET = $ADMIN_SECRET — notez-le maintenant."
else
    info "Configuration existante conservée : $CONFIG_FILE"
fi

# ── 6c. Configuration cluster (optionnel, --cluster seulement) ────────────────

AGENT_SECRET_BEFORE="$(deploy_env_value "$CONFIG_FILE" AGENT_SECRET)"
deploy_apply_mode \
    "$EFFECTIVE_MODE" "$CONFIG_FILE" "$CONFIG_DIR" \
    "$SCRIPT_DIR/deploy/nodes.yaml.example"
chmod 640 "$CONFIG_FILE"
chown root:"$SERVICE_USER" "$CONFIG_FILE"

if [[ "$EFFECTIVE_MODE" == "cluster" ]]; then
    NODES_FILE="$(deploy_env_value "$CONFIG_FILE" CLUSTER_NODES_PATH)"
    chown root:"$SERVICE_USER" "$NODES_FILE"
    warn "Mode cluster actif; les fichiers existants et le secret partagé sont conservés."
    if deploy_secret_is_missing "$AGENT_SECRET_BEFORE"; then
        warn "Un AGENT_SECRET initial a été créé dans $CONFIG_FILE; il ne sera pas régénéré."
    fi
    warn "→ Éditez $NODES_FILE, configurez TLS, puis copiez AGENT_SECRET sur CHAQUE agent."
    warn "→ Installez chaque agent séparément; ce script ne modifie aucun nœud distant."
else
    info "Mode local actif : le gateway pilote llama-server sur cet hôte."
fi

# ── 6b. Registre des modèles (models.yaml) ────────────────────────────────────

CONFIGURED_MODELS_FILE="$(deploy_env_value "$CONFIG_FILE" MODELS_CONFIG_PATH)"
LEGACY_MODELS_FILE="$CONFIG_DIR/models.yaml"
MODELS_FILE="${CONFIGURED_MODELS_FILE:-$DATA_DIR/models.yaml}"

# Les mutations admin sont atomiques (tempfile + rename) et exigent donc un
# dossier writable par llmservice. Une ancienne installation sous /etc est
# copiée sans suppression vers /var/lib, puis le chemin env est ajusté.
if [[ "$MODELS_FILE" == "$LEGACY_MODELS_FILE" ]]; then
    MODELS_FILE="$DATA_DIR/models.yaml"
    if [[ -f "$LEGACY_MODELS_FILE" && ! -f "$MODELS_FILE" ]]; then
        cp "$LEGACY_MODELS_FILE" "$MODELS_FILE"
    fi
    deploy_set_env_value "$CONFIG_FILE" MODELS_CONFIG_PATH "$MODELS_FILE"
    warn "Registre migré sans suppression vers $MODELS_FILE pour permettre les mises à jour atomiques."
elif [[ -z "$CONFIGURED_MODELS_FILE" ]]; then
    deploy_set_env_value "$CONFIG_FILE" MODELS_CONFIG_PATH "$MODELS_FILE"
fi

if [[ ! -f "$MODELS_FILE" ]]; then
    info "Installation du registre de modèles initial…"
    cp "$SCRIPT_DIR/models.yaml" "$MODELS_FILE"
    chmod 640 "$MODELS_FILE"
    chown "$SERVICE_USER:$SERVICE_USER" "$MODELS_FILE"
    warn "Registre des modèles installé dans $MODELS_FILE"
    warn "IMPORTANT : vérifiez les chemins 'path' dans ce fichier avant de démarrer."
else
    info "Registre de modèles existant conservé : $MODELS_FILE"
fi

if [[ "$MODELS_FILE" == "$DATA_DIR/"* ]]; then
    chown "$SERVICE_USER:$SERVICE_USER" "$MODELS_FILE"
    chmod 640 "$MODELS_FILE"
else
    warn "Registre personnalisé conservé : vérifiez que llmservice peut écrire dans $(dirname "$MODELS_FILE") pour les mutations admin."
fi

# ── 7. Service systemd ────────────────────────────────────────────────────────

info "Installation du service systemd…"
if [[ "$EFFECTIVE_MODE" == "cluster" ]]; then
    cp "$SCRIPT_DIR/deploy/llm-gateway-cluster.service" /etc/systemd/system/llm-gateway.service
else
    cp "$SCRIPT_DIR/deploy/llm-gateway.service" /etc/systemd/system/llm-gateway.service
fi
systemctl daemon-reload
systemctl enable llm-gateway.service
info "Service systemd installé et activé."

# ── 7b. Timer de sauvegarde quotidienne de la DB ──────────────────────────────
# Le service oneshot exécute /opt/llm-gateway/deploy/llm-gateway-backup.sh ; le
# script doit donc être déployé dans INSTALL_DIR (pas seulement dans le dépôt).

info "Installation du timer de sauvegarde SQLite…"
mkdir -p "$INSTALL_DIR/deploy"
cp "$SCRIPT_DIR/deploy/llm-gateway-backup.sh" "$INSTALL_DIR/deploy/"
chown -R root:"$SERVICE_USER" "$INSTALL_DIR/deploy"
chmod 750 "$INSTALL_DIR/deploy" "$INSTALL_DIR/deploy/llm-gateway-backup.sh"
cp "$SCRIPT_DIR/deploy/llm-gateway-backup.service" /etc/systemd/system/
cp "$SCRIPT_DIR/deploy/llm-gateway-backup.timer"   /etc/systemd/system/
systemctl daemon-reload
# `enable` (sans --now) : planifie le prochain 03:15 sans lancer de backup
# immédiat — la DB n'est initialisée qu'à l'étape 9.
if command -v sqlite3 &>/dev/null; then
    systemctl enable llm-gateway-backup.timer
    info "Timer de sauvegarde activé (quotidien 03:15, rétention 14 j)."
else
    warn "sqlite3 introuvable — timer copié mais NON activé. Après 'apt install sqlite3' :"
    warn "  sudo systemctl enable --now llm-gateway-backup.timer"
fi

# ── 8. Nginx ──────────────────────────────────────────────────────────────────

if command -v nginx &>/dev/null; then
    info "Configuration nginx…"
    cp "$SCRIPT_DIR/deploy/nginx.conf" /etc/nginx/sites-available/llm-gateway
    ln -sf /etc/nginx/sites-available/llm-gateway /etc/nginx/sites-enabled/llm-gateway 2>/dev/null || true

    if nginx -t 2>/dev/null; then
        info "Configuration nginx valide."
    else
        warn "Vérifiez la configuration nginx manuellement (certificat TLS peut-être absent)."
    fi
else
    warn "nginx non trouvé — installer manuellement et copier deploy/nginx.conf"
fi

# ── 8b. Rotation des logs journald ────────────────────────────────────────────
# Réglages GLOBAUX au journal système (journald n'a pas de quota par-service).
# Créé si absent ; jamais écrasé pour préserver un ajustement local (SystemMaxUse
# dépend de l'espace disque de /var/log/journal).

JOURNALD_DROPIN="/etc/systemd/journald.conf.d/llm-gateway.conf"
if [[ ! -f "$JOURNALD_DROPIN" ]]; then
    info "Installation de la rotation journald…"
    mkdir -p /etc/systemd/journald.conf.d
    cp "$SCRIPT_DIR/deploy/journald-llm-gateway.conf" "$JOURNALD_DROPIN"
    systemctl restart systemd-journald
    info "Rotation journald installée (SystemMaxUse=500M, rétention 30 j)."
else
    info "Configuration journald existante conservée : $JOURNALD_DROPIN"
fi

# ── 9. Initialisation de la DB ────────────────────────────────────────────────

info "Initialisation de la base de données…"
cd "$INSTALL_DIR" && \
    DB_PATH="$DATA_DIR/gateway.db" \
    "$INSTALL_DIR/venv/bin/python" -c "
import asyncio, sys
sys.path.insert(0, '.')
import database
asyncio.run(database.init_db())
print('DB initialisée.')
"
chown "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR/gateway.db" 2>/dev/null || true

# ── 10. Résumé ────────────────────────────────────────────────────────────────

echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Installation terminée !${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo ""
echo "  Prochaines étapes :"
echo ""
echo "  Mode installé : $EFFECTIVE_MODE"
echo ""
if [[ "$EFFECTIVE_MODE" == "local" ]]; then
    echo "  1. Télécharger les GGUF sur CET hôte dans /models :"
    echo "     huggingface-cli download bartowski/Llama-3.3-70B-Instruct-GGUF \\"
    echo "       --include '*Q4_K_M*' --local-dir /models/"
    echo ""
    echo "  2. Adapter $MODELS_FILE et le budget VRAM local."
else
    echo "  1. Éditer la topologie et installer chaque node-agent séparément :"
    echo "     sudo nano $(deploy_env_value "$CONFIG_FILE" CLUSTER_NODES_PATH)"
    echo "     sudo bash node_agent/deploy/install-agent.sh --node-id <id> \\"
    echo "       --agent-secret-file /root/evaruntime-agent-secret --orchestrator-cidr <IP>/32"
    echo ""
    echo "  2. Copier les mêmes GGUF, aux mêmes chemins, sur CHAQUE nœud éligible;"
    echo "     adapter $MODELS_FILE sur l'orchestrateur; configurer AGENT_SECRET et TLS."
fi
echo "     sudo nano $CONFIG_FILE"
echo "     sudo nano $MODELS_FILE"
echo ""
echo "  3. Configurer le certificat TLS :"
echo "     sudo certbot certonly --nginx -d llm.eva.univ-pau.fr"
echo "     sudo nano /etc/nginx/sites-available/llm-gateway  # adapter le domaine"
echo ""
echo "  4. Démarrer le service :"
echo "     sudo systemctl start llm-gateway"
echo "     sudo systemctl status llm-gateway"
echo "     sudo journalctl -u llm-gateway -f"
echo ""
echo "  5. Créer le premier utilisateur :"
echo "     cd $INSTALL_DIR"
echo "     sudo -u $SERVICE_USER ./venv/bin/python cli.py add-user alice --email alice@univ-pau.fr"
echo "     sudo -u $SERVICE_USER ./venv/bin/python cli.py create-key alice --name 'these-2025'"
echo ""
echo "  6. Tester :"
echo '     curl -s https://llm.eva.univ-pau.fr/v1/chat/completions \'
echo '       -H "Authorization: Bearer <VOTRE_CLE>" \'
echo '       -H "Content-Type: application/json" \'
echo '       -d '"'"'{"model":"llama-3.3-70b-instruct","messages":[{"role":"user","content":"Bonjour !"}]}'"'"
echo ""
