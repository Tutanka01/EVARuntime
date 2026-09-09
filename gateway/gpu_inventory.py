"""Sonde GPU locale et mesure de VRAM bornée par ``CUDA_VISIBLE_DEVICES``.

Le budget de ``LocalModelManager`` était historiquement confronté à une seule
valeur produite en additionnant ``memory.used`` de toutes les lignes de
``nvidia-smi``. Cette valeur est trompeuse dès qu'un autre workload utilise un
GPU non exposé au service. Ce module garde donc l'identité de chaque device
(UUID) jusqu'au dernier consommateur et ne calcule des agrégats que pour les
devices visibles par le runtime.

La sonde interroge explicitement l'inventaire physique avec
``CUDA_VISIBLE_DEVICES`` retirée de l'environnement du sous-processus, puis
applique la valeur configurée au résultat. Cela évite que nvidia-smi renumérote
ou masque des devices avant que nous ayons pu faire la correspondance UUID.
Une mesure incomplète, une scope invalide ou une sonde indisponible est
représentée par ``status="unavailable"`` : aucune valeur partielle n'est
présentée comme une mesure exploitable. Les UUID MIG sont conservés dans le
diagnostic mais refusés (`mig_unsupported`) tant qu'une sonde dédiée aux
instances MIG n'est pas disponible.
"""
from __future__ import annotations

import asyncio
import math
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Literal, Mapping, Sequence


# ``nounits`` rend les valeurs de mémoire stables et faciles à parser (MiB).
# L'UUID est conservé dans la même ligne afin de ne jamais dépendre d'un index
# renuméroté par CUDA.
NVIDIA_SMI_QUERY = (
    "--query-gpu=index,uuid,name,memory.used,memory.total,driver_version,"
    "compute_cap,mig.mode.current"
)
NVIDIA_SMI_FORMAT = "--format=csv,noheader,nounits"

# Bornes défensives : ``nvidia-smi`` est local, mais sa sortie est recopiée
# dans un statut admin et ne doit jamais permettre de gonfler indéfiniment la
# mémoire du worker ou la cardinalité d'une exposition Prometheus.
MAX_NVIDIA_OUTPUT_BYTES = 64 * 1024
MAX_GPU_DEVICES = 128
MAX_SCOPE_VALUE_LENGTH = 4096
MAX_DETAIL_LENGTH = 256

MEASUREMENT_MEASURED = "measured"
MEASUREMENT_UNAVAILABLE = "unavailable"
MeasurementStatus = Literal["measured", "unavailable"]

_UNAVAILABLE_MEMORY = frozenset({"", "n/a", "[n/a]", "na", "[na]", "unknown"})
_MIG_UUID_PREFIX = "MIG-"


def _timestamp() -> str:
    """Timestamp UTC lisible et stable pour le statut d'administration."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _decode_output(value: bytes | str | None) -> str:
    """Décode la sortie d'un fake ou d'un vrai ``asyncio`` subprocess."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _bounded_scope(raw: str | None) -> str | None:
    """Conserve le scope de configuration sans laisser gonfler le statut."""
    if raw is None or len(raw) <= MAX_SCOPE_VALUE_LENGTH:
        return raw
    return raw[:MAX_SCOPE_VALUE_LENGTH]


def _bounded_detail(detail: str | None) -> str | None:
    """Conserve un diagnostic court dans le statut administrateur."""
    if detail is None or len(detail) <= MAX_DETAIL_LENGTH:
        return detail
    return detail[:MAX_DETAIL_LENGTH]


def _parse_memory(value: str) -> float | None:
    """Parse une mémoire en MiB, sans jamais inventer une valeur."""
    normalized = value.strip().lower()
    if normalized in _UNAVAILABLE_MEMORY:
        return None
    try:
        parsed = float(normalized)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed


@dataclass(frozen=True)
class GpuVramSample:
    """Mesure d'un GPU identifié par UUID.

    ``memory_*_mb`` sont les valeurs ``nvidia-smi`` en MiB malgré le suffixe
    historique ``mb`` utilisé par les réponses de la gateway. Les propriétés en
    octets sont fournies pour les consommateurs qui ont besoin d'une unité
    explicite.
    """

    index: int | None
    uuid: str
    name: str
    memory_used_mb: float | None
    memory_total_mb: float | None
    driver_version: str = ""
    compute_capability: str = ""
    mig_mode_current: str = ""
    visible: bool = True

    @property
    def memory_used_bytes(self) -> int | None:
        if self.memory_used_mb is None:
            return None
        return int(self.memory_used_mb * 1024 * 1024)

    @property
    def memory_total_bytes(self) -> int | None:
        if self.memory_total_mb is None:
            return None
        return int(self.memory_total_mb * 1024 * 1024)

    def to_dict(self) -> dict[str, object]:
        """Projection JSON sans valeur dérivée ambiguë."""
        return {
            "index": self.index,
            "uuid": self.uuid,
            "name": self.name,
            "memory_used_mb": self.memory_used_mb,
            "memory_total_mb": self.memory_total_mb,
            "memory_used_bytes": self.memory_used_bytes,
            "memory_total_bytes": self.memory_total_bytes,
            "driver_version": self.driver_version,
            "compute_capability": self.compute_capability,
            "mig_mode_current": self.mig_mode_current,
            "visible": self.visible,
        }


@dataclass(frozen=True)
class VisibleGpuSelection:
    """Résolution déterministe de ``CUDA_VISIBLE_DEVICES``."""

    devices: tuple[GpuVramSample, ...]
    unresolved: tuple[str, ...]
    declared: bool


def resolve_visible_devices(
    gpus: Sequence[GpuVramSample],
    raw: str | None,
) -> VisibleGpuSelection:
    """Résout une liste d'index ou d'UUID sans agréger hors périmètre.

    ``None`` signifie variable absente (tous les devices sont visibles), tandis
    qu'une chaîne vide signifie variable explicitement définie mais sans GPU.
    Comme CUDA, une liste est tronquée au premier token inconnu : le résultat
    est toutefois marqué invalide par ``unresolved`` et ne peut pas être
    considéré comme une mesure réussie.
    """
    if raw is None:
        return VisibleGpuSelection(tuple(gpus), (), declared=False)

    # Une valeur de configuration anormalement longue ne peut pas être une
    # liste GPU raisonnable. Refuser avant ``split`` garde la sonde bornée et
    # évite qu'un statut admin ne reflète un scope arbitrairement volumineux.
    if len(raw) > MAX_SCOPE_VALUE_LENGTH:
        return VisibleGpuSelection(
            (), (raw[:MAX_SCOPE_VALUE_LENGTH],), declared=True
        )

    normalized = raw.strip()
    if not normalized or normalized == "-1":
        return VisibleGpuSelection((), (), declared=True)

    tokens = [token.strip() for token in raw.split(",")]
    if any(not token for token in tokens):
        return VisibleGpuSelection((), ("empty_token",), declared=True)

    selected: list[GpuVramSample] = []
    for token in tokens:
        if token.isdigit():
            match = next((gpu for gpu in gpus if gpu.index == int(token)), None)
        else:
            match = next((gpu for gpu in gpus if gpu.uuid == token), None)
        if match is None:
            return VisibleGpuSelection(tuple(selected), (token,), declared=True)
        if match not in selected:
            selected.append(match)
    return VisibleGpuSelection(tuple(selected), (), declared=True)


@dataclass(frozen=True)
class GpuVramMeasurement:
    """Résultat d'une sonde, y compris son état explicite d'indisponibilité."""

    status: MeasurementStatus
    reason: str
    measured_at: str | None
    cuda_visible_devices: str | None
    devices: tuple[GpuVramSample, ...] = ()
    visible_uuids: tuple[str, ...] = ()
    detail: str | None = None

    @classmethod
    def not_measured(cls, raw: str | None) -> "GpuVramMeasurement":
        """État initial avant le premier essai de sonde."""
        return cls(
            status=MEASUREMENT_UNAVAILABLE,
            reason="not_measured",
            measured_at=None,
            cuda_visible_devices=_bounded_scope(raw),
        )

    @classmethod
    def unavailable(
        cls,
        reason: str,
        raw: str | None,
        *,
        devices: Sequence[GpuVramSample] = (),
        visible_uuids: Sequence[str] = (),
        detail: str | None = None,
    ) -> "GpuVramMeasurement":
        return cls(
            status=MEASUREMENT_UNAVAILABLE,
            reason=reason,
            measured_at=_timestamp(),
            cuda_visible_devices=_bounded_scope(raw),
            devices=tuple(devices),
            visible_uuids=tuple(visible_uuids),
            detail=_bounded_detail(detail),
        )

    @classmethod
    def measured(
        cls,
        raw: str | None,
        devices: Sequence[GpuVramSample],
        visible_uuids: Sequence[str],
    ) -> "GpuVramMeasurement":
        visible = set(visible_uuids)
        decorated = tuple(
            replace(device, visible=device.uuid in visible) for device in devices
        )
        return cls(
            status=MEASUREMENT_MEASURED,
            reason="ok",
            measured_at=_timestamp(),
            cuda_visible_devices=_bounded_scope(raw),
            devices=decorated,
            visible_uuids=tuple(visible_uuids),
        )

    @classmethod
    def measured_aggregate(
        cls,
        used_mb: float,
        raw: str | None,
    ) -> "GpuVramMeasurement":
        """Compatibilité avec les tests/consommateurs legacy sans UUID.

        Ce chemin n'est utilisé que lorsque ``probe_gpu_used_mb`` est remplacée
        par un appelant historique. La sonde livrée ne l'emprunte jamais.
        """
        try:
            valid = math.isfinite(used_mb) and used_mb >= 0
        except (TypeError, ValueError):
            valid = False
        if not valid:
            return cls.unavailable("vram_used_invalid", raw)
        return cls(
            status=MEASUREMENT_MEASURED,
            reason="legacy_aggregate",
            measured_at=_timestamp(),
            cuda_visible_devices=_bounded_scope(raw),
            devices=(GpuVramSample(
                index=None,
                uuid="legacy-aggregate",
                name="legacy aggregate",
                memory_used_mb=used_mb,
                memory_total_mb=None,
                visible=True,
            ),),
            visible_uuids=("legacy-aggregate",),
        )

    @property
    def visible_devices(self) -> tuple[GpuVramSample, ...]:
        devices_by_uuid = {device.uuid: device for device in self.devices}
        # ``CUDA_VISIBLE_DEVICES`` est ordonné et CUDA réindexe selon cet ordre.
        # L'inventaire physique reste dans l'ordre nvidia-smi, mais toute vue
        # des devices gouvernés par le runtime conserve l'ordre configuré.
        return tuple(
            devices_by_uuid[uuid]
            for uuid in self.visible_uuids
            if uuid in devices_by_uuid
        )

    @property
    def visible_used_mb(self) -> float | None:
        if self.status != MEASUREMENT_MEASURED:
            return None
        values = [device.memory_used_mb for device in self.visible_devices]
        if any(value is None for value in values):
            return None
        return sum(value for value in values if value is not None)

    @property
    def visible_total_mb(self) -> float | None:
        if self.status != MEASUREMENT_MEASURED:
            return None
        values = [device.memory_total_mb for device in self.visible_devices]
        if any(value is None for value in values):
            return None
        return sum(value for value in values if value is not None)

    def to_dict(self) -> dict[str, object]:
        """Projection destinée au bloc admin ``vram_budget``."""
        payload: dict[str, object] = {
            "status": self.status,
            "reason": self.reason,
            "measured_at": self.measured_at,
            "cuda_visible_devices": self.cuda_visible_devices,
            "visible_uuids": list(self.visible_uuids),
            "devices": [device.to_dict() for device in self.devices],
            "visible_used_mb": self.visible_used_mb,
            "visible_total_mb": self.visible_total_mb,
        }
        if self.detail:
            payload["detail"] = self.detail
        return payload


# Noms courts conservés pour rendre l'API facile à découvrir dans les tests et
# les intégrations internes.
GpuMemorySample = GpuVramSample
GpuMemoryMeasurement = GpuVramMeasurement


def _parse_rows(text: str) -> tuple[list[GpuVramSample], int]:
    """Parse les lignes et renvoie aussi le nombre de lignes irrécupérables."""
    samples: list[GpuVramSample] = []
    malformed = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        if len(samples) >= MAX_GPU_DEVICES:
            malformed += 1
            continue
        fields = [field.strip() for field in line.split(",")]
        # Le format courant contient 8 champs. Les formes 4, 6 et 7 sont acceptées
        # pour replay de captures anciennes : elles n'inventent pas ``used``.
        if len(fields) < 4:
            malformed += 1
            continue

        index: int | None
        try:
            index = int(fields[0])
        except ValueError:
            index = None

        uuid = fields[1].strip()
        if not uuid or uuid.lower() in {"uuid", "gpu_uuid", "n/a", "[n/a]"}:
            malformed += 1
            continue

        name = ""
        used_raw = ""
        total_raw = ""
        driver = ""
        compute = ""
        mig_mode = ""
        if len(fields) >= 8:
            name, used_raw, total_raw, driver, compute, mig_mode = fields[2:8]
        elif len(fields) == 7:
            name, used_raw, total_raw, driver, compute = fields[2:7]
        elif len(fields) == 6:
            # Sortie historique doctor : index, uuid, name, total, driver, cap.
            name, total_raw, driver, compute = fields[2:6]
        else:
            # Capture compacte : index, uuid, used, total.
            used_raw, total_raw = fields[2:4]

        samples.append(GpuVramSample(
            index=index,
            uuid=uuid,
            name=name,
            memory_used_mb=_parse_memory(used_raw),
            memory_total_mb=_parse_memory(total_raw),
            driver_version=driver,
            compute_capability=compute,
            mig_mode_current=mig_mode,
        ))
    return samples, malformed


def parse_nvidia_smi_csv(text: str) -> list[GpuVramSample]:
    """Parse une capture ``nvidia-smi`` sans lever d'exception."""
    samples, _ = _parse_rows(text)
    return samples


def _subprocess_environment(source: Mapping[str, str]) -> dict[str, str]:
    """Demande l'inventaire physique, jamais l'inventaire déjà filtré."""
    environment = dict(source)
    environment.pop("CUDA_VISIBLE_DEVICES", None)
    # Les champs interrogés sont numériques ; une locale non-C peut toutefois
    # changer la représentation décimale sur certaines installations.
    environment["LC_ALL"] = "C"
    environment["LANG"] = "C"
    return environment


def _decorate_scope(
    samples: Sequence[GpuVramSample],
    raw: str | None,
) -> tuple[tuple[GpuVramSample, ...], VisibleGpuSelection]:
    selection = resolve_visible_devices(samples, raw)
    visible_uuids = {device.uuid for device in selection.devices}
    decorated = tuple(
        replace(device, visible=device.uuid in visible_uuids) for device in samples
    )
    by_uuid = {device.uuid: device for device in decorated}
    return decorated, VisibleGpuSelection(
        # L'ordre déclaré est un élément du contrat CUDA : ``1,0`` renumérote
        # les devices dans cet ordre et ne doit pas redevenir l'ordre physique.
        tuple(by_uuid[device.uuid] for device in selection.devices),
        selection.unresolved,
        selection.declared,
    )


async def probe_gpu_memory(
    timeout: float = 5.0,
    *,
    cuda_visible_devices: str | None = None,
    env: Mapping[str, str] | None = None,
) -> GpuVramMeasurement:
    """Mesure la VRAM utilisée et totale de chaque GPU visible.

    Le résultat est toujours un objet : ``status="unavailable"`` et un code
    ``reason`` stable remplacent ``None`` lorsque la mesure ne peut pas être
    interprétée. Les lignes de devices hors scope restent dans l'inventaire,
    mais ne contribuent jamais aux propriétés ``visible_*``.
    """
    source_env = env if env is not None else os.environ
    raw = (
        cuda_visible_devices
        if cuda_visible_devices is not None
        else source_env.get("CUDA_VISIBLE_DEVICES")
    )

    try:
        valid_timeout = math.isfinite(timeout) and timeout > 0
    except (TypeError, ValueError):
        valid_timeout = False
    if not valid_timeout:
        return GpuVramMeasurement.unavailable("invalid_timeout", raw)

    try:
        process = await asyncio.create_subprocess_exec(
            "nvidia-smi",
            NVIDIA_SMI_QUERY,
            NVIDIA_SMI_FORMAT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=_subprocess_environment(source_env),
        )
    except FileNotFoundError:
        return GpuVramMeasurement.unavailable("nvidia_smi_unavailable", raw)
    except OSError as exc:
        return GpuVramMeasurement.unavailable(
            "nvidia_smi_unavailable", raw, detail=type(exc).__name__
        )
    except Exception as exc:
        return GpuVramMeasurement.unavailable(
            "nvidia_smi_probe_error", raw, detail=type(exc).__name__
        )

    try:
        stdout, _ = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
        try:
            await process.wait()
        except Exception:
            pass
        return GpuVramMeasurement.unavailable("nvidia_smi_timeout", raw)
    except asyncio.CancelledError:
        # La tâche de réconciliation peut être annulée au shutdown alors que
        # nvidia-smi tourne encore. Nettoyer le fils avant de propager l'annulation
        # évite de laisser un processus orphelin à chaque redémarrage.
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
        try:
            await process.wait()
        except (Exception, asyncio.CancelledError):
            pass
        raise
    except Exception as exc:
        return GpuVramMeasurement.unavailable(
            "nvidia_smi_probe_error", raw, detail=type(exc).__name__
        )

    if process.returncode != 0:
        # stderr n'est jamais remonté : le détail d'un pilote peut contenir des
        # chemins ou des informations d'hôte. Le code de retour suffit au statut.
        return GpuVramMeasurement.unavailable("nvidia_smi_failed", raw)

    if isinstance(stdout, bytes) and len(stdout) > MAX_NVIDIA_OUTPUT_BYTES:
        return GpuVramMeasurement.unavailable("nvidia_smi_output_too_large", raw)
    if isinstance(stdout, str) and len(stdout.encode("utf-8")) > MAX_NVIDIA_OUTPUT_BYTES:
        return GpuVramMeasurement.unavailable("nvidia_smi_output_too_large", raw)

    output = _decode_output(stdout)
    samples, malformed = _parse_rows(output)
    if not samples:
        reason = "nvidia_smi_parse_error" if output.strip() else "gpu_inventory_empty"
        return GpuVramMeasurement.unavailable(reason, raw)

    # ``--query-gpu`` ne décrit pas correctement les instances MIG enfants.
    # Refuser aussi un scope MIG explicite, même si nvidia-smi ne renvoie que
    # l'UUID du GPU parent, plutôt que d'attribuer sa VRAM entière à l'instance.
    if raw and any(
        token.strip().upper().startswith(_MIG_UUID_PREFIX)
        for token in raw.split(",")
    ):
        return GpuVramMeasurement.unavailable("mig_unsupported", raw, devices=samples)

    decorated, selection = _decorate_scope(samples, raw)
    if malformed:
        return GpuVramMeasurement.unavailable(
            "nvidia_smi_parse_error",
            raw,
            devices=decorated,
            visible_uuids=[device.uuid for device in selection.devices],
        )

    uuids = [device.uuid for device in decorated]
    if len(set(uuids)) != len(uuids):
        return GpuVramMeasurement.unavailable(
            "gpu_uuid_duplicate",
            raw,
            devices=decorated,
            visible_uuids=[device.uuid for device in selection.devices],
        )
    if any(device.uuid.upper().startswith(_MIG_UUID_PREFIX) for device in decorated):
        return GpuVramMeasurement.unavailable(
            "mig_unsupported",
            raw,
            devices=decorated,
            visible_uuids=[device.uuid for device in selection.devices],
        )
    if any(
        device.mig_mode_current.strip().lower() == "enabled"
        for device in selection.devices
    ):
        return GpuVramMeasurement.unavailable(
            "mig_unsupported",
            raw,
            devices=decorated,
            visible_uuids=[device.uuid for device in selection.devices],
        )
    if selection.unresolved:
        return GpuVramMeasurement.unavailable(
            "cuda_visible_devices_invalid",
            raw,
            devices=decorated,
            visible_uuids=[device.uuid for device in selection.devices],
            detail=selection.unresolved[0],
        )
    if raw is not None and not selection.devices:
        return GpuVramMeasurement.unavailable(
            "cuda_visible_devices_empty", raw, devices=decorated
        )

    visible_uuids = [device.uuid for device in selection.devices]
    visible_devices = [device for device in decorated if device.visible]
    if any(device.memory_used_mb is None for device in visible_devices):
        return GpuVramMeasurement.unavailable(
            "vram_used_unavailable",
            raw,
            devices=decorated,
            visible_uuids=visible_uuids,
        )
    if any(device.memory_total_mb is None for device in visible_devices):
        return GpuVramMeasurement.unavailable(
            "vram_total_unavailable",
            raw,
            devices=decorated,
            visible_uuids=visible_uuids,
        )

    return GpuVramMeasurement.measured(raw, decorated, visible_uuids)


# Alias explicite pour les appelants qui parlent de VRAM plutôt que de mémoire.
probe_gpu_vram = probe_gpu_memory
