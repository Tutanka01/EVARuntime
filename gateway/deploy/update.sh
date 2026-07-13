#!/usr/bin/env bash
# update.sh — Mise à jour transactionnelle de la gateway
#
# Usage (sur le serveur GPU, depuis n'importe quel répertoire) :
#   sudo bash /chemin/vers/repo/gateway/deploy/update.sh
#
# Ce script est idempotent et préserve :
#   - /etc/llm-gateway/env  (hors clés de mode explicitement demandées)
#   - /var/lib/llm-gateway/gateway.db  (base de données)
#   - /models/  (modèles GGUF)
#
# Il rafraîchit en revanche les artefacts d'exploitation versionnés : service
# systemd principal, timer de sauvegarde SQLite et son script. La rotation
# journald est installée si absente (jamais écrasée). Le timer de sauvegarde
# n'est (ré)activé automatiquement que s'il n'a jamais été installé — un timer
# volontairement désactivé par l'opérateur est laissé tel quel.
#
# Il ne régénère jamais un secret existant et ne remplace jamais nodes.yaml.
# Pour mettre à jour aussi nginx : ajouter --nginx en argument.

set -Eeuo pipefail
IFS=$'\n\t'

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
section() { echo -e "\n${CYAN}▶ $*${NC}"; }

# SCRIPT_DIR = gateway/ (un niveau au-dessus de deploy/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=deploy-mode-lib.sh
source "$SCRIPT_DIR/deploy/deploy-mode-lib.sh"

usage() {
    cat <<EOF
Usage: $0 [--mode local|cluster] [--cluster] [--allow-mode-change] [--nginx] [--dry-run]

Sans --mode, le mode présent dans /etc/llm-gateway/env est conservé (local si
la clé est absente). --cluster reste un alias de --mode cluster.
Une migration exige --allow-mode-change. --dry-run ne modifie ni le dépôt ni l'hôte.
EOF
}

UPDATE_NGINX=false
REQUESTED_MODE=""
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
            REQUESTED_MODE="$value"; MODE_WAS_EXPLICIT=true; shift ;;
        --cluster)
            [[ "$MODE_WAS_EXPLICIT" != true || "$REQUESTED_MODE" == "cluster" ]] || { echo "--cluster contredit --mode $REQUESTED_MODE" >&2; exit 2; }
            REQUESTED_MODE="cluster"; MODE_WAS_EXPLICIT=true; shift ;;
        --allow-mode-change) ALLOW_MODE_CHANGE=true; shift ;;
        --nginx) UPDATE_NGINX=true; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Option inconnue : $1" >&2; usage; exit 2 ;;
    esac
done

# Répertoires
INSTALL_DIR="${LLM_GATEWAY_INSTALL_DIR:-/opt/llm-gateway}"
DATA_DIR="${LLM_GATEWAY_DATA_DIR:-/var/lib/llm-gateway}"
CONFIG_DIR="${LLM_GATEWAY_CONFIG_DIR:-/etc/llm-gateway}"
DB_PATH="$DATA_DIR/gateway.db"
BACKUP_DIR="$DATA_DIR/backups"
SERVICE_USER="llmservice"
CONFIG_FILE="$CONFIG_DIR/env"
CURRENT_MODE="$(deploy_env_value "$CONFIG_FILE" CLUSTER_MODE)"
EFFECTIVE_MODE="$(deploy_select_mode "$CONFIG_FILE" "$REQUESTED_MODE")" || exit 1
PREVIOUS_MODE="${CURRENT_MODE:-local}"

if [[ -n "$CURRENT_MODE" && "$CURRENT_MODE" != "$EFFECTIVE_MODE" && "$ALLOW_MODE_CHANGE" != true && "$DRY_RUN" != true ]]; then
    error "Migration $CURRENT_MODE → $EFFECTIVE_MODE non confirmée. Vérifiez avec --dry-run puis ajoutez --allow-mode-change."
fi

echo ""
echo "EVARuntime — préflight mise à jour"
echo "  Mode demandé : ${REQUESTED_MODE:-<auto>}"
echo "  Mode existant  : ${CURRENT_MODE:-<absent; local par défaut>}"
echo "  Mode effectif  : $EFFECTIVE_MODE"
echo "  Conservation   : env, models.yaml, nodes.yaml, secrets, DB et GGUF"

if [[ "$DRY_RUN" == true ]]; then
    echo "  Action         : aucune (--dry-run; pas de git pull, pip, systemd ou écriture)"
    if [[ -n "$CURRENT_MODE" && "$CURRENT_MODE" != "$EFFECTIVE_MODE" ]]; then
        echo "  Migration      : $CURRENT_MODE → $EFFECTIVE_MODE; l'exécution exigera --allow-mode-change"
    fi
    [[ "$EFFECTIVE_MODE" == "cluster" ]] && echo "  Agents         : à mettre à jour séparément sur chaque nœud"
    exit 0
fi

[[ $EUID -eq 0 ]] || error "Ce script doit être exécuté en root : sudo bash update.sh"

[[ -d "$INSTALL_DIR" ]] || error "$INSTALL_DIR n'existe pas — lancez d'abord install.sh"
[[ -f "$INSTALL_DIR/venv/bin/python" ]] || error "venv introuvable — lancez d'abord install.sh"
[[ -f "$CONFIG_FILE" ]] || error "Configuration introuvable : $CONFIG_FILE"
for required in awk chmod chown cp curl find git mkdir mktemp mv systemctl; do
    command -v "$required" &>/dev/null || error "Préflight : commande requise introuvable : $required"
done
if [[ "$EFFECTIVE_MODE" == "cluster" ]]; then
    [[ -f "$SCRIPT_DIR/deploy/llm-gateway-cluster.service" ]] || error "Préflight : unité orchestrateur introuvable"
else
    command -v nvidia-smi &>/dev/null || error "Préflight local : nvidia-smi introuvable"
    LLAMA_BIN="$(deploy_env_value "$CONFIG_FILE" LLAMA_SERVER_BIN)"
    [[ -x "${LLAMA_BIN:-/usr/local/bin/llama-server}" ]] || error "Préflight local : llama-server non exécutable (${LLAMA_BIN:-/usr/local/bin/llama-server})"
fi
info "Préflight validé; mise à jour en mode $EFFECTIVE_MODE."

prepare_model_registry() {
    local configured_models_file legacy_models_file
    configured_models_file="$(deploy_env_value "$CONFIG_FILE" MODELS_CONFIG_PATH)"
    legacy_models_file="$CONFIG_DIR/models.yaml"
if [[ -z "$configured_models_file" || "$configured_models_file" == "$legacy_models_file" ]]; then
    MODELS_FILE="$DATA_DIR/models.yaml"
    if [[ ! -f "$MODELS_FILE" ]]; then
        if [[ -f "$legacy_models_file" ]]; then
            cp "$legacy_models_file" "$MODELS_FILE"
        else
            cp "$SCRIPT_DIR/models.yaml" "$MODELS_FILE"
        fi
    fi
    chown "$SERVICE_USER:$SERVICE_USER" "$MODELS_FILE"
    chmod 640 "$MODELS_FILE"
    deploy_set_env_value "$CONFIG_FILE" MODELS_CONFIG_PATH "$MODELS_FILE"
    warn "Registre copié sans suppression vers $MODELS_FILE pour permettre les mutations admin atomiques."
else
    MODELS_FILE="$configured_models_file"
    if [[ "$MODELS_FILE" != "$DATA_DIR/"* ]]; then
        warn "Registre personnalisé conservé : vérifiez que llmservice peut écrire dans $(dirname "$MODELS_FILE")."
    fi
fi
}

install_gateway_service_unit() {
    local mode="$1"
    if [[ "$mode" == "cluster" ]]; then
        cp "$SCRIPT_DIR/deploy/llm-gateway-cluster.service" /etc/systemd/system/llm-gateway.service
    else
        cp "$SCRIPT_DIR/deploy/llm-gateway.service" /etc/systemd/system/llm-gateway.service
    fi
}

restore_previous_service_unit() {
    local fallback_mode="$1"
    if [[ -f "${UNIT_SNAPSHOT:-}" ]]; then
        cp "$UNIT_SNAPSHOT" /etc/systemd/system/llm-gateway.service
        chmod 644 /etc/systemd/system/llm-gateway.service
    else
        install_gateway_service_unit "$fallback_mode"
    fi
}

restore_code_snapshot() {
    local snapshot="$1"
    cp "$snapshot"/*.py "$INSTALL_DIR/"
    cp "$snapshot/requirements.txt" "$INSTALL_DIR/"
    rm -rf "$INSTALL_DIR/cluster" "$INSTALL_DIR/static"
    [[ ! -d "$snapshot/cluster" ]] || cp -a "$snapshot/cluster" "$INSTALL_DIR/cluster"
    [[ ! -d "$snapshot/static" ]] || cp -a "$snapshot/static" "$INSTALL_DIR/static"
    rm -rf "$INSTALL_DIR/__pycache__" "$INSTALL_DIR/cluster/__pycache__"
    chown root:"$SERVICE_USER" "$INSTALL_DIR"/*.py "$INSTALL_DIR/requirements.txt"
    [[ ! -d "$INSTALL_DIR/cluster" ]] || chown -R root:"$SERVICE_USER" "$INSTALL_DIR/cluster"
    [[ ! -d "$INSTALL_DIR/static" ]] || chown -R root:"$SERVICE_USER" "$INSTALL_DIR/static"
    chmod 640 "$INSTALL_DIR"/*.py
    chmod 644 "$INSTALL_DIR/requirements.txt"
}

VENV_SWITCHED=false
PREVIOUS_VENV_TARGET=""
TRANSACTION_ARMED=false
CODE_MUTATED=false
MODE_ACTIVATED=false
SERVICE_RESTART_STARTED=false

activate_staged_venv() {
    if [[ -L "$INSTALL_DIR/venv" ]]; then
        PREVIOUS_VENV_TARGET="$(readlink -f "$INSTALL_DIR/venv")"
        rm -f "$INSTALL_DIR/venv"
    else
        PREVIOUS_VENV_TARGET="$INSTALL_DIR/venv-pre-update-$(date +%Y%m%d-%H%M%S)"
        mv "$INSTALL_DIR/venv" "$PREVIOUS_VENV_TARGET"
    fi
    # Armer le rollback dès que l'ancien chemin a été retiré. Si la création du
    # nouveau symlink échoue, le trap peut ainsi restaurer l'ancien venv.
    VENV_SWITCHED=true
    ln -s "$STAGED_VENV" "$INSTALL_DIR/venv"
}

rollback_venv() {
    [[ "$VENV_SWITCHED" == true ]] || return 0
    rm -f "$INSTALL_DIR/venv"
    ln -s "$PREVIOUS_VENV_TARGET" "$INSTALL_DIR/venv"
    VENV_SWITCHED=false
}

rollback_failed_transaction() {
    local exit_code="$1"
    [[ "$TRANSACTION_ARMED" == true ]] || return "$exit_code"

    trap - ERR
    set +e
    warn "Erreur avant validation finale : rollback transactionnel automatique."
    deploy_set_env_value "$CONFIG_FILE" CLUSTER_MODE "${PREVIOUS_MODE:-local}"
    rollback_venv
    if [[ "$CODE_MUTATED" == true ]]; then
        restore_code_snapshot "$CODE_SNAPSHOT"
    fi
    restore_previous_service_unit "${PREVIOUS_MODE:-local}"
    systemctl daemon-reload
    if [[ "$SERVICE_RESTART_STARTED" == true ]]; then
        systemctl start llm-gateway
    fi
    warn "Code, venv, mode et unité précédents restaurés. Snapshot : $CODE_SNAPSHOT"
    exit "$exit_code"
}

echo ""
echo -e "${CYAN}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  LLM Gateway — Mise à jour du code${NC}"
echo -e "${CYAN}═══════════════════════════════════════════════════════════════${NC}"
echo "  Repo  : $REPO_DIR"
echo "  Cible : $INSTALL_DIR"
echo ""

# ── 1. git pull ───────────────────────────────────────────────────────────────

section "1/5  Mise à jour du dépôt git"
cd "$REPO_DIR"

BEFORE=$(git rev-parse HEAD)
if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
    error "Checkout Git modifié : committez ou stash-ez les changements avant la mise à jour."
fi
git pull --ff-only
AFTER=$(git rev-parse HEAD)

if [[ "$BEFORE" == "$AFTER" ]]; then
    warn "Aucune nouvelle version disponible (HEAD = ${AFTER:0:8})."
    warn "Le déploiement continue quand même (dépendances ou static peut-être modifiés)."
else
    info "Mise à jour : ${BEFORE:0:8} → ${AFTER:0:8}"
    git log --oneline "$BEFORE".."$AFTER"
fi

# Snapshot du code réellement déployé. Le rollback ne modifie jamais le
# checkout Git de l'opérateur et ne le laisse pas en detached HEAD.
CODE_SNAPSHOT="$BACKUP_DIR/code-pre-update-$(date +%Y%m%d-%H%M%S)-${BEFORE:0:8}"
mkdir -p "$CODE_SNAPSHOT"
cp "$INSTALL_DIR"/*.py "$CODE_SNAPSHOT/"
cp "$INSTALL_DIR/requirements.txt" "$CODE_SNAPSHOT/"
[[ ! -d "$INSTALL_DIR/cluster" ]] || cp -a "$INSTALL_DIR/cluster" "$CODE_SNAPSHOT/cluster"
[[ ! -d "$INSTALL_DIR/static" ]] || cp -a "$INSTALL_DIR/static" "$CODE_SNAPSHOT/static"
UNIT_SNAPSHOT="$CODE_SNAPSHOT/llm-gateway.service"
[[ ! -f /etc/systemd/system/llm-gateway.service ]] || cp -a /etc/systemd/system/llm-gateway.service "$UNIT_SNAPSHOT"
chmod -R go-rwx "$CODE_SNAPSHOT"
info "Snapshot de rollback du code : $CODE_SNAPSHOT"
TRANSACTION_ARMED=true
trap 'rollback_failed_transaction $?' ERR

section "Préparation transactionnelle des dépendances Python"
STAGED_VENV="$INSTALL_DIR/venv-release-${AFTER:0:12}-$(date +%Y%m%d%H%M%S)"
"$INSTALL_DIR/venv/bin/python" -m venv "$STAGED_VENV"
"$STAGED_VENV/bin/pip" install --upgrade pip --quiet
"$STAGED_VENV/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" --quiet
"$STAGED_VENV/bin/pip" check
info "Venv neuf validé : $STAGED_VENV (l'ancien reste actif jusqu'au redémarrage)."
if [[ "$EFFECTIVE_MODE" == "cluster" ]]; then
    deploy_apply_mode \
        cluster "$CONFIG_FILE" "$CONFIG_DIR" \
        "$SCRIPT_DIR/deploy/nodes.yaml.example"
    NODES_FILE="$(deploy_env_value "$CONFIG_FILE" CLUSTER_NODES_PATH)"
    chown root:"$SERVICE_USER" "$NODES_FILE"
    if ! "$STAGED_VENV/bin/python" -c \
        'import sys; from pathlib import Path; sys.path.insert(0, sys.argv[2]); from cluster.nodes_config import load_nodes_config; cfg = load_nodes_config(Path(sys.argv[1])); print(f"Topologie valide: {len(cfg.nodes)} nœud(s)")' \
        "$NODES_FILE" "$SCRIPT_DIR"; then
        deploy_set_env_value "$CONFIG_FILE" CLUSTER_MODE "$PREVIOUS_MODE"
        error "Topologie cluster invalide. Le mode $PREVIOUS_MODE est conservé; corrigez $NODES_FILE puis relancez."
    fi
    if [[ "$PREVIOUS_MODE" != "cluster" ]]; then
        deploy_set_env_value "$CONFIG_FILE" CLUSTER_MODE "$PREVIOUS_MODE"
    fi
    info "Topologie cluster validée avant toute synchronisation du code."
fi
prepare_model_registry

# ── 2. Synchronisation du code Python ─────────────────────────────────────────

section "2/5  Synchronisation du code source"
CODE_MUTATED=true
cp "$SCRIPT_DIR"/*.py "$INSTALL_DIR/"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"

# Package cluster/ — requis en CLUSTER_MODE=cluster (importé par model_manager)
mkdir -p "$INSTALL_DIR/cluster"
cp "$SCRIPT_DIR/cluster"/*.py "$INSTALL_DIR/cluster/"

# Purger le bytecode obsolète (modules renommés ou supprimés entre versions)
rm -rf "$INSTALL_DIR/__pycache__" "$INSTALL_DIR/cluster/__pycache__"

chown root:"$SERVICE_USER" "$INSTALL_DIR"/*.py "$INSTALL_DIR/requirements.txt"
chown -R root:"$SERVICE_USER" "$INSTALL_DIR/cluster"
chmod 640 "$INSTALL_DIR"/*.py "$INSTALL_DIR/cluster"/*.py
chmod 750 "$INSTALL_DIR/cluster"
chmod 644 "$INSTALL_DIR/requirements.txt"

info "Fichiers Python copiés (gateway + cluster/)."

# ── 3. Synchronisation des fichiers statiques ─────────────────────────────────

section "3/5  Synchronisation des fichiers statiques"
if [[ -d "$SCRIPT_DIR/static" ]]; then
    mkdir -p "$INSTALL_DIR/static"
    cp -r "$SCRIPT_DIR/static/." "$INSTALL_DIR/static/"
    chown -R root:"$SERVICE_USER" "$INSTALL_DIR/static"
    find "$INSTALL_DIR/static" -type d -exec chmod 755 {} \;
    find "$INSTALL_DIR/static" -type f -exec chmod 644 {} \;
    info "Fichiers statiques copiés ($(find "$SCRIPT_DIR/static" -type f | wc -l) fichiers)."
else
    info "Aucun répertoire static/ dans le dépôt — rien à copier."
fi

# ── 4. Mise à jour des dépendances Python ────────────────────────────────────

section "4/5  Dépendances Python validées"
info "Le venv staged a passé pip check; permutation au redémarrage."

apply_selected_mode() {
section "Activation du mode $EFFECTIVE_MODE"
deploy_apply_mode \
    "$EFFECTIVE_MODE" "$CONFIG_FILE" "$CONFIG_DIR" \
    "$SCRIPT_DIR/deploy/nodes.yaml.example"
chmod 640 "$CONFIG_FILE"
chown root:"$SERVICE_USER" "$CONFIG_FILE"

info "Mode $EFFECTIVE_MODE activé."
}

# ── 4b. Mise à jour nginx (optionnel) ────────────────────────────────────────

if [[ "$UPDATE_NGINX" == true ]]; then
    section "4b. Mise à jour nginx"
    if command -v nginx &>/dev/null; then
        cp "$SCRIPT_DIR/deploy/nginx.conf" /etc/nginx/sites-available/llm-gateway
        if nginx -t 2>/dev/null; then
            nginx -s reload
            info "nginx rechargé."
        else
            warn "Erreur de configuration nginx — rechargement annulé. Vérifiez manuellement."
        fi
    else
        warn "nginx non trouvé — ignoré."
    fi
fi

# ── 4c. Sauvegarde de la base de données AVANT redémarrage ────────────────────
# On sauvegarde AVANT de redémarrer : si le nouveau code migre/altère le schéma
# au démarrage, on garde une copie cohérente de l'état d'avant la mise à jour.
# `sqlite3 .backup` gère correctement une base WAL active (contrairement à un
# simple `cp` qui peut capturer un WAL incohérent).

section "4c. Sauvegarde de la base de données"
BACKUP_FILE=""
if [[ -f "$DB_PATH" ]]; then
    if command -v sqlite3 &>/dev/null; then
        mkdir -p "$BACKUP_DIR"
        chown "$SERVICE_USER:$SERVICE_USER" "$BACKUP_DIR" 2>/dev/null || true
        chmod 750 "$BACKUP_DIR"
        BACKUP_FILE="$BACKUP_DIR/gateway-pre-update-$(date +%Y%m%d-%H%M%S).db"
        # .backup est atomique et sûr sur une base WAL active
        if sqlite3 "$DB_PATH" ".backup '$BACKUP_FILE'"; then
            chown "$SERVICE_USER:$SERVICE_USER" "$BACKUP_FILE" 2>/dev/null || true
            chmod 600 "$BACKUP_FILE"
            info "Sauvegarde DB : $BACKUP_FILE ($(du -h "$BACKUP_FILE" | cut -f1))"
        else
            warn "Échec de la sauvegarde SQLite — la mise à jour continue quand même."
            warn "Vérifiez manuellement la base $DB_PATH avant de poursuivre."
            BACKUP_FILE=""
        fi
    else
        warn "sqlite3 introuvable — sauvegarde DB ignorée (installer : apt install sqlite3)."
    fi
else
    info "Pas de base de données à sauvegarder ($DB_PATH absent)."
fi

# ── 4d. Artefacts d'exploitation (timer de sauvegarde + rotation journald) ────
# Rafraîchis à chaque mise à jour, comme le service principal. On respecte le
# choix de l'opérateur : le timer n'est activé automatiquement que s'il n'a jamais
# été installé (première mise à jour depuis cette version) ; s'il a été désactivé
# délibérément on n'y retouche pas. La conf journald (globale, souvent ajustée au
# disque local) est créée si absente mais jamais écrasée.

section "4d. Timer de sauvegarde + rotation journald"

# État AVANT copie : distingue « jamais installé » de « désactivé volontairement ».
BACKUP_TIMER_STATE="$(systemctl is-enabled llm-gateway-backup.timer 2>/dev/null || true)"

mkdir -p "$INSTALL_DIR/deploy"
cp "$SCRIPT_DIR/deploy/llm-gateway-backup.sh" "$INSTALL_DIR/deploy/"
chown -R root:"$SERVICE_USER" "$INSTALL_DIR/deploy"
chmod 750 "$INSTALL_DIR/deploy" "$INSTALL_DIR/deploy/llm-gateway-backup.sh"
cp "$SCRIPT_DIR/deploy/llm-gateway-backup.service" /etc/systemd/system/
cp "$SCRIPT_DIR/deploy/llm-gateway-backup.timer"   /etc/systemd/system/
systemctl daemon-reload

case "$BACKUP_TIMER_STATE" in
    enabled)
        info "Timer de sauvegarde déjà actif — unités rafraîchies."
        ;;
    disabled|masked)
        warn "Timer de sauvegarde présent mais désactivé (choix opérateur) — laissé tel quel."
        warn "  Réactiver : sudo systemctl enable --now llm-gateway-backup.timer"
        ;;
    *)
        # Vide/introuvable = jamais installé (première mise à jour depuis cette version).
        if command -v sqlite3 &>/dev/null; then
            systemctl enable llm-gateway-backup.timer
            info "Timer de sauvegarde quotidienne activé (03:15, rétention 14 j)."
        else
            warn "sqlite3 introuvable — timer copié mais NON activé."
            warn "  apt install sqlite3 && sudo systemctl enable --now llm-gateway-backup.timer"
        fi
        ;;
esac

# Rotation journald : créée si absente, jamais écrasée (peut être ajustée localement).
JOURNALD_DROPIN="/etc/systemd/journald.conf.d/llm-gateway.conf"
if [[ ! -f "$JOURNALD_DROPIN" ]]; then
    mkdir -p /etc/systemd/journald.conf.d
    cp "$SCRIPT_DIR/deploy/journald-llm-gateway.conf" "$JOURNALD_DROPIN"
    systemctl restart systemd-journald
    info "Rotation journald installée (SystemMaxUse=500M, rétention 30 j)."
else
    info "Rotation journald déjà présente — conservée ($JOURNALD_DROPIN)."
fi

# ── 5. Mise à jour du service systemd + redémarrage ──────────────────────────

section "5/5  Redémarrage du service"
apply_selected_mode
MODE_ACTIVATED=true
install_gateway_service_unit "$EFFECTIVE_MODE"
systemctl daemon-reload

# Arrêt propre (laisse le temps à llama-server de se terminer)
info "Arrêt du service (max 30s)…"
SERVICE_RESTART_STARTED=true
systemctl stop llm-gateway || true
activate_staged_venv

info "Démarrage du service…"
systemctl start llm-gateway || warn "systemctl start a échoué; la readiness déclenchera le rollback."

# ── Attente du health check ───────────────────────────────────────────────────

echo -n "  Attente du démarrage"
HEALTHY=false
for i in $(seq 1 20); do
    sleep 2
    echo -n "."
    if curl -sf http://127.0.0.1:8000/ready > /dev/null 2>&1; then
        HEALTHY=true
        break
    fi
done
echo ""

if [[ "$HEALTHY" == true ]]; then
    HEALTH=$(curl -s http://127.0.0.1:8000/ready)
    info "Service prêt : $HEALTH"
else
    TRANSACTION_ARMED=false
    trap - ERR
    # ── Rollback automatique ──────────────────────────────────────────────────
    # Le service n'est pas devenu ready. Code, venv, unité et mode reviennent au
    # snapshot précédent; la DB n'est jamais restaurée sans arbitrage humain.
    warn "Le service ne répond pas après $((20 * 2))s."

    if [[ "$PREVIOUS_MODE" != "$EFFECTIVE_MODE" ]]; then
        section "ROLLBACK  Mode $EFFECTIVE_MODE → $PREVIOUS_MODE"
        deploy_set_env_value "$CONFIG_FILE" CLUSTER_MODE "$PREVIOUS_MODE"
        rollback_venv
        restore_code_snapshot "$CODE_SNAPSHOT"
        restore_previous_service_unit "$PREVIOUS_MODE"
        systemctl daemon-reload
        systemctl stop llm-gateway || true
        systemctl start llm-gateway || true
        for i in $(seq 1 20); do
            sleep 2
            if curl -sf http://127.0.0.1:8000/ready > /dev/null 2>&1; then
                error "Migration de mode échouée; le mode $PREVIOUS_MODE a été restauré et le service est sain."
            fi
        done
        error "Migration de mode et rollback ont échoué. Intervention requise : journalctl -u llm-gateway -n 100"
    fi

    section "ROLLBACK  Restauration du snapshot déployé"
    rollback_venv
    restore_code_snapshot "$CODE_SNAPSHOT"
    restore_previous_service_unit "$EFFECTIVE_MODE"
    systemctl daemon-reload
    systemctl stop llm-gateway || true
    systemctl start llm-gateway || true

    ROLLBACK_OK=false
    for i in $(seq 1 20); do
        sleep 2
        if curl -sf http://127.0.0.1:8000/ready > /dev/null 2>&1; then
            ROLLBACK_OK=true
            break
        fi
    done

    if [[ "$ROLLBACK_OK" == true ]]; then
        warn "Rollback réussi depuis $CODE_SNAPSHOT; le checkout Git est resté intact."
        warn "La version ${AFTER:0:8} n'est pas déployée; investiguez avant de réessayer."
        [[ -n "$BACKUP_FILE" ]] && warn "Sauvegarde DB pré-update : $BACKUP_FILE"
        exit 1
    else
        error "Rollback ÉCHOUÉ. Intervention requise : sudo journalctl -u llm-gateway -n 100 --no-pager"
    fi
fi

TRANSACTION_ARMED=false
trap - ERR

# ── Vérification des secrets ──────────────────────────────────────────────────
# Les routes /admin répondent 503 tant qu'ADMIN_SECRET est vide ou CHANGE_ME_*.
if grep -qE '^(ADMIN_SECRET|INTERNAL_API_KEY|AGENT_SECRET)=(CHANGE_ME|[[:space:]]*$)' "$CONFIG_FILE" 2>/dev/null; then
    warn "Des secrets non configurés (CHANGE_ME_* ou vides) subsistent dans $CONFIG_FILE."
    warn "Les routes /admin restent DÉSACTIVÉES (503) tant qu'ADMIN_SECRET n'est pas défini."
    warn "Générer : python3 -c \"import secrets; print(secrets.token_urlsafe(32))\""
fi

# ── Résumé ────────────────────────────────────────────────────────────────────

echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Mise à jour terminée  ($(date '+%Y-%m-%d %H:%M:%S'))${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo ""
echo "  Version déployée : $(git -C "$REPO_DIR" log -1 --format='%h  %s  (%cr)')"
echo "  Mode déployé    : $EFFECTIVE_MODE"
echo ""
echo "  Commandes utiles :"
echo "    sudo journalctl -u llm-gateway -f          # logs en temps réel"
echo "    sudo systemctl status llm-gateway          # état du service"
echo "    curl http://127.0.0.1:8000/health          # santé de l'API"
echo "    curl http://127.0.0.1:8000/ready           # gate de readiness production"
if [[ "$EFFECTIVE_MODE" == "cluster" ]]; then
    echo ""
    echo "  IMPORTANT : update.sh ne met pas les nœuds à jour à distance."
    echo "  Exécutez node_agent/deploy/update-agent.sh sur chaque agent."
fi
echo ""
