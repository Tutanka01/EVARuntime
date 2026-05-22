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

from typing import Optional

from pydantic import BaseModel, Field


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


# ── Réponses ──────────────────────────────────────────────────────────────────

class LoadResponse(BaseModel):
    """
    Réponse à un POST /agent/models/load réussi.

    L'orchestrateur utilise llama_url + internal_api_key pour proxifier
    directement les requêtes d'inférence vers le llama-server local du nœud.
    Cela évite un hop superflu via l'agent pour les flux SSE longs.
    """
    model_id: str
    llama_url: str = Field(
        ...,
        description="URL HTTP du llama-server à utiliser pour proxifier "
                    "les requêtes d'inférence — p.ex. http://node-a:8081",
    )
    internal_api_key: str = Field(
        ...,
        description="Clé à passer dans Authorization: Bearer <key> "
                    "à chaque requête vers llama_url",
    )
    port: int
    pid: Optional[int] = None
    already_loaded: bool = Field(
        default=False,
        description="True si le modèle était déjà chargé (load idempotent)",
    )


class UnloadResponse(BaseModel):
    """Réponse à POST /agent/models/{id}/unload."""
    model_id: str
    unloaded: bool
    freed_vram_gb: float = 0.0
    message: str = ""


# ── Health & Status ───────────────────────────────────────────────────────────

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


class NodeStatus(BaseModel):
    """
    GET /agent/status — réponse détaillée (équivalent du model_manager.status()
    mais scopé au seul nœud). Utilisé par /admin/cluster côté orchestrateur.
    """
    node_id: str
    health: NodeHealth
    models: list[ModelStateOnNode] = Field(default_factory=list)
