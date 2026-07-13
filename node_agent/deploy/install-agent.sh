#!/usr/bin/env bash
# Installation idempotente du node-agent sur un nœud GPU Ubuntu/systemd.
set -euo pipefail
IFS=$'\n\t'

NODE_ID="${NODE_ID:-node-a}"
AGENT_HOST="${AGENT_HOST:-0.0.0.0}"
AGENT_PORT="${AGENT_PORT:-9443}"
LLAMA_SERVER_HOST="${LLAMA_SERVER_HOST:-0.0.0.0}"
BASE_LLAMA_PORT="${BASE_LLAMA_PORT:-8081}"
MAX_LOADED_MODELS="${MAX_LOADED_MODELS:-5}"
ORCHESTRATOR_CIDR="${ORCHESTRATOR_CIDR:-}"
AGENT_SECRET_FILE=""

INSTALL_DIR="/opt/llm-gateway"
VENV_DIR="$INSTALL_DIR/venv-agent"
CONF_DIR="/etc/llm-gateway-agent"
ENV_FILE="$CONF_DIR/env"
TLS_DIR="$CONF_DIR/tls"
LOG_DIR="/var/log/llm-gateway-agent"
SERVICE="llm-gateway-agent"
SERVICE_USER="llmservice"

info() { printf '[INFO] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
die()  { printf '[ERREUR] %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Usage: sudo bash install-agent.sh [options]

  --node-id ID                 Identifiant présent dans nodes.yaml
  --host IP                    Bind HTTPS de l'agent (défaut 0.0.0.0)
  --port PORT                  Port HTTPS de l'agent (défaut 9443)
  --llama-host IP              Bind data-plane llama-server (défaut 0.0.0.0)
  --base-llama-port PORT       Premier port data-plane (défaut 8081)
  --max-loaded-models N        Taille du pool de ports (défaut 5)
  --orchestrator-cidr CIDR     Source autorisée par UFW (ex. 10.42.0.10/32)
  --agent-secret-file PATH     Lit le secret partagé depuis un fichier root-only

Sans --agent-secret-file, un secret fort est généré dans /etc/llm-gateway-agent/env.
Il n'est jamais affiché automatiquement.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --node-id) NODE_ID="${2:-}"; shift 2 ;;
        --host) AGENT_HOST="${2:-}"; shift 2 ;;
        --port) AGENT_PORT="${2:-}"; shift 2 ;;
        --llama-host) LLAMA_SERVER_HOST="${2:-}"; shift 2 ;;
        --base-llama-port) BASE_LLAMA_PORT="${2:-}"; shift 2 ;;
        --max-loaded-models) MAX_LOADED_MODELS="${2:-}"; shift 2 ;;
        --orchestrator-cidr) ORCHESTRATOR_CIDR="${2:-}"; shift 2 ;;
        --agent-secret-file) AGENT_SECRET_FILE="${2:-}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "Option inconnue : $1" ;;
    esac
done

[[ $EUID -eq 0 ]] || die "Exécutez ce script avec sudo/root."
[[ "$NODE_ID" =~ ^[a-z0-9][a-z0-9._-]{0,62}$ ]] || \
    die "NODE_ID invalide (minuscules/chiffres/._-, 63 caractères max)."
for value in "$AGENT_PORT" "$BASE_LLAMA_PORT" "$MAX_LOADED_MODELS"; do
    [[ "$value" =~ ^[0-9]+$ ]] || die "Ports et capacité doivent être des entiers positifs."
done
(( AGENT_PORT >= 1 && AGENT_PORT <= 65535 )) || die "AGENT_PORT hors plage."
(( BASE_LLAMA_PORT >= 1 && BASE_LLAMA_PORT <= 65535 )) || die "BASE_LLAMA_PORT hors plage."
(( MAX_LOADED_MODELS >= 1 )) || die "MAX_LOADED_MODELS doit être >= 1."
LAST_LLAMA_PORT=$((BASE_LLAMA_PORT + MAX_LOADED_MODELS - 1))
(( LAST_LLAMA_PORT <= 65535 )) || die "La plage data-plane dépasse le port 65535."
if (( AGENT_PORT >= BASE_LLAMA_PORT && AGENT_PORT <= LAST_LLAMA_PORT )); then
    die "Le port agent chevauche la plage data-plane."
fi

for command in python3 rsync openssl systemctl; do
    command -v "$command" >/dev/null || die "Commande requise absente : $command"
done
python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' || \
    die "Python 3.11 ou plus récent est requis."
if [[ -n "$ORCHESTRATOR_CIDR" ]]; then
    python3 -c 'import ipaddress,sys; ipaddress.ip_network(sys.argv[1], strict=False)' \
        "$ORCHESTRATOR_CIDR" || die "CIDR orchestrateur invalide."
fi
for host in "$AGENT_HOST" "$LLAMA_SERVER_HOST"; do
    python3 -c '
import ipaddress, re, sys
value = sys.argv[1]
try:
    ipaddress.ip_address(value)
except ValueError:
    if not re.fullmatch(r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", value):
        raise SystemExit(1)
' "$host" || die "Hôte invalide : $host"
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
[[ -f "$REPO_ROOT/node_agent/main.py" && -f "$REPO_ROOT/gateway/server_manager.py" ]] || \
    die "Dépôt EVARuntime incomplet autour de $SCRIPT_DIR."

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    info "Utilisateur système $SERVICE_USER créé."
fi
GPU_GROUPS=()
for group in render video; do
    if getent group "$group" >/dev/null; then
        usermod -a -G "$group" "$SERVICE_USER"
        GPU_GROUPS+=("$group")
    else
        warn "Groupe GPU '$group' absent; vérifiez les permissions /dev/nvidia* ou /dev/dri/*."
    fi
done
info "Groupes GPU appliqués : ${GPU_GROUPS[*]:-(aucun détecté)}"

install -d -m 0755 -o root -g root "$INSTALL_DIR"
install -d -m 0750 -o root -g "$SERVICE_USER" "$CONF_DIR" "$TLS_DIR"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$LOG_DIR"
install -d -m 0755 -o root -g root /models

rsync -a --delete --delete-excluded \
    --exclude '.env' --exclude '.venv' --exclude '__pycache__' \
    --exclude '.pytest_cache' --exclude '.ruff_cache' --exclude '.DS_Store' \
    "$REPO_ROOT/node_agent/" "$INSTALL_DIR/node_agent/"
rsync -a --delete --delete-excluded \
    --exclude '.env' --exclude '.venv' --exclude '__pycache__' \
    --exclude '.pytest_cache' --exclude '.ruff_cache' --exclude '.DS_Store' \
    "$REPO_ROOT/gateway/" "$INSTALL_DIR/gateway/"
chown -R root:root "$INSTALL_DIR/node_agent" "$INSTALL_DIR/gateway"
find "$INSTALL_DIR/node_agent" "$INSTALL_DIR/gateway" -type d -exec chmod 0755 {} +
find "$INSTALL_DIR/node_agent" "$INSTALL_DIR/gateway" -type f -exec chmod 0644 {} +
chmod 0755 "$INSTALL_DIR/node_agent/deploy/"*.sh
info "Code installé en lecture seule pour le service."

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
"$VENV_DIR/bin/python" -m pip install --quiet -r "$INSTALL_DIR/node_agent/requirements.txt"
chown -R root:root "$VENV_DIR"

if [[ ! -f "$TLS_DIR/agent.crt" || ! -f "$TLS_DIR/agent.key" ]]; then
    PRIMARY_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
    FQDN="$(hostname -f 2>/dev/null || hostname)"
    SAN="DNS:$NODE_ID,DNS:$FQDN"
    if [[ -n "$PRIMARY_IP" ]]; then
        SAN="$SAN,IP:$PRIMARY_IP"
    fi
    openssl req -x509 -newkey rsa:4096 -sha256 -days 825 -nodes \
        -keyout "$TLS_DIR/agent.key" -out "$TLS_DIR/agent.crt" \
        -subj "/CN=$NODE_ID" -addext "subjectAltName=$SAN" >/dev/null 2>&1
    info "Certificat TLS auto-signé généré (825 jours)."
else
    info "Certificat TLS existant conservé."
fi
chown root:"$SERVICE_USER" "$TLS_DIR/agent.key" "$TLS_DIR/agent.crt"
chmod 0640 "$TLS_DIR/agent.key"
chmod 0644 "$TLS_DIR/agent.crt"

generate_secret() {
    python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
}
read_env_value() {
    local key="$1"
    sed -n "s/^${key}=//p" "$ENV_FILE" | tail -n 1
}
append_env_if_missing() {
    local key="$1" value="$2"
    if ! grep -q "^${key}=" "$ENV_FILE"; then
        printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    fi
}
replace_env_value() {
    local key="$1" value="$2"
    sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
}

PROVIDED_AGENT_SECRET=""
if [[ -n "$AGENT_SECRET_FILE" ]]; then
    [[ -f "$AGENT_SECRET_FILE" ]] || die "Fichier de secret introuvable : $AGENT_SECRET_FILE"
    IFS= read -r PROVIDED_AGENT_SECRET < "$AGENT_SECRET_FILE" || true
    [[ ${#PROVIDED_AGENT_SECRET} -ge 32 ]] || die "Le secret fourni doit contenir au moins 32 caractères."
    [[ "$PROVIDED_AGENT_SECRET" =~ ^[A-Za-z0-9._~+/=-]+$ ]] || \
        die "Le secret fourni contient des caractères incompatibles avec EnvironmentFile."
fi

if [[ ! -f "$ENV_FILE" ]]; then
    AGENT_SECRET_VALUE="${PROVIDED_AGENT_SECRET:-$(generate_secret)}"
    INTERNAL_API_KEY_VALUE="$(generate_secret)"
    umask 0027
    cat > "$ENV_FILE" <<EOF
# Node-agent EVARuntime — fichier root-only, compatible systemd EnvironmentFile
NODE_ID=$NODE_ID
AGENT_HOST=$AGENT_HOST
AGENT_PORT=$AGENT_PORT
AGENT_TLS_CERT=$TLS_DIR/agent.crt
AGENT_TLS_KEY=$TLS_DIR/agent.key
AGENT_SECRET=$AGENT_SECRET_VALUE
INTERNAL_API_KEY=$INTERNAL_API_KEY_VALUE

LLAMA_SERVER_BIN=/usr/local/bin/llama-server
LLAMA_SERVER_HOST=$LLAMA_SERVER_HOST
LLAMA_SERVER_HEALTH_HOST=127.0.0.1
BASE_LLAMA_PORT=$BASE_LLAMA_PORT
MAX_LOADED_MODELS=$MAX_LOADED_MODELS
LLAMA_SERVER_MIN_BUILD=0
MODEL_LOAD_TIMEOUT_SECONDS=180
IDLE_CHECK_INTERVAL_SECONDS=10

TOTAL_VRAM_GB=120.0
VRAM_OVERHEAD_GB=4.0
VRAM_SAFETY_MARGIN=0.03
ALLOWED_MODEL_DIRS=/models
CUDA_VISIBLE_DEVICES=0
LOG_DIR=$LOG_DIR
EOF
    info "Configuration créée; deux secrets forts ont été générés."
else
    info "Configuration existante conservée; ajout des nouvelles clés manquantes."
    append_env_if_missing AGENT_HOST "$AGENT_HOST"
    append_env_if_missing AGENT_PORT "$AGENT_PORT"
    append_env_if_missing AGENT_TLS_CERT "$TLS_DIR/agent.crt"
    append_env_if_missing AGENT_TLS_KEY "$TLS_DIR/agent.key"
    append_env_if_missing LLAMA_SERVER_HOST "$LLAMA_SERVER_HOST"
    append_env_if_missing LLAMA_SERVER_HEALTH_HOST 127.0.0.1
    append_env_if_missing IDLE_CHECK_INTERVAL_SECONDS 10
    if [[ -n "$PROVIDED_AGENT_SECRET" ]]; then
        if grep -q '^AGENT_SECRET=' "$ENV_FILE"; then
            replace_env_value AGENT_SECRET "$PROVIDED_AGENT_SECRET"
        else
            append_env_if_missing AGENT_SECRET "$PROVIDED_AGENT_SECRET"
        fi
    fi
    CURRENT_AGENT_SECRET="$(read_env_value AGENT_SECRET)"
    if [[ -z "$CURRENT_AGENT_SECRET" || "$CURRENT_AGENT_SECRET" == CHANGE_ME* ]]; then
        if grep -q '^AGENT_SECRET=' "$ENV_FILE"; then
            replace_env_value AGENT_SECRET "$(generate_secret)"
        else
            append_env_if_missing AGENT_SECRET "$(generate_secret)"
        fi
        info "Placeholder AGENT_SECRET remplacé par un secret généré."
    elif (( ${#CURRENT_AGENT_SECRET} < 32 )); then
        die "AGENT_SECRET existant trop court; fournissez --agent-secret-file pour le remplacer."
    fi
    CURRENT_INTERNAL_KEY="$(read_env_value INTERNAL_API_KEY)"
    if [[ -z "$CURRENT_INTERNAL_KEY" || "$CURRENT_INTERNAL_KEY" == CHANGE_ME* ]]; then
        if grep -q '^INTERNAL_API_KEY=' "$ENV_FILE"; then
            replace_env_value INTERNAL_API_KEY "$(generate_secret)"
        else
            append_env_if_missing INTERNAL_API_KEY "$(generate_secret)"
        fi
        info "Placeholder INTERNAL_API_KEY remplacé par une clé générée."
    elif (( ${#CURRENT_INTERNAL_KEY} < 32 )); then
        die "INTERNAL_API_KEY existante trop courte; corrigez $ENV_FILE."
    fi
fi
chown root:"$SERVICE_USER" "$ENV_FILE"
chmod 0640 "$ENV_FILE"

install -m 0644 -o root -g root \
    "$INSTALL_DIR/node_agent/deploy/llm-gateway-agent.service" \
    "/etc/systemd/system/$SERVICE.service"

"$VENV_DIR/bin/python" "$INSTALL_DIR/node_agent/preflight.py" --env "$ENV_FILE" || \
    die "Préflight échoué; le service n'a pas été démarré. Corrigez la configuration ci-dessus."

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null

if [[ -n "$ORCHESTRATOR_CIDR" ]]; then
    if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
        ufw allow proto tcp from "$ORCHESTRATOR_CIDR" to any port "$AGENT_PORT" \
            comment 'EVARuntime node-agent control' >/dev/null
        ufw allow proto tcp from "$ORCHESTRATOR_CIDR" to any port "$BASE_LLAMA_PORT:$LAST_LLAMA_PORT" \
            comment 'EVARuntime llama data-plane' >/dev/null
        info "UFW limité à $ORCHESTRATOR_CIDR pour control + data-plane."
    else
        warn "UFW absent/inactif : appliquez l'équivalent dans votre firewall réseau."
    fi
else
    warn "Aucun --orchestrator-cidr : aucune règle firewall créée."
    warn "Autorisez uniquement l'orchestrateur vers TCP $AGENT_PORT et $BASE_LLAMA_PORT-$LAST_LLAMA_PORT."
fi

cat <<EOF

Installation node-agent terminée (service activé mais non démarré).

1. Récupérez explicitement le secret pour nodes.yaml, sans le copier dans les logs :
   sudo sed -n 's/^AGENT_SECRET=//p' $ENV_FILE
2. Copiez $TLS_DIR/agent.crt vers le trust store de l'orchestrateur.
3. Vérifiez le firewall puis démarrez :
   sudo systemctl start $SERVICE
4. Contrôlez sans afficher le secret :
   sudo $VENV_DIR/bin/python $INSTALL_DIR/node_agent/preflight.py --env $ENV_FILE --check-health

Data-plane attendu : <IP privée du nœud>:$BASE_LLAMA_PORT-$LAST_LLAMA_PORT
EOF
