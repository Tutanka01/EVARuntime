"""
DTOs Pydantic du protocole orchestrateur ↔ node-agent.

Schémas partagés — source unique de vérité pour les corps HTTP échangés.
Importé à la fois par gateway/cluster/node_client.py (côté orchestrateur)
et par node_agent/main.py (côté agent).

Le canal de contrôle (load/unload/health) passe par ces DTOs.
Le canal de données (proxy SSE vers llama-server) reste OpenAI-natif et
n'utilise PAS ces schémas — voir gateway/proxy.py.
"""
from __future__ import annotations

import hashlib
import json
from typing import Literal, Optional

from pydantic import BaseModel, Field


def deployment_digest(model: dict) -> str:
    """Retourne le digest stable de la définition runtime complète.

    Le JSON canonique rend l'identité indépendante de l'ordre des clés YAML.
    Le modèle est volontairement le seul élément haché : les identifiants
    d'opération et de génération décrivent une exécution, pas son contenu.
    """
    try:
        canonical = json.dumps(
            model,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Définition de modèle non sérialisable") from exc
    return hashlib.sha256(canonical).hexdigest()


# ── Requêtes ──────────────────────────────────────────────────────────────────

class LoadRequest(BaseModel):
    """
    POST /agent/models/load — demande de chargement.
    Le ModelDefinition complet est inline (dict YAML) pour que l'agent
    n'ait pas besoin d'accéder à models.yaml de l'orchestrateur.
    """
    # Représentation YAML d'une entrée models.yaml (cf. ModelRegistry._parse_entry).
    # On passe un dict brut plutôt qu'un schéma typé : l'agent réutilise le
    # ModelRegistry pour valider, garantissant que les mêmes règles de sécurité
    # (regex id, allowed_model_dirs, .gguf, etc.) s'appliquent côté nœud.
    model: dict = Field(..., description="Entrée YAML du modèle à charger")
    # L'identifiant du modèle seul ne suffit pas à distinguer deux définitions
    # runtime successives. Ces champs sont obligatoires sur le fil.
    deployment_digest: str = Field(
        ...,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    )
    generation_id: str = Field(..., min_length=1, max_length=128)
    operation_id: str = Field(..., min_length=1, max_length=128)
    # Budget relatif négocié par l'agent avec sa propre horloge monotone.
    deadline_seconds: float = Field(..., gt=0, le=86_400)


# ── Réponses ──────────────────────────────────────────────────────────────────

class LoadResponse(BaseModel):
    """
    Réponse à un POST /agent/models/load réussi.

    L'orchestrateur utilise llama_url + internal_api_key pour proxifier
    directement les requêtes d'inférence vers le llama-server local du nœud.
    Cela évite un hop superflu via l'agent pour les flux SSE longs.
    """
    model_id: str
    deployment_digest: str = ""
    generation_id: str = ""
    operation_id: str = ""
    state: Literal[
        "accepted", "loading", "ready", "failed", "conflict", "deadline_exceeded"
    ] = "ready"
    progress: float = Field(default=1.0, ge=0.0, le=1.0)
    # Une réponse 202 n'expose pas encore de data-plane utilisable.
    llama_url: Optional[str] = Field(default=None)
    internal_api_key: Optional[str] = Field(default=None)
    port: Optional[int] = None
    pid: Optional[int] = None
    already_loaded: bool = Field(
        default=False,
        description="True si le modèle était déjà chargé (load idempotent)",
    )
    deadline_seconds: Optional[float] = Field(default=None, gt=0, le=86_400)
    error_code: Optional[str] = None
    message: str = ""


class OperationStatusResponse(LoadResponse):
    """État d'une opération interrogée après un timeout du RPC initial."""


class UnloadResponse(BaseModel):
    """Réponse à POST /agent/models/{id}/unload."""
    model_id: str
    unloaded: bool
    freed_vram_gb: float = 0.0
    message: str = ""


# ── Health & Status ───────────────────────────────────────────────────────────

class GpuVramSample(BaseModel):
    """Mesure matérielle d'un GPU, identifié durablement par son UUID."""

    index: Optional[int] = None
    uuid: str = Field(..., min_length=1, max_length=160)
    name: str = Field(default="", max_length=160)
    memory_used_mb: Optional[float] = Field(default=None, ge=0)
    memory_total_mb: Optional[float] = Field(default=None, ge=0)
    memory_used_bytes: Optional[int] = Field(default=None, ge=0)
    memory_total_bytes: Optional[int] = Field(default=None, ge=0)
    driver_version: str = Field(default="", max_length=80)
    compute_capability: str = Field(default="", max_length=40)
    mig_mode_current: str = Field(default="", max_length=40)
    visible: bool = True


class GpuVramMeasurement(BaseModel):
    """Snapshot GPU additif ; ``unavailable`` n'est jamais assimilé à zéro."""

    status: Literal["measured", "unavailable"]
    reason: str = Field(..., min_length=1, max_length=80)
    measured_at: Optional[str] = Field(default=None, max_length=64)
    cuda_visible_devices: Optional[str] = Field(default=None, max_length=4096)
    visible_uuids: list[str] = Field(default_factory=list, max_length=128)
    devices: list[GpuVramSample] = Field(default_factory=list, max_length=128)
    visible_used_mb: Optional[float] = Field(default=None, ge=0)
    visible_total_mb: Optional[float] = Field(default=None, ge=0)
    detail: Optional[str] = Field(default=None, max_length=256)

class NodeHealth(BaseModel):
    """
    GET /agent/health — réponse compacte utilisée par le heartbeat.
    Doit rester rapide à calculer côté agent (pas de fork, pas d'I/O lourd).
    """
    status: str = "ok"
    agent_version: str = "1.0.0"
    total_vram_gb: float
    used_vram_gb: float
    available_vram_gb: float
    loaded_model_ids: list[str] = Field(default_factory=list)
    # Capacité du pool de ports résiduel (utile au scheduler pour rejeter
    # un nœud saturé même s'il a de la VRAM)
    free_ports: int = 0
    # Additif et optionnel pour permettre une mise à jour progressive des
    # orchestrateurs et agents. Les trois agrégats historiques ci-dessus
    # restent disponibles, mais ne sont jamais présentés comme « mesurés ».
    gpu_measurement: Optional[GpuVramMeasurement] = None


class ModelStateOnNode(BaseModel):
    """État live d'un modèle tel que vu par l'agent."""
    id: str
    state: str  # unloaded | loading | ready | unloading
    port: Optional[int] = None
    pid: Optional[int] = None
    uptime_seconds: Optional[float] = None
    idle_seconds: Optional[float] = None
    active_requests: int = 0
    vram_gb: float = 0.0
    llama_params: Optional[dict] = None
    deployment_digest: str = ""
    generation_id: str = ""
    operation_id: Optional[str] = None
    progress: float = Field(default=1.0, ge=0.0, le=1.0)
    deadline_seconds: Optional[float] = Field(default=None, gt=0, le=86_400)


class NodeStatus(BaseModel):
    """
    GET /agent/status — réponse détaillée (équivalent du model_manager.status()
    mais scopé au seul nœud). Utilisé par /admin/cluster côté orchestrateur.
    """
    node_id: str
    health: NodeHealth
    models: list[ModelStateOnNode] = Field(default_factory=list)
