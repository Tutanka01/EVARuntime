#!/usr/bin/env bash
# install.sh — Installation d'EVARuntime sur macOS (Mac Studio)
# Testé sur : macOS 14+ (Sonoma) / Apple Silicon (M2/M3/M4 Ultra)
#
# Prérequis :
#   - Homebrew installé (https://brew.sh)
#   - Python 3.11+ via Homebrew
#   - llama.cpp compilé et installé via Homebrew (brew install llama.cpp)
#
# Usage :
#   bash install.sh --mode local    # mono-nœud (SEUL mode supporté sur macOS)
#   bash install.sh --dry-run       # affiche le plan sans modifier le système
#
# Ce script est idempotent : le relancer ne casse pas une installation existante.
#
# IMPORTANT : Le mode cluster n'est PAS supporté sur macOS. Seul le mode local
# (mono-nœud) est fonctionnel. Toute tentative d'installer en mode cluster échouera.

set -euo pipefail
IFS=$'\n\t'

# ── Arguments ─────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=deploy-mode-lib.sh
source "$SCRIPT_DIR/deploy-macos/deploy-mode-lib.sh"
# shellcheck source=gpu-preflight-lib.sh
source "$SCRIPT_DIR/deploy-macos/gpu-preflight-lib.sh"
# shellcheck source=env-template-lib.sh
source "$SCRIPT_DIR/deploy-macos/env-template-lib.sh"
# shellcheck source=code-layout-lib.sh
source "$SCRIPT_DIR/deploy-macos/code-layout-lib.sh"

usage() {
    cat <<EOF
Usage: $0 [--mode local] [--dry-run] [-h|--help]

  --mode local       Gateway mono-nœud (SEUL mode supporté sur macOS).
  --dry-run          Affiche le plan sans modifier l'hôte.

Sur macOS, EVARuntime s'installe dans ~/Library/Application Support/evaruntime/
et utilise launchd pour la gestion du service. Homebrew est requis pour nginx
(optionnel) et python3.

Le binaire llama-server doit être installé via Homebrew avant de lancer ce script :
  brew install llama.cpp

IMPORTANT : Le mode cluster n'est PAS supporté sur macOS. Seul le mode local
            (mono-nœud) est fonctionnel.
EOF
}

REQUESTED_MODE="local"
MODE_WAS_EXPLICIT=false
DRY_RUN=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)
            [[ $# -ge 2 ]] || { echo "--mode requiert local" >&2; usage; exit 2; }
            deploy_validate_mode "$2" || { echo "Mode invalide : $2" >&2; usage; exit 2; }
            REQUESTED_MODE="$2"; MODE_WAS_EXPLICIT=true; shift 2 ;;
        --mode=*)
            value="${1#*=}"
            deploy_validate_mode "$value" || { echo "Mode invalide : $value" >&2; usage; exit 2; }
            REQUESTED_MODE="$value"; MODE_WAS_EXPLICIT=true; shift ;;
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

# ── Configuration macOS ───────────────────────────────────────────────────────

INSTALL_DIR="${EVARUNE_INSTALL_DIR:-$HOME/Library/Application Support/evaruntime/gateway}"
DATA_DIR="${EVARUNE_DATA_DIR:-$HOME/Library/Application Support/evaruntime/data}"
LOG_DIR="${EVARUNE_LOG_DIR:-$HOME/Library/Application Support/evaruntime/logs}"
CONFIG_DIR="${EVARUNE_CONFIG_DIR:-$HOME/.config/evaruntime}"
MODELS_DIR="${EVARUNE_MODELS_DIR:-$HOME/Library/Application Support/evaruntime/models}"
SERVICE_USER="$(whoami)"  # macOS : on utilise l'utilisateur courant
PYTHON="${PYTHON:-python3}"
CONFIG_FILE="$CONFIG_DIR/env"
CURRENT_MODE="$(deploy_env_value "$CONFIG_FILE" CLUSTER_MODE)"
EFFECTIVE_MODE="$REQUESTED_MODE"
USER_NAME="$(whoami)"
GROUP_NAME="$(id -gn)"

if [[ -n "$CURRENT_MODE" ]] && ! deploy_validate_mode "$CURRENT_MODE"; then
    error "Valeur CLUSTER_MODE invalide dans $CONFIG_FILE : '$CURRENT_MODE'"
fi
if [[ -n "$CURRENT_MODE" && "$CURRENT_MODE" != "$EFFECTIVE_MODE" ]]; then
    if [[ "$MODE_WAS_EXPLICIT" != true ]]; then
        error "Installation existante en mode $CURRENT_MODE. Relancez avec --mode $CURRENT_MODE."
    fi
fi

echo ""
echo "EVARuntime — préflight installation (macOS)"
echo "  Mode demandé : $EFFECTIVE_MODE"
echo "  Mode existant  : ${CURRENT_MODE:-<aucun>}"
echo "  Configuration  : $CONFIG_FILE"
echo "  Conservation   : env, models.yaml et secrets existants"

if [[ "$DRY_RUN" == true ]]; then
    echo "  Action         : aucune (--dry-run)"
    if [[ -n "$CURRENT_MODE" && "$CURRENT_MODE" != "$EFFECTIVE_MODE" ]]; then
        echo "  Migration      : $CURRENT_MODE → $EFFECTIVE_MODE; l'exécution exigera --mode $CURRENT_MODE"
    fi
    exit 0
fi

# ── Prérequis Homebrew ────────────────────────────────────────────────────────

info "Vérification des prérequis Homebrew…"
if ! command -v brew &>/dev/null; then
    error "Homebrew est requis mais introuvable. Installez-le depuis https://brew.sh"
fi
info "Homebrew OK : $(brew --version | head -n1)"

# ── Prérequis Python ──────────────────────────────────────────────────────────

info "Vérification de Python…"
PYTHON_VERSION=$("$PYTHON" --version 2>&1 | cut -d' ' -f2)
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d'.' -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d'.' -f2)

if [[ "$PYTHON_MAJOR" -lt 3 ]] || [[ "$PYTHON_MAJOR" -eq 3 && "$PYTHON_MINOR" -lt 11 ]]; then
    error "Python 3.11+ requis. Version trouvée : $PYTHON_VERSION"
fi
info "Python $PYTHON_VERSION OK."

# ── Prérequis llama-server ────────────────────────────────────────────────────

LLAMA_BIN="$(deploy_env_value "$CONFIG_FILE" LLAMA_SERVER_BIN)"
LLAMA_BIN="${LLAMA_BIN:-$(brew --prefix 2>/dev/null)/bin/llama-server}"

if [[ ! -x "$LLAMA_BIN" ]]; then
    warn "llama-server introuvable à $LLAMA_BIN."
    warn "→ Installez-le avec : brew install llama.cpp"
    warn "→ Le socle sera installé, mais /ready restera rouge jusqu'à ce que le binaire soit disponible."
fi

# ── Préflight GPU Metal ───────────────────────────────────────────────────────

GPU_VERDICT="$(deploy_gpu_verdict "$EFFECTIVE_MODE" "")" || true
if [[ "$GPU_VERDICT" == "metal-detected" ]]; then
    info "GPU Apple Silicon détecté (Metal) — accélération GPU activée."
else
    warn "Accélération GPU non détectée ($GPU_VERDICT). Les performances seront limitées au CPU."
fi

info "Préflight validé; installation en mode $EFFECTIVE_MODE."

# ── 1. Création des répertoires ───────────────────────────────────────────────

info "Création des répertoires…"
for dir in "$INSTALL_DIR" "$DATA_DIR" "$LOG_DIR" "$CONFIG_DIR"; do
    mkdir -p "$dir"
done
if [[ "$EFFECTIVE_MODE" == "local" ]]; then
    mkdir -p "$MODELS_DIR"
fi
info "Répertoires créés."

# ── 2. Copie du code source ───────────────────────────────────────────────────

info "Copie du code source vers ${INSTALL_DIR}…"
deploy_sync_gateway_code "$SCRIPT_DIR" "$INSTALL_DIR"

# Fichiers statiques (dashboard admin servi par /admin/dashboard)
if [[ -d "$SCRIPT_DIR/static" ]]; then
    mkdir -p "$INSTALL_DIR/static"
    cp -r "$SCRIPT_DIR/static/." "$INSTALL_DIR/static/"
fi

# Fichiers opérationnels (backup, smoke test, etc.)
info "Copie des fichiers opérationnels…"
deploy_sync_gateway_operational_files "$SCRIPT_DIR" "$INSTALL_DIR"

deploy_set_file_permissions "$INSTALL_DIR"

# ── 3. Environnement virtuel Python ──────────────────────────────────────────

info "Création/mise à jour de l'environnement virtuel…"
if [[ ! -d "$INSTALL_DIR/venv" ]]; then
    "$PYTHON" -m venv "$INSTALL_DIR/venv"
fi

"$INSTALL_DIR/venv/bin/pip" install --require-hashes \
    -r "$INSTALL_DIR/requirements.lock" --quiet
info "Dépendances installées."

# ── 4. Fichier de configuration ───────────────────────────────────────────────

CONFIG_FILE="$CONFIG_DIR/env"

if [[ ! -f "$CONFIG_FILE" ]]; then
    info "Génération de la configuration initiale…"

    INTERNAL_KEY=$(python3 -c "import secrets; print('llmgw-internal-' + secrets.token_urlsafe(32))")
    ADMIN_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")

    deploy_render_env_file \
        "$CONFIG_FILE" "$DATA_DIR" "$LOG_DIR" "$CONFIG_DIR" \
        "$MODELS_DIR" "$SCRIPT_DIR/models.yaml" "$INTERNAL_KEY" "$ADMIN_SECRET"

    chmod 600 "$CONFIG_FILE"

    warn "Configuration générée dans $CONFIG_FILE"
    warn "ADMIN_SECRET = $ADMIN_SECRET — notez-le maintenant."
else
    info "Configuration existante conservée : $CONFIG_FILE"
fi

# ── 4b. Symlink .env vers INSTALL_DIR pour pydantic-settings ──────────────────
# pydantic-settings cherche un fichier .env dans le working directory.
# Sur macOS, l'env file est dans ~/.config/evaruntime/env, pas dans INSTALL_DIR.
# On crée un symlink pour que la configuration soit trouvée au démarrage.
if [[ ! -L "$INSTALL_DIR/.env" ]]; then
    info "Création du symlink .env vers la configuration..."
    ln -sf "$CONFIG_FILE" "$INSTALL_DIR/.env"
fi

# ── 5. Registre des modèles (models.yaml) ─────────────────────────────────────

CONFIGURED_MODELS_FILE="$(deploy_env_value "$CONFIG_FILE" MODELS_CONFIG_PATH)"
MODELS_FILE="${CONFIGURED_MODELS_FILE:-$DATA_DIR/models.yaml}"

if [[ ! -f "$MODELS_FILE" ]]; then
    info "Installation du registre de modèles initial…"
    cp "$SCRIPT_DIR/models.yaml" "$MODELS_FILE"
    chmod 640 "$MODELS_FILE"
    warn "Registre des modèles installé dans $MODELS_FILE"
    warn "IMPORTANT : vérifiez les chemins 'path' dans ce fichier avant de démarrer."
else
    info "Registre de modèles existant conservé : $MODELS_FILE"
fi

# ── 6. Service launchd ────────────────────────────────────────────────────────

info "Installation du service launchd…"
PLIST_DIR="/Library/LaunchDaemons"

# Copier le template plist et substituer les variables
info "Génération du plist launchd…"
sed -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" \
    -e "s|@CONFIG_FILE@|$CONFIG_FILE|g" \
    -e "s|@LOG_DIR@|$LOG_DIR|g" \
    -e "s|@USER_NAME@|$USER_NAME|g" \
    -e "s|@GROUP_NAME@|$GROUP_NAME|g" \
    "$SCRIPT_DIR/deploy-macos/com.evaruntime.gateway.plist" \
    | sudo tee "$PLIST_DIR/com.evaruntime.gateway.plist" >/dev/null

# Charger le service dans launchd (nécessite sudo pour LaunchDaemons)
if sudo launchctl load "$PLIST_DIR/com.evaruntime.gateway.plist" 2>&1 | grep -q "failed"; then
    error "Échec du chargement du service launchd. Vérifiez les permissions et les logs.";
fi
info "Service launchd installé et chargé."

# ── 7. Nginx (optionnel) ─────────────────────────────────────────────────────

if command -v nginx &>/dev/null; then
    info "nginx détecté — configuration optionnelle disponible."
    warn "Pour activer nginx comme reverse-proxy :"
    warn "  1. Copier gateway/deploy-macos/nginx.conf.macOS dans /opt/homebrew/etc/nginx/servers/"
    warn "  2. Modifier les chemins de certificat TLS si nécessaire"
    warn "  3. Recharger : brew services restart nginx"
else
    info "nginx non installé — la gateway est accessible directement sur http://127.0.0.1:8000"
fi

# ── 8. Initialisation de la DB ────────────────────────────────────────────────

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

# ── 9. Résumé ────────────────────────────────────────────────────────────────

echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Installation terminée !${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo ""
echo "  Prochaines étapes :"
echo ""
echo "  Mode installé : $EFFECTIVE_MODE"
echo "  Installé dans : $INSTALL_DIR"
echo "  Configuration : $CONFIG_FILE"
echo "  Registre      : $MODELS_FILE"
echo ""
echo "  1. Vérifier le service :"
echo "     launchctl list com.evaruntime.gateway"
echo "     tail -f $LOG_DIR/gateway.log"
echo ""
echo "  2. Tester la gateway :"
echo "     curl http://127.0.0.1:8000/health"
echo ""
echo "  3. Accéder au dashboard admin :"
echo "     open http://127.0.0.1:8000/admin/dashboard"
echo ""
if [[ "$EFFECTIVE_MODE" == "local" ]]; then
    echo "  4. Charger un modèle (après avoir placé les GGUF dans $MODELS_DIR) :"
    echo "     curl -X POST http://127.0.0.1:8000/admin/models/{id}/load \\"
    echo "       -H 'Authorization: Bearer <VOTRE_CLE>'  # depuis $CONFIG_FILE"
fi
echo ""
