"""
Un seul verdict GPU : runtime, doctor et inventaire bootstrap concordent.

Il existait trois analyseurs de la sortie `nvidia-smi` et trois résolveurs de
`CUDA_VISIBLE_DEVICES` (sonde d'admission `gpu_inventory`, `doctor`,
`bootstrap.inventory`), plus une analyse en ligne dans l'attestation de
calibration (`bootstrap.production`). Ils divergeaient sur les mêmes GPU :
`0,,1` passait doctor alors que la sonde d'admission le refusait, `-1` était
« inconnu » pour doctor et l'inventaire mais « aucun GPU » pour le runtime,
`0,7,1` exposait [0, 1] pour doctor contre [0] pour CUDA.

Le runtime fait référence — c'est lui qui gouverne l'admission. Ces tests
lancent les TROIS sondes réelles contre un même faux `nvidia-smi` (aucun GPU
requis) et exigent le même verdict et le même périmètre pour chaque valeur.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import doctor
import gpu_inventory
from bootstrap import calibration as cal
from bootstrap import inventory
from bootstrap import production as prod

# Requête runtime (8 champs) : index, uuid, name, used, total, driver, cap, mig.
FAKE_CSV = (
    "0, GPU-aaaa0000, NVIDIA L40S, 1000, 46068, 550.54.15, 8.9, [N/A]\n"
    "1, GPU-bbbb1111, NVIDIA L40S, 2000, 46068, 550.54.15, 8.9, [N/A]\n"
)

OK = "ok"
EMPTY = gpu_inventory.SCOPE_EMPTY
INVALID = gpu_inventory.SCOPE_INVALID
MIG = gpu_inventory.SCOPE_MIG_UNSUPPORTED

# (CUDA_VISIBLE_DEVICES, verdict attendu, index exposés). `None` = non définie.
TABLE = [
    pytest.param(None, OK, {0, 1}, id="unset"),
    pytest.param("", EMPTY, set(), id="empty"),
    pytest.param("0", OK, {0}, id="0"),
    pytest.param("1,0", OK, {0, 1}, id="1,0"),
    pytest.param("GPU-bbbb1111", OK, {1}, id="uuid"),
    pytest.param("MIG-1234abcd/1/0", MIG, None, id="mig"),
    # Coquille MIG : non vérifiable sans `nvidia-smi -L` (que le runtime ne
    # lance pas) — MIG, donc avertissement visible, jamais un vert silencieux.
    pytest.param("MIG-typo", MIG, None, id="mig-typo"),
    pytest.param("0,MIG-x", MIG, None, id="0,MIG-mixed"),
    # Un jeton non-MIG invalide ou un élément vide l'emporte sur MIG.
    pytest.param("7,MIG-x", INVALID, None, id="7,MIG-invalid"),
    pytest.param("MIG-x,7", INVALID, None, id="MIG,7-invalid"),
    pytest.param("0,,MIG-x", INVALID, None, id="0,,MIG-empty-token"),
    pytest.param("0,7,1", INVALID, {0}, id="0,7,1-truncated"),
    pytest.param("0,,1", INVALID, set(), id="0,,1-empty-token"),
    pytest.param("-1", EMPTY, set(), id="minus-one"),
]


@pytest.fixture
def fake_nvidia_smi(tmp_path, monkeypatch) -> Path:
    """Un `nvidia-smi` déterministe en tête de PATH, qui journalise son argv."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "argv.log"
    script = bin_dir / "nvidia-smi"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{argv_log}"\n'
        "cat <<'CSV'\n"
        f"{FAKE_CSV}"
        "CSV\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    return argv_log


def runtime_verdict(raw: str | None) -> tuple[str, set[int]]:
    measurement = asyncio.run(
        gpu_inventory.probe_gpu_memory(5.0, cuda_visible_devices=raw)
    )
    reason = OK if measurement.status == gpu_inventory.MEASUREMENT_MEASURED else measurement.reason
    return reason, {d.index for d in measurement.visible_devices}


def doctor_check(raw: str | None, total: float = 40.0, net: float = 30.0):
    gpus, reason = asyncio.run(doctor.probe_nvidia_smi())
    assert gpus is not None and len(gpus) == 2, reason
    config = SimpleNamespace(
        cluster_mode="local",
        total_vram_gb=total,
        cuda_visible_devices=raw,
        effective_vram_budget_gb=lambda: net,
    )
    return doctor.check_vram_detected(config, gpus)


def doctor_verdict(raw: str | None) -> tuple[str, int | None]:
    result = doctor_check(raw)
    verdict = {
        "no_visible_gpu": EMPTY,
        "cuda_visible_devices_invalid": INVALID,
        "mig_unsupported": MIG,
    }.get(result.code, OK)
    if verdict == OK:
        assert result.status in {"pass", "warn"}, result
    if verdict == MIG:
        # Jamais `skip` : non bloquant mais visible (code de sortie « warnings »).
        assert result.status == "warn" and result.is_blocking is False, result
    if verdict in (EMPTY, INVALID):
        assert result.status == "fail" and result.is_blocking is True, result
    exposed = None
    if verdict == OK:
        exposed = int(result.message.split("/", 1)[0])
    return verdict, exposed


def inventory_verdict(raw: str | None) -> tuple[str, set[int]]:
    probe = inventory.probe_nvidia_smi(timeout=5.0)
    assert probe.outcome == inventory.NVIDIA_OK, probe.detail
    env = {} if raw is None else {"CUDA_VISIBLE_DEVICES": raw}
    profile = inventory.collect_hardware(raw=inventory.RawHost(
        system="Linux", machine="x86_64", nvidia=probe, env=env,
    ))
    found = {f.code for f in profile.findings}
    verdict = OK
    for code in (MIG, INVALID, EMPTY):
        if code in found:
            verdict = code
    return verdict, {g.index for g in profile.visible_gpus}


@pytest.mark.parametrize(("raw", "expected", "exposed"), TABLE)
def test_runtime_doctor_et_inventaire_rendent_le_meme_verdict(
    fake_nvidia_smi, raw, expected, exposed
):
    runtime = runtime_verdict(raw)
    medecin = doctor_verdict(raw)
    inv = inventory_verdict(raw)

    assert runtime[0] == expected, f"runtime : {runtime}"
    assert medecin[0] == expected, f"doctor : {medecin} (runtime : {runtime})"
    assert inv[0] == expected, f"inventaire : {inv} (runtime : {runtime})"
    if exposed is not None:
        assert runtime[1] == exposed
        assert inv[1] == exposed, "l'inventaire n'expose pas les devices du runtime"
        if expected == OK:
            assert medecin[1] == len(exposed), "doctor ne compte pas les devices du runtime"


def test_les_trois_sondes_lancent_la_meme_requete_nvidia_smi(fake_nvidia_smi):
    """Un seul format de sortie : la requête du runtime, à l'identique."""
    asyncio.run(gpu_inventory.probe_gpu_memory(5.0, cuda_visible_devices="0"))
    asyncio.run(doctor.probe_nvidia_smi())
    inventory.probe_nvidia_smi(timeout=5.0)
    lines = fake_nvidia_smi.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3, lines
    assert len(set(lines)) == 1, f"requêtes divergentes : {lines}"
    assert gpu_inventory.NVIDIA_SMI_QUERY in lines[0]


def test_doctor_reste_utilisable_sans_nvidia_smi(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    gpus, reason = asyncio.run(doctor.probe_nvidia_smi())
    assert gpus is None
    assert "nvidia-smi introuvable" in reason


def test_doctor_mig_budget_plausible_avertit(fake_nvidia_smi):
    """UUID MIG plausible : warn non bloquant, budget confié à la configuration."""
    result = doctor_check("MIG-1234abcd/1/0", total=80.0, net=75.0)
    assert (result.status, result.code) == ("warn", "mig_unsupported")
    assert "nvidia-smi -L" in result.message


def test_doctor_mig_budget_superieur_a_tout_l_hote_bloque(fake_nvidia_smi):
    """Aucune instance MIG ne dépasse la VRAM physique de l'hôte (2 × 45 Go)."""
    result = doctor_check("MIG-1234abcd/1/0", total=120.0, net=110.0)
    assert (result.status, result.code) == ("fail", "vram_budget_exceeds_hardware")
    assert result.is_blocking is True


def test_doctor_zero_virgule_virgule_un_ne_passe_plus(fake_nvidia_smi):
    """Défaut corrigé : `0,,1` était PASS dans doctor, refusé par le runtime."""
    gpus, _ = asyncio.run(doctor.probe_nvidia_smi())
    config = SimpleNamespace(
        cluster_mode="local", total_vram_gb=40.0, cuda_visible_devices="0,,1",
        effective_vram_budget_gb=lambda: 30.0,
    )
    result = doctor.check_vram_detected(config, gpus)
    assert result.status == "fail"
    assert result.is_blocking is True
    assert "élément vide" in result.message


# ── Attestation de calibration (bootstrap.production) ────────────────────────

def _attest(monkeypatch, tmp_path, csv: str, plan_uuids: tuple[str, ...]):
    probes = prod.LlamaServerCalibrationProbes(
        binary=tmp_path / "llama-server", targets={}, port=19091,
        load_timeout_seconds=30, visible_gpu_uuids=plan_uuids,
    )
    identity = cal.CalibrationIdentity(
        model_id="model",
        runtime_version="b6042",
        hardware_fingerprint=cal.hardware_fingerprint([{
            "name": "NVIDIA L40S", "vram_total_mib": 46068,
            "driver_version": "550.54.15", "compute_cap": "8.9",
        }]),
        params_fingerprint=cal.CalibrationParams(ctx_size=4096).fingerprint(),
    )
    seen: list[tuple[str, ...]] = []

    async def version_ok(_binary):
        return SimpleNamespace(build=6042)

    async def inventory(command, *, timeout):
        seen.append(tuple(command))
        return 0, csv, ""

    monkeypatch.setattr(prod, "probe_llama_version", version_ok)
    monkeypatch.setattr(prod, "_subprocess_output", inventory)
    asyncio.run(probes.validate_environment(identity))
    return seen


def test_attestation_utilise_la_requete_et_le_resolveur_du_runtime(monkeypatch, tmp_path):
    seen = _attest(monkeypatch, tmp_path, FAKE_CSV, ("GPU-bbbb1111",))
    assert seen == [(
        "nvidia-smi", gpu_inventory.NVIDIA_SMI_QUERY, gpu_inventory.NVIDIA_SMI_FORMAT,
    )]


def test_attestation_refuse_un_uuid_du_plan_absent(monkeypatch, tmp_path):
    with pytest.raises(cal.CalibrationError, match="UUID GPU visibles différents du plan"):
        _attest(monkeypatch, tmp_path, FAKE_CSV, ("GPU-bbbb1111", "GPU-absent"))


def test_attestation_refuse_un_gpu_du_plan_en_mode_mig(monkeypatch, tmp_path):
    mig = FAKE_CSV.replace(
        "1, GPU-bbbb1111, NVIDIA L40S, 2000, 46068, 550.54.15, 8.9, [N/A]",
        "1, GPU-bbbb1111, NVIDIA L40S, 2000, 46068, 550.54.15, 8.9, Enabled",
    )
    with pytest.raises(cal.CalibrationError, match="mig_unsupported"):
        _attest(monkeypatch, tmp_path, mig, ("GPU-bbbb1111",))


def test_attestation_refuse_une_sortie_partiellement_illisible(monkeypatch, tmp_path):
    with pytest.raises(cal.CalibrationError, match="illisible"):
        _attest(monkeypatch, tmp_path, FAKE_CSV + "ligne, tronquee\n", ("GPU-bbbb1111",))
