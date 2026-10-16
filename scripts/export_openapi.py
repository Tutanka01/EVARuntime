#!/usr/bin/env python3
"""
Exporte ou vérifie le contrat OpenAPI versionné de la gateway.

    python scripts/export_openapi.py            # régénère docs/openapi.json
    python scripts/export_openapi.py --check    # échoue si le fichier a dérivé

Le mode `--check` sert de garde en CI. Le mode par défaut est ce qu'un
développeur lance après avoir modifié une route, dans le même commit que la
modification.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GATEWAY_ROOT = REPO_ROOT / "gateway"

# Les modules de la gateway sont top-level (`config`, `main`, `schemas`...),
# cf. AGENTS.md : on les expose comme le fait pytest depuis `gateway/`.
if str(GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(GATEWAY_ROOT))

# Valeurs factices : importer `main` déclenche la validation de configuration,
# qui refuse un secret absent ou trivial (fail-closed). Elles ne figurent pas
# dans le schéma — `gateway/schemas.py` ne lit pas `settings` — et ne doivent
# jamais ressembler à des secrets réels.
_PLACEHOLDER_ENV = {
    "INTERNAL_API_KEY": "openapi-export-internal-placeholder-value",
    "ADMIN_SECRET": "openapi-export-admin-placeholder-value",
    "AGENT_SECRET": "openapi-export-agent-placeholder-value",
    "DB_PATH": ":memory:",
    "MODELS_CONFIG_PATH": str(GATEWAY_ROOT / "models.yaml"),
}
for name, value in _PLACEHOLDER_ENV.items():
    os.environ.setdefault(name, value)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="ne rien écrire ; sortir en erreur si le fichier versionné a dérivé",
    )
    args = parser.parse_args(argv)

    # Import différé : il dépend de l'amorçage de `sys.path` et des valeurs
    # d'environnement posées ci-dessus. Au niveau module, il imposerait cet
    # ordre à tout lecteur et obligerait à un `noqa` dépendant de la
    # configuration ruff du répertoire.
    from openapi_contract import (
        SPEC_PATH,
        read_committed_spec,
        render_current_spec,
        write_spec,
    )

    current = render_current_spec()
    committed = read_committed_spec()
    relative = SPEC_PATH.relative_to(REPO_ROOT)

    if args.check:
        if committed is None:
            print(f"{relative} est absent. Lancez : python scripts/export_openapi.py")
            return 1
        if committed != current:
            print(
                f"{relative} ne correspond plus aux routes de la gateway.\n"
                "Régénérez-le dans le même commit que votre modification :\n"
                "  python scripts/export_openapi.py"
            )
            return 1
        print(f"{relative} est à jour.")
        return 0

    if committed == current:
        print(f"{relative} est déjà à jour.")
        return 0

    write_spec(current)
    print(f"{relative} régénéré.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
