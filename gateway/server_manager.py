"""
Gestionnaire du cycle de vie d'un llama-server.

Principe fondamental : la SEULE façon de libérer 100% la VRAM GPU est de
tuer le processus. L'option --sleep-idle-seconds de llama-server laisse un
contexte CUDA actif (~600MB). On gère donc llama-server comme un sous-processus
asyncio et on le tue (SIGTERM → SIGKILL) après la fenêtre d'inactivité.

Machine à états :
    UNLOADED → LOADING → READY → UNLOADING → UNLOADED

Gestion de la concurrence :
    - asyncio.Lock sur les transitions d'état
    - asyncio.Event pour les waiters pendant le chargement
      (N requêtes simultanées pendant le boot → toutes attendent, toutes
       reprennent dès que le serveur est prêt, aucune n'est perdue)

Cette classe est maintenant paramétrique : une instance par modèle chargé.
Le ModelManager crée et détruit les instances dynamiquement.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from collections import deque
from collections.abc import Callable
from enum import Enum

import httpx

from config import settings
from model_registry import ModelDefinition

log = logging.getLogger(__name__)


class ModelState(str, Enum):
    UNLOADED  = "unloaded"
    LOADING   = "loading"
    READY     = "ready"
    UNLOADING = "unloading"


class ServerManager:
    """
    Gère le sous-processus llama-server pour un modèle donné sur un port donné.
    Instancié dynamiquement par ModelManager.

    Args:
        model      : définition du modèle à charger
        port       : port sur lequel llama-server écoutera
        on_unload  : callback appelé après déchargement complet —
                     permet à ModelManager de libérer le port et nettoyer son état
    """

    def __init__(
        self,
        model: ModelDefinition,
        port: int,
        on_unload: Callable[[str], None] | None = None,
    ) -> None:
        self._model = model
        self._port = port
        self._on_unload = on_unload

        self._process: asyncio.subprocess.Process | None = None
        self._state: ModelState = ModelState.UNLOADED
        self._last_request_time: float = 0.0
        self._load_start_time: float = 0.0

        self._state_lock = asyncio.Lock()
        self._ready_event: asyncio.Event | None = None
        self._load_error: Exception | None = None
        self._idle_task: asyncio.Task | None = None

        # Compteur de requêtes actuellement en cours sur ce modèle.
        # Incrémenté par pin() avant de proxifier, décrémenté par unpin() après.
        # En asyncio single-thread, les opérations entières sont atomiques — pas de lock.
        self._active_requests: int = 0

        # Buffer des dernières lignes stderr — alimenté par _drain_logs, lu au crash.
        self._stderr_tail: deque[str] = deque(maxlen=30)

    # ── Propriétés publiques ──────────────────────────────────────────────────

    @property
    def state(self) -> ModelState:
        return self._state

    @property
    def port(self) -> int:
        return self._port

    @property
    def model(self) -> ModelDefinition:
        return self._model

    @property
    def uptime_seconds(self) -> float | None:
        if self._state == ModelState.READY and self._load_start_time:
            return time.monotonic() - self._load_start_time
        return None

    @property
    def idle_seconds(self) -> float:
        if self._last_request_time == 0:
            return 0.0
        return time.monotonic() - self._last_request_time

    @property
    def is_pinned(self) -> bool:
        """True si au moins une requête est en cours sur ce modèle — interdit l'éviction."""
        return self._active_requests > 0

    @property
    def active_requests(self) -> int:
        return self._active_requests

    def pin(self) -> None:
        """
        Signale le début d'une requête active.
        Appelé juste avant de proxifier vers llama-server.
        Atomique en asyncio single-thread (pas de lock nécessaire).
        """
        self._active_requests += 1

    def unpin(self) -> None:
        """
        Signale la fin d'une requête active (terminée, exception ou client déconnecté).
        Toujours appelé depuis un bloc finally — ne peut pas être oublié.
        """
        self._active_requests = max(0, self._active_requests - 1)

    def llama_url(self, path: str) -> str:
        """URL complète vers llama-server pour ce modèle."""
        return f"http://{settings.llama_server_host}:{self._port}{path}"

    # ── Point d'entrée principal ──────────────────────────────────────────────

    async def ensure_loaded(self) -> None:
        """
        Appelé avant chaque requête proxy.
        - Si READY → met à jour le timestamp et retourne immédiatement.
        - Si LOADING → attend la fin du chargement (Event).
        - Si UNLOADED → lance le chargement (un seul coroutine le fait, les autres attendent).
        Lance une exception si le chargement échoue ou dépasse le timeout.
        """
        # Fast path — aucun lock nécessaire si déjà prêt
        if self._state == ModelState.READY:
            self._last_request_time = time.monotonic()
            return

        async with self._state_lock:
            if self._state == ModelState.READY:
                self._last_request_time = time.monotonic()
                return

            if self._state == ModelState.LOADING:
                event = self._ready_event
            elif self._state == ModelState.UNLOADED:
                event = asyncio.Event()
                self._ready_event = event
                self._load_error = None
                self._state = ModelState.LOADING
                asyncio.create_task(self._load_and_signal(event))
            else:
                raise RuntimeError(
                    f"Le modèle '{self._model.id}' est en cours de déchargement, "
                    f"réessayez dans quelques secondes."
                )

        effective_timeout = (self._model.load_timeout_seconds or settings.model_load_timeout_seconds) + 10
        try:
            await asyncio.wait_for(event.wait(), timeout=effective_timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"Le modèle '{self._model.id}' n'a pas démarré dans les "
                f"{effective_timeout}s imparties."
            )

        if self._load_error:
            raise self._load_error

        self._last_request_time = time.monotonic()

    # ── Chargement ────────────────────────────────────────────────────────────

    async def _load_and_signal(self, event: asyncio.Event) -> None:
        """Lance llama-server et signale l'event quand prêt (ou en cas d'erreur)."""
        try:
            await self._start_process()
            await self._wait_for_health()
            self._load_start_time = time.monotonic()
            self._last_request_time = time.monotonic()

            async with self._state_lock:
                self._state = ModelState.READY

            log.info(
                "llama-server prêt — modèle '%s' chargé sur port %d (PID %d)",
                self._model.id,
                self._port,
                self._process.pid if self._process else -1,
            )

            self._idle_task = asyncio.create_task(self._idle_monitor())

        except Exception as exc:
            log.error("Échec du chargement du modèle '%s' : %s", self._model.id, exc)
            self._load_error = exc
            await self._kill_process()
            async with self._state_lock:
                self._state = ModelState.UNLOADED
        finally:
            event.set()

    async def _start_process(self) -> None:
        cmd = self._model.build_llama_cmd(
            binary=settings.llama_server_bin,
            host=settings.llama_server_host,
            port=self._port,
            internal_api_key=settings.internal_api_key,
            log_path=settings.log_dir / f"llama-server-{self._model.id}.log",
        )
        log.info("Lancement llama-server '%s' : %s", self._model.id, " ".join(cmd))

        env = os.environ.copy()
        env["CUDA_VISIBLE_DEVICES"] = settings.cuda_visible_devices

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )

        asyncio.create_task(self._drain_logs())

    async def _drain_logs(self) -> None:
        """
        Lit le stderr de llama-server en continu pour éviter le blocage du pipe.
        Alimente _stderr_tail (30 lignes max) pour diagnostic en cas de crash.
        """
        if not self._process:
            return
        llama_log = logging.getLogger(f"llama-server.{self._model.id}")
        try:
            while True:
                if self._process.stderr:
                    line = await self._process.stderr.readline()
                    if not line:
                        break
                    decoded = line.decode(errors="replace").rstrip()
                    self._stderr_tail.append(decoded)
                    llama_log.debug(decoded)
        except Exception:
            pass

    async def _wait_for_health(self) -> None:
        """
        Poll /health toutes les 2s jusqu'à {"status": "ok"}.
        Lève TimeoutError si le serveur ne répond pas dans le délai configuré.
        Lève RuntimeError si le processus meurt avant d'être prêt — inclut les
        dernières lignes stderr pour diagnostic (CUDA OOM, mauvais chemin, etc.).
        """
        url = self.llama_url("/health")
        timeout = self._model.load_timeout_seconds or settings.model_load_timeout_seconds
        deadline = time.monotonic() + timeout

        async with httpx.AsyncClient(timeout=3.0) as client:
            while time.monotonic() < deadline:
                if self._process and self._process.returncode is not None:
                    # Laisser _drain_logs vider le pipe avant de lire le tail
                    await asyncio.sleep(0.15)
                    tail = list(self._stderr_tail)
                    tail_text = "\n  ".join(tail) if tail else "(aucune sortie capturée)"
                    raise RuntimeError(
                        f"llama-server '{self._model.id}' s'est terminé prématurément "
                        f"(code {self._process.returncode}).\n"
                        f"Stderr (dernières {len(tail)} lignes) :\n  {tail_text}"
                    )

                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("status") in ("ok", "no slot available"):
                            return
                except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError):
                    pass

                await asyncio.sleep(2)

        raise TimeoutError(
            f"llama-server '{self._model.id}' n'a pas répondu sur {url} "
            f"dans les {timeout}s."
        )

    # ── Déchargement ──────────────────────────────────────────────────────────

    async def unload(self, reason: str = "manuel") -> None:
        """
        Termine llama-server et libère 100% de la VRAM GPU.
        Idempotent : sans effet si déjà UNLOADED.
        Appelle le callback on_unload après déchargement complet.
        """
        async with self._state_lock:
            if self._state in (ModelState.UNLOADED, ModelState.UNLOADING):
                return
            self._state = ModelState.UNLOADING

        log.info("Déchargement du modèle '%s' (%s)…", self._model.id, reason)

        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
            try:
                await self._idle_task
            except asyncio.CancelledError:
                pass
            self._idle_task = None

        await self._kill_process()

        async with self._state_lock:
            self._state = ModelState.UNLOADED

        log.info(
            "Modèle '%s' déchargé — port %d libéré. "
            "Vérifier avec : nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits",
            self._model.id,
            self._port,
        )

        # Notifier ModelManager pour qu'il retourne le port au pool et nettoie son état
        if self._on_unload:
            self._on_unload(self._model.id)

    async def _kill_process(self) -> None:
        """SIGTERM → attente 10s → SIGKILL. Opère sur le process group entier."""
        if not self._process or self._process.returncode is not None:
            self._process = None
            return

        pid = self._process.pid
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            self._process = None
            return

        try:
            log.debug("SIGTERM → process group %d (modèle '%s')", pgid, self._model.id)
            os.killpg(pgid, signal.SIGTERM)

            try:
                await asyncio.wait_for(self._process.wait(), timeout=10.0)
                log.debug("llama-server '%s' terminé proprement (PID %d)", self._model.id, pid)
            except asyncio.TimeoutError:
                log.warning("SIGTERM ignoré, SIGKILL → '%s' PID %d", self._model.id, pid)
                os.killpg(pgid, signal.SIGKILL)
                await self._process.wait()
                log.debug("llama-server '%s' tué (PID %d)", self._model.id, pid)

        except ProcessLookupError:
            pass

        self._process = None

    # ── Moniteur d'inactivité ─────────────────────────────────────────────────

    async def _idle_monitor(self) -> None:
        """
        Tâche background : vérifie l'inactivité toutes les N secondes.
        Décharge le modèle si aucune requête depuis IDLE_TIMEOUT secondes.
        """
        interval = settings.idle_check_interval_seconds
        timeout  = settings.idle_timeout_seconds

        log.debug(
            "Moniteur d'inactivité '%s' démarré (timeout=%ds, check=%ds)",
            self._model.id, timeout, interval,
        )

        while True:
            await asyncio.sleep(interval)

            if self._state != ModelState.READY:
                break

            idle_for = time.monotonic() - self._last_request_time
            log.debug("'%s' idle depuis %.0fs / %ds", self._model.id, idle_for, timeout)

            if idle_for >= timeout:
                log.info(
                    "Inactivité détectée sur '%s' (%.0fs sans requête) — déchargement.",
                    self._model.id, idle_for,
                )
                await self.unload(reason=f"inactivité ({idle_for:.0f}s)")
                break

    # ── Statut ────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        """Retourne un dict de statut pour l'endpoint /admin/status."""
        return {
            "id": self._model.id,
            "description": self._model.description,
            "enabled": self._model.enabled,
            "vram_gb": self._model.vram_gb,
            "capabilities": self._model.capabilities,
            "state": self._state.value,
            "path": str(self._model.path),
            "pid": self._process.pid if self._process else None,
            "port": self._port,
            "uptime_seconds": self.uptime_seconds,
            "idle_seconds": round(self.idle_seconds, 1) if self._last_request_time else None,
            "active_requests": self._active_requests,
            "llama_params": {
                "n_gpu_layers": self._model.llama_params.n_gpu_layers,
                "ctx_size": self._model.llama_params.ctx_size,
                "parallel": self._model.llama_params.parallel,
                "flash_attn": self._model.llama_params.flash_attn,
                "cache_type_k": self._model.llama_params.cache_type_k,
                "cache_type_v": self._model.llama_params.cache_type_v,
                "cpu_moe": self._model.llama_params.cpu_moe,
            },
        }
