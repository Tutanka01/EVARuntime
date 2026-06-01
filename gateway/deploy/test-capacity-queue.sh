#!/usr/bin/env bash
# Test prod de la queue d'admission VRAM locale.
#
# Objectif:
#   1. Garder MODEL_BUSY occupe avec plusieurs generations longues.
#   2. Demander MODEL_TARGET pendant que MODEL_BUSY est pinned.
#   3. Observer si MODEL_TARGET attend, charge apres liberation, ou retourne
#      un 503 propre avec Retry-After quand la queue expire/est pleine.
#
# Exemple:
#   export API_KEY="llmgw-..."
#   export ADMIN_SECRET="$(sudo awk -F= '/^ADMIN_SECRET=/{print $2}' /etc/llm-gateway/env)"
#   export MODEL_BUSY="llama-3.3-70b-instruct"
#   export MODEL_TARGET="minimax-m2.7"
#   bash gateway/deploy/test-capacity-queue.sh
#
# Variables utiles:
#   GW=http://127.0.0.1:8000
#   API_KEY=...                      # cle utilisateur, pas ADMIN_SECRET
#   ADMIN_SECRET=...                 # optionnel, auto-lu depuis /etc/llm-gateway/env si possible
#   MODEL_BUSY=...
#   MODEL_TARGET=...
#   BUSY_REQUESTS=4
#   BUSY_MAX_TOKENS=2048
#   TARGET_MAX_TOKENS=64
#   START_DELAY_SECONDS=5
#   STATUS_INTERVAL_SECONDS=2

set -euo pipefail
IFS=$'\n\t'

GW="${GW:-http://127.0.0.1:8000}"
MODEL_BUSY="${MODEL_BUSY:-}"
MODEL_TARGET="${MODEL_TARGET:-}"
BUSY_REQUESTS="${BUSY_REQUESTS:-4}"
BUSY_MAX_TOKENS="${BUSY_MAX_TOKENS:-2048}"
TARGET_MAX_TOKENS="${TARGET_MAX_TOKENS:-64}"
START_DELAY_SECONDS="${START_DELAY_SECONDS:-5}"
STATUS_INTERVAL_SECONDS="${STATUS_INTERVAL_SECONDS:-2}"
CONNECT_TIMEOUT_SECONDS="${CONNECT_TIMEOUT_SECONDS:-10}"
MAX_TIME_SECONDS="${MAX_TIME_SECONDS:-900}"

API_KEY="${API_KEY:-${UPPA_LLM_KEY:-}}"
ADMIN_SECRET="${ADMIN_SECRET:-}"
if [[ -z "$ADMIN_SECRET" && -r /etc/llm-gateway/env ]]; then
  ADMIN_SECRET="$(awk -F= '/^ADMIN_SECRET=/{print $2}' /etc/llm-gateway/env || true)"
fi

if [[ -z "$API_KEY" ]]; then
  echo "[ERROR] API_KEY ou UPPA_LLM_KEY requis pour appeler /v1/chat/completions." >&2
  exit 2
fi
if [[ -z "$MODEL_BUSY" || -z "$MODEL_TARGET" ]]; then
  echo "[ERROR] MODEL_BUSY et MODEL_TARGET sont requis." >&2
  echo "Exemple: MODEL_BUSY=llama-3.3-70b-instruct MODEL_TARGET=minimax-m2.7 API_KEY=... bash $0" >&2
  exit 2
fi
if [[ "$MODEL_BUSY" == "$MODEL_TARGET" ]]; then
  echo "[ERROR] MODEL_BUSY et MODEL_TARGET doivent etre differents." >&2
  exit 2
fi

TMP_DIR="$(mktemp -d)"
PIDS=()
MONITOR_PID=""

cleanup() {
  local pid
  if [[ -n "$MONITOR_PID" ]] && kill -0 "$MONITOR_PID" >/dev/null 2>&1; then
    kill "$MONITOR_PID" >/dev/null 2>&1 || true
  fi
  for pid in "${PIDS[@]:-}"; do
    if kill -0 "$pid" >/dev/null 2>&1; then
      kill "$pid" >/dev/null 2>&1 || true
    fi
  done
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT INT TERM

json_escape() {
  python3 - "$1" <<'PY'
import json
import sys
print(json.dumps(sys.argv[1]))
PY
}

post_chat() {
  local model="$1"
  local max_tokens="$2"
  local prompt="$3"
  local out_body="$4"
  local out_headers="$5"

  local escaped_prompt
  escaped_prompt="$(json_escape "$prompt")"

  curl -sS \
    --connect-timeout "$CONNECT_TIMEOUT_SECONDS" \
    --max-time "$MAX_TIME_SECONDS" \
    -D "$out_headers" \
    -o "$out_body" \
    -w "%{http_code} %{time_total}\n" \
    "$GW/v1/chat/completions" \
    -H "Authorization: Bearer $API_KEY" \
    -H "Content-Type: application/json" \
    --data-binary @- <<JSON
{
  "model": "$model",
  "messages": [
    {
      "role": "user",
      "content": $escaped_prompt
    }
  ],
  "max_tokens": $max_tokens,
  "temperature": 0.8,
  "stream": false
}
JSON
}

print_status() {
  if [[ -z "$ADMIN_SECRET" ]]; then
    echo "[STATUS] ADMIN_SECRET absent: skip /admin/status"
    return
  fi

  local status_json="$TMP_DIR/status.json"
  if ! curl -fsS "$GW/admin/status" \
      -H "Authorization: Bearer $ADMIN_SECRET" \
      -o "$status_json" >/dev/null 2>&1; then
    echo "[STATUS] /admin/status indisponible"
    return
  fi

  python3 - "$status_json" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as f:
    data = json.load(f)

queue = data.get("capacity_queue") or {}
budget = data.get("vram_budget") or {}
models = [
    f"{m.get('id')}={m.get('state')} active={m.get('active_requests', 0)}"
    for m in data.get("models", [])
    if m.get("state") != "unloaded"
]
print(
    "[STATUS] "
    f"queue={queue.get('waiters', '?')}/{queue.get('max_waiters', '?')} "
    f"enabled={queue.get('enabled', '?')} "
    f"vram={budget.get('used_gb', '?')}/{budget.get('budget_net_gb', budget.get('total_gb', '?'))}GB "
    f"loaded=[{', '.join(models) if models else '-'}]"
)
PY
}

start_status_monitor() {
  (
    while true; do
      sleep "$STATUS_INTERVAL_SECONDS"
      print_status
    done
  ) &
  MONITOR_PID="$!"
}

stop_status_monitor() {
  if [[ -n "$MONITOR_PID" ]] && kill -0 "$MONITOR_PID" >/dev/null 2>&1; then
    kill "$MONITOR_PID" >/dev/null 2>&1 || true
    wait "$MONITOR_PID" 2>/dev/null || true
  fi
  MONITOR_PID=""
}

echo "== Capacity queue prod test =="
echo "Gateway       : $GW"
echo "MODEL_BUSY    : $MODEL_BUSY"
echo "MODEL_TARGET  : $MODEL_TARGET"
echo "BUSY_REQUESTS : $BUSY_REQUESTS"
echo "Busy tokens   : $BUSY_MAX_TOKENS"
echo "Target tokens : $TARGET_MAX_TOKENS"
echo ""
echo "Conseil: lance ce test depuis le serveur, idealement pendant une fenetre calme."
echo ""

echo "== Initial status =="
print_status
echo ""

echo "== Starting busy requests on $MODEL_BUSY =="
for i in $(seq 1 "$BUSY_REQUESTS"); do
  body="$TMP_DIR/busy-$i.body"
  headers="$TMP_DIR/busy-$i.headers"
  log="$TMP_DIR/busy-$i.log"
  (
    prompt="Test de charge ${i}. Ecris une reponse tres longue, structuree en sections numerotees, avec beaucoup de details, jusqu'a atteindre la limite de tokens."
    result="$(post_chat "$MODEL_BUSY" "$BUSY_MAX_TOKENS" "$prompt" "$body" "$headers" || true)"
    echo "$result" > "$log"
  ) &
  PIDS+=("$!")
  echo "[BUSY $i] pid=${PIDS[-1]} started"
done

echo ""
echo "Waiting ${START_DELAY_SECONDS}s before requesting target..."
for _ in $(seq 1 "$START_DELAY_SECONDS"); do
  sleep 1
  print_status
done

echo ""
echo "== Requesting target model $MODEL_TARGET =="
target_body="$TMP_DIR/target.body"
target_headers="$TMP_DIR/target.headers"
target_start="$(date +%s)"
start_status_monitor
target_result="$(post_chat "$MODEL_TARGET" "$TARGET_MAX_TOKENS" \
  "Reponds en une phrase: test de bascule de modele apres liberation VRAM." \
  "$target_body" "$target_headers" || true)"
stop_status_monitor
target_end="$(date +%s)"

target_code="$(awk '{print $1}' <<<"$target_result")"
target_time="$(awk '{print $2}' <<<"$target_result")"
retry_after="$(awk 'BEGIN{IGNORECASE=1} /^Retry-After:/ {gsub("\r","",$2); print $2}' "$target_headers" | tail -n1)"

echo "[TARGET] http_code=$target_code curl_time=${target_time}s wall_time=$((target_end - target_start))s retry_after=${retry_after:-none}"
echo "[TARGET] response preview:"
python3 - "$target_body" <<'PY'
import json
import sys

raw = open(sys.argv[1], "r", encoding="utf-8", errors="replace").read()
try:
    data = json.loads(raw)
    if "error" in data:
        print(json.dumps(data["error"], ensure_ascii=False, indent=2))
    else:
        content = ""
        choices = data.get("choices") or []
        if choices:
            content = (choices[0].get("message") or {}).get("content") or ""
        print((content or json.dumps(data, ensure_ascii=False))[:1000])
except Exception:
    print(raw[:1000])
PY

echo ""
echo "== Waiting for busy requests to finish =="
for idx in "${!PIDS[@]}"; do
  pid="${PIDS[$idx]}"
  if wait "$pid"; then
    :
  else
    echo "[BUSY $((idx + 1))] process ended with non-zero status"
  fi
done

echo ""
echo "== Busy request results =="
for i in $(seq 1 "$BUSY_REQUESTS"); do
  log="$TMP_DIR/busy-$i.log"
  if [[ -f "$log" ]]; then
    read -r code total < "$log" || true
    echo "[BUSY $i] http_code=${code:-?} curl_time=${total:-?}s"
  else
    echo "[BUSY $i] no log"
  fi
done

echo ""
echo "== Final status =="
print_status

echo ""
case "$target_code" in
  200)
    echo "[OK] MODEL_TARGET a ete servi. Si le temps est nettement > une requete chaude, la queue + eviction/load ont fonctionne."
    ;;
  503)
    echo "[OK] MODEL_TARGET a retourne 503 proprement. Verifie retry_after et le message: queue pleine/expiree ou modele impossible a charger."
    ;;
  400|401|403|404|429)
    echo "[WARN] Erreur de configuration ou politique API: code $target_code."
    ;;
  *)
    echo "[WARN] Resultat inattendu: code $target_code."
    ;;
esac
