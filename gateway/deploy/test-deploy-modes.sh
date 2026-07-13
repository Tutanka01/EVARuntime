#!/usr/bin/env bash
# Tests non destructifs du routage local/cluster. Aucune commande root, réseau,
# pip ou systemd n'est exécutée : les scripts ciblent un dossier temporaire.

set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/evaruntime-deploy-test.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT
mkdir -p "$TMP_DIR/etc"
mkdir -p "$TMP_DIR/bin"
for command_name in git systemctl nvidia-smi; do
    printf '#!/usr/bin/env bash\necho invoked >> "%s"\nexit 99\n' "$TMP_DIR/host-command-invoked" > "$TMP_DIR/bin/$command_name"
    chmod +x "$TMP_DIR/bin/$command_name"
done

run_install() {
    PATH="$TMP_DIR/bin:$PATH" \
    LLM_GATEWAY_CONFIG_DIR="$TMP_DIR/etc" \
    LLM_GATEWAY_INSTALL_DIR="$TMP_DIR/opt" \
    LLM_GATEWAY_DATA_DIR="$TMP_DIR/data" \
    LLM_GATEWAY_LOG_DIR="$TMP_DIR/log" \
    LLM_GATEWAY_MODELS_DIR="$TMP_DIR/models" \
        bash "$DEPLOY_DIR/install.sh" "$@"
}

run_update() {
    PATH="$TMP_DIR/bin:$PATH" \
    LLM_GATEWAY_CONFIG_DIR="$TMP_DIR/etc" \
    LLM_GATEWAY_INSTALL_DIR="$TMP_DIR/opt" \
    LLM_GATEWAY_DATA_DIR="$TMP_DIR/data" \
        bash "$DEPLOY_DIR/update.sh" "$@"
}

assert_contains() {
    local output="$1" expected="$2"
    [[ "$output" == *"$expected"* ]] || {
        printf 'Attendu: %s\nReçu:\n%s\n' "$expected" "$output" >&2
        exit 1
    }
}

output="$(run_install --dry-run)"
assert_contains "$output" "Mode demandé : local"

output="$(run_install --mode cluster --dry-run)"
assert_contains "$output" "Mode demandé : cluster"
output="$(run_install --cluster --dry-run)"
assert_contains "$output" "Mode demandé : cluster"

printf 'ADMIN_SECRET=stable\nCLUSTER_MODE=cluster\nAGENT_SECRET=agent-stable\n' > "$TMP_DIR/etc/env"
before="$(cksum "$TMP_DIR/etc/env")"
output="$(run_install --mode cluster --dry-run)"
assert_contains "$output" "Mode existant  : cluster"
[[ "$(cksum "$TMP_DIR/etc/env")" == "$before" ]]

if run_install --dry-run > "$TMP_DIR/unexpected" 2>&1; then
    echo "install sans mode ne doit pas basculer implicitement cluster vers local" >&2
    exit 1
fi
if run_install --mode local > "$TMP_DIR/unexpected" 2>&1; then
    echo "une migration install doit exiger --allow-mode-change" >&2
    exit 1
fi

output="$(run_update --dry-run)"
assert_contains "$output" "Mode effectif  : cluster"

output="$(run_update --mode local --dry-run)"
assert_contains "$output" "Migration      : cluster → local"
[[ "$(cksum "$TMP_DIR/etc/env")" == "$before" ]]
if run_update --mode local > "$TMP_DIR/unexpected" 2>&1; then
    echo "une migration update doit exiger --allow-mode-change" >&2
    exit 1
fi

if run_update --mode local --cluster --dry-run > "$TMP_DIR/unexpected" 2>&1; then
    echo "des options de mode contradictoires doivent être refusées" >&2
    exit 1
fi

# Test unitaire du helper : une mise à jour déduplique CLUSTER_MODE et ne
# touche jamais aux secrets existants.
# shellcheck source=deploy-mode-lib.sh
source "$DEPLOY_DIR/deploy-mode-lib.sh"
printf 'CLUSTER_MODE=local\nADMIN_SECRET=stable\nCLUSTER_MODE=cluster\n' > "$TMP_DIR/helper-env"
deploy_set_env_value "$TMP_DIR/helper-env" CLUSTER_MODE cluster
[[ "$(grep -c '^CLUSTER_MODE=' "$TMP_DIR/helper-env")" == 1 ]]
[[ "$(deploy_env_value "$TMP_DIR/helper-env" ADMIN_SECRET)" == stable ]]

printf 'CLUSTER_MODE=local\nAGENT_SECRET=agent-stable\nCLUSTER_NODES_PATH=%s\n' \
    "$TMP_DIR/custom-nodes.yaml" > "$TMP_DIR/apply-env"
printf 'nodes:\n  - id: node-a\n    base_url: https://node-a:9443\n' > "$TMP_DIR/custom-nodes.yaml"
nodes_before="$(cksum "$TMP_DIR/custom-nodes.yaml")"
deploy_apply_mode cluster "$TMP_DIR/apply-env" "$TMP_DIR" "$DEPLOY_DIR/nodes.yaml.example"
deploy_apply_mode cluster "$TMP_DIR/apply-env" "$TMP_DIR" "$DEPLOY_DIR/nodes.yaml.example"
[[ "$(deploy_env_value "$TMP_DIR/apply-env" AGENT_SECRET)" == agent-stable ]]
[[ "$(deploy_env_value "$TMP_DIR/apply-env" CLUSTER_NODES_PATH)" == "$TMP_DIR/custom-nodes.yaml" ]]
[[ "$(cksum "$TMP_DIR/custom-nodes.yaml")" == "$nodes_before" ]]

[[ ! -e "$TMP_DIR/host-command-invoked" ]]
[[ ! -e "$TMP_DIR/opt" && ! -e "$TMP_DIR/data" && ! -e "$TMP_DIR/log" && ! -e "$TMP_DIR/models" ]]

echo "OK: parcours install/update local|cluster et dry-run validés"
