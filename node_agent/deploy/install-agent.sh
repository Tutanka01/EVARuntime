#!/usr/bin/env bash
# install-agent.sh — Installation du node-agent LLM Gateway sur un DGX Spark
#
# Usage :
#   sudo bash install-agent.sh [--node-id <id>] [--port <port>]
#
# Pré-requis :
#   - Ubuntu 24.04 ARM64 (aarch64)
#   - Python ≥ 3.11
#   - llama-server déjà compilé (voir docs/build-llama-cpp-dgx-spark.md)
#   - AGENT_SECRET partagé avec l'orchestrateur
set -euo pipefail

NODE_ID="${NODE_ID:-node-a}"
AGENT_PORT="${AGENT_PORT:-9443}"
INSTALL_DIR="/opt/llm-gateway"
VENV_DIR="$INSTALL_DIR/venv-agent"
CONF_DIR="/etc/llm-gateway-agent"
TLS_DIR="$CONF_DIR/tls"
LOG_DIR="/var/log/llm-gateway-agent"
SERVICE="llm-gateway-agent"
USER="llmservice"

# ── Parse args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --node-id) NODE_ID="$2"; shift 2 ;;
        --port)    AGENT_PORT="$2"; shift 2 ;;
        *)         echo "Usage: $0 [--node-id ID] [--port PORT]"; exit 1 ;;
    esac
done

echo "=== Installation du node-agent LLM Gateway ==="
echo "  Node ID  : $NODE_ID"
echo "  Port     : $AGENT_PORT"
echo "  Dossier  : $INSTALL_DIR"

# ── Création de l'utilisateur système ────────────────────────────────────────
if ! id "$USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$USER"
    echo "[+] Utilisateur '$USER' créé."
fi

# ── Dossiers ─────────────────────────────────────────────────────────────────
install -d -m 755 -o root -g root       "$INSTALL_DIR"
install -d -m 750 -o root -g "$USER"   "$CONF_DIR" "$TLS_DIR"
install -d -m 755 -o "$USER" -g "$USER" "$LOG_DIR"

# ── Code source ───────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

rsync -a --delete "$REPO_ROOT/node_agent/" "$INSTALL_DIR/node_agent/"
rsync -a --delete "$REPO_ROOT/gateway/"    "$INSTALL_DIR/gateway/"
chown -R "$USER:$USER" "$INSTALL_DIR"
echo "[+] Code source copié dans $INSTALL_DIR."

# ── Environnement virtuel Python ──────────────────────────────────────────────
if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv "$VENV_DIR"
    echo "[+] Venv créé : $VENV_DIR"
fi

"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet \
    fastapi uvicorn[standard] httpx pydantic pydantic-settings pyyaml uvloop
echo "[+] Dépendances Python installées."

# ── Certificat TLS auto-signé (si absent) ────────────────────────────────────
if [[ ! -f "$TLS_DIR/agent.crt" ]]; then
    openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 \
        -nodes \
        -keyout "$TLS_DIR/agent.key" \
        -out    "$TLS_DIR/agent.crt" \
        -subj   "/CN=$NODE_ID" \
        -addext "subjectAltName=DNS:$NODE_ID,DNS:$(hostname -f),IP:$(hostname -I | awk '{print $1}')" \
        2>/dev/null
    chmod 640 "$TLS_DIR/agent.key" "$TLS_DIR/agent.crt"
    chown "$USER:$USER" "$TLS_DIR/agent.key" "$TLS_DIR/agent.crt"
    echo "[+] Certificat TLS auto-signé généré (valable 10 ans)."
    echo "    Copiez $TLS_DIR/agent.crt vers l'orchestrateur pour tls_verify."
else
    echo "[i] Certificat TLS existant conservé."
fi

# ── Fichier de configuration ──────────────────────────────────────────────────
ENV_FILE="$CONF_DIR/env"
if [[ ! -f "$ENV_FILE" ]]; then
    cat > "$ENV_FILE" <<ENVEOF
# Configuration du node-agent — EDITER avant de démarrer le service

NODE_ID=$NODE_ID
AGENT_PORT=$AGENT_PORT
AGENT_HOST=0.0.0.0

# Secret partagé avec l'orchestrateur — DOIT être identique dans nodes.yaml
AGENT_SECRET=CHANGE_ME_GENERATE_WITH_python3_-c_"import_secrets;_print(secrets.token_urlsafe(32))"

# Clé interne orchestrateur → llama-server (peut être différente par nœud)
INTERNAL_API_KEY=CHANGE_ME_INTERNAL_KEY

# Mémoire GPU (GB10 : 120 recommandé sur 128 GB physiques)
TOTAL_VRAM_GB=120.0
VRAM_OVERHEAD_GB=4.0
VRAM_SAFETY_MARGIN=0.03

# Pool de ports llama-server sur ce nœud
BASE_LLAMA_PORT=8081
MAX_LOADED_MODELS=5

# Chemin du binaire llama-server compilé pour sm_121
LLAMA_SERVER_BIN=/usr/local/bin/llama-server

# Répertoires autorisés pour les .gguf (laisser vide = pas de restriction)
ALLOWED_MODEL_DIRS=/models

# GPU (GB10 = device 0)
CUDA_VISIBLE_DEVICES=0

# Logs
LOG_DIR=/var/log/llm-gateway-agent
ENVEOF
    chmod 640 "$ENV_FILE"
    chown "root:$USER" "$ENV_FILE"
    echo "[+] Fichier $ENV_FILE créé — EDITEZ-LE avant de démarrer."
else
    echo "[i] $ENV_FILE existant conservé (mise à jour manuelle requise si nouvelle variable)."
fi

# ── Service systemd ───────────────────────────────────────────────────────────
cp "$INSTALL_DIR/node_agent/deploy/llm-gateway-agent.service" \
   "/etc/systemd/system/$SERVICE.service"

# Injecter le port réel dans le service
sed -i "s/--port 9443/--port $AGENT_PORT/" "/etc/systemd/system/$SERVICE.service"

systemctl daemon-reload
systemctl enable "$SERVICE"
echo "[+] Service systemd '$SERVICE' enregistré (démarrage automatique activé)."

# ── Résumé ────────────────────────────────────────────────────────────────────
cat <<SUMMARY

=== Installation terminée ===

ÉTAPES SUIVANTES :

1. Editez $ENV_FILE :
     - Remplacez AGENT_SECRET par la valeur partagée avec l'orchestrateur
     - Remplacez INTERNAL_API_KEY par une clé unique
     - Vérifiez TOTAL_VRAM_GB et LLAMA_SERVER_BIN

2. Démarrez le service :
     sudo systemctl start $SERVICE
     sudo journalctl -fu $SERVICE   # pour suivre les logs

3. Vérifiez la santé :
     curl -k -H "Authorization: Bearer <AGENT_SECRET>" \\
       https://localhost:$AGENT_PORT/agent/health

4. Sur l'orchestrateur, ajoutez ce nœud dans nodes.yaml :
     nodes:
       - id: $NODE_ID
         base_url: https://$(hostname -f):$AGENT_PORT

5. Copiez le certificat TLS si tls_verify pointe vers le bundle :
     scp $TLS_DIR/agent.crt orchestrateur:/etc/ssl/certs/$NODE_ID.crt
SUMMARY
