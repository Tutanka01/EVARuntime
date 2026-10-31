"""
CLU-104 — la sonde de préflight contre de vraies poignées de main TLS.

Le test de câblage de `test_preflight.py` remplace le contexte SSL par un double :
il ne pouvait donc pas voir qu'un certificat émis par une AC échouait sur
Python < 3.13, ce qui envoyait `update-agent.sh` en rollback sur un nœud sain.
Ici, les certificats et le serveur sont réels ; seul le service health est réduit
à une réponse JSON.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

import preflight
from config import AgentSettings

pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="binaire openssl requis"
)

_SECRET = "a" * 32
_LEAF_EXTENSIONS = (
    "basicConstraints=critical,CA:FALSE\n"
    "keyUsage=critical,digitalSignature,keyEncipherment\n"
    "extendedKeyUsage=serverAuth\n"
    "subjectKeyIdentifier=hash\n"
    "authorityKeyIdentifier=keyid,issuer\n"
    "subjectAltName=DNS:gpu-node-a\n"
)
_CA_EXTENSIONS = (
    "basicConstraints=critical,CA:TRUE\n"
    "keyUsage=critical,keyCertSign,cRLSign\n"
    "subjectKeyIdentifier=hash\n"
    "authorityKeyIdentifier=keyid,issuer\n"
)


def _openssl(*args: str | Path) -> None:
    subprocess.run(["openssl", *map(str, args)], check=True, capture_output=True)


def _self_signed(directory: Path, name: str) -> tuple[Path, Path]:
    """Même commande que `install-agent.sh`."""
    cert, key = directory / f"{name}.crt", directory / f"{name}.key"
    _openssl(
        "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "30", "-nodes",
        "-keyout", key, "-out", cert, "-subj", "/CN=gpu-node-a",
        "-addext", "subjectAltName=DNS:gpu-node-a",
    )
    return cert, key


def _self_signed_sans_key_identifier(directory: Path, name: str) -> tuple[Path, Path]:
    """Forme produite par d'anciens outils : refusée par le mode strict de Python 3.13+."""
    cert, key, config = (directory / f"{name}.{ext}" for ext in ("crt", "key", "cnf"))
    config.write_text(
        "[req]\ndistinguished_name=dn\nx509_extensions=v3\nprompt=no\n"
        "[dn]\nCN=gpu-node-a\n"
        "[v3]\nbasicConstraints=CA:TRUE\nsubjectAltName=DNS:gpu-node-a\n",
        encoding="utf-8",
    )
    _openssl(
        "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "30", "-nodes",
        "-keyout", key, "-out", cert, "-config", config,
    )
    return cert, key


def _issued(
    directory: Path, name: str, issuer: tuple[Path, Path], extensions: str,
) -> tuple[Path, Path]:
    cert, key, csr = (directory / f"{name}.{ext}" for ext in ("crt", "key", "csr"))
    ext_file = directory / f"{name}.ext"
    ext_file.write_text(extensions, encoding="utf-8")
    _openssl(
        "req", "-newkey", "rsa:2048", "-nodes", "-subj", f"/CN={name}",
        "-keyout", key, "-out", csr,
    )
    _openssl(
        "x509", "-req", "-in", csr, "-CA", issuer[0], "-CAkey", issuer[1],
        "-CAcreateserial", "-days", "30", "-sha256", "-extfile", ext_file, "-out", cert,
    )
    return cert, key


def _concat(target: Path, *parts: Path) -> Path:
    target.write_text("".join(p.read_text(encoding="utf-8") for p in parts), encoding="utf-8")
    return target


@pytest.fixture
def pki(tmp_path) -> dict[str, tuple[Path, Path]]:
    """Formes de certificat qu'un AGENT_TLS_CERT peut prendre sur un nœud."""
    root = _self_signed(tmp_path, "root")
    intermediate = _issued(tmp_path, "intermediate", root, _CA_EXTENSIONS)
    leaf = _issued(tmp_path, "leaf", root, _LEAF_EXTENSIONS)
    chained = _issued(tmp_path, "chained", intermediate, _LEAF_EXTENSIONS)
    return {
        "auto-signe": _self_signed(tmp_path, "self"),
        "auto-signe-sans-identifiant": _self_signed_sans_key_identifier(tmp_path, "legacy"),
        "ac-feuille-seule": leaf,
        "ac-fullchain-avec-racine": (_concat(tmp_path / "full.crt", leaf[0], root[0]), leaf[1]),
        "ac-fullchain-sans-racine": (
            _concat(tmp_path / "partial.crt", chained[0], intermediate[0]), chained[1],
        ),
        "autre-serveur": _self_signed(tmp_path, "other"),
    }


class _Health(BaseHTTPRequestHandler):
    authorizations: list[str | None] = []

    def do_GET(self) -> None:  # noqa: N802 — nom imposé par http.server
        type(self).authorizations.append(self.headers.get("Authorization"))
        payload = json.dumps({"status": "ok", "loaded_model_ids": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:
        return None


@pytest.fixture
def serve():
    """Démarre un serveur HTTPS réel sur le loopback avec le certificat donné."""
    servers: list[ThreadingHTTPServer] = []

    def _start(cert: Path, key: Path) -> int:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Health)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        # Une poignée de main refusée par le client ne doit pas bruiter le test.
        server.handle_error = lambda *args: None
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server.server_address[1]

    _Health.authorizations = []
    yield _start
    for server in servers:
        server.shutdown()
        server.server_close()


def _settings(cert: Path, key: Path, port: int) -> AgentSettings:
    key.chmod(0o640)
    return AgentSettings(
        _env_file=None,
        agent_secret=_SECRET,
        internal_api_key="b" * 32,
        agent_host="127.0.0.1",
        agent_port=port,
        agent_tls_cert=cert,
        agent_tls_key=key,
        allowed_model_dirs="/models",
    )


@pytest.mark.parametrize("forme", [
    "auto-signe",
    "auto-signe-sans-identifiant",
    "ac-feuille-seule",
    "ac-fullchain-avec-racine",
    "ac-fullchain-sans-racine",
])
def test_la_sonde_accepte_toute_forme_de_certificat_du_noeud(pki, serve, forme):
    cert, key = pki[forme]
    port = serve(cert, key)

    asyncio.run(preflight.check_local_health(_settings(cert, key, port), 5.0))

    assert _Health.authorizations == [f"Bearer {_SECRET}"]


def test_la_sonde_refuse_un_autre_serveur_et_ne_lui_livre_pas_le_secret(pki, serve):
    """
    Contrôle négatif : un processus qui occupe le port avec un autre certificat ne
    doit recevoir ni réponse acceptée ni AGENT_SECRET. Sans lui, une sonde
    redevenue `verify=False` passerait tous les cas ci-dessus.
    """
    cert, key = pki["auto-signe"]
    port = serve(*pki["autre-serveur"])

    with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
        asyncio.run(preflight.check_local_health(_settings(cert, key, port), 5.0))

    assert _Health.authorizations == []
