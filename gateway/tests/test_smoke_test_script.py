"""
Tests de la logique de décision de `gateway/deploy/smoke_test.sh` (COR-006).

Avant COR-006, `update.sh` conservait ou restaurait une version sur la seule
base de `/ready`. Une version qui ouvre un stream en 200 sans jamais produire un
token était donc acceptée. Le smoke test est le gate qui ferme ce trou — encore
faut-il que SA logique de décision soit elle-même testée : un smoke test dont on
n'a jamais vérifié le verdict ne vaut pas mieux que `/ready`.

Ces tests n'ont besoin ni de GPU, ni de `llama-server`, ni de systemd : le script
est lancé contre un faux serveur HTTP local qui rejoue tour à tour les scénarios
d'échec réels (stream muet, erreur upstream, absence de `[DONE]`, readiness 503,
TTFT lent…).
"""
from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

REPO_GATEWAY = Path(__file__).resolve().parents[1]
SMOKE_SCRIPT = REPO_GATEWAY / "deploy" / "smoke_test.sh"

# Secrets « reconnaissables » : aucun ne doit apparaître dans la sortie, le
# rapport, ni dans la ligne de commande d'un processus fils.
ADMIN_SECRET = "ADMINSECRETSENTINEL0123456789abcdefghij"
SENTINEL_KEY = "llmgw-SMOKEKEYSENTINEL0123456789abcdef"
KEY_PREFIX = "llmgw-SMOKEKEY"

MODEL_ID = "qwen3.5-9b-q5_k_m"

# Exit codes documentés dans l'en-tête du script.
EXIT_OK = 0
EXIT_GENERATION = 1
EXIT_USAGE = 2
EXIT_PREFLIGHT = 3
EXIT_TTFT = 4
EXIT_IDENTITY = 5
EXIT_UNLOAD = 6


# ── Faux serveur ──────────────────────────────────────────────────────────────

class Scenario:
    """Configuration mutable partagée avec le handler."""

    def __init__(self) -> None:
        self.ready_status = 200
        self.chat = "ok"
        self.usage_entries = 1
        self.content_delay_s = 0.0
        self.hang_s = 0.0
        self.delete_key_status = 200
        self.delete_user_status = 200
        self.load_status = 200
        self.unload_status = 200
        self.create_user_status = 201
        self.model_state = "unloaded"
        self.calls: list[tuple[str, str]] = []
        self.auth: dict[str, str] = {}
        self.lock = threading.Lock()

    def record(self, method: str, path: str, auth: str) -> None:
        with self.lock:
            self.calls.append((method, path))
            self.auth[f"{method} {path.split('?')[0]}"] = auth

    def seen(self, method: str, prefix: str) -> bool:
        with self.lock:
            return any(m == method and p.startswith(prefix) for m, p in self.calls)


def _chunk(content: str | None, *, usage: bool = False, model: str = MODEL_ID,
           reasoning: str | None = None, tools: bool = False) -> bytes:
    payload: dict = {
        "id": "chatcmpl-smoke",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
    }
    if content is not None:
        payload["choices"][0]["delta"]["content"] = content
    if reasoning is not None:
        payload["choices"][0]["delta"]["reasoning_content"] = reasoning
    if tools:
        payload["choices"][0]["delta"]["tool_calls"] = [
            {"index": 0, "id": "call-smoke", "type": "function",
             "function": {"name": "get_weather", "arguments": "{}"}},
        ]
    if usage:
        payload["usage"] = {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}
    return ("data: " + json.dumps(payload) + "\n\n").encode()


_ROLE_CHUNK = (
    "data: " + json.dumps({
        "id": "chatcmpl-smoke",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": MODEL_ID,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }) + "\n\n"
).encode()


def _make_handler(scenario: Scenario):
    class Handler(BaseHTTPRequestHandler):
        # HTTP/1.0 : la fermeture de connexion délimite le corps, ce qui permet
        # de simuler un stream tronqué sans jouer avec le chunked encoding.
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):  # silence
            pass

        # ── utilitaires ───────────────────────────────────────────────────
        def _auth(self) -> str:
            return self.headers.get("Authorization", "")

        def _json(self, status: int, body) -> None:
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length))
            except ValueError:
                return {}

        # ── routage ───────────────────────────────────────────────────────
        def do_GET(self):
            parsed = urlparse(self.path)
            scenario.record("GET", self.path, self._auth())
            if parsed.path == "/health":
                self._json(200, {"status": "ok", "models_loaded": []})
            elif parsed.path == "/ready":
                if scenario.ready_status == 200:
                    self._json(200, {"status": "ready", "level": "structural"})
                else:
                    self._json(503, {"status": "not_ready", "level": "none",
                                     "reason": "model_file_missing"})
            elif parsed.path == "/admin/models":
                self._json(200, [
                    {"id": "llama-3.3-70b-instruct", "enabled": True, "vram_gb": 42.0,
                     "state": "unloaded"},
                    {"id": MODEL_ID, "enabled": True, "vram_gb": 7.0,
                     "state": scenario.model_state},
                    {"id": "tiny-disabled", "enabled": False, "vram_gb": 1.0,
                     "state": "unloaded"},
                ])
            elif parsed.path == "/admin/usage":
                query = parse_qs(parsed.query)
                entries = [
                    {"id": i, "timestamp": "2026-07-30T00:00:00", "model": MODEL_ID,
                     "username": (query.get("username") or [""])[0],
                     "prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13,
                     "duration_ms": 120, "status_code": 200, "request_id": "r"}
                    for i in range(scenario.usage_entries)
                ]
                self._json(200, entries)
            else:
                self._json(404, {"detail": "not found"})

        def do_POST(self):
            parsed = urlparse(self.path)
            scenario.record("POST", self.path, self._auth())
            body = self._read_body()
            if parsed.path == "/admin/users":
                if scenario.create_user_status != 201:
                    self._json(scenario.create_user_status, {"detail": "refusé"})
                    return
                self._json(201, {"id": 42, "username": body.get("username"),
                                 "email": None, "created_at": "2026-07-30T00:00:00",
                                 "is_active": True, "rpm_limit": 60,
                                 "monthly_token_limit": None, "notes": body.get("notes")})
            elif re.fullmatch(r"/admin/users/[^/]+/keys", parsed.path):
                self._json(201, {"api_key": SENTINEL_KEY, "key_prefix": KEY_PREFIX,
                                 "name": "smoke-test", "created_at": "2026-07-30T00:00:00",
                                 "expires_at": None})
            elif re.fullmatch(r"/admin/models/[^/]+/load", parsed.path):
                self._json(scenario.load_status, {"message": "chargé"})
            elif re.fullmatch(r"/admin/models/[^/]+/unload", parsed.path):
                self._json(scenario.unload_status, {"message": "déchargé"})
            elif parsed.path == "/v1/chat/completions":
                self._chat()
            else:
                self._json(404, {"detail": "not found"})

        def do_DELETE(self):
            parsed = urlparse(self.path)
            scenario.record("DELETE", self.path, self._auth())
            if parsed.path.startswith("/admin/keys/"):
                self._json(scenario.delete_key_status, {"message": "révoquée"})
            elif parsed.path.startswith("/admin/users/"):
                self._json(scenario.delete_user_status, {"status": "anonymized"})
            else:
                self._json(404, {"detail": "not found"})

        # ── scénarios de génération ───────────────────────────────────────
        def _sse_headers(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

        def _write(self, data: bytes) -> None:
            self.wfile.write(data)
            self.wfile.flush()

        def _chat(self) -> None:
            mode = scenario.chat
            if mode == "http_error":
                self._json(503, {"error": {"message": "modèle indisponible",
                                           "type": "service_unavailable", "code": "503"}})
                return

            self._sse_headers()
            self._write(_ROLE_CHUNK)

            if mode == "no_content":
                # Le stream s'ouvre, se termine proprement… et n'a rien généré.
                self._write(_chunk(""))
                self._write(_chunk("", usage=True))
                self._write(b"data: [DONE]\n\n")
                return
            if mode == "empty_generated_fields":
                # Régression : les champs génératifs présents mais VIDES ne
                # prouvent rien — même combinaison que « no_content », étendue
                # à reasoning_content.
                self._write(_chunk("", reasoning=""))
                self._write(_chunk("", reasoning="", usage=True))
                self._write(b"data: [DONE]\n\n")
                return
            if mode == "reasoning_only":
                # Modèle thinking avec max_tokens court : toute la génération
                # part dans reasoning_content (routage llama.cpp/DeepSeek).
                for _ in range(3):
                    self._write(_chunk(None, reasoning="réflexion…"))
                self._write(_chunk(None, reasoning="…", usage=True))
                self._write(b"data: [DONE]\n\n")
                return
            if mode == "reasoning_then_content":
                self._write(_chunk(None, reasoning="réflexion…"))
                self._write(_chunk("OK", usage=True))
                self._write(b"data: [DONE]\n\n")
                return
            if mode == "tool_calls_only":
                self._write(_chunk(None, tools=True))
                self._write(_chunk(None, usage=True))
                self._write(b"data: [DONE]\n\n")
                return
            if mode == "upstream_error":
                self._write(b'data: {"error": {"message": "backend", "type": "server_error"}}\n\n')
                self._write(b"data: [DONE]\n\n")
                return
            if mode == "no_done":
                self._write(_chunk("OK", usage=True))
                return  # connexion fermée sans [DONE]
            if mode == "hang":
                time.sleep(scenario.hang_s)
                return
            if mode == "wrong_model":
                self._write(_chunk("OK", usage=True, model="un-autre-modele"))
                self._write(b"data: [DONE]\n\n")
                return

            # mode "ok" (éventuellement ralenti pour exercer le seuil TTFT)
            if scenario.content_delay_s:
                time.sleep(scenario.content_delay_s)
            self._write(_chunk("OK"))
            self._write(_chunk("!", usage=True))
            self._write(b"data: [DONE]\n\n")

    return Handler


@pytest.fixture
def gateway():
    scenario = Scenario()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(scenario))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    scenario.url = f"http://127.0.0.1:{server.server_address[1]}"  # type: ignore[attr-defined]
    try:
        yield scenario
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / "env"
    path.write_text(
        f"ADMIN_SECRET={ADMIN_SECRET}\n"
        "CLUSTER_MODE=local\n"
        "GATEWAY_HOST=127.0.0.1\n"
        "GATEWAY_PORT=8000\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def run_smoke(gateway, env_file: Path, *extra: str, timeout: int = 90):
    """Lance le script contre le faux serveur et renvoie le CompletedProcess."""
    cmd = [
        "bash", str(SMOKE_SCRIPT),
        "--base-url", gateway.url,
        "--admin-url", gateway.url,
        "--env-file", str(env_file),
        "--model", MODEL_ID,
        "--connect-timeout", "5",
        "--ready-timeout", "5",
        "--admin-timeout", "10",
        "--load-timeout", "10",
        "--stream-timeout", "15",
        "--usage-timeout", "3",
        *extra,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def combined(proc) -> str:
    return proc.stdout + proc.stderr


def assert_identity_cleaned(gateway) -> None:
    """L'utilisateur éphémère DOIT être anonymisé, quoi qu'il arrive."""
    assert gateway.seen("DELETE", "/admin/users/evaruntime-smoke-"), (
        "l'identité éphémère n'a pas été anonymisée : "
        f"appels observés = {gateway.calls}"
    )


def assert_no_secret(text: str) -> None:
    assert SENTINEL_KEY not in text, "la clé éphémère a fuité dans la sortie"
    assert ADMIN_SECRET not in text, "l'ADMIN_SECRET a fuité dans la sortie"
    # Le préfixe de clé n'est pas un secret, mais il reste un identifiant
    # d'authentification : il n'a rien à faire dans un rapport archivé.
    assert KEY_PREFIX not in text, "le préfixe de clé a fuité dans la sortie"


# ── Chemin nominal ────────────────────────────────────────────────────────────

def test_generation_reussie_est_un_succes(gateway, env_file):
    proc = run_smoke(gateway, env_file)
    assert proc.returncode == EXIT_OK, combined(proc)
    assert "SUCCÈS" in proc.stdout
    assert_identity_cleaned(gateway)


def test_ttft_mesure_et_rapporte(gateway, env_file):
    proc = run_smoke(gateway, env_file, "--json")
    assert proc.returncode == EXIT_OK, combined(proc)
    report = json.loads(proc.stdout)
    assert report["exit_code"] == EXIT_OK
    assert report["reason"] == "ok"
    assert report["content_chunks"] == 2
    assert report["prompt_tokens"] == 9 and report["completion_tokens"] == 4
    assert report["usage_entries"] == 1
    assert report["headers_ms"] >= 0
    # Le TTFT est mesuré et n'est jamais antérieur aux en-têtes : il compte le
    # premier delta UTILE, qui arrive forcément après.
    assert report["ttft_ms"] >= report["headers_ms"]
    assert report["model"] == MODEL_ID


def test_le_chemin_public_est_bien_authentifie_par_la_cle_ephemere(gateway, env_file):
    proc = run_smoke(gateway, env_file)
    assert proc.returncode == EXIT_OK, combined(proc)
    # La génération doit traverser l'authentification utilisateur, pas l'admin.
    assert gateway.auth["POST /v1/chat/completions"] == f"Bearer {SENTINEL_KEY}"
    assert gateway.auth["POST /admin/users"] == f"Bearer {ADMIN_SECRET}"
    # /health est publique : l'ADMIN_SECRET n'a rien à faire sur le chemin public.
    assert gateway.auth["GET /health"] == ""


def test_modele_derive_de_la_configuration_quand_non_precise(gateway, env_file):
    """Défaut dérivé : le plus petit modèle activé, jamais codé en dur."""
    cmd = [
        "bash", str(SMOKE_SCRIPT),
        "--base-url", gateway.url, "--admin-url", gateway.url,
        "--env-file", str(env_file), "--json",
        "--connect-timeout", "5", "--ready-timeout", "5", "--admin-timeout", "10",
        "--load-timeout", "10", "--stream-timeout", "15", "--usage-timeout", "3",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    assert proc.returncode == EXIT_OK, combined(proc)
    assert json.loads(proc.stdout)["model"] == MODEL_ID


# ── Le cœur de l'item : « répondre » n'est pas « servir » ─────────────────────

def test_stream_ouvert_sans_aucun_contenu_est_un_echec(gateway, env_file):
    """200 + [DONE] mais zéro delta utile : la version ne sert pas."""
    gateway.chat = "no_content"
    proc = run_smoke(gateway, env_file)
    assert proc.returncode == EXIT_GENERATION, combined(proc)
    assert "no_content" in combined(proc)
    assert_identity_cleaned(gateway)


def test_champs_generatifs_vides_ne_prouvent_toujours_rien(gateway, env_file):
    """Contrôle inverse : content/reasoning_content présents mais vides échouent."""
    gateway.chat = "empty_generated_fields"
    proc = run_smoke(gateway, env_file)
    assert proc.returncode == EXIT_GENERATION, combined(proc)
    assert "no_content" in combined(proc)
    assert_identity_cleaned(gateway)


# ── Deltas génératifs au sens de proxy._record_ttft ───────────────────────────

def test_stream_de_pure_reflexion_est_un_succes(gateway, env_file):
    """Modèle thinking + max_tokens court : reasoning_content prouve la génération.

    Faux négatif constaté en revue de PR #23 : un modèle qui raisonne épuise les
    16 tokens par défaut dans des deltas `reasoning_content`, routés tels quels
    par la gateway (proxy.py préserve ce champ). Le parseur ne comptait que
    `delta.content` et déclarait « no_content » sur un service qui génère.
    """
    gateway.chat = "reasoning_only"
    proc = run_smoke(gateway, env_file, "--json")
    assert proc.returncode == EXIT_OK, combined(proc)
    report = json.loads(proc.stdout)
    assert report["reason"] == "ok"
    assert report["content_chunks"] == 0
    assert report["reasoning_chunks"] > 0
    # Le TTFT est bien ancré sur le premier delta de réflexion, pas sur [DONE].
    assert report["ttft_ms"] >= report["headers_ms"]


def test_raisonnement_puis_contenu_sont_tous_deux_comptes(gateway, env_file):
    gateway.chat = "reasoning_then_content"
    proc = run_smoke(gateway, env_file, "--json")
    assert proc.returncode == EXIT_OK, combined(proc)
    report = json.loads(proc.stdout)
    assert report["reason"] == "ok"
    assert report["content_chunks"] == 1
    assert report["reasoning_chunks"] >= 1


def test_tool_calls_seuls_prouvent_la_generation(gateway, env_file):
    """Même définition côté outils : un stream d'appels d'outils est servi."""
    gateway.chat = "tool_calls_only"
    proc = run_smoke(gateway, env_file, "--json")
    assert proc.returncode == EXIT_OK, combined(proc)
    assert json.loads(proc.stdout)["tool_chunks"] > 0


def test_erreur_upstream_pendant_le_stream_est_un_echec(gateway, env_file):
    gateway.chat = "upstream_error"
    proc = run_smoke(gateway, env_file)
    assert proc.returncode == EXIT_GENERATION, combined(proc)
    assert "upstream_error" in combined(proc)
    assert_identity_cleaned(gateway)


def test_statut_http_non_200_est_un_echec(gateway, env_file):
    gateway.chat = "http_error"
    proc = run_smoke(gateway, env_file)
    assert proc.returncode == EXIT_GENERATION, combined(proc)
    assert "http_status" in combined(proc)
    assert_identity_cleaned(gateway)


def test_stream_sans_done_est_un_echec_sans_blocage(gateway, env_file):
    gateway.chat = "no_done"
    started = time.monotonic()
    proc = run_smoke(gateway, env_file)
    elapsed = time.monotonic() - started
    assert proc.returncode == EXIT_GENERATION, combined(proc)
    assert "no_done" in combined(proc)
    # Le stream est fermé par le serveur : aucune attente de --stream-timeout.
    assert elapsed < 30, "le smoke test a bloqué au lieu de conclure"
    assert_identity_cleaned(gateway)


def test_stream_qui_ne_se_termine_jamais_est_borne(gateway, env_file):
    """--stream-timeout borne un backend muet : jamais de blocage indéfini."""
    gateway.chat = "hang"
    gateway.hang_s = 60.0
    started = time.monotonic()
    proc = run_smoke(gateway, env_file, "--stream-timeout", "5", timeout=120)
    elapsed = time.monotonic() - started
    assert proc.returncode == EXIT_GENERATION, combined(proc)
    assert elapsed < 45, f"le smoke test n'a pas été borné ({elapsed:.0f}s)"
    assert_identity_cleaned(gateway)


def test_modele_incorrect_dans_le_stream_est_un_echec(gateway, env_file):
    gateway.chat = "wrong_model"
    proc = run_smoke(gateway, env_file)
    assert proc.returncode == EXIT_GENERATION, combined(proc)
    assert "model_mismatch" in combined(proc)
    assert_identity_cleaned(gateway)


def test_log_usage_absent_est_un_echec(gateway, env_file):
    """Une génération non comptabilisée est une perte de facturation."""
    gateway.usage_entries = 0
    proc = run_smoke(gateway, env_file)
    assert proc.returncode == EXIT_GENERATION, combined(proc)
    assert "usage_log_missing" in combined(proc)
    assert_identity_cleaned(gateway)


def test_chargement_de_modele_impossible_est_un_echec(gateway, env_file):
    gateway.load_status = 503
    proc = run_smoke(gateway, env_file)
    assert proc.returncode == EXIT_GENERATION, combined(proc)
    assert not gateway.seen("POST", "/v1/chat/completions")
    assert_identity_cleaned(gateway)


# ── Cycle de vie du modèle : chargement / déchargement (sujet #28) ────────────

def test_modele_charge_par_la_recette_est_decharge(gateway, env_file):
    """Contrôle positif : ce que la recette charge, elle le décharge."""
    proc = run_smoke(gateway, env_file)
    assert proc.returncode == EXIT_OK, combined(proc)
    assert gateway.seen("POST", f"/admin/models/{MODEL_ID}/load")
    assert gateway.seen("POST", f"/admin/models/{MODEL_ID}/unload")


def test_modele_deja_charge_n_est_pas_decharge(gateway, env_file):
    """
    Un modèle déjà chargé par l'opérateur ne doit ni être rechargé, ni être
    déchargé en fin de recette : le décharger ferait tomber un service
    volontairement prêt. Le contrôle d'absence porte sur `load`/`unload` ; la
    lecture du registre et la génération en sont les contrôles positifs.
    """
    gateway.model_state = "ready"
    proc = run_smoke(gateway, env_file)
    assert proc.returncode == EXIT_OK, combined(proc)
    assert gateway.seen("GET", "/admin/models")
    assert gateway.seen("POST", "/v1/chat/completions")
    assert not gateway.seen("POST", f"/admin/models/{MODEL_ID}/load")
    assert not gateway.seen("POST", f"/admin/models/{MODEL_ID}/unload")


def test_echec_dechargement_est_un_exit_code_dedie(gateway, env_file):
    """Un modèle chargé par la recette mais jamais déchargé ne doit JAMAIS
    passer pour un succès : code de sortie dédié et rapport explicite."""
    gateway.unload_status = 500
    proc = run_smoke(gateway, env_file, "--json")
    assert proc.returncode == EXIT_UNLOAD, combined(proc)
    report = json.loads(proc.stdout)
    assert report["reason"] == "model_unload_failed"
    assert "DÉCHARGEMENT RÉSIDUEL" in combined(proc)
    assert_identity_cleaned(gateway)


# ── Préflight ─────────────────────────────────────────────────────────────────

def test_readiness_503_echoue_avant_toute_generation(gateway, env_file):
    gateway.ready_status = 503
    proc = run_smoke(gateway, env_file)
    assert proc.returncode == EXIT_PREFLIGHT, combined(proc)
    assert "model_file_missing" in combined(proc)
    # Ni identité créée, ni modèle chargé, ni génération tentée.
    assert not gateway.seen("POST", "/admin/users")
    assert not gateway.seen("POST", "/v1/chat/completions")


def test_liveness_injoignable_echoue_en_preflight(env_file, tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
    dead = f"http://127.0.0.1:{dead_port}"
    proc = subprocess.run(
        ["bash", str(SMOKE_SCRIPT), "--base-url", dead, "--admin-url", dead,
         "--env-file", str(env_file), "--model", MODEL_ID,
         "--connect-timeout", "2", "--ready-timeout", "3"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == EXIT_PREFLIGHT, combined(proc)


# ── TTFT : alerte par défaut, gate seulement sur demande explicite ────────────

def test_ttft_au_dessus_du_seuil_reste_un_succes_avec_avertissement(gateway, env_file):
    """§11 : une régression TTFT ne doit jamais déclencher de rollback seule."""
    gateway.content_delay_s = 0.6
    proc = run_smoke(gateway, env_file, "--ttft-threshold-ms", "100")
    assert proc.returncode == EXIT_OK, combined(proc)
    assert "ttft_threshold_exceeded_warning" in proc.stdout
    assert "aucun rollback" in combined(proc).lower()


def test_ttft_au_dessus_du_seuil_echoue_si_le_gate_est_active(gateway, env_file):
    gateway.content_delay_s = 0.6
    proc = run_smoke(gateway, env_file, "--ttft-threshold-ms", "100", "--fail-on-ttft")
    assert proc.returncode == EXIT_TTFT, combined(proc)
    assert "ttft_threshold_exceeded" in proc.stdout
    assert_identity_cleaned(gateway)


def test_ttft_sous_le_seuil_ne_declenche_rien(gateway, env_file):
    proc = run_smoke(gateway, env_file, "--ttft-threshold-ms", "60000", "--fail-on-ttft")
    assert proc.returncode == EXIT_OK, combined(proc)


# ── Nettoyage de l'identité éphémère ──────────────────────────────────────────

def test_cleanup_revoque_la_cle_et_anonymise_l_utilisateur(gateway, env_file):
    proc = run_smoke(gateway, env_file)
    assert proc.returncode == EXIT_OK, combined(proc)
    assert gateway.seen("DELETE", f"/admin/keys/{KEY_PREFIX}")
    assert_identity_cleaned(gateway)


def test_cleanup_survit_a_un_echec_de_generation(gateway, env_file):
    gateway.chat = "no_content"
    proc = run_smoke(gateway, env_file)
    assert proc.returncode == EXIT_GENERATION
    assert gateway.seen("DELETE", f"/admin/keys/{KEY_PREFIX}")
    assert_identity_cleaned(gateway)


def test_cleanup_survit_a_une_interruption(gateway, env_file):
    """Un Ctrl-C ne doit jamais laisser une identité de smoke test active."""
    gateway.chat = "hang"
    gateway.hang_s = 60.0
    cmd = [
        "bash", str(SMOKE_SCRIPT),
        "--base-url", gateway.url, "--admin-url", gateway.url,
        "--env-file", str(env_file), "--model", MODEL_ID,
        "--connect-timeout", "5", "--ready-timeout", "5", "--admin-timeout", "10",
        "--load-timeout", "10", "--stream-timeout", "60", "--usage-timeout", "3",
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if gateway.seen("POST", "/v1/chat/completions"):
                break
            time.sleep(0.1)
        else:  # pragma: no cover - filet de sécurité
            pytest.fail("la génération n'a jamais démarré")
        # Ctrl-C réel : le signal va au groupe de processus, comme depuis un TTY.
        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        out, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:  # pragma: no cover
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.communicate(timeout=10)

    assert proc.returncode != 0
    assert_identity_cleaned(gateway)
    assert_no_secret(out + err)


def test_cleanup_est_idempotent_meme_si_la_cle_est_deja_revoquee(gateway, env_file):
    gateway.delete_key_status = 404
    gateway.delete_user_status = 404
    proc = run_smoke(gateway, env_file)
    # 404 = déjà nettoyé : toléré, le succès fonctionnel est préservé.
    assert proc.returncode == EXIT_OK, combined(proc)


def test_echec_de_nettoyage_est_signale_par_un_exit_code_dedie(gateway, env_file):
    """Une identité résiduelle ne doit jamais passer pour un succès."""
    gateway.delete_user_status = 500
    proc = run_smoke(gateway, env_file)
    assert proc.returncode == EXIT_IDENTITY, combined(proc)
    assert "RÉSIDUELLE" in combined(proc)
    assert "identity_cleanup_failed" in proc.stdout


def test_identite_non_creable_ne_tente_pas_de_generer(gateway, env_file):
    gateway.create_user_status = 409
    proc = run_smoke(gateway, env_file)
    assert proc.returncode == EXIT_IDENTITY, combined(proc)
    assert not gateway.seen("POST", "/v1/chat/completions")
    # Armé avant l'appel : le nettoyage est tenté même si la création a échoué.
    assert_identity_cleaned(gateway)


# ── Absence de secret ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("scenario_name", ["ok", "no_content", "upstream_error", "http_error"])
def test_aucun_secret_dans_la_sortie(gateway, env_file, scenario_name):
    gateway.chat = scenario_name
    proc = run_smoke(gateway, env_file)
    assert_no_secret(combined(proc))


def test_aucun_secret_dans_le_rapport_json(gateway, env_file):
    proc = run_smoke(gateway, env_file, "--json")
    assert_no_secret(proc.stdout)
    document = json.dumps(json.loads(proc.stdout))
    assert_no_secret(document)


def test_aucun_secret_dans_les_lignes_de_commande(gateway, env_file):
    """La clé éphémère ne doit jamais être visible dans `ps`."""
    gateway.chat = "hang"
    gateway.hang_s = 8.0
    cmd = [
        "bash", str(SMOKE_SCRIPT),
        "--base-url", gateway.url, "--admin-url", gateway.url,
        "--env-file", str(env_file), "--model", MODEL_ID,
        "--connect-timeout", "5", "--ready-timeout", "5", "--admin-timeout", "10",
        "--load-timeout", "10", "--stream-timeout", "10", "--usage-timeout", "3",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    seen_generation = False
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and proc.poll() is None:
            snapshot = subprocess.run(
                ["ps", "-Ao", "args"], capture_output=True, text=True
            ).stdout
            assert SENTINEL_KEY not in snapshot, "la clé éphémère est visible dans ps"
            assert ADMIN_SECRET not in snapshot, "l'ADMIN_SECRET est visible dans ps"
            if gateway.seen("POST", "/v1/chat/completions"):
                seen_generation = True
                break
            time.sleep(0.05)
        # On continue d'échantillonner pendant la génération elle-même.
        for _ in range(20):
            if proc.poll() is not None:
                break
            snapshot = subprocess.run(
                ["ps", "-Ao", "args"], capture_output=True, text=True
            ).stdout
            assert SENTINEL_KEY not in snapshot, "la clé éphémère est visible dans ps"
            assert ADMIN_SECRET not in snapshot, "l'ADMIN_SECRET est visible dans ps"
            time.sleep(0.05)
    finally:
        if proc.poll() is None:
            proc.terminate()
        out, err = proc.communicate(timeout=60)
    assert seen_generation, "la génération n'a jamais démarré"
    assert_no_secret(out + err)


# ── Contrat d'usage ───────────────────────────────────────────────────────────

def test_option_inconnue_rend_le_code_d_usage(env_file):
    proc = subprocess.run(
        ["bash", str(SMOKE_SCRIPT), "--pas-une-option"],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == EXIT_USAGE


def test_admin_secret_placeholder_est_refuse(tmp_path, gateway):
    env = tmp_path / "env"
    env.write_text("ADMIN_SECRET=CHANGE_ME_ADMIN\n", encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(SMOKE_SCRIPT), "--env-file", str(env), "--base-url", gateway.url],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == EXIT_USAGE
    assert "ADMIN_SECRET" in combined(proc)
    assert not gateway.calls, "aucun appel ne doit partir sans secret exploitable"


def test_aide_sort_en_zero():
    proc = subprocess.run(
        ["bash", str(SMOKE_SCRIPT), "--help"], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == EXIT_OK
    for code in ("0 succès", "1 échec fonctionnel", "2 erreur d'usage",
                 "3 préflight", "4 seuil TTFT", "5 identité", "6 modèle de recette"):
        assert code in proc.stdout


def test_script_syntaxiquement_valide():
    for script in (SMOKE_SCRIPT, UPDATE_SCRIPT):
        proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert proc.returncode == 0, f"{script.name} : {proc.stderr}"


# ── Source unique Linux/macOS : défaut de configuration choisi à l'exécution ──
#
# `deploy/smoke_test.sh` est la seule copie (issue #29) : `deploy-macos/` n'en a
# plus. Le fichier d'environnement par défaut dépend de `uname -s`. On simule la
# plateforme par un faux `uname` en tête de PATH, et HOME pointe vers tmp_path :
# aucun chemin réel de l'hôte n'est lu.

MACOS_LAYOUT_LIB = REPO_GATEWAY / "deploy-macos" / "code-layout-lib.sh"


def _platform_env(tmp_path: Path, system: str, **extra: str) -> dict[str, str]:
    fake_bin = tmp_path / f"fake-bin-{system}"
    fake_bin.mkdir(exist_ok=True)
    uname = fake_bin / "uname"
    uname.write_text(f"#!/bin/sh\necho {system}\n", encoding="utf-8")
    uname.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "LLM_GATEWAY_CONFIG_DIR"}
    env.update(PATH=f"{fake_bin}{os.pathsep}{env.get('PATH', '')}", HOME=str(tmp_path / "home"))
    env.update(extra)
    return env


def _write_env(path: Path, gateway) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"ADMIN_SECRET={ADMIN_SECRET}\nCLUSTER_MODE=local\n"
        "GATEWAY_HOST=127.0.0.1\nGATEWAY_PORT=8000\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def _run_without_env_file(script: Path, gateway, env: dict[str, str]):
    """Comme run_smoke, mais SANS --env-file : le défaut de plateforme s'applique."""
    cmd = [
        "bash", str(script),
        "--base-url", gateway.url, "--admin-url", gateway.url, "--model", MODEL_ID,
        "--connect-timeout", "5", "--ready-timeout", "5", "--admin-timeout", "10",
        "--load-timeout", "10", "--stream-timeout", "15", "--usage-timeout", "3",
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=90, env=env)


@pytest.mark.parametrize(
    ("system", "expected"),
    [("Darwin", "{home}/.config/evaruntime/env"), ("Linux", "/etc/llm-gateway/env")],
)
def test_aide_annonce_le_defaut_de_la_plateforme(tmp_path, system, expected):
    env = _platform_env(tmp_path, system)
    proc = subprocess.run(
        ["bash", str(SMOKE_SCRIPT), "--help"], capture_output=True, text=True, timeout=30, env=env
    )
    assert proc.returncode == EXIT_OK, combined(proc)
    assert f"(défaut : {expected.format(home=env['HOME'])})" in proc.stdout


def test_macos_lit_l_env_utilisateur_par_defaut(tmp_path, gateway):
    """Sur Darwin, sans --env-file, la recette lit ~/.config/evaruntime/env."""
    env = _platform_env(tmp_path, "Darwin")
    _write_env(Path(env["HOME"]) / ".config" / "evaruntime" / "env", gateway)
    proc = _run_without_env_file(SMOKE_SCRIPT, gateway, env)
    assert proc.returncode == EXIT_OK, combined(proc)
    assert_identity_cleaned(gateway)
    assert_no_secret(combined(proc))


def test_macos_n_utilise_jamais_le_chemin_linux(tmp_path, gateway):
    """Contrôle croisé : sur Darwin, l'absence de l'env utilisateur est signalée
    avec le chemin macOS — jamais /etc/llm-gateway — et aucun appel ne part."""
    env = _platform_env(tmp_path, "Darwin")
    proc = _run_without_env_file(SMOKE_SCRIPT, gateway, env)
    assert proc.returncode == EXIT_USAGE
    assert f"{env['HOME']}/.config/evaruntime/env" in combined(proc)
    assert "/etc/llm-gateway" not in combined(proc)
    assert not gateway.calls


def test_llm_gateway_config_dir_prime_sur_le_defaut_macos(tmp_path, gateway):
    config_dir = tmp_path / "explicit-config"
    _write_env(config_dir / "env", gateway)
    env = _platform_env(tmp_path, "Darwin", LLM_GATEWAY_CONFIG_DIR=str(config_dir))
    proc = _run_without_env_file(SMOKE_SCRIPT, gateway, env)
    assert proc.returncode == EXIT_OK, combined(proc)


def _without_home(env: dict[str, str]) -> dict[str, str]:
    """Job launchd : HOME peut être absent de l'environnement."""
    return {k: v for k, v in env.items() if k != "HOME"}


def test_macos_sans_home_l_aide_reste_disponible(tmp_path):
    env = _without_home(_platform_env(tmp_path, "Darwin"))
    proc = subprocess.run(
        ["bash", str(SMOKE_SCRIPT), "--help"], capture_output=True, text=True, timeout=30, env=env
    )
    assert proc.returncode == EXIT_OK, combined(proc)
    assert "unbound variable" not in combined(proc)
    assert "HOME non défini" in proc.stdout


def test_macos_sans_home_llm_gateway_config_dir_suffit(tmp_path, gateway):
    config_dir = tmp_path / "explicit-config"
    _write_env(config_dir / "env", gateway)
    env = _without_home(_platform_env(tmp_path, "Darwin", LLM_GATEWAY_CONFIG_DIR=str(config_dir)))
    proc = _run_without_env_file(SMOKE_SCRIPT, gateway, env)
    assert proc.returncode == EXIT_OK, combined(proc)
    assert_identity_cleaned(gateway)


def test_macos_sans_home_env_file_suffit(tmp_path, gateway, env_file):
    env = _without_home(_platform_env(tmp_path, "Darwin"))
    cmd = [
        "bash", str(SMOKE_SCRIPT), "--env-file", str(env_file),
        "--base-url", gateway.url, "--admin-url", gateway.url, "--model", MODEL_ID,
        "--connect-timeout", "5", "--ready-timeout", "5", "--admin-timeout", "10",
        "--load-timeout", "10", "--stream-timeout", "15", "--usage-timeout", "3",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90, env=env)
    assert proc.returncode == EXIT_OK, combined(proc)


def test_macos_sans_home_ni_surcharge_echoue_clairement(tmp_path, gateway):
    env = _without_home(_platform_env(tmp_path, "Darwin"))
    proc = _run_without_env_file(SMOKE_SCRIPT, gateway, env)
    assert proc.returncode == EXIT_USAGE
    assert "HOME n'est pas défini" in combined(proc)
    assert "unbound variable" not in combined(proc)
    assert not gateway.calls


def test_linux_sans_home_garde_le_defaut_systeme(tmp_path):
    env = _without_home(_platform_env(tmp_path, "Linux"))
    proc = subprocess.run(
        ["bash", str(SMOKE_SCRIPT), "--help"], capture_output=True, text=True, timeout=30, env=env
    )
    assert proc.returncode == EXIT_OK, combined(proc)
    assert "(défaut : /etc/llm-gateway/env)" in proc.stdout


def test_recette_installee_par_le_layout_macos_tourne_hors_du_checkout(tmp_path, gateway):
    """L'installeur macOS livre la source unique ET la lib qu'elle source.

    Avant la source unique, deploy-macos/ ne livrait pas deploy-mode-lib.sh : la
    recette installée échouait au `source`, donc ne pouvait pas être relancée
    pendant un incident sans le checkout.
    """
    install_dir = tmp_path / "home" / "Library" / "Application Support" / "evaruntime" / "gateway"
    sync = subprocess.run(
        ["bash", "-c", 'source "$1"; deploy_sync_gateway_operational_files "$2" "$3"',
         "bash", str(MACOS_LAYOUT_LIB), str(REPO_GATEWAY), str(install_dir)],
        capture_output=True, text=True, timeout=30,
    )
    assert sync.returncode == 0, sync.stderr
    installed = install_dir / "deploy" / "smoke_test.sh"
    assert installed.read_bytes() == SMOKE_SCRIPT.read_bytes(), "copie installée ≠ source unique"

    env = _platform_env(tmp_path, "Darwin")
    _write_env(Path(env["HOME"]) / ".config" / "evaruntime" / "env", gateway)
    proc = _run_without_env_file(installed, gateway, env)
    assert proc.returncode == EXIT_OK, combined(proc)
    assert_identity_cleaned(gateway)


# ── Branchement du gate dans update.sh ────────────────────────────────────────

UPDATE_SCRIPT = REPO_GATEWAY / "deploy" / "update.sh"


def run_update_dry(tmp_path: Path, *extra: str, env_lines: str = "ADMIN_SECRET=x\n"):
    (tmp_path / "etc").mkdir(exist_ok=True)
    (tmp_path / "etc" / "env").write_text(env_lines, encoding="utf-8")
    env = dict(os.environ)
    env.update(
        LLM_GATEWAY_CONFIG_DIR=str(tmp_path / "etc"),
        LLM_GATEWAY_INSTALL_DIR=str(tmp_path / "opt"),
        LLM_GATEWAY_DATA_DIR=str(tmp_path / "data"),
    )
    return subprocess.run(
        ["bash", str(UPDATE_SCRIPT), "--dry-run", *extra],
        capture_output=True, text=True, timeout=60, env=env,
    )


def test_update_annonce_le_gate_de_recette(tmp_path):
    proc = run_update_dry(tmp_path)
    assert proc.returncode == 0, combined(proc)
    assert "recette du premier token" in proc.stdout
    assert "doctor (avant/après)" in proc.stdout
    assert "Seuil TTFT     : désactivé" in proc.stdout


def test_update_annonce_le_ttft_en_alerte_par_defaut(tmp_path):
    proc = run_update_dry(tmp_path, "--ttft-threshold-ms", "4000")
    assert proc.returncode == 0, combined(proc)
    assert "alerte seulement (aucun rollback)" in proc.stdout


def test_update_annonce_le_ttft_en_gate_si_demande(tmp_path):
    proc = run_update_dry(tmp_path, "--ttft-threshold-ms", "4000", "--ttft-gate")
    assert proc.returncode == 0, combined(proc)
    assert "GATE (dépassement = rollback)" in proc.stdout


def test_update_signale_la_desactivation_de_la_recette(tmp_path):
    proc = run_update_dry(tmp_path, "--skip-smoke-test")
    assert proc.returncode == 0, combined(proc)
    assert "/ready SEUL" in proc.stdout


def test_update_refuse_un_seuil_ttft_invalide(tmp_path):
    proc = run_update_dry(tmp_path, "--ttft-threshold-ms", "vite")
    assert proc.returncode == 2, combined(proc)


def test_update_refuse_un_seuil_ttft_invalide_depuis_l_environnement(tmp_path):
    env = dict(os.environ, EVA_SMOKE_TTFT_THRESHOLD_MS="rapide")
    (tmp_path / "etc").mkdir(exist_ok=True)
    (tmp_path / "etc" / "env").write_text("ADMIN_SECRET=x\n", encoding="utf-8")
    env.update(
        LLM_GATEWAY_CONFIG_DIR=str(tmp_path / "etc"),
        LLM_GATEWAY_INSTALL_DIR=str(tmp_path / "opt"),
        LLM_GATEWAY_DATA_DIR=str(tmp_path / "data"),
    )
    proc = subprocess.run(
        ["bash", str(UPDATE_SCRIPT), "--dry-run"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert proc.returncode == 2, combined(proc)


def test_update_branche_les_gates_dans_le_bon_ordre():
    """
    Verrouille l'ordonnancement du gate : doctor AVANT la bascule (service encore
    debout), puis readiness, puis recette du premier token, puis doctor après.
    Un réordonnancement accidentel enlèverait au gate tout son intérêt.
    """
    source = UPDATE_SCRIPT.read_text(encoding="utf-8")
    markers = [
        'run_doctor "$STAGED_VENV/bin/python" "avant bascule"',
        'section "5/5  Redémarrage du service"',
        "HEALTHY=true",
        "run_smoke_test || SMOKE_RC=$?",
        'run_doctor "$INSTALL_DIR/venv/bin/python" "après bascule"',
    ]
    positions = []
    for marker in markers:
        index = source.find(marker)
        assert index != -1, f"marqueur absent de update.sh : {marker}"
        positions.append(index)
    assert positions == sorted(positions), "l'ordre des gates de update.sh a changé"


def test_update_roule_en_arriere_sur_echec_fonctionnel_pas_sur_ttft_seul():
    """
    §11 : le succès fonctionnel est un hard gate, la régression TTFT ne l'est que
    si l'opérateur l'a demandée. Le code 4 (seuil dépassé) n'est atteignable
    qu'avec --fail-on-ttft, et le code 5 (identité résiduelle) ne doit PAS
    restaurer une version qui a prouvé qu'elle sert.
    """
    source = UPDATE_SCRIPT.read_text(encoding="utf-8")
    branch = source.split("run_smoke_test || SMOKE_RC=$?", 1)[1].split("# ── doctor APRÈS", 1)[0]
    # Le cas 5 conserve la version déployée.
    case_five = branch.split("        5)", 1)[1].split("        *)", 1)[0]
    assert "rollback_deployed_release" not in case_five
    assert "RESTE déployée" in case_five
    # Le cas par défaut (échec fonctionnel) restaure.
    case_default = branch.split("        *)", 1)[1]
    assert "rollback_deployed_release" in case_default
    # --fail-on-ttft n'est transmis que si l'opérateur l'a demandé.
    assert '[[ "$TTFT_GATE" != true ]] || args+=(--fail-on-ttft)' in source


def test_update_ne_verifie_jamais_les_empreintes_gguf():
    """`--verify-hashes` relirait plusieurs centaines de Go à chaque update."""
    code = [
        line for line in UPDATE_SCRIPT.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]
    assert not any("--verify-hashes" in line for line in code)


# ── Contrat avec l'API réelle ─────────────────────────────────────────────────
# Le faux serveur ci-dessus ne vaut que s'il rejoue le VRAI contrat. Ces deux
# tests verrouillent les routes et les champs dont dépend smoke_test.sh : si une
# route est renommée ou un champ retiré, la recette casserait en production sans
# qu'aucun test unitaire ne le voie.

def _iter_app_routes(router):
    """
    Énumère `(méthode, chemin)` pour toutes les routes d'une application.

    Le parcours doit descendre récursivement : depuis FastAPI 0.141,
    `include_router()` n'aplatit plus les routes dans `app.routes`, il y dépose
    un `_IncludedRouter` qui garde une référence via `original_router`. Une
    lecture directe de `app.routes` ne voit donc plus AUCUNE route de routeur
    inclus — soit, ici, tout `/admin/*`.

    `requirements.txt` déclare `fastapi>=0.115.0,<1.0.0` : les deux formes
    coexistent dans la plage supportée, et ce parcours les couvre toutes deux.
    """
    for route in getattr(router, "routes", []) or []:
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from _iter_app_routes(original)
            continue
        path = getattr(route, "path", None)
        if path is None:  # Mount sans chemin propre
            continue
        for method in getattr(route, "methods", None) or ():
            yield (method, path)


def test_les_routes_exercees_existent_reellement():
    import main  # noqa: PLC0415 - import tardif : conftest prépare l'environnement

    exercised = {
        ("GET", "/health"),
        ("GET", "/ready"),
        ("GET", "/admin/models"),
        ("POST", "/admin/users"),
        ("POST", "/admin/users/{username}/keys"),
        ("POST", "/admin/models/{model_id}/load"),
        ("POST", "/admin/models/{model_id}/unload"),
        ("GET", "/admin/usage"),
        ("DELETE", "/admin/keys/{key_prefix}"),
        ("DELETE", "/admin/users/{username}"),
        ("POST", "/v1/chat/completions"),
    }
    available = set(_iter_app_routes(main.app))
    assert exercised <= available, f"routes disparues : {sorted(exercised - available)}"


def test_le_parcours_des_routes_voit_les_routeurs_inclus():
    """
    Garde-fou du garde-fou.

    Si le parcours ci-dessus cessait de descendre dans les routeurs inclus, le
    test précédent deviendrait vide de sens sur une version récente de FastAPI :
    il ne verrait plus que les routes déclarées directement sur l'application.
    On vérifie donc explicitement qu'au moins une route de routeur inclus est
    atteinte, pour `/admin/*` comme pour les métriques.
    """
    import main  # noqa: PLC0415

    paths = {path for _, path in _iter_app_routes(main.app)}
    assert "/admin/status" in paths
    assert "/admin/metrics/prometheus" in paths


def test_les_champs_consommes_par_la_recette_existent():
    import readiness  # noqa: PLC0415
    import schemas  # noqa: PLC0415

    assert {"api_key", "key_prefix"} <= set(schemas.KeyCreateResponse.model_fields)
    assert {"id", "enabled", "vram_gb"} <= set(schemas.ModelStatusResponse.model_fields)
    assert {"model", "total_tokens"} <= set(schemas.UsageEntry.model_fields)
    assert {"username", "notes"} <= set(schemas.UserCreate.model_fields)

    # `/ready` doit continuer d'exposer `level` et, en échec, `reason`.
    failing = readiness.ReadinessReport(
        mode="local",
        checks=(readiness.CheckResult("model_files", "fail", "model_file_missing", "x"),),
        models_ready=(),
        vram_available_gb=0.0,
    )
    body = failing.public_body()
    assert body["level"] == "none"
    assert body["reason"] == "model_file_missing"


def test_update_installe_la_recette_a_cote_du_service():
    """Un opérateur en incident doit pouvoir la relancer sans le checkout Git."""
    source = UPDATE_SCRIPT.read_text(encoding="utf-8")
    assert 'source "$SCRIPT_DIR/deploy/code-layout-lib.sh"' in source
    assert 'deploy_sync_gateway_operational_files "$SCRIPT_DIR" "$INSTALL_DIR"' in source
