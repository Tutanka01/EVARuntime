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
from integrity import attest_model_artifacts
from model_registry import IntegrityError, ModelDefinition
from telemetry import MODEL_LOAD_SECONDS

log = logging.getLogger(__name__)


def format_url_host(host: str) -> str:
    """Encadre un littéral IPv6 pour produire une URL HTTP valide."""
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


# Marqueurs de saturation GPU/CUDA recherchés dans le stderr de llama-server.
# Source unique : LocalModelManager._is_load_capacity_error s'appuie sur la
# même liste pour déclencher sa retry après éviction réelle.
LOAD_CAPACITY_MARKERS: tuple[str, ...] = (
    "cuda",
    "out of memory",
    "unable to allocate",
    "failed to allocate",
    "failed to fit params to free device memory",
    "cudamalloc failed",
)


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
        idle_unload_enabled : active l'auto-déchargement après inactivité. Le
                     watchdog de crash reste actif même si cette option est False.
    """

    def __init__(
        self,
        model: ModelDefinition,
        port: int,
        on_unload: Callable[[str], None] | None = None,
        on_capacity_change: Callable[[], None] | None = None,
        idle_unload_enabled: bool = True,
    ) -> None:
        self._model = model
        self._port = port
        self._on_unload = on_unload
        self._on_capacity_change = on_capacity_change
        self._idle_unload_enabled = idle_unload_enabled

        self._process: asyncio.subprocess.Process | None = None
        self._state: ModelState = ModelState.UNLOADED
        self._last_request_time: float = 0.0
        self._load_start_time: float = 0.0
        # Durée du dernier chargement réussi (transition READY), en secondes.
        # None tant qu'aucun chargement n'a abouti sur cette instance ; l'instance
        # étant détruite à chaque déchargement, la valeur est bien « dernier
        # chargement du modèle actuellement en mémoire ».
        self._last_load_seconds: float | None = None

        self._state_lock = asyncio.Lock()
        self._ready_event: asyncio.Event | None = None
        self._load_error: Exception | None = None
        self._idle_task: asyncio.Task | None = None
        # Task de chargement conservée pour pouvoir l'annuler si un unload()
        # concurrent survient pendant LOADING (évite une coroutine fantôme qui
        # continuerait de poller /health sur un port mort jusqu'au timeout).
        self._load_task: asyncio.Task | None = None

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
    def last_load_seconds(self) -> float | None:
        """Durée du dernier chargement réussi, ou None si jamais prêt."""
        return self._last_load_seconds

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

    @property
    def telemetry_node(self) -> str:
        return "local"

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
        was_pinned = self._active_requests > 0
        self._active_requests = max(0, self._active_requests - 1)
        # Une requête vient de se terminer : repartir d'une fenêtre idle fraîche.
        # Sans ça, un stream plus long qu'idle_timeout serait déchargé
        # immédiatement après son dernier chunk.
        self._last_request_time = time.monotonic()
        if was_pinned and self._active_requests == 0 and self._on_capacity_change:
            self._on_capacity_change()

    def llama_url(self, path: str) -> str:
        """URL complète vers llama-server pour ce modèle."""
        # Le node-agent peut binder llama-server sur une interface réseau tout
        # en utilisant loopback pour ses sondes locales. Le mode mono-nœud ne
        # définit pas ce réglage spécialisé et conserve llama_server_host.
        host = getattr(settings, "llama_server_health_host", settings.llama_server_host)
        return f"http://{format_url_host(host)}:{self._port}{path}"

    def auth_headers(self) -> dict[str, str]:
        """Headers d'authentification interne gateway ↔ llama-server."""
        return {"Authorization": f"Bearer {settings.internal_api_key}"}

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
                self._load_task = asyncio.create_task(self._load_and_signal(event))
            else:
                raise RuntimeError(
                    f"Le modèle '{self._model.id}' est en cours de déchargement, "
                    f"réessayez dans quelques secondes."
                )

        effective_timeout = (self._model.load_timeout_seconds or settings.model_load_timeout_seconds) + 10
        try:
            await asyncio.wait_for(event.wait(), timeout=effective_timeout)
        except asyncio.TimeoutError as exc:
            # Le délai porte sur le chargement partagé : abandonner l'appelant
            # sans arrêter _load_task libérerait ensuite le manager et son port
            # alors que la task pourrait encore démarrer llama-server. Passer
            # par unload() annule, attend et tue un éventuel processus avant de
            # rendre le contrôle au gestionnaire de pool.
            await self.unload(reason="timeout de chargement")
            raise TimeoutError(
                f"Le modèle '{self._model.id}' n'a pas démarré dans les "
                f"{effective_timeout}s imparties."
            ) from exc

        if self._load_error:
            raise self._load_error

        # Le waiter ne doit retourner « prêt » que si le modèle a RÉELLEMENT
        # atteint READY. Un unload() concurrent pendant LOADING annule la task de
        # chargement (CancelledError — non capturée par `except Exception`, donc
        # pas de _load_error) tout en réveillant l'event ; le chemin d'abandon de
        # _load_and_signal réveille aussi sans erreur. Sans cette vérification, le
        # waiter retournerait un faux succès sur un modèle UNLOADED → 502 côté proxy.
        if self._state != ModelState.READY:
            raise RuntimeError(
                f"Le modèle '{self._model.id}' n'a pas pu être chargé "
                f"(déchargement concurrent) — réessayez."
            )

        self._last_request_time = time.monotonic()

    # ── Chargement ────────────────────────────────────────────────────────────

    async def _verify_artifact_integrity(self) -> None:
        """
        Attestation d'intégrité du GGUF (SEC-ART-001), fail-closed : appelée
        JUSTE AVANT _start_process, aucun sous-processus llama-server n'est
        lancé si le fichier ne correspond plus à l'empreinte déclarée — un
        GGUF substitué après le démarrage de la gateway est donc refusé à
        chaque transition vers LOADING.

        Coût O(stat) quand le fichier est inchangé : le cache attesté
        d'integrity.py est clé sur (chemin résolu, empreinte déclarée)
        confrontée à l'identité fichier (dev/ino/size/mtime_ns). Le
        single-flight des hachages est de toute façon garanti par _load_task :
        _load_and_signal ne tourne que dans une task unique par ServerManager.

        Sanitisation (même contrat que _wait_for_health) : le détail complet
        de l'IntegrityError — chemin, empreintes — part au journal seul via
        log.critical ; le message client ne contient ni chemin, ni empreinte,
        ni stderr, ET aucun marqueur de LOAD_CAPACITY_MARKERS : une atteinte
        à l'intégrité ne doit jamais être classée « saturation GPU » par
        LocalModelManager._is_load_capacity_error (sinon la retry après
        éviction déchargerait d'autres modèles en pure perte).
        """
        # No-op pour un modèle texte sans empreinte déclarée. Une définition
        # vision, elle, porte toujours l'empreinte de son projecteur (REG-002).
        if (
            getattr(self._model, "sha256", None) is None
            and "vision" not in (getattr(self._model, "capabilities", ()) or ())
        ):
            return
        try:
            await attest_model_artifacts(self._model)
        except IntegrityError as exc:
            log.critical("Intégrité GGUF compromise (SEC-ART-001) : %s", exc)
            raise RuntimeError(
                f"Artefact du modèle '{self._model.id}' refusé par la vérification "
                "d'intégrité — chargement annulé."
            ) from exc

    async def _load_and_signal(self, event: asyncio.Event) -> None:
        """Lance llama-server et signale l'event quand prêt (ou en cas d'erreur)."""
        started_at = time.monotonic()
        outcome = "success"
        try:
            # SEC-ART-001 : fail-closed avant tout lancement de sous-processus.
            await self._verify_artifact_integrity()
            await self._start_process()
            await self._wait_for_health()

            async with self._state_lock:
                # Un unload() concurrent a pu survenir pendant _wait_for_health
                # (rien n'interdit d'unload un modèle LOADING) : il a posé
                # UNLOADING→UNLOADED et tué le process. Ne PAS écraser cet état
                # avec un READY fantôme (process mort, self._process=None), sinon
                # ensure_loaded prendrait le fast-path et toutes les requêtes
                # échoueraient en 502 sans jamais revenir à UNLOADED.
                if self._state != ModelState.LOADING:
                    outcome = "cancelled"
                    log.info(
                        "Chargement de '%s' abandonné (état=%s) — unload concurrent.",
                        self._model.id, self._state.value,
                    )
                    await self._kill_process()  # idempotent, sécurité
                    return  # le finally s'exécute quand même (event.set + capacity)
                self._state = ModelState.READY
                now = time.monotonic()
                self._last_load_seconds = max(0.0, now - started_at)
                self._load_start_time = now
                self._last_request_time = now

            log.info(
                "llama-server prêt — modèle '%s' chargé sur port %d (PID %d) "
                "en %.1f s",
                self._model.id,
                self._port,
                self._process.pid if self._process else -1,
                self._last_load_seconds,
            )

            self._idle_task = asyncio.create_task(self._idle_monitor())

        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception as exc:
            outcome = "error"
            log.error("Échec du chargement du modèle '%s' : %s", self._model.id, exc)
            self._load_error = exc
            await self._kill_process()
            async with self._state_lock:
                self._state = ModelState.UNLOADED
        finally:
            MODEL_LOAD_SECONDS.observe(
                max(0.0, time.monotonic() - started_at),
                model=self._model.id,
                node="local",
                outcome=outcome,
            )
            event.set()
            if self._on_capacity_change:
                self._on_capacity_change()

    async def _start_process(self) -> None:
        cmd = self._model.build_llama_cmd(
            binary=settings.llama_server_bin,
            host=settings.llama_server_host,
            port=self._port,
            log_path=settings.log_dir / f"llama-server-{self._model.id}.log",
        )
        log.info("Lancement llama-server '%s' : %s", self._model.id, " ".join(cmd))

        env = os.environ.copy()
        env["CUDA_VISIBLE_DEVICES"] = settings.cuda_visible_devices
        # Clé interne passée par variable d'environnement (équivalent --api-key)
        # plutôt qu'en argument : les arguments sont visibles de tous les
        # utilisateurs locaux via ps/procfs et finiraient aussi dans les logs.
        env["LLAMA_API_KEY"] = settings.internal_api_key

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )

        # Drainer les DEUX pipes : un pipe plein (64 KB) bloque llama-server.
        asyncio.create_task(self._drain_stream(self._process.stderr))
        asyncio.create_task(self._drain_stream(self._process.stdout))

    async def _drain_stream(self, stream: asyncio.StreamReader | None) -> None:
        """
        Lit un pipe (stdout ou stderr) de llama-server en continu pour éviter
        son blocage. Alimente _stderr_tail (30 lignes max) pour diagnostic
        en cas de crash.
        """
        if stream is None:
            return
        llama_log = logging.getLogger(f"llama-server.{self._model.id}")
        try:
            while True:
                line = await stream.readline()
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
        Lève RuntimeError si le processus meurt avant d'être prêt.

        Le message d'exception est relayé tel quel au client par proxy.py :
        il ne doit donc contenir NI stderr brut, NI chemin de fichier, NI URL
        interne (SEC — fuite d'infra au client, audit 2026-08-28). Le tail
        stderr complet part au journal serveur via log.error. Seul marqueur
        conservé dans le message : « out of memory » quand le stderr l'indique,
        pour que LocalModelManager._is_load_capacity_error déclenche sa retry
        après éviction réelle.
        """
        url = self.llama_url("/health")
        timeout = self._model.load_timeout_seconds or settings.model_load_timeout_seconds
        deadline = time.monotonic() + timeout

        async with httpx.AsyncClient(timeout=3.0) as client:
            while time.monotonic() < deadline:
                if self._process and self._process.returncode is not None:
                    # Laisser _drain_stream vider les pipes avant de lire le tail
                    await asyncio.sleep(0.15)
                    tail = list(self._stderr_tail)
                    tail_text = "\n  ".join(tail) if tail else "(aucune sortie capturée)"
                    log.error(
                        "llama-server '%s' mort (code %s) — tail stderr complet :\n  %s",
                        self._model.id,
                        self._process.returncode,
                        tail_text,
                    )
                    message = (
                        f"Le modèle '{self._model.id}' n'a pas démarré : le "
                        f"processus llama-server s'est terminé prématurément "
                        f"(code {self._process.returncode})."
                    )
                    if any(m in tail_text.lower() for m in LOAD_CAPACITY_MARKERS):
                        message += (
                            " Cause probable : mémoire GPU insuffisante "
                            "(out of memory)."
                        )
                    raise RuntimeError(message)

                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("status") in ("ok", "no slot available"):
                            return
                except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError):
                    pass

                await asyncio.sleep(2)

        log.error(
            "llama-server '%s' injoignable sur %s après %ds (timeout de santé).",
            self._model.id,
            url,
            timeout,
        )
        raise TimeoutError(
            f"Le modèle '{self._model.id}' n'a pas répondu dans les {timeout}s "
            f"imparties."
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

        # Annuler la task de chargement si un unload() survient pendant LOADING :
        # sans ça elle continuerait de poller /health sur un port mort jusqu'au
        # timeout complet. Jamais la tâche courante (symétrique à _idle_task).
        if (
            self._load_task
            and not self._load_task.done()
            and self._load_task is not asyncio.current_task()
        ):
            self._load_task.cancel()
            try:
                await self._load_task
            except asyncio.CancelledError:
                pass
            self._load_task = None
            # Débloquer tout waiter d'ensure_loaded : si la task de chargement est
            # annulée avant d'avoir commencé à s'exécuter, son bloc finally
            # (event.set) peut ne jamais tourner et les waiters resteraient bloqués
            # jusqu'au timeout. On garantit ici le réveil (l'état UNLOADED fera que
            # la requête repassera par ensure_loaded → rechargement).
            if self._ready_event is not None:
                self._ready_event.set()

        # Annuler le moniteur d'inactivité, SAUF si on EST dans cette tâche
        # (unload déclenché par _idle_monitor lui-même) : s'auto-cancel+await
        # lèverait une erreur asyncio. Dans ce cas, on laisse la tâche courante
        # se terminer naturellement après le return de unload().
        if (
            self._idle_task
            and not self._idle_task.done()
            and self._idle_task is not asyncio.current_task()
        ):
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
        Watchdog background : détecte toujours les crashs du sous-processus.
        Si idle_unload_enabled=True, décharge aussi le modèle après inactivité.

        Le node-agent met idle_unload_enabled=False : le trafic d'inférence va
        directement de l'orchestrateur vers llama-server, donc l'agent ne peut
        pas déduire l'inactivité depuis ses propres compteurs pin/unpin.
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

            # Détection de crash : si llama-server meurt en état READY (CUDA OOM,
            # segfault…), sa VRAM reste comptée (state READY) et bloque le budget,
            # forçant des évictions LRU inutiles ailleurs. On transitionne alors
            # vers UNLOADED pour libérer le port et le budget. On NE fait PAS
            # unload() ici : elle cancel+await self._idle_task, or on EST dans
            # cette tâche → auto-annulation. On utilise un nettoyage dédié.
            # Un process qui a tourné puis est mort a un returncode non nul ;
            # on ne considère PAS _process is None comme un crash (cet état
            # n'existe pas pour un vrai modèle READY et casserait des invariants).
            if self._process is not None and self._process.returncode is not None:
                await self._mark_crashed()
                break

            # En cluster, conserver uniquement le watchdog de crash. Une
            # éviction idle décidée ici serait aveugle au trafic direct et
            # pourrait tuer un stream en cours.
            if not self._idle_unload_enabled:
                continue

            # INVARIANT : un modèle avec des requêtes actives ne doit jamais être
            # déchargé. Un stream plus long qu'idle_timeout ne rafraîchit
            # _last_request_time qu'à son admission — sans ce garde-fou, le
            # moniteur tuerait llama-server en plein milieu de la génération.
            if self.is_pinned:
                continue

            idle_for = time.monotonic() - self._last_request_time
            log.debug("'%s' idle depuis %.0fs / %ds", self._model.id, idle_for, timeout)

            if idle_for >= timeout:
                log.info(
                    "Inactivité détectée sur '%s' (%.0fs sans requête) — déchargement.",
                    self._model.id, idle_for,
                )
                await self.unload(reason=f"inactivité ({idle_for:.0f}s)")
                break

    async def _mark_crashed(self) -> None:
        """
        Nettoyage dédié au crash de llama-server détecté en état READY.
        N'annule PAS la tâche courante (contrairement à unload()) : il est
        appelé DEPUIS _idle_monitor (donc self._idle_task). Libère le port
        (on_unload) et signale le changement de capacité (on_capacity_change).
        """
        returncode = self._process.returncode if self._process else None
        tail = list(self._stderr_tail)
        tail_text = "\n  ".join(tail) if tail else "(aucune sortie capturée)"
        log.error(
            "llama-server '%s' a crashé en état READY (code %s) — VRAM libérée, "
            "modèle marqué UNLOADED.\nStderr (dernières %d lignes) :\n  %s",
            self._model.id, returncode, len(tail), tail_text,
        )

        async with self._state_lock:
            self._state = ModelState.UNLOADED
        self._process = None

        if self._on_unload:
            self._on_unload(self._model.id)
        if self._on_capacity_change:
            self._on_capacity_change()

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
            "last_load_seconds": (
                round(self._last_load_seconds, 2)
                if self._last_load_seconds is not None
                else None
            ),
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
            "speculative": (
                {
                    "type": self._model.speculative.type,
                    "draft_max": self._model.speculative.draft_max,
                    "draft_min": self._model.speculative.draft_min,
                    "draft_p_min": self._model.speculative.draft_p_min,
                }
                if self._model.speculative is not None
                else None
            ),
        }
