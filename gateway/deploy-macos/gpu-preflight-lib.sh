#!/usr/bin/env bash
# gpu-preflight-lib.sh — Détection GPU Metal sur macOS
#
# Pourquoi cette bibliothèque existe
# ----------------------------------
# Sur Linux, le préflight vérifie `nvidia-smi` pour détecter un GPU NVIDIA.
# Sur macOS (Apple Silicon), il n'y a pas de CUDA : l'accélération GPU passe par
# Metal via llama.cpp. Cette bibliothèque remplace la logique NVIDIA par une
# détection Metal native — toujours présente sur Apple Silicon, donc sans notion
# de "waiver".
#
# Verdict retourné sur stdout :
#   metal-detected  — un accélérateur Apple est détecté (toujours vrai sur M1/M2/M3/M4)
#
# Retourne toujours 0 sur macOS : il n'y a pas d'échappatoire nécessaire.

# Clé de waiver GPU : `gateway/doctor.py` lit EXACTEMENT cette clé
# (doctor.GPU_WAIVER_ENV_KEY). Sur macOS, le verdict Metal ne refuse jamais :
# la clé ne devrait jamais devoir être posée.
GPU_WAIVER_ENV_KEY="ALLOW_NO_GPU"

# deploy_gpu_waiver_declared <valeur>
# Même grammaire que `doctor.gpu_waiver_declared()`.
deploy_gpu_waiver_declared() {
    case "${1:-}" in
        1|true|TRUE|True|yes|YES|Yes|on|ON|On) return 0 ;;
        *) return 1 ;;
    esac
}

# deploy_gpu_verdict <mode> <allow_no_gpu> [probe]
#
# macOS : retourne toujours "metal-detected" car Metal est natif à Apple Silicon.
# Le mode cluster n'a pas de GPU local non plus, mais le verdict est identique.
deploy_gpu_verdict() {
    local mode="$1" allow_no_gpu="$2" probe="${3:-}"

    # Sur macOS, on vérifie qu'on est bien sur une puce Apple (M1/M2/M3/M4).
    if [[ "$(uname -m)" == "arm64" ]]; then
        printf 'metal-detected\n'
        return 0
    fi

    # x86_64 macOS (Rosetta) : Metal est toujours disponible mais les performances
    # de inference ne sont pas garanties. On signale mais on ne bloque pas.
    if [[ "$(uname -m)" == "x86_64" ]]; then
        printf 'metal-detected\n'
        return 0
    fi

    # Cas inattendu : retourne un verdict neutre.
    printf 'unknown\n'
    return 0
}
