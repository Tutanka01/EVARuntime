"""AUT-017 — raccords réels, stricts et sans secret du bootstrap."""
from __future__ import annotations

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import httpx
import pytest

import cli as cli_module
from config import Settings
from bootstrap import applier as ap
from bootstrap import calibration as cal
from bootstrap import catalog as cat
from bootstrap import downloader as dl
from bootstrap import execution as ex
from bootstrap import production as prod
from bootstrap import first_token as ft
from bootstrap import registry_writer as rw
from bootstrap import runtime_resolver as rr
from bootstrap import schema as sc


def _resolution(*, reference: str = "https://artifacts.example/llama-b6042.tar.gz"):
    variant = rr.ArtifactVariant(
        source=rr.SOURCE_OFFICIAL_RELEASE,
        backend=rr.BACKEND_CPU,
        platform="linux-x86_64",
        evidence=rr.EVIDENCE_SPEC,
        evidence_note="fixture épinglée relue par l'opérateur.",
        reference=reference,
        artifact_sha256="a" * 64,
        approx_bytes=1234,
    )
    manifest = rr.ProvenanceManifest(
        version="b6042",
        commit="abcdef1234567890",
        source=variant.source,
        backend=variant.backend,
        platform=variant.platform,
        artifact_sha256=variant.artifact_sha256,
        installed_at="2026-08-01T12:00:00Z",
    )
    return rr.RuntimeResolution(
        profile=rr.HardwareProfile(
            platform="linux-x86_64", backend_candidates=(rr.BACKEND_CPU,)
        ),
        min_build=6000,
        resolved=True,
        reuse_existing=False,
        degraded=False,
        targeted_backend=None,
        variant=variant,
        manifest=manifest,
        observed_build=None,
        summary="official-release · cpu · linux-x86_64 · b6042",
        findings=(),
        rejected=("conteneur écarté",),
    )


def _plan_fragment(resolution=None):
    resolution = resolution or _resolution()
    return {
        "sections": [{
            "name": sc.SECTION_RUNTIME,
            "summary": resolution.summary,
            "data": resolution.to_data(),
            "findings": [],
        }]
    }


def test_runtime_resolution_est_reconstruite_exactement_depuis_le_plan():
    expected = _resolution()
    rebuilt = prod.runtime_resolution_from_plan(_plan_fragment(expected))
    assert rebuilt.to_data() == expected.to_data()
    assert rebuilt.manifest == expected.manifest


@pytest.mark.parametrize("with_manifest", [True, False])
def test_runtime_existant_est_reconstruit_avec_variant_null(with_manifest):
    initial = _resolution()
    expected = replace(
        initial,
        reuse_existing=True,
        variant=None,
        manifest=initial.manifest if with_manifest else None,
        observed_build=6042,
        summary="runtime existant conservé",
    )
    rebuilt = prod.runtime_resolution_from_plan(_plan_fragment(expected))
    assert rebuilt.to_data() == expected.to_data()
    assert rebuilt.reuse_existing is True


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("selected_backend",), "cuda12"),
        (("manifest", "backend"), "cuda12"),
        (("variant", "artifact_sha256"), "b" * 64),
    ],
)
def test_runtime_incoherent_est_refuse_sans_recalcul(path, value):
    document = copy.deepcopy(_plan_fragment())
    node = document["sections"][0]["data"]
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(prod.ProductionWiringError):
        prod.runtime_resolution_from_plan(document)


def test_runtime_refuse_un_champ_ajoute_qu_il_ne_comprend_pas():
    document = _plan_fragment()
    document["sections"][0]["data"]["archive_url_override"] = "https://evil.invalid/a"
    with pytest.raises(prod.ProductionWiringError, match="champs inconnus"):
        prod.runtime_resolution_from_plan(document)


def test_installateur_prend_l_url_epinglee_du_plan_et_aucune_autre(tmp_path):
    installer = prod.runtime_installer_from_plan(_plan_fragment(), tmp_path / "runtime")
    assert installer.request.archive_url == _resolution().variant.reference
    assert installer.request.install_root == tmp_path / "runtime"


def test_installateur_refuse_une_page_generique_non_https(tmp_path):
    document = _plan_fragment(_resolution(reference="http://artifacts.example/releases"))
    with pytest.raises(prod.ProductionWiringError, match="HTTPS"):
        prod.runtime_installer_from_plan(document, tmp_path)


def test_admin_secret_vient_d_un_fichier_prive_ou_de_l_environnement(tmp_path):
    secret = "secret-administration-assez-long"
    path = tmp_path / "admin.secret"
    path.write_text(secret + "\n", encoding="utf-8")
    path.chmod(0o600)
    assert prod.read_admin_secret(path=path, environ={}) == secret
    assert prod.read_admin_secret(environ={"ADMIN_SECRET": secret}) == secret


def test_admin_secret_refuse_un_fichier_lisible_par_d_autres(tmp_path):
    path = tmp_path / "admin.secret"
    path.write_text("valeur-qui-ne-doit-pas-sortir", encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(prod.ProductionWiringError) as error:
        prod.read_admin_secret(path=path, environ={})
    assert "valeur-qui-ne-doit-pas-sortir" not in str(error.value)
    assert "chmod 600" in str(error.value)


def test_admin_secret_refuse_un_fichier_possede_par_un_autre_uid(tmp_path, monkeypatch):
    path = tmp_path / "admin.secret"
    path.write_text("valeur-qui-ne-doit-pas-sortir", encoding="utf-8")
    path.chmod(0o600)
    real_fstat = prod.os.fstat

    def foreign_owner(descriptor):
        info = real_fstat(descriptor)
        return SimpleNamespace(st_mode=info.st_mode, st_uid=prod.os.geteuid() + 1000)

    monkeypatch.setattr(prod.os, "fstat", foreign_owner)
    with pytest.raises(prod.ProductionWiringError, match="appartient") as error:
        prod.read_admin_secret(path=path, environ={})
    assert "valeur-qui-ne-doit-pas-sortir" not in str(error.value)


def test_acceptation_de_licence_est_explicite_et_reliee_au_catalogue():
    catalogue = cat.load_catalog()
    entry = catalogue.entries[0]
    accepted = prod.license_acceptances(
        catalogue,
        [entry.id],
        operator_reference="CHG-2026-081",
        accepted_at="2026-08-01T12:00:00Z",
    )
    assert len(accepted) == 1
    assert accepted[0].accepted is True
    assert accepted[0].base_model_license == entry.license.base_model.id
    assert accepted[0].fine_tune_license == entry.license.fine_tune.id


def test_acceptation_refuse_id_inconnu_reference_vide_et_doublon():
    catalogue = cat.load_catalog()
    entry_id = catalogue.entries[0].id
    with pytest.raises(prod.ProductionWiringError):
        prod.license_acceptances(catalogue, ["absent"], operator_reference="CHG-1")
    with pytest.raises(prod.ProductionWiringError):
        prod.license_acceptances(catalogue, [entry_id], operator_reference="")
    with pytest.raises(prod.ProductionWiringError):
        prod.license_acceptances(
            catalogue, [entry_id, entry_id], operator_reference="CHG-1"
        )


class _FakeAsyncClient:
    constructor_kwargs = []

    def __init__(self, *args, **kwargs):
        self.constructor_kwargs.append(kwargs)
        self._response = httpx.Response(
            200,
            json={"status": "ok"},
            request=httpx.Request("GET", "https://eva.example/health"),
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def request(self, *args, **kwargs):
        return self._response

    @asynccontextmanager
    async def stream(self, *args, **kwargs):
        response = httpx.Response(
            200,
            content=b'data: {"choices": []}\n\ndata: [DONE]\n\n',
            request=httpx.Request("POST", "https://eva.example/v1/chat/completions"),
        )
        yield response


def test_client_http_concret_satisfait_requete_et_flux(monkeypatch):
    _FakeAsyncClient.constructor_kwargs.clear()
    monkeypatch.setattr(prod.httpx, "AsyncClient", _FakeAsyncClient)
    client = prod.AsyncHttpClient()

    async def exercise():
        response = await client.request("GET", "https://eva.example/health", timeout=1)
        assert response.status == 200 and response.body == {"status": "ok"}
        async with client.stream(
            "POST", "https://eva.example/v1/chat/completions", timeout=1
        ) as stream:
            return [line async for line in stream.aiter_lines()]

    lines = asyncio.run(exercise())
    assert lines[0].startswith("data:")
    assert "data: [DONE]" in lines
    assert _FakeAsyncClient.constructor_kwargs == [
        {"follow_redirects": False, "trust_env": False},
        {"follow_redirects": False, "trust_env": False},
    ]


def test_client_http_refuse_les_identifiants_dans_url():
    client = prod.AsyncHttpClient()
    with pytest.raises(prod.ProductionWiringError, match="identifiants"):
        asyncio.run(client.request(
            "GET", "https://user:password@eva.example/health", timeout=1
        ))


def test_synchronisation_live_envoie_un_contrat_etroit_sans_secret_dans_l_url():
    calls = []

    class Client:
        async def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            return ft.HttpResponse(status=200, body={"status": "ok"})

    sync = prod.LiveRegistrySyncClient(
        admin_url="http://127.0.0.1:8000",
        admin_secret="secret-administration-de-test",
        client=Client(),
        timeout_seconds=12,
        lease_seconds=900,
    )
    asyncio.run(sync.activate("modele-a", 12.5, "a" * 64))
    asyncio.run(sync.rollback("modele-a", "b" * 64))
    asyncio.run(sync.confirm("modele-a", "c" * 64))

    assert [call[2]["json"]["action"] for call in calls] == [
        "activate", "rollback", "confirm",
    ]
    assert calls[0][2]["json"]["vram_gb"] == 12.5
    assert calls[0][2]["json"]["lease_seconds"] == 900
    assert all("secret-administration-de-test" not in call[1] for call in calls)
    assert all(call[2]["timeout"] == 12 for call in calls)
    assert all(
        call[2]["headers"]["Authorization"]
        == "Bearer secret-administration-de-test"
        for call in calls
    )


def test_synchronisation_live_refuse_un_http_non_200_sans_republier_le_corps():
    class Client:
        async def request(self, *_args, **_kwargs):
            return ft.HttpResponse(
                status=409,
                body={"detail": "hf_" + "A" * 32},
            )

    sync = prod.LiveRegistrySyncClient(
        admin_url="http://127.0.0.1:8000",
        admin_secret="secret-administration-de-test",
        client=Client(),
        timeout_seconds=12,
        lease_seconds=900,
    )
    with pytest.raises(prod.ProductionWiringError) as error:
        asyncio.run(sync.rollback("modele-a", "a" * 64))
    assert "hf_" not in str(error.value)
    assert "HTTP 409" in str(error.value)


def test_bail_live_couvre_tous_les_timeouts_sequentiels():
    settings = ft.FirstTokenSettings(
        base_url="https://eva.example.edu",
        admin_url="http://127.0.0.1:8000",
        load_timeout_s=910.0,
    )

    lease = prod.derive_live_registry_lease_seconds(
        settings,
        sync_timeout_seconds=40.0,
    )

    assert lease == 1336


def test_bail_live_refuse_une_recette_plus_longue_que_son_maximum():
    settings = ft.FirstTokenSettings(
        base_url="https://eva.example.edu",
        admin_url="http://127.0.0.1:8000",
        load_timeout_s=4000.0,
    )

    with pytest.raises(prod.ProductionWiringError, match="3600"):
        prod.derive_live_registry_lease_seconds(
            settings,
            sync_timeout_seconds=40.0,
        )


@pytest.mark.parametrize("admin_url", [
    "https://gateway.internal:8000", "http://192.168.1.20:8000",
])
def test_admin_url_est_strictement_loopback(admin_url):
    with pytest.raises(prod.ProductionWiringError, match="loopback"):
        prod.validate_gateway_urls(
            base_url="https://eva.example", admin_url=admin_url
        )


def test_url_publique_distante_exige_https_et_loopback_tolere_http():
    with pytest.raises(prod.ProductionWiringError, match="HTTPS"):
        prod.validate_gateway_urls(
            base_url="http://eva.example", admin_url="http://127.0.0.1:8000"
        )
    assert prod.validate_gateway_urls(
        base_url="http://localhost:8000", admin_url="http://[::1]:8000"
    ) == ("http://localhost:8000", "http://[::1]:8000")


def test_origins_gateway_normalisent_un_unique_slash_final():
    assert prod.validate_gateway_urls(
        base_url="https://eva.example/", admin_url="http://127.0.0.1:8000/"
    ) == ("https://eva.example", "http://127.0.0.1:8000")


@pytest.mark.parametrize(
    "suffix",
    ["/v1", "//", "?tenant=a", "#section", "/?", "/#"],
)
@pytest.mark.parametrize("field_name", ["base_url", "admin_url"])
def test_origins_gateway_refusent_chemin_query_et_fragment(field_name, suffix):
    urls = {
        "base_url": "https://eva.example",
        "admin_url": "http://127.0.0.1:8000",
    }
    urls[field_name] += suffix

    with pytest.raises(prod.ProductionWiringError, match=r"origin HTTP\(S\)"):
        prod.validate_gateway_urls(**urls)


@pytest.mark.parametrize("port", ["abc", "70000"])
def test_url_avec_port_invalide_est_refusee_au_cablage(port):
    with pytest.raises(prod.ProductionWiringError, match="port invalide"):
        prod.validate_gateway_urls(
            base_url=f"https://eva.example:{port}",
            admin_url="http://127.0.0.1:8000",
        )


def test_sonde_vram_additionne_les_gpu(monkeypatch, tmp_path):
    async def command(_args, *, timeout):
        assert timeout == 5.0
        return 0, "0, GPU-0, 100, 48000\n1, GPU-1, 200, 48000\n", ""

    monkeypatch.setattr(prod, "_subprocess_output", command)
    probes = prod.LlamaServerCalibrationProbes(
        binary=tmp_path / "llama-server", targets={}, port=19091,
        load_timeout_seconds=30,
    )
    reading = asyncio.run(probes.read_vram())
    assert reading.ok
    assert reading.used_bytes == 300 * 1024 * 1024
    assert reading.total_bytes == 96000 * 1024 * 1024


def test_sonde_vram_ignore_les_gpu_masques(monkeypatch, tmp_path):
    async def command(_args, *, timeout):
        return 0, "0, GPU-0, 100, 48000\n1, GPU-1, 900, 48000\n", ""

    monkeypatch.setattr(prod, "_subprocess_output", command)
    probes = prod.LlamaServerCalibrationProbes(
        binary=tmp_path / "llama-server", targets={}, port=19091,
        load_timeout_seconds=30, visible_gpu_indices=(0,),
    )
    reading = asyncio.run(probes.read_vram())
    assert reading.ok
    assert reading.used_bytes == 100 * 1024 * 1024
    assert reading.total_bytes == 48000 * 1024 * 1024


def test_sonde_vram_refuse_si_aucun_gpu_du_plan_ne_correspond(monkeypatch, tmp_path):
    async def command(_args, *, timeout):
        return 0, "1, GPU-1, 900, 48000\n", ""

    monkeypatch.setattr(prod, "_subprocess_output", command)
    probes = prod.LlamaServerCalibrationProbes(
        binary=tmp_path / "llama-server", targets={}, port=19091,
        load_timeout_seconds=30, visible_gpu_indices=(0,),
    )
    reading = asyncio.run(probes.read_vram())
    assert reading.ok is False
    assert "aucun GPU du plan" in reading.detail


def test_commande_calibration_est_loopback_et_ne_porte_aucun_secret(tmp_path):
    params = cal.CalibrationParams(ctx_size=4096, parallel=2)
    target = prod.CalibrationTarget(tmp_path / "model.gguf", params)
    probes = prod.LlamaServerCalibrationProbes(
        binary=tmp_path / "llama-server", targets={"model": target}, port=19091,
        load_timeout_seconds=30,
    )
    command = probes._command(
        target, cal.LoadRequest("model", cal.PHASE_TARGET, 4096, 2)
    )
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert command[command.index("--port") + 1] == "19091"
    assert command[command.index("--alias") + 1] == "model"
    assert "--api-key" not in command
    assert "Authorization" not in " ".join(command)
    for option in ("-b", "-ub", "-t", "--threads-http", "--cache-prompt"):
        assert option in command


def test_processus_calibration_force_les_uuid_gpu_du_plan(monkeypatch, tmp_path):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "GPU-AUTRE")
    probes = prod.LlamaServerCalibrationProbes(
        binary=tmp_path / "llama-server", targets={}, port=19091,
        load_timeout_seconds=30, visible_gpu_indices=(0, 2),
        visible_gpu_uuids=("GPU-PLAN-0", "GPU-PLAN-2"),
    )
    environment = probes._process_env()
    assert environment is not None
    assert environment["CUDA_VISIBLE_DEVICES"] == "GPU-PLAN-0,GPU-PLAN-2"


def test_processus_calibration_n_herite_d_aucun_secret(monkeypatch, tmp_path):
    """DP-001 : ce llama-server parse un GGUF, comme ceux du pool de production."""
    monkeypatch.setenv("ADMIN_SECRET", "secret-admin")
    monkeypatch.setenv("HF_TOKEN", "secret-hf")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "GPU-OPERATEUR")
    monkeypatch.setenv("PATH", "/usr/bin")
    probes = prod.LlamaServerCalibrationProbes(
        binary=tmp_path / "llama-server", targets={}, port=19091,
        load_timeout_seconds=30,
    )
    environment = probes._process_env()
    assert "ADMIN_SECRET" not in environment
    assert "HF_TOKEN" not in environment
    # Contrôle positif : l'environnement utile est bien transmis, sans plan GPU.
    assert environment["PATH"] == "/usr/bin"
    assert environment["CUDA_VISIBLE_DEVICES"] == "GPU-OPERATEUR"


def test_attestation_refuse_runtime_ou_pilote_gpu_courant_different(
    monkeypatch, tmp_path
):
    probes = prod.LlamaServerCalibrationProbes(
        binary=tmp_path / "llama-server", targets={}, port=19091,
        load_timeout_seconds=30, visible_gpu_indices=(0,),
        visible_gpu_uuids=("GPU-PLAN-0",),
    )
    expected_gpu = {
        "name": "NVIDIA L40S", "vram_total_mib": 46068,
        "driver_version": "570.86", "compute_cap": "8.9",
    }
    identity = cal.CalibrationIdentity(
        model_id="model",
        runtime_version="b6042",
        hardware_fingerprint=cal.hardware_fingerprint([expected_gpu]),
        params_fingerprint=cal.CalibrationParams(ctx_size=4096).fingerprint(),
    )

    async def version_ok(_binary):
        return SimpleNamespace(build=6042)

    async def inventory_ok(_command, *, timeout):
        assert timeout == 5.0
        return 0, "0, GPU-PLAN-0, NVIDIA L40S, 46068, 570.86, 8.9\n", ""

    monkeypatch.setattr(prod, "probe_llama_version", version_ok)
    monkeypatch.setattr(prod, "_subprocess_output", inventory_ok)
    asyncio.run(probes.validate_environment(identity))

    async def version_changed(_binary):
        return SimpleNamespace(build=7000)

    monkeypatch.setattr(prod, "probe_llama_version", version_changed)
    with pytest.raises(cal.CalibrationError, match="runtime courant"):
        asyncio.run(probes.validate_environment(identity))

    async def inventory_driver_changed(_command, *, timeout):
        return 0, "0, GPU-PLAN-0, NVIDIA L40S, 46068, 575.10, 8.9\n", ""

    monkeypatch.setattr(prod, "probe_llama_version", version_ok)
    monkeypatch.setattr(prod, "_subprocess_output", inventory_driver_changed)
    with pytest.raises(cal.CalibrationError, match="empreinte GPU courante"):
        asyncio.run(probes.validate_environment(identity))


def test_port_calibration_occupe_est_refuse_avant_lancement(monkeypatch, tmp_path):
    binary = tmp_path / "llama-server"
    model = tmp_path / "model.gguf"
    binary.write_bytes(b"binary")
    model.write_bytes(b"GGUF")
    probes = prod.LlamaServerCalibrationProbes(
        binary=binary,
        targets={"model": prod.CalibrationTarget(
            model, cal.CalibrationParams(ctx_size=4096)
        )},
        port=19091,
        load_timeout_seconds=30,
    )

    async def version(_binary):
        return SimpleNamespace(build=6042)

    def occupied(_port):
        raise prod.ProductionWiringError("port loopback déjà occupé")

    async def must_not_start(*args, **kwargs):
        raise AssertionError("le subprocess ne doit pas être lancé")

    monkeypatch.setattr(prod, "probe_llama_version", version)
    monkeypatch.setattr(prod, "_assert_loopback_port_available", occupied)
    monkeypatch.setattr(prod.asyncio, "create_subprocess_exec", must_not_start)
    outcome = asyncio.run(probes.load_model(
        cal.LoadRequest("model", cal.PHASE_TARGET, 4096, 1)
    ))
    assert outcome.ok is False
    assert "occupé" in outcome.detail


def test_health_etranger_est_refuse_par_identite_v1_models(monkeypatch, tmp_path):
    binary = tmp_path / "llama-server"
    model = tmp_path / "model.gguf"
    binary.write_bytes(b"binary")
    model.write_bytes(b"GGUF")
    probes = prod.LlamaServerCalibrationProbes(
        binary=binary,
        targets={"model": prod.CalibrationTarget(
            model, cal.CalibrationParams(ctx_size=4096)
        )},
        port=19091,
        load_timeout_seconds=30,
    )
    process = SimpleNamespace(returncode=None, stdout=None, stderr=None)

    async def create_process(*args, **kwargs):
        return process

    async def version(_binary):
        return SimpleNamespace(build=6042)

    async def request(_method, url, **kwargs):
        if url.endswith("/health"):
            return ft.HttpResponse(status=200, body={})
        return ft.HttpResponse(status=200, body={"data": [{"id": "foreign"}]})

    cleaned = []

    async def stop():
        cleaned.append(True)
        probes._process = None

    monkeypatch.setattr(prod.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(prod, "probe_llama_version", version)
    monkeypatch.setattr(prod, "_assert_loopback_port_available", lambda _port: None)
    monkeypatch.setattr(probes.http, "request", request)
    monkeypatch.setattr(probes, "_stop_process", stop)
    outcome = asyncio.run(probes.load_model(
        cal.LoadRequest("model", cal.PHASE_TARGET, 4096, 1)
    ))
    assert outcome.ok is False
    assert "identité" in outcome.detail and "/v1/models" in outcome.detail
    assert cleaned == [True]


def test_echec_de_health_nettoie_toujours_le_processus(monkeypatch, tmp_path):
    binary = tmp_path / "llama-server"
    model = tmp_path / "model.gguf"
    binary.write_bytes(b"binary")
    model.write_bytes(b"GGUF")
    target = prod.CalibrationTarget(model, cal.CalibrationParams(ctx_size=4096))
    probes = prod.LlamaServerCalibrationProbes(
        binary=binary, targets={"model": target}, port=19091,
        load_timeout_seconds=30, visible_gpu_indices=(0,),
    )

    class Process:
        returncode = None
        stdout = None
        stderr = None

    async def create_process(*args, **kwargs):
        return Process()

    async def version(_binary):
        return SimpleNamespace(build=6042)

    async def broken_request(*args, **kwargs):
        raise RuntimeError("health cassée")

    cleaned = []

    async def stop():
        cleaned.append(True)
        probes._process = None

    monkeypatch.setattr(prod.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(prod, "probe_llama_version", version)
    monkeypatch.setattr(prod, "_assert_loopback_port_available", lambda _port: None)
    monkeypatch.setattr(probes.http, "request", broken_request)
    monkeypatch.setattr(probes, "_stop_process", stop)
    with pytest.raises(RuntimeError, match="health cassée"):
        asyncio.run(probes.load_model(
            cal.LoadRequest("model", cal.PHASE_TARGET, 4096, 1)
        ))
    assert cleaned == [True]


def test_health_retry_une_connexion_refusee_pendant_le_demarrage(monkeypatch, tmp_path):
    binary = tmp_path / "llama-server"
    model = tmp_path / "model.gguf"
    binary.write_bytes(b"binary")
    model.write_bytes(b"GGUF")
    probes = prod.LlamaServerCalibrationProbes(
        binary=binary,
        targets={"model": prod.CalibrationTarget(
            model, cal.CalibrationParams(ctx_size=4096)
        )},
        port=19091,
        load_timeout_seconds=30,
    )

    async def create_process(*args, **kwargs):
        return SimpleNamespace(returncode=None, stdout=None, stderr=None)

    async def version(_binary):
        return SimpleNamespace(build=6042)

    calls = []

    async def request(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise httpx.ConnectError(
                "pas encore en écoute", request=httpx.Request("GET", "http://127.0.0.1")
            )
        if len(calls) == 2:
            return ft.HttpResponse(status=200, body={})
        return ft.HttpResponse(status=200, body={"data": [{"id": "model"}]})

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(prod.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(prod, "probe_llama_version", version)
    monkeypatch.setattr(prod, "_assert_loopback_port_available", lambda _port: None)
    monkeypatch.setattr(probes.http, "request", request)
    monkeypatch.setattr(prod.asyncio, "sleep", no_sleep)
    outcome = asyncio.run(probes.load_model(
        cal.LoadRequest("model", cal.PHASE_TARGET, 4096, 1)
    ))
    assert outcome.ok is True
    assert len(calls) == 3
    probes._process = None


def test_annulation_de_health_nettoie_toujours_le_processus(monkeypatch, tmp_path):
    binary = tmp_path / "llama-server"
    model = tmp_path / "model.gguf"
    binary.write_bytes(b"binary")
    model.write_bytes(b"GGUF")
    target = prod.CalibrationTarget(model, cal.CalibrationParams(ctx_size=4096))
    probes = prod.LlamaServerCalibrationProbes(
        binary=binary, targets={"model": target}, port=19091,
        load_timeout_seconds=30, visible_gpu_indices=(0,),
    )

    async def create_process(*args, **kwargs):
        return SimpleNamespace(returncode=None, stdout=None, stderr=None)

    async def version(_binary):
        return SimpleNamespace(build=6042)

    entered = asyncio.Event()

    async def cancelled_request(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    cleaned = []

    async def stop():
        cleaned.append("start")
        await asyncio.sleep(0.01)
        cleaned.append("done")
        probes._process = None

    monkeypatch.setattr(prod.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(prod, "probe_llama_version", version)
    monkeypatch.setattr(prod, "_assert_loopback_port_available", lambda _port: None)
    monkeypatch.setattr(probes.http, "request", cancelled_request)
    monkeypatch.setattr(probes, "_stop_process", stop)
    async def exercise():
        task = asyncio.create_task(probes.load_model(
            cal.LoadRequest("model", cal.PHASE_TARGET, 4096, 1)
        ))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert cleaned == ["start", "done"]


def test_cibles_calibration_derivent_les_fichiers_et_parametres_du_catalogue(tmp_path):
    catalogue = cat.load_catalog()
    entry = catalogue.entries[0]
    targets = prod.calibration_targets(catalogue, tmp_path, [entry.id])
    target = targets[entry.id]
    weights = next(item for item in entry.files if item.role == "weights")
    assert target.model_path == tmp_path / weights.name
    assert target.params.ctx_size == entry.runtime.defaults.ctx_size
    assert target.params.parallel == entry.runtime.defaults.parallel


def test_preuve_catalogue_calibree_est_acceptable_par_activation_provisoire(tmp_path):
    catalogue = cat.load_catalog()
    entry = catalogue.entries[0]
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    target = prod.calibration_targets(catalogue, models_dir, [entry.id])[entry.id]
    registry_entry = rw.build_registry_entry(
        entry.to_dict(), models_dir=models_dir, allowed_model_dirs=(models_dir,)
    )
    expected = rw.params_fingerprint(registry_entry["llama_params"])
    assert target.params.fingerprint() == expected

    measured_at = "2026-08-01T12:00:00Z"
    hardware_fingerprint = "sha256:" + "4" * 64
    proof = rw.CalibrationProof(
        model_id=entry.id,
        runtime_version="b6042",
        hardware_fingerprint=hardware_fingerprint,
        params_fingerprint=target.params.fingerprint(),
        peak_vram_gb=12.0,
        peak_ram_gb=2.0,
        load_seconds=3.0,
        measured_at=measured_at,
    )
    config = rw.WriterConfig(
        registry_path=tmp_path / "models.yaml",
        models_dir=models_dir,
        allowed_model_dirs=(models_dir,),
        runtime_version="b6042",
        hardware_fingerprint=hardware_fingerprint,
        vram_budget_gb=24.0,
        catalog_entries={entry.id: entry.to_dict()},
        now=lambda: datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc),
    )
    rw._check_calibration(config, entry.id, proof, registry_entry)


def test_cibles_calibration_choisissent_le_premier_shard(tmp_path):
    catalogue = cat.load_catalog()
    original = catalogue.entries[0]
    files = cat.FileSet(files=(
        cat.CatalogFile(
            "modele-00001-of-00002.gguf", "weights_shard", "a" * 64, 10
        ),
        cat.CatalogFile(
            "modele-00002-of-00002.gguf", "weights_shard", "b" * 64, 10
        ),
    ))
    entry = replace(original, files=files)
    split_catalogue = replace(catalogue, entries=(entry,))
    target = prod.calibration_targets(
        split_catalogue, tmp_path, [entry.id]
    )[entry.id]
    assert target.model_path == tmp_path / "modele-00001-of-00002.gguf"


def test_factory_de_generation_lie_chaque_sonde_au_modele(monkeypatch):
    captured = []

    def fake_probe(*, settings, client, admin_secret):
        captured.append((settings.model_id, client, admin_secret))

        async def probe():
            raise AssertionError("la fabrique ne doit pas exécuter la sonde")

        return probe

    monkeypatch.setattr(prod, "generation_probe_from_recipe", fake_probe)
    client = prod.AsyncHttpClient()
    factory = prod.generation_probe_factory_from_recipe(
        settings=ft.FirstTokenSettings(
            base_url="https://eva.example", admin_url="http://127.0.0.1:8000"
        ),
        client=client,
        admin_secret="secret-administration-de-test",
    )
    assert callable(factory("modele-a")) and callable(factory("modele-b"))
    assert [item[0] for item in captured] == ["modele-a", "modele-b"]


def test_empreinte_materielle_adapte_le_document_inventaire():
    document = {"sections": [{
        "name": sc.SECTION_HARDWARE,
        "data": {"gpus": [{
            "model": "NVIDIA L40S",
            "vram_total_bytes": 46068 * 1024 * 1024,
            "driver_version": "570.86",
            "compute_capability": "8.9",
            "index": 0,
            "uuid": "GPU-PLAN-0",
            "visible": True,
        }]},
    }]}
    expected = cal.hardware_fingerprint([{
        "name": "NVIDIA L40S",
        "vram_total_mib": 46068,
        "driver_version": "570.86",
        "compute_cap": "8.9",
    }])
    assert prod.hardware_fingerprint_from_plan(document) == expected
    assert prod.visible_gpu_indices_from_plan(document) == (0,)
    assert prod.visible_gpu_uuids_from_plan(document) == ("GPU-PLAN-0",)


def test_module_ne_contient_pas_de_valeur_de_secret_dans_ses_objets_publics():
    # Contrôle positif : le détecteur sait voir un secret sous le mauvais nom.
    fake = "sk-" + "A" * 24
    assert sc.find_secret_leaks({"value": fake})
    public = {
        "client": prod.AsyncHttpClient().__class__.__name__,
        "runtime": _resolution().to_data(),
    }
    assert not sc.find_secret_leaks(json.loads(json.dumps(public)))


def test_cablage_production_enregistre_les_neuf_actions(monkeypatch, tmp_path):
    catalogue = cat.load_catalog()
    entry = catalogue.entries[0]
    resolution = _resolution()
    hardware = {
        "gpus": [{
            "index": 0,
            "uuid": "GPU-PLAN-0",
            "model": "NVIDIA L40S",
            "vram_total_bytes": 46068 * 1024 * 1024,
            "driver_version": "570.86",
            "compute_capability": "8.9",
            "visible": True,
        }],
    }
    sections = (
        sc.PlanSection(
            name=sc.SECTION_RUNTIME, version=1, status="ok",
            summary=resolution.summary, data=resolution.to_data(),
        ),
        sc.PlanSection(
            name=sc.SECTION_HARDWARE, version=1, status="ok",
            summary="GPU visible", data=hardware,
        ),
    )
    action_targets = [
        (sc.ACTION_VERIFY_ARTIFACT, "llama-server b6042 (cpu)"),
        (sc.ACTION_INSTALL_RUNTIME, "llama-server b6042 (cpu)"),
        (sc.ACTION_ACCEPT_LICENSE, f"{entry.id} — {entry.license.base_model.id}"),
        (sc.ACTION_DOWNLOAD_MODEL, f"{entry.repo_id}@{entry.revision}"),
        (sc.ACTION_VERIFY_ARTIFACT, entry.id),
        (sc.ACTION_WRITE_REGISTRY, f"models.yaml → {entry.id}"),
        (sc.ACTION_CALIBRATE_MODEL, entry.id),
        (sc.ACTION_ENABLE_MODEL, entry.id),
        (sc.ACTION_SMOKE_TEST, entry.id),
        (sc.ACTION_WARMUP_MODEL, entry.id),
    ]
    plan = sc.BootstrapPlan(
        generated_at="2026-08-01T12:00:00Z",
        mode="local",
        sections=sections,
        steps=tuple(
            sc.PlanStep(
                order=index, action=action, target=target,
                detail=f"étape {index}", reversible=True,
            )
            for index, (action, target) in enumerate(action_targets, 1)
        ),
        decisions=(sc.Decision(
            topic="runtime", choice="archive épinglée", rationale="SHA relu"
        ),),
    )
    loaded = ex.load_plan_document(json.dumps(plan.to_dict()))
    monkeypatch.setenv("ADMIN_SECRET", "secret-administration-de-test-assez-long")
    common = dict(
        applier_module=ap,
        catalog_module=cat,
        downloader_module=dl,
        production_module=prod,
        writer_module=rw,
        loaded_plan=loaded,
        catalog_path=None,
        models_dir=tmp_path / "models",
        registry_path=tmp_path / "models.yaml",
        runtime_version=None,
        hardware_fingerprint=None,
        vram_budget_gb=43.0,
        runtime_root=tmp_path / "runtime",
        llama_server_binary=None,
        calibration_report_dir=tmp_path / "calibration",
        calibration_port=19091,
        calibration_load_timeout=30.0,
        base_url="https://eva.example",
        admin_url="http://127.0.0.1:8000",
        admin_secret_file=None,
        service_env_path=tmp_path / "env",
        service_settings=Settings(
            _env_file=None,
            models_config_path=tmp_path / "models.yaml",
            llama_server_bin=tmp_path / "runtime" / "current" / "llama-server",
        ),
        admin_secret_environ=None,
        accepted_license_ids=(entry.id,),
        license_reference="CHG-2026-081",
        ttft_threshold_ms=0,
        ttft_gate=False,
    )
    config = cli_module._build_applier_config(**common, dry_run=False)
    registry = ap.build_registry(config, ap.ProofLedger())
    assert set(registry.registered_actions()) == set(sc.PLAN_ACTIONS)

    monkeypatch.delenv("ADMIN_SECRET")
    dry_config = cli_module._build_applier_config(**common, dry_run=True)
    assert dry_config.first_token is not None
    assert dry_config.warmup is not None
    with pytest.raises(prod.ProductionWiringError, match="ADMIN_SECRET"):
        cli_module._build_applier_config(**common, dry_run=False)

    secret_from_env_file = dict(common)
    secret_from_env_file["admin_secret_environ"] = {
        "ADMIN_SECRET": "secret-lu-depuis-environment-file"
    }
    from_env = cli_module._build_applier_config(
        **secret_from_env_file, dry_run=False
    )
    assert from_env.first_token is not None
    assert from_env.first_token.admin_secret == "secret-lu-depuis-environment-file"

    wrong_registry = dict(common)
    wrong_registry["service_settings"] = Settings(
        _env_file=None,
        models_config_path=tmp_path / "other-models.yaml",
        llama_server_bin=tmp_path / "runtime" / "current" / "llama-server",
    )
    with pytest.raises(prod.ProductionWiringError, match="MODELS_CONFIG_PATH"):
        cli_module._build_applier_config(**wrong_registry, dry_run=True)

    wrong_binary = dict(common)
    wrong_binary["service_settings"] = Settings(
        _env_file=None,
        models_config_path=tmp_path / "models.yaml",
        llama_server_bin=tmp_path / "other-runtime" / "llama-server",
    )
    with pytest.raises(prod.ProductionWiringError, match="LLAMA_SERVER_BIN"):
        cli_module._build_applier_config(**wrong_binary, dry_run=True)
