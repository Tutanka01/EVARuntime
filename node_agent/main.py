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
import secrets
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

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
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# Chargé APRÈS avoir ajusté sys.path
from config import settings  # → node_agent/config.py
from llama_version import enforce_llama_min_build
from model_registry import IntegrityError, ModelRegistry
from server_manager import ModelState, ServerManager
from cluster.node_protocol import (
    LoadRequest,
    LoadResponse,
    ModelStateOnNode,
    NodeHealth,
    NodeStatus,
    UnloadResponse,
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
    return ModelRegistry(
        config_path=Path(tmp.name),
        allowed_model_dirs=settings.allowed_model_dirs or None,
    )


# ── État de l'agent ───────────────────────────────────────────────────────────

class _AgentState:
    """
    Singleton local : pool de ServerManager + pool de ports + budget VRAM.
    Même logique que LocalModelManager, mais sans couche de routage.
    """

    def __init__(self) -> None:
        self._validator = _make_validator_registry()
        self._managers: dict[str, ServerManager] = {}
        self._allocated_ports: dict[str, int] = {}
        self._port_pool: list[int] = list(range(
            settings.base_llama_port,
            settings.base_llama_port + settings.max_loaded_models,
        ))
        self._lock = asyncio.Lock()

    def _used_vram(self) -> float:
        return sum(
            mgr.model.vram_gb
            for mgr in self._managers.values()
            if mgr.state in (ModelState.READY, ModelState.LOADING)
        )

    def _available_vram(self) -> float:
        return settings.effective_vram_budget_gb() - self._used_vram()

    async def load(self, model_dict: dict) -> LoadResponse:
        """Charge un modèle depuis sa définition YAML. Idempotent."""
        # Valider via le même parseur que la gateway — mêmes règles de sécurité.
        try:
            model = self._validator._parse_entry(model_dict)
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=f"Définition de modèle invalide : {exc}") from exc

        # Garde-fou supply-chain (opt-in) : si le modèle déclare un `sha256`, on
        # vérifie l'intégrité du GGUF AVANT de lancer le sous-processus. Coût :
        # hash complet d'un gros fichier (plusieurs Go) — acceptable au chargement,
        # jamais dans le chemin de requête. Inerte si `sha256` absent.
        if model.sha256 is not None:
            try:
                model.verify_integrity()
            except IntegrityError as exc:
                raise HTTPException(
                    status_code=422,
                    detail=f"Vérification d'intégrité échouée : {exc}",
                ) from exc

        async with self._lock:
            existing = self._managers.get(model.id)
            if existing and existing.state in (ModelState.READY, ModelState.LOADING):
                port = self._allocated_ports[model.id]
                return LoadResponse(
                    model_id=model.id,
                    llama_url=f"http://{settings.llama_server_host}:{port}",
                    internal_api_key=settings.internal_api_key,
                    port=port,
                    pid=existing._process.pid if existing._process else None,
                    already_loaded=True,
                )

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
            mgr = ServerManager(model=model, port=port, on_unload=self._on_unloaded)
            self._managers[model.id] = mgr

        try:
            await mgr.ensure_loaded()
        except Exception as exc:
            async with self._lock:
                self._managers.pop(model.id, None)
                freed_port = self._allocated_ports.pop(model.id, None)
                if freed_port is not None:
                    self._port_pool.append(freed_port)
            raise HTTPException(status_code=500, detail=f"Échec du chargement : {exc}") from exc

        return LoadResponse(
            model_id=model.id,
            llama_url=f"http://{settings.llama_server_host}:{port}",
            internal_api_key=settings.internal_api_key,
            port=port,
            pid=mgr._process.pid if mgr._process else None,
            already_loaded=False,
        )

    async def unload(self, model_id: str) -> UnloadResponse:
        async with self._lock:
            mgr = self._managers.get(model_id)
        if mgr is None:
            return UnloadResponse(model_id=model_id, unloaded=False, message="Modèle non chargé.")
        vram = mgr.model.vram_gb
        await mgr.unload(reason="orchestrateur request")
        return UnloadResponse(model_id=model_id, unloaded=True, freed_vram_gb=vram)

    async def unload_all(self) -> None:
        for mid in list(self._managers):
            mgr = self._managers.get(mid)
            if mgr:
                await mgr.unload(reason="shutdown")

    def _on_unloaded(self, model_id: str) -> None:
        port = self._allocated_ports.pop(model_id, None)
        if port is not None:
            self._port_pool.append(port)
        self._managers.pop(model_id, None)

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
        models = [
            ModelStateOnNode(
                id=mid,
                state=mgr.state.value,
                port=mgr.port,
                pid=mgr._process.pid if mgr._process else None,
                uptime_seconds=mgr.uptime_seconds,
                idle_seconds=round(mgr.idle_seconds, 1) if mgr._last_request_time else None,
                active_requests=mgr.active_requests,
                vram_gb=mgr.model.vram_gb,
            )
            for mid, mgr in self._managers.items()
        ]
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
    if settings.agent_secret_is_placeholder():
        log.critical(
            "AGENT_SECRET non configuré (vide ou CHANGE_ME_*) — toutes les "
            "requêtes seront refusées (503) tant qu'un secret fort n'est pas défini."
        )
    log.info(
        "Budget VRAM : %.1f GB total → %.1f GB net",
        settings.total_vram_gb, settings.effective_vram_budget_gb(),
    )

    # Garde-fou supply-chain : version du binaire llama-server. NON FATAL par
    # défaut (aucun binaire réel en test). Refuse le démarrage UNIQUEMENT si
    # LLAMA_SERVER_MIN_BUILD > 0 et build lu < minimum (cf. GHSA-8947-pfff-2f3c).
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
        await _state.unload_all()
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
    return await state.load(body.model)


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
