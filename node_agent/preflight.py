"""Préflight opératoire du node-agent, sans jamais afficher ses secrets."""
from __future__ import annotations

import argparse
import asyncio
import os
import stat
from pathlib import Path

import httpx
from pydantic_settings import DotEnvSettingsSource

from config import AgentSettings


def load_settings_file(env_file: Path) -> AgentSettings:
    """Charge exactement EnvironmentFile, sans override accidentel du shell appelant."""
    source = DotEnvSettingsSource(
        AgentSettings,
        env_file=env_file,
        env_file_encoding="utf-8",
    )
    raw = source()
    missing = [
        name for name, field in AgentSettings.model_fields.items()
        if field.is_required() and name not in raw
    ]
    if missing:
        raise ValueError(f"variables obligatoires absentes : {', '.join(missing)}")
    # Fournir explicitement tous les defaults en kwargs leur donne priorité sur
    # os.environ dans BaseSettings. Ainsi, même une variable ambiante invalide
    # ne peut ni remplacer ni compléter silencieusement le fichier ciblé.
    values = {
        name: field.get_default(call_default_factory=True)
        for name, field in AgentSettings.model_fields.items()
        if not field.is_required()
    }
    values.update(raw)
    return AgentSettings(_env_file=None, **values)


def validate_sensitive_file(path: Path, label: str) -> list[str]:
    """Refuse les secrets lisibles par tous ou modifiables par le groupe."""
    if not path.is_file():
        return [f"{label} introuvable : {path}"]
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o027:
        return [
            f"permissions trop ouvertes pour {label} ({path}, mode {mode:04o}); "
            "attendu 0600 ou 0640 (jamais group-writable/world-readable)"
        ]
    return []


def validate_runtime_files(configured: AgentSettings) -> list[str]:
    """Retourne les problèmes de fichiers/périphériques détectables avant systemd."""
    errors: list[str] = []
    try:
        configured.validate_runtime_security()
    except RuntimeError as exc:
        errors.append(str(exc))

    binary = configured.llama_server_bin
    if not binary.is_file():
        errors.append(f"llama-server introuvable : {binary}")
    elif not os.access(binary, os.X_OK):
        errors.append(f"llama-server non exécutable : {binary}")

    if configured.agent_tls_cert is not None and not configured.agent_tls_cert.is_file():
        errors.append(f"certificat TLS introuvable : {configured.agent_tls_cert}")
    if configured.agent_tls_key is not None:
        errors.extend(validate_sensitive_file(configured.agent_tls_key, "clé TLS"))

    for directory in configured.allowed_model_dirs_list():
        model_dir = Path(directory)
        if not model_dir.is_dir():
            errors.append(f"répertoire de modèles introuvable : {model_dir}")
        elif not os.access(model_dir, os.R_OK | os.X_OK):
            errors.append(f"répertoire de modèles non lisible : {model_dir}")

    return errors


async def check_local_health(configured: AgentSettings, timeout: float) -> None:
    """Vérifie l'agent local après restart sans exposer AGENT_SECRET à `ps`."""
    url = local_health_url(configured)
    async with httpx.AsyncClient(verify=False, timeout=timeout) as client:
        response = await client.get(
            url,
            headers={"Authorization": f"Bearer {configured.agent_secret}"},
        )
    response.raise_for_status()
    body = response.json()
    if body.get("status") != "ok" or body.get("loaded_model_ids") is None:
        raise RuntimeError(f"réponse health invalide sur {url}")


def local_health_url(configured: AgentSettings) -> str:
    """Construit une sonde valide pour wildcard, bind précis, IPv4 ou IPv6."""
    host = configured.agent_host
    if host == "0.0.0.0":
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if ":" in host:
        host = f"[{host}]"
    return f"https://{host}:{configured.agent_port}/agent/health"


def main() -> int:
    parser = argparse.ArgumentParser(description="Valide le déploiement node-agent")
    parser.add_argument("--env", type=Path, required=True, help="EnvironmentFile systemd")
    parser.add_argument("--check-health", action="store_true", help="sonde aussi l'agent local")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    if not args.env.is_file():
        parser.error(f"fichier env introuvable : {args.env}")

    try:
        configured = load_settings_file(args.env)
        errors = validate_runtime_files(configured)
        errors.extend(validate_sensitive_file(args.env, "EnvironmentFile"))
    except Exception as exc:
        print(f"[ERREUR] Configuration invalide : {exc}")
        return 1

    if errors:
        for error in errors:
            print(f"[ERREUR] {error}")
        return 1

    if args.check_health:
        try:
            asyncio.run(check_local_health(configured, args.timeout))
        except Exception as exc:
            print(f"[ERREUR] Health-check node-agent : {exc}")
            return 1

    last_port = configured.base_llama_port + configured.max_loaded_models - 1
    print(
        "[OK] node-agent valide : "
        f"node={configured.node_id}, control={configured.agent_host}:{configured.agent_port}, "
        f"data={configured.llama_server_host}:{configured.base_llama_port}-{last_port}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
