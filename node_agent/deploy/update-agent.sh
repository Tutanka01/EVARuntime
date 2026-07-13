#!/usr/bin/env bash
# Mise à jour transactionnelle d'un node-agent déjà installé.
# Préserve /etc/llm-gateway-agent, les certificats, secrets, logs et /models.
set -Eeuo pipefail
IFS=$'\n\t'

DRY_RUN=false
PULL_REPOSITORY=true
INSTALL_DIR="/opt/llm-gateway"
VENV_DIR="$INSTALL_DIR/venv-agent"
ENV_FILE="/etc/llm-gateway-agent/env"
SERVICE="llm-gateway-agent"
SERVICE_FILE="/etc/systemd/system/$SERVICE.service"
ROLLBACK_DIR="$INSTALL_DIR/.agent-rollback"
WORK_DIR=""
ROLLBACK_READY=false
WAS_ACTIVE=false

info() { printf '[INFO] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
die()  { printf '[ERREUR] %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Usage: sudo bash update-agent.sh [--dry-run] [--no-pull]

  --dry-run   Exécute les préflights et décrit les actions, sans aucune écriture
  --no-pull   Déploie le checkout courant sans `git pull --ff-only`

La mise à jour construit un venv neuf, sauvegarde la version installée, redémarre
et sonde l'agent. En cas d'échec, code + venv + unité systemd sont restaurés.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true; shift ;;
        --no-pull) PULL_REPOSITORY=false; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "Option inconnue : $1" ;;
    esac
done

[[ $EUID -eq 0 ]] || die "Exécutez ce script avec sudo/root."
for command in python3 rsync systemctl; do
    command -v "$command" >/dev/null || die "Commande requise absente : $command"
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SOURCE_AGENT="$REPO_ROOT/node_agent"
SOURCE_GATEWAY="$REPO_ROOT/gateway"

[[ -f "$SOURCE_AGENT/main.py" && -f "$SOURCE_GATEWAY/server_manager.py" ]] || \
    die "Checkout EVARuntime incomplet : $REPO_ROOT"
[[ -x "$VENV_DIR/bin/python" ]] || die "Installation absente : $VENV_DIR"
[[ -f "$ENV_FILE" ]] || die "Configuration absente : $ENV_FILE"
[[ -f "$SERVICE_FILE" ]] || die "Unité systemd absente : $SERVICE_FILE"

validate_source() {
    bash -n "$SOURCE_AGENT/deploy/install-agent.sh"
    bash -n "$SOURCE_AGENT/deploy/update-agent.sh"
    python3 -c '
import ast, pathlib, sys
for root in map(pathlib.Path, sys.argv[1:]):
    for path in root.rglob("*.py"):
        if ".venv" not in path.parts:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
' "$SOURCE_AGENT" "$SOURCE_GATEWAY"
}

info "Préflight du checkout $REPO_ROOT"
validate_source
"$VENV_DIR/bin/python" "$SOURCE_AGENT/preflight.py" --env "$ENV_FILE"

if [[ "$DRY_RUN" == true ]]; then
    info "DRY-RUN : aucune modification effectuée."
    if [[ "$PULL_REPOSITORY" == true ]]; then
        info "Action prévue : git pull --ff-only dans $REPO_ROOT"
    fi
    info "Actions prévues : venv neuf, sync node_agent+gateway, unité systemd, restart+health."
    info "Rollback prévu : $ROLLBACK_DIR (code + venv + unité précédents)."
    exit 0
fi

if [[ "$PULL_REPOSITORY" == true ]]; then
    command -v git >/dev/null || die "git est requis sans --no-pull."
    git -C "$REPO_ROOT" diff --quiet --ignore-submodules -- || \
        die "Checkout modifié localement; committez/stashez ou utilisez --no-pull."
    git -C "$REPO_ROOT" diff --cached --quiet --ignore-submodules -- || \
        die "Checkout contient des changements indexés; committez/stashez ou utilisez --no-pull."
    BEFORE="$(git -C "$REPO_ROOT" rev-parse --short HEAD)"
    git -C "$REPO_ROOT" pull --ff-only
    AFTER="$(git -C "$REPO_ROOT" rev-parse --short HEAD)"
    info "Checkout mis à jour : $BEFORE -> $AFTER"
    # Le pull peut avoir changé les scripts : revalider ce qui va être déployé.
    validate_source
fi

WORK_DIR="$(mktemp -d "$INSTALL_DIR/.agent-update.XXXXXX")"
cleanup() {
    [[ -n "$WORK_DIR" && -d "$WORK_DIR" ]] && rm -rf "$WORK_DIR"
}

rollback() {
    local original_status="$1"
    warn "Échec de mise à jour; restauration de la version précédente."
    set +e
    systemctl stop "$SERVICE"
    if [[ -d "$ROLLBACK_DIR/node_agent" ]]; then
        rsync -a --delete "$ROLLBACK_DIR/node_agent/" "$INSTALL_DIR/node_agent/"
    fi
    if [[ -d "$ROLLBACK_DIR/gateway" ]]; then
        rsync -a --delete "$ROLLBACK_DIR/gateway/" "$INSTALL_DIR/gateway/"
    fi
    if [[ -d "$ROLLBACK_DIR/venv-agent" ]]; then
        rm -rf "$VENV_DIR"
        mv "$ROLLBACK_DIR/venv-agent" "$VENV_DIR"
    fi
    if [[ -f "$ROLLBACK_DIR/llm-gateway-agent.service" ]]; then
        cp "$ROLLBACK_DIR/llm-gateway-agent.service" "$SERVICE_FILE"
    fi
    systemctl daemon-reload
    if [[ "$WAS_ACTIVE" == true ]]; then
        systemctl start "$SERVICE"
    fi
    set -e
    warn "Rollback terminé. Inspectez : journalctl -u $SERVICE -n 100"
    return "$original_status"
}

on_error() {
    local status=$?
    trap - ERR
    if [[ "$ROLLBACK_READY" == true ]]; then
        rollback "$status" || true
    fi
    cleanup
    exit "$status"
}
trap on_error ERR
trap cleanup EXIT

info "Construction du staging isolé : $WORK_DIR"
rsync -a --delete --delete-excluded \
    --exclude '.env' --exclude '.venv' --exclude '__pycache__' \
    --exclude '.pytest_cache' --exclude '.ruff_cache' --exclude '.DS_Store' \
    "$SOURCE_AGENT/" "$WORK_DIR/node_agent/"
rsync -a --delete --delete-excluded \
    --exclude '.env' --exclude '.venv' --exclude '__pycache__' \
    --exclude '.pytest_cache' --exclude '.ruff_cache' --exclude '.DS_Store' \
    "$SOURCE_GATEWAY/" "$WORK_DIR/gateway/"
python3 -m venv "$WORK_DIR/venv-agent"
"$WORK_DIR/venv-agent/bin/python" -m pip install --quiet --upgrade pip
"$WORK_DIR/venv-agent/bin/python" -m pip install --quiet \
    -r "$WORK_DIR/node_agent/requirements.txt"
"$WORK_DIR/venv-agent/bin/python" -m pip check
"$WORK_DIR/venv-agent/bin/python" "$WORK_DIR/node_agent/preflight.py" --env "$ENV_FILE"

if systemctl is-active --quiet "$SERVICE"; then
    WAS_ACTIVE=true
fi

# Une seule sauvegarde complète et déterministe : la dernière version connue bonne.
rm -rf "$ROLLBACK_DIR"
install -d -m 0700 -o root -g root "$ROLLBACK_DIR"
rsync -a "$INSTALL_DIR/node_agent/" "$ROLLBACK_DIR/node_agent/"
rsync -a "$INSTALL_DIR/gateway/" "$ROLLBACK_DIR/gateway/"
cp "$SERVICE_FILE" "$ROLLBACK_DIR/llm-gateway-agent.service"

ROLLBACK_READY=true
if [[ "$WAS_ACTIVE" == true ]]; then
    systemctl stop "$SERVICE"
fi
mv "$VENV_DIR" "$ROLLBACK_DIR/venv-agent"

mv "$WORK_DIR/venv-agent" "$VENV_DIR"
rsync -a --delete "$WORK_DIR/node_agent/" "$INSTALL_DIR/node_agent/"
rsync -a --delete "$WORK_DIR/gateway/" "$INSTALL_DIR/gateway/"
chown -R root:root "$VENV_DIR" "$INSTALL_DIR/node_agent" "$INSTALL_DIR/gateway"
find "$INSTALL_DIR/node_agent" "$INSTALL_DIR/gateway" -type d -exec chmod 0755 {} +
find "$INSTALL_DIR/node_agent" "$INSTALL_DIR/gateway" -type f -exec chmod 0644 {} +
chmod 0755 "$INSTALL_DIR/node_agent/deploy/"*.sh
install -m 0644 -o root -g root \
    "$INSTALL_DIR/node_agent/deploy/llm-gateway-agent.service" "$SERVICE_FILE"

"$VENV_DIR/bin/python" "$INSTALL_DIR/node_agent/preflight.py" --env "$ENV_FILE"
systemctl daemon-reload

if [[ "$WAS_ACTIVE" == true ]]; then
    systemctl start "$SERVICE"
    HEALTHY=false
    for attempt in 1 2 3 4 5; do
        if "$VENV_DIR/bin/python" "$INSTALL_DIR/node_agent/preflight.py" \
            --env "$ENV_FILE" --check-health --timeout 5; then
            HEALTHY=true
            break
        fi
        warn "Health-check $attempt/5 échoué; nouvelle tentative..."
        sleep 2
    done
    [[ "$HEALTHY" == true ]] || false
else
    info "Service précédemment inactif : état préservé, pas de démarrage automatique."
fi

ROLLBACK_READY=false
info "Mise à jour node-agent réussie. Rollback conservé dans $ROLLBACK_DIR."
info "Secrets, TLS, modèles et configuration n'ont pas été modifiés."
