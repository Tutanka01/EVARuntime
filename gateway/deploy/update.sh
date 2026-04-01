#!/usr/bin/env bash
# update.sh — Mise à jour du code de la gateway (sans toucher à la config ni à la DB)
#
# Usage (sur le serveur GPU, depuis n'importe quel répertoire) :
#   sudo bash /chemin/vers/repo/gateway/deploy/update.sh
#
# Ce script est idempotent et ne touche PAS à :
#   - /etc/llm-gateway/env  (config + secrets)
#   - /var/lib/llm-gateway/gateway.db  (base de données)
#   - /models/  (modèles GGUF)
#
# Pour mettre à jour aussi nginx : ajouter --nginx en argument.

set -euo pipefail
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

[[ $EUID -eq 0 ]] || error "Ce script doit être exécuté en root : sudo bash update.sh"

UPDATE_NGINX=false
for arg in "$@"; do
    [[ "$arg" == "--nginx" ]] && UPDATE_NGINX=true
done

# Répertoires
INSTALL_DIR="/opt/llm-gateway"
SERVICE_USER="llmservice"
# SCRIPT_DIR = gateway/  (un niveau au-dessus de deploy/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

[[ -d "$INSTALL_DIR" ]] || error "$INSTALL_DIR n'existe pas — lancez d'abord install.sh"
[[ -f "$INSTALL_DIR/venv/bin/python" ]] || error "venv introuvable — lancez d'abord install.sh"

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
git pull
AFTER=$(git rev-parse HEAD)

if [[ "$BEFORE" == "$AFTER" ]]; then
    warn "Aucune nouvelle version disponible (HEAD = ${AFTER:0:8})."
    warn "Le déploiement continue quand même (dépendances ou static peut-être modifiés)."
else
    info "Mise à jour : ${BEFORE:0:8} → ${AFTER:0:8}"
    git log --oneline "$BEFORE".."$AFTER"
fi

# ── 2. Synchronisation du code Python ─────────────────────────────────────────

section "2/5  Synchronisation du code source"
cp "$SCRIPT_DIR"/*.py "$INSTALL_DIR/"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"

chown root:"$SERVICE_USER" "$INSTALL_DIR"/*.py "$INSTALL_DIR/requirements.txt"
chmod 640 "$INSTALL_DIR"/*.py
chmod 644 "$INSTALL_DIR/requirements.txt"

info "Fichiers Python copiés."

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

section "4/5  Mise à jour des dépendances Python"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip --quiet
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" --quiet
info "Dépendances à jour."

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

# ── 5. Mise à jour du service systemd + redémarrage ──────────────────────────

section "5/5  Redémarrage du service"
cp "$SCRIPT_DIR/deploy/llm-gateway.service" /etc/systemd/system/
systemctl daemon-reload

# Arrêt propre (laisse le temps à llama-server de se terminer)
info "Arrêt du service (max 30s)…"
systemctl stop llm-gateway || true

info "Démarrage du service…"
systemctl start llm-gateway

# ── Attente du health check ───────────────────────────────────────────────────

echo -n "  Attente du démarrage"
HEALTHY=false
for i in $(seq 1 20); do
    sleep 2
    echo -n "."
    if curl -sf http://127.0.0.1:8000/health > /dev/null 2>&1; then
        HEALTHY=true
        break
    fi
done
echo ""

if [[ "$HEALTHY" == true ]]; then
    HEALTH=$(curl -s http://127.0.0.1:8000/health)
    info "Service opérationnel : $HEALTH"
else
    warn "Le service ne répond pas encore. Vérifiez :"
    warn "  sudo journalctl -u llm-gateway -f --since now"
fi

# ── Résumé ────────────────────────────────────────────────────────────────────

echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Mise à jour terminée  ($(date '+%Y-%m-%d %H:%M:%S'))${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo ""
echo "  Version déployée : $(git -C "$REPO_DIR" log -1 --format='%h  %s  (%cr)')"
echo ""
echo "  Commandes utiles :"
echo "    sudo journalctl -u llm-gateway -f          # logs en temps réel"
echo "    sudo systemctl status llm-gateway          # état du service"
echo "    curl http://127.0.0.1:8000/health          # santé de l'API"
echo ""
