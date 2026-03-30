"""
Gestionnaire du cycle de vie de llama-server.

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
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from enum import Enum

import httpx

from config import settings

log = logging.getLogger(__name__)


class ModelState(str, Enum):
    UNLOADED  = "unloaded"
    LOADING   = "loading"
    READY     = "ready"
    UNLOADING = "unloading"


class ServerManager:
    """
    Singleton gérant le sous-processus llama-server.
    Instancié une fois dans main.py et injecté via dependency injection FastAPI.
    """

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._state: ModelState = ModelState.UNLOADED
        self._last_request_time: float = 0.0
        self._load_start_time: float = 0.0

        # Lock pour serialiser les transitions d'état
        self._state_lock = asyncio.Lock()
        # Event signalé quand le chargement se termine (succès ou échec)
        self._ready_event: asyncio.Event | None = None
        # Exception capturée pendant le chargement pour la propager aux waiters
        self._load_error: Exception | None = None

        self._idle_task: asyncio.Task | None = None

    # ── Propriétés publiques ──────────────────────────────────────────────────

    @property
    def state(self) -> ModelState:
        return self._state

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

    # ── Point d'entrée principal ──────────────────────────────────────────────

    async def ensure_loaded(self) -> None:
        """
        Appelé avant chaque requête proxy.
        - Si READY → met à jour le timestamp et retourne immédiatement.
        - Si LOADING → attend la fin du chargement (Event).
        - Si UNLOADED → lance le chargement (un seul thread le fait, les autres attendent).
        Lance une exception si le chargement échoue ou dépasse le timeout.
        """
        # Fast path — aucun lock nécessaire si déjà prêt
        if self._state == ModelState.READY:
            self._last_request_time = time.monotonic()
            return

        async with self._state_lock:
            # Re-vérifier après acquisition du lock
            if self._state == ModelState.READY:
                self._last_request_time = time.monotonic()
                return

            if self._state == ModelState.LOADING:
                # Un autre coroutine charge déjà : récupérer l'event courant
                event = self._ready_event
            elif self._state == ModelState.UNLOADED:
                # On est le premier : on démarre le chargement
                event = asyncio.Event()
                self._ready_event = event
                self._load_error = None
                self._state = ModelState.LOADING
                asyncio.create_task(self._load_and_signal(event))
            else:
                # UNLOADING — on attend et on réessaie
                raise RuntimeError("Le modèle est en cours de déchargement, réessayez dans quelques secondes.")

        # Attendre que le chargement se termine (hors du lock)
        try:
            await asyncio.wait_for(
                event.wait(),
                timeout=settings.model_load_timeout_seconds + 10,
            )
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"Le modèle n'a pas démarré dans les "
                f"{settings.model_load_timeout_seconds + 10}s imparties."
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
                "llama-server prêt — modèle '%s' chargé (PID %d)",
                settings.model_path.name,
                self._process.pid if self._process else -1,
            )

            # Démarrer le moniteur d'inactivité
            self._idle_task = asyncio.create_task(self._idle_monitor())

        except Exception as exc:
            log.error("Échec du chargement du modèle : %s", exc)
            self._load_error = exc
            # Nettoyer le processus si lancé
            await self._kill_process()
            async with self._state_lock:
                self._state = ModelState.UNLOADED
        finally:
            # Toujours signaler pour débloquer les waiters
            event.set()

    async def _start_process(self) -> None:
        cmd = settings.build_llama_cmd()
        log.info("Lancement llama-server : %s", " ".join(cmd))

        env = os.environ.copy()
        env["CUDA_VISIBLE_DEVICES"] = settings.cuda_visible_devices

        # start_new_session=True : crée un nouveau process group.
        # os.killpg() tuera llama-server ET tous ses enfants éventuels.
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )

        # Lancer une tâche de lecture des logs pour éviter le blocage du pipe
        asyncio.create_task(self._drain_logs())

    async def _drain_logs(self) -> None:
        """Lit les sorties de llama-server pour éviter le blocage du pipe."""
        if not self._process:
            return
        llama_log = logging.getLogger("llama-server")
        try:
            while True:
                if self._process.stderr:
                    line = await self._process.stderr.readline()
                    if not line:
                        break
                    llama_log.debug(line.decode(errors="replace").rstrip())
        except Exception:
            pass

    async def _wait_for_health(self) -> None:
        """
        Poll /health toutes les 2s jusqu'à {"status": "ok"}.
        Lève TimeoutError si le serveur ne répond pas dans le délai configuré.
        Lève RuntimeError si le processus meurt avant d'être prêt.
        """
        url = f"{settings.llama_server_url()}/health"
        deadline = time.monotonic() + settings.model_load_timeout_seconds

        async with httpx.AsyncClient(timeout=3.0) as client:
            while time.monotonic() < deadline:
                # Vérifier que le processus est encore vivant
                if self._process and self._process.returncode is not None:
                    raise RuntimeError(
                        f"llama-server s'est terminé prématurément "
                        f"(code {self._process.returncode})"
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
            f"llama-server n'a pas répondu sur {url} "
            f"dans les {settings.model_load_timeout_seconds}s."
        )

    # ── Déchargement ──────────────────────────────────────────────────────────

    async def unload(self, reason: str = "manuel") -> None:
        """
        Termine llama-server et libère 100% de la VRAM GPU.
        Idempotent : sans effet si déjà UNLOADED.
        """
        async with self._state_lock:
            if self._state in (ModelState.UNLOADED, ModelState.UNLOADING):
                return
            self._state = ModelState.UNLOADING

        log.info("Déchargement du modèle (%s)…", reason)

        # Annuler le moniteur d'inactivité
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
            "Modèle déchargé — GPU libéré. Vérifier avec : "
            "nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits"
        )

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
            log.debug("SIGTERM → process group %d", pgid)
            os.killpg(pgid, signal.SIGTERM)

            try:
                await asyncio.wait_for(self._process.wait(), timeout=10.0)
                log.debug("llama-server terminé proprement (PID %d)", pid)
            except asyncio.TimeoutError:
                log.warning("SIGTERM ignoré, envoi SIGKILL → PID %d", pid)
                os.killpg(pgid, signal.SIGKILL)
                await self._process.wait()
                log.debug("llama-server tué (PID %d)", pid)

        except ProcessLookupError:
            pass  # Déjà mort

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
            "Moniteur d'inactivité démarré (timeout=%ds, check=%ds)",
            timeout, interval,
        )

        while True:
            await asyncio.sleep(interval)

            if self._state != ModelState.READY:
                break

            idle_for = time.monotonic() - self._last_request_time
            log.debug("Idle depuis %.0fs / %ds", idle_for, timeout)

            if idle_for >= timeout:
                log.info(
                    "Inactivité détectée (%.0fs sans requête) — déchargement du modèle.",
                    idle_for,
                )
                await self.unload(reason=f"inactivité ({idle_for:.0f}s)")
                break

    # ── Statut ────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        """Retourne un dict de statut pour le endpoint /health et /admin/status."""
        return {
            "model_state": self._state.value,
            "model_path": str(settings.model_path),
            "model_name": settings.model_public_name,
            "pid": self._process.pid if self._process else None,
            "uptime_seconds": self.uptime_seconds,
            "idle_seconds": round(self.idle_seconds, 1) if self._last_request_time else None,
            "idle_timeout_seconds": settings.idle_timeout_seconds,
            "llama_params": {
                "n_gpu_layers": settings.llama_n_gpu_layers,
                "ctx_size": settings.llama_ctx_size,
                "parallel": settings.llama_parallel,
                "flash_attn": settings.llama_flash_attn,
                "cache_type_k": settings.llama_cache_type_k,
                "cache_type_v": settings.llama_cache_type_v,
            },
        }


# ── Instance globale ──────────────────────────────────────────────────────────
# Créée ici, injectée dans l'app via app.state.server_manager

server_manager = ServerManager()
