#!/usr/bin/env bash
# update.sh — Mise à jour transactionnelle de la gateway sur macOS
#
# Usage :
#   bash update.sh [--nginx]
#
# Ce script est idempotent et préserve :
#   - ~/.config/evaruntime/env  (hors clés de mode explicitement demandées)
#   - ~/Library/Application Support/evaruntime/data/gateway.db  (base de données)
#   - ~/Library/Application Support/evaruntime/models/  (modèles GGUF)
#
# Il rafraîchit en revanche les artefacts d'exploitation versionnés : service
# launchd principal. La rotation des logs est gérée par macOS (logrotate natif).
#
# Validation de la version déployée (COR-006) :
#   Une version n'est conservée que si elle SERT réellement, pas seulement si elle
#   répond. Trois contrôles se succèdent :
#     1. `/ready` après le redémarrage (readiness structurelle stricte).
#     2. `deploy-macos/smoke_test.sh` : recette du premier token de bout en bout.

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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=deploy-macos/code-layout-lib.sh
source "$SCRIPT_DIR/deploy-macos/code-layout-lib.sh"
# env-template-lib fournit DEPLOY_HARDENING_KEYS (signal SEC-002), comme la
# copie Linux qui source deploy/env-template-lib.sh dans son update.sh.
# shellcheck source=deploy-macos/env-template-lib.sh
source "$SCRIPT_DIR/deploy-macos/env-template-lib.sh"

UPDATE_NGINX=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --nginx) UPDATE_NGINX=true; shift ;;
        *) echo "Option inconnue : $1" >&2; exit 2 ;;
    esac
done

# ── Configuration macOS ───────────────────────────────────────────────────────

INSTALL_DIR="${EVARUNE_INSTALL_DIR:-$HOME/Library/Application Support/evaruntime/gateway}"
DATA_DIR="${EVARUNE_DATA_DIR:-$HOME/Library/Application Support/evaruntime/data}"
LOG_DIR="${EVARUNE_LOG_DIR:-$HOME/Library/Application Support/evaruntime/logs}"
CONFIG_FILE="$HOME/.config/evaruntime/env"

# ── Préflight : vérifier que le service actuel est fonctionnel ────────────────

section "Vérification du service en cours…"

if ! sudo launchctl list com.evaruntime.gateway &>/dev/null; then
    warn "Service com.evaruntime.gateway non chargé dans launchd."
    warn "→ L'update continuera mais le service ne sera pas redémarré automatiquement."
fi

# ── Durcissements absents d'un environnement antérieur à SEC-002 ──────────────
# Même sémantique que deploy/update.sh : update.sh ne régénère JAMAIS le fichier
# d'environnement. Sur une installation antérieure à SEC-002, ces clés peuvent
# manquer. Elles ne sont PAS ajoutées d'autorité — écrire « CORS_ALLOW_ORIGINS= »
# sur une installation qui sert un client navigateur la casserait en silence,
# au milieu d'une mise à jour. On signale, l'opérateur tranche.
if [[ -f "$CONFIG_FILE" ]]; then
    MISSING_HARDENING=()
    for hardening_key in "${DEPLOY_HARDENING_KEYS[@]}"; do
        if ! grep -qE "^[[:space:]]*${hardening_key}=" "$CONFIG_FILE"; then
            MISSING_HARDENING+=("$hardening_key")
        fi
    done
    if (( ${#MISSING_HARDENING[@]} > 0 )); then
        warn "Durcissements SEC-002 absents de $CONFIG_FILE : ${MISSING_HARDENING[*]}"
        warn "→ Cette mise à jour ne les ajoute pas : les poser sans vous demander"
        warn "  pourrait couper un client navigateur ou refuser le démarrage."
        warn "→ Voir docs/deployment.md §5, puis « bash $SCRIPT_DIR/deploy-macos/doctor-macos.sh »."
    fi
fi

# ── 1. Sauvegarde de la version actuelle ──────────────────────────────────────

section "Sauvegarde de la version actuelle…"

BACKUP_DIR="$HOME/Library/Application Support/evaruntime/backup"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_NAME="pre_update_${TIMESTAMP}"

mkdir -p "$BACKUP_DIR/$BACKUP_NAME"

# Sauvegarder le venv actuel (pour rollback rapide si nécessaire)
if [[ -d "$INSTALL_DIR/venv" ]]; then
    info "Sauvegarde du venv…"
    cp -R "$INSTALL_DIR/venv" "$BACKUP_DIR/$BACKUP_NAME/venv" 2>/dev/null || true
fi

# Sauvegarder les fichiers de configuration (sauf secrets)
if [[ -f "$CONFIG_FILE" ]]; then
    info "Sauvegarde de la configuration…"
    # Le fichier d'env contient ADMIN_SECRET et INTERNAL_API_KEY : la source
    # est en 600, la sauvegarde doit l'être aussi (sinon umask → 644 lisible
    # par tous les comptes locaux).
    cp "$CONFIG_FILE" "$BACKUP_DIR/$BACKUP_NAME/env.bak"
    chmod 600 "$BACKUP_DIR/$BACKUP_NAME/env.bak"
fi

info "Sauvegarde terminée : $BACKUP_DIR/$BACKUP_NAME/"

# ── 2. Synchronisation du code ────────────────────────────────────────────────

section "Synchronisation du code…"

deploy_sync_gateway_code "$SCRIPT_DIR" "$INSTALL_DIR"
if [[ -d "$SCRIPT_DIR/static" ]]; then
    mkdir -p "$INSTALL_DIR/static"
    cp -r "$SCRIPT_DIR/static/." "$INSTALL_DIR/static/"
fi

# Fichiers opérationnels (backup, smoke test, etc.)
info "Copie des fichiers opérationnels…"
deploy_sync_gateway_operational_files "$SCRIPT_DIR" "$INSTALL_DIR"

deploy_set_file_permissions "$INSTALL_DIR"
info "Code synchronisé."

# ── 3. Mise à jour des dépendances Python ────────────────────────────────────

section "Mise à jour des dépendances…"

if [[ -f "$INSTALL_DIR/requirements.lock" ]]; then
    info "Installation des dépendances mises à jour…"
    "$INSTALL_DIR/venv/bin/pip" install --require-hashes \
        -r "$INSTALL_DIR/requirements.lock" --quiet 2>&1 | tail -n5 || true
    info "Dépendances mises à jour."
else
    warn "requirements.lock introuvable — les dépendances ne sont pas modifiées."
fi

# ── 4. Redémarrage du service ────────────────────────────────────────────────

section "Redémarrage du service…"

if sudo launchctl list com.evaruntime.gateway &>/dev/null; then
    info "Arrêt du service…"
    if ! sudo launchctl bootout system/com.evaruntime.gateway 2>/dev/null; then
        warn "Échec de l'arrêt du service via launchctl bootout."
        if ! sudo launchctl unload "/Library/LaunchDaemons/com.evaruntime.gateway.plist" 2>/dev/null; then
            error "Impossible d'arrêter le service. Vérifiez les permissions.";
        fi
    fi
    
    # Petit délai pour libérer le port
    sleep 2
    
    info "Démarrage du service…"
    if ! sudo launchctl bootstrap system "/Library/LaunchDaemons/com.evaruntime.gateway.plist" 2>/dev/null; then
        warn "Échec de launchctl bootstrap. Tentative avec launchctl load…"
        if ! sudo launchctl load "/Library/LaunchDaemons/com.evaruntime.gateway.plist" 2>/dev/null; then
            error "Impossible de démarrer le service. Vérifiez les permissions.";
        fi
    fi
    
    # Attendre que le service soit prêt
    info "Attente du ready state…"
    for i in $(seq 1 30); do
        if curl -s http://127.0.0.1:8000/ready &>/dev/null; then
            info "Service opérationnel après $i secondes."
            break
        fi
        sleep 1
    done
    
    # Vérifier le ready state final
    if ! curl -s http://127.0.0.1:8000/ready &>/dev/null; then
        warn "Le service n'a pas répondu à /ready après 30 secondes."
        warn "→ Consultez les logs : tail -f $LOG_DIR/gateway-error.log"
    else
        info "Service opérationnel — mise à jour réussie !"
    fi
else
    warn "Service non chargé — lancez manuellement :"
    warn "  sudo launchctl load /Library/LaunchDaemons/com.evaruntime.gateway.plist"
fi

# ── 5. Recette du premier token (smoke test) ──────────────────────────────────

section "Recette du premier token…"

# Exécuter la copie de CET arbre : c'est celle qu'install.sh déploie et que cet
# update maintient. Exercer la copie Linux validerait un autre artefact que
# celui réellement déployé (parité deploy/deploy-macos, issue #29).
SMOKE_TEST_SCRIPT="$SCRIPT_DIR/deploy-macos/smoke_test.sh"

if [[ -f "$SMOKE_TEST_SCRIPT" ]]; then
    info "Exécution de la recette du premier token…"
    if bash "$SMOKE_TEST_SCRIPT" --env-file "$CONFIG_FILE"; then
        info "Recette du premier token réussie."
    else
        warn "La recette du premier token a échoué."
        warn "→ La version déployée répond mais ne sert pas. Consultez les logs."
    fi
else
    warn "smoke_test.sh introuvable — la recette est ignorée."
fi

# ── 6. Nginx (optionnel) ─────────────────────────────────────────────────────

if [[ "$UPDATE_NGINX" == true ]] && command -v nginx &>/dev/null; then
    section "Mise à jour de la configuration nginx…"
    
    NGINX_CONF_DIR="$(brew --prefix 2>/dev/null)/etc/nginx/servers"
    mkdir -p "$NGINX_CONF_DIR"
    
    if [[ -f "$SCRIPT_DIR/deploy-macos/nginx.conf.macOS" ]]; then
        cp "$SCRIPT_DIR/deploy-macos/nginx.conf.macOS" "$NGINX_CONF_DIR/llm-gateway"
        info "Configuration nginx copiée dans $NGINX_CONF_DIR/llm-gateway"
        
        if nginx -t 2>/dev/null; then
            info "Configuration nginx valide."
            brew services restart nginx 2>/dev/null || sudo nginx -s reload 2>/dev/null || true
            info "nginx rechargé."
        else
            warn "Vérifiez la configuration nginx manuellement (certificat TLS peut-être absent)."
        fi
    else
        warn "nginx.conf.macOS introuvable — mise à jour nginx ignorée."
    fi
fi

# ── 7. Nettoyage des anciennes sauvegardes ────────────────────────────────────

section "Nettoyage…"

# Garder uniquement les 3 dernières sauvegardes
cd "$BACKUP_DIR" 2>/dev/null || exit 0
backup_count=$(ls -1d pre_update_* 2>/dev/null | wc -l)
if [[ "$backup_count" -gt 3 ]]; then
    remove_count=$((backup_count - 3))
    info "Suppression de $remove_count ancienne(s) sauvegarde(s)…"
    ls -1dt pre_update_* | tail -n "$remove_count" | xargs rm -rf
fi

# ── Résumé ────────────────────────────────────────────────────────────────────

echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Mise à jour terminée !${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo ""
echo "  Sauvegardes conservées : $BACKUP_DIR/"
echo "  Logs : $LOG_DIR/gateway.log"
echo ""
