"""
Tests d'AUT-002 — inventaire matériel automatique.

Ce que ces tests verrouillent
-----------------------------
- le contrat de données du profil matériel : chaque clé attendue est
  présente, typée, et le document reste sérialisable JSON ;
- le filtrage par `CUDA_VISIBLE_DEVICES` dans ses quatre formes (absente, vide,
  index, UUID) et la troncature CUDA au premier jeton invalide ;
- l'ABSENCE DE REPLI SILENCIEUX : `nvidia-smi` absent, muet ou cassé donnent
  trois codes de constat distincts, et le cas « cassé » ne propose AUCUN backend ;
- le refus d'un profil `--hardware-profile` incohérent, avec un message qui dit
  quoi corriger ;
- la NON-DIVULGATION de la section produite, avec contrôle positif.

Testabilité sans matériel
-------------------------
Une seule fonction du module touche l'hôte (`capture_host`). Tout le reste est
une fonction pure d'un `RawHost` : ces tests décrivent donc des machines que la
CI ne possède pas — L40S bi-GPU, pilote préhistorique, `/models` absent — sans
jamais dépendre de la machine qui les exécute. Deux tests seulement frappent le
vrai hôte, et n'assertent que l'absence d'exception.
"""
from __future__ import annotations

import json

import pytest

from bootstrap import inventory, schema


# ── Fabriques ─────────────────────────────────────────────────────────────────

CPUINFO_X86 = """\
processor\t: 0
vendor_id\t: GenuineIntel
model name\t: Intel(R) Xeon(R) Gold 6430
flags\t\t: fpu vme de pse tsc msr sse4_2 avx avx2 f16c fma avx512f avx512_vnni \
xsaveopt rdtscp arch_perfmon pebs bts
processor\t: 1
model name\t: Intel(R) Xeon(R) Gold 6430
"""

MEMINFO = """\
MemTotal:       528214596 kB
MemFree:          1234567 kB
MemAvailable:   401234567 kB
Buffers:           123456 kB
"""

OS_RELEASE = """\
PRETTY_NAME="Ubuntu 24.04.1 LTS"
NAME="Ubuntu"
ID=ubuntu
VERSION_ID="24.04"
"""

# Deux L40S : 46068 MiB exposés chacune, très en dessous des « 48 Go »
# commerciaux — c'est exactement l'écart relevé en §0.9.
NVIDIA_CSV_TWO_L40S = (
    "0, GPU-1111aaaa-0000-0000-0000-000000000000, NVIDIA L40S, 46068, 550.54.15, 8.9\n"
    "1, GPU-2222bbbb-0000-0000-0000-000000000000, NVIDIA L40S, 46068, 550.54.15, 8.9\n"
)

L40S_BYTES = 46068 * 1024 * 1024


def raw_host(**overrides) -> inventory.RawHost:
    """Un hôte Linux bi-L40S sain ; chaque champ est surchargeable."""
    base: dict = {
        "system": "Linux",
        "release": "6.8.0-40-generic",
        "machine": "x86_64",
        "os_release_text": OS_RELEASE,
        "cpuinfo_text": CPUINFO_X86,
        "meminfo_text": MEMINFO,
        "ram_total_bytes": 528214596 * 1024,
        "ram_available_bytes": 401234567 * 1024,
        "disk_available_bytes": 3_000_000_000_000,
        "disk_path": "/models",
        "nvidia": inventory.NvidiaSmiProbe(inventory.NVIDIA_OK, stdout=NVIDIA_CSV_TWO_L40S),
        "env": {"CUDA_VISIBLE_DEVICES": "0,1"},
    }
    base.update(overrides)
    return inventory.RawHost(**base)


def codes(profile: inventory.HardwareProfile) -> set[str]:
    return {f.code for f in profile.findings}


def finding(profile: inventory.HardwareProfile, code: str) -> schema.Finding:
    matches = [f for f in profile.findings if f.code == code]
    assert matches, f"constat {code!r} absent ; présents : {sorted(codes(profile))}"
    return matches[0]


def valid_profile_document(**overrides) -> dict:
    document = {
        "os": "ubuntu",
        "os_version": "24.04",
        "arch": "x86_64",
        "cpu_model": "AMD EPYC 9354",
        "cpu_flags": ["avx2"],
        "ram_total_bytes": 512 * 1024**3,
        "ram_available_bytes": 400 * 1024**3,
        "disk_available_bytes": 2 * 1024**4,
        "gpus": [{
            "index": 0,
            "uuid": "GPU-3333cccc-0000-0000-0000-000000000000",
            "vendor": "nvidia",
            "model": "NVIDIA L40S",
            "vram_total_bytes": L40S_BYTES,
            "driver_version": "550.54.15",
            "compute_capability": "8.9",
        }],
        "backend_candidates": ["cuda12", "vulkan", "cpu"],
    }
    document.update(overrides)
    return document


# ── Contrat de données §5 ─────────────────────────────────────────────────────

def test_profile_carries_every_field_of_section_five():
    profile = inventory.collect_hardware(raw=raw_host())
    data = profile.to_dict()

    for key in (
        "os", "os_version", "arch", "cpu_model", "cpu_flags",
        "ram_total_bytes", "ram_available_bytes", "disk_available_bytes",
        "gpus", "backend_candidates",
    ):
        assert key in data, f"clé §5 manquante : {key}"

    assert data["os"] == "ubuntu"
    assert data["os_version"] == "24.04"
    assert data["arch"] == "x86_64"
    assert "Xeon" in data["cpu_model"]
    assert data["ram_total_bytes"] == 528214596 * 1024
    assert data["ram_available_bytes"] == 401234567 * 1024
    assert data["disk_available_bytes"] == 3_000_000_000_000

    gpu = data["gpus"][0]
    for key in (
        "uuid", "vendor", "model", "vram_total_bytes",
        "driver_version", "compute_capability",
    ):
        assert key in gpu, f"clé §5 manquante sur un GPU : {key}"
    assert gpu["vendor"] == "nvidia"
    assert gpu["vram_total_bytes"] == L40S_BYTES

    # Sérialisable JSON sans encodeur maison : le plan est un document, pas un
    # pickle Python.
    assert json.loads(json.dumps(data)) == data


def test_plan_section_conforms_to_the_shared_contract():
    section = inventory.to_plan_section(inventory.collect_hardware(raw=raw_host()))
    assert section.name == schema.SECTION_HARDWARE
    assert section.version == inventory.SECTION_VERSION
    assert section.status == "ok"
    assert section.summary

    plan = schema.BootstrapPlan(
        generated_at="2026-07-31T09:00:00Z", mode="local", sections=(section,)
    )
    assert schema.validate_plan_dict(plan.to_dict()) == ()
    assert plan.applicable


def test_cpu_flags_keep_the_decisive_ones_and_drop_the_noise():
    """Deux cents drapeaux rendraient le plan illisible ; seuls comptent ceux-ci."""
    profile = inventory.collect_hardware(raw=raw_host())
    assert "avx2" in profile.cpu_flags
    assert "avx512_vnni" in profile.cpu_flags
    assert "sse4_2" in profile.cpu_flags
    # Contrôle négatif : le bruit est bien écarté, pas simplement absent du CPU.
    assert "pebs" not in profile.cpu_flags
    assert "xsaveopt" not in profile.cpu_flags


def test_missing_cpu_flags_are_reported_not_silently_empty():
    profile = inventory.collect_hardware(raw=raw_host(cpuinfo_text=None))
    assert "cpu_flags_unavailable" in codes(profile)
    assert "cpu_model_unknown" in codes(profile)
    assert profile.status == "warn"


def test_mem_available_is_preferred_over_the_sysconf_approximation():
    """`MemFree` sous-estime massivement ; `MemAvailable` est la seule bonne source."""
    profile = inventory.collect_hardware(raw=raw_host())
    assert profile.ram_available_bytes == 401234567 * 1024
    assert "ram_available_approximate" not in codes(profile)

    fallback = inventory.collect_hardware(
        raw=raw_host(meminfo_text=None, ram_available_bytes=8 * 1024**3)
    )
    assert fallback.ram_available_bytes == 8 * 1024**3
    assert "ram_available_approximate" in codes(fallback)


def test_missing_ram_total_blocks_the_section():
    profile = inventory.collect_hardware(
        raw=raw_host(meminfo_text=None, ram_total_bytes=None)
    )
    assert "ram_total_unknown" in codes(profile)
    assert profile.status == "fail"


# ── Disque : le volume qui compte, pas « / » ──────────────────────────────────

def test_disk_is_measured_on_the_volume_that_will_hold_the_gguf():
    profile = inventory.collect_hardware(
        raw=raw_host(disk_path="/srv/gguf", disk_available_bytes=42)
    )
    assert profile.disk_path == "/srv/gguf"
    assert profile.disk_available_bytes == 42
    assert "/srv/gguf" in inventory.to_plan_section(profile).summary


def test_unreadable_disk_blocks_rather_than_reporting_zero():
    profile = inventory.collect_hardware(
        raw=raw_host(disk_available_bytes=None, disk_error="FileNotFoundError")
    )
    assert profile.status == "fail"
    message = finding(profile, "disk_unreadable").message
    assert "/models" in message
    assert "--models-dir" in message


def test_capture_host_honours_the_requested_volume(tmp_path):
    options = inventory.InventoryOptions(models_dir=tmp_path, env={})
    raw = inventory.capture_host(options)
    assert raw.disk_path == str(tmp_path)
    assert raw.disk_available_bytes is not None


# ── CUDA_VISIBLE_DEVICES ──────────────────────────────────────────────────────

def test_vram_counts_only_the_devices_exposed_by_cuda_visible_devices():
    """La demande explicite de §5 : le budget suit la variable, pas le matériel."""
    both = inventory.collect_hardware(raw=raw_host(env={"CUDA_VISIBLE_DEVICES": "0,1"}))
    assert both.visible_vram_total_bytes == 2 * L40S_BYTES

    one = inventory.collect_hardware(raw=raw_host(env={"CUDA_VISIBLE_DEVICES": "1"}))
    assert len(one.gpus) == 2, "les deux GPU restent inventoriés"
    assert one.visible_vram_total_bytes == L40S_BYTES
    assert [g.visible for g in one.gpus] == [False, True]
    assert "cuda_visible_devices_partial" in codes(one)


def test_cuda_visible_devices_accepts_uuids():
    profile = inventory.collect_hardware(raw=raw_host(env={
        "CUDA_VISIBLE_DEVICES": "GPU-2222bbbb-0000-0000-0000-000000000000",
    }))
    assert profile.visible_vram_total_bytes == L40S_BYTES
    assert [g.visible for g in profile.gpus] == [False, True]


def test_unset_cuda_visible_devices_exposes_everything_and_says_so():
    profile = inventory.collect_hardware(raw=raw_host(env={}))
    assert profile.visible_vram_total_bytes == 2 * L40S_BYTES
    assert "cuda_visible_devices_unset" in codes(profile)
    assert finding(profile, "cuda_visible_devices_unset").level == "info"


def test_empty_cuda_visible_devices_is_a_blocking_inconsistency():
    """Définie et vide ≠ non définie : CUDA n'expose alors AUCUN device."""
    profile = inventory.collect_hardware(raw=raw_host(env={"CUDA_VISIBLE_DEVICES": ""}))
    assert profile.visible_vram_total_bytes == 0
    assert profile.status == "fail"
    assert "cuda_visible_devices_empty" in codes(profile)


def test_invalid_token_truncates_the_selection_as_cuda_does():
    """CUDA ignore tout ce qui suit un jeton invalide — l'inventaire aussi."""
    profile = inventory.collect_hardware(raw=raw_host(env={
        "CUDA_VISIBLE_DEVICES": "0,7,1",
    }))
    assert profile.visible_vram_total_bytes == L40S_BYTES, (
        "le GPU 1, situé après le jeton invalide, ne doit pas être compté"
    )
    assert profile.status == "fail"
    assert "cuda_visible_devices_invalid" in codes(profile)


def test_resolve_visible_devices_distinguishes_unset_from_empty():
    gpus = inventory.parse_nvidia_smi_csv(NVIDIA_CSV_TWO_L40S)
    assert len(inventory.resolve_visible_devices(gpus, None).devices) == 2
    assert inventory.resolve_visible_devices(gpus, "").devices == ()
    assert inventory.resolve_visible_devices(gpus, "  ").devices == ()


# ── Aucun repli silencieux (§6) ───────────────────────────────────────────────

def test_absent_nvidia_smi_is_a_warning_not_a_clean_zero_gpu():
    profile = inventory.collect_hardware(raw=raw_host(
        nvidia=inventory.NvidiaSmiProbe(
            inventory.NVIDIA_ABSENT, detail="nvidia-smi introuvable dans le PATH"
        ),
    ))
    assert profile.status == "warn"
    assert "gpu_probe_unavailable" in codes(profile)
    assert profile.backend_candidates == ("cpu",)


def test_nvidia_smi_that_answers_without_gpu_has_its_own_code():
    profile = inventory.collect_hardware(raw=raw_host(
        nvidia=inventory.NvidiaSmiProbe(inventory.NVIDIA_OK, stdout="\n"),
    ))
    assert profile.status == "warn"
    assert "gpu_absent" in codes(profile)
    assert "gpu_probe_unavailable" not in codes(profile), (
        "un pilote qui répond n'est pas un pilote absent"
    )
    assert profile.backend_candidates == ("cpu",)


def test_broken_nvidia_smi_blocks_and_proposes_no_backend_at_all():
    """
    Le cœur de la règle : pilote cassé ≠ hôte CPU.

    Proposer `cpu` ici produirait une installation « réussie » au TTFT
    inacceptable sur une machine pourtant équipée. La liste vide force le
    résolveur de runtime à refuser.
    """
    profile = inventory.collect_hardware(raw=raw_host(
        nvidia=inventory.NvidiaSmiProbe(
            inventory.NVIDIA_FAILED, detail="nvidia-smi a échoué (code 9)"
        ),
    ))
    assert profile.status == "fail"
    assert "gpu_probe_failed" in codes(profile)
    assert "gpu_absent" not in codes(profile)
    assert profile.backend_candidates == ()

    # Contrôle positif : sur le même hôte avec une sonde qui aboutit, la liste
    # est bien peuplée — l'assertion d'absence ci-dessus voit donc quelque chose.
    healthy = inventory.collect_hardware(raw=raw_host())
    assert healthy.backend_candidates


def test_a_blocked_hardware_section_makes_the_plan_inapplicable():
    section = inventory.to_plan_section(inventory.collect_hardware(raw=raw_host(
        nvidia=inventory.NvidiaSmiProbe(inventory.NVIDIA_FAILED, detail="code 9"),
    )))
    plan = schema.BootstrapPlan(
        generated_at="2026-07-31T09:00:00Z", mode="local", sections=(section,)
    )
    assert not plan.applicable
    assert plan.exit_code() == schema.EXIT_BLOCKED


# ── Backends ──────────────────────────────────────────────────────────────────

def test_backend_branch_follows_the_driver_version():
    recent = inventory.collect_hardware(raw=raw_host(nvidia=inventory.NvidiaSmiProbe(
        inventory.NVIDIA_OK,
        stdout="0, GPU-a, NVIDIA H200, 143771, 580.65.06, 9.0\n",
    ), env={}))
    assert recent.backend_candidates == ("cuda13", "cuda12", "vulkan", "cpu")

    twelve = inventory.collect_hardware(raw=raw_host(env={}))
    assert twelve.backend_candidates == ("cuda12", "vulkan", "cpu")


def test_the_oldest_driver_of_the_visible_set_governs_the_branch():
    """Une branche CUDA que le plus vieux pilote refuse ferait échouer un GPU du lot."""
    profile = inventory.collect_hardware(raw=raw_host(nvidia=inventory.NvidiaSmiProbe(
        inventory.NVIDIA_OK,
        stdout=(
            "0, GPU-a, NVIDIA H200, 143771, 580.65.06, 9.0\n"
            "1, GPU-b, NVIDIA L40S, 46068, 550.54.15, 8.9\n"
        ),
    ), env={}))
    assert profile.backend_candidates == ("cuda12", "vulkan", "cpu")


def test_a_driver_too_old_for_cuda12_is_reported_and_cuda_is_withheld():
    profile = inventory.collect_hardware(raw=raw_host(nvidia=inventory.NvidiaSmiProbe(
        inventory.NVIDIA_OK,
        stdout="0, GPU-a, NVIDIA A100-SXM4-80GB, 81920, 470.199.02, 8.0\n",
    ), env={}))
    assert "nvidia_driver_too_old" in codes(profile)
    assert "cuda12" not in profile.backend_candidates
    assert profile.backend_candidates == ("vulkan", "cpu")


def test_metal_is_offered_on_apple_silicon_only():
    mac = inventory.collect_hardware(raw=raw_host(
        system="Darwin", machine="arm64", os_release_text=None,
        cpuinfo_text=None, meminfo_text=None,
        nvidia=inventory.NvidiaSmiProbe(inventory.NVIDIA_ABSENT, detail="absent"),
        env={},
    ))
    assert mac.backend_candidates == ("metal", "cpu")
    assert mac.os == "macos"

    linux = inventory.collect_hardware(raw=raw_host(
        nvidia=inventory.NvidiaSmiProbe(inventory.NVIDIA_ABSENT, detail="absent"),
        env={},
    ))
    assert "metal" not in linux.backend_candidates


# ── VRAM exposée (§0.9) ───────────────────────────────────────────────────────

def test_exposed_vram_is_reported_as_the_only_truth():
    """
    §0.9 : `TOTAL_VRAM_GB=48.0` contre ~45,0 Go exposés par une L40S.

    Aucune heuristique sur le nom commercial (« L40S » ne porte aucun chiffre de
    VRAM) : on rapporte la valeur exposée et on désigne son usage.
    """
    profile = inventory.collect_hardware(raw=raw_host(env={"CUDA_VISIBLE_DEVICES": "0"}))
    assert profile.visible_vram_total_bytes == L40S_BYTES
    assert profile.visible_vram_total_bytes / 1024**3 == pytest.approx(45.0, abs=0.1)

    message = finding(profile, "vram_exposed_is_authoritative").message
    assert "45.0 Go" in message
    assert "TOTAL_VRAM_GB" in message


def test_no_vram_finding_when_nothing_is_exposed():
    profile = inventory.collect_hardware(raw=raw_host(env={"CUDA_VISIBLE_DEVICES": ""}))
    assert "vram_exposed_is_authoritative" not in codes(profile)
    # Contrôle positif : le constat existe bel et bien quand un GPU est exposé.
    assert "vram_exposed_is_authoritative" in codes(
        inventory.collect_hardware(raw=raw_host())
    )


# ── Analyseurs ────────────────────────────────────────────────────────────────

def test_nvidia_csv_parsing_is_defensive():
    parsed = inventory.parse_nvidia_smi_csv(
        "\n"
        "pas, du, tout, du, csv\n"
        "0, GPU-ok, NVIDIA L40S, 46068, 550.54.15, 8.9\n"
        "1, GPU-vide, , 46068, 550.54.15, 8.9\n"
        "2, GPU-nan, NVIDIA L40S, [N/A], 550.54.15, 8.9\n"
        "3, GPU-zero, NVIDIA L40S, 0, 550.54.15, 8.9\n"
        "x, GPU-idx, NVIDIA L40S, 46068, 550.54.15, 8.9\n"
    )
    assert [g.uuid for g in parsed] == ["GPU-ok", "GPU-idx"]
    assert parsed[0].vram_total_bytes == L40S_BYTES
    assert parsed[1].index is None


def test_os_release_parsing_handles_quotes_and_comments():
    assert inventory.parse_os_release(OS_RELEASE) == ("ubuntu", "24.04")
    assert inventory.parse_os_release("# rien\nID='debian'\n") == ("debian", "")
    assert inventory.parse_os_release("") == ("", "")


def test_meminfo_parsing_converts_kilobytes():
    total, available = inventory.parse_meminfo(MEMINFO)
    assert total == 528214596 * 1024
    assert available == 401234567 * 1024
    assert inventory.parse_meminfo("MemTotal: bruit\n") == (None, None)


def test_arm_cpuinfo_features_are_recognised():
    model, flags = inventory.parse_cpuinfo(
        "processor\t: 0\nFeatures\t: fp asimd asimdhp i8mm sve2 crc32\n"
        "CPU implementer\t: 0x41\n"
    )
    assert "asimd" in flags and "i8mm" in flags and "sve2" in flags
    assert "crc32" not in flags


# ── Profil injecté (`--hardware-profile`) ─────────────────────────────────────

def test_a_declared_profile_replaces_the_probe_and_says_it_is_declared():
    profile = inventory.load_hardware_profile(json.dumps(valid_profile_document()))
    assert profile.source == inventory.SOURCE_DECLARED
    assert profile.cpu_model == "AMD EPYC 9354"
    assert profile.visible_vram_total_bytes == L40S_BYTES
    assert profile.backend_candidates == ("cuda12", "vulkan", "cpu")
    assert "hardware_profile_declared" in codes(profile)
    assert profile.status == "warn", "un profil affirmé n'a pas la valeur d'une mesure"
    assert "déclaré" in inventory.to_plan_section(profile).summary


def test_cuda_visible_devices_also_filters_a_declared_profile():
    document = valid_profile_document(gpus=[
        {**valid_profile_document()["gpus"][0], "index": 0, "uuid": "GPU-a"},
        {**valid_profile_document()["gpus"][0], "index": 1, "uuid": "GPU-b"},
    ])
    profile = inventory.load_hardware_profile(
        json.dumps(document), env={"CUDA_VISIBLE_DEVICES": "1"}
    )
    assert profile.visible_vram_total_bytes == L40S_BYTES
    assert [g.visible for g in profile.gpus] == [False, True]


def test_malformed_json_is_refused_with_a_locatable_message():
    with pytest.raises(inventory.InventoryError) as excinfo:
        inventory.load_hardware_profile("{\n  \"os\": \n}")
    assert "ligne" in str(excinfo.value)
    assert "--hardware-profile" in str(excinfo.value)


@pytest.mark.parametrize(("overrides", "expected"), [
    ({"os": ""}, "os doit être une chaîne non vide"),
    ({"ram_total_bytes": 0}, "ram_total_bytes vaut 0"),
    ({"ram_available_bytes": 999 * 1024**3}, "dépasse ram_total_bytes"),
    ({"disk_available_bytes": -1}, "disk_available_bytes"),
    ({"cpu_flags": "avx2"}, "cpu_flags doit être une liste"),
    ({"backend_candidates": ["cuda99"]}, "backends inconnus"),
    ({"gpus": []}, "annonce un backend GPU"),
    ({"gpus": None}, "gpus doit être présent"),
])
def test_an_incoherent_declared_profile_is_refused_with_an_actionable_message(
    overrides, expected,
):
    """Un fichier d'entrée n'est pas une source de confiance."""
    with pytest.raises(inventory.InventoryError) as excinfo:
        inventory.load_hardware_profile(json.dumps(valid_profile_document(**overrides)))
    assert expected in str(excinfo.value)


def test_a_declared_gpu_without_vram_is_refused():
    document = valid_profile_document()
    document["gpus"][0]["vram_total_bytes"] = 0
    with pytest.raises(inventory.InventoryError) as excinfo:
        inventory.load_hardware_profile(json.dumps(document))
    assert "gpus[0].vram_total_bytes" in str(excinfo.value)


def test_duplicate_gpu_uuids_are_refused_because_they_double_the_budget():
    gpu = valid_profile_document()["gpus"][0]
    document = valid_profile_document(gpus=[gpu, dict(gpu, index=1)])
    with pytest.raises(inventory.InventoryError) as excinfo:
        inventory.load_hardware_profile(json.dumps(document))
    assert "uuid en double" in str(excinfo.value)


def test_a_declared_profile_carrying_a_secret_is_refused():
    document = valid_profile_document(cpu_model="EPYC hf_abcdefghijklmnopqrstuvwxyz")
    with pytest.raises(inventory.InventoryError) as excinfo:
        inventory.load_hardware_profile(json.dumps(document))
    message = str(excinfo.value)
    assert "secret" in message
    assert "hf_abcdefghijklmnopqrstuvwxyz" not in message, (
        "un rapport de fuite qui recopie le secret est lui-même une fuite"
    )


def test_validate_profile_document_accepts_a_gpu_less_host():
    document = valid_profile_document(gpus=[], backend_candidates=["cpu"])
    assert inventory.validate_profile_document(document) == ()
    # Contrôle positif : la même fonction sait bien produire des erreurs.
    assert inventory.validate_profile_document({}) != ()


# ── Non-divulgation ───────────────────────────────────────────────────────────

def test_the_hardware_section_leaks_nothing():
    section = inventory.to_plan_section(inventory.collect_hardware(raw=raw_host()))
    assert schema.find_secret_leaks(section.to_dict()) == ()

    # Contrôle positif : le détecteur inspecte réellement CETTE structure. Sans
    # lui, l'assertion ci-dessus resterait verte même si find_secret_leaks()
    # devenait aveugle aux sections.
    poisoned = section.to_dict()
    poisoned["data"]["cpu_model"] = "Xeon hf_abcdefghijklmnopqrstuvwxyz"
    assert schema.find_secret_leaks(poisoned) != ()


def test_rendering_a_plan_that_contains_the_section_stays_clean():
    section = inventory.to_plan_section(inventory.collect_hardware(raw=raw_host()))
    plan = schema.BootstrapPlan(
        generated_at="2026-07-31T09:00:00Z", mode="local", sections=(section,)
    )
    rendered = schema.render_human(plan)
    assert "Inventaire matériel" in rendered
    assert json.loads(schema.render_json(plan))["sections"][0]["name"] == "hardware"


# ── Sondes réelles : elles ne doivent jamais lever ────────────────────────────

def test_the_real_nvidia_probe_never_raises_on_a_host_without_the_tool():
    probe = inventory.probe_nvidia_smi(timeout=5.0)
    assert probe.outcome in (
        inventory.NVIDIA_OK, inventory.NVIDIA_ABSENT, inventory.NVIDIA_FAILED,
    )
    if probe.outcome != inventory.NVIDIA_OK:
        assert probe.detail, "un échec de sonde doit dire pourquoi"


def test_collecting_from_the_real_host_produces_a_valid_section(tmp_path):
    """Quelle que soit la machine de CI, la section reste structurellement saine."""
    profile = inventory.collect_hardware(
        options=inventory.InventoryOptions(models_dir=tmp_path, env={})
    )
    section = inventory.to_plan_section(profile)
    plan = schema.BootstrapPlan(
        generated_at="2026-07-31T09:00:00Z", mode="local", sections=(section,)
    )
    assert schema.validate_plan_dict(plan.to_dict()) == ()
    assert schema.find_secret_leaks(plan.to_dict()) == ()
