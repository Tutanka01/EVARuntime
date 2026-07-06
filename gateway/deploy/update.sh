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
DATA_DIR="/var/lib/llm-gateway"
DB_PATH="$DATA_DIR/gateway.db"
BACKUP_DIR="$DATA_DIR/backups"
SERVICE_USER="llmservice"
# SCRIPT_DIR = gateway/  (un niveau au-dessus de deploy/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

[[ -d "$INSTALL_DIR" ]] || error "$INSTALL_DIR n'existe pas — lancez d'abord install.sh"
[[ -f "$INSTALL_DIR/venv/bin/python" ]] || error "venv introuvable — lancez d'abord install.sh"

# ── Fonction : synchronise le code Python + static du dépôt vers INSTALL_DIR ──
# Utilisée par le flux normal ET par le rollback (après git checkout). Idempotente.
sync_code() {
    cp "$SCRIPT_DIR"/*.py "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"

    mkdir -p "$INSTALL_DIR/cluster"
    cp "$SCRIPT_DIR/cluster"/*.py "$INSTALL_DIR/cluster/"

    rm -rf "$INSTALL_DIR/__pycache__" "$INSTALL_DIR/cluster/__pycache__"

    chown root:"$SERVICE_USER" "$INSTALL_DIR"/*.py "$INSTALL_DIR/requirements.txt"
    chown -R root:"$SERVICE_USER" "$INSTALL_DIR/cluster"
    chmod 640 "$INSTALL_DIR"/*.py "$INSTALL_DIR/cluster"/*.py
    chmod 750 "$INSTALL_DIR/cluster"
    chmod 644 "$INSTALL_DIR/requirements.txt"

    if [[ -d "$SCRIPT_DIR/static" ]]; then
        mkdir -p "$INSTALL_DIR/static"
        cp -r "$SCRIPT_DIR/static/." "$INSTALL_DIR/static/"
        chown -R root:"$SERVICE_USER" "$INSTALL_DIR/static"
        find "$INSTALL_DIR/static" -type d -exec chmod 755 {} \;
        find "$INSTALL_DIR/static" -type f -exec chmod 644 {} \;
    fi

    "$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" --quiet
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
    # ── Rollback automatique ──────────────────────────────────────────────────
    # Le service n'est pas devenu healthy après les tentatives. On revient à la
    # version d'avant (BEFORE) SEULEMENT si le code a réellement changé. On ne
    # restaure PAS la DB automatiquement (la sauvegarde 4c reste disponible pour
    # une restauration manuelle si nécessaire) : le schéma peut avoir évolué et
    # écraser la base sans arbitrage humain serait plus risqué qu'utile.
    warn "Le service ne répond pas après $((20 * 2))s."

    if [[ "$BEFORE" == "$AFTER" ]]; then
        warn "Aucun changement de code à annuler (HEAD inchangé)."
        warn "Diagnostiquez : sudo journalctl -u llm-gateway -f --since now"
    else
        section "ROLLBACK  Retour à la version précédente (${BEFORE:0:8})"
        if git -C "$REPO_DIR" checkout --quiet "$BEFORE"; then
            info "Dépôt ramené à ${BEFORE:0:8}. Re-déploiement du code précédent…"
            sync_code
            cp "$SCRIPT_DIR/deploy/llm-gateway.service" /etc/systemd/system/
            systemctl daemon-reload
            systemctl stop llm-gateway || true
            systemctl start llm-gateway

            echo -n "  Attente du démarrage (rollback)"
            ROLLBACK_OK=false
            for i in $(seq 1 20); do
                sleep 2
                echo -n "."
                if curl -sf http://127.0.0.1:8000/health > /dev/null 2>&1; then
                    ROLLBACK_OK=true
                    break
                fi
            done
            echo ""

            if [[ "$ROLLBACK_OK" == true ]]; then
                warn "Rollback réussi : le service est reparti sur ${BEFORE:0:8}."
                warn "La mise à jour vers ${AFTER:0:8} a ÉCHOUÉ — investiguez avant de réessayer."
                warn "Note : le dépôt est en 'detached HEAD' sur ${BEFORE:0:8}."
                warn "  Pour repartir de la branche : git -C \"$REPO_DIR\" checkout main"
                [[ -n "$BACKUP_FILE" ]] && warn "Sauvegarde DB pré-update disponible : $BACKUP_FILE"
            else
                error "Rollback ÉCHOUÉ : le service ne démarre ni en neuf ni en ancien. Intervention manuelle requise. Logs : sudo journalctl -u llm-gateway -n 100 --no-pager"
            fi
        else
            error "git checkout $BEFORE a échoué (dépôt sale ?). Rollback manuel requis. Logs : sudo journalctl -u llm-gateway -f"
        fi
    fi
fi

# ── Vérification des secrets ──────────────────────────────────────────────────
# Les routes /admin répondent 503 tant qu'ADMIN_SECRET est vide ou CHANGE_ME_*.
if grep -qE '^(ADMIN_SECRET|INTERNAL_API_KEY|AGENT_SECRET)=(CHANGE_ME|[[:space:]]*$)' /etc/llm-gateway/env 2>/dev/null; then
    warn "Des secrets non configurés (CHANGE_ME_* ou vides) subsistent dans /etc/llm-gateway/env."
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
echo ""
echo "  Commandes utiles :"
echo "    sudo journalctl -u llm-gateway -f          # logs en temps réel"
echo "    sudo systemctl status llm-gateway          # état du service"
echo "    curl http://127.0.0.1:8000/health          # santé de l'API"
echo ""
