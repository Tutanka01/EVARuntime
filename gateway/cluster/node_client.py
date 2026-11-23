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
    (4xx/5xx, JSON malformé, schéma non conforme). Les heartbeats appliquent
    le même seuil offline que pour une panne réseau.
"""
from __future__ import annotations

import logging
from typing import Protocol
from urllib.parse import quote, urlparse

import httpx
from pydantic import ValidationError

from .node_protocol import (
    LoadRequest,
    LoadResponse,
    NodeHealth,
    NodeStatus,
    OperationStatusResponse,
    UnloadResponse,
)

log = logging.getLogger(__name__)


class NodeUnreachableError(RuntimeError):
    """Le nœud ne répond pas (réseau, DNS, timeout, conn refused)."""


# 4xx qui ne prouvent PAS un refus de l'agent : timeout ou limitation posés par
# un intermédiaire, ou invitation explicite à réessayer.
_RETRYABLE_CLIENT_STATUSES = frozenset({408, 425, 429})


class NodeProtocolError(RuntimeError):
    """Le nœud répond mais l'échange est invalide (4xx/5xx ou payload non conforme)."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        # Statut HTTP de la réponse fautive ; None pour un payload non conforme.
        self.status_code = status_code

    @property
    def is_rejection(self) -> bool:
        """
        True si l'agent a explicitement refusé la requête (4xx définitif).

        Pour `POST /agent/models/load`, l'agent valide définition, identité et
        échéance AVANT d'enregistrer l'opération : un refus prouve que rien n'a
        été accepté, contrairement à un timeout ou à un 5xx.
        """
        return (
            self.status_code is not None
            and 400 <= self.status_code < 500
            and self.status_code not in _RETRYABLE_CLIENT_STATUSES
        )


# ── Interface commune ─────────────────────────────────────────────────────────

class NodeClient(Protocol):
    """Contrat exposé par chaque implémentation de client de nœud."""

    node_id: str
    base_url: str  # Informatif (logs, /admin/cluster). "in-process" pour Local.

    async def health(self) -> NodeHealth: ...
    async def status(self) -> NodeStatus: ...
    async def metrics(self) -> dict: ...
    async def start_load_model(
        self,
        model_dict: dict,
        *,
        deployment_digest: str,
        generation_id: str,
        operation_id: str,
        deadline_seconds: float,
    ) -> LoadResponse: ...
    async def get_operation_status(self, operation_id: str) -> OperationStatusResponse: ...
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
        unload_timeout_seconds: float = 30.0,
        verify: bool | str = True,
    ) -> None:
        self.node_id = node_id
        self.base_url = base_url.rstrip("/")
        self._request_timeout = timeout_seconds
        self._health_timeout = health_timeout_seconds
        # Budget par défaut d'un chargement quand la définition n'en porte pas
        # (`ClusterManager._load_deadline_seconds`) : un gros GGUF peut prendre
        # plusieurs minutes côté agent.
        self._load_timeout = load_timeout_seconds
        # Un unload attend côté agent l'arrêt du llama-server (SIGTERM, grâce
        # 10 s, puis SIGKILL et reap jusqu'à 15 s) : le timeout court du plan
        # de contrôle déclarait « incertain » un unload simplement lent.
        self._unload_timeout = unload_timeout_seconds

        # trust_env=False : ce client porte AGENT_SECRET, le secret partagé qui
        # contrôle TOUS les nœuds. Sans lui, httpx lit HTTPS_PROXY/ALL_PROXY et
        # y route aussi les URL privées : un proxy d'environnement (ou un MITM
        # rendu possible par `tls_verify: false`) recevrait le secret du plan de
        # contrôle. Même règle que le data plane (`proxy.py`) et `metrics.py`.
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout_seconds,
            verify=verify,
            trust_env=False,
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

    async def start_load_model(
        self,
        model_dict: dict,
        *,
        deployment_digest: str,
        generation_id: str,
        operation_id: str,
        deadline_seconds: float,
    ) -> LoadResponse:
        payload = LoadRequest(
            model=model_dict,
            deployment_digest=deployment_digest,
            generation_id=generation_id,
            operation_id=operation_id,
            deadline_seconds=deadline_seconds,
        ).model_dump()
        # Le POST ne doit attendre que l'acceptation de l'opération. Un éventuel
        # timeout est ensuite résolu par GET /agent/operations/{id}, avec le même
        # identifiant, au lieu de lancer un doublon sur un autre nœud.
        raw = await self._post(
            "/agent/models/load",
            json=payload,
            timeout=min(self._request_timeout, max(0.1, deadline_seconds)),
        )
        resp = self._parse(LoadResponse, raw)
        if not resp.deployment_digest:
            resp = resp.model_copy(update={"deployment_digest": deployment_digest})
        if not resp.generation_id:
            resp = resp.model_copy(update={"generation_id": generation_id})
        if not resp.operation_id:
            resp = resp.model_copy(update={"operation_id": operation_id})
        if resp.state == "ready":
            return self._with_trusted_url(resp)
        return resp

    async def get_operation_status(self, operation_id: str) -> OperationStatusResponse:
        safe_operation_id = quote(operation_id, safe="._-")
        raw = await self._get(f"/agent/operations/{safe_operation_id}")
        response = self._parse(OperationStatusResponse, raw)
        if response.state == "ready":
            response = self._with_trusted_url(response)
        return response

    def _with_trusted_url(self, resp: LoadResponse) -> LoadResponse:
        if resp.port is None or not resp.llama_url:
            raise NodeProtocolError(
                f"Nœud '{self.node_id}' : réponse READY sans port/llama_url"
            )
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
        # urlparse().hostname retire les crochets IPv6. Ils sont obligatoires
        # quand on reconstruit une URL avec un port.
        url_host = f"[{hostname}]" if ":" in hostname else hostname
        trusted_url = f"http://{url_host}:{resp.port}"

        # Signal de falsification / mauvaise config : l'agent a renvoyé un hôte
        # différent de celui du nœud. On loggue mais on utilise TOUJOURS l'URL
        # de confiance reconstruite.
        reported_host = urlparse(resp.llama_url).hostname if resp.llama_url else None
        # 0.0.0.0 / :: sont les binds attendus d'un llama-server exposé sur
        # les interfaces du nœud ; ce ne sont ni des destinations routables ni
        # un signal de falsification. L'URL utilisée reste toujours reconstruite.
        wildcard_hosts = {"0.0.0.0", "::"}
        if (
            reported_host
            and reported_host not in wildcard_hosts
            and reported_host != hostname
        ):
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
        raw = await self._post(
            f"/agent/models/{model_id}/unload", timeout=self._unload_timeout
        )
        return self._parse(UnloadResponse, raw)

    async def unload_all(self) -> None:
        # L'agent décharge ses modèles en parallèle : un seul budget d'unload
        # couvre l'arrêt du plus lent.
        await self._post("/agent/unload-all", timeout=self._unload_timeout)

    async def close(self) -> None:
        await self._client.aclose()

    # ── Helpers HTTP ──────────────────────────────────────────────────────────

    async def _get(self, path: str, *, timeout: float | None = None) -> dict:
        try:
            kwargs: dict = {}
            if timeout is not None:
                kwargs["timeout"] = timeout
            resp = await self._client.get(path, **kwargs)
        except httpx.RequestError as exc:
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
        except httpx.RequestError as exc:
            raise NodeUnreachableError(
                f"Nœud '{self.node_id}' injoignable ({path}) : {exc.__class__.__name__}"
            ) from exc
        return self._extract_json(resp, path)

    def _extract_json(self, resp: httpx.Response, path: str) -> dict:
        if resp.status_code >= 500:
            raise NodeProtocolError(
                f"Nœud '{self.node_id}' a renvoyé {resp.status_code} sur {path} : "
                f"{resp.text[:300]}",
                status_code=resp.status_code,
            )
        if resp.status_code >= 400:
            # 4xx : pas un problème de transport. L'agent rejette explicitement
            # (modèle inconnu, payload invalide…). On remonte le message.
            raise NodeProtocolError(
                f"Nœud '{self.node_id}' a refusé {path} ({resp.status_code}) : "
                f"{resp.text[:300]}",
                status_code=resp.status_code,
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
        health(), status(), unload_model(id), unload_all(), et soit le contrat
        d'opérations (start_load_model + get_operation_status), soit un
        load_model(dict) synchrone adapté ci-dessous.
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

    async def start_load_model(
        self,
        model_dict: dict,
        *,
        deployment_digest: str,
        generation_id: str,
        operation_id: str,
        deadline_seconds: float,
    ) -> LoadResponse:
        """Expose le nouveau contrat tout en gardant les backends de test legacy."""
        starter = getattr(self._backend, "start_load_model", None)
        if starter is not None:
            return await starter(
                model_dict,
                deployment_digest=deployment_digest,
                generation_id=generation_id,
                operation_id=operation_id,
                deadline_seconds=deadline_seconds,
            )
        # Backend synchrone in-process : son issue EST l'état terminal de
        # l'opération (aucune réponse tardive possible). Un refus explicite
        # garde sa sémantique d'agent ; toute autre exception devient `failed`
        # au lieu d'une incertitude réseau qui n'existe pas ici.
        identity = {
            "deployment_digest": deployment_digest,
            "generation_id": generation_id,
            "operation_id": operation_id,
            "deadline_seconds": deadline_seconds,
        }
        try:
            response = await self._backend.load_model(model_dict)
        except NodeProtocolError as exc:
            if exc.is_rejection:
                raise
            return LoadResponse(
                model_id=model_dict.get("id", ""),
                state="failed",
                progress=0.0,
                error_code="load_failed",
                error_status=exc.status_code,
                message=str(exc),
                **identity,
            )
        except Exception as exc:
            return LoadResponse(
                model_id=model_dict.get("id", ""),
                state="failed",
                progress=0.0,
                error_code="load_failed",
                message=str(exc),
                **identity,
            )
        return response.model_copy(
            update={**identity, "state": "ready", "progress": 1.0}
        )

    async def get_operation_status(self, operation_id: str) -> OperationStatusResponse:
        getter = getattr(self._backend, "get_operation_status", None)
        if getter is None:
            raise NodeProtocolError(
                f"Nœud local '{self.node_id}' ne fournit pas le statut d'opération"
            )
        response = await getter(operation_id)
        return OperationStatusResponse.model_validate(response.model_dump())

    async def unload_model(self, model_id: str) -> UnloadResponse:
        return await self._backend.unload_model(model_id)

    async def unload_all(self) -> None:
        await self._backend.unload_all()

    async def close(self) -> None:
        # Rien à fermer en in-process. Le backend est arrêté via shutdown global.
        return None
