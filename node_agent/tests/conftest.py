"""
Configuration pytest pour node_agent/tests.

`node_agent/main.py` gère lui-même son sys.path à l'import : il insère
node_agent/ en position 0, puis gateway/ en position 1, puis la racine du
repo en position 2 — mais seulement si ces répertoires ne sont pas déjà
présents dans sys.path (cf. les `if ... not in sys.path` en tête de main.py).

Piège observé avec ce rootdir (node_agent/) : pytest insère LUI-MÊME
`node_agent/` dans sys.path (pour la découverte de rootdir/conftest) à une
position postérieure à 0 (typiquement juste après `tests/`, inséré en
position 0 pour importer `test_main` comme module top-level) — et ce, AVANT
que `conftest.py` ne s'exécute. Résultat : quand `main.py` teste
`if str(_AGENT_DIR) not in sys.path`, node_agent/ est déjà présent (mais pas
en tête), donc `main.py` NE LE RÉINSÈRE PAS en position 0. Il insère ensuite
gateway/ en position 1, ce qui le fait passer DEVANT node_agent/ dans l'ordre
de recherche. Un `import config` ambigu (le nom est utilisé à la fois par
node_agent/config.py et gateway/config.py, cf. AGENTS.md) résout alors vers
gateway/config.py — silencieusement faux : `main.settings` devient un
`gateway.config.Settings` sans `node_id`, et le lifespan explose.

Fix : retirer toute entrée préexistante de node_agent/ et gateway/ dans
sys.path AVANT d'importer `main`, pour que son propre sys.path.insert(0, ...)
s'exécute effectivement et place node_agent/ en tête, comme prévu.

Pas de plugin async (ni pytest-asyncio ni anyio) dans ce venv : les tests
asynchrones de ce dossier utilisent `asyncio.run(...)` directement dans des
fonctions de test synchrones normales.
"""
from __future__ import annotations

import sys
from pathlib import Path

_AGENT_DIR = Path(__file__).resolve().parents[1]
_GATEWAY_DIR = _AGENT_DIR.parent / "gateway"

for _stale in (str(_AGENT_DIR), str(_GATEWAY_DIR)):
    while _stale in sys.path:
        sys.path.remove(_stale)

# Réinsertion propre en tête, dans le même ordre que main.py (node_agent/
# avant gateway/) — main.py verra alors ses deux répertoires déjà présents
# dans le bon ordre et ne fera rien de plus (ses `if ... not in sys.path`
# sont alors no-op, ce qui est le comportement voulu ici).
sys.path.insert(0, str(_AGENT_DIR))
sys.path.insert(1, str(_GATEWAY_DIR))

import main  # noqa: E402,F401 — déclenche le reste du sys.path.insert de main.py (repo root)

assert sys.modules["config"].__file__ == str(_AGENT_DIR / "config.py"), (
    "Collision de module 'config' : gateway/config.py a été chargé à la place de "
    "node_agent/config.py — sys.path mal ordonné."
)
