"""
NodeClient — interface unifiée orchestrateur → nœud.

Deux implémentations, même API :
  - RemoteNodeClient : appelle l'agent distant via HTTPS + Bearer agent_secret.
  - LocalNodeAdapter : appelle un ServerManager / ModelManager local in-process.

L'interface utilise les DTOs Pydantic de node_protocol — ce qui garantit que les
deux chemins (mono-nœud / multi-nœud) traversent EXACTEMENT le même schéma de
données. Cela simplifie les tests et évite les divergences silencieuses.

Exceptions :
  - NodeUnreachableError : le nœud ne répond pas (timeout, DNS, conn refused).
    Le ClusterManager le mettra `offline` au prochain heartbeat.
  - NodeProtocolError   : le nœud répond mais l'échange est invalide
    (4xx/5xx, JSON malformé, schéma non conforme). Le nœud reste `online`,
    on remonte l'erreur au caller.
"""
from __future__ import annotations

import logging
from typing import Protocol
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError

from .node_protocol import (
    LoadRequest,
    LoadResponse,
    NodeHealth,
    NodeStatus,
    UnloadResponse,
)

log = logging.getLogger(__name__)


class NodeUnreachableError(RuntimeError):
    """Le nœud ne répond pas (réseau, DNS, timeout, conn refused)."""


class NodeProtocolError(RuntimeError):
    """Le nœud répond mais l'échange est invalide (4xx/5xx ou payload non conforme)."""


# ── Interface commune ─────────────────────────────────────────────────────────

class NodeClient(Protocol):
    """Contrat exposé par chaque implémentation de client de nœud."""

    node_id: str
    base_url: str  # Informatif (logs, /admin/cluster). "in-process" pour Local.

    async def health(self) -> NodeHealth: ...
    async def status(self) -> NodeStatus: ...
    async def metrics(self) -> dict: ...
    async def load_model(self, model_dict: dict) -> LoadResponse: ...
    async def unload_model(self, model_id: str) -> UnloadResponse: ...
    async def unload_all(self) -> None: ...
    async def close(self) -> None: ...


# ── Implémentation HTTPS (cluster mode) ───────────────────────────────────────

class RemoteNodeClient:
    """
    Client HTTPS vers un node-agent distant.

    Sécurité :
      - Authorization: Bearer <agent_secret> sur toutes les requêtes.
      - TLS vérifié selon `verify` (chemin CA, True, ou False en LAN strict).
      - Aucun secret retourné par l'agent n'est journalisé.
    """

    def __init__(
        self,
        node_id: str,
        base_url: str,
        agent_secret: str,
        *,
        timeout_seconds: float = 10.0,
        health_timeout_seconds: float = 3.0,
        load_timeout_seconds: float = 300.0,
        verify: bool | str = True,
    ) -> None:
        self.node_id = node_id
        self.base_url = base_url.rstrip("/")
        self._health_timeout = health_timeout_seconds
        # Timeout dédié au chargement de modèle — bien plus long que le timeout
        # du plan de contrôle car un gros GGUF peut prendre plusieurs minutes.
        self._load_timeout = load_timeout_seconds

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout_seconds,
            verify=verify,
            headers={
                "Authorization": f"Bearer {agent_secret}",
                "User-Agent": "llm-gateway-orchestrator",
            },
        )

    # ── Lecture ───────────────────────────────────────────────────────────────

    async def health(self) -> NodeHealth:
        # Timeout dédié plus court — le heartbeat doit dégager vite.
        raw = await self._get("/agent/health", timeout=self._health_timeout)
        return self._parse(NodeHealth, raw)

    async def status(self) -> NodeStatus:
        raw = await self._get("/agent/status")
        return self._parse(NodeStatus, raw)

    async def metrics(self) -> dict:
        """
        Métriques llama-server agrégées du nœud (JSON compact par model_id).
        Additif : purement observabilité, hors chemin d'inférence. Le payload ne
        contient que des compteurs/gauges — jamais de contenu de prompt.
        """
        raw = await self._get("/agent/metrics", timeout=self._health_timeout)
        # Pas de schéma Pydantic strict (métriques best-effort, clés variables) :
        # on valide juste que c'est bien un objet JSON.
        if not isinstance(raw, dict):
            raise NodeProtocolError(
                f"Nœud '{self.node_id}' : /agent/metrics n'a pas renvoyé un objet JSON."
            )
        return raw

    # ── Mutations ─────────────────────────────────────────────────────────────

    async def load_model(self, model_dict: dict) -> LoadResponse:
        payload = LoadRequest(model=model_dict).model_dump()
        # Timeout long : le chargement d'un gros modèle dépasse largement le
        # timeout court du plan de contrôle.
        raw = await self._post(
            "/agent/models/load", json=payload, timeout=self._load_timeout
        )
        resp = self._parse(LoadResponse, raw)
        # Zéro confiance dans le llama_url renvoyé par l'agent (SSRF /
        # exfiltration de prompts) : on reconstruit l'URL à partir de l'hôte
        # RÉEL du nœud (base_url, source de confiance) + le port retourné.
        # Ce chemin couvre aussi already_loaded=True (idempotent).
        trusted_url = self._trusted_llama_url(resp)
        return resp.model_copy(update={"llama_url": trusted_url})

    def _trusted_llama_url(self, resp: LoadResponse) -> str:
        """
        Reconstruit l'URL du llama-server à partir de l'hôte de confiance du
        nœud (self.base_url, issu de nodes.yaml) et du port retourné par l'agent.
        Le llama-server tourne sur le même hôte physique que l'agent, sur un
        autre port, en HTTP simple (interne, non exposé).
        """
        hostname = urlparse(self.base_url).hostname
        if not hostname:
            raise NodeProtocolError(
                f"Nœud '{self.node_id}' : base_url sans hostname exploitable "
                f"({self.base_url!r})"
            )
        if not isinstance(resp.port, int) or not (1 <= resp.port <= 65535):
            raise NodeProtocolError(
                f"Nœud '{self.node_id}' : port llama-server invalide "
                f"({resp.port!r})"
            )
        trusted_url = f"http://{hostname}:{resp.port}"

        # Signal de falsification / mauvaise config : l'agent a renvoyé un hôte
        # différent de celui du nœud. On loggue mais on utilise TOUJOURS l'URL
        # de confiance reconstruite.
        reported_host = urlparse(resp.llama_url).hostname if resp.llama_url else None
        if reported_host and reported_host != hostname:
            log.warning(
                "Nœud '%s' : llama_url renvoyé (hôte=%s) diffère de l'hôte du "
                "nœud (%s) — URL de confiance utilisée à la place",
                self.node_id,
                reported_host,
                hostname,
            )
        return trusted_url

    async def unload_model(self, model_id: str) -> UnloadResponse:
        # model_id est validé en amont par ModelRegistry (regex stricte).
        # On évite tout risque d'injection dans l'URL en le passant tel quel —
        # httpx ne fait pas d'encoding ici car il n'y a aucun caractère spécial autorisé.
        raw = await self._post(f"/agent/models/{model_id}/unload")
        return self._parse(UnloadResponse, raw)

    async def unload_all(self) -> None:
        await self._post("/agent/unload-all")

    async def close(self) -> None:
        await self._client.aclose()

    # ── Helpers HTTP ──────────────────────────────────────────────────────────

    async def _get(self, path: str, *, timeout: float | None = None) -> dict:
        try:
            kwargs: dict = {}
            if timeout is not None:
                kwargs["timeout"] = timeout
            resp = await self._client.get(path, **kwargs)
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
            raise NodeUnreachableError(
                f"Nœud '{self.node_id}' injoignable ({path}) : {exc.__class__.__name__}"
            ) from exc
        return self._extract_json(resp, path)

    async def _post(
        self, path: str, *, json: dict | None = None, timeout: float | None = None
    ) -> dict:
        try:
            kwargs: dict = {}
            if timeout is not None:
                kwargs["timeout"] = timeout
            resp = await self._client.post(path, json=json, **kwargs)
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
            raise NodeUnreachableError(
                f"Nœud '{self.node_id}' injoignable ({path}) : {exc.__class__.__name__}"
            ) from exc
        return self._extract_json(resp, path)

    def _extract_json(self, resp: httpx.Response, path: str) -> dict:
        if resp.status_code >= 500:
            raise NodeProtocolError(
                f"Nœud '{self.node_id}' a renvoyé {resp.status_code} sur {path} : "
                f"{resp.text[:300]}"
            )
        if resp.status_code >= 400:
            # 4xx : pas un problème de transport. L'agent rejette explicitement
            # (modèle inconnu, payload invalide…). On remonte le message.
            raise NodeProtocolError(
                f"Nœud '{self.node_id}' a refusé {path} ({resp.status_code}) : "
                f"{resp.text[:300]}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise NodeProtocolError(
                f"Nœud '{self.node_id}' a renvoyé un JSON invalide sur {path}"
            ) from exc

    @staticmethod
    def _parse(model_cls, raw: dict):
        try:
            return model_cls.model_validate(raw)
        except ValidationError as exc:
            raise NodeProtocolError(
                f"Réponse non conforme au schéma {model_cls.__name__} : {exc}"
            ) from exc


# ── Implémentation in-process (local mode + tests) ────────────────────────────
#
# LocalNodeAdapter est volontairement minimaliste. Le mode local "officiel"
# (CLUSTER_MODE=local) court-circuite cet adaptateur et utilise LocalModelManager
# directement — c'est ce qui garantit la rétrocompatibilité.
#
# Cet adaptateur sert :
#   1. Aux tests du ClusterManager (pour avoir deux implémentations
#      interchangeables sans monter de serveur HTTP).
#   2. À un éventuel cas avancé où l'orchestrateur veut piloter en plus d'agents
#      distants un sous-processus local (V2).


class LocalNodeAdapter:
    """
    Adaptateur in-process qui présente la même API qu'un nœud distant.

    Le LocalBackend passé en injection doit exposer les méthodes asynchrones :
        health(), status(), load_model(dict), unload_model(id), unload_all().
    Cela permet de mocker ou d'injecter n'importe quelle implémentation
    (ServerManager + état local, ou un fake en tests).
    """

    def __init__(self, node_id: str, backend) -> None:
        self.node_id = node_id
        self.base_url = "in-process"
        self._backend = backend

    async def health(self) -> NodeHealth:
        return await self._backend.health()

    async def status(self) -> NodeStatus:
        return await self._backend.status()

    async def metrics(self) -> dict:
        # Best-effort : tous les backends de test n'implémentent pas metrics().
        backend_metrics = getattr(self._backend, "metrics", None)
        if backend_metrics is None:
            return {}
        return await backend_metrics()

    async def load_model(self, model_dict: dict) -> LoadResponse:
        return await self._backend.load_model(model_dict)

    async def unload_model(self, model_id: str) -> UnloadResponse:
        return await self._backend.unload_model(model_id)

    async def unload_all(self) -> None:
        await self._backend.unload_all()

    async def close(self) -> None:
        # Rien à fermer en in-process. Le backend est arrêté via shutdown global.
        return None
