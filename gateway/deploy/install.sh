#!/usr/bin/env bash
# install.sh — Installation d'EVARuntime
# Testé sur : Ubuntu 22.04 / 24.04
#
# Usage :
#   sudo bash install.sh --mode local    # mono-nœud (défaut)
#   sudo bash install.sh --mode cluster  # orchestrateur multi-nœuds (opt-in)
#   bash install.sh --mode cluster --dry-run
#
# Ce script est idempotent : le relancer ne casse pas une installation existante.

set -euo pipefail
IFS=$'\n\t'

# ── Arguments ─────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=deploy-mode-lib.sh
source "$SCRIPT_DIR/deploy/deploy-mode-lib.sh"
# shellcheck source=nginx-lib.sh
source "$SCRIPT_DIR/deploy/nginx-lib.sh"
# shellcheck source=gpu-preflight-lib.sh
source "$SCRIPT_DIR/deploy/gpu-preflight-lib.sh"
# shellcheck source=env-template-lib.sh
source "$SCRIPT_DIR/deploy/env-template-lib.sh"
# shellcheck source=code-layout-lib.sh
source "$SCRIPT_DIR/deploy/code-layout-lib.sh"

usage() {
    cat <<EOF
Usage: $0 [--mode local|cluster] [--cluster] [--allow-mode-change] [--allow-no-gpu] [--dry-run]

  --mode local       Gateway mono-nœud (défaut sur une installation neuve).
  --mode cluster     Orchestrateur multi-nœuds; les agents s'installent à part.
  --cluster          Alias historique de --mode cluster.
  --allow-mode-change
                     Confirme une migration d'une installation existante.
  --allow-no-gpu     Mode local sur un hôte SANS GPU NVIDIA. Assume l'absence au
                     lieu de la subir : le choix est inscrit dans
                     $GPU_WAIVER_ENV_KEY du fichier d'environnement généré et
                     remonté par « evaruntime doctor ».
  --dry-run          Affiche le mode et le plan sans modifier l'hôte.

Sans option, install.sh choisit local. Sur une installation existante, indiquez
toujours le mode existant; aucun changement de mode n'est implicite.
EOF
}

REQUESTED_MODE="local"
MODE_WAS_EXPLICIT=false
ALLOW_MODE_CHANGE=false
ALLOW_NO_GPU=false
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
            REQUESTED_MODE="$value"
            MODE_WAS_EXPLICIT=true; shift ;;
        --cluster)
            [[ "$MODE_WAS_EXPLICIT" != true || "$REQUESTED_MODE" == "cluster" ]] || { echo "--cluster contredit --mode $REQUESTED_MODE" >&2; exit 2; }
            REQUESTED_MODE="cluster"; MODE_WAS_EXPLICIT=true; shift ;;
        --allow-mode-change) ALLOW_MODE_CHANGE=true; shift ;;
        --allow-no-gpu) ALLOW_NO_GPU=true; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Option inconnue : $1" >&2; usage; exit 2 ;;
    esac
done

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()    { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── Démarrages systemd (COR-017) ──────────────────────────────────────────────
# Une unité qui a échoué plusieurs fois de suite atteint son start-limit :
# systemd refuse alors TOUT démarrage (« Start request repeated too quickly »)
# tant que le compteur n'est pas remis à zéro. `reset-failed` remet ce compteur
# à zéro; sur une unité saine c'est un no-op, donc sans risque avant chaque
# démarrage. Toute (re)mise en marche d'une unité passe par ces fonctions.
systemctl_restart() {
    local unit="$1"
    systemctl reset-failed "$unit" 2>/dev/null || true
    systemctl restart "$unit"
}

# `enable --now` = enable + start : il arme réellement le timer (OPS-008).
systemctl_enable_now() {
    local unit="$1"
    systemctl reset-failed "$unit" 2>/dev/null || true
    systemctl enable --now "$unit"
}

# ── Commandes exigées par le préflight (OPS-011) ──────────────────────────────
# Source de vérité UNIQUE et déclarative des dépendances de commandes de ce
# script. `docs/deployment.md` §1 doit les lister toutes, et
# `gateway/tests/test_deploy_required_commands.py` DÉRIVE la liste attendue de
# ces tableaux au lieu de la recopier : ajouter une commande ici suffit pour que
# le test exige sa documentation. Corollaire, vérifié par le même test : aucun
# `command -v <nom littéral>` ne doit exister ailleurs dans ce script, sinon la
# dépendance échappe à la documentation — c'est exactement ainsi que `rsync`
# avait disparu des prérequis du node-agent.
INSTALL_REQUIRED_COMMANDS=(awk chmod chown cp find id mkdir mktemp mv python3 systemctl useradd)
# Mode local uniquement : l'orchestrateur cluster ne touche pas aux groupes GPU.
INSTALL_REQUIRED_COMMANDS_LOCAL=(usermod)
# Mode local SANS --allow-no-gpu. Séparée des précédentes parce que c'est la
# seule dépendance à laquelle l'opérateur peut renoncer explicitement (OPS-012) ;
# la sonde et le message de refus vivent dans deploy/gpu-preflight-lib.sh.
INSTALL_REQUIRED_COMMANDS_GPU=(nvidia-smi)
# Absentes, ces commandes ne bloquent pas l'installation mais désactivent une
# fonction : `nginx` (reverse-proxy) et `sqlite3` (armement du timer de backup).
INSTALL_OPTIONAL_COMMANDS=(nginx sqlite3)

# ── Configuration ─────────────────────────────────────────────────────────────

INSTALL_DIR="${LLM_GATEWAY_INSTALL_DIR:-/opt/llm-gateway}"
DATA_DIR="${LLM_GATEWAY_DATA_DIR:-/var/lib/llm-gateway}"
LOG_DIR="${LLM_GATEWAY_LOG_DIR:-/var/log/llm-gateway}"
MODELS_DIR="${LLM_GATEWAY_MODELS_DIR:-/models}"
CONFIG_DIR="${LLM_GATEWAY_CONFIG_DIR:-/etc/llm-gateway}"
SERVICE_USER="llmservice"
PYTHON="${PYTHON:-python3}"
CONFIG_FILE="$CONFIG_DIR/env"
CURRENT_MODE="$(deploy_env_value "$CONFIG_FILE" CLUSTER_MODE)"
EFFECTIVE_MODE="$REQUESTED_MODE"

if [[ -n "$CURRENT_MODE" ]] && ! deploy_validate_mode "$CURRENT_MODE"; then
    error "Valeur CLUSTER_MODE invalide dans $CONFIG_FILE : '$CURRENT_MODE'"
fi
if [[ -n "$CURRENT_MODE" && "$CURRENT_MODE" != "$EFFECTIVE_MODE" ]]; then
    if [[ "$MODE_WAS_EXPLICIT" != true ]]; then
        error "Installation existante en mode $CURRENT_MODE. Relancez avec --mode $CURRENT_MODE (aucune migration implicite)."
    fi
    if [[ "$ALLOW_MODE_CHANGE" != true && "$DRY_RUN" != true ]]; then
        error "Migration $CURRENT_MODE → $EFFECTIVE_MODE non confirmée. Vérifiez le plan avec --dry-run puis ajoutez --allow-mode-change."
    fi
fi

echo ""
echo "EVARuntime — préflight installation"
echo "  Mode demandé : $EFFECTIVE_MODE"
echo "  Mode existant  : ${CURRENT_MODE:-<aucun>}"
echo "  Configuration  : $CONFIG_FILE"
echo "  Conservation   : env, models.yaml, nodes.yaml et secrets existants"

if [[ "$DRY_RUN" == true ]]; then
    echo "  Action         : aucune (--dry-run)"
    if [[ -n "$CURRENT_MODE" && "$CURRENT_MODE" != "$EFFECTIVE_MODE" ]]; then
        echo "  Migration      : $CURRENT_MODE → $EFFECTIVE_MODE; l'exécution exigera --allow-mode-change"
    fi
    if [[ "$EFFECTIVE_MODE" == "cluster" ]]; then
        echo "  Parcours       : orchestrateur sans GPU local; agents, TLS et ports inter-nœuds à configurer séparément"
    else
        echo "  Parcours       : llama-server et modèles sur cet hôte"
        if [[ "$ALLOW_NO_GPU" == true ]]; then
            echo "  GPU            : absence ASSUMÉE (--allow-no-gpu) → $GPU_WAIVER_ENV_KEY=true dans $CONFIG_FILE"
        else
            echo "  GPU            : exigé; sans nvidia-smi le préflight refusera (échappatoire : --allow-no-gpu)"
        fi
    fi
    exit 0
fi

[[ $EUID -eq 0 ]] || error "Ce script doit être exécuté en root (sudo bash install.sh)"

required=()
required+=("${INSTALL_REQUIRED_COMMANDS[@]}")
if [[ "$EFFECTIVE_MODE" != "cluster" ]]; then
    required+=("${INSTALL_REQUIRED_COMMANDS_LOCAL[@]}")
fi
for command_name in "${required[@]}"; do
    # `python3` est le nom DOCUMENTÉ ; PYTHON= substitue l'interpréteur contrôlé
    # sans changer la dépendance annoncée.
    [[ "$command_name" == python3 ]] && command_name="$PYTHON"
    command -v "$command_name" &>/dev/null || \
        error "Préflight : commande requise introuvable : $command_name (cf. docs/deployment.md §1)"
done
[[ -f "$SCRIPT_DIR/requirements.txt" && -f "$SCRIPT_DIR/requirements.lock" ]] || \
    error "Préflight : requirements.txt/requirements.lock introuvable"
[[ -f "$SCRIPT_DIR/deploy/llm-gateway.service" ]] || error "Préflight : unité systemd locale introuvable"
if [[ "$EFFECTIVE_MODE" == "cluster" ]]; then
    [[ -f "$SCRIPT_DIR/deploy/llm-gateway-cluster.service" ]] || error "Préflight : unité systemd orchestrateur introuvable"
    [[ -f "$SCRIPT_DIR/deploy/nodes.yaml.example" ]] || error "Préflight : template nodes.yaml introuvable"
else
    LLAMA_BIN="$(deploy_env_value "$CONFIG_FILE" LLAMA_SERVER_BIN)"
    LLAMA_BIN="${LLAMA_BIN:-/opt/llama.cpp/current/llama-server}"
    if [[ ! -x "$LLAMA_BIN" ]]; then
        warn "Runtime llama-server encore absent ($LLAMA_BIN)."
        warn "→ Le socle sera installé, mais /ready restera rouge jusqu'à bootstrap-apply."
        warn "→ /health et les routes d'administration resteront disponibles pour l'amorçage."
        warn "→ Pour le parcours local direct, compiler llama.cpp avant de relancer ce script."
    fi
fi

# Verdict GPU (OPS-012). Sans --allow-no-gpu, le refus historique est conservé;
# la bibliothèque écrit alors la conduite à tenir sur stderr avant de rendre 1.
GPU_VERDICT="$(deploy_gpu_verdict "$EFFECTIVE_MODE" "$ALLOW_NO_GPU" "${INSTALL_REQUIRED_COMMANDS_GPU[0]}")" || \
    error "Préflight local : hôte sans GPU, aucune échappatoire demandée (conduite à tenir ci-dessus)."
if [[ "$GPU_VERDICT" == "waived" ]]; then
    warn "Hôte SANS GPU, absence assumée par --allow-no-gpu."
    warn "→ Inscrit dans $CONFIG_FILE ($GPU_WAIVER_ENV_KEY=true) et remonté par « evaruntime doctor »."
    warn "→ Aucun modèle ne sera offloadé sur GPU; adaptez TOTAL_VRAM_GB et les attentes de latence."
elif [[ "$ALLOW_NO_GPU" == true && "$GPU_VERDICT" == "detected" ]]; then
    warn "--allow-no-gpu ignoré : un GPU est bien détecté sur cet hôte."
elif [[ "$ALLOW_NO_GPU" == true && "$GPU_VERDICT" == "delegated" ]]; then
    warn "--allow-no-gpu sans objet en mode cluster : l'orchestrateur n'a jamais de GPU local."
fi
info "Préflight validé; installation en mode $EFFECTIVE_MODE."

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

# L'orchestrateur cluster n'accède jamais au GPU local.
if [[ "$EFFECTIVE_MODE" == "local" ]]; then
    usermod -aG render,video "$SERVICE_USER" 2>/dev/null || true
fi

# ── 2. Création des répertoires ───────────────────────────────────────────────

info "Création des répertoires…"
for dir in "$INSTALL_DIR" "$DATA_DIR" "$LOG_DIR" "$CONFIG_DIR"; do
    mkdir -p "$dir"
done
if [[ "$EFFECTIVE_MODE" == "local" ]]; then
    mkdir -p "$MODELS_DIR"
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR" "$LOG_DIR"
chown -R root:root "$INSTALL_DIR" "$CONFIG_DIR"
chmod 755 "$INSTALL_DIR" "$CONFIG_DIR"
chmod 750 "$DATA_DIR" "$LOG_DIR"

# Modèles locaux : lisibles par le service, pas par les autres.
if [[ "$EFFECTIVE_MODE" == "local" ]]; then
    chown -R root:"$SERVICE_USER" "$MODELS_DIR"
    chmod -R 750 "$MODELS_DIR"
fi

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
deploy_sync_gateway_code "$SCRIPT_DIR" "$INSTALL_DIR"

# Fichiers statiques (dashboard admin servi par /admin/dashboard)
if [[ -d "$SCRIPT_DIR/static" ]]; then
    mkdir -p "$INSTALL_DIR/static"
    cp -r "$SCRIPT_DIR/static/." "$INSTALL_DIR/static/"
fi

chown -R root:"$SERVICE_USER" "$INSTALL_DIR"
chmod -R 640 "$INSTALL_DIR"/*.py
chmod 640 "$INSTALL_DIR/cluster"/*.py
chmod 640 "$INSTALL_DIR/bootstrap"/*.py "$INSTALL_DIR/bootstrap/catalog.yaml"
chmod 750 "$INSTALL_DIR/cluster" "$INSTALL_DIR/bootstrap"
if [[ -d "$INSTALL_DIR/static" ]]; then
    find "$INSTALL_DIR/static" -type d -exec chmod 755 {} \;
    find "$INSTALL_DIR/static" -type f -exec chmod 644 {} \;
fi
chmod 644 "$INSTALL_DIR/requirements.txt" "$INSTALL_DIR/requirements.lock"

# ── 5. Environnement virtuel Python ──────────────────────────────────────────

info "Création/mise à jour de l'environnement virtuel…"
if [[ ! -d "$INSTALL_DIR/venv" ]]; then
    "$PYTHON" -m venv "$INSTALL_DIR/venv"
fi

"$INSTALL_DIR/venv/bin/pip" install --require-hashes \
    -r "$INSTALL_DIR/requirements.lock" --quiet
info "Dépendances installées."

# ── 6. Fichier de configuration ───────────────────────────────────────────────

CONFIG_FILE="$CONFIG_DIR/env"

if [[ ! -f "$CONFIG_FILE" ]]; then
    info "Génération de la configuration initiale…"

    INTERNAL_KEY=$(python3 -c "import secrets; print('llmgw-internal-' + secrets.token_urlsafe(32))")
    ADMIN_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")

    # Le rendu vit dans deploy/env-template-lib.sh : il porte les trois
    # durcissements de SEC-002 et il est exerçable en test, hors root.
    deploy_render_env_file \
        "$CONFIG_FILE" "$DATA_DIR" "$LOG_DIR" "$CONFIG_DIR" \
        "$MODELS_DIR" "$SCRIPT_DIR/models.yaml" "$INTERNAL_KEY" "$ADMIN_SECRET"

    chmod 640 "$CONFIG_FILE"
    chown root:"$SERVICE_USER" "$CONFIG_FILE"

    warn "Configuration générée dans $CONFIG_FILE"
    warn "ADMIN_SECRET = $ADMIN_SECRET — notez-le maintenant."
else
    info "Configuration existante conservée : $CONFIG_FILE"
fi

# ── 6a. Renonciation GPU explicite (OPS-012) ──────────────────────────────────
# Le choix est INSCRIT dans l'environnement, pas seulement subi par le shell qui
# a lancé l'installation : c'est ce qui permet à `doctor` de distinguer « pas de
# GPU, assumé » de « GPU attendu mais absent ». Écrit après la génération de la
# configuration pour couvrir aussi une réinstallation sur un env existant.
if [[ "$GPU_VERDICT" == "waived" ]]; then
    deploy_set_env_value "$CONFIG_FILE" "$GPU_WAIVER_ENV_KEY" true
    info "Absence de GPU inscrite dans $CONFIG_FILE ($GPU_WAIVER_ENV_KEY=true)."
elif [[ "$GPU_VERDICT" == "detected" ]]; then
    # Une renonciation périmée doit disparaître dès qu'un GPU est là, sinon
    # doctor tairait une vraie panne de pilote sur la foi d'un ancien choix.
    if [[ -n "$(deploy_env_value "$CONFIG_FILE" "$GPU_WAIVER_ENV_KEY")" ]]; then
        deploy_set_env_value "$CONFIG_FILE" "$GPU_WAIVER_ENV_KEY" false
        info "GPU détecté : renonciation $GPU_WAIVER_ENV_KEY remise à false."
    fi
fi

# ── 6c. Configuration cluster (optionnel, --cluster seulement) ────────────────

AGENT_SECRET_BEFORE="$(deploy_env_value "$CONFIG_FILE" AGENT_SECRET)"
deploy_apply_mode \
    "$EFFECTIVE_MODE" "$CONFIG_FILE" "$CONFIG_DIR" \
    "$SCRIPT_DIR/deploy/nodes.yaml.example"
chmod 640 "$CONFIG_FILE"
chown root:"$SERVICE_USER" "$CONFIG_FILE"

if [[ "$EFFECTIVE_MODE" == "cluster" ]]; then
    NODES_FILE="$(deploy_env_value "$CONFIG_FILE" CLUSTER_NODES_PATH)"
    chown root:"$SERVICE_USER" "$NODES_FILE"
    warn "Mode cluster actif; les fichiers existants et le secret partagé sont conservés."
    if deploy_secret_is_missing "$AGENT_SECRET_BEFORE"; then
        warn "Un AGENT_SECRET initial a été créé dans $CONFIG_FILE; il ne sera pas régénéré."
    fi
    warn "→ Éditez $NODES_FILE, configurez TLS, puis copiez AGENT_SECRET sur CHAQUE agent."
    warn "→ Installez chaque agent séparément; ce script ne modifie aucun nœud distant."
else
    info "Mode local actif : le gateway pilote llama-server sur cet hôte."
fi

# ── 6b. Registre des modèles (models.yaml) ────────────────────────────────────

CONFIGURED_MODELS_FILE="$(deploy_env_value "$CONFIG_FILE" MODELS_CONFIG_PATH)"
LEGACY_MODELS_FILE="$CONFIG_DIR/models.yaml"
MODELS_FILE="${CONFIGURED_MODELS_FILE:-$DATA_DIR/models.yaml}"

# Les mutations admin sont atomiques (tempfile + rename) et exigent donc un
# dossier writable par llmservice. Une ancienne installation sous /etc est
# copiée sans suppression vers /var/lib, puis le chemin env est ajusté.
if [[ "$MODELS_FILE" == "$LEGACY_MODELS_FILE" ]]; then
    MODELS_FILE="$DATA_DIR/models.yaml"
    if [[ -f "$LEGACY_MODELS_FILE" && ! -f "$MODELS_FILE" ]]; then
        cp "$LEGACY_MODELS_FILE" "$MODELS_FILE"
    fi
    deploy_set_env_value "$CONFIG_FILE" MODELS_CONFIG_PATH "$MODELS_FILE"
    warn "Registre migré sans suppression vers $MODELS_FILE pour permettre les mises à jour atomiques."
elif [[ -z "$CONFIGURED_MODELS_FILE" ]]; then
    deploy_set_env_value "$CONFIG_FILE" MODELS_CONFIG_PATH "$MODELS_FILE"
fi

if [[ ! -f "$MODELS_FILE" ]]; then
    info "Installation du registre de modèles initial…"
    cp "$SCRIPT_DIR/models.yaml" "$MODELS_FILE"
    chmod 640 "$MODELS_FILE"
    chown "$SERVICE_USER:$SERVICE_USER" "$MODELS_FILE"
    warn "Registre des modèles installé dans $MODELS_FILE"
    warn "IMPORTANT : vérifiez les chemins 'path' dans ce fichier avant de démarrer."
else
    info "Registre de modèles existant conservé : $MODELS_FILE"
fi

if [[ "$MODELS_FILE" == "$DATA_DIR/"* ]]; then
    chown "$SERVICE_USER:$SERVICE_USER" "$MODELS_FILE"
    chmod 640 "$MODELS_FILE"
else
    warn "Registre personnalisé conservé : vérifiez que llmservice peut écrire dans $(dirname "$MODELS_FILE") pour les mutations admin."
fi

# ── 7. Service systemd ────────────────────────────────────────────────────────

info "Installation du service systemd…"
if [[ "$EFFECTIVE_MODE" == "cluster" ]]; then
    cp "$SCRIPT_DIR/deploy/llm-gateway-cluster.service" /etc/systemd/system/llm-gateway.service
else
    cp "$SCRIPT_DIR/deploy/llm-gateway.service" /etc/systemd/system/llm-gateway.service
fi
systemctl daemon-reload
systemctl enable llm-gateway.service
# Réinstallation sur un hôte où l'unité était en `failed` : sans cette remise à
# zéro, le `systemctl start` que l'opérateur lance à l'étape finale peut se voir
# refuser par le start-limit systemd (COR-017).
systemctl reset-failed llm-gateway.service 2>/dev/null || true
info "Service systemd installé et activé."

# ── 8. Nginx ──────────────────────────────────────────────────────────────────

if command -v nginx &>/dev/null; then
    info "Configuration nginx…"
    # HTTP/2 est activé sous la forme que comprend le nginx local (OPS-009) :
    # la conf livrée reste neutre, le script écrit la bonne directive.
    nginx_render_conf "$SCRIPT_DIR/deploy/nginx.conf" /etc/nginx/sites-available/llm-gateway
    info "nginx ${NGINX_DETECTED_VERSION:-?} — HTTP/2 : ${NGINX_HTTP2_FORM}"
    ln -sf /etc/nginx/sites-available/llm-gateway /etc/nginx/sites-enabled/llm-gateway 2>/dev/null || true

    if nginx -t 2>/dev/null; then
        info "Configuration nginx valide."
    else
        warn "Vérifiez la configuration nginx manuellement (certificat TLS peut-être absent)."
    fi
else
    warn "nginx non trouvé — installer manuellement et copier deploy/nginx.conf"
fi

# ── 8b. Rotation des logs journald ────────────────────────────────────────────
# Réglages GLOBAUX au journal système (journald n'a pas de quota par-service).
# Créé si absent ; jamais écrasé pour préserver un ajustement local (SystemMaxUse
# dépend de l'espace disque de /var/log/journal).

JOURNALD_DROPIN="/etc/systemd/journald.conf.d/llm-gateway.conf"
if [[ ! -f "$JOURNALD_DROPIN" ]]; then
    info "Installation de la rotation journald…"
    mkdir -p /etc/systemd/journald.conf.d
    cp "$SCRIPT_DIR/deploy/journald-llm-gateway.conf" "$JOURNALD_DROPIN"
    systemctl_restart systemd-journald
    info "Rotation journald installée (SystemMaxUse=500M, rétention 30 j)."
else
    info "Configuration journald existante conservée : $JOURNALD_DROPIN"
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

# ── 9b. Timer de sauvegarde quotidienne de la DB ──────────────────────────────
# Le service oneshot exécute /opt/llm-gateway/deploy/llm-gateway-backup.sh ; le
# script doit donc être déployé dans INSTALL_DIR (pas seulement dans le dépôt).
#
# ORDRE (OPS-008) : cette étape suit délibérément l'initialisation de la base.
# Le timer est armé avec `--now`, et sur une RÉinstallation le stamp
# `Persistent=true` peut être périmé — systemd rattraperait alors l'occurrence
# manquée immédiatement. Armer après l'étape 9 garantit qu'une sauvegarde
# déclenchée aussitôt trouve une base déjà initialisée.

info "Installation du timer de sauvegarde SQLite…"
deploy_sync_gateway_operational_files "$SCRIPT_DIR" "$INSTALL_DIR"
chown -R root:"$SERVICE_USER" "$INSTALL_DIR/deploy"
chmod 750 "$INSTALL_DIR/deploy" "$INSTALL_DIR/deploy/llm-gateway-backup.sh" \
          "$INSTALL_DIR/deploy/smoke_test.sh"
chmod 640 "$INSTALL_DIR/deploy/deploy-mode-lib.sh" \
          "$INSTALL_DIR/deploy/nginx-lib.sh" \
          "$INSTALL_DIR/deploy/runtime-variants.yaml.example"
cp "$SCRIPT_DIR/deploy/llm-gateway-backup.service" /etc/systemd/system/
cp "$SCRIPT_DIR/deploy/llm-gateway-backup.timer"   /etc/systemd/system/
systemctl daemon-reload
# `--now` est INDISPENSABLE : `enable` seul crée le lien dans timers.target mais
# laisse le timer `inactive` jusqu'au prochain reboot — absent de `list-timers`,
# aucune sauvegarde. Sur une installation neuve, ce premier `start` ne déclenche
# PAS de rattrapage : sans stamp préexistant, systemd pose le stamp sans exécuter
# le job.
if command -v sqlite3 &>/dev/null; then
    if systemctl_enable_now llm-gateway-backup.timer; then
        info "Timer de sauvegarde armé (quotidien 03:15, rétention 14 j)."
    else
        warn "Timer de sauvegarde NON armé — vérifiez puis relancez :"
        warn "  sudo systemctl enable --now llm-gateway-backup.timer"
    fi
else
    warn "sqlite3 introuvable — timer copié mais NON armé. Après 'apt install sqlite3' :"
    warn "  sudo systemctl enable --now llm-gateway-backup.timer"
fi

# ── 10. Résumé ────────────────────────────────────────────────────────────────

echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Installation terminée !${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo ""
echo "  Prochaines étapes :"
echo ""
echo "  Mode installé : $EFFECTIVE_MODE"
echo ""
if [[ "$EFFECTIVE_MODE" == "local" ]]; then
    echo "  1. Premier token local :"
    echo "     Guide : docs/deployment.md, §0 « Déploiement local — premier token »"
    echo ""
    echo "  2. Production épinglée : préparer une matrice runtime :"
    echo "     sudo install -d -m 0755 /etc/evaruntime"
    echo "     sudo install -m 0644 $INSTALL_DIR/deploy/runtime-variants.yaml.example \\"
    echo "       /etc/evaruntime/runtime-variants.yaml"
    echo "     sudoedit /etc/evaruntime/runtime-variants.yaml"
    echo ""
    echo "     Produire, relire puis appliquer un plan strict bootstrap-plan/bootstrap-apply."
    echo "     Parcours complet : docs/deployment.md, « Parcours production complet » ."
else
    echo "  1. Éditer la topologie et installer chaque node-agent séparément :"
    echo "     sudo nano $(deploy_env_value "$CONFIG_FILE" CLUSTER_NODES_PATH)"
    echo "     sudo bash node_agent/deploy/install-agent.sh --node-id <id> \\"
    echo "       --llama-min-build <premier_build_corrige> \\"
    echo "       --agent-secret-file /root/evaruntime-agent-secret --orchestrator-cidr <IP>/32"
    echo ""
    echo "  2. Copier les mêmes GGUF, aux mêmes chemins, sur CHAQUE nœud éligible;"
    echo "     adapter $MODELS_FILE sur l'orchestrateur; configurer AGENT_SECRET et TLS."
fi
echo "     sudoedit $CONFIG_FILE"
if [[ "$EFFECTIVE_MODE" == "cluster" ]]; then
    echo "     sudoedit $MODELS_FILE"
fi
echo ""
echo "  3. Configurer le certificat TLS :"
echo "     sudo certbot certonly --nginx -d gateway.example.com"
echo "     sudo nano /etc/nginx/sites-available/llm-gateway  # adapter le domaine"
echo ""
echo "  4. Démarrer le service :"
echo "     sudo systemctl start llm-gateway"
echo "     sudo systemctl status llm-gateway"
echo "     sudo journalctl -u llm-gateway -f"
echo ""
echo "  5. Créer le premier utilisateur :"
echo "     cd $INSTALL_DIR"
echo "     sudo -u $SERVICE_USER ./venv/bin/python cli.py add-user alice --email alice@example.com"
echo "     sudo -u $SERVICE_USER ./venv/bin/python cli.py create-key alice --name 'these-2025'"
echo ""
echo "  6. Tester :"
echo '     curl -s https://gateway.example.com/v1/chat/completions \'
echo '       -H "Authorization: Bearer <VOTRE_CLE>" \'
echo '       -H "Content-Type: application/json" \'
echo '       -d '"'"'{"model":"llama-3.3-70b-instruct","messages":[{"role":"user","content":"Bonjour !"}]}'"'"
echo ""
