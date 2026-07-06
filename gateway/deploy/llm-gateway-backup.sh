#!/usr/bin/env bash
# llm-gateway-backup.sh — Sauvegarde périodique de la base SQLite de la gateway.
#
# Conçu pour être lancé par llm-gateway-backup.service (Type=oneshot), déclenché
# quotidiennement par llm-gateway-backup.timer. Peut aussi être lancé à la main :
#   sudo -u llmservice bash /opt/llm-gateway/deploy/llm-gateway-backup.sh
#
# Propriétés :
#   - Utilise `sqlite3 .backup` (sûr sur une base WAL active, contrairement à cp).
#   - Écrit un fichier horodaté dans BACKUP_DIR (permissions strictes 600).
#   - Rotation : ne garde que les RETENTION_DAYS derniers jours de sauvegardes.
#   - Idempotent, sans dépendance externe hormis sqlite3.

set -euo pipefail
IFS=$'\n\t'

# ── Configuration (surchargée par l'environnement / l'EnvironmentFile) ────────
# DB_PATH et BACKUP_DIR peuvent être fournis par /etc/llm-gateway/env.
DB_PATH="${DB_PATH:-/var/lib/llm-gateway/gateway.db}"
BACKUP_DIR="${BACKUP_DIR:-/var/lib/llm-gateway/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"

log()  { echo "[backup] $*"; }
err()  { echo "[backup][ERROR] $*" >&2; exit 1; }

command -v sqlite3 >/dev/null 2>&1 || err "sqlite3 introuvable (apt install sqlite3)."
[[ -f "$DB_PATH" ]] || err "Base introuvable : $DB_PATH"

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
