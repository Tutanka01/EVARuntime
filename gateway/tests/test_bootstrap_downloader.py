"""
AUT-006 — régressions du téléchargement sûr (`bootstrap/downloader.py`).

Ce module est le seul du parcours M2 qui écrit des dizaines de gigaoctets sur
l'hôte à partir d'une source distante. Les tests verrouillent donc six familles
d'invariants, dans l'ordre de ce qu'une régression coûterait :

1. **le renommage n'arrive jamais avant la preuve** — aucun fichier ne porte son
   nom définitif sans avoir été confronté au SHA-256 du catalogue. C'est
   l'invariant qui empêche un `llama-server` de charger un GGUF tronqué ;
2. **la reprise est prouvée ou refusée** — reprendre sur des octets dont on ne
   peut pas établir la provenance est pire que recommencer, et un serveur qui
   ignore le `Range` ne doit pas produire un fichier concaténé ;
3. **rien n'est réparé en silence** — un fichier présent à la mauvaise empreinte
   est un refus, jamais un écrasement ;
4. **l'ensemble est indivisible** — un ensemble partiellement téléchargé n'obtient
   pas de manifeste et n'est jamais déclaré utilisable ;
5. **la licence ne s'invente pas** — une acceptation vient de l'opérateur ou
   n'existe pas ; le caractère permissif d'une licence n'en tient jamais lieu ;
6. **le jeton ne sort nulle part** — ni journal, ni erreur, ni preuve, ni URL,
   ni en-tête envoyé à un hôte tiers.

Aucun test ne touche le réseau : le transport est injecté. Aucun test n'écrit
hors de `tmp_path` : le contexte d'exécution n'autorise que cette racine, et
`execution.ensure_within_allowed_roots` refuse le reste.

Chaque test d'ABSENCE porte son contrôle positif — un test qui affirme « pas de
jeton », « pas de requête » ou « pas de fichier » sans prouver qu'il saurait en
voir un passerait au vert le jour où l'assertion deviendrait inerte.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import socket
from pathlib import Path

import pytest
import yaml

from bootstrap import catalog as catalog_mod
from bootstrap import downloader, execution, schema

# ── Données synthétiques ──────────────────────────────────────────────────────

REVISION = "0123456789abcdef0123456789abcdef01234567"
OTHER_REVISION = "fedcba9876543210fedcba9876543210fedcba98"

WEIGHTS = b"GGUF-poids-" + b"W" * 300
MMPROJ = b"GGUF-mmproj-" + b"M" * 120

WEIGHTS_NAME = "modele-q4_k_m.gguf"
MMPROJ_NAME = "mmproj-modele.gguf"

REPO_ID = "organisation/depot-de-test"
ENTRY_ID = "modele-test"

# Jeton volontairement SANS le préfixe `hf_` : c'est le cas que le filet de
# `schema.find_secret_leaks()` ne voit pas, et que seul le remplacement littéral
# de `_scrub()` attrape. Un jeton d'entreprise ressemble à ça.
OPAQUE_TOKEN = "jeton-opaque-dentreprise-9f3a2b71"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_entry(name: str, role: str, data: bytes, *, sha: str | None = "auto",
                size: int | None = -1) -> dict:
    return {
        "name": name,
        "role": role,
        "sha256": _sha(data) if sha == "auto" else sha,
        "size_bytes": len(data) if size == -1 else size,
    }


def write_catalog(
    tmp_path: Path,
    *,
    files: list[dict] | None = None,
    revision: str | None = REVISION,
    acceptance_required: bool = True,
    gated: bool = False,
    base_license: str = "apache-2.0",
    fine_tune_license: str = "apache-2.0",
    requires_mmproj: bool = True,
    downloader_name: str = "huggingface_hub",
    redistribution_allowed: bool = True,
) -> catalog_mod.Catalog:
    """
    Fabrique un catalogue RÉEL, relu par `catalog.load_catalog()`.

    Passer par le vrai chargeur plutôt que par des dataclasses construites à la
    main est délibéré : les contraintes du catalogue (ensemble indivisible,
    licence identifiée, épinglage) font partie de ce que le téléchargeur
    s'appuie dessus pour être fail-closed. Les court-circuiter ici testerait un
    montage qui n'existe pas en production.
    """
    if files is None:
        files = [
            _file_entry(WEIGHTS_NAME, "weights", WEIGHTS),
            _file_entry(MMPROJ_NAME, "mmproj", MMPROJ),
        ]
    document = {
        "catalog_version": 1,
        "downloader": {"name": downloader_name, "license_id": "apache-2.0"},
        "models": [{
            "id": ENTRY_ID,
            "family": "test",
            "display_name": "Modèle de test",
            "description": "Entrée synthétique pour les tests d'AUT-006.",
            "use_cases": ["chat"],
            "source": {
                "provider": "huggingface",
                "repo_id": REPO_ID,
                "repo_url": f"https://huggingface.co/{REPO_ID}",
                "revision": revision,
                "revision_recorded_on": "2026-08-01",
                "files": files,
            },
            "license": {
                "base_model": {"id": base_license},
                "fine_tune": {"id": fine_tune_license},
                "usage_terms": None,
                "gated": gated,
                "redistribution_allowed": redistribution_allowed,
                "operator_acceptance_required": acceptance_required,
                "notes": None,
            },
            "runtime": {
                "min_llama_build": 0,
                "capabilities": ["text_generation"],
                "requires_mmproj": requires_mmproj,
                "defaults": {
                    "ctx_size": 4096, "parallel": 1,
                    "cache_type_k": "f16", "cache_type_v": "f16",
                },
            },
            "resources": {
                "disk_gb": 1.0, "initial_vram_gb": 1.0, "initial_ram_gb": 1.0,
                "estimation_basis": "gguf_header",
            },
        }],
    }
    target = tmp_path / "catalog.yaml"
    target.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    return catalog_mod.load_catalog(target)


# ── Transport simulé ──────────────────────────────────────────────────────────

class FakeResponse:
    """Réponse simulée. Rend le corps par petits blocs, comme un vrai flux."""

    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = {k.lower(): v for k, v in headers.items()}
        self._body = body
        self.closed = False

    async def chunks(self):
        for index in range(0, len(self._body), 7):
            yield self._body[index:index + 7]

    async def close(self) -> None:
        self.closed = True


class FakeTransport:
    """
    Transport injectable. N'ouvre aucune socket et enregistre tout ce qu'on lui demande.

    Les défauts qu'il sait simuler sont exactement ceux que §8 oblige à traiter :
    un serveur qui ignore `Range`, un `Content-Range` menteur, un corps corrompu,
    un corps tronqué, une redirection vers un CDN tiers, un refus d'accès.
    """

    def __init__(
        self,
        bodies: dict[str, bytes] | None = None,
        *,
        status: dict[str, int] | None = None,
        ignore_range: bool = False,
        corrupt: set[str] | None = None,
        truncate: dict[str, int] | None = None,
        redirect_to: str | None = None,
        content_range_override: str | None = None,
    ) -> None:
        self.bodies = bodies if bodies is not None else {
            WEIGHTS_NAME: WEIGHTS, MMPROJ_NAME: MMPROJ,
        }
        self.status = status or {}
        self.ignore_range = ignore_range
        self.corrupt = corrupt or set()
        self.truncate = truncate or {}
        self.redirect_to = redirect_to
        self.content_range_override = content_range_override
        self.requests: list[tuple[str, dict[str, str]]] = []
        self._redirected: set[str] = set()

    def headers_for(self, filename: str) -> list[dict[str, str]]:
        return [h for url, h in self.requests if url.endswith(filename)]

    async def open(self, url: str, headers):
        self.requests.append((url, dict(headers)))
        name = url.rsplit("/", 1)[-1]

        if self.redirect_to and url not in self._redirected:
            self._redirected.add(url)
            return FakeResponse(302, {"Location": f"{self.redirect_to}/{name}"}, b"")

        forced = self.status.get(name)
        if forced is not None:
            return FakeResponse(forced, {}, b"")

        body = self.bodies.get(name)
        if body is None:
            return FakeResponse(404, {}, b"")
        if name in self.corrupt:
            body = bytes(len(body))

        raw_range = headers.get("Range")
        if raw_range and not self.ignore_range:
            start = int(raw_range.split("=", 1)[1].split("-", 1)[0])
            segment = body[start:]
            content_range = (
                self.content_range_override
                if self.content_range_override is not None
                else f"bytes {start}-{len(body) - 1}/{len(body)}"
            )
            segment = self._maybe_truncate(name, segment)
            return FakeResponse(206, {"Content-Range": content_range}, segment)

        return FakeResponse(200, {"Content-Length": str(len(body))}, self._maybe_truncate(name, body))

    def _maybe_truncate(self, name: str, payload: bytes) -> bytes:
        keep = self.truncate.get(name)
        return payload if keep is None else payload[:keep]


class _RawHttpsResponse:
    def __init__(self, *, status: int = 200, body: bytes = b"ok") -> None:
        self.status = status
        self.headers: dict[str, str] = {}
        self._body = body
        self.closed = False

    def read(self, size: int) -> bytes:
        block, self._body = self._body[:size], self._body[size:]
        return block

    def close(self) -> None:
        self.closed = True


class _PinnedConnection:
    def __init__(self, response: _RawHttpsResponse) -> None:
        self.response = response
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.closed = False

    def request(self, method: str, url: str, *, headers: dict[str, str]) -> None:
        self.requests.append((method, url, headers))

    def getresponse(self) -> _RawHttpsResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


class _PinnedConnectionFactory:
    def __init__(self, responses: list[_RawHttpsResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, int, tuple[downloader.public_https.AddressInfo, ...], float]] = []
        self.connections: list[_PinnedConnection] = []

    def __call__(self, host, port, addresses, timeout):
        self.calls.append((host, port, tuple(addresses), timeout))
        connection = _PinnedConnection(self.responses.pop(0))
        self.connections.append(connection)
        return connection


def _address(ip: str) -> downloader.public_https.AddressInfo:
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443))


# ── Montage ───────────────────────────────────────────────────────────────────

def make_context(tmp_path: Path, *, mode=execution.ExecutionMode.APPLY, log=None):
    return execution.ExecutionContext(
        mode,
        allowed_roots=(tmp_path,),
        now=lambda: "2026-08-01T12:00:00Z",
        log=log if log is not None else (lambda message: None),
    )


def make_config(
    tmp_path: Path,
    catalog: catalog_mod.Catalog,
    *,
    transport: FakeTransport | None = None,
    free_bytes: int = 10 ** 12,
    token: str | None = None,
    acceptances: tuple[downloader.LicenseAcceptance, ...] = (),
    margin_min: int = 0,
) -> downloader.DownloadConfig:
    models = tmp_path / "models"
    models.mkdir(exist_ok=True)
    return downloader.DownloadConfig(
        catalog=catalog,
        models_dir=models,
        transport=transport if transport is not None else FakeTransport(),
        token_provider=lambda: token,
        disk_free=lambda path: free_bytes,
        acceptances=acceptances,
        chunk_bytes=64,
        disk_margin_min_bytes=margin_min,
    )


def acceptance(**overrides) -> downloader.LicenseAcceptance:
    base = {
        "entry_id": ENTRY_ID,
        "base_model_license": "apache-2.0",
        "fine_tune_license": "apache-2.0",
        "operator_reference": "CHG-2026-0142",
        "accepted_at": "2026-08-01T11:00:00Z",
        "accepted": True,
    }
    base.update(overrides)
    return downloader.LicenseAcceptance(**base)


def write_stored_acceptance(config: downloader.DownloadConfig, **overrides) -> None:
    document = acceptance().to_dict()
    document.update(overrides)
    downloader.acceptance_path(config.models_dir, ENTRY_ID).write_text(
        json.dumps(document), encoding="utf-8"
    )


def step(action: str, target: str, order: int = 1) -> schema.PlanStep:
    return schema.PlanStep(
        order=order, action=action, target=target,
        detail="étape synthétique de test", requires_root=False, reversible=True,
    )


def download_step(order: int = 1, revision: str = REVISION) -> schema.PlanStep:
    return step(schema.ACTION_DOWNLOAD_MODEL, f"{REPO_ID}@{revision}", order)


def verify_step(order: int = 1) -> schema.PlanStep:
    return step(schema.ACTION_VERIFY_ARTIFACT, ENTRY_ID, order)


def accept_step(order: int = 1, licence: str = "apache-2.0") -> schema.PlanStep:
    return step(schema.ACTION_ACCEPT_LICENSE, f"{ENTRY_ID} — {licence}", order)


def run(executor, plan_step, context):
    return asyncio.run(executor(plan_step, context))


def run_download(tmp_path, config, context=None, order=1, revision=REVISION):
    context = context or make_context(tmp_path)
    return run(downloader.make_executors(config)[schema.ACTION_DOWNLOAD_MODEL],
               download_step(order, revision), context)


def run_verify(tmp_path, config, context=None):
    context = context or make_context(tmp_path)
    return run(downloader.make_executors(config)[schema.ACTION_VERIFY_ARTIFACT],
               verify_step(), context)


def run_accept(tmp_path, config, context=None, licence: str = "apache-2.0"):
    context = context or make_context(tmp_path)
    return run(downloader.make_executors(config)[schema.ACTION_ACCEPT_LICENSE],
               accept_step(licence=licence), context)


def granted(tmp_path, **kwargs):
    """Catalogue + config avec l'acceptation de licence déjà fournie."""
    catalog = write_catalog(tmp_path)
    return catalog, make_config(tmp_path, catalog, acceptances=(acceptance(),), **kwargs)


# ── 1. URL, révision figée, liste exacte de fichiers ──────────────────────────

def test_url_utilise_la_revision_epinglee_jamais_une_branche(tmp_path):
    entry = write_catalog(tmp_path).entries[0]
    url = downloader.build_file_url(entry, WEIGHTS_NAME, endpoint="https://exemple.test")
    assert url == f"https://exemple.test/{REPO_ID}/resolve/{REVISION}/{WEIGHTS_NAME}"
    assert "/main/" not in url


def test_url_refuse_une_revision_qui_nest_pas_un_commit(tmp_path):
    entry = write_catalog(tmp_path).entries[0]
    mobile = dataclasses.replace(entry, revision="main")
    with pytest.raises(downloader.DownloadError, match="révision"):
        downloader.build_file_url(mobile, WEIGHTS_NAME, endpoint="https://exemple.test")


def test_url_refuse_un_fichier_absent_de_la_liste_exacte(tmp_path):
    entry = write_catalog(tmp_path).entries[0]
    # Contrôle positif : le fichier listé, lui, passe.
    downloader.build_file_url(entry, WEIGHTS_NAME, endpoint="https://exemple.test")
    with pytest.raises(downloader.DownloadError, match="liste exacte"):
        downloader.build_file_url(entry, "config.json", endpoint="https://exemple.test")


def test_chemin_final_refuse_une_evasion_de_repertoire(tmp_path):
    with pytest.raises(downloader.DownloadError, match="hors de"):
        downloader._final_path(tmp_path, "../ailleurs.gguf")


# ── 2. Jeton : portée, expurgation ───────────────────────────────────────────

def test_jeton_envoye_a_lhote_dorigine_et_a_lui_seul():
    origine = downloader.authorization_headers(
        "https://huggingface.co/o/d/resolve/abc/f.gguf",
        origin_host="huggingface.co", token=OPAQUE_TOKEN,
    )
    # Contrôle positif : l'en-tête EXISTE quand l'hôte est le bon, sans quoi
    # l'assertion d'absence ci-dessous serait vraie pour la mauvaise raison.
    assert origine["Authorization"] == f"Bearer {OPAQUE_TOKEN}"

    cdn = downloader.authorization_headers(
        "https://cdn-lfs.example.net/o/d/f.gguf",
        origin_host="huggingface.co", token=OPAQUE_TOKEN,
    )
    assert cdn == {}


def test_jeton_jamais_envoye_en_clair():
    assert downloader.authorization_headers(
        "http://huggingface.co/o/d/f.gguf", origin_host="huggingface.co", token=OPAQUE_TOKEN,
    ) == {}


def test_sans_jeton_aucun_en_tete_dautorisation():
    assert downloader.authorization_headers(
        "https://huggingface.co/o/d/f.gguf", origin_host="huggingface.co", token=None,
    ) == {}


def test_scrub_retire_un_jeton_de_forme_quelconque():
    """
    Le remplacement littéral doit porter SEUL, sans l'aide des motifs de `schema`.

    Un message contenant « Bearer <jeton> » serait de toute façon attrapé par
    `execution.redact_for_log()`. Le cas qui distingue les deux filets est celui
    d'un jeton d'entreprise posé nu dans une phrase : aucun motif ne le
    reconnaît, et seul le remplacement littéral peut le retirer.
    """
    nu = f"échec d'ouverture du descripteur {OPAQUE_TOKEN} sur exemple.test"
    assert schema.find_secret_leaks(nu) == (), "le motif générique le voit déjà : cas mal choisi"
    assert OPAQUE_TOKEN not in downloader._scrub(nu, OPAQUE_TOKEN)

    entete = f"échec de https://exemple.test — en-tête Bearer {OPAQUE_TOKEN}"
    assert OPAQUE_TOKEN not in downloader._scrub(entete, OPAQUE_TOKEN)

    # Contrôle positif : un message sans secret traverse intact, donc les
    # assertions ci-dessus ne passent pas simplement parce que `_scrub` efface tout.
    assert downloader._scrub("téléchargement de modele.gguf", OPAQUE_TOKEN) == (
        "téléchargement de modele.gguf"
    )


def test_une_exception_de_transport_ne_recopie_pas_le_jeton_dans_le_rapport(tmp_path):
    """
    Le transport est injectable, donc arbitraire : son exception peut tout dire.

    Beaucoup de clients HTTP recopient les en-têtes de la requête dans le
    message de leurs erreurs. Si cette exception remontait telle quelle,
    `execute_plan()` la consignerait en n'appliquant que les motifs de `schema`,
    qui ne connaissent pas la valeur d'un jeton d'entreprise.
    """
    class TransportBavard:
        def __init__(self):
            self.requests = []

        async def open(self, url, headers):
            self.requests.append((url, dict(headers)))
            raise RuntimeError(f"connexion impossible (headers={headers})")

    transport = TransportBavard()
    catalog = write_catalog(tmp_path)
    config = downloader.DownloadConfig(
        catalog=catalog, models_dir=tmp_path / "models", transport=transport,
        token_provider=lambda: OPAQUE_TOKEN, disk_free=lambda p: 10 ** 12,
        acceptances=(acceptance(),), chunk_bytes=64, disk_margin_min_bytes=0,
    )
    config.models_dir.mkdir()

    result = run_download(tmp_path, config)

    assert result.status == execution.STEP_FAILED
    # Contrôle positif : le jeton a bien été envoyé, et l'exception le portait —
    # sans quoi ce test serait vert pour la mauvaise raison.
    assert transport.requests[0][1]["Authorization"] == f"Bearer {OPAQUE_TOKEN}"
    assert OPAQUE_TOKEN in str(RuntimeError(f"headers={transport.requests[0][1]}"))

    surfaces = [result.error, json.dumps(result.evidence, ensure_ascii=False), result.summary]
    for surface in surfaces:
        assert OPAQUE_TOKEN not in surface
    # …et le rapport parle quand même du fichier concerné.
    assert WEIGHTS_NAME in json.dumps(result.evidence, ensure_ascii=False)


def test_provider_de_jeton_lit_lenvironnement_pas_argv(monkeypatch):
    for name in downloader.TOKEN_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(downloader, "DEFAULT_TOKEN_FILE", Path("/inexistant/token"))
    assert downloader.default_token_provider() is None

    monkeypatch.setenv("HF_TOKEN", OPAQUE_TOKEN)
    assert downloader.default_token_provider() == OPAQUE_TOKEN


# ── 3. Téléchargement nominal, renommage atomique ────────────────────────────

def test_telechargement_complet_ecrit_verifie_et_renomme(tmp_path):
    _, config = granted(tmp_path)
    result = run_download(tmp_path, config)

    assert result.status == execution.STEP_DONE
    models = config.models_dir
    assert (models / WEIGHTS_NAME).read_bytes() == WEIGHTS
    assert (models / MMPROJ_NAME).read_bytes() == MMPROJ
    # Aucun résidu : ni fichier temporaire, ni preuve de reprise.
    assert list(models.glob("*.part")) == []
    assert list(models.glob("*.part.json")) == []
    assert result.evidence["downloaded_bytes"] == len(WEIGHTS) + len(MMPROJ)


def test_premier_telechargement_ne_demande_aucune_reprise(tmp_path):
    transport = FakeTransport()
    _, config = granted(tmp_path)
    config = dataclasses.replace(config, transport=transport)
    run_download(tmp_path, config)

    envoyes = transport.headers_for(WEIGHTS_NAME)
    assert envoyes, "aucune requête n'a été enregistrée — le test serait inerte"
    assert all("Range" not in h for h in envoyes)


def test_manifeste_de_provenance_repond_a_dou_vient_ce_fichier(tmp_path):
    _, config = granted(tmp_path)
    run_download(tmp_path, config)

    manifest = json.loads(
        downloader.provenance_path(config.models_dir, ENTRY_ID).read_text(encoding="utf-8")
    )
    assert manifest["source"]["repo_id"] == REPO_ID
    assert manifest["source"]["revision"] == REVISION
    assert manifest["downloaded_at"] == "2026-08-01T12:00:00Z"
    assert {f["name"]: f["sha256"] for f in manifest["files"]} == {
        WEIGHTS_NAME: _sha(WEIGHTS), MMPROJ_NAME: _sha(MMPROJ),
    }
    assert manifest["total_bytes"] == len(WEIGHTS) + len(MMPROJ)
    assert manifest["license"]["base_model"]["id"] == "apache-2.0"
    assert manifest["license_acceptance"]["operator_reference"] == "CHG-2026-0142"
    assert manifest["downloader"]["name"] == downloader.DOWNLOADER_NAME
    # Un manifeste est conservé indéfiniment : il ne doit rien porter de sensible.
    assert schema.find_secret_leaks(manifest) == ()


def test_manifeste_signale_la_divergence_de_telechargeur_declare(tmp_path):
    catalog = write_catalog(tmp_path, downloader_name="huggingface_hub")
    config = make_config(tmp_path, catalog, acceptances=(acceptance(),))
    result = run_download(tmp_path, config)
    codes = {f.code for f in result.findings}
    assert "provenance_telechargeur_divergent" in codes


# ── 4. L'empreinte commande le renommage ─────────────────────────────────────

def test_empreinte_fausse_ne_produit_jamais_de_fichier_definitif(tmp_path):
    _, sain = granted(tmp_path)
    # Contrôle positif : sur des octets corrects, le fichier définitif EXISTE.
    run_download(tmp_path, sain)
    assert (sain.models_dir / WEIGHTS_NAME).is_file()

    autre = tmp_path / "corrompu"
    autre.mkdir()
    catalog = write_catalog(autre)
    config = downloader.DownloadConfig(
        catalog=catalog, models_dir=autre / "models",
        transport=FakeTransport(corrupt={WEIGHTS_NAME}),
        token_provider=lambda: None, disk_free=lambda p: 10 ** 12,
        acceptances=(acceptance(),), chunk_bytes=64, disk_margin_min_bytes=0,
    )
    config.models_dir.mkdir()
    result = run(
        downloader.make_executors(config)[schema.ACTION_DOWNLOAD_MODEL],
        download_step(), make_context(autre),
    )

    assert result.status == execution.STEP_FAILED
    assert not (config.models_dir / WEIGHTS_NAME).exists()
    # Le partiel faux est détruit : il ne doit pas pouvoir être « repris » plus tard.
    assert not (config.models_dir / (WEIGHTS_NAME + downloader.PART_SUFFIX)).exists()
    assert "empreinte SHA-256" in result.error


def test_corps_tronque_conserve_le_partiel_pour_la_reprise(tmp_path):
    catalog = write_catalog(tmp_path)
    transport = FakeTransport(truncate={WEIGHTS_NAME: 100})
    config = make_config(tmp_path, catalog, transport=transport, acceptances=(acceptance(),))
    result = run_download(tmp_path, config)

    assert result.status == execution.STEP_FAILED
    assert not (config.models_dir / WEIGHTS_NAME).exists()
    partial = config.models_dir / (WEIGHTS_NAME + downloader.PART_SUFFIX)
    assert partial.read_bytes() == WEIGHTS[:100]
    assert (config.models_dir / (WEIGHTS_NAME + downloader.RESUME_SUFFIX)).is_file()


# ── 5. Ensemble indivisible ──────────────────────────────────────────────────

def test_ensemble_partiel_nobtient_pas_de_manifeste(tmp_path):
    _, complet = granted(tmp_path)
    # Contrôle positif : un ensemble complet, lui, produit bien un manifeste.
    run_download(tmp_path, complet)
    assert downloader.provenance_path(complet.models_dir, ENTRY_ID).is_file()

    autre = tmp_path / "partiel"
    autre.mkdir()
    catalog = write_catalog(autre)
    config = downloader.DownloadConfig(
        catalog=catalog, models_dir=autre / "models",
        transport=FakeTransport(corrupt={MMPROJ_NAME}),
        token_provider=lambda: None, disk_free=lambda p: 10 ** 12,
        acceptances=(acceptance(),), chunk_bytes=64, disk_margin_min_bytes=0,
    )
    config.models_dir.mkdir()
    result = run(
        downloader.make_executors(config)[schema.ACTION_DOWNLOAD_MODEL],
        download_step(), make_context(autre),
    )

    assert result.status == execution.STEP_FAILED
    # Le premier fichier est sain et conservé — la reprise a du sens…
    assert (config.models_dir / WEIGHTS_NAME).is_file()
    # …mais l'ensemble n'est PAS déclaré utilisable.
    assert not downloader.provenance_path(config.models_dir, ENTRY_ID).exists()
    assert "ensemble_incomplet" in {f.code for f in result.findings}


def test_ensemble_partiel_echoue_a_la_verification(tmp_path):
    catalog = write_catalog(tmp_path)
    config = make_config(
        tmp_path, catalog, transport=FakeTransport(corrupt={MMPROJ_NAME}),
        acceptances=(acceptance(),),
    )
    run_download(tmp_path, config)
    result = run_verify(tmp_path, config)
    assert result.status == execution.STEP_FAILED
    assert "manifeste de provenance absent" in result.error


# ── 6. Reprise sûre ──────────────────────────────────────────────────────────

def _seed_partial(config, data: bytes, keep: int, *, revision: str = REVISION,
                  sidecar: bool = True) -> Path:
    part = config.models_dir / (WEIGHTS_NAME + downloader.PART_SUFFIX)
    part.write_bytes(data[:keep])
    if sidecar:
        state = downloader.ResumeState(
            repo_id=REPO_ID, revision=revision, file_name=WEIGHTS_NAME,
            sha256=_sha(data), size_bytes=len(data),
        )
        (config.models_dir / (WEIGHTS_NAME + downloader.RESUME_SUFFIX)).write_text(
            json.dumps(state.to_dict()), encoding="utf-8"
        )
    return part


def test_reprise_prouvee_ne_retelecharge_que_la_fin(tmp_path):
    transport = FakeTransport()
    catalog = write_catalog(tmp_path)
    config = make_config(tmp_path, catalog, transport=transport, acceptances=(acceptance(),))
    _seed_partial(config, WEIGHTS, 120)

    result = run_download(tmp_path, config)

    assert result.status == execution.STEP_DONE
    assert (config.models_dir / WEIGHTS_NAME).read_bytes() == WEIGHTS
    assert transport.headers_for(WEIGHTS_NAME)[0]["Range"] == "bytes=120-"
    poids = next(f for f in result.evidence["files"] if f["name"] == WEIGHTS_NAME)
    assert poids["resumed_from"] == 120
    assert poids["bytes_downloaded"] == len(WEIGHTS) - 120


def test_partiel_sans_preuve_dorigine_est_jete(tmp_path):
    transport = FakeTransport()
    catalog = write_catalog(tmp_path)
    config = make_config(tmp_path, catalog, transport=transport, acceptances=(acceptance(),))
    _seed_partial(config, WEIGHTS, 120, sidecar=False)

    result = run_download(tmp_path, config)

    assert result.status == execution.STEP_DONE
    assert (config.models_dir / WEIGHTS_NAME).read_bytes() == WEIGHTS
    assert "Range" not in transport.headers_for(WEIGHTS_NAME)[0]
    assert "reprise_refusee" in {f.code for f in result.findings}


def test_partiel_dune_autre_revision_est_jete(tmp_path):
    transport = FakeTransport()
    catalog = write_catalog(tmp_path)
    config = make_config(tmp_path, catalog, transport=transport, acceptances=(acceptance(),))
    _seed_partial(config, WEIGHTS, 120, revision=OTHER_REVISION)

    result = run_download(tmp_path, config)

    assert result.status == execution.STEP_DONE
    assert "Range" not in transport.headers_for(WEIGHTS_NAME)[0]
    assert "reprise_refusee" in {f.code for f in result.findings}


def test_partiel_plus_gros_que_la_cible_est_jete(tmp_path):
    transport = FakeTransport()
    catalog = write_catalog(tmp_path)
    config = make_config(tmp_path, catalog, transport=transport, acceptances=(acceptance(),))
    part = config.models_dir / (WEIGHTS_NAME + downloader.PART_SUFFIX)
    part.write_bytes(WEIGHTS + b"surplus")
    state = downloader.ResumeState(
        repo_id=REPO_ID, revision=REVISION, file_name=WEIGHTS_NAME,
        sha256=_sha(WEIGHTS), size_bytes=len(WEIGHTS),
    )
    (config.models_dir / (WEIGHTS_NAME + downloader.RESUME_SUFFIX)).write_text(
        json.dumps(state.to_dict()), encoding="utf-8"
    )

    result = run_download(tmp_path, config)

    assert result.status == execution.STEP_DONE
    assert (config.models_dir / WEIGHTS_NAME).read_bytes() == WEIGHTS
    assert "reprise_refusee" in {f.code for f in result.findings}


def test_serveur_qui_ignore_range_ne_produit_pas_de_fichier_concatene(tmp_path):
    transport = FakeTransport(ignore_range=True)
    catalog = write_catalog(tmp_path)
    config = make_config(tmp_path, catalog, transport=transport, acceptances=(acceptance(),))
    _seed_partial(config, WEIGHTS, 120)

    result = run_download(tmp_path, config)

    assert result.status == execution.STEP_DONE
    obtenu = (config.models_dir / WEIGHTS_NAME).read_bytes()
    assert obtenu == WEIGHTS
    assert len(obtenu) == len(WEIGHTS), "les octets repris auraient été concaténés"
    assert "reprise_non_honoree" in {f.code for f in result.findings}


def test_content_range_incoherent_refuse_la_reprise(tmp_path):
    catalog = write_catalog(tmp_path)
    config = make_config(
        tmp_path, catalog,
        transport=FakeTransport(content_range_override="bytes 120-999/999999"),
        acceptances=(acceptance(),),
    )
    _seed_partial(config, WEIGHTS, 120)

    result = run_download(tmp_path, config)

    assert result.status == execution.STEP_FAILED
    assert "Content-Range" in result.error
    assert not (config.models_dir / WEIGHTS_NAME).exists()
    assert not (config.models_dir / (WEIGHTS_NAME + downloader.PART_SUFFIX)).exists()


def test_content_range_absent_refuse_la_reprise(tmp_path):
    catalog = write_catalog(tmp_path)
    config = make_config(
        tmp_path, catalog, transport=FakeTransport(content_range_override=""),
        acceptances=(acceptance(),),
    )
    _seed_partial(config, WEIGHTS, 120)
    result = run_download(tmp_path, config)
    assert result.status == execution.STEP_FAILED
    assert "Content-Range absent" in result.error


# ── 7. Espace disque prévu avant écriture ────────────────────────────────────

def test_espace_insuffisant_refuse_avant_le_premier_octet(tmp_path):
    transport = FakeTransport()
    catalog = write_catalog(tmp_path)
    config = make_config(
        tmp_path, catalog, transport=transport, free_bytes=10,
        acceptances=(acceptance(),), margin_min=1024,
    )
    result = run_download(tmp_path, config)

    assert result.status == execution.STEP_FAILED
    assert "espace_disque_insuffisant" in {f.code for f in result.findings}
    # Aucune requête, aucun fichier : le refus est prononcé en amont.
    assert transport.requests == []
    assert list(config.models_dir.iterdir()) == []
    assert "Gio" in result.error and "marge" in result.error


def test_marge_est_le_plancher_quand_le_volume_est_petit(tmp_path):
    catalog = write_catalog(tmp_path)
    config = make_config(tmp_path, catalog, margin_min=4096)
    forecast = downloader.forecast_disk(100, 5000, config)
    assert forecast.margin_bytes == 4096
    assert forecast.needed_bytes == 4196
    assert forecast.sufficient is True
    assert downloader.forecast_disk(100, 4000, config).sufficient is False


def test_taille_absente_du_catalogue_refuse_le_telechargement(tmp_path):
    catalog = write_catalog(tmp_path, files=[
        _file_entry(WEIGHTS_NAME, "weights", WEIGHTS, size=None),
        _file_entry(MMPROJ_NAME, "mmproj", MMPROJ),
    ])
    transport = FakeTransport()
    config = make_config(tmp_path, catalog, transport=transport, acceptances=(acceptance(),))
    result = run_download(tmp_path, config)

    assert result.status == execution.STEP_FAILED
    assert "taille_inconnue" in {f.code for f in result.findings}
    assert transport.requests == []


# ── 8. Idempotence, et jamais de réparation silencieuse ──────────────────────

def test_ensemble_deja_complet_ne_retelecharge_rien(tmp_path):
    transport = FakeTransport()
    catalog = write_catalog(tmp_path)
    config = make_config(tmp_path, catalog, transport=transport, acceptances=(acceptance(),))

    premier = run_download(tmp_path, config)
    assert premier.status == execution.STEP_DONE
    # Contrôle positif : la première exécution a bien émis des requêtes.
    assert transport.requests

    transport.requests.clear()
    second = run_download(tmp_path, config)
    assert second.status == execution.STEP_ALREADY_SATISFIED
    assert transport.requests == []
    assert second.evidence["downloaded_bytes"] == 0


def test_manifeste_manquant_est_reecrit_et_rapporte_comme_une_modification(tmp_path):
    catalog = write_catalog(tmp_path)
    config = make_config(tmp_path, catalog, acceptances=(acceptance(),))
    run_download(tmp_path, config)
    downloader.provenance_path(config.models_dir, ENTRY_ID).unlink()

    result = run_download(tmp_path, config)
    assert result.status == execution.STEP_DONE
    assert result.evidence["downloaded_bytes"] == 0
    assert downloader.provenance_path(config.models_dir, ENTRY_ID).is_file()


def test_fichier_present_a_la_mauvaise_empreinte_nest_jamais_repare(tmp_path):
    transport = FakeTransport()
    catalog = write_catalog(tmp_path)
    config = make_config(tmp_path, catalog, transport=transport, acceptances=(acceptance(),))
    intrus = b"X" * len(WEIGHTS)
    (config.models_dir / WEIGHTS_NAME).write_bytes(intrus)

    result = run_download(tmp_path, config)

    assert result.status == execution.STEP_FAILED
    assert "artefact_existant_divergent" in {f.code for f in result.findings}
    # Ni écrasé, ni complété : l'opérateur décide.
    assert (config.models_dir / WEIGHTS_NAME).read_bytes() == intrus
    assert transport.requests == []


def test_fichier_present_a_la_mauvaise_taille_est_refuse(tmp_path):
    catalog = write_catalog(tmp_path)
    config = make_config(tmp_path, catalog, acceptances=(acceptance(),))
    (config.models_dir / WEIGHTS_NAME).write_bytes(WEIGHTS[:50])
    result = run_download(tmp_path, config)
    assert result.status == execution.STEP_FAILED
    assert "artefact_existant_divergent" in {f.code for f in result.findings}


# ── 9. Licence : fournie, jamais déduite ─────────────────────────────────────

def test_telechargement_refuse_sans_acceptation_de_licence(tmp_path):
    transport = FakeTransport()
    catalog = write_catalog(tmp_path)
    config = make_config(tmp_path, catalog, transport=transport)  # aucune acceptation

    result = run_download(tmp_path, config)

    assert result.status == execution.STEP_FAILED
    assert "licence_non_acceptee" in {f.code for f in result.findings}
    assert transport.requests == []
    assert list(config.models_dir.iterdir()) == []


def test_licence_permissive_ne_vaut_jamais_acceptation(tmp_path):
    """
    Le cas que §4 interdit de « simplifier ».

    L'entrée est intégralement sous Apache-2.0 — donc `permissive` — mais le
    catalogue exige une acceptation. Aucune n'est fournie : le refus doit tenir.
    """
    catalog = write_catalog(tmp_path, base_license="apache-2.0", fine_tune_license="apache-2.0")
    assert catalog.entries[0].license.permissive is True
    config = make_config(tmp_path, catalog)

    result = run_accept(tmp_path, config)

    assert result.status == execution.STEP_FAILED
    assert not downloader.acceptance_path(config.models_dir, ENTRY_ID).exists()


def test_acceptation_fournie_est_enregistree(tmp_path):
    catalog = write_catalog(tmp_path)
    config = make_config(tmp_path, catalog, acceptances=(acceptance(),))
    result = run_accept(tmp_path, config)

    assert result.status == execution.STEP_DONE
    document = json.loads(
        downloader.acceptance_path(config.models_dir, ENTRY_ID).read_text(encoding="utf-8")
    )
    assert document["accepted"] is True
    assert document["operator_reference"] == "CHG-2026-0142"
    assert document["recorded_at"] == "2026-08-01T12:00:00Z"
    assert document["base_model_license"] == "apache-2.0"


def test_acceptation_pour_une_autre_licence_est_refusee(tmp_path):
    catalog = write_catalog(tmp_path, base_license="llama3.1")
    config = make_config(
        tmp_path, catalog, acceptances=(acceptance(base_model_license="apache-2.0"),)
    )
    result = run_accept(tmp_path, config, licence="llama3.1")

    assert result.status == execution.STEP_FAILED
    assert "licence_acceptation_invalide" in {f.code for f in result.findings}
    assert "llama3.1" in result.error


def test_refus_explicite_de_licence_bloque(tmp_path):
    catalog = write_catalog(tmp_path)
    config = make_config(tmp_path, catalog, acceptances=(acceptance(accepted=False),))
    result = run_accept(tmp_path, config)
    assert result.status == execution.STEP_FAILED
    assert "REFUSÉE" in result.error


def test_acceptation_sans_reference_est_refusee(tmp_path):
    catalog = write_catalog(tmp_path)
    config = make_config(tmp_path, catalog, acceptances=(acceptance(operator_reference="  "),))
    result = run_accept(tmp_path, config)
    assert result.status == execution.STEP_FAILED
    assert "operator_reference" in result.error


def test_acceptation_deja_enregistree_est_idempotente(tmp_path):
    catalog = write_catalog(tmp_path)
    avec = make_config(tmp_path, catalog, acceptances=(acceptance(),))
    assert run_accept(tmp_path, avec).status == execution.STEP_DONE

    sans = make_config(tmp_path, catalog)
    result = run_accept(tmp_path, sans)
    assert result.status == execution.STEP_ALREADY_SATISFIED


def test_acceptation_enregistree_debloque_le_telechargement(tmp_path):
    catalog = write_catalog(tmp_path)
    run_accept(tmp_path, make_config(tmp_path, catalog, acceptances=(acceptance(),)))

    sans = make_config(tmp_path, catalog)  # plus aucune acceptation en configuration
    result = run_download(tmp_path, sans)
    assert result.status == execution.STEP_DONE


def test_acceptation_enregistree_avec_false_booleen_reste_un_refus(tmp_path):
    transport = FakeTransport()
    catalog = write_catalog(tmp_path)
    config = make_config(tmp_path, catalog, transport=transport)
    write_stored_acceptance(config, accepted=False)

    result = run_download(tmp_path, config)

    assert result.status == execution.STEP_FAILED
    assert "REFUSÉE" in result.error
    assert transport.requests == []


@pytest.mark.parametrize("accepted", ["false", "true", 0, None, [], {}])
def test_acceptation_enregistree_refuse_un_accepted_non_booleen(tmp_path, accepted):
    transport = FakeTransport()
    catalog = write_catalog(tmp_path)
    config = make_config(tmp_path, catalog, transport=transport)
    write_stored_acceptance(config, accepted=accepted)

    result = run_download(tmp_path, config)

    assert result.status == execution.STEP_FAILED
    assert "illisible" in result.error
    assert transport.requests == []


@pytest.mark.parametrize(
    "field_name",
    [
        "entry_id",
        "base_model_license",
        "fine_tune_license",
        "operator_reference",
        "accepted_at",
    ],
)
@pytest.mark.parametrize("value", [0, False, None, [], {}])
def test_acceptation_enregistree_refuse_un_champ_texte_non_string(
    tmp_path, field_name, value
):
    transport = FakeTransport()
    catalog = write_catalog(tmp_path)
    config = make_config(tmp_path, catalog, transport=transport)
    write_stored_acceptance(config, **{field_name: value})

    result = run_download(tmp_path, config)

    assert result.status == execution.STEP_FAILED
    assert "illisible" in result.error
    assert transport.requests == []


def test_acceptation_enregistree_pour_une_licence_perimee_ne_vaut_plus(tmp_path):
    catalog = write_catalog(tmp_path)
    config = make_config(tmp_path, catalog, acceptances=(acceptance(),))
    run_accept(tmp_path, config)

    # Le dépôt repasse sous une autre licence : l'acceptation d'hier ne couvre pas.
    nouveau = write_catalog(tmp_path, base_license="llama3.1")
    result = run_download(tmp_path, make_config(tmp_path, nouveau))
    assert result.status == execution.STEP_FAILED
    assert "périmée" in result.error


def test_plan_demandant_une_acceptation_non_requise_est_refuse(tmp_path):
    catalog = write_catalog(tmp_path, acceptance_required=False)
    config = make_config(tmp_path, catalog, acceptances=(acceptance(),))
    result = run_accept(tmp_path, config)
    assert result.status == execution.STEP_FAILED
    assert "licence_plan_divergent" in {f.code for f in result.findings}


def test_simulation_ne_cache_pas_une_acceptation_manquante(tmp_path):
    catalog = write_catalog(tmp_path)
    config = make_config(tmp_path, catalog)
    context = make_context(tmp_path, mode=execution.ExecutionMode.DRY_RUN)
    result = run_accept(tmp_path, config, context)
    assert result.status == execution.STEP_FAILED


def test_licence_non_permissive_acceptee_laisse_un_avertissement(tmp_path):
    catalog = write_catalog(tmp_path, base_license="llama3.1", fine_tune_license="llama3.1")
    config = make_config(
        tmp_path, catalog,
        acceptances=(acceptance(base_model_license="llama3.1", fine_tune_license="llama3.1"),),
    )
    result = run_accept(tmp_path, config, licence="llama3.1")
    assert result.status == execution.STEP_DONE
    assert "licence_non_permissive_acceptee" in {f.code for f in result.findings}


# ── 10. Simulation : aucun octet, aucune requête ─────────────────────────────

def test_simulation_ne_touche_ni_le_reseau_ni_le_disque(tmp_path):
    transport = FakeTransport()
    catalog = write_catalog(tmp_path)
    config = make_config(tmp_path, catalog, transport=transport, acceptances=(acceptance(),))
    context = make_context(tmp_path, mode=execution.ExecutionMode.DRY_RUN)

    result = run_download(tmp_path, config, context)

    assert result.status == execution.STEP_WOULD_APPLY
    assert transport.requests == []
    assert list(config.models_dir.iterdir()) == []
    assert {f["name"] for f in result.evidence["would_download"]} == {WEIGHTS_NAME, MMPROJ_NAME}
    assert result.evidence["disk"]["required_bytes"] == len(WEIGHTS) + len(MMPROJ)

    # Contrôle positif : en mode application, le même montage écrit et requête.
    applique = run_download(tmp_path, config)
    assert applique.status == execution.STEP_DONE
    assert transport.requests


def test_simulation_annonce_un_espace_insuffisant_sans_lappliquer(tmp_path):
    catalog = write_catalog(tmp_path)
    config = make_config(tmp_path, catalog, free_bytes=10, acceptances=(acceptance(),),
                         margin_min=1024)
    context = make_context(tmp_path, mode=execution.ExecutionMode.DRY_RUN)
    result = run_download(tmp_path, config, context)

    assert result.status == execution.STEP_WOULD_APPLY
    assert "espace_disque_insuffisant" in {f.code for f in result.findings}
    assert "INSUFFISANT" in result.summary


def test_simulation_ne_pretend_pas_avoir_verifie_les_empreintes(tmp_path):
    catalog = write_catalog(tmp_path)
    config = make_config(tmp_path, catalog, acceptances=(acceptance(),))
    (config.models_dir / WEIGHTS_NAME).write_bytes(WEIGHTS)
    context = make_context(tmp_path, mode=execution.ExecutionMode.DRY_RUN)

    result = run_download(tmp_path, config, context)

    assert result.status == execution.STEP_WOULD_APPLY
    etat = next(f for f in result.evidence["files"] if f["name"] == WEIGHTS_NAME)
    assert etat["digest_matches"] is None
    assert "n'ont PAS été recalculées" in result.evidence["avertissement"]


def test_simulation_de_verification_ne_lit_pas_les_fichiers(tmp_path):
    catalog = write_catalog(tmp_path)
    config = make_config(tmp_path, catalog)
    context = make_context(tmp_path, mode=execution.ExecutionMode.DRY_RUN)
    result = run_verify(tmp_path, config, context)
    assert result.status == execution.STEP_WOULD_APPLY
    assert result.evidence["files"] == [WEIGHTS_NAME, MMPROJ_NAME]


# ── 11. Vérification ─────────────────────────────────────────────────────────

def test_verification_dun_ensemble_sain_ne_change_rien(tmp_path):
    _, config = granted(tmp_path)
    run_download(tmp_path, config)
    result = run_verify(tmp_path, config)

    assert result.status == execution.STEP_ALREADY_SATISFIED
    assert result.evidence["problems"] == []
    assert result.evidence["provenance_present"] is True


def test_verification_refuse_un_fichier_disparu(tmp_path):
    _, config = granted(tmp_path)
    run_download(tmp_path, config)
    (config.models_dir / MMPROJ_NAME).unlink()

    result = run_verify(tmp_path, config)
    assert result.status == execution.STEP_FAILED
    assert f"{MMPROJ_NAME} : absent" in result.error


def test_verification_refuse_un_manifeste_dune_autre_revision(tmp_path):
    _, config = granted(tmp_path)
    run_download(tmp_path, config)
    chemin = downloader.provenance_path(config.models_dir, ENTRY_ID)
    document = json.loads(chemin.read_text(encoding="utf-8"))
    document["source"]["revision"] = OTHER_REVISION
    chemin.write_text(json.dumps(document), encoding="utf-8")

    result = run_verify(tmp_path, config)
    assert result.status == execution.STEP_FAILED
    assert "révision du manifeste" in result.error


def test_verification_refuse_un_fichier_altere_apres_coup(tmp_path):
    _, config = granted(tmp_path)
    run_download(tmp_path, config)
    (config.models_dir / WEIGHTS_NAME).write_bytes(b"Z" * len(WEIGHTS))

    result = run_verify(tmp_path, config)
    assert result.status == execution.STEP_FAILED
    assert "empreinte SHA-256 différente" in result.error


# ── 11 bis. Durabilité du renommage, un seul hachage par run ─────────────────

def test_le_gguf_est_fsynce_avant_le_renommage_et_le_repertoire_apres(tmp_path, monkeypatch):
    """
    Régression : `os.replace(part, final)` sans `fsync` du fichier. Après une
    coupure, le nom définitif pouvait porter un contenu incomplet, ensuite
    traité comme « divergent, ni écrasé ni réparé ».
    """
    import stat as stat_mod
    import threading

    _, config = granted(tmp_path)
    evenements: list[tuple[str, str]] = []
    principal = threading.main_thread()
    vrai_fsync, vrai_replace = downloader.os.fsync, downloader.os.replace

    def fsync(fd):
        assert threading.current_thread() is not principal, "fsync hors boucle d'événements"
        genre = "dir" if stat_mod.S_ISDIR(downloader.os.fstat(fd).st_mode) else "file"
        evenements.append(("fsync", genre))
        return vrai_fsync(fd)

    def replace(src, dst):
        evenements.append(("replace", Path(dst).name))
        return vrai_replace(src, dst)

    monkeypatch.setattr(downloader.os, "fsync", fsync)
    monkeypatch.setattr(downloader.os, "replace", replace)

    assert run_download(tmp_path, config).status == execution.STEP_DONE

    for nom in (WEIGHTS_NAME, MMPROJ_NAME):
        rang = evenements.index(("replace", nom))
        assert evenements[rang - 1] == ("fsync", "file")
        assert evenements[rang + 1] == ("fsync", "dir")


def test_un_fsync_de_repertoire_non_supporte_ne_fait_pas_echouer_le_telechargement(
    tmp_path, monkeypatch
):
    """
    Régression : sur NFS/FUSE/CIFS, `fsync` d'un répertoire peut rendre EINVAL.
    Le fichier est alors publié et vérifié ; l'étape ne doit pas être déclarée
    en échec. Le `fsync` du FICHIER reste obligatoire : son échec, lui, fait
    échouer l'étape avant tout renommage.
    """
    import errno
    import stat as stat_mod

    _, config = granted(tmp_path)
    vrai_fsync = downloader.os.fsync
    echec = {"dir": errno.EINVAL, "file": None}

    def fsync(fd):
        genre = "dir" if stat_mod.S_ISDIR(downloader.os.fstat(fd).st_mode) else "file"
        if echec[genre] is not None:
            raise OSError(echec[genre], downloader.os.strerror(echec[genre]))
        return vrai_fsync(fd)

    monkeypatch.setattr(downloader.os, "fsync", fsync)
    result = run_download(tmp_path, config)
    assert result.status == execution.STEP_DONE, result.error
    assert (config.models_dir / WEIGHTS_NAME).read_bytes() == WEIGHTS

    # Contrôle : un fsync de fichier en échec n'est jamais toléré.
    second = tmp_path / "second"
    second.mkdir()
    _, autre = granted(second)
    echec.update({"dir": None, "file": errno.EIO})
    result = run_download(second, autre)
    assert result.status == execution.STEP_FAILED
    assert not (autre.models_dir / WEIGHTS_NAME).exists()


def _compter_les_lectures(monkeypatch) -> list[str]:
    lectures: list[str] = []
    vrai = downloader._hash_file

    def compte(path, chunk_bytes, hasher=None):
        lectures.append(Path(path).name)
        return vrai(path, chunk_bytes, hasher)

    monkeypatch.setattr(downloader, "_hash_file", compte)
    return lectures


def _download_puis_verify(tmp_path, config):
    """Un run : UNE paire d'exécuteurs, comme `applier.build_registry`."""
    executeurs = downloader.make_executors(config)
    contexte = make_context(tmp_path)
    telechargement = run(executeurs[schema.ACTION_DOWNLOAD_MODEL], download_step(1), contexte)
    verification = run(executeurs[schema.ACTION_VERIFY_ARTIFACT], verify_step(2), contexte)
    return telechargement, verification


def test_un_gguf_inchange_n_est_hache_qu_une_fois_par_run(tmp_path, monkeypatch):
    """
    Régression : installation neuve = hachage en flux PUIS relecture complète
    par `verify_artifact` ; reprise = deux relectures. Désormais : aucune
    relecture sur une installation neuve, une seule à la reprise.
    """
    _, config = granted(tmp_path)
    lectures = _compter_les_lectures(monkeypatch)

    telechargement, verification = _download_puis_verify(tmp_path, config)
    assert telechargement.status == execution.STEP_DONE
    assert verification.status == execution.STEP_ALREADY_SATISFIED
    assert lectures == [], "l'empreinte calculée en flux suffit pour ce run"

    # Run suivant : nouveau mémo, chaque fichier est relu une fois, pas deux.
    telechargement, verification = _download_puis_verify(tmp_path, config)
    assert telechargement.status == execution.STEP_ALREADY_SATISFIED
    assert verification.status == execution.STEP_ALREADY_SATISFIED
    assert sorted(lectures) == sorted([WEIGHTS_NAME, MMPROJ_NAME])


def test_un_fichier_modifie_entre_download_et_verify_est_rehache_et_refuse(
    tmp_path, monkeypatch
):
    _, config = granted(tmp_path)
    lectures = _compter_les_lectures(monkeypatch)
    executeurs = downloader.make_executors(config)
    contexte = make_context(tmp_path)
    assert run(
        executeurs[schema.ACTION_DOWNLOAD_MODEL], download_step(1), contexte
    ).status == execution.STEP_DONE

    poids = config.models_dir / WEIGHTS_NAME
    mtime = poids.stat().st_mtime_ns
    poids.write_bytes(b"Z" * len(WEIGHTS))  # même taille, même inode
    downloader.os.utime(poids, ns=(mtime + 1_000_000_000, mtime + 1_000_000_000))

    verification = run(executeurs[schema.ACTION_VERIFY_ARTIFACT], verify_step(2), contexte)
    assert verification.status == execution.STEP_FAILED
    assert "empreinte SHA-256 différente" in verification.error
    assert lectures == [WEIGHTS_NAME]  # seul le fichier modifié est relu


# ── 12. Entrée non planifiable ───────────────────────────────────────────────

def test_entree_non_epinglee_nest_pas_telechargeable(tmp_path):
    catalog = write_catalog(tmp_path, files=[
        _file_entry(WEIGHTS_NAME, "weights", WEIGHTS, sha=None),
        _file_entry(MMPROJ_NAME, "mmproj", MMPROJ),
    ])
    assert catalog.entries[0].plannable is False
    transport = FakeTransport()
    config = make_config(tmp_path, catalog, transport=transport, acceptances=(acceptance(),))

    with pytest.raises(catalog_mod.CatalogError, match="pas planifiable"):
        run_download(tmp_path, config)
    assert transport.requests == []


def test_cible_inconnue_du_catalogue_est_refusee(tmp_path):
    catalog = write_catalog(tmp_path)
    config = make_config(tmp_path, catalog)
    with pytest.raises(downloader.DownloadError, match="divergent"):
        run_download(tmp_path, config, revision=OTHER_REVISION)


def test_resolution_dune_etape_daccept_license(tmp_path):
    catalog = write_catalog(tmp_path)
    entry = downloader.resolve_entry(accept_step(), catalog)
    assert entry.id == ENTRY_ID


# ── 13. Non-divulgation du jeton, de bout en bout ────────────────────────────

def test_le_jeton_ne_ressort_dans_aucun_artefact(tmp_path, monkeypatch):
    """
    Un jeton présent dans l'environnement ne doit apparaître nulle part.

    « Nulle part » se vérifie sur les trois surfaces qui sortent réellement de
    la machine : le rapport JSON, le rendu humain et le journal. Le contrôle
    positif vérifie que ces trois surfaces contiennent bien le dépôt — sans
    quoi une assertion « le jeton n'y est pas » serait vraie parce qu'il n'y a
    rien du tout.
    """
    for name in downloader.TOKEN_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HF_TOKEN", OPAQUE_TOKEN)

    journal: list[str] = []
    catalog = write_catalog(tmp_path)
    transport = FakeTransport(status={WEIGHTS_NAME: 403})
    config = downloader.DownloadConfig(
        catalog=catalog, models_dir=tmp_path / "models", transport=transport,
        disk_free=lambda p: 10 ** 12, acceptances=(acceptance(),),
        chunk_bytes=64, disk_margin_min_bytes=0,
    )
    config.models_dir.mkdir()
    context = make_context(tmp_path, log=journal.append)

    plan = execution.LoadedPlan(
        document={}, steps=(download_step(),), fingerprint="sha256:" + "0" * 64,
        generated_at="2026-08-01T11:00:00Z", mode="apply", origin="<test>",
    )
    registry = execution.ExecutorRegistry()
    downloader.register_executors(registry, config)
    report = asyncio.run(execution.execute_plan(plan, registry, context))

    rendu_json = execution.render_execution_json(report)
    rendu_humain = execution.render_execution_human(report)
    rendu_journal = "\n".join(journal)

    assert report.verdict() == execution.VERDICT_FAILED
    for surface in (rendu_json, rendu_humain, rendu_journal):
        assert OPAQUE_TOKEN not in surface
        assert "Bearer" not in surface
    # Contrôles positifs : les surfaces ne sont pas vides et parlent bien du sujet.
    assert REPO_ID in rendu_json and REPO_ID in rendu_humain
    assert "download_model" in rendu_journal
    # Le jeton a pourtant bien été utilisé : sans ça le test ne prouverait rien.
    assert transport.requests[0][1]["Authorization"] == f"Bearer {OPAQUE_TOKEN}"


def test_le_jeton_ne_suit_pas_une_redirection_vers_un_cdn_tiers(tmp_path):
    catalog = write_catalog(tmp_path)
    transport = FakeTransport(redirect_to="https://cdn-lfs.exemple-tiers.test/blobs")
    config = downloader.DownloadConfig(
        catalog=catalog, models_dir=tmp_path / "models", transport=transport,
        token_provider=lambda: OPAQUE_TOKEN, disk_free=lambda p: 10 ** 12,
        acceptances=(acceptance(),), chunk_bytes=64, disk_margin_min_bytes=0,
    )
    config.models_dir.mkdir()

    result = run_download(tmp_path, config)
    assert result.status == execution.STEP_DONE

    origine = [h for url, h in transport.requests if url.startswith(downloader.DEFAULT_ENDPOINT)]
    tiers = [h for url, h in transport.requests if "exemple-tiers.test" in url]
    # Contrôle positif : l'hôte d'origine a bien reçu le jeton.
    assert origine and all(h["Authorization"] == f"Bearer {OPAQUE_TOKEN}" for h in origine)
    assert tiers and all("Authorization" not in h for h in tiers)


def test_redirection_vers_du_non_https_est_refusee(tmp_path):
    catalog = write_catalog(tmp_path)
    config = make_config(
        tmp_path, catalog, transport=FakeTransport(redirect_to="http://miroir.interne.test"),
        acceptances=(acceptance(),),
    )
    result = run_download(tmp_path, config)
    assert result.status == execution.STEP_FAILED
    assert "HTTPS" in result.error


@pytest.mark.parametrize("endpoint", [
    "https://localhost",
    "https://127.0.0.1",
    "https://10.42.0.7",
    "https://169.254.169.254",
    "https://[::1]",
])
def test_endpoint_de_modele_refuse_une_destination_locale_ou_privee(tmp_path, endpoint):
    catalog = write_catalog(tmp_path)
    with pytest.raises(downloader.DownloadError, match="endpoint invalide"):
        downloader.DownloadConfig(catalog=catalog, models_dir=tmp_path, endpoint=endpoint)


def test_redirection_https_vers_une_ip_privee_est_refusee_avant_emission(tmp_path):
    catalog = write_catalog(tmp_path)
    transport = FakeTransport(redirect_to="https://169.254.169.254/latest")
    config = make_config(
        tmp_path,
        catalog,
        transport=transport,
        acceptances=(acceptance(),),
    )

    result = run_download(tmp_path, config)

    assert result.status == execution.STEP_FAILED
    assert "privée" in result.error
    assert len(transport.requests) == 1
    assert "169.254.169.254" not in transport.requests[0][0]


@pytest.mark.parametrize("addresses", [
    (_address("10.0.0.7"),),
    (_address("93.184.216.34"), _address("169.254.169.254")),
])
def test_transport_de_modele_refuse_un_dns_prive_ou_mixte_avant_connexion(addresses):
    factory = _PinnedConnectionFactory([])
    transport = downloader.UrllibTransport(
        resolver=lambda _host, _port: addresses,
        connection_factory=factory,
    )

    with pytest.raises(downloader.DownloadError, match="privée"):
        asyncio.run(transport.open("https://models.example.test/a.gguf", {}))

    assert factory.calls == []


def test_transport_de_modele_epingle_la_resolution_validee_sans_resoudre_deux_fois():
    response = _RawHttpsResponse(body=b"modele")
    factory = _PinnedConnectionFactory([response])
    resolver_calls: list[tuple[str, int]] = []

    def resolve(host: str, port: int):
        resolver_calls.append((host, port))
        return (_address("93.184.216.34"),)

    transport = downloader.UrllibTransport(
        resolver=resolve,
        connection_factory=factory,
        chunk_bytes=4,
    )

    async def exercise() -> bytes:
        opened = await transport.open("https://models.example.test/a.gguf?sig=x", {})
        body = b"".join([chunk async for chunk in opened.chunks()])
        await opened.close()
        return body

    assert asyncio.run(exercise()) == b"modele"
    assert resolver_calls == [("models.example.test", 443)]
    assert factory.calls == [
        ("models.example.test", 443, (_address("93.184.216.34"),), 60.0)
    ]
    assert factory.connections[0].requests == [("GET", "/a.gguf?sig=x", {})]
    assert response.closed is True
    assert factory.connections[0].closed is True


def test_manifeste_declare_lusage_dun_jeton_sans_le_porter(tmp_path):
    catalog = write_catalog(tmp_path)
    config = make_config(tmp_path, catalog, token=OPAQUE_TOKEN, acceptances=(acceptance(),))
    run_download(tmp_path, config)

    texte = downloader.provenance_path(config.models_dir, ENTRY_ID).read_text(encoding="utf-8")
    assert OPAQUE_TOKEN not in texte
    document = json.loads(texte)
    assert document["token_used"] is True
    assert schema.find_secret_leaks(document) == ()


# ── 14. Enregistrement et intégration au lanceur ─────────────────────────────

def test_register_executors_branche_exactement_trois_actions(tmp_path):
    catalog = write_catalog(tmp_path)
    config = make_config(tmp_path, catalog)
    registry = execution.ExecutorRegistry()
    downloader.register_executors(registry, config)

    assert set(registry.registered_actions()) == {
        schema.ACTION_DOWNLOAD_MODEL,
        schema.ACTION_VERIFY_ARTIFACT,
        schema.ACTION_ACCEPT_LICENSE,
    }
    assert registry.missing_actions((download_step(), verify_step(), accept_step())) == ()


def test_second_enregistrement_est_refuse(tmp_path):
    catalog = write_catalog(tmp_path)
    config = make_config(tmp_path, catalog)
    registry = execution.ExecutorRegistry()
    downloader.register_executors(registry, config)
    with pytest.raises(execution.ExecutionError, match="déjà enregistré"):
        downloader.register_executors(registry, config)


def test_sequence_complete_acceptation_telechargement_verification(tmp_path):
    catalog = write_catalog(tmp_path)
    config = make_config(tmp_path, catalog, acceptances=(acceptance(),))
    registry = execution.ExecutorRegistry()
    downloader.register_executors(registry, config)

    plan = execution.LoadedPlan(
        document={},
        steps=(accept_step(1), download_step(2), verify_step(3)),
        fingerprint="sha256:" + "1" * 64,
        generated_at="2026-08-01T11:00:00Z", mode="apply", origin="<test>",
    )
    report = asyncio.run(execution.execute_plan(plan, registry, make_context(tmp_path)))

    assert [r.status for r in report.results] == [
        execution.STEP_DONE, execution.STEP_DONE, execution.STEP_ALREADY_SATISFIED,
    ]
    assert report.verdict() == execution.VERDICT_OK
    assert report.exit_code() == execution.EXIT_OK
    # Le rapport rendu est validé de bout en bout par le contrat d'exécution.
    document = json.loads(execution.render_execution_json(report))
    assert execution.validate_execution_document(document) == ()


def test_une_simulation_complete_ne_sort_jamais_en_zero(tmp_path):
    catalog = write_catalog(tmp_path)
    config = make_config(tmp_path, catalog, acceptances=(acceptance(),))
    registry = execution.ExecutorRegistry()
    downloader.register_executors(registry, config)

    plan = execution.LoadedPlan(
        document={}, steps=(accept_step(1), download_step(2), verify_step(3)),
        fingerprint="sha256:" + "2" * 64,
        generated_at="2026-08-01T11:00:00Z", mode="dry-run", origin="<test>",
    )
    context = make_context(tmp_path, mode=execution.ExecutionMode.DRY_RUN)
    report = asyncio.run(execution.execute_plan(plan, registry, context))

    assert all(r.status == execution.STEP_WOULD_APPLY for r in report.results)
    assert report.exit_code() == execution.EXIT_PARTIAL
    assert list(config.models_dir.iterdir()) == []


def test_ecriture_hors_des_racines_autorisees_est_refusee(tmp_path):
    catalog = write_catalog(tmp_path)
    config = dataclasses.replace(
        make_config(tmp_path, catalog, acceptances=(acceptance(),)),
        models_dir=Path("/ailleurs/models"),
    )
    with pytest.raises(execution.ExecutionError, match="hors des racines"):
        run_download(tmp_path, config)


def test_config_refuse_un_endpoint_non_https(tmp_path):
    catalog = write_catalog(tmp_path)
    with pytest.raises(downloader.DownloadError, match="endpoint invalide"):
        downloader.DownloadConfig(
            catalog=catalog, models_dir=tmp_path, endpoint="http://miroir.interne.test",
        )
