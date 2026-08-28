#!/usr/bin/env bash
# code-layout-lib.sh — Layout du code Python déployé sur macOS
#
# Les scripts d'installation et de mise à jour doivent copier le même ensemble :
# modules racine, package cluster et package bootstrap avec son catalogue. Garder
# cette règle dans une fonction sourçable permet de l'exercer sans root ni hôte
# systemd ; une copie oubliée ne doit plus être découverte après installation.

_deploy_code_safe_roots() {
    local source_root="$1" target_root="$2" operation="$3"
    if [[ -z "$source_root" || -z "$target_root" || "$target_root" == "/" || \
          "$source_root" == "$target_root" ]]; then
        echo "$operation refusée : racines source/cible dangereuses ou identiques" >&2
        return 1
    fi
}

deploy_sync_gateway_code() {
    local source_root="$1" target_root="$2"

    _deploy_code_safe_roots "$source_root" "$target_root" "Synchronisation du code" || return 1

    [[ -f "$source_root/requirements.txt" && -f "$source_root/requirements.lock" ]] || {
        echo "Source gateway incomplète : requirements.txt/requirements.lock introuvable dans $source_root" >&2
        return 1
    }
    [[ -d "$source_root/cluster" ]] || {
        echo "Source gateway incomplète : package cluster/ introuvable dans $source_root" >&2
        return 1
    }
    [[ -d "$source_root/bootstrap" && -f "$source_root/bootstrap/catalog.yaml" ]] || {
        echo "Source gateway incomplète : package bootstrap/ ou catalog.yaml introuvable" >&2
        return 1
    }

    mkdir -p "$target_root"
    cp "$source_root"/*.py "$target_root/"
    cp "$source_root/requirements.txt" "$source_root/requirements.lock" "$target_root/"

    # Remplacer les packages plutôt que les superposer : un module supprimé dans
    # une release ne doit pas rester importable dans la suivante.
    rm -rf "$target_root/cluster" "$target_root/bootstrap"
    mkdir -p "$target_root/cluster" "$target_root/bootstrap"
    cp "$source_root/cluster"/*.py "$target_root/cluster/"
    cp "$source_root/bootstrap"/*.py "$target_root/bootstrap/"
    cp "$source_root/bootstrap/catalog.yaml" "$target_root/bootstrap/"

    rm -rf "$target_root/__pycache__" \
           "$target_root/cluster/__pycache__" \
           "$target_root/bootstrap/__pycache__"
}


# Artefacts d'exploitation qui doivent exister sur une installation NEUVE
# comme après une mise à jour. Les garder avec la règle de copie du code évite
# qu'un runbook pointe vers un fichier uniquement livré par update.sh.
#
# Sur macOS, on ne copie que les scripts exécutables (backup et smoke test).
# Les bibliothèques (deploy-mode-lib.sh, etc.) sont source directement depuis
# deploy-macos/ par les scripts d'installation, pas depuis l'installation elle-même.
_DEPLOY_OPERATIONAL_FILES=(
    llm-gateway-backup.sh
    smoke_test.sh
)

# deploy_sync_gateway_operational_files <source_root> <target_install_dir>
# Copie les fichiers opérationnels depuis gateway/deploy-macos vers l'installation.
# source_root est le répertoire gateway (ex: /Users/<USER>/EVARuntime/gateway).
deploy_sync_gateway_operational_files() {
    local source_root="$1" target_root="$2" file

    _deploy_code_safe_roots "$source_root" "$target_root" \
        "Synchronisation des artefacts d'exploitation" || return 1

    for file in "${_DEPLOY_OPERATIONAL_FILES[@]}"; do
        [[ -f "$source_root/deploy-macos/$file" ]] || {
            echo "Source gateway incomplète : deploy-macos/$file introuvable" >&2
            return 1
        }
    done

    mkdir -p "$target_root/deploy"
    for file in "${_DEPLOY_OPERATIONAL_FILES[@]}"; do
        cp "$source_root/deploy-macos/$file" "$target_root/deploy/$file"
    done
}


# deploy_sync_static_files <source_root> <target_root>
deploy_sync_static_files() {
    local source_root="$1" target_root="$2"

    _deploy_code_safe_roots "$source_root" "$target_root" "Synchronisation des fichiers statiques" || return 1

    if [[ -d "$source_root/static" ]]; then
        mkdir -p "$target_root/static"
        cp -r "$source_root/static/." "$target_root/static/"
    fi
}


# deploy_set_file_permissions <install_dir>
# macOS : pas de service user dédié, les fichiers appartiennent à l'utilisateur courant.
deploy_set_file_permissions() {
    local install_dir="$1"

    # Sur macOS, on garde les permissions simples — pas d'isolation par utilisateur.
    chmod 640 "$install_dir"/*.py 2>/dev/null || true
    chmod 640 "$install_dir/cluster"/*.py 2>/dev/null || true
    chmod 640 "$install_dir/bootstrap"/*.py "$install_dir/bootstrap/catalog.yaml" 2>/dev/null || true
    chmod 750 "$install_dir/cluster" "$install_dir/bootstrap" 2>/dev/null || true

    if [[ -d "$install_dir/static" ]]; then
        find "$install_dir/static" -type d -exec chmod 755 {} \;
        find "$install_dir/static" -type f -exec chmod 644 {} \;
    fi
    chmod 644 "$install_dir/requirements.txt" "$install_dir/requirements.lock" 2>/dev/null || true
}
