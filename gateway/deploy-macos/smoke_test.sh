#!/usr/bin/env bash
# smoke_test.sh — Recette du premier token (COR-006)
#
# Prouve qu'une version déployée SERT réellement, pas seulement qu'elle RÉPOND.
# `/health` prouve que le processus vit, `/ready` (COR-005) prouve que
# l'installation est structurellement saine — ni l'un ni l'autre ne prouve qu'un
# token sort. Ce script exerce le vrai chemin public :
#
#   client → nginx si configuré → authentification → quota/rate limit
#          → résolution du modèle → llama-server → chunk SSE généré
#          (content, reasoning_content ou tool_calls) → log d'usage
#
# Usage manuel (incident, validation avant ouverture du trafic) :
#   sudo bash gateway/deploy-macos/smoke_test.sh
#   sudo bash gateway/deploy-macos/smoke_test.sh --base-url https://llm.example.fr \
#        --model llama-3.1-8b-instruct --ttft-threshold-ms 5000
#
# Usage automatisé : appelé par update.sh après le redémarrage. Un échec
# fonctionnel y déclenche la restauration de la version précédente.
#
# ── Ce que chaque URL de base couvre ─────────────────────────────────────────
#
# --base-url  : chemin PUBLIC exercé par la génération. Le viser sur nginx
#               (https://…) couvre TLS, le rate limit nginx, `proxy_buffering
#               off` et `X-Accel-Buffering` — c'est-à-dire la seule configuration
#               qui prouve que le SSE n'est pas bufferisé par le reverse-proxy.
#               Le viser directement sur la gateway (http://127.0.0.1:8000, le
#               défaut) NE couvre PAS : TLS, les timeouts nginx, l'anti-slowloris,
#               `limit_req`/`limit_conn`, ni le buffering du proxy. Un premier
#               token vert en direct peut donc rester invisible côté client
#               derrière un nginx mal configuré.
#
# --admin-url : plan de CONTRÔLE (création/retrait de l'identité éphémère,
#               chargement du modèle, lecture du log d'usage, `/ready`).
#               Il vise la gateway EN DIRECT par défaut, volontairement :
#                 * le secret admin n'a aucune raison de traverser la façade ;
#                   l'appel direct reste valable même avec un nginx ancien ou
#                   personnalisé dont le timeout de chargement serait trop court.
#
# ── Sécurité ─────────────────────────────────────────────────────────────────
#
#   * Aucun secret n'est jamais passé en argument : ni `ADMIN_SECRET`, ni la clé
#     éphémère n'apparaissent dans `ps`, `/proc/*/cmdline` ou les journaux. Ils
#     transitent par des fichiers de configuration curl en mode 600, dans un
#     répertoire temporaire 700 détruit par le trap (même schéma que
#     `node_agent/deploy/install-agent.sh`).
#   * `printf` est le builtin bash : la ligne écrite dans le fichier de
#     configuration curl n'atteint jamais la table des processus.
#   * L'identité éphémère est nettoyée par un `trap` sur EXIT/INT/TERM/HUP. Une
#     identité de smoke test qui survit à un échec est une porte d'entrée
#     résiduelle : le nettoyage est idempotent et s'exécute même en cas
#     d'interruption.
#   * Le rapport ne contient ni clé, ni préfixe de clé, ni contenu généré :
#     seulement des compteurs et des durées.
#
# ── Exit codes (stables, consommés par update.sh) ────────────────────────────
#
#   0  Succès : un delta SSE avec du contenu a traversé tout le chemin public.
#      Un TTFT au-dessus du seuil sans --fail-on-ttft reste un succès (alerte).
#   1  Échec FONCTIONNEL — hard gate : pas de contenu, erreur upstream, stream
#      sans [DONE], enveloppe/modèle/usage invalides, chargement du modèle
#      impossible, ou log d'usage absent.
#   2  Erreur d'usage : option inconnue, dépendance manquante, configuration
#      inexploitable. Aucun appel n'a été fait. (Aligné sur doctor/Typer.)
#   3  Préflight : liveness ou readiness structurelle en échec. La génération n'a
#      même pas été tentée.
#   4  TTFT au-dessus du seuil AVEC --fail-on-ttft, alors que la génération est
#      fonctionnelle. Séparé de 1 à dessein (§11 du plan de consolidation).
#   5  Identité éphémère non créée, ou non nettoyée. Un 5 après un succès
#      fonctionnel signale une identité résiduelle à retirer À LA MAIN.

set -Eeuo pipefail
IFS=$'\n\t'

# Décimales déterministes : `%{time_starttransfer}` de curl doit rester lisible
# par python quelle que soit la locale de l'opérateur.
export LC_ALL=C

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

EXIT_OK=0
EXIT_GENERATION=1
EXIT_USAGE=2
EXIT_PREFLIGHT=3
EXIT_TTFT=4
EXIT_IDENTITY=5
EXIT_UNLOAD=6

# Les traces vont sur stderr, le RAPPORT sur stdout : `--json` reste ainsi
# directement exploitable par un pipeline.
info()    { echo -e "${GREEN}[INFO]${NC}  $*" >&2; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*" >&2; }
fail()    { echo -e "${RED}[FAIL]${NC}  $*" >&2; }
section() { echo -e "\n${CYAN}▶ $*${NC}" >&2; }

die_usage() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit "$EXIT_USAGE"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat <<EOF
Usage: $0 [options]

Recette du premier token : prouve qu'une version déployée génère réellement.

Options :
  --base-url URL           Chemin public exercé par la génération
                           (défaut : http://GATEWAY_HOST:GATEWAY_PORT de l'env)
  --admin-url URL          Plan de contrôle et /ready, en direct sur la gateway
                           (défaut : identique au défaut de --base-url)
  --env-file PATH          EnvironmentFile lu pour ADMIN_SECRET, le port et le
                           modèle par défaut (défaut : ~/.config/evaruntime/env)
  --admin-secret-file PATH Lit ADMIN_SECRET dans un fichier root-only plutôt que
                           dans l'EnvironmentFile
  --model ID               Modèle à exercer. Défaut dérivé de la configuration :
                           DEFAULT_MODEL_ID, sinon le plus petit modèle activé
                           annoncé par GET /admin/models
  --prompt TEXT            Prompt du smoke test (défaut : une phrase courte)
  --max-tokens N           Plafond de génération (défaut : 16)
  --ttft-threshold-ms N    Seuil d'alerte sur le TTFT. 0 = désactivé (défaut).
  --fail-on-ttft           Transforme le dépassement de seuil en échec (exit 4).
                           Sans cette option, un TTFT lent est une ALERTE et ne
                           provoque jamais de rollback (§11).
  --load-timeout SEC       Attente du chargement explicite (défaut : 330)
  --stream-timeout SEC     Durée totale maximale du stream (défaut : 120)
  --ready-timeout SEC      Attente de /health et /ready (défaut : 20)
  --connect-timeout SEC    Établissement de connexion (défaut : 10)
  --usage-timeout SEC      Attente de l'écriture du log d'usage (défaut : 15)
  --admin-timeout SEC      Appels d'administration courts (défaut : 30)
  --ca-cert PATH           Autorité de certification à utiliser pour TLS
  --insecure-tls           N'authentifie pas le certificat TLS. À réserver à un
                           diagnostic : ne prouve alors plus la chaîne TLS.
  --json                   Rapport JSON sur stdout au lieu du rapport humain
  -h, --help               Cette aide

Exit codes : 0 succès, 1 échec fonctionnel, 2 erreur d'usage, 3 préflight,
4 seuil TTFT dépassé (avec --fail-on-ttft), 5 identité éphémère résiduelle,
6 modèle de recette non déchargé.
EOF
}

# ── Options ───────────────────────────────────────────────────────────────────

BASE_URL=""
ADMIN_URL=""
ENV_FILE="${LLM_GATEWAY_CONFIG_DIR:-$HOME/.config/evaruntime}/env"
ADMIN_SECRET_FILE=""
MODEL_ID="${SMOKE_TEST_MODEL:-}"
PROMPT="Answer with a single word: OK"
MAX_TOKENS=16
TTFT_THRESHOLD_MS=0
FAIL_ON_TTFT=false
LOAD_TIMEOUT=330
STREAM_TIMEOUT=120
READY_TIMEOUT=20
CONNECT_TIMEOUT=10
USAGE_TIMEOUT=15
ADMIN_TIMEOUT=30
CA_CERT=""
INSECURE_TLS=false
JSON_OUTPUT=false

require_value() { [[ -n "${2:-}" ]] || die_usage "$1 exige une valeur."; }
require_uint()  { [[ "${2:-}" =~ ^[0-9]+$ ]] || die_usage "$1 exige un entier positif (reçu : ${2:-<vide>})."; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --base-url)           require_value "$1" "${2:-}"; BASE_URL="$2"; shift 2 ;;
        --admin-url)          require_value "$1" "${2:-}"; ADMIN_URL="$2"; shift 2 ;;
        --env-file)           require_value "$1" "${2:-}"; ENV_FILE="$2"; shift 2 ;;
        --admin-secret-file)  require_value "$1" "${2:-}"; ADMIN_SECRET_FILE="$2"; shift 2 ;;
        --model)              require_value "$1" "${2:-}"; MODEL_ID="$2"; shift 2 ;;
        --prompt)             require_value "$1" "${2:-}"; PROMPT="$2"; shift 2 ;;
        --max-tokens)         require_uint  "$1" "${2:-}"; MAX_TOKENS="$2"; shift 2 ;;
        --ttft-threshold-ms)  require_uint  "$1" "${2:-}"; TTFT_THRESHOLD_MS="$2"; shift 2 ;;
        --fail-on-ttft)       FAIL_ON_TTFT=true; shift ;;
        --load-timeout)       require_uint  "$1" "${2:-}"; LOAD_TIMEOUT="$2"; shift 2 ;;
        --stream-timeout)     require_uint  "$1" "${2:-}"; STREAM_TIMEOUT="$2"; shift 2 ;;
        --ready-timeout)      require_uint  "$1" "${2:-}"; READY_TIMEOUT="$2"; shift 2 ;;
        --connect-timeout)    require_uint  "$1" "${2:-}"; CONNECT_TIMEOUT="$2"; shift 2 ;;
        --usage-timeout)      require_uint  "$1" "${2:-}"; USAGE_TIMEOUT="$2"; shift 2 ;;
        --admin-timeout)      require_uint  "$1" "${2:-}"; ADMIN_TIMEOUT="$2"; shift 2 ;;
        --ca-cert)            require_value "$1" "${2:-}"; CA_CERT="$2"; shift 2 ;;
        --insecure-tls)       INSECURE_TLS=true; shift ;;
        --json)               JSON_OUTPUT=true; shift ;;
        -h|--help)            usage; exit "$EXIT_OK" ;;
        *) echo "Option inconnue : $1" >&2; usage >&2; exit "$EXIT_USAGE" ;;
    esac
done

for required in curl python3 mktemp date; do
    command -v "$required" &>/dev/null || die_usage "Commande requise introuvable : $required"
done

# `deploy_env_value` lit une clé sans sourcer le fichier : sourcer un
# EnvironmentFile qui contient des secrets exporterait tout dans l'environnement
# du script et de ses enfants.
# shellcheck source=deploy-mode-lib.sh
source "$SCRIPT_DIR/deploy-mode-lib.sh"

env_value() { deploy_env_value "$ENV_FILE" "$1"; }

# ── Résolution de la configuration ────────────────────────────────────────────

ADMIN_SECRET=""
if [[ -n "$ADMIN_SECRET_FILE" ]]; then
    [[ -f "$ADMIN_SECRET_FILE" ]] || die_usage "Fichier de secret introuvable : $ADMIN_SECRET_FILE"
    IFS= read -r ADMIN_SECRET < "$ADMIN_SECRET_FILE" || true
else
    [[ -f "$ENV_FILE" ]] || die_usage \
        "EnvironmentFile introuvable : $ENV_FILE. Passez --env-file ou --admin-secret-file."
    ADMIN_SECRET="$(env_value ADMIN_SECRET)"
fi

if [[ -z "$ADMIN_SECRET" || "$ADMIN_SECRET" == CHANGE_ME* ]]; then
    die_usage "ADMIN_SECRET absent ou laissé à sa valeur d'exemple : les routes /admin répondent 503."
fi
# Même contrainte que install-agent.sh : un secret exotique casserait le fichier
# de configuration curl (guillemets, retours à la ligne).
[[ "$ADMIN_SECRET" =~ ^[A-Za-z0-9._~+/=-]+$ ]] || \
    die_usage "ADMIN_SECRET contient des caractères incompatibles avec un fichier de configuration curl."

GATEWAY_HOST="$(env_value GATEWAY_HOST)"; GATEWAY_HOST="${GATEWAY_HOST:-127.0.0.1}"
GATEWAY_PORT="$(env_value GATEWAY_PORT)"; GATEWAY_PORT="${GATEWAY_PORT:-8000}"
[[ "$GATEWAY_PORT" =~ ^[0-9]+$ ]] || die_usage "GATEWAY_PORT invalide dans $ENV_FILE : $GATEWAY_PORT"
# 0.0.0.0 signifie « écoute partout » : on s'y adresse par le loopback.
case "$GATEWAY_HOST" in 0.0.0.0|::|"*") GATEWAY_HOST="127.0.0.1" ;; esac
DIRECT_URL="http://$GATEWAY_HOST:$GATEWAY_PORT"

BASE_URL="${BASE_URL:-$DIRECT_URL}"
ADMIN_URL="${ADMIN_URL:-$DIRECT_URL}"
BASE_URL="${BASE_URL%/}"
ADMIN_URL="${ADMIN_URL%/}"

CLUSTER_MODE="$(env_value CLUSTER_MODE)"; CLUSTER_MODE="${CLUSTER_MODE:-local}"

case "$ADMIN_URL" in
    http://127.0.0.1:*|http://localhost:*|http://[::1]:*) : ;;
    *) warn "URL de contrôle non-loopback ($ADMIN_URL) : préférez la gateway locale pour ne pas exposer ADMIN_SECRET et éviter un timeout de proxy ancien/personnalisé." ;;
esac

# ── Répertoire temporaire (700) et nettoyage garanti ──────────────────────────

umask 077
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/evaruntime-smoke.XXXXXX")"
chmod 700 "$TMP_DIR"
: > "$TMP_DIR/curl.err"

SMOKE_USER="evaruntime-smoke-$(date +%Y%m%d-%H%M%S)-$$"
IDENTITY_ARMED=false
IDENTITY_CLEANED=false
CLEANUP_FAILED=false
UNLOAD_FAILED=false
MODEL_LOADED=false
DONT_UNLOAD=false
KEY_PREFIX=""

# Idempotent : appelable par `finish` (pour que le rapport reflète le nettoyage)
# ET par le trap (pour couvrir une interruption ou une sortie imprévue).
unload_model() {
    [[ "$MODEL_LOADED" == true ]] || return 0
    [[ "$DONT_UNLOAD" == true ]] && return 0
    MODEL_LOADED=false

    local restore_errexit=false code
    if [[ -o errexit ]]; then restore_errexit=true; fi
    set +e

    # POST /admin/models/{id}/unload répond 404 si le modèle est déjà déchargé.
    code="$(admin_call POST "/admin/models/$MODEL_ID/unload" "$TMP_DIR/cleanup-unload.json" "$ADMIN_TIMEOUT")"
    case "$code" in
        200|404) ;;
        *) UNLOAD_FAILED=true; fail "Déchargement du modèle : HTTP ${code:-<aucune réponse>}" ;;
    esac

    if [[ "$UNLOAD_FAILED" == true ]]; then
        fail "DÉCHARGEMENT RÉSIDUEL — retirez-le à la main :"
        fail "  curl -X POST -H 'Authorization: Bearer <ADMIN_SECRET>' $ADMIN_URL/admin/models/$MODEL_ID/unload"
    else
        info "Modèle déchargé avec succès."
    fi

    if [[ "$restore_errexit" == true ]]; then set -e; fi
    return 0
}

cleanup_identity() {
    [[ "$IDENTITY_ARMED" == true ]] || return 0
    [[ "$IDENTITY_CLEANED" != true ]] || return 0
    IDENTITY_CLEANED=true

    local restore_errexit=false code
    if [[ -o errexit ]]; then restore_errexit=true; fi
    set +e

    # DELETE /admin/keys/{prefix} répond 404 si la clé est déjà révoquée ;
    # DELETE /admin/users/{u} répond 200 (already_anonymized) si l'utilisateur
    # l'est déjà, et 404 s'il n'a jamais existé. Les deux sont donc rejouables.
    if [[ -n "$KEY_PREFIX" ]]; then
        code="$(admin_call DELETE "/admin/keys/$KEY_PREFIX" "$TMP_DIR/cleanup-key.json" "$ADMIN_TIMEOUT")"
        case "$code" in
            200|404) ;;
            *) CLEANUP_FAILED=true; fail "Révocation de la clé éphémère : HTTP ${code:-<aucune réponse>}" ;;
        esac
    fi
    # L'anonymisation révoque de toute façon TOUTES les clés du compte : c'est le
    # filet de sécurité si le préfixe n'a pas pu être relevé.
    code="$(admin_call DELETE "/admin/users/$SMOKE_USER" "$TMP_DIR/cleanup-user.json" "$ADMIN_TIMEOUT")"
    case "$code" in
        200|404) ;;
        *) CLEANUP_FAILED=true; fail "Anonymisation de l'utilisateur éphémère : HTTP ${code:-<aucune réponse>}" ;;
    esac

    if [[ "$CLEANUP_FAILED" == true ]]; then
        fail "IDENTITÉ RÉSIDUELLE — retirez-la à la main :"
        fail "  curl -X DELETE -H 'Authorization: Bearer <ADMIN_SECRET>' $ADMIN_URL/admin/users/$SMOKE_USER"
    else
        info "Identité éphémère nettoyée (clé révoquée, utilisateur anonymisé)."
    fi

    if [[ "$restore_errexit" == true ]]; then set -e; fi
    return 0
}

on_exit() {
    local rc=$?
    trap - EXIT INT TERM HUP
    set +e
    cleanup_identity
    unload_model
    # Une identité résiduelle ne doit jamais passer pour un succès.
    if [[ "$CLEANUP_FAILED" == true && $rc -eq 0 ]]; then
        rc=$EXIT_IDENTITY
    fi
    [[ -z "${TMP_DIR:-}" ]] || rm -rf "$TMP_DIR"
    exit "$rc"
}

trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# ── Fichiers de configuration curl (secrets hors argv) ────────────────────────

ANON_CONF="$TMP_DIR/anon.curl"
ADMIN_CONF="$TMP_DIR/admin.curl"
USER_CONF="$TMP_DIR/user.curl"

: > "$ANON_CONF"
# `printf` builtin : la valeur n'apparaît jamais dans la table des processus.
printf 'header = "Authorization: Bearer %s"\n' "$ADMIN_SECRET" > "$ADMIN_CONF"
chmod 600 "$ADMIN_CONF"
ADMIN_SECRET=""
unset ADMIN_SECRET

CURL_TLS=()
[[ -z "$CA_CERT" ]]           || CURL_TLS+=(--cacert "$CA_CERT")
[[ "$INSECURE_TLS" != true ]] || CURL_TLS+=(--insecure)

# ── Auxiliaires python (aucun secret ne les traverse) ─────────────────────────

HELPER="$TMP_DIR/helper.py"
cat > "$HELPER" <<'PYEOF'
"""
Auxiliaires du smoke test EVARuntime (COR-006).

Commandes, toutes sans etat et sans reseau :

  sse MODEL            filtre : lit le flux SSE brut sur stdin, mesure le temps
                       jusqu'au premier delta UTILE et rend un verdict ;
  get FILE KEY         extrait un champ scalaire d'une reponse JSON ;
  pick-model FILE      choisit le plus petit modele active du registre ;
  is-loaded FILE ID    indique si le modèle spécifié est chargé ; renvoie "true" ou "false".
  usage-count FILE M   compte les entrees du log d'usage pour le modele M ;
  request-body M P N   construit le corps JSON de la requete de generation ;
  report-json          convertit des lignes cle=valeur (stdin) en document JSON.

Aucun contenu genere et aucune cle n'est jamais imprime : seuls des
identifiants, des compteurs et des durees le sont.
"""
import json
import sys
import time


def _emit(pairs):
    for key, value in pairs:
        sys.stdout.write("%s=%s\n" % (key, value))


def cmd_sse():
    """
    Verdict fonctionnel sur un flux SSE `/v1/chat/completions`.

    Le TTFT rapporte est le temps jusqu'au premier delta qui porte reellement du
    contenu (`choices[].delta.content` non vide) — PAS le premier octet SSE. Le
    premier octet arrive typiquement avec un chunk de role
    (`{"delta": {"role": "assistant"}}`) qui ne prouve rien : un backend peut
    ouvrir un stream en 200, emettre ce chunk, puis ne jamais generer.

    Base de temps : `time_starttransfer` mesure par curl (temps jusqu'au premier
    octet de reponse, donc jusqu'aux en-tetes), auquel on ajoute le delai
    OBSERVE ici entre la premiere ligne lue et la premiere ligne de contenu. La
    mesure ne depend donc pas de l'instant de demarrage de cet interpreteur.
    """
    stdin = sys.stdin
    try:
        stdin.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:  # pragma: no cover
        pass

    t_first_line = None
    t_first_content = None
    stats = {}
    saw_data = False
    saw_done = False
    content_chunks = 0
    content_chars = 0
    reasoning_chunks = 0
    reasoning_chars = 0
    tool_chunks = 0
    bad_envelope = 0
    upstream_error = None
    models = set()
    prompt_tokens = 0
    completion_tokens = 0

    for raw in iter(stdin.readline, ""):
        now = time.monotonic()
        line = raw.rstrip("\r\n")
        if t_first_line is None:
            t_first_line = now
        if line.startswith("__CURL_STATS__"):
            for token in line.split()[1:]:
                key, _, value = token.partition("=")
                stats[key] = value
            continue
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            saw_done = True
            continue
        saw_data = True
        try:
            chunk = json.loads(payload)
        except ValueError:
            bad_envelope += 1
            continue
        if not isinstance(chunk, dict):
            bad_envelope += 1
            continue
        # La gateway convertit une panne upstream en chunk SSE d'erreur suivi de
        # [DONE] : un stream « propre » peut donc masquer un 502/504.
        if isinstance(chunk.get("error"), dict):
            upstream_error = str(chunk["error"].get("type") or "server_error")
            continue
        if chunk.get("object") != "chat.completion.chunk" or not chunk.get("id"):
            bad_envelope += 1
        if chunk.get("model"):
            models.add(str(chunk["model"]))
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            prompt_tokens = int(usage.get("prompt_tokens") or prompt_tokens)
            completion_tokens = int(usage.get("completion_tokens") or completion_tokens)
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                continue
            # Un delta est « utile » selon la MÊME définition que la gateway
            # elle-même (proxy._record_ttft) : content, reasoning_content ou
            # tool_calls non vides. Les modèles thinking (DeepSeek/Qwen/MiniMax)
            # épuisent un max_tokens court en tokens de réflexion routés dans
            # `reasoning_content` : les ignorer ferait échouer un service qui
            # génère réellement — faux négatif constaté sur macOS (PR #23).
            generated = False
            content = delta.get("content")
            # `!= ""` et non `.strip()` : un token compose d'espaces EST du
            # contenu genere. Seul un delta vide ou absent ne prouve rien.
            if isinstance(content, str) and content != "":
                content_chunks += 1
                content_chars += len(content)
                generated = True
            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning != "":
                reasoning_chunks += 1
                reasoning_chars += len(reasoning)
                generated = True
            if delta.get("tool_calls"):
                tool_chunks += 1
                generated = True
            if generated and t_first_content is None:
                t_first_content = now

    expected_model = sys.argv[2] if len(sys.argv) > 2 else ""
    http_code = stats.get("http_code", "000")
    try:
        headers_ms = int(round(float(stats.get("time_starttransfer", "nan")) * 1000))
    except ValueError:
        headers_ms = -1
    try:
        total_ms = int(round(float(stats.get("time_total", "nan")) * 1000))
    except ValueError:
        total_ms = -1

    if t_first_content is not None and t_first_line is not None and headers_ms >= 0:
        ttft_ms = headers_ms + int(round((t_first_content - t_first_line) * 1000))
    else:
        ttft_ms = -1

    reason = "ok"
    if http_code != "200":
        reason = "http_status"
    elif upstream_error is not None:
        reason = "upstream_error"
    elif not saw_data:
        reason = "no_sse_data"
    elif content_chunks == 0 and reasoning_chunks == 0 and tool_chunks == 0:
        # Coeur de COR-006 : le stream s'est ouvert, il a meme pu se terminer
        # proprement — mais aucun token utile n'est sorti. « Le service repond »
        # n'est pas « le service sert ».
        reason = "no_content"
    elif not saw_done:
        reason = "no_done"
    elif expected_model and any(m != expected_model for m in models):
        # La gateway reecrit `chunk["model"]` avec l'id du registre sur CHAQUE
        # chunk : un seul chunk divergent trahit un mauvais routage de modele.
        reason = "model_mismatch"
    elif bad_envelope:
        reason = "bad_envelope"
    elif prompt_tokens <= 0 or completion_tokens <= 0:
        reason = "no_usage"

    _emit([
        ("gen_result", "ok" if reason == "ok" else "fail"),
        ("gen_reason", reason),
        ("http_code", http_code),
        ("headers_ms", headers_ms),
        ("ttft_ms", ttft_ms),
        ("total_ms", total_ms),
        ("content_chunks", content_chunks),
        ("content_chars", content_chars),
        ("reasoning_chunks", reasoning_chunks),
        ("reasoning_chars", reasoning_chars),
        ("tool_chunks", tool_chunks),
        ("prompt_tokens", prompt_tokens),
        ("completion_tokens", completion_tokens),
        ("stream_model", sorted(models)[0] if models else ""),
        ("saw_done", "true" if saw_done else "false"),
    ])
    return 0 if reason == "ok" else 1


def _load(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def cmd_get():
    try:
        data = _load(sys.argv[2])
    except (OSError, ValueError):
        return 1
    if not isinstance(data, dict):
        return 1
    value = data.get(sys.argv[3])
    if value is None:
        return 1
    sys.stdout.write("%s\n" % value)
    return 0


def cmd_pick_model():
    """
    Defaut derive de la configuration, jamais code en dur : le plus petit modele
    ACTIVE. C'est celui qui charge le plus vite et consomme le moins de VRAM —
    un smoke test ne doit pas mobiliser 42 GB pour prouver qu'un token sort.
    """
    try:
        data = _load(sys.argv[2])
    except (OSError, ValueError):
        return 1
    if not isinstance(data, list):
        return 1
    enabled = [m for m in data if isinstance(m, dict) and m.get("enabled") and m.get("id")]
    if not enabled:
        return 1

    def size(entry):
        try:
            return float(entry.get("vram_gb") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    # Departage par id pour un choix reproductible d'une execution a l'autre.
    enabled.sort(key=lambda m: (size(m), str(m["id"])))
    sys.stdout.write("%s\n" % enabled[0]["id"])
    return 0


def cmd_is_loaded():
    """Indique si le modèle spécifié est chargé."""
    try:
        data = _load(sys.argv[2])
    except (OSError, ValueError):
        return 1
    if not isinstance(data, list):
        return 1
    model_id = sys.argv[3] if len(sys.argv) > 3 else ""
    for entry in data:
        if isinstance(entry, dict) and entry.get("id") == model_id and entry.get("state") == "ready":
            sys.stdout.write("true\n")
            return 0
    sys.stdout.write("false\n")
    return 0


def cmd_usage_count():
    """Entrees du log d'usage imputees au modele exerce, avec des tokens."""
    count = 0
    try:
        data = _load(sys.argv[2])
    except (OSError, ValueError):
        data = []
    model = sys.argv[3] if len(sys.argv) > 3 else ""
    if isinstance(data, list):
        for entry in data:
            if not isinstance(entry, dict):
                continue
            if model and str(entry.get("model") or "") != model:
                continue
            try:
                if int(entry.get("total_tokens") or 0) <= 0:
                    continue
            except (TypeError, ValueError):
                continue
            count += 1
    sys.stdout.write("%d\n" % count)
    return 0


def cmd_request_body():
    """Corps JSON du smoke test — echappement delegue au serialiseur."""
    body = {
        "model": sys.argv[2],
        "messages": [{"role": "user", "content": sys.argv[3]}],
        "max_tokens": int(sys.argv[4]),
        "stream": True,
        "temperature": 0,
    }
    sys.stdout.write(json.dumps(body, ensure_ascii=True))
    return 0


def cmd_user_body():
    body = {
        "username": sys.argv[2],
        "notes": "Identite ephemere de smoke test (COR-006), anonymisee en fin d'execution.",
    }
    sys.stdout.write(json.dumps(body, ensure_ascii=True))
    return 0


_NUMERIC = {
    "headers_ms", "ttft_ms", "total_ms", "content_chunks", "content_chars",
    "reasoning_chunks", "reasoning_chars", "tool_chunks",
    "prompt_tokens", "completion_tokens", "usage_entries", "exit_code",
    "ttft_threshold_ms", "max_tokens",
}


def _read_pairs():
    document = {}
    for raw in sys.stdin:
        key, sep, value = raw.rstrip("\n").partition("=")
        if sep:
            document[key] = value
    return document


def cmd_report_json():
    raw = _read_pairs()
    document = {}
    for key, value in raw.items():
        if key in _NUMERIC:
            try:
                document[key] = int(value)
            except ValueError:
                document[key] = None
        elif value in ("true", "false"):
            document[key] = value == "true"
        else:
            document[key] = value
    sys.stdout.write(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return 0


_VERDICTS = {
    "0": "SUCCÈS",
    "1": "ÉCHEC FONCTIONNEL",
    "2": "ERREUR D'USAGE",
    "3": "ÉCHEC DE PRÉFLIGHT",
    "4": "SEUIL TTFT DÉPASSÉ",
    "5": "IDENTITÉ ÉPHÉMÈRE RÉSIDUELLE",
}

# (clé, libellé, suffixe, valeur par défaut)
_REPORT_ROWS = (
    ("mode", "Mode", "", "local"),
    ("base_url", "URL publique", "", ""),
    ("admin_url", "URL de contrôle", "", ""),
    ("model", "Modèle exercé", "", "<non résolu>"),
    ("identity", "Identité éphémère", "", "<non créée>"),
    ("http_code", "Statut HTTP génération", "", "-"),
    ("headers_ms", "Temps jusqu'aux en-têtes", " ms", "-"),
    ("ttft_ms", "TTFT (1er delta utile)", " ms", "-"),
    ("total_ms", "Durée totale du stream", " ms", "-"),
    ("content_chunks", "Chunks de contenu", "", "-"),
    ("content_chars", "Caractères générés", "", "-"),
    ("reasoning_chunks", "Chunks de raisonnement", "", "-"),
    ("prompt_tokens", "Tokens de prompt", "", "-"),
    ("completion_tokens", "Tokens de complétion", "", "-"),
    ("usage_entries", "Entrées de log d'usage", "", "-"),
    ("ttft_threshold_ms", "Seuil TTFT", " ms (0 = désactivé)", "0"),
    ("reason", "Cause", "", "ok"),
)


def cmd_report_human():
    """
    Rapport lisible. Rendu ici plutôt qu'en bash : `printf '%-30s'` aligne sur
    des OCTETS, ce qui décale toute étiquette accentuée en UTF-8.
    """
    values = _read_pairs()
    code = values.get("exit_code", "1")
    width = max(len(label) for _, label, _, _ in _REPORT_ROWS)
    lines = [
        "",
        "== Recette du premier token — rapport " + "=" * 26,
        "  %-*s : %s" % (width, "Résultat", _VERDICTS.get(code, "ÉCHEC")),
    ]
    for key, label, suffix, default in _REPORT_ROWS:
        raw = values.get(key, "")
        if raw == "":
            raw = default
        lines.append("  %-*s : %s%s" % (width, label, raw, suffix if raw != "-" else ""))
    lines.append("  %-*s : %s" % (width, "Exit code", code))
    lines.append("=" * 63)
    lines.append("")
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


_COMMANDS = {
    "sse": cmd_sse,
    "get": cmd_get,
    "pick-model": cmd_pick_model,
    "is-loaded": cmd_is_loaded,
    "usage-count": cmd_usage_count,
    "request-body": cmd_request_body,
    "user-body": cmd_user_body,
    "report-json": cmd_report_json,
    "report-human": cmd_report_human,
}

if __name__ == "__main__":
    # Le script exporte LC_ALL=C pour que curl produise des decimales
    # deterministes ; on force explicitement l'UTF-8 en sortie pour que les
    # libelles accentues du rapport ne dependent pas du mode UTF-8 implicite.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover
            pass
    handler = _COMMANDS.get(sys.argv[1] if len(sys.argv) > 1 else "")
    if handler is None:
        sys.stderr.write("commande inconnue\n")
        raise SystemExit(2)
    raise SystemExit(handler())
PYEOF

helper() { python3 "$HELPER" "$@"; }

# ── Appels HTTP ───────────────────────────────────────────────────────────────

# Écrit le corps dans $4 et imprime le code HTTP. Ne lève jamais : l'appelant
# décide, y compris depuis le nettoyage.
http_call() {
    local conf="$1" method="$2" url="$3" out="$4" timeout="$5" data_file="${6:-}"
    local args=(
        --config "$conf"
        --silent --show-error
        --request "$method"
        --connect-timeout "$CONNECT_TIMEOUT"
        --max-time "$timeout"
        --output "$out"
        --write-out '%{http_code}'
    )
    [[ ${#CURL_TLS[@]} -eq 0 ]] || args+=("${CURL_TLS[@]}")
    if [[ -n "$data_file" ]]; then
        args+=(--header 'Content-Type: application/json' --data-binary "@$data_file")
    fi
    curl "${args[@]}" "$url" 2>>"$TMP_DIR/curl.err" || true
}

admin_call() { http_call "$ADMIN_CONF" "$1" "$ADMIN_URL$2" "$3" "$4" "${5:-}"; }
anon_call()  { http_call "$ANON_CONF"  "$1" "$2" "$3" "$4"; }

# ── Rapport ───────────────────────────────────────────────────────────────────

REPORT_FILE="$TMP_DIR/report.kv"
: > "$REPORT_FILE"
put() { printf '%s=%s\n' "$1" "$2" >> "$REPORT_FILE"; }

report_get() {
    local key="$1" line
    while IFS= read -r line; do
        [[ "$line" == "$key="* ]] || continue
        printf '%s\n' "${line#*=}"
        return 0
    done < "$REPORT_FILE"
    printf '%s\n' "${2:-}"
}

# Le rendu est délégué à l'auxiliaire python : `printf '%-30s'` aligne sur des
# OCTETS, ce qui décalerait toute étiquette accentuée en UTF-8.
emit_report() {
    local code="$1"
    put exit_code "$code"
    if [[ "$JSON_OUTPUT" == true ]]; then
        helper report-json < "$REPORT_FILE"
    else
        helper report-human < "$REPORT_FILE"
    fi
}

# Nettoie AVANT d'imprimer, pour que le rapport reflète l'état réel de
# l'identité éphémère et que l'exit code annoncé soit celui rendu au shell.
finish() {
    local code="$1"
    cleanup_identity
    if [[ "$CLEANUP_FAILED" == true && "$code" -eq 0 ]]; then
        code="$EXIT_IDENTITY"
        put reason "identity_cleanup_failed"
    fi
    unload_model
    if [[ "$UNLOAD_FAILED" == true && "$code" -eq 0 ]]; then
        code="$EXIT_UNLOAD"
        put reason "model_unload_failed"
    fi
    emit_report "$code"
    exit "$code"
}

put base_url  "$BASE_URL"
put admin_url "$ADMIN_URL"
put mode      "$CLUSTER_MODE"
put max_tokens "$MAX_TOKENS"
put ttft_threshold_ms "$TTFT_THRESHOLD_MS"
put ttft_gate "$FAIL_ON_TTFT"

echo "" >&2
echo -e "${CYAN}═══════════════════════════════════════════════════════════════${NC}" >&2
echo -e "${CYAN}  EVARuntime — recette du premier token${NC}" >&2
echo -e "${CYAN}═══════════════════════════════════════════════════════════════${NC}" >&2
echo "  Chemin public    : $BASE_URL" >&2
echo "  Plan de contrôle : $ADMIN_URL" >&2
echo "  Mode             : $CLUSTER_MODE" >&2

# ── 1. Liveness ───────────────────────────────────────────────────────────────
# Sans en-tête d'autorisation : /health est publique, et l'ADMIN_SECRET n'a rien
# à faire sur le chemin public (journaux nginx, proxies intermédiaires).

section "1/8  Liveness (GET /health sur le chemin public)"
CODE="$(anon_call GET "$BASE_URL/health" "$TMP_DIR/health.json" "$READY_TIMEOUT")"
if [[ "$CODE" != "200" ]]; then
    put reason "liveness_failed"
    fail "/health a répondu ${CODE:-<aucune réponse>} sur $BASE_URL — processus mort, ou nginx ne le joint pas."
    finish "$EXIT_PREFLIGHT"
fi
info "Liveness confirmée."

# ── 2. Readiness structurelle ─────────────────────────────────────────────────
# Le plan de contrôle direct évite de transmettre ADMIN_SECRET à la façade.

section "2/8  Readiness structurelle (GET /ready sur le plan de contrôle)"
CODE="$(admin_call GET /ready "$TMP_DIR/ready.json" "$READY_TIMEOUT")"
if [[ "$CODE" != "200" ]]; then
    READY_REASON="$(helper get "$TMP_DIR/ready.json" reason 2>/dev/null || echo inconnue)"
    put reason "readiness_failed:$READY_REASON"
    fail "/ready a répondu ${CODE:-<aucune réponse>} (cause : $READY_REASON)."
    fail "Readiness structurelle en échec : la génération n'est même pas tentée."
    finish "$EXIT_PREFLIGHT"
fi
READY_LEVEL="$(helper get "$TMP_DIR/ready.json" level 2>/dev/null || echo structural)"
info "Readiness structurelle confirmée (niveau : $READY_LEVEL)."

# ── 3. Résolution du modèle ───────────────────────────────────────────────────

section "3/8  Résolution du modèle à exercer"
if [[ -z "$MODEL_ID" ]]; then
    MODEL_ID="$(env_value DEFAULT_MODEL_ID)"
    [[ -z "$MODEL_ID" ]] || info "Modèle repris de DEFAULT_MODEL_ID."
fi
if [[ -z "$MODEL_ID" ]]; then
    CODE="$(admin_call GET /admin/models "$TMP_DIR/models.json" "$ADMIN_TIMEOUT")"
    if [[ "$CODE" != "200" ]]; then
        put reason "model_discovery_failed"
        fail "GET /admin/models a répondu ${CODE:-<aucune réponse>} : aucun modèle par défaut dérivable."
        finish "$EXIT_PREFLIGHT"
    fi
    MODEL_ID="$(helper pick-model "$TMP_DIR/models.json" 2>/dev/null || true)"
    if [[ -z "$MODEL_ID" ]]; then
        put reason "no_enabled_model"
        fail "Aucun modèle activé dans le registre : rien à exercer."
        finish "$EXIT_PREFLIGHT"
    fi
    info "Modèle dérivé du registre (plus petit modèle activé)."
fi
put model "$MODEL_ID"
info "Modèle exercé : $MODEL_ID"

# ── 4. Identité éphémère ──────────────────────────────────────────────────────

section "4/8  Création de l'identité éphémère"
helper user-body "$SMOKE_USER" > "$TMP_DIR/user-create.json"

# Armé AVANT l'appel : un timeout réseau qui aurait quand même créé la ligne
# doit être nettoyé. Le nettoyage tolère un 404.
IDENTITY_ARMED=true
put identity "$SMOKE_USER"
CODE="$(admin_call POST /admin/users "$TMP_DIR/user.json" "$ADMIN_TIMEOUT" "$TMP_DIR/user-create.json")"
if [[ "$CODE" != "201" ]]; then
    put reason "identity_create_failed"
    fail "POST /admin/users a répondu ${CODE:-<aucune réponse>} : identité de smoke test impossible."
    finish "$EXIT_IDENTITY"
fi

printf '{"name":"smoke-test"}\n' > "$TMP_DIR/key-create.json"
CODE="$(admin_call POST "/admin/users/$SMOKE_USER/keys" "$TMP_DIR/key.json" "$ADMIN_TIMEOUT" "$TMP_DIR/key-create.json")"
if [[ "$CODE" != "201" ]]; then
    put reason "identity_key_failed"
    fail "POST /admin/users/{u}/keys a répondu ${CODE:-<aucune réponse>}."
    finish "$EXIT_IDENTITY"
fi

# La clé brute ne transite que par un pipe et une variable ; le fichier de
# réponse est détruit dans la foulée, sans attendre le trap.
SMOKE_KEY="$(helper get "$TMP_DIR/key.json" api_key 2>/dev/null || true)"
KEY_PREFIX="$(helper get "$TMP_DIR/key.json" key_prefix 2>/dev/null || true)"
rm -f "$TMP_DIR/key.json"
if [[ -z "$SMOKE_KEY" ]]; then
    put reason "identity_key_unreadable"
    fail "Réponse de création de clé inexploitable."
    finish "$EXIT_IDENTITY"
fi
if [[ ! "$SMOKE_KEY" =~ ^[A-Za-z0-9._~+/=-]+$ ]]; then
    SMOKE_KEY=""
    put reason "identity_key_charset"
    fail "La clé émise contient des caractères incompatibles avec un fichier de configuration curl."
    finish "$EXIT_IDENTITY"
fi
printf 'header = "Authorization: Bearer %s"\n' "$SMOKE_KEY" > "$USER_CONF"
chmod 600 "$USER_CONF"
SMOKE_KEY=""
unset SMOKE_KEY
info "Identité éphémère créée (clé jamais imprimée ni passée en argument)."

# ── 5. Chargement explicite du modèle ─────────────────────────────────────────
# Chargement EXPLICITE, à ne pas confondre avec le pré-chauffage automatique du
# modèle par défaut au démarrage (AUT-010, hors périmètre) : ici c'est le smoke
# test qui demande le chargement, le temps de sa propre exécution.

section "5/8  Chargement explicite du modèle (max ${LOAD_TIMEOUT}s)"
# Un modèle déjà chargé doit rester chargé : le décharger en fin de recette
# nuirait à un préchargement conservé volontairement (sujet #28). Le registre
# n'est récupéré en section 3 qu'en auto-découverte : on le rafraîchit ici pour
# que `is-loaded` réponde même avec un --model explicite. `admin_call` ne sort
# jamais en erreur ; le `|| true` garde la substitution hors de portée du set -e.
CODE="$(admin_call GET /admin/models "$TMP_DIR/models.json" "$ADMIN_TIMEOUT")"
if [[ "$CODE" == "200" ]] && [[ "$(helper is-loaded "$TMP_DIR/models.json" "$MODEL_ID" 2>/dev/null || true)" == true ]]; then
    DONT_UNLOAD=true
    info "Modèle déjà chargé, ne sera pas déchargé."
else
    CODE="$(admin_call POST "/admin/models/$MODEL_ID/load" "$TMP_DIR/load.json" "$LOAD_TIMEOUT")"
    if [[ "$CODE" != "200" ]]; then
        put reason "model_load_failed:${CODE:-000}"
        fail "POST /admin/models/$MODEL_ID/load a répondu ${CODE:-<aucune réponse>}."
        [[ "$CODE" != "504" ]] || fail "504 : l'appel de contrôle a probablement traversé un proxy dont le timeout est trop court. Visez --admin-url en direct."
        finish "$EXIT_GENERATION"
    fi
    MODEL_LOADED=true
    info "Modèle chargé et prêt."
fi

# ── 6. Génération streamée sur le chemin public ───────────────────────────────

section "6/8  Génération streamée (POST /v1/chat/completions, stream: true)"
helper request-body "$MODEL_ID" "$PROMPT" "$MAX_TOKENS" > "$TMP_DIR/chat.json"

set +e
curl --config "$USER_CONF" \
     --silent --show-error --no-buffer \
     --request POST \
     --header 'Content-Type: application/json' \
     --header 'Accept: text/event-stream' \
     --data-binary "@$TMP_DIR/chat.json" \
     --connect-timeout "$CONNECT_TIMEOUT" \
     --max-time "$STREAM_TIMEOUT" \
     --write-out '\n__CURL_STATS__ http_code=%{http_code} time_starttransfer=%{time_starttransfer} time_total=%{time_total}\n' \
     ${CURL_TLS[@]+"${CURL_TLS[@]}"} \
     "$BASE_URL/v1/chat/completions" 2>>"$TMP_DIR/curl.err" \
  | python3 -u "$HELPER" sse "$MODEL_ID" > "$TMP_DIR/gen.kv" 2>>"$TMP_DIR/helper.err"
# PIPESTATUS est réécrit par la commande suivante, y compris une simple
# affectation : on le copie d'un seul coup.
PIPE_RC=("${PIPESTATUS[@]}")
set -e
CURL_RC="${PIPE_RC[0]:-0}"
PARSE_RC="${PIPE_RC[1]:-0}"

GEN_RESULT="fail"
GEN_REASON="no_sse_data"
TTFT_MS="-1"
while IFS= read -r line; do
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
        gen_result) GEN_RESULT="$value" ;;
        gen_reason) GEN_REASON="$value" ;;
        ttft_ms)    TTFT_MS="$value"; put ttft_ms "$value" ;;
        http_code|headers_ms|total_ms|content_chunks|content_chars|reasoning_chunks|tool_chunks|prompt_tokens|completion_tokens|saw_done)
            put "$key" "$value" ;;
        stream_model) [[ -z "$value" ]] || put stream_model "$value" ;;
    esac
done < "$TMP_DIR/gen.kv"

# curl 28 = --max-time atteint : un stream qui ne se termine jamais est borné,
# jamais bloquant. Le verdict reste celui du parseur (no_done / no_content).
if [[ "$CURL_RC" -ne 0 ]]; then
    warn "curl a terminé en erreur (code $CURL_RC) — flux interrompu ou expiré."
fi

if [[ "$GEN_RESULT" != "ok" || "$PARSE_RC" -ne 0 ]]; then
    put reason "generation:$GEN_REASON"
    fail "Génération NON prouvée — cause : $GEN_REASON"
    case "$GEN_REASON" in
        no_content)     fail "Le stream s'est ouvert mais n'a produit aucun delta généré (content, reasoning_content ou tool_calls) : la version répond sans servir." ;;
        no_done)        fail "Le stream s'est interrompu avant [DONE]." ;;
        no_sse_data)    fail "Aucun événement SSE reçu." ;;
        upstream_error) fail "Le backend d'inférence a renvoyé une erreur pendant le stream." ;;
        http_status)    fail "Statut HTTP non-200 : $(report_get http_code '-')." ;;
        model_mismatch) fail "Le modèle annoncé dans le stream ne correspond pas à $MODEL_ID." ;;
        no_usage)       fail "Aucune comptabilisation de tokens dans le stream : l'usage ne serait pas facturé." ;;
        bad_envelope)   fail "Enveloppe SSE non conforme au format chat.completion.chunk." ;;
    esac
    finish "$EXIT_GENERATION"
fi
info "Premier delta utile reçu, [DONE] atteint, enveloppe/modèle/usage conformes."
info "En-têtes : $(report_get headers_ms '-') ms | TTFT : ${TTFT_MS} ms | total : $(report_get total_ms '-') ms"

# ── 7. Log d'usage ────────────────────────────────────────────────────────────
# `log_usage` est en fire-and-forget après la fin du générateur : on laisse à la
# tâche de fond le temps d'écrire, sans jamais attendre indéfiniment.

section "7/8  Vérification de l'écriture du log d'usage"
USAGE_ENTRIES=0
USAGE_DEADLINE=$(( $(date +%s) + USAGE_TIMEOUT ))
while :; do
    CODE="$(admin_call GET "/admin/usage?username=$SMOKE_USER&limit=10" "$TMP_DIR/usage.json" "$ADMIN_TIMEOUT")"
    if [[ "$CODE" == "200" ]]; then
        USAGE_ENTRIES="$(helper usage-count "$TMP_DIR/usage.json" "$MODEL_ID" 2>/dev/null || true)"
        [[ "$USAGE_ENTRIES" =~ ^[0-9]+$ ]] || USAGE_ENTRIES=0
        if [[ "$USAGE_ENTRIES" -gt 0 ]]; then
            break
        fi
    fi
    [[ "$(date +%s)" -lt "$USAGE_DEADLINE" ]] || break
    sleep 1
done
put usage_entries "$USAGE_ENTRIES"
if [[ "$USAGE_ENTRIES" -le 0 ]]; then
    put reason "usage_log_missing"
    fail "Aucune entrée de log d'usage pour $MODEL_ID après ${USAGE_TIMEOUT}s : la comptabilisation est perdue."
    finish "$EXIT_GENERATION"
fi
info "Log d'usage confirmé ($USAGE_ENTRIES entrée(s))."

# ── 8. Seuil TTFT — alerte par défaut, gate seulement si demandé ──────────────
# §11 du plan : le succès fonctionnel est un hard gate ; une régression TTFT est
# d'abord une ALERTE. Un rollback automatique sur une machine momentanément
# chargée provoquerait des boucles de restauration sans défaut applicatif.

section "8/8  Contrôle du TTFT et verdict"
TTFT_BREACH=false
if [[ "$TTFT_THRESHOLD_MS" -le 0 ]]; then
    info "Aucun seuil TTFT configuré (--ttft-threshold-ms 0) : mesure rapportée sans gate."
elif [[ "$TTFT_MS" -lt 0 ]]; then
    warn "TTFT non mesurable : seuil non évalué."
elif [[ "$TTFT_MS" -gt "$TTFT_THRESHOLD_MS" ]]; then
    TTFT_BREACH=true
    warn "TTFT ${TTFT_MS} ms > seuil ${TTFT_THRESHOLD_MS} ms."
else
    info "TTFT ${TTFT_MS} ms <= seuil ${TTFT_THRESHOLD_MS} ms."
fi

if [[ "$TTFT_BREACH" == true && "$FAIL_ON_TTFT" == true ]]; then
    put reason "ttft_threshold_exceeded"
    fail "Gate TTFT explicitement activé : version refusée malgré une génération fonctionnelle."
    finish "$EXIT_TTFT"
fi
if [[ "$TTFT_BREACH" == true ]]; then
    put reason "ttft_threshold_exceeded_warning"
    warn "Régression TTFT signalée en ALERTE : la version reste validée (aucun rollback)."
    warn "Pour en faire un gate bloquant : --fail-on-ttft."
else
    put reason "ok"
fi

info "Premier token prouvé de bout en bout sur le chemin public."
finish "$EXIT_OK"
