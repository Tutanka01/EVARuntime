"""
Régression de l'audit de sécurité du 30 octobre 2026.

Chaque test de ce fichier correspond à un défaut vérifié dans le code réel, avec
le comportement fautif reproduit avant correction. Le nom du test dit ce qui
était cassé, pas seulement ce qui doit marcher — c'est ce qui permet de savoir,
dans deux ans, si le test protège encore quelque chose.

Les identifiants `SEC-A-01`, `DP-001`, `DP-002`… renvoient au rapport d'audit
(`docs/audits/2026-10-audit-securite.md`).
"""
from __future__ import annotations

import asyncio
import logging
import sys
from collections import deque
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import database as db
import main
import proxy
import server_manager
from config import settings
from server_manager import DRAIN_MAX_LINE_BYTES, build_child_env


# ── Fixtures (mêmes formes que test_admin_routes.py) ──────────────────────────

@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.admin_secret}"}


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Base SQLite jetable sur fichier : `:memory:` ne persiste pas d'un appel à l'autre."""
    db_file = tmp_path / "audit_securite.db"
    monkeypatch.setattr(settings, "db_path", db_file)
    asyncio.run(db.init_db())
    return db_file


# ── SEC-A-01 : révocation par joker ──────────────────────────────────────────

def test_revoquer_avec_le_joker_pourcent_ne_coupe_pas_toutes_les_cles(
    client, admin_headers, temp_db,
):
    """
    Défaut corrigé : `revoke_key` faisait `WHERE key_prefix LIKE ?`, donc un
    préfixe reçu du chemin d'URL était interprété comme un MOTIF. `%25` dans
    l'URL se décode en `%`, `LIKE '%%'` ne filtre plus rien, et une seule
    requête révoquait les clés actives de TOUS les comptes — en répondant 200.

    Le contrôle porte sur deux utilisateurs distincts : c'est la propagation à
    un compte qui n'était pas visé qui constitue la faille.
    """
    alice = asyncio.run(db.create_user(username="alice-audit"))
    bob = asyncio.run(db.create_user(username="bob-audit"))
    _, cle_alice = asyncio.run(db.create_api_key(alice["id"], name="alice"))
    _, cle_bob = asyncio.run(db.create_api_key(bob["id"], name="bob"))

    response = client.delete("/admin/keys/%25", headers=admin_headers)

    assert response.status_code == 404, response.text
    cles_alice = asyncio.run(db.list_keys_for_user(alice["id"]))
    cles_bob = asyncio.run(db.list_keys_for_user(bob["id"]))
    assert cles_alice[0]["is_active"] == 1, "la clé d'alice a été révoquée par un joker"
    assert cles_bob[0]["is_active"] == 1, "la clé de bob a été révoquée par un joker"
    assert cle_alice["key_prefix"] != cle_bob["key_prefix"]


def test_revoquer_un_prefixe_exact_ne_touche_que_sa_cle(
    client, admin_headers, temp_db,
):
    """
    Contrôle positif du test précédent : la révocation légitime fonctionne
    toujours. Sans lui, une route devenue inerte passerait le test du joker.
    """
    alice = asyncio.run(db.create_user(username="alice-exact"))
    bob = asyncio.run(db.create_user(username="bob-exact"))
    _, cle_alice = asyncio.run(db.create_api_key(alice["id"], name="alice"))
    _, cle_bob = asyncio.run(db.create_api_key(bob["id"], name="bob"))

    response = client.delete(
        f"/admin/keys/{cle_alice['key_prefix']}", headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    assert asyncio.run(db.list_keys_for_user(alice["id"]))[0]["is_active"] == 0
    assert asyncio.run(db.list_keys_for_user(bob["id"]))[0]["is_active"] == 1


def test_le_joker_souligne_ne_revoque_pas_une_cle_voisine(client, admin_headers, temp_db):
    """
    `_` est aussi un joker LIKE (un caractère quelconque). Un préfixe saisi
    `llmgw-a_c` révoquait donc des clés qu'il ne désignait pas.
    """
    alice = asyncio.run(db.create_user(username="alice-underscore"))
    _, cle_alice = asyncio.run(db.create_api_key(alice["id"], name="alice"))
    prefixe = cle_alice["key_prefix"]
    # `_` est un joker LIKE (un caractère quelconque). On remplace un caractère
    # qui n'est PAS déjà `_` : sinon le motif serait identique au vrai préfixe et
    # le test deviendrait vert sous l'ancien code comme sous le nouveau. Sous
    # `LIKE`, ce motif correspondait à cette clé ; sous égalité, il n'existe pas.
    index = next(i for i, c in enumerate(prefixe) if c != "_")
    motif = prefixe[:index] + "_" + prefixe[index + 1:]
    assert motif != prefixe

    response = client.delete(f"/admin/keys/{motif}", headers=admin_headers)

    assert response.status_code == 404, response.text
    assert asyncio.run(db.list_keys_for_user(alice["id"]))[0]["is_active"] == 1


# ── DP-001 : secrets hérités par le sous-processus llama-server ──────────────

def test_le_sous_processus_llama_server_n_herite_d_aucun_secret(monkeypatch):
    """
    Défaut corrigé : `env = os.environ.copy()` transmettait à llama-server
    ADMIN_SECRET, AGENT_SECRET et INTERNAL_API_KEY. Le backend est un binaire
    C++ qui parse des GGUF non fiables : une compromission de son parsing
    donnait /proc/self/environ, donc le contrôle total de /admin et de tous les
    node-agents. L'environnement transmis est désormais une allowlist.
    """
    monkeypatch.setenv("ADMIN_SECRET", "secret-admin-qui-ne-doit-pas-fuiter")
    monkeypatch.setenv("AGENT_SECRET", "secret-agent-qui-ne-doit-pas-fuiter")
    monkeypatch.setenv("INTERNAL_API_KEY", "cle-interne-qui-ne-doit-pas-fuiter")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/llama/lib")

    env = build_child_env()

    assert "ADMIN_SECRET" not in env
    assert "AGENT_SECRET" not in env
    assert not any(
        "fuiter" in valeur for valeur in env.values()
    ), f"une valeur secrète traverse encore l'environnement enfant : {sorted(env)}"

    # Contrôle positif : l'environnement n'est pas vide pour autant. Sans lui,
    # une allowlist qui ne transmettrait plus rien casserait llama-server en
    # silence et ce test resterait vert.
    assert env["CUDA_VISIBLE_DEVICES"] == settings.cuda_visible_devices
    assert "LLAMA_API_KEY" in env
    assert env["LD_LIBRARY_PATH"] == "/opt/llama/lib"


# ── DP-002 : le data-plane suivait les proxies d'environnement ───────────────

def test_le_client_d_inference_ignore_les_proxies_d_environnement():
    """
    Défaut corrigé : sans `trust_env=False`, httpx lit HTTP_PROXY/ALL_PROXY et y
    route AUSSI les URL loopback. Sur un hôte avec un proxy d'environnement,
    chaque prompt partait en clair vers ce proxy avec l'en-tête Authorization
    qui ouvre tous les llama-server.
    """
    client = proxy._build_http_client()
    try:
        assert client._trust_env is False
    finally:
        asyncio.run(client.aclose())


# ── DP-003 : bornes de génération non validées ──────────────────────────────

@pytest.mark.parametrize("corps", [
    {"n_predict": -1},
    {"max_tokens": -1},
    {"max_completion_tokens": -5},
    {"max_tokens": True},
    {"max_tokens": "4096"},
    {"n_predict": 1.5},
])
def test_une_borne_de_generation_invalide_est_refusee(corps):
    """
    Défaut corrigé : le corps était forwardé tel quel. `n_predict: -1` signifie
    « jusqu'à remplir le contexte » côté llama.cpp ; un booléen devenait 1 token
    (`bool` est une sous-classe de `int`) ; une chaîne partait au backend qui
    décidait seul de sa validité.
    """
    refus = proxy._validate_generation_bounds(corps)
    assert refus is not None, f"{corps} aurait dû être refusé"
    assert refus.status_code == 400


def test_une_borne_de_generation_valide_ou_absente_passe(monkeypatch):
    """
    Contrôle positif : la validation ne casse pas les clients légitimes.
    Omettre le champ reste la façon de demander le défaut du backend.
    """
    monkeypatch.setattr(settings, "max_new_tokens_per_request", 0)

    assert proxy._validate_generation_bounds({}) is None
    assert proxy._validate_generation_bounds({"max_tokens": 0}) is None
    assert proxy._validate_generation_bounds({"max_tokens": 4096}) is None
    assert proxy._validate_generation_bounds({"n_predict": None}) is None


def test_la_borne_configurable_refuse_au_dela(monkeypatch):
    """MAX_NEW_TOKENS_PER_REQUEST borne la demande sans jamais écrêter en silence."""
    monkeypatch.setattr(settings, "max_new_tokens_per_request", 1024)

    assert proxy._validate_generation_bounds({"max_tokens": 1024}) is None
    refus = proxy._validate_generation_bounds({"max_tokens": 1025})
    assert refus is not None and refus.status_code == 400


# ── SEC-A-03 : l'anonymisation n'effaçait pas le fichier ────────────────────

def test_le_nom_efface_ne_survit_pas_dans_le_fichier_sqlite(temp_db):
    """
    Défaut corrigé : sans `PRAGMA secure_delete`, SQLite laissait en clair la
    cellule de l'index `username TEXT UNIQUE`. `strings gateway.db` retrouvait
    le nom que `DELETE /admin/users/{username}` venait de déclarer irrécupérable,
    et toute sauvegarde produite APRÈS l'anonymisation le contenait encore.

    Le WAL est inclus dans la recherche : une écriture récente peut y vivre
    encore, et c'est aussi une copie du fichier.
    """
    nom = "charles.marcheur.731"
    utilisateur = asyncio.run(db.create_user(
        username=nom, email="charles@example.org", notes="bureau 204",
    ))
    asyncio.run(db.create_api_key(utilisateur["id"], name="poste-charles"))

    resultat = asyncio.run(db.anonymize_user(utilisateur["id"]))
    assert resultat is not None and resultat["already_anonymized"] is False

    octets = bytearray(temp_db.read_bytes())
    for suffixe in ("-wal", "-shm"):
        annexe = Path(f"{temp_db}{suffixe}")
        if annexe.exists():
            octets += annexe.read_bytes()

    # Contrôle positif : le contrôle cherche vraiment quelque chose. Le
    # pseudonyme doit, lui, être présent — sinon un fichier vide passerait.
    assert b"anon-" in bytes(octets) or str(utilisateur["id"]).encode() in bytes(octets)
    assert nom.encode() not in bytes(octets), (
        "le nom anonymisé est encore lisible dans le fichier de base"
    )
    assert b"bureau 204" not in bytes(octets), "les notes anonymisées sont encore lisibles"


# ── DP-005 : une ligne de journal démesurée tuait le drainage des pipes ─────

class _ServeurFactice:
    """
    Ne porte que l'état lu par `_drain_stream` / `_append_tail` : le modèle et la
    queue de diagnostic. `_append_tail` est EMPRUNTÉE à `ServerManager` — le test
    doit exercer le code livré, pas une copie qui pourrait diverger.
    """

    _append_tail = server_manager.ServerManager._append_tail

    def __init__(self, model_id: str = "drain-test") -> None:
        self._stderr_tail: deque[str] = deque(maxlen=30)
        self._model = type("_M", (), {"id": model_id})()


def _drainer(programme: str) -> _ServeurFactice:
    """Lance un vrai sous-processus, draine son stdout, rend l'état accumulé."""

    async def scenario() -> _ServeurFactice:
        faux = _ServeurFactice()
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", programme,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        drain = asyncio.create_task(
            server_manager.ServerManager._drain_stream(faux, process.stdout)
        )
        # Si le drainage meurt sur la ligne longue, le pipe ne se vide plus et
        # ce wait() ne rend jamais : c'est exactement le blocage observé.
        await asyncio.wait_for(process.wait(), timeout=20)
        await asyncio.wait_for(drain, timeout=5)
        return faux

    return asyncio.run(scenario())


def test_le_drainage_survit_a_une_ligne_demesuree_et_laisse_le_reap_aboutir():
    """
    Défaut corrigé : `_drain_stream` utilisait `readline()`, qui lève ValueError
    au-delà de la limite interne du StreamReader (64 Kio), et avalait
    l'exception. La tâche de drainage mourait en silence, le pipe se remplissait,
    llama-server se bloquait sur son écriture, et `process.wait()` — qui ne rend
    qu'une fois les pipes refermés — ne répondait plus jamais : le modèle restait
    en UNLOADING et son éviction LRU bloquait les chargements suivants.

    Vrai sous-processus, vraie méthode : c'est la seule façon de prouver que le
    pipe ne se remplit plus.
    """
    faux = _drainer(
        "import sys\n"
        "sys.stdout.write('x' * 300000 + '\\n')\n"
        "sys.stdout.write('apres-la-ligne-longue\\n')\n"
        "sys.stdout.flush()\n"
    )

    assert "apres-la-ligne-longue" in list(faux._stderr_tail)[-2:], (
        "le drainage s'est arrêté sur la ligne démesurée : le pipe n'est plus vidé"
    )


def test_une_ligne_demesuree_est_tronquee_dans_le_diagnostic():
    """
    La borne `DRAIN_MAX_LINE_BYTES` s'applique au contenu conservé : 300 Kio
    recopiés dans un deque de 30 entrées feraient 9 Mo de diagnostic pour un
    message utile d'une ligne.
    """
    faux = _ServeurFactice("drain-borne")
    faux._append_tail(b"y" * (DRAIN_MAX_LINE_BYTES * 3), logging.getLogger("test"))

    assert len(faux._stderr_tail) == 1
    conservee = faux._stderr_tail[0]
    assert "tronqué" in conservee
    assert len(conservee) < DRAIN_MAX_LINE_BYTES * 2
