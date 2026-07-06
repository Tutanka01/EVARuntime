"""
Chargement et validation de nodes.yaml — topologie du cluster.

Symétrique à `model_registry.py` (yaml.safe_load, regex stricte sur l'id,
écriture impossible côté orchestrateur : ce fichier est en lecture seule, édité
à la main par l'opérateur). Les timeouts / intervalles vivent dans `settings`
(env-tunables), seules les infos topologiques sont ici.

Structure du fichier :

    cluster:
      tls_verify: /etc/ssl/certs/uppa-internal-ca.pem
      # OU :
      tls_verify: false   # LAN strict, certs auto-signés non vérifiés

    nodes:
      - id: dgx-spark-a
        base_url: https://dgx-a.internal.uppa.fr:9443
        description: "DGX Spark A — GB10 128 GB unified"

      - id: dgx-spark-b
        base_url: https://dgx-b.internal.uppa.fr:9443
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

log = logging.getLogger(__name__)

# Garde-fou opt-in : si activé, une config https:// + tls_verify=false (TLS
# activé mais jamais vérifié — incohérent et dangereux, cf. `_check_tls_verify_
# consistency`) fait échouer le chargement au lieu d'un simple warning. Réservé
# aux déploiements qui veulent un fail-fast strict ; désactivé par défaut pour
# ne pas casser les configs de dev existantes (cert auto-signé + tls_verify:
# false est un usage documenté, cf. nodes.yaml.example).
_STRICT_TLS_ENV = "CLUSTER_STRICT_TLS_VERIFY"

# Même grammaire que model_id : minuscules + chiffres + . _ -.
_NODE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


@dataclass(frozen=True)
class NodeConfig:
    """Définition statique d'un nœud du cluster (lue depuis nodes.yaml)."""
    id: str
    base_url: str
    description: str = ""


@dataclass(frozen=True)
class ClusterConfig:
    """
    Configuration cluster complète.

    `tls_verify` reflète la valeur passée à `httpx.AsyncClient(verify=...)` :
      - chemin vers un bundle CA → vérification stricte contre ce bundle
      - True → vérification système par défaut
      - False → désactivée (uniquement acceptable en LAN strict isolé)
    """
    nodes: tuple[NodeConfig, ...]
    tls_verify: bool | str = True

    def get(self, node_id: str) -> NodeConfig | None:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None


def load_nodes_config(path: Path) -> ClusterConfig:
    """
    Charge et valide `nodes.yaml`. Lève FileNotFoundError ou ValueError sinon.

    À appeler une fois au démarrage du `ClusterManager` quand
    `settings.cluster_mode == "cluster"`. Le mode `local` ne touche pas ce
    fichier.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"Fichier de configuration du cluster introuvable : {path}\n"
            f"Créez ce fichier ou définissez CLUSTER_NODES_PATH dans .env. "
            f"Voir gateway/deploy/nodes.yaml.example pour un modèle."
        )

    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)  # safe_load — jamais yaml.load()

    if not isinstance(data, dict):
        raise ValueError(f"Format invalide dans {path} : racine doit être un dict")
    if "nodes" not in data:
        raise ValueError(f"Format invalide dans {path} : clé 'nodes' manquante")

    cluster_section = data.get("cluster", {}) or {}
    tls_verify = _parse_tls_verify(cluster_section.get("tls_verify", True))

    raw_nodes = data["nodes"]
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValueError(
            f"Format invalide dans {path} : 'nodes' doit être une liste non vide"
        )

    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    nodes: list[NodeConfig] = []
    for entry in raw_nodes:
        node = _parse_node_entry(entry, source=path)
        if node.id in seen_ids:
            raise ValueError(f"ID de nœud dupliqué dans {path} : '{node.id}'")
        if node.base_url in seen_urls:
            raise ValueError(
                f"base_url dupliquée dans {path} : '{node.base_url}'. "
                f"Chaque nœud doit avoir une URL unique."
            )
        seen_ids.add(node.id)
        seen_urls.add(node.base_url)
        nodes.append(node)

    _check_tls_verify(tls_verify, nodes, source=path)

    log.info(
        "Configuration cluster chargée depuis %s — %d nœud(s), tls_verify=%r",
        path, len(nodes), tls_verify,
    )
    return ClusterConfig(nodes=tuple(nodes), tls_verify=tls_verify)


def _parse_node_entry(entry: object, *, source: Path) -> NodeConfig:
    if not isinstance(entry, dict):
        raise ValueError(f"Entrée de nœud invalide dans {source} : {entry!r}")

    node_id = str(entry.get("id", "")).strip()
    if not node_id:
        raise ValueError(f"Nœud sans 'id' dans {source}")
    if not _NODE_ID_RE.match(node_id):
        raise ValueError(
            f"ID de nœud invalide : {node_id!r}. "
            f"Autorisé : minuscules, chiffres, tirets, points, underscores. "
            f"Doit commencer par une lettre ou un chiffre. Max 63 caractères."
        )

    base_url = str(entry.get("base_url", "")).strip()
    _validate_base_url(node_id, base_url)
    # Normalisation : on retire le slash final pour éviter les doublons
    # entre "https://x/" et "https://x" → URL canonique.
    base_url = base_url.rstrip("/")

    description = str(entry.get("description", "")).strip()

    return NodeConfig(id=node_id, base_url=base_url, description=description)


def _validate_base_url(node_id: str, base_url: str) -> None:
    if not base_url:
        raise ValueError(f"[{node_id}] base_url manquante")

    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"[{node_id}] base_url doit utiliser http:// ou https:// — reçu : {base_url!r}"
        )
    if not parsed.hostname:
        raise ValueError(f"[{node_id}] base_url sans hôte : {base_url!r}")
    if parsed.path not in ("", "/"):
        raise ValueError(
            f"[{node_id}] base_url ne doit pas inclure de chemin : {base_url!r}"
        )
    if parsed.scheme == "http":
        log.warning(
            "[%s] base_url utilise HTTP en clair (%s). Acceptable en dev local mais "
            "à remplacer par HTTPS pour tout déploiement réseau partagé.",
            node_id, base_url,
        )


def _check_tls_verify(
    tls_verify: bool | str, nodes: list[NodeConfig], *, source: Path
) -> None:
    """
    Avertit (et, en mode strict opt-in, refuse) quand la vérification TLS est
    désactivée. Symétrique au warning HTTP en clair de `_validate_base_url`.

    - `tls_verify=False` seul : warning explicite, vérification TLS totalement
      désactivée côté httpx → vulnérable au MITM sur le LAN inter-nœuds.
    - `tls_verify=False` + au moins un nœud en `https://` : incohérence forte
      (on paie le coût TLS mais sans jamais vérifier le certificat serveur —
      aucune protection contre un `llama_url` usurpé). Warning renforcé.
      Refus dur uniquement si `CLUSTER_STRICT_TLS_VERIFY=true` est positionné :
      par défaut on NE bloque PAS, pour ne pas casser les déploiements de dev
      existants qui utilisent des certs auto-signés avec tls_verify: false.
    """
    if tls_verify is not False:
        return

    log.warning(
        "Configuration cluster (%s) : tls_verify=false — la vérification du "
        "certificat TLS des node_agent est DÉSACTIVÉE. À réserver strictement "
        "au dev/LAN isolé et de confiance : ce réglage expose la liaison "
        "orchestrateur ↔ node-agent (et donc les prompts) à une interception "
        "(MITM). Pour la production, utilisez tls_verify avec un chemin vers "
        "un bundle CA valide.",
        source,
    )

    https_nodes = [n.id for n in nodes if urlparse(n.base_url).scheme == "https"]
    if not https_nodes:
        return

    strict = os.environ.get(_STRICT_TLS_ENV, "").strip().lower() in ("true", "1", "yes")
    message = (
        "Configuration cluster (%s) incohérente : les nœuds %s utilisent "
        "https:// mais tls_verify=false — le certificat serveur n'est jamais "
        "vérifié. Cette combinaison n'apporte AUCUNE protection contre un "
        "'llama_url' usurpé (MITM) tout en payant le coût TLS. Corrigez en "
        "pointant tls_verify vers un bundle CA, ou repassez ces nœuds en "
        "http:// si le LAN est réellement isolé."
    )
    if strict:
        raise ValueError(message % (source, https_nodes))
    log.warning(message, source, https_nodes)


def _parse_tls_verify(raw: object) -> bool | str:
    """
    Accepte : bool, "true"/"false" (insensible à la casse), ou un chemin vers
    un bundle CA. Tout le reste est refusé.
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in ("true", "yes", "on"):
            return True
        if lowered in ("false", "no", "off"):
            return False
        # Chemin de CA — on vérifie qu'il existe pour cracher tôt si erreur de conf
        ca_path = Path(raw).expanduser()
        if not ca_path.exists():
            raise ValueError(
                f"tls_verify pointe vers un fichier inexistant : {ca_path}. "
                f"Indiquez un bundle CA valide, ou true/false."
            )
        return str(ca_path)
    raise ValueError(
        f"tls_verify doit être un bool ou un chemin vers un bundle CA — reçu : {raw!r}"
    )
