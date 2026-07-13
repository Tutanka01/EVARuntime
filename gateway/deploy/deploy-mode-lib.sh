#!/usr/bin/env bash
# Fonctions partagées par install.sh et update.sh pour choisir le mode de
# déploiement sans sourcer le fichier env (qui contient des secrets).

deploy_env_value() {
    local file="$1" key="$2"
    [[ -f "$file" ]] || return 0
    awk -v key="$key" '
        $0 !~ /^[[:space:]]*#/ {
            line = $0
            sub(/^[[:space:]]*/, "", line)
            pos = index(line, "=")
            if (!pos) next
            lhs = substr(line, 1, pos - 1)
            gsub(/[[:space:]]/, "", lhs)
            if (lhs == key) {
                print substr(line, pos + 1)
                exit
            }
        }
    ' "$file"
}

deploy_validate_mode() {
    case "$1" in
        local|cluster) return 0 ;;
        *) return 1 ;;
    esac
}

deploy_select_mode() {
    local config_file="$1" requested_mode="$2" current_mode

    current_mode="$(deploy_env_value "$config_file" CLUSTER_MODE)"
    if [[ -n "$current_mode" ]] && ! deploy_validate_mode "$current_mode"; then
        echo "Valeur CLUSTER_MODE invalide dans $config_file : '$current_mode'" >&2
        return 1
    fi

    if [[ -n "$requested_mode" ]]; then
        printf '%s\n' "$requested_mode"
    elif [[ -n "$current_mode" ]]; then
        printf '%s\n' "$current_mode"
    else
        printf 'local\n'
    fi
}

# Remplace la première occurrence active de KEY, supprime les doublons actifs et
# ajoute KEY si elle est absente. L'écriture reste atomique dans le même dossier.
deploy_set_env_value() {
    local file="$1" key="$2" value="$3" tmp
    tmp="$(mktemp "${file}.tmp.XXXXXX")"
    awk -v key="$key" -v value="$value" '
        BEGIN { written = 0 }
        {
            line = $0
            sub(/^[[:space:]]*/, "", line)
            pos = index(line, "=")
            lhs = pos ? substr(line, 1, pos - 1) : ""
            gsub(/[[:space:]]/, "", lhs)
            if ($0 !~ /^[[:space:]]*#/ && lhs == key) {
                if (!written) {
                    print key "=" value
                    written = 1
                }
                next
            }
            print
        }
        END {
            if (!written) print key "=" value
        }
    ' "$file" > "$tmp"
    chmod --reference="$file" "$tmp" 2>/dev/null || chmod 640 "$tmp"
    chown --reference="$file" "$tmp" 2>/dev/null || true
    mv -f "$tmp" "$file"
}

deploy_secret_is_missing() {
    local value="$1"
    [[ -z "$value" || "$value" == CHANGE_ME* ]]
}

deploy_apply_mode() {
    local mode="$1" config_file="$2" config_dir="$3" nodes_template="$4"
    local nodes_file agent_secret

    if [[ "$mode" != "cluster" ]]; then
        deploy_set_env_value "$config_file" CLUSTER_MODE "$mode"
        return 0
    fi

    nodes_file="$(deploy_env_value "$config_file" CLUSTER_NODES_PATH)"
    if [[ -z "$nodes_file" ]]; then
        nodes_file="$config_dir/nodes.yaml"
        deploy_set_env_value "$config_file" CLUSTER_NODES_PATH "$nodes_file"
    fi
    if [[ "$nodes_file" != /* ]]; then
        echo "CLUSTER_NODES_PATH doit être absolu : $nodes_file" >&2
        return 1
    fi
    if [[ ! -d "$(dirname "$nodes_file")" ]]; then
        echo "Dossier de CLUSTER_NODES_PATH introuvable : $(dirname "$nodes_file")" >&2
        return 1
    fi
    agent_secret="$(deploy_env_value "$config_file" AGENT_SECRET)"
    if deploy_secret_is_missing "$agent_secret"; then
        agent_secret="$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")"
        deploy_set_env_value "$config_file" AGENT_SECRET "$agent_secret"
    fi

    if [[ ! -f "$nodes_file" ]]; then
        cp "$nodes_template" "$nodes_file"
        chmod 640 "$nodes_file"
    fi

    # CLUSTER_MODE est écrit en dernier : un échec de préparation ne laisse
    # jamais une configuration à moitié basculée en cluster.
    deploy_set_env_value "$config_file" CLUSTER_MODE cluster
}
