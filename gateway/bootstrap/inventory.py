"""
AUT-002 — inventaire matériel automatique : CPU, RAM, disque, GPU, backends.

Ce que produit ce module
------------------------
Un `HardwareProfile` conforme au contrat de données d'inventaire, projeté vers la section `hardware` du plan
de bootstrap. C'est le premier maillon du pipeline §5 : tout ce qui suit —
résolution du runtime (AUT-003), recommandation LLMfit (AUT-004), filtre
catalogue (AUT-005) — raisonne sur ce profil et sur rien d'autre.

Pourquoi tout est injectable
----------------------------
Une seule fonction de ce module touche l'hôte : `capture_host()`. Elle produit un
`RawHost`, instantané BRUT et non interprété (contenu de `/proc/cpuinfo`, texte
rendu par `nvidia-smi`, octets libres du volume…). Tout le reste — `_probe_cpu`,
`_probe_memory`, `_probe_disk`, `_probe_gpus`, `collect_hardware` — est une
fonction pure de ce `RawHost`.

Ce n'est pas de la coquetterie de testabilité : c'est la seule façon d'avoir la
même couverture sur un Mac sans GPU, dans une CI Linux sans pilote NVIDIA, et sur
la L40S de production. La technique est celle de `doctor.py` (sondes séparées des
contrôles, cf. `probe_nvidia_smi` / `check_gpu_inventory`), poussée un cran plus
loin : ici la frontière impure est unique et nommée.

`capture_host()` est SYNCHRONE, contrairement à `doctor.probe_nvidia_smi()`. Le
planificateur est une commande one-shot, jamais un chemin de requête ; et une
fonction synchrone reste appelable depuis un contexte async (`asyncio.to_thread`)
alors que l'inverse impose une boucle à tous les appelants.

Aucun repli silencieux (§6)
---------------------------
« Ne jamais basculer silencieusement d'un backend GPU vers CPU. » Un inventaire
qui rend `0 GPU, backend cpu` quand la sonde a échoué ferait exactement cela, et
l'installation paraîtrait réussie avec un TTFT inacceptable. Quatre issues sont
donc distinguées, avec des codes de constat distincts :

| Situation                                  | Code                      | Niveau | `backend_candidates` |
|--------------------------------------------|---------------------------|--------|----------------------|
| `nvidia-smi` répond, ≥ 1 GPU               | —                         | —      | `cudaXX`, `vulkan`, `cpu` |
| `nvidia-smi` absent du PATH                | `gpu_probe_unavailable`   | warn   | `cpu` (+ `metal`)    |
| `nvidia-smi` répond, 0 GPU                 | `gpu_absent`              | warn   | `cpu` (+ `metal`)    |
| `nvidia-smi` présent mais échoue/timeout   | `gpu_probe_failed`        | fail   | **vide**             |

La dernière ligne est le cœur de la règle. Un pilote installé mais cassé n'est
pas un hôte CPU : proposer `cpu` reviendrait à décider à la place de l'opérateur.
Une liste de candidats VIDE dit honnêtement « je ne sais pas quoi installer » et
force le résolveur de runtime à refuser plutôt qu'à se rabattre.

VRAM nominale contre VRAM exposée — décision assumée
-----------------------------------------------------
Un écart réel a été relevé : `TOTAL_VRAM_GB=48.0` posé par
`install.sh` contre ~45,0 Go réellement exposés par une L40S. L'item demandait
d'envisager un constat déclenché par l'écart au « nominal commercial déduit du
nom du modèle ».

**Cette heuristique n'est pas implémentée, délibérément.** Le nom rendu par
`nvidia-smi` pour la carte qui a motivé l'item — `NVIDIA L40S` — ne contient
aucun chiffre de VRAM ; `RTX 4090` en contient un qui n'en est pas un (4090 est
un numéro de gamme, pas 4090 Go) ; seuls quelques SKU datacenter portent leur
capacité (`A100-SXM4-80GB`). Une heuristique qui échoue sur son cas fondateur et
qui produit un faux positif sur le GPU grand public le plus répandu n'est pas un
contrôle, c'est du bruit — et un avertissement bruyant est un avertissement que
l'opérateur apprend à ignorer.

Ce module rapporte donc **la valeur exposée comme seule vérité**, en octets, et
émet à la place un constat `info` `vram_exposed_is_authoritative` qui nomme la
valeur à écrire dans `TOTAL_VRAM_GB`. Le contrôle de cohérence entre valeur
CONFIGURÉE et valeur exposée existe déjà, au bon endroit et avec les deux
grandeurs en main : `doctor.check_vram_detected` (code
`total_vram_gb_overstated`). Le dupliquer ici avec une seule des deux grandeurs
serait moins juste, pas plus.

Non-divulgation
---------------
`PlanSection.data` est destiné à être copié dans un ticket. Aucun champ produit
ici ne peut porter de secret : ce sont des grandeurs matérielles et des chaînes
de modèle. Le seul chemin d'entrée non fiable est le fichier `--hardware-profile`,
et il est filtré à la frontière par `validate_profile_document()`, qui rejette un
document dont une valeur ressemble à un secret (`schema.find_secret_leaks`).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence

from . import schema

SECTION_NAME = schema.SECTION_HARDWARE
SECTION_VERSION = 1


class InventoryError(Exception):
    """Entrée d'inventaire inexploitable — profil injecté invalide, notamment."""


# ── Backends ──────────────────────────────────────────────────────────────────
#
# Ensemble FERMÉ, sur le modèle de `schema.PLAN_ACTIONS` : un profil injecté ne
# doit pas pouvoir nommer un backend que le résolveur de runtime ne sait pas
# installer. Un nom libre y passerait pour légitime jusqu'à l'échec du build.

BACKEND_CUDA_12 = "cuda12"
BACKEND_CUDA_13 = "cuda13"
BACKEND_ROCM = "rocm"
BACKEND_VULKAN = "vulkan"
BACKEND_METAL = "metal"
BACKEND_CPU = "cpu"

KNOWN_BACKENDS: frozenset[str] = frozenset({
    BACKEND_CUDA_12,
    BACKEND_CUDA_13,
    BACKEND_ROCM,
    BACKEND_VULKAN,
    BACKEND_METAL,
    BACKEND_CPU,
})

VENDOR_NVIDIA = "nvidia"

# Versions minimales de pilote NVIDIA par branche CUDA (Linux, x86_64). En
# dessous de `_CUDA_12_MIN_DRIVER`, aucun binaire CUDA moderne ne démarre : le
# proposer produirait un « backend installé, modèle qui ne charge jamais ».
_CUDA_12_MIN_DRIVER = 525
_CUDA_13_MIN_DRIVER = 580

# Volume par défaut : celui où atterrissent les GGUF. `install.sh` pose
# `MODELS_DIR="${LLM_GATEWAY_MODELS_DIR:-/models}"` ; mesurer `/` à la place
# donnerait une réponse juste sur une machine mono-volume et fausse sur toutes
# les autres — c'est-à-dire sur celles où la question se pose.
DEFAULT_MODELS_DIR = Path("/models")

# Requête à l'identique de `doctor._NVIDIA_SMI_QUERY`. Aucun secret ne transite par argv.
NVIDIA_SMI_QUERY = "--query-gpu=index,uuid,name,memory.total,driver_version,compute_cap"
NVIDIA_SMI_FORMAT = "--format=csv,noheader,nounits"

NVIDIA_OK = "ok"
NVIDIA_ABSENT = "absent"
NVIDIA_FAILED = "failed"

SOURCE_PROBE = "probe"
SOURCE_DECLARED = "declared"

# Drapeaux CPU conservés. Un x86_64 récent en expose plus de deux cents : les
# recopier tous rendrait le plan illisible pour l'humain qui doit le relire avant
# application, sans rien apprendre à personne. Seuls figurent ici les drapeaux
# qui font choisir un binaire llama.cpp plutôt qu'un autre (AUT-003).
_RELEVANT_CPU_FLAGS: tuple[str, ...] = (
    # x86_64
    "sse4_2", "avx", "avx2", "f16c", "fma",
    "avx512f", "avx512bw", "avx512vl", "avx512_vnni", "avx512_bf16",
    "amx_tile", "amx_int8", "amx_bf16",
    # aarch64
    "neon", "asimd", "asimdhp", "asimddp", "dotprod", "i8mm", "sve", "sve2", "fphp",
)
_RELEVANT_CPU_FLAGS_SET: frozenset[str] = frozenset(_RELEVANT_CPU_FLAGS)

_DRIVER_MAJOR_RE = re.compile(r"^\s*(\d+)")

# Champs obligatoires d'un profil injecté, avec le type attendu. La liste est
# celle de §5 : un profil amputé n'est pas « partiellement utile », il fait
# raisonner les étages suivants sur du vide.
_PROFILE_STRING_FIELDS: tuple[str, ...] = ("os", "os_version", "arch", "cpu_model")
_PROFILE_SIZE_FIELDS: tuple[str, ...] = (
    "ram_total_bytes", "ram_available_bytes", "disk_available_bytes",
)
_GPU_STRING_FIELDS: tuple[str, ...] = (
    "uuid", "vendor", "model", "driver_version", "compute_capability",
)


# ── Types du profil ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GpuDevice:
    """
    Un GPU tel que rapporté par la sonde, ou déclaré par un profil injecté.

    `visible` dit si le device est exposé par `CUDA_VISIBLE_DEVICES` : c'est
    l'unique critère qui décide s'il compte dans le budget VRAM (§5).
    """
    index: int | None
    uuid: str
    vendor: str
    model: str
    vram_total_bytes: int
    driver_version: str
    compute_capability: str
    visible: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "uuid": self.uuid,
            "vendor": self.vendor,
            "model": self.model,
            "vram_total_bytes": self.vram_total_bytes,
            "driver_version": self.driver_version,
            "compute_capability": self.compute_capability,
            "visible": self.visible,
        }

    @property
    def driver_major(self) -> int | None:
        match = _DRIVER_MAJOR_RE.match(self.driver_version or "")
        return int(match.group(1)) if match else None


@dataclass(frozen=True)
class HardwareProfile:
    """
    Profil matériel complet. Immuable : un inventaire relu ne change plus.

    `gpus` liste TOUS les GPU de l'hôte, chacun portant son drapeau `visible`.
    Les agrégats de VRAM (`visible_vram_total_bytes`) ne comptent, eux, que les
    devices exposés : c'est la demande explicite de §5, et c'est ce qui évite
    d'annoncer 96 Go sur une machine bi-GPU dont une seule carte est allouée.
    """
    os: str
    os_version: str
    arch: str
    cpu_model: str
    cpu_flags: tuple[str, ...]
    ram_total_bytes: int
    ram_available_bytes: int
    disk_available_bytes: int
    disk_path: str
    gpus: tuple[GpuDevice, ...]
    backend_candidates: tuple[str, ...]
    source: str = SOURCE_PROBE
    findings: tuple[schema.Finding, ...] = field(default_factory=tuple)

    @property
    def visible_gpus(self) -> tuple[GpuDevice, ...]:
        return tuple(g for g in self.gpus if g.visible)

    @property
    def visible_vram_total_bytes(self) -> int:
        return sum(g.vram_total_bytes for g in self.visible_gpus)

    @property
    def status(self) -> schema.SectionStatus:
        """Statut dérivé des constats : un `fail` domine, un `warn` dégrade."""
        return schema.worst_status(
            "ok", *(_LEVEL_TO_STATUS[f.level] for f in self.findings)
        )

    def to_dict(self) -> dict[str, Any]:
        """Document §5, sérialisable JSON. Les clés du contrat sont littérales."""
        return {
            "os": self.os,
            "os_version": self.os_version,
            "arch": self.arch,
            "cpu_model": self.cpu_model,
            "cpu_flags": list(self.cpu_flags),
            "ram_total_bytes": self.ram_total_bytes,
            "ram_available_bytes": self.ram_available_bytes,
            "disk_available_bytes": self.disk_available_bytes,
            "disk_path": self.disk_path,
            "gpus": [g.to_dict() for g in self.gpus],
            "backend_candidates": list(self.backend_candidates),
            "visible_gpu_count": len(self.visible_gpus),
            "visible_vram_total_bytes": self.visible_vram_total_bytes,
            "source": self.source,
        }


_LEVEL_TO_STATUS: dict[str, str] = {"info": "ok", "warn": "warn", "fail": "fail"}


# ── Instantané brut de l'hôte ─────────────────────────────────────────────────

@dataclass(frozen=True)
class NvidiaSmiProbe:
    """
    Résultat brut de la sonde NVIDIA, non interprété.

    `outcome` sépare les trois cas que la suite du module doit distinguer :
    `absent` (binaire introuvable), `failed` (présent mais inutilisable) et `ok`
    (sortie exploitable, éventuellement vide).
    """
    outcome: Literal["ok", "absent", "failed"]
    stdout: str = ""
    detail: str = ""


@dataclass(frozen=True)
class RawHost:
    """
    Instantané BRUT de l'hôte : des textes et des nombres, aucune interprétation.

    C'est la totalité de ce que l'inventaire lit du monde extérieur. Un test qui
    construit un `RawHost` reproduit donc n'importe quelle machine — y compris
    celles auxquelles la CI n'a pas accès.
    """
    system: str = ""
    release: str = ""
    machine: str = ""
    os_release_text: str | None = None
    cpuinfo_text: str | None = None
    cpu_brand: str | None = None
    cpu_features: str | None = None
    meminfo_text: str | None = None
    ram_total_bytes: int | None = None
    ram_available_bytes: int | None = None
    disk_available_bytes: int | None = None
    disk_path: str = str(DEFAULT_MODELS_DIR)
    disk_error: str | None = None
    nvidia: NvidiaSmiProbe = field(default_factory=lambda: NvidiaSmiProbe(NVIDIA_ABSENT))
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class InventoryOptions:
    """Ce que l'appelant peut décider ; tout le reste est mesuré."""
    models_dir: Path = DEFAULT_MODELS_DIR
    nvidia_smi_timeout: float = 5.0
    env: Mapping[str, str] | None = None


# ── Analyseurs purs ───────────────────────────────────────────────────────────

def parse_os_release(text: str) -> tuple[str, str]:
    """
    Extrait `(ID, VERSION_ID)` d'un `/etc/os-release`. Ne lève jamais.

    Retourne des chaînes vides pour ce qui manque : un champ absent est une
    information (« distribution non identifiable »), pas une exception.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        values[key.strip()] = raw.strip().strip('"').strip("'")
    return values.get("ID", ""), values.get("VERSION_ID", "")


def parse_cpuinfo(text: str) -> tuple[str, tuple[str, ...]]:
    """
    Extrait `(modèle, drapeaux pertinents)` d'un `/proc/cpuinfo`. Ne lève jamais.

    Le premier cœur suffit : un hôte à cœurs hétérogènes reste hors du périmètre
    de ce jalon, et prétendre le contraire donnerait un profil faux plutôt
    qu'incomplet. `flags` est la clé x86, `Features` la clé aarch64.
    """
    model = ""
    flags: tuple[str, ...] = ()
    for line in text.splitlines():
        key, sep, raw = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        value = raw.strip()
        if not model and key in ("model name", "hardware", "cpu"):
            model = value
        if not flags and key in ("flags", "features"):
            flags = _filter_cpu_flags(value.split())
    return model, flags


def _filter_cpu_flags(tokens: Iterable[str]) -> tuple[str, ...]:
    """Ne garde que les drapeaux qui font choisir un binaire, dans un ordre stable."""
    present = {t.strip().lower() for t in tokens if t.strip()}
    return tuple(f for f in _RELEVANT_CPU_FLAGS if f in present & _RELEVANT_CPU_FLAGS_SET)


def parse_meminfo(text: str) -> tuple[int | None, int | None]:
    """
    Extrait `(MemTotal, MemAvailable)` en octets d'un `/proc/meminfo`.

    `MemAvailable` — et non `MemFree` — est la seule estimation qui tienne compte
    du cache récupérable. Utiliser `MemFree` sur un hôte qui tourne depuis un mois
    ferait conclure à quelques centaines de Mo disponibles sur une machine à
    768 Go, et bloquerait tout chargement de modèle sans raison.
    """
    total: int | None = None
    available: int | None = None
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        if not sep:
            continue
        key = key.strip()
        if key not in ("MemTotal", "MemAvailable"):
            continue
        fields = rest.split()
        if not fields:
            continue
        try:
            value = int(fields[0])
        except ValueError:
            continue
        if len(fields) > 1 and fields[1].lower() == "kb":
            value *= 1024
        if key == "MemTotal":
            total = value
        else:
            available = value
    return total, available


def parse_nvidia_smi_csv(text: str) -> list[GpuDevice]:
    """
    Parse la sortie `--format=csv,noheader,nounits`. Ne lève jamais.

    `memory.total` est en MiB avec `nounits` ; le profil §5 exige des octets, la
    conversion est faite ici pour qu'aucun consommateur n'ait à deviner l'unité.
    Une ligne inexploitable est ignorée, pas devinée : un GPU dont la VRAM ne se
    lit pas ne doit pas entrer dans le budget avec une valeur inventée.
    """
    gpus: list[GpuDevice] = []
    for line in text.splitlines():
        fields = [f.strip() for f in line.split(",")]
        if len(fields) < 6 or not fields[2]:
            continue
        try:
            index: int | None = int(fields[0])
        except ValueError:
            index = None
        try:
            memory_mib = float(fields[3])
        except ValueError:
            continue
        if memory_mib <= 0:
            continue
        gpus.append(GpuDevice(
            index=index,
            uuid=fields[1],
            vendor=VENDOR_NVIDIA,
            model=fields[2],
            vram_total_bytes=int(memory_mib * 1024 * 1024),
            driver_version=fields[4],
            compute_capability=fields[5],
        ))
    return gpus


# ── CUDA_VISIBLE_DEVICES ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class VisibleSelection:
    """Devices réellement exposés, et ce qui a empêché d'en exposer davantage."""
    devices: tuple[GpuDevice, ...]
    unresolved: tuple[str, ...]
    declared: bool


def resolve_visible_devices(
    gpus: Sequence[GpuDevice],
    raw: str | None,
) -> VisibleSelection:
    """
    Restreint l'inventaire aux devices que `CUDA_VISIBLE_DEVICES` expose.

    Trois formes, sémantiques distinctes — les confondre est la source d'erreur
    classique, et `doctor.visible_devices()` les confond parce que sa source est
    une chaîne de configuration incapable de porter la nuance :

    - **variable absente** (`None`) : CUDA expose TOUS les GPU. C'est le défaut ;
    - **variable présente et vide** (`""`) : CUDA n'expose AUCUN GPU. Sur un hôte
      qui en possède, c'est une erreur de configuration, pas un hôte CPU ;
    - **liste** : index (`0,1`) ou UUID (`GPU-…`), les deux acceptés.

    La troncature est reproduite fidèlement : CUDA n'expose que les devices
    situés AVANT le premier jeton invalide, et ignore tout ce qui suit. Un
    inventaire qui résoudrait quand même les jetons suivants annoncerait de la
    VRAM que le runtime ne verra jamais.
    """
    if raw is None:
        return VisibleSelection(tuple(gpus), (), declared=False)

    tokens = [t.strip() for t in raw.split(",")]
    tokens = [t for t in tokens if t]
    if not tokens:
        return VisibleSelection((), (), declared=True)

    visible: list[GpuDevice] = []
    for token in tokens:
        if token.isdigit():
            match = next((g for g in gpus if g.index == int(token)), None)
        else:
            match = next((g for g in gpus if g.uuid == token), None)
        if match is None:
            # Troncature CUDA : tout ce qui suit un jeton invalide est perdu.
            return VisibleSelection(tuple(visible), (token,), declared=True)
        if match not in visible:
            visible.append(match)
    return VisibleSelection(tuple(visible), (), declared=True)


# ── Sondes (impures, isolées) ─────────────────────────────────────────────────

def probe_nvidia_smi(timeout: float = 5.0) -> NvidiaSmiProbe:
    """
    Interroge `nvidia-smi` avec la requête de §5. Ne lève jamais.

    Distingue « binaire absent » (hôte plausiblement sans GPU) de « binaire
    présent qui échoue » (pilote cassé, conteneur sans `--gpus`, GPU en erreur) :
    les deux n'appellent pas la même conduite, et les confondre est précisément
    le repli silencieux que §6 interdit. Aucun secret ne transite par argv.
    """
    try:
        proc = subprocess.run(  # noqa: S603 - argv fixe, aucune entrée utilisateur
            ["nvidia-smi", NVIDIA_SMI_QUERY, NVIDIA_SMI_FORMAT],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return NvidiaSmiProbe(NVIDIA_ABSENT, detail="nvidia-smi introuvable dans le PATH")
    except subprocess.TimeoutExpired:
        return NvidiaSmiProbe(
            NVIDIA_FAILED,
            detail=f"nvidia-smi n'a pas répondu en {timeout:.0f} s",
        )
    except OSError as exc:
        return NvidiaSmiProbe(
            NVIDIA_FAILED, detail=f"nvidia-smi non exécutable ({type(exc).__name__})"
        )

    stdout = (proc.stdout or b"").decode("utf-8", errors="replace")
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        return NvidiaSmiProbe(
            NVIDIA_FAILED,
            stdout=stdout,
            detail=f"nvidia-smi a échoué (code {proc.returncode}) : {stderr[:200]}",
        )
    return NvidiaSmiProbe(NVIDIA_OK, stdout=stdout)


def _read_text(path: Path) -> str | None:
    """Lit un fichier système, ou `None` s'il n'existe pas / n'est pas lisible."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _sysconf_bytes(pages_name: str) -> int | None:
    """Octets dérivés de `sysconf`, ou `None` sur une plateforme qui l'ignore."""
    try:
        pages = os.sysconf(pages_name)
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return None
    if not pages or not page_size or pages < 0 or page_size < 0:
        return None
    return int(pages) * int(page_size)


def capture_host(options: InventoryOptions | None = None) -> RawHost:
    """
    **Seule** fonction du module qui touche l'hôte. Ne lève jamais.

    Elle ne juge rien : elle ramène des textes bruts et des nombres. Toute
    l'interprétation vit dans les fonctions pures qui la suivent, ce qui rend
    l'inventaire entièrement rejouable en test.
    """
    options = options or InventoryOptions()
    uname = os.uname() if hasattr(os, "uname") else None
    env: Mapping[str, str] = options.env if options.env is not None else dict(os.environ)

    disk_available: int | None = None
    disk_error: str | None = None
    try:
        disk_available = shutil.disk_usage(options.models_dir).free
    except OSError as exc:
        disk_error = f"{type(exc).__name__} sur {options.models_dir}"

    return RawHost(
        system=(uname.sysname if uname else ""),
        release=(uname.release if uname else ""),
        machine=(uname.machine if uname else ""),
        os_release_text=_read_text(Path("/etc/os-release")),
        cpuinfo_text=_read_text(Path("/proc/cpuinfo")),
        cpu_brand=None,
        cpu_features=None,
        meminfo_text=_read_text(Path("/proc/meminfo")),
        ram_total_bytes=_sysconf_bytes("SC_PHYS_PAGES"),
        ram_available_bytes=_sysconf_bytes("SC_AVPHYS_PAGES"),
        disk_available_bytes=disk_available,
        disk_path=str(options.models_dir),
        disk_error=disk_error,
        nvidia=probe_nvidia_smi(options.nvidia_smi_timeout),
        env=env,
    )


# ── Sondes pures ──────────────────────────────────────────────────────────────

def _probe_os(raw: RawHost) -> tuple[str, str, str, list[schema.Finding]]:
    """`(os, os_version, arch)` — le trio qui décide de la variante de binaire."""
    findings: list[schema.Finding] = []
    system = (raw.system or "").strip()
    arch = (raw.machine or "").strip()

    os_id, os_version = "", ""
    if raw.os_release_text:
        os_id, os_version = parse_os_release(raw.os_release_text)
    if not os_id:
        os_id = {"darwin": "macos"}.get(system.lower(), system.lower())
    if not os_version:
        os_version = (raw.release or "").strip()

    if not os_id or not arch:
        findings.append(schema.Finding(
            code="os_identification_incomplete",
            level="warn",
            message=(
                "Système ou architecture non identifiables (`/etc/os-release` "
                "absent et `uname` muet). La résolution du runtime ne pourra pas "
                "choisir de variante : fournissez --hardware-profile."
            ),
        ))
    return os_id, os_version, arch, findings


def _probe_cpu(raw: RawHost) -> tuple[str, tuple[str, ...], list[schema.Finding]]:
    """
    `(modèle, drapeaux)`. Les drapeaux sont filtrés (cf. `_RELEVANT_CPU_FLAGS`).

    L'absence de drapeaux n'est pas anodine : sans `avx2`, la variante CPU de
    `llama.cpp` change et les performances avec. On le dit plutôt que de laisser
    une liste vide passer pour « ce CPU n'a rien ».
    """
    findings: list[schema.Finding] = []
    model, flags = "", ()
    if raw.cpuinfo_text:
        model, flags = parse_cpuinfo(raw.cpuinfo_text)
    if not model and raw.cpu_brand:
        model = raw.cpu_brand.strip()
    if not flags and raw.cpu_features:
        flags = _filter_cpu_flags(raw.cpu_features.split())

    if not model:
        model = "inconnu"
        findings.append(schema.Finding(
            code="cpu_model_unknown",
            level="warn",
            message=(
                "Modèle de CPU non lisible (`/proc/cpuinfo` absent). Le profil "
                "reste exploitable, mais aucune variante CPU optimisée ne pourra "
                "être justifiée."
            ),
        ))
    if not flags:
        findings.append(schema.Finding(
            code="cpu_flags_unavailable",
            level="warn",
            message=(
                "Jeux d'instructions du CPU non détectables : impossible de "
                "confirmer AVX2/AVX-512. La liste vide signifie « non mesuré », "
                "pas « absent » — ne l'interprétez pas comme un CPU sans SIMD."
            ),
        ))
    return model, flags, findings


def _probe_memory(raw: RawHost) -> tuple[int, int, list[schema.Finding]]:
    """`(RAM totale, RAM disponible)` en octets, avec la franchise sur la source."""
    findings: list[schema.Finding] = []
    total: int | None = None
    available: int | None = None
    if raw.meminfo_text:
        total, available = parse_meminfo(raw.meminfo_text)

    approximated = False
    if total is None:
        total = raw.ram_total_bytes
    if available is None:
        available = raw.ram_available_bytes
        approximated = available is not None

    if not total:
        total = 0
        findings.append(schema.Finding(
            code="ram_total_unknown",
            level="fail",
            message=(
                "RAM totale non mesurable : aucun modèle ne peut être "
                "dimensionné sans elle. Fournissez --hardware-profile."
            ),
        ))
    if available is None:
        available = 0
        findings.append(schema.Finding(
            code="ram_available_unknown",
            level="warn",
            message=(
                "RAM disponible non mesurable ; seule la RAM totale sera "
                "utilisée pour dimensionner, ce qui surestime la marge réelle."
            ),
        ))
    elif approximated:
        findings.append(schema.Finding(
            code="ram_available_approximate",
            level="info",
            message=(
                "RAM disponible dérivée de `sysconf(SC_AVPHYS_PAGES)`, qui ne "
                "compte que les pages libres : la valeur SOUS-estime le "
                "récupérable (`MemAvailable` de Linux n'est pas disponible ici)."
            ),
        ))
    return int(total), int(available), findings


def _probe_disk(raw: RawHost) -> tuple[int, str, list[schema.Finding]]:
    """
    Octets libres sur le volume où atterriront les GGUF.

    Mesurer `/` en dur donnerait la bonne réponse sur une machine mono-volume et
    une réponse fausse partout ailleurs — or `install.sh` monte justement
    `/models` à part sur les hôtes qui ont de la place.
    """
    findings: list[schema.Finding] = []
    if raw.disk_available_bytes is None:
        findings.append(schema.Finding(
            code="disk_unreadable",
            level="fail",
            message=(
                f"Espace libre illisible sur « {raw.disk_path} » "
                f"({raw.disk_error or 'volume absent'}). Aucun téléchargement ne "
                "peut être planifié sans connaître la place disponible : créez le "
                "répertoire ou pointez --models-dir sur le bon volume."
            ),
        ))
        return 0, raw.disk_path, findings
    return int(raw.disk_available_bytes), raw.disk_path, findings


def _probe_gpus(raw: RawHost) -> tuple[tuple[GpuDevice, ...], bool, list[schema.Finding]]:
    """
    `(GPU marqués visibles, sonde exploitable, constats)`.

    Le booléen retourné n'est pas « il y a des GPU » mais « la sonde a conclu » :
    c'est lui qui autorise, ou non, à proposer un backend. Un `False` conduit à
    une liste de candidats VIDE, jamais à un repli sur CPU (§6).
    """
    findings: list[schema.Finding] = []
    probe = raw.nvidia

    if probe.outcome == NVIDIA_FAILED:
        findings.append(schema.Finding(
            code="gpu_probe_failed",
            level="fail",
            message=(
                f"`nvidia-smi` est présent mais inutilisable : {probe.detail}. "
                "Un pilote installé et cassé n'est PAS un hôte sans GPU : aucun "
                "backend n'est proposé tant que la sonde n'aboutit pas. Vérifiez "
                "le module noyau (`modprobe nvidia`), la correspondance "
                "pilote/bibliothèques, et l'exposition des devices au conteneur."
            ),
        ))
        return (), False, findings

    if probe.outcome == NVIDIA_ABSENT:
        findings.append(schema.Finding(
            code="gpu_probe_unavailable",
            level="warn",
            message=(
                f"Sonde GPU impossible : {probe.detail}. L'hôte est traité comme "
                "dépourvu de GPU NVIDIA — légitime sur un poste de développement "
                "ou un orchestrateur de cluster, anormal sur un nœud "
                "d'inférence, où seul un backend CPU pourra être proposé."
            ),
        ))
        return (), True, findings

    gpus = parse_nvidia_smi_csv(probe.stdout)
    if not gpus:
        findings.append(schema.Finding(
            code="gpu_absent",
            level="warn",
            message=(
                "`nvidia-smi` répond mais ne rapporte aucun GPU exploitable : "
                "pilote chargé sans carte visible (conteneur sans `--gpus`, ou "
                "carte en erreur). Seul un backend CPU pourra être proposé."
            ),
        ))
        return (), True, findings

    marked, selection_findings = _apply_visibility(gpus, raw.env)
    findings.extend(selection_findings)
    return marked, True, findings


def _apply_visibility(
    gpus: Sequence[GpuDevice],
    env: Mapping[str, str],
) -> tuple[tuple[GpuDevice, ...], list[schema.Finding]]:
    """Marque chaque GPU selon `CUDA_VISIBLE_DEVICES` et rapporte les anomalies."""
    findings: list[schema.Finding] = []
    if not gpus:
        # Sans GPU, la variable n'a rien à filtrer : en parler ajouterait du
        # bruit là où le constat utile (`gpu_absent`) est déjà émis.
        return (), findings
    raw_value = env.get("CUDA_VISIBLE_DEVICES")
    selection = resolve_visible_devices(gpus, raw_value)
    visible_uuids = {g.uuid for g in selection.devices}
    marked = tuple(
        GpuDevice(**{**g.to_dict(), "visible": g.uuid in visible_uuids})
        for g in gpus
    )

    if not selection.declared:
        findings.append(schema.Finding(
            code="cuda_visible_devices_unset",
            level="info",
            message=(
                f"CUDA_VISIBLE_DEVICES n'est pas définie : les {len(gpus)} GPU de "
                "l'hôte sont comptés. `install.sh` la renseigne — si le service "
                "final ne doit en voir qu'une partie, le budget VRAM planifié "
                "ici sera surévalué."
            ),
        ))
    elif selection.unresolved:
        findings.append(schema.Finding(
            code="cuda_visible_devices_invalid",
            level="fail",
            message=(
                f"CUDA_VISIBLE_DEVICES={raw_value!r} désigne un device "
                f"inexistant ({selection.unresolved[0]}) ; CUDA n'expose que les "
                f"devices situés AVANT lui, soit {len(selection.devices)} sur "
                f"{len(gpus)}. Corrigez la variable : index présents = "
                f"{', '.join(str(g.index) for g in gpus)}."
            ),
        ))
    elif not selection.devices:
        findings.append(schema.Finding(
            code="cuda_visible_devices_empty",
            level="fail",
            message=(
                f"CUDA_VISIBLE_DEVICES est définie et vide alors que {len(gpus)} "
                "GPU sont présents : aucun device ne sera exposé aux "
                "llama-server et tout chargement échouera. Ce n'est pas un hôte "
                "CPU, c'est une configuration incohérente — renseignez-la "
                "(ex. « 0 ») ou supprimez-la."
            ),
        ))
    elif len(selection.devices) < len(gpus):
        findings.append(schema.Finding(
            code="cuda_visible_devices_partial",
            level="info",
            message=(
                f"{len(selection.devices)}/{len(gpus)} GPU exposés par "
                f"CUDA_VISIBLE_DEVICES={raw_value!r} : le budget VRAM ne compte "
                "que ceux-là."
            ),
        ))
    return marked, findings


def _vram_findings(visible: Sequence[GpuDevice]) -> list[schema.Finding]:
    """
    Constat sur la VRAM réellement exposée (§0.9).

    Aucune comparaison au « nominal commercial » n'est tentée : voir le docstring
    du module pour le raisonnement. On énonce la valeur mesurée et l'usage
    attendu, ce qui suffit à fermer l'écart constaté (`TOTAL_VRAM_GB=48.0` posé à
    la main contre ~45,0 Go exposés) sans heuristique fragile.
    """
    if not visible:
        return []
    total_gb = sum(g.vram_total_bytes for g in visible) / 1024**3
    described = ", ".join(
        f"{g.model} {g.vram_total_bytes / 1024**3:.1f} Go" for g in visible
    )
    return [schema.Finding(
        code="vram_exposed_is_authoritative",
        level="info",
        message=(
            f"VRAM exposée par les devices visibles : {total_gb:.1f} Go "
            f"({described}). C'est cette valeur — et non la capacité commerciale "
            f"du modèle — qui doit être écrite dans TOTAL_VRAM_GB : l'écart "
            f"entre les deux ronge la marge de sécurité d'admission."
        ),
    )]


def _backend_candidates(
    arch: str,
    system: str,
    gpus: Sequence[GpuDevice],
    probe_conclusive: bool,
) -> tuple[tuple[str, ...], list[schema.Finding]]:
    """
    Backends plausibles, du plus spécifique au plus universel.

    Retourne une liste **vide** quand la sonde GPU n'a pas conclu. C'est le point
    dur de §6 : sans certitude sur le matériel, refuser de proposer vaut mieux
    que proposer `cpu`, qui produirait une installation « réussie » au TTFT
    inacceptable sur une machine pourtant équipée.
    """
    findings: list[schema.Finding] = []
    if not probe_conclusive:
        return (), findings

    candidates: list[str] = []
    visible = [g for g in gpus if g.visible]
    nvidia = [g for g in visible if g.vendor == VENDOR_NVIDIA]

    if nvidia:
        majors = [g.driver_major for g in nvidia if g.driver_major is not None]
        if not majors:
            findings.append(schema.Finding(
                code="nvidia_driver_version_unreadable",
                level="warn",
                message=(
                    "Version de pilote NVIDIA illisible : aucune branche CUDA ne "
                    "peut être garantie compatible, seul Vulkan est proposé côté "
                    "GPU."
                ),
            ))
        else:
            # La branche retenue doit convenir à TOUS les devices visibles : le
            # plus ancien pilote gouverne, sinon un GPU du lot ne chargera pas.
            oldest = min(majors)
            if oldest >= _CUDA_13_MIN_DRIVER:
                candidates.extend([BACKEND_CUDA_13, BACKEND_CUDA_12])
            elif oldest >= _CUDA_12_MIN_DRIVER:
                candidates.append(BACKEND_CUDA_12)
            else:
                findings.append(schema.Finding(
                    code="nvidia_driver_too_old",
                    level="warn",
                    message=(
                        f"Pilote NVIDIA {oldest} trop ancien pour CUDA 12 "
                        f"(minimum {_CUDA_12_MIN_DRIVER}) : aucune branche CUDA "
                        "n'est proposée. Mettez le pilote à jour, sinon "
                        "l'inférence GPU restera hors de portée."
                    ),
                ))
        candidates.append(BACKEND_VULKAN)

    if system.lower() == "darwin" and arch.lower() in ("arm64", "aarch64"):
        candidates.append(BACKEND_METAL)

    candidates.append(BACKEND_CPU)
    return tuple(dict.fromkeys(candidates)), findings


# ── Collecte ──────────────────────────────────────────────────────────────────

def collect_hardware(
    *,
    raw: RawHost | None = None,
    options: InventoryOptions | None = None,
) -> HardwareProfile:
    """
    Assemble le profil matériel. Ne lève jamais : elle rapporte.

    `raw` fourni → aucune I/O, la collecte est une fonction pure (c'est la voie
    des tests et de `--hardware-profile` recalculé). `raw` omis → `capture_host()`
    prend l'instantané avec `options`.

    Une panne de sonde ne fait pas échouer la collecte : elle produit un profil
    partiel ET les constats qui disent où est le trou. Un plan qui n'existe pas
    n'explique rien à l'opérateur ; un plan bloqué avec sa raison, si.
    """
    if raw is None:
        raw = capture_host(options)

    findings: list[schema.Finding] = []
    os_id, os_version, arch, os_findings = _probe_os(raw)
    findings.extend(os_findings)

    cpu_model, cpu_flags, cpu_findings = _probe_cpu(raw)
    findings.extend(cpu_findings)

    ram_total, ram_available, mem_findings = _probe_memory(raw)
    findings.extend(mem_findings)

    disk_bytes, disk_path, disk_findings = _probe_disk(raw)
    findings.extend(disk_findings)

    gpus, conclusive, gpu_findings = _probe_gpus(raw)
    findings.extend(gpu_findings)
    findings.extend(_vram_findings([g for g in gpus if g.visible]))

    backends, backend_findings = _backend_candidates(arch, raw.system, gpus, conclusive)
    findings.extend(backend_findings)

    return HardwareProfile(
        os=os_id,
        os_version=os_version,
        arch=arch,
        cpu_model=cpu_model,
        cpu_flags=cpu_flags,
        ram_total_bytes=ram_total,
        ram_available_bytes=ram_available,
        disk_available_bytes=disk_bytes,
        disk_path=disk_path,
        gpus=gpus,
        backend_candidates=backends,
        source=SOURCE_PROBE,
        findings=schema.merge_findings(findings),
    )


# ── Profil injecté (`--hardware-profile`) ─────────────────────────────────────

def validate_profile_document(document: Any) -> tuple[str, ...]:
    """
    Contrôle un profil matériel fourni par fichier. Retourne les erreurs.

    Un fichier `--hardware-profile` est une entrée NON FIABLE, au même titre
    qu'un corps de requête : il vient d'un opérateur pressé, d'un copier-coller
    ou d'un générateur maison. Le valider ne relève pas de la défiance mais du
    fait qu'un profil incohérent — RAM disponible supérieure à la RAM totale,
    backend `cuda12` sans le moindre GPU — produirait un plan qui semble sain et
    qui échoue à l'exécution, quand la cause n'est plus rattachable.

    Chaque message désigne le champ fautif par son chemin et dit quoi faire,
    comme `schema.validate_plan_dict()`.
    """
    errors: list[str] = []
    if not isinstance(document, dict):
        return (
            f"le profil matériel doit être un objet JSON, reçu "
            f"{type(document).__name__}",
        )

    for key in _PROFILE_STRING_FIELDS:
        value = document.get(key)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{key} doit être une chaîne non vide (reçu {value!r})")

    flags = document.get("cpu_flags", [])
    if not isinstance(flags, list) or not all(isinstance(f, str) for f in flags):
        errors.append("cpu_flags doit être une liste de chaînes (ex. [\"avx2\"])")

    sizes: dict[str, int] = {}
    for key in _PROFILE_SIZE_FIELDS:
        value = document.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            errors.append(f"{key} doit être un entier d'octets >= 0 (reçu {value!r})")
        else:
            sizes[key] = value

    if sizes.get("ram_total_bytes") == 0:
        errors.append(
            "ram_total_bytes vaut 0 : un profil sans RAM ne permet de "
            "dimensionner aucun modèle — renseignez la RAM physique en octets"
        )
    total = sizes.get("ram_total_bytes")
    available = sizes.get("ram_available_bytes")
    if total is not None and available is not None and available > total:
        errors.append(
            f"ram_available_bytes ({available}) dépasse ram_total_bytes ({total}) "
            "— profil incohérent, vérifiez les unités (octets, pas Go)"
        )

    errors.extend(_validate_profile_gpus(document.get("gpus")))
    errors.extend(_validate_profile_backends(document))
    errors.extend(
        f"{leak} — un profil matériel ne contient jamais de secret"
        for leak in schema.find_secret_leaks(document)
    )
    return tuple(errors)


def _validate_profile_gpus(gpus: Any) -> list[str]:
    errors: list[str] = []
    if gpus is None:
        return ["gpus doit être présent (liste vide si l'hôte n'a pas de GPU)"]
    if not isinstance(gpus, list):
        return ["gpus doit être une liste"]

    seen: set[str] = set()
    for index, gpu in enumerate(gpus):
        path = f"gpus[{index}]"
        if not isinstance(gpu, dict):
            errors.append(f"{path} doit être un objet")
            continue
        for key in _GPU_STRING_FIELDS:
            value = gpu.get(key)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{path}.{key} doit être une chaîne non vide")
        uuid = gpu.get("uuid")
        if isinstance(uuid, str) and uuid in seen:
            errors.append(
                f"{path}.uuid en double : {uuid!r} — deux entrées pour le même "
                "device doubleraient le budget VRAM"
            )
        elif isinstance(uuid, str):
            seen.add(uuid)
        vram = gpu.get("vram_total_bytes")
        if not isinstance(vram, int) or isinstance(vram, bool) or vram <= 0:
            errors.append(
                f"{path}.vram_total_bytes doit être un entier d'octets > 0 "
                f"(reçu {vram!r})"
            )
        idx = gpu.get("index")
        if idx is not None and (not isinstance(idx, int) or isinstance(idx, bool)):
            errors.append(f"{path}.index doit être un entier ou null")
    return errors


def _validate_profile_backends(document: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    backends = document.get("backend_candidates")
    if not isinstance(backends, list) or not all(isinstance(b, str) for b in backends):
        return ["backend_candidates doit être une liste de chaînes"]

    unknown = [b for b in backends if b not in KNOWN_BACKENDS]
    if unknown:
        errors.append(
            f"backend_candidates contient des backends inconnus "
            f"({', '.join(unknown)}) ; attendu parmi {sorted(KNOWN_BACKENDS)}"
        )

    gpus = document.get("gpus")
    gpu_count = len(gpus) if isinstance(gpus, list) else 0
    gpu_backends = [
        b for b in backends
        if b in (BACKEND_CUDA_12, BACKEND_CUDA_13, BACKEND_ROCM, BACKEND_VULKAN)
    ]
    if gpu_backends and gpu_count == 0:
        errors.append(
            f"backend_candidates annonce un backend GPU ({', '.join(gpu_backends)}) "
            "alors que gpus est vide — décrivez le GPU ou retirez le backend, "
            "sinon le plan téléchargera un runtime qui ne démarrera pas"
        )
    return errors


def load_hardware_profile(
    text: str,
    *,
    origin: str = "--hardware-profile",
    env: Mapping[str, str] | None = None,
) -> HardwareProfile:
    """
    Charge un profil matériel déclaré, à la place de la sonde (§5).

    Existe pour les VM, le passthrough et les hôtes où les outils constructeur
    échouent — les cas où sonder rend une réponse fausse plutôt qu'aucune.

    Le profil obtenu porte toujours un constat `warn` : il est DÉCLARÉ, pas
    mesuré. Le silence serait ici le vrai défaut — un plan bâti sur des chiffres
    affirmés par un humain n'a pas la même valeur de preuve qu'un plan bâti sur
    une mesure, et l'opérateur qui relit le plan doit le voir.

    `CUDA_VISIBLE_DEVICES` s'applique aussi à un profil déclaré : la variable
    gouverne ce que CUDA expose au runtime, quelle que soit l'origine de la liste
    de GPU. Lève `InventoryError` si le document est invalide.
    """
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InventoryError(
            f"{origin} : JSON illisible ({exc.msg}, ligne {exc.lineno}, "
            f"colonne {exc.colno}). Attendu : un profil matériel, voir "
            f"--hardware-profile dans docs/admin.md."
        ) from exc

    errors = validate_profile_document(document)
    if errors:
        raise InventoryError(
            f"{origin} : profil matériel invalide, aucune sonde ne le corrigera —\n"
            + "\n".join(f"  - {e}" for e in errors)
        )

    declared_gpus = [
        GpuDevice(
            index=gpu.get("index"),
            uuid=gpu["uuid"],
            vendor=gpu["vendor"],
            model=gpu["model"],
            vram_total_bytes=gpu["vram_total_bytes"],
            driver_version=gpu["driver_version"],
            compute_capability=gpu["compute_capability"],
        )
        for gpu in document["gpus"]
    ]
    gpus, findings = _apply_visibility(declared_gpus, env if env is not None else {})
    findings = list(findings)
    findings.append(schema.Finding(
        code="hardware_profile_declared",
        level="warn",
        message=(
            f"Profil matériel DÉCLARÉ via {origin}, non mesuré : les capacités "
            "ci-dessous n'ont été confrontées à aucune sonde. Un chiffre trop "
            "généreux ne se verra qu'au chargement du premier modèle."
        ),
    ))
    findings.extend(_vram_findings([g for g in gpus if g.visible]))

    return HardwareProfile(
        os=document["os"],
        os_version=document["os_version"],
        arch=document["arch"],
        cpu_model=document["cpu_model"],
        cpu_flags=tuple(document.get("cpu_flags", [])),
        ram_total_bytes=document["ram_total_bytes"],
        ram_available_bytes=document["ram_available_bytes"],
        disk_available_bytes=document["disk_available_bytes"],
        disk_path=str(document.get("disk_path") or str(DEFAULT_MODELS_DIR)),
        gpus=gpus,
        backend_candidates=tuple(document["backend_candidates"]),
        source=SOURCE_DECLARED,
        findings=schema.merge_findings(findings),
    )


# ── Projection vers le plan ───────────────────────────────────────────────────

def to_plan_section(profile: HardwareProfile) -> schema.PlanSection:
    """
    Projette le profil vers la section `hardware` du plan (contrat AUT-001).

    `data` est le document §5 tel quel : les consommateurs en aval lisent le
    contrat public, pas une représentation intermédiaire propre à ce module.
    """
    return schema.PlanSection(
        name=SECTION_NAME,
        version=SECTION_VERSION,
        status=profile.status,
        summary=_summarize(profile),
        data=profile.to_dict(),
        findings=profile.findings,
    )


def _summarize(profile: HardwareProfile) -> str:
    """Une ligne, lisible par un opérateur : la machine et ce qu'elle offre."""
    visible = profile.visible_gpus
    if visible:
        gpu_text = (
            f"{len(visible)}/{len(profile.gpus)} GPU exposé(s), "
            f"{profile.visible_vram_total_bytes / 1024**3:.1f} Go de VRAM"
        )
    elif profile.gpus:
        gpu_text = f"{len(profile.gpus)} GPU présent(s), aucun exposé"
    else:
        gpu_text = "aucun GPU"
    backends = ", ".join(profile.backend_candidates) or "aucun backend proposable"
    origin = "mesuré" if profile.source == SOURCE_PROBE else "déclaré"
    return (
        f"{profile.os} {profile.os_version} {profile.arch} ({origin}) — "
        f"{profile.ram_total_bytes / 1024**3:.1f} Go de RAM, "
        f"{profile.disk_available_bytes / 1024**3:.1f} Go libres sur "
        f"{profile.disk_path}, {gpu_text} ; backends : {backends}"
    )
