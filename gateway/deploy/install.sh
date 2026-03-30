#!/usr/bin/env bash
# install.sh — Installation du LLM Gateway UPPA
# Testé sur : Ubuntu 22.04 / 24.04
#
# Usage :
#   sudo bash install.sh
#
# Ce script est idempotent : le relancer ne casse pas une installation existante.

set -euo pipefail
IFS=$'\n\t'

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()    { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

[[ $EUID -eq 0 ]] || error "Ce script doit être exécuté en root (sudo bash install.sh)"

# ── Configuration ─────────────────────────────────────────────────────────────

INSTALL_DIR="/opt/llm-gateway"
DATA_DIR="/var/lib/llm-gateway"
LOG_DIR="/var/log/llm-gateway"
MODELS_DIR="/models"
CONFIG_DIR="/etc/llm-gateway"
SERVICE_USER="llmservice"
PYTHON="${PYTHON:-python3}"

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

# Accès GPU
usermod -aG render,video "$SERVICE_USER" 2>/dev/null || true

# ── 2. Création des répertoires ───────────────────────────────────────────────

info "Création des répertoires…"
for dir in "$INSTALL_DIR" "$DATA_DIR" "$LOG_DIR" "$CONFIG_DIR"; do
    mkdir -p "$dir"
done
mkdir -p "$MODELS_DIR"

chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR" "$LOG_DIR"
chown -R root:root "$INSTALL_DIR" "$CONFIG_DIR"
chmod 755 "$INSTALL_DIR" "$CONFIG_DIR"
chmod 750 "$DATA_DIR" "$LOG_DIR"

# Modèles : lisibles par le service, pas par les autres
chown -R root:"$SERVICE_USER" "$MODELS_DIR"
chmod -R 750 "$MODELS_DIR"

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
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Copier tous les fichiers Python
cp "$SCRIPT_DIR"/*.py "$INSTALL_DIR/"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"

chown -R root:"$SERVICE_USER" "$INSTALL_DIR"
chmod -R 640 "$INSTALL_DIR"/*.py
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

# ── Chemins ───────────────────────────────────────────────────────────────────
MODEL_PATH=/models/Llama-3.3-70B-Instruct-Q4_K_M.gguf
LLAMA_SERVER_BIN=/usr/local/bin/llama-server
DB_PATH=${DATA_DIR}/gateway.db
LOG_DIR=${LOG_DIR}

# ── Modèle ────────────────────────────────────────────────────────────────────
MODEL_PUBLIC_NAME=llama-3.3-70b-instruct

# ── Paramètres llama-server (optimisés L40S 48GB) ─────────────────────────────
LLAMA_N_GPU_LAYERS=999
LLAMA_CTX_SIZE=32768
LLAMA_PARALLEL=4
LLAMA_BATCH_SIZE=4096
LLAMA_UBATCH_SIZE=512
LLAMA_FLASH_ATTN=true
LLAMA_CACHE_TYPE_K=q8_0
LLAMA_CACHE_TYPE_V=q8_0
LLAMA_THREADS=8
LLAMA_THREADS_HTTP=4
CUDA_VISIBLE_DEVICES=0

# ── Lifecycle ─────────────────────────────────────────────────────────────────
IDLE_TIMEOUT_SECONDS=300
MODEL_LOAD_TIMEOUT_SECONDS=180
IDLE_CHECK_INTERVAL_SECONDS=30

# ── Sécurité (NE PAS PARTAGER) ────────────────────────────────────────────────
INTERNAL_API_KEY=${INTERNAL_KEY}
ADMIN_SECRET=${ADMIN_SECRET}

# ── Réseau ────────────────────────────────────────────────────────────────────
GATEWAY_HOST=127.0.0.1
GATEWAY_PORT=8000
LLAMA_SERVER_HOST=127.0.0.1
LLAMA_SERVER_PORT=8081

# ── Rate limiting par défaut ───────────────────────────────────────────────────
DEFAULT_RPM_LIMIT=20
DEFAULT_MONTHLY_TOKEN_LIMIT=0
EOF

    chmod 640 "$CONFIG_FILE"
    chown root:"$SERVICE_USER" "$CONFIG_FILE"

    warn "Configuration générée dans $CONFIG_FILE"
    warn "IMPORTANT : vérifiez MODEL_PATH et adaptez les paramètres si nécessaire."
    warn "ADMIN_SECRET = $ADMIN_SECRET — notez-le maintenant."
else
    info "Configuration existante conservée : $CONFIG_FILE"
fi

# ── 7. Service systemd ────────────────────────────────────────────────────────

info "Installation du service systemd…"
cp "$SCRIPT_DIR/deploy/llm-gateway.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable llm-gateway.service
info "Service systemd installé et activé."

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
echo "  1. Télécharger le modèle :"
echo "     huggingface-cli download bartowski/Llama-3.3-70B-Instruct-GGUF \\"
echo "       --include '*Q4_K_M*' --local-dir /models/"
echo ""
echo "  2. Adapter la configuration :"
echo "     sudo nano $CONFIG_FILE"
echo ""
echo "  3. Configurer le certificat TLS :"
echo "     sudo certbot certonly --nginx -d llm.univ-pau.fr"
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
echo '     curl -s https://llm.univ-pau.fr/v1/chat/completions \'
echo '       -H "Authorization: Bearer <VOTRE_CLE>" \'
echo '       -H "Content-Type: application/json" \'
echo '       -d '"'"'{"model":"llama-3.3-70b-instruct","messages":[{"role":"user","content":"Bonjour !"}]}'"'"
echo ""
