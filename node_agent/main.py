"""
Node Agent — FastAPI léger qui pilote llama-server sur un nœud GPU.

Exposé sur HTTPS :9443 (TLS classique), protégé par Bearer AGENT_SECRET.
L'orchestrateur (ClusterManager) est le seul client légitime de ces endpoints.

Import order / sys.path :
  1. node_agent/ en tête de sys.path → `from config import settings` charge
     node_agent/config.py (paramètres locaux du nœud, pas ceux de la gateway).
  2. gateway/ ensuite → `from model_registry import ...` et
     `from server_manager import ...` chargent les modules gateway réutilisés.
  3. gateway/cluster/ pour les DTOs node_protocol.

Cette séquence garantit qu'aucune variable de gateway n'entre en conflit
avec la config locale de l'agent.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import httpx

# ── Initialisation sys.path ───────────────────────────────────────────────────
_AGENT_DIR = Path(__file__).resolve().parent
_GATEWAY_DIR = _AGENT_DIR.parent / "gateway"

# node_agent/ AVANT gateway/ → `from config import settings` → agent/config.py
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))
# gateway/ pour model_registry, server_manager
if str(_GATEWAY_DIR) not in sys.path:
    sys.path.insert(1, str(_GATEWAY_DIR))
# gateway/ parent pour `from cluster.node_protocol import ...`
if str(_GATEWAY_DIR.parent) not in sys.path:
    sys.path.insert(2, str(_GATEWAY_DIR.parent))

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# Chargé APRÈS avoir ajusté sys.path
from config import settings  # → node_agent/config.py
from integrity import attest_model_artifacts
from llama_version import enforce_llama_min_build
from model_registry import IntegrityError, ModelRegistry
from server_manager import ModelState, ServerManager, format_url_host
from cluster.node_protocol import (
    LoadRequest,
    LoadResponse,
    ModelStateOnNode,
    NodeHealth,
    NodeStatus,
    OperationStatusResponse,
    UnloadResponse,
    deployment_digest as compute_deployment_digest,
)

log = logging.getLogger(__name__)
_bearer = HTTPBearer(auto_error=True)


# ── Authentification ──────────────────────────────────────────────────────────

def require_agent_secret(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> None:
    # Fail-closed : l'agent écoute sur le réseau (0.0.0.0 par défaut) — un
    # secret laissé à sa valeur d'exemple équivaudrait à aucune authentification.
    if settings.agent_secret_is_placeholder():
        log.critical(
            "Requête refusée : AGENT_SECRET non configuré (vide ou CHANGE_ME_*). "
            "Définissez un secret fort identique sur l'orchestrateur et l'agent."
        )
        raise HTTPException(
            status_code=503,
            detail="Agent désactivé : AGENT_SECRET non configuré.",
        )
    # Comparaison constant-time — évite les attaques par timing sur le secret
    if not secrets.compare_digest(
        creds.credentials.encode(), settings.agent_secret.encode()
    ):
        raise HTTPException(status_code=401, detail="Agent secret invalide.")


# ── Registre de validation ────────────────────────────────────────────────────

def _make_validator_registry() -> ModelRegistry:
    """
    Crée un ModelRegistry vide (fichier YAML temporaire) pour valider les
    model_dicts reçus de l'orchestrateur. On n'a pas besoin d'un models.yaml
    permanent sur l'agent — la seule opération utilisée est `_parse_entry()`.
    """
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    )
    tmp.write("models: []\n")
    tmp.flush()
    tmp.close()
    tmp_path = Path(tmp.name)
    try:
        return ModelRegistry(
            config_path=tmp_path,
            allowed_model_dirs=settings.allowed_model_dirs_list() or None,
        )
    finally:
        tmp_path.unlink(missing_ok=True)


def _validate_model_files(model) -> None:
    """Fail-fast sur les artefacts présents sur CE nœud, avant toute réservation."""
    files = [("GGUF", model.path)]
    if model.mmproj_path is not None:
        files.append(("projecteur multimodal", model.mmproj_path))
    for label, path in files:
        if not path.is_file():
            raise HTTPException(
                status_code=422,
                detail=f"Fichier {label} introuvable sur le nœud : {path}",
            )
        if not os.access(path, os.R_OK):
            raise HTTPException(
                status_code=422,
                detail=f"Fichier {label} non lisible par le node-agent : {path}",
            )


# ── État de l'agent ───────────────────────────────────────────────────────────


_LOAD_DEADLINE_MAX_SECONDS = 86_400.0
_OPERATION_RETENTION_SECONDS = 600.0
_MAX_RETAINED_OPERATIONS = 256


@dataclass
class _LoadOperation:
    model_id: str
    model: object
    deployment_digest: str
    generation_id: str
    operation_id: str
    deadline_seconds: float
    created_at: float = field(default_factory=time.monotonic)
    state: str = "accepted"
    progress: float = 0.0
    task: asyncio.Task | None = None
    response: LoadResponse | None = None
    error_status: int = 500
    error_detail: str = ""
    completed_at: float | None = None


class _AgentState:
    """
    Singleton local : pool de ServerManager + pool de ports + budget VRAM.
    Même logique que LocalModelManager, mais sans couche de routage.
    """

    def __init__(self) -> None:
        self._validator = _make_validator_registry()
        self._managers: dict[str, ServerManager] = {}
        self._allocated_ports: dict[str, int] = {}
        # Sérialise load/unload pour un même modèle sans bloquer les chargements
        # indépendants sur les autres ports/nœuds.
        self._model_locks: dict[str, asyncio.Lock] = {}
        self._port_pool: list[int] = list(range(
            settings.base_llama_port,
            settings.base_llama_port + settings.max_loaded_models,
        ))
        self._lock = asyncio.Lock()
        # L'opération est la clé d'idempotence du plan de contrôle. Les tâches
        # continuent après une déconnexion HTTP et leur résultat terminal est
        # conservé pour les réponses tardives/polling.
        self._operations: dict[str, _LoadOperation] = {}
        self._active_operations: dict[str, str] = {}
        self._manager_identity: dict[str, tuple[str, str, str]] = {}
        self._closing = False

    def _used_vram(self) -> float:
        return sum(
            mgr.model.vram_gb
            for mgr in self._managers.values()
            if mgr.state in (ModelState.READY, ModelState.LOADING)
        )

    def _available_vram(self) -> float:
        return settings.effective_vram_budget_gb() - self._used_vram()

    @staticmethod
    def _reported_llama_url(port: int) -> str:
        return f"http://{format_url_host(settings.llama_server_host)}:{port}"

    @staticmethod
    def _effective_load_deadline(model) -> float:
        timeout = getattr(model, "load_timeout_seconds", None)
        if timeout is None:
            timeout = settings.model_load_timeout_seconds
        return min(_LOAD_DEADLINE_MAX_SECONDS, max(1.0, float(timeout) + 10.0))

    def _parse_model(self, model_dict: dict):
        """Parse sans I/O la définition avant d'enregistrer l'opération."""
        try:
            model = self._validator._parse_entry(model_dict)
        except (ValueError, KeyError) as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Définition de modèle invalide : {exc}",
            ) from exc

        return model

    async def _validate_model_runtime(self, model) -> None:
        """Contrôles coûteux exécutés après l'acceptation HTTP."""
        _validate_model_files(model)
        if (
            model.sha256 is not None
            or "vision" in getattr(model, "capabilities", ())
        ):
            try:
                await attest_model_artifacts(model)
            except IntegrityError as exc:
                raise HTTPException(
                    status_code=422,
                    detail=f"Vérification d'intégrité échouée : {exc}",
                ) from exc

    def _snapshot_operation(self, operation: _LoadOperation) -> LoadResponse:
        if operation.response is not None:
            return operation.response.model_copy(
                update={
                    "state": operation.state,
                    "progress": operation.progress,
                    "deadline_seconds": operation.deadline_seconds,
                }
            )
        return LoadResponse(
            model_id=operation.model_id,
            deployment_digest=operation.deployment_digest,
            generation_id=operation.generation_id,
            operation_id=operation.operation_id,
            state=operation.state,
            progress=operation.progress,
            deadline_seconds=operation.deadline_seconds,
        )

    def _prune_operations_locked(self) -> None:
        now = time.monotonic()
        terminal = {"ready", "failed", "conflict", "deadline_exceeded"}
        for operation_id, operation in list(self._operations.items()):
            finished_at = operation.completed_at
            if (
                operation.state in terminal
                and operation_id not in self._active_operations.values()
                and finished_at is not None
                and now - finished_at > _OPERATION_RETENTION_SECONDS
            ):
                self._operations.pop(operation_id, None)
        if len(self._operations) <= _MAX_RETAINED_OPERATIONS:
            return
        candidates = sorted(
            (
                operation
                for operation in self._operations.values()
                if operation.state in terminal
                and operation.operation_id not in self._active_operations.values()
            ),
            key=lambda operation: operation.created_at,
        )
        for operation in candidates:
            if len(self._operations) <= _MAX_RETAINED_OPERATIONS:
                break
            self._operations.pop(operation.operation_id, None)

    @staticmethod
    def _identity_conflict(model_id: str) -> HTTPException:
        return HTTPException(
            status_code=409,
            detail=(
                f"Le modèle '{model_id}' est déjà associé à une autre "
                "génération ou définition de déploiement."
            ),
        )

    async def start_load(
        self,
        model_dict: dict,
        *,
        deployment_digest: str | None = None,
        generation_id: str | None = None,
        operation_id: str | None = None,
        deadline_seconds: float | None = None,
    ) -> LoadResponse:
        """Accepte un chargement et retourne sans attendre llama-server READY."""
        if self._closing:
            raise HTTPException(status_code=503, detail="Agent en cours d'arrêt.")
        # Rejouer la même opération doit être un simple observe, même si la
        # première requête était encore en train d'attester un GGUF volumineux.
        if operation_id is not None:
            async with self._lock:
                existing = self._operations.get(operation_id)
                if existing is not None:
                    if (
                        model_dict.get("id") != existing.model_id
                        or (
                            deployment_digest is not None
                            and deployment_digest != existing.deployment_digest
                        )
                        or (
                            generation_id is not None
                            and generation_id != existing.generation_id
                        )
                    ):
                        raise self._identity_conflict(existing.model_id)
                    return self._snapshot_operation(existing)

        model = self._parse_model(model_dict)
        computed_digest = compute_deployment_digest(model_dict)
        if (
            deployment_digest is not None
            and deployment_digest != computed_digest
        ):
            raise HTTPException(
                status_code=409,
                detail=f"Digest de déploiement incohérent pour '{model.id}'.",
            )
        digest = deployment_digest or computed_digest
        model_lock = self._model_locks.setdefault(model.id, asyncio.Lock())
        async with model_lock:
            async with self._lock:
                self._prune_operations_locked()
                identity = self._manager_identity.get(model.id)
                active_id = self._active_operations.get(model.id)
                active = self._operations.get(active_id) if active_id else None
                if generation_id is None:
                    if active is not None and active.deployment_digest == digest:
                        generation_id = active.generation_id
                    elif identity is not None and identity[0] == digest:
                        generation_id = identity[1]
                    else:
                        generation_id = uuid4().hex
                if operation_id is None:
                    operation_id = uuid4().hex
                deadline = (
                    self._effective_load_deadline(model)
                    if deadline_seconds is None
                    else float(deadline_seconds)
                )
                if not 0 < deadline <= _LOAD_DEADLINE_MAX_SECONDS:
                    raise HTTPException(
                        status_code=422,
                        detail="deadline_seconds doit être strictement positive et bornée.",
                    )

                existing_operation = self._operations.get(operation_id)
                if existing_operation is not None:
                    if (
                        existing_operation.model_id != model.id
                        or existing_operation.deployment_digest != digest
                        or existing_operation.generation_id != generation_id
                    ):
                        raise self._identity_conflict(model.id)
                    return self._snapshot_operation(existing_operation)

                if active is not None:
                    if (
                        active.deployment_digest != digest
                        or active.generation_id != generation_id
                    ):
                        raise self._identity_conflict(model.id)
                    return self._snapshot_operation(active)

                manager = self._managers.get(model.id)
                if manager is not None and manager.state == ModelState.READY:
                    if identity is not None and identity[0] != digest:
                        raise self._identity_conflict(model.id)
                    port = self._allocated_ports[model.id]
                    response = LoadResponse(
                        model_id=model.id,
                        deployment_digest=digest,
                        generation_id=generation_id,
                        operation_id=operation_id,
                        state="ready",
                        progress=1.0,
                        llama_url=self._reported_llama_url(port),
                        internal_api_key=settings.internal_api_key,
                        port=port,
                        pid=manager._process.pid if manager._process else None,
                        already_loaded=True,
                        deadline_seconds=deadline,
                    )
                    self._operations[operation_id] = _LoadOperation(
                        model_id=model.id,
                        model=model,
                        deployment_digest=digest,
                        generation_id=generation_id,
                        operation_id=operation_id,
                        deadline_seconds=deadline,
                        state="ready",
                        progress=1.0,
                        response=response,
                        completed_at=time.monotonic(),
                    )
                    return response
                if manager is not None and manager.state == ModelState.UNLOADING:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Le modèle '{model.id}' est en cours de déchargement; réessayez.",
                    )

                operation = _LoadOperation(
                    model_id=model.id,
                    model=model,
                    deployment_digest=digest,
                    generation_id=generation_id,
                    operation_id=operation_id,
                    deadline_seconds=deadline,
                )
                self._operations[operation_id] = operation
                self._active_operations[model.id] = operation_id
                operation.task = asyncio.create_task(self._run_load(operation))
                operation.task.add_done_callback(self._consume_operation_task)
                return self._snapshot_operation(operation)

    async def load(
        self,
        model_dict: dict,
        *,
        deployment_digest: str | None = None,
        generation_id: str | None = None,
        operation_id: str | None = None,
        deadline_seconds: float | None = None,
    ) -> LoadResponse:
        """Compatibilité locale : démarre puis attend l'opération terminale."""
        response = await self.start_load(
            model_dict,
            deployment_digest=deployment_digest,
            generation_id=generation_id,
            operation_id=operation_id,
            deadline_seconds=deadline_seconds,
        )
        if response.state in {"ready", "failed", "conflict", "deadline_exceeded"}:
            operation = self._operations.get(response.operation_id)
            if operation is not None and operation.state in {
                "failed", "conflict", "deadline_exceeded"
            }:
                raise HTTPException(
                    status_code=operation.error_status,
                    detail=operation.error_detail,
                )
            return response
        operation = self._operations.get(response.operation_id)
        if operation is None or operation.task is None:
            return response
        await asyncio.shield(operation.task)
        async with self._lock:
            result = self._snapshot_operation(operation)
        if result.state in {"failed", "conflict", "deadline_exceeded"}:
            raise HTTPException(
                status_code=operation.error_status,
                detail=operation.error_detail,
            )
        return result

    @staticmethod
    def _consume_operation_task(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        # Lire exception() empêche « Task exception was never retrieved » même
        # lorsqu'aucun client ne repolle l'opération.
        task.exception()

    async def _cancel_model_load(self, model_id: str) -> None:
        async with self._lock:
            manager = self._managers.get(model_id)
        if manager is None:
            return
        try:
            await manager.unload(reason="deadline d'opération")
        except Exception as exc:
            log.warning("Nettoyage du chargement '%s' échoué : %s", model_id, exc)
        async with self._lock:
            self._release_manager(model_id, manager)

    async def _run_load(self, operation: _LoadOperation) -> None:
        load_task: asyncio.Task | None = None
        deadline_at = operation.created_at + operation.deadline_seconds
        try:
            async with self._lock:
                operation.state = "loading"
                operation.progress = 0.05
            remaining = deadline_at - time.monotonic()
            if remaining <= 0:
                raise HTTPException(
                    status_code=504,
                    detail=(
                        f"Le chargement du modèle '{operation.model_id}' "
                        "a dépassé l'échéance négociée."
                    ),
                )
            try:
                await asyncio.wait_for(
                    self._validate_model_runtime(operation.model), timeout=remaining
                )
            except asyncio.TimeoutError as exc:
                raise HTTPException(
                    status_code=504,
                    detail=(
                        f"Le chargement du modèle '{operation.model_id}' "
                        "a dépassé l'échéance négociée."
                    ),
                ) from exc
            async with self._lock:
                operation.progress = 0.25
            model_lock = self._model_locks.setdefault(operation.model_id, asyncio.Lock())
            async with model_lock:
                remaining = deadline_at - time.monotonic()
                if remaining <= 0:
                    raise HTTPException(
                        status_code=504,
                        detail=(
                            f"Le chargement du modèle '{operation.model_id}' "
                            "a dépassé l'échéance négociée."
                        ),
                    )
                load_task = asyncio.create_task(self._load_serialized(operation.model))
                try:
                    response = await asyncio.wait_for(
                        asyncio.shield(load_task),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError as exc:
                    load_task.cancel()
                    await asyncio.gather(load_task, return_exceptions=True)
                    await self._cancel_model_load(operation.model_id)
                    raise HTTPException(
                        status_code=504,
                        detail=(
                            f"Le chargement du modèle '{operation.model_id}' "
                            "a dépassé l'échéance négociée."
                        ),
                    ) from exc
                except asyncio.CancelledError:
                    load_task.cancel()
                    await asyncio.gather(load_task, return_exceptions=True)
                    await self._cancel_model_load(operation.model_id)
                    raise

            response = response.model_copy(
                update={
                    "deployment_digest": operation.deployment_digest,
                    "generation_id": operation.generation_id,
                    "operation_id": operation.operation_id,
                    "state": "ready",
                    "progress": 1.0,
                    "deadline_seconds": operation.deadline_seconds,
                }
            )
            async with self._lock:
                operation.response = response
                operation.state = "ready"
                operation.progress = 1.0
                operation.completed_at = time.monotonic()
                self._manager_identity[operation.model_id] = (
                    operation.deployment_digest,
                    operation.generation_id,
                    operation.operation_id,
                )
                if self._active_operations.get(operation.model_id) == operation.operation_id:
                    self._active_operations.pop(operation.model_id, None)
        except asyncio.CancelledError:
            async with self._lock:
                if self._active_operations.get(operation.model_id) == operation.operation_id:
                    self._active_operations.pop(operation.model_id, None)
                operation.state = "failed"
                operation.completed_at = time.monotonic()
                operation.error_status = 503
                operation.error_detail = "Opération interrompue pendant l'arrêt de l'agent."
            raise
        except HTTPException as exc:
            async with self._lock:
                operation.state = (
                    "deadline_exceeded" if exc.status_code == 504 else "failed"
                )
                operation.completed_at = time.monotonic()
                operation.progress = 1.0 if operation.state == "deadline_exceeded" else operation.progress
                operation.error_status = exc.status_code
                operation.error_detail = str(exc.detail)
                operation.response = LoadResponse(
                    model_id=operation.model_id,
                    deployment_digest=operation.deployment_digest,
                    generation_id=operation.generation_id,
                    operation_id=operation.operation_id,
                    state=operation.state,
                    progress=operation.progress,
                    deadline_seconds=operation.deadline_seconds,
                    error_code=operation.state,
                    message=operation.error_detail,
                )
                if self._active_operations.get(operation.model_id) == operation.operation_id:
                    self._active_operations.pop(operation.model_id, None)
        except Exception as exc:
            log.error("Échec du chargement de '%s' : %s", operation.model_id, exc)
            async with self._lock:
                operation.state = "failed"
                operation.completed_at = time.monotonic()
                operation.error_status = 500
                operation.error_detail = (
                    f"Échec du chargement du modèle '{operation.model_id}' sur le nœud."
                )
                operation.response = LoadResponse(
                    model_id=operation.model_id,
                    deployment_digest=operation.deployment_digest,
                    generation_id=operation.generation_id,
                    operation_id=operation.operation_id,
                    state="failed",
                    progress=operation.progress,
                    deadline_seconds=operation.deadline_seconds,
                    error_code="load_failed",
                    message=operation.error_detail,
                )
                if self._active_operations.get(operation.model_id) == operation.operation_id:
                    self._active_operations.pop(operation.model_id, None)

    async def operation_status(self, operation_id: str) -> OperationStatusResponse:
        async with self._lock:
            self._prune_operations_locked()
            operation = self._operations.get(operation_id)
            if operation is None:
                raise HTTPException(status_code=404, detail="Opération inconnue.")
            return OperationStatusResponse.model_validate(
                self._snapshot_operation(operation).model_dump()
            )

    async def _load_serialized(self, model) -> LoadResponse:
        """Charge sous verrou par modèle; n'expose jamais une URL encore LOADING."""
        already_loaded = False
        async with self._lock:
            existing = self._managers.get(model.id)
            if existing and existing.state == ModelState.READY:
                port = self._allocated_ports[model.id]
                return LoadResponse(
                    model_id=model.id,
                    llama_url=self._reported_llama_url(port),
                    internal_api_key=settings.internal_api_key,
                    port=port,
                    pid=existing._process.pid if existing._process else None,
                    already_loaded=True,
                )

            if existing and existing.state == ModelState.LOADING:
                # Cas défensif (p.ex. état restauré par un backend custom) : le
                # verrou empêche les nouveaux chemins normaux d'arriver ici, mais
                # on attend tout de même READY au lieu de router prématurément.
                mgr = existing
                port = self._allocated_ports[model.id]
                already_loaded = True
            else:
                if existing and existing.state == ModelState.UNLOADING:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Le modèle '{model.id}' est en cours de déchargement; réessayez.",
                    )
                if existing:
                    self._release_manager(model.id, existing)

                if not self._port_pool:
                    raise HTTPException(
                        status_code=503,
                        detail=(
                            f"Pool de ports épuisé ({settings.max_loaded_models} max). "
                            "Déchargez un modèle avant d'en charger un autre."
                        ),
                    )
                if self._available_vram() < model.vram_gb:
                    raise HTTPException(
                        status_code=503,
                        detail=(
                            f"VRAM insuffisante : besoin {model.vram_gb:.1f} GB, "
                            f"disponible {self._available_vram():.1f} GB."
                        ),
                    )

                port = self._port_pool.pop(0)
                self._allocated_ports[model.id] = port
                manager_ref: ServerManager | None = None

                def on_unload(mid: str) -> None:
                    self._on_unloaded(mid, manager_ref)

                mgr = ServerManager(
                    model=model,
                    port=port,
                    on_unload=on_unload,
                    # Le data-plane contourne l'agent : ses compteurs pin/unpin ne
                    # voient pas les requêtes distantes. Garder le watchdog de crash,
                    # mais interdire toute éviction idle aveugle.
                    idle_unload_enabled=False,
                )
                manager_ref = mgr
                self._managers[model.id] = mgr

        try:
            await mgr.ensure_loaded()
        except Exception as exc:
            # Le détail (tail stderr llama-server, chemins de modèles) reste au
            # journal de l'agent : le corps HTTP remonte tel quel dans les
            # exceptions du node_client côté orchestrateur, qui étaient
            # retransmises au client final (SEC — fuite d'infra, audit 2026-08-28).
            log.error("Échec du chargement de '%s' sur le nœud : %s", model.id, exc)
            async with self._lock:
                self._release_manager(model.id, mgr)
            raise HTTPException(
                status_code=500,
                detail=f"Échec du chargement du modèle '{model.id}' sur le nœud.",
            ) from exc

        return LoadResponse(
            model_id=model.id,
            llama_url=self._reported_llama_url(port),
            internal_api_key=settings.internal_api_key,
            port=port,
            pid=mgr._process.pid if mgr._process else None,
            already_loaded=already_loaded,
        )

    async def unload(self, model_id: str) -> UnloadResponse:
        model_lock = self._model_locks.setdefault(model_id, asyncio.Lock())
        async with model_lock:
            async with self._lock:
                mgr = self._managers.get(model_id)
            if mgr is None:
                return UnloadResponse(model_id=model_id, unloaded=False, message="Modèle non chargé.")
            vram = mgr.model.vram_gb
            await mgr.unload(reason="orchestrateur request")
            async with self._lock:
                # Le vrai ServerManager appelle déjà le callback; ce fallback
                # couvre un backend custom/idempotent sans callback.
                self._release_manager(model_id, mgr)
            return UnloadResponse(model_id=model_id, unloaded=True, freed_vram_gb=vram)

    async def shutdown(self) -> None:
        """Arrête les opérations acceptées avant de libérer les processus."""
        self._closing = True
        async with self._lock:
            tasks = [
                operation.task
                for operation in self._operations.values()
                if operation.task is not None
                and not operation.task.done()
            ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.unload_all()

    async def unload_all(self) -> None:
        for mid in list(self._managers):
            await self.unload(mid)

    def _on_unloaded(self, model_id: str, expected: ServerManager | None) -> None:
        self._release_manager(model_id, expected)

    def _release_manager(self, model_id: str, expected: ServerManager | None) -> None:
        """Libère uniquement le manager attendu; callback ancien = no-op sûr."""
        current = self._managers.get(model_id)
        if expected is None or current is not expected:
            return
        port = self._allocated_ports.pop(model_id, None)
        if port is not None and port not in self._port_pool:
            self._port_pool.append(port)
            self._port_pool.sort()
        self._managers.pop(model_id, None)
        self._manager_identity.pop(model_id, None)

    def health(self) -> NodeHealth:
        used = self._used_vram()
        return NodeHealth(
            status="ok",
            agent_version="1.0.0",
            total_vram_gb=settings.total_vram_gb,
            used_vram_gb=round(used, 2),
            available_vram_gb=round(max(0.0, settings.effective_vram_budget_gb() - used), 2),
            loaded_model_ids=list(self._managers),
            free_ports=len(self._port_pool),
        )

    @staticmethod
    def _parse_prometheus(text: str) -> dict[str, float]:
        """
        Parse minimaliste du format texte Prometheus des llama-server locaux.
        Extrait les métriques scalaires sans labels (cohérent avec le parseur
        de la gateway, gateway/metrics.py::_parse_prometheus).
        """
        result: dict[str, float] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "{" in line:
                continue
            parts = line.split()
            if len(parts) == 2:
                try:
                    result[parts[0]] = float(parts[1])
                except ValueError:
                    pass
        return result

    async def agent_metrics(self) -> dict:
        """
        Agrège les métriques Prometheus des llama-server READY de CE nœud en un
        JSON compact {model_id: {clé: valeur|None}}. Ne renvoie AUCUN contenu de
        prompt. Robuste : un llama-server injoignable est simplement omis, jamais
        d'exception propagée.
        """
        async with self._lock:
            ready = [
                (mid, mgr)
                for mid, mgr in self._managers.items()
                if mgr.state == ModelState.READY
            ]
        result: dict = {}
        if not ready:
            return result
        async with httpx.AsyncClient(timeout=3.0) as client:
            for model_id, mgr in ready:
                try:
                    resp = await client.get(
                        mgr.llama_url("/metrics"),
                        headers=mgr.auth_headers(),
                    )
                    if resp.status_code != 200:
                        continue
                    raw = self._parse_prometheus(resp.text)
                    result[model_id] = {
                        "kv_cache_usage_ratio": raw.get("llamacpp:kv_cache_usage_ratio"),
                        "kv_cache_tokens": raw.get("llamacpp:kv_cache_tokens"),
                        "requests_processing": raw.get("llamacpp:requests_processing"),
                        "requests_deferred": raw.get("llamacpp:requests_deferred"),
                        "tokens_per_second": raw.get("llamacpp:tokens_per_second"),
                        "prompt_tokens_total": raw.get("llamacpp:prompt_tokens_total"),
                        "tokens_predicted_total": raw.get("llamacpp:tokens_predicted_total"),
                    }
                except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError):
                    pass
                except Exception:
                    log.exception("Métriques llama indisponibles pour '%s'", model_id)
        return result

    def node_status(self) -> NodeStatus:
        models = []
        for mid, mgr in self._managers.items():
            identity = self._manager_identity.get(mid)
            active_operation_id = self._active_operations.get(mid)
            operation = (
                self._operations.get(
                    identity[2] if identity is not None else active_operation_id
                )
                if identity is not None or active_operation_id is not None
                else None
            )
            models.append(
                ModelStateOnNode(
                    id=mid,
                    state=mgr.state.value,
                    port=mgr.port,
                    pid=mgr._process.pid if mgr._process else None,
                    uptime_seconds=mgr.uptime_seconds,
                    idle_seconds=round(mgr.idle_seconds, 1) if mgr._last_request_time else None,
                    active_requests=mgr.active_requests,
                    vram_gb=mgr.model.vram_gb,
                    deployment_digest=(
                        identity[0]
                        if identity
                        else operation.deployment_digest if operation else ""
                    ),
                    generation_id=(
                        identity[1]
                        if identity
                        else operation.generation_id if operation else ""
                    ),
                    operation_id=(
                        identity[2]
                        if identity
                        else operation.operation_id if operation else None
                    ),
                    progress=operation.progress if operation else (1.0 if mgr.state == ModelState.READY else 0.0),
                    deadline_seconds=operation.deadline_seconds if operation else None,
                )
            )
        represented = {model.id for model in models}
        for operation in self._operations.values():
            if (
                operation.model_id in represented
                or operation.state not in {"accepted", "loading"}
            ):
                continue
            models.append(
                ModelStateOnNode(
                    id=operation.model_id,
                    state="loading",
                    vram_gb=getattr(operation.model, "vram_gb", 0.0),
                    deployment_digest=operation.deployment_digest,
                    generation_id=operation.generation_id,
                    operation_id=operation.operation_id,
                    progress=operation.progress,
                    deadline_seconds=operation.deadline_seconds,
                )
            )
        return NodeStatus(node_id=settings.node_id, health=self.health(), models=models)


# ── Singleton ─────────────────────────────────────────────────────────────────

_state: _AgentState | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _state
    log.info(
        "=== Node Agent démarrage — node_id=%s, port=%d ===",
        settings.node_id, settings.agent_port,
    )
    # L'agent et ses llama-server écoutent le réseau : contrairement au mode
    # local, démarrer avec une clé d'exemple créerait une exposition immédiate.
    settings.validate_runtime_security()
    log.info(
        "Budget VRAM : %.1f GB total → %.1f GB net",
        settings.total_vram_gb, settings.effective_vram_budget_gb(),
    )

    # Garde-fou supply-chain : version du binaire llama-server. Inerte tant que
    # LLAMA_SERVER_MIN_BUILD=0 (défaut, aucun binaire réel en test). Dès qu'un
    # plancher est exigé, la politique est FAIL-CLOSED (SEC-009) : build lu sous
    # le plancher OU version illisible → refus (cf. GHSA-8947-pfff-2f3c).
    ok = await enforce_llama_min_build(
        settings.llama_server_bin, settings.llama_server_min_build
    )
    if not ok:
        raise RuntimeError(
            "llama-server ne satisfait pas LLAMA_SERVER_MIN_BUILD — "
            "démarrage de l'agent refusé (binaire potentiellement vulnérable)."
        )

    _state = _AgentState()
    yield
    log.info("Arrêt de l'agent — déchargement de tous les modèles…")
    if _state:
        await _state.shutdown()
    log.info("=== Node Agent arrêt propre ===")


# ── Application ───────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

app = FastAPI(
    title="LLM Gateway — Node Agent",
    description="Agent de contrôle d'un nœud GPU. Accès réservé à l'orchestrateur.",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def _get_state() -> _AgentState:
    if _state is None:
        raise HTTPException(status_code=503, detail="Agent non initialisé.")
    return _state


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/agent/health", response_model=NodeHealth)
async def health(
    _: None = Depends(require_agent_secret),
    state: _AgentState = Depends(_get_state),
) -> NodeHealth:
    return state.health()


@app.get("/agent/status", response_model=NodeStatus)
async def status(
    _: None = Depends(require_agent_secret),
    state: _AgentState = Depends(_get_state),
) -> NodeStatus:
    return state.node_status()


@app.get("/agent/metrics")
async def agent_metrics(
    _: None = Depends(require_agent_secret),
    state: _AgentState = Depends(_get_state),
) -> dict:
    """
    Métriques llama-server agrégées du nœud (Prometheus → JSON compact par
    model_id). Protégé par AGENT_SECRET, consommé par l'orchestrateur pour
    peupler /admin/metrics/llama et /admin/metrics/prometheus en mode cluster.
    Ne renvoie jamais de contenu de prompt.
    """
    return await state.agent_metrics()


@app.post("/agent/models/load", response_model=LoadResponse)
async def load_model(
    body: LoadRequest,
    _: None = Depends(require_agent_secret),
    state: _AgentState = Depends(_get_state),
) -> LoadResponse:
    response = await state.start_load(
        body.model,
        deployment_digest=body.deployment_digest,
        generation_id=body.generation_id,
        operation_id=body.operation_id,
        deadline_seconds=body.deadline_seconds,
    )
    if response.state in {"accepted", "loading"}:
        return JSONResponse(
            status_code=202,
            content=response.model_dump(mode="json"),
        )
    return response


@app.get("/agent/operations/{operation_id}", response_model=OperationStatusResponse)
async def operation_status(
    operation_id: str,
    _: None = Depends(require_agent_secret),
    state: _AgentState = Depends(_get_state),
) -> OperationStatusResponse:
    return await state.operation_status(operation_id)


@app.post("/agent/models/{model_id}/unload", response_model=UnloadResponse)
async def unload_model(
    model_id: str,
    _: None = Depends(require_agent_secret),
    state: _AgentState = Depends(_get_state),
) -> UnloadResponse:
    return await state.unload(model_id)


@app.post("/agent/unload-all")
async def unload_all(
    _: None = Depends(require_agent_secret),
    state: _AgentState = Depends(_get_state),
) -> dict:
    await state.unload_all()
    return {"unloaded": True}
