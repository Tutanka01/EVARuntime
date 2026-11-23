#!/usr/bin/env bash
# llm-gateway-backup.sh — Sauvegarde périodique de la base SQLite de la gateway.
#
# Linux : lancé par llm-gateway-backup.service (Type=oneshot), déclenché
# quotidiennement par llm-gateway-backup.timer. Peut aussi être lancé à la main :
#   sudo -u llmservice bash /opt/llm-gateway/deploy/llm-gateway-backup.sh
# macOS : pas de planification livrée ; à lancer à la main (utilisateur courant) :
#   bash "$HOME/Library/Application Support/evaruntime/gateway/deploy/llm-gateway-backup.sh"
#
# Source UNIQUE pour les deux plateformes (issue #29) : `deploy-macos/` n'en a
# pas de copie. Seuls les chemins par défaut dépendent de la plateforme,
# choisis à l'exécution (`uname -s`).
#
# Propriétés :
#   - Utilise `sqlite3 .backup` (sûr sur une base WAL active, contrairement à cp).
#   - Écrit un fichier horodaté dans BACKUP_DIR (permissions strictes 600).
#   - Rotation : ne garde que les RETENTION_DAYS derniers jours de sauvegardes.
#   - Idempotent, sans dépendance externe hormis sqlite3.

set -euo pipefail
IFS=$'\n\t'

# ── Configuration (surchargée par l'environnement) ────────────────────────────
# DB_PATH et BACKUP_DIR peuvent être fournis par l'environnement (Linux :
# EnvironmentFile /etc/llm-gateway/env du service). Défauts selon la plateforme
# qui exécute le script (table « Architecture macOS vs Linux », docs/deployment.md).
case "$(uname -s 2>/dev/null || true)" in
    Darwin)
        # HOME peut manquer (job launchd) : lu seulement s'il est défini, pour
        # que DB_PATH/BACKUP_DIR fournis suffisent (voir le contrôle plus bas).
        DEFAULT_STATE_DIR="${HOME:+$HOME/Library/Application Support/evaruntime}"
        DEFAULT_DB_PATH="${DEFAULT_STATE_DIR:+$DEFAULT_STATE_DIR/data/gateway.db}"
        SQLITE_INSTALL_HINT="brew install sqlite"
        ;;
    *)
        DEFAULT_STATE_DIR="/var/lib/llm-gateway"
        DEFAULT_DB_PATH="$DEFAULT_STATE_DIR/gateway.db"
        SQLITE_INSTALL_HINT="apt install sqlite3"
        ;;
esac
DB_PATH="${DB_PATH:-$DEFAULT_DB_PATH}"
BACKUP_DIR="${BACKUP_DIR:-${DEFAULT_STATE_DIR:+$DEFAULT_STATE_DIR/backups}}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"

log()  { echo "[backup] $*"; }
err()  { echo "[backup][ERROR] $*" >&2; exit 1; }

# Un défaut vide ne peut venir que d'un HOME absent sur macOS.
[[ -n "$DB_PATH" ]] || err "HOME n'est pas défini : impossible de déduire DB_PATH par défaut. Fournissez DB_PATH."
[[ -n "$BACKUP_DIR" ]] || err "HOME n'est pas défini : impossible de déduire BACKUP_DIR par défaut. Fournissez BACKUP_DIR."

command -v sqlite3 >/dev/null 2>&1 || err "sqlite3 introuvable ($SQLITE_INSTALL_HINT)."
[[ -f "$DB_PATH" ]] || err "Base introuvable : $DB_PATH"

# `$DEST` est ensuite interpolé DANS une commande sqlite3 délimitée par des
# quotes simples (`.backup '$DEST'`) : une quote dans BACKUP_DIR fermerait la
# chaîne et permettrait d'y ajouter une commande shell sqlite3, le `chmod 600`
# portant alors sur un autre fichier que celui réellement écrit. Le vecteur
# suppose de maîtriser l'environnement (fichier root), donc l'élévation reste
# faible — mais l'interpolation n'a aucune raison d'exister : on refuse la
# valeur plutôt que de la laisser traverser une frontière de quoting.
[[ "$BACKUP_DIR" == /* ]] || err "BACKUP_DIR doit être un chemin absolu : $BACKUP_DIR"
[[ "$BACKUP_DIR" != *"'"* ]] || err "BACKUP_DIR ne doit pas contenir de quote simple : $BACKUP_DIR"
[[ "$BACKUP_DIR" != *$'\n'* ]] || err "BACKUP_DIR ne doit pas contenir de saut de ligne."

# ── Préparation du répertoire de destination ──────────────────────────────────
mkdir -p "$BACKUP_DIR"
chmod 750 "$BACKUP_DIR" 2>/dev/null || true

STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$BACKUP_DIR/gateway-$STAMP.db"

# ── Sauvegarde cohérente (gère le WAL actif) ──────────────────────────────────
# `.backup` produit une copie transactionnellement cohérente même si la gateway
# écrit pendant l'opération. On effectue ensuite un contrôle d'intégrité rapide.
sqlite3 "$DB_PATH" ".backup '$DEST'" || err "Échec du .backup SQLite."
chmod 600 "$DEST"

INTEGRITY="$(sqlite3 "$DEST" 'PRAGMA integrity_check;' 2>/dev/null || echo 'échec')"
if [[ "$INTEGRITY" != "ok" ]]; then
    rm -f "$DEST"
    err "Contrôle d'intégrité de la sauvegarde échoué ($INTEGRITY) — fichier supprimé."
fi

log "Sauvegarde créée : $DEST ($(du -h "$DEST" | cut -f1))"

# ── Rotation : suppression des sauvegardes plus vieilles que RETENTION_DAYS ────
# On ne touche qu'aux fichiers gateway-*.db de ce répertoire.
DELETED=0
while IFS= read -r -d '' old; do
    rm -f "$old"
    DELETED=$((DELETED + 1))
done < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'gateway-*.db' -mtime +"$RETENTION_DAYS" -print0)

log "Rotation : $DELETED sauvegarde(s) de plus de ${RETENTION_DAYS} jours supprimée(s)."
log "Terminé."
