"""
AUT-017 — raccords de production du bootstrap.

Les modules de M2 définissent volontairement des protocoles étroits. Ce module
est la frontière impure qui les relie au vrai hôte : HTTP asynchrone, mesures
RAM/VRAM et processus ``llama-server`` de calibration. Il ne décide rien ; il
reconstruit strictement les décisions déjà publiées dans le plan.

Deux propriétés de sécurité structurent l'implémentation :

* un secret d'administration vient de l'environnement ou d'un fichier privé,
  jamais d'argv ;
* la calibration lance un serveur isolé sur loopback. Elle ne rend pas une
  entrée désactivée publiquement servable pour contourner la barrière
  d'activation sur preuve.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import os
import re
import signal
import socket
import stat
import time
import urllib.parse
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Sequence

import httpx

from child_env import inherited_child_env
from llama_version import probe_llama_version

from . import calibration
from . import catalog as catalog_mod
from . import execution
from . import first_token
from . import runtime_installer
from . import runtime_resolver
from . import runtime_variants
from . import schema
from . import warmup

_MIB = 1024 * 1024
_RUNTIME_DATA_KEYS = frozenset({
    "resolved", "reuse_existing", "degraded", "platform",
    "backend_candidates", "targeted_backend", "selected_backend",
    "gpu_vendor", "gpu_count", "driver_version", "cuda_major",
    "min_build", "observed_build", "variant", "manifest", "rejected",
    # AUT-019 — la politique du résolveur voyage dans le plan. La reconstruction
    # doit la relire aussi strictement que le reste : c'est elle qui dit si un
    # repli CPU était autorisé, et l'application ne doit pas pouvoir en changer.
    "policy",
})
_POLICY_KEYS = frozenset({
    "allow_container", "allow_local_build", "allow_cpu_fallback",
})
_VARIANT_KEYS = frozenset({
    "source", "backend", "platform", "evidence", "evidence_note",
    "reference", "artifact_sha256", "container_digest", "approx_bytes",
})
_MANIFEST_KEYS = frozenset({
    "project", "version", "commit", "source", "backend", "platform",
    "artifact_sha256", "container_digest", "build_options", "installed_at",
})


class ProductionWiringError(execution.ExecutionError):
    """Le raccord réel ne peut pas être construit sans inventer une donnée."""


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProductionWiringError(
            f"{label} doit être un objet JSON, reçu {type(value).__name__}"
        )
    return value


def _closed(document: dict[str, Any], keys: frozenset[str], label: str) -> None:
    missing = sorted(keys - set(document))
    unknown = sorted(set(document) - keys)
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append(f"champs manquants {missing}")
        if unknown:
            details.append(f"champs inconnus {unknown}")
        raise ProductionWiringError(f"{label} : " + " ; ".join(details))


def _strict_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ProductionWiringError(f"{label} doit être un booléen JSON")
    return value


def _strict_int(value: Any, label: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ProductionWiringError(f"{label} doit être un entier >= {minimum}")
    return value


def _optional_int(value: Any, label: str) -> int | None:
    if value is None:
        return None
    return _strict_int(value, label)


def _optional_str(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ProductionWiringError(f"{label} doit être une chaîne non vide ou null")
    return value


def _string(value: Any, label: str) -> str:
    result = _optional_str(value, label)
    if result is None:
        raise ProductionWiringError(f"{label} doit être une chaîne non vide")
    return result


def _section(document: Mapping[str, Any], name: str) -> dict[str, Any]:
    sections = document.get("sections")
    if not isinstance(sections, list):
        raise ProductionWiringError("sections doit être une liste")
    matches = [s for s in sections if isinstance(s, dict) and s.get("name") == name]
    if len(matches) != 1:
        raise ProductionWiringError(
            f"le plan doit porter exactement une section {name!r}, trouvé {len(matches)}"
        )
    return matches[0]


def runtime_resolution_from_plan(document: Mapping[str, Any]) -> runtime_resolver.RuntimeResolution:
    """
    Reconstruit la décision AUT-003 sans la recalculer ni l'assouplir.

    Le document est fermé champ par champ, puis le rendu de l'objet reconstruit
    est comparé au bloc original. Cette dernière égalité attrape aussi une
    incohérence entre ``selected_backend``, le manifeste et la variante.
    """
    section = _section(document, schema.SECTION_RUNTIME)
    data = _object(section.get("data"), "sections.runtime.data")
    _closed(data, _RUNTIME_DATA_KEYS, "sections.runtime.data")

    reuse_existing = _strict_bool(data["reuse_existing"], "runtime.reuse_existing")
    raw_variant_value = data["variant"]
    if reuse_existing:
        if raw_variant_value is not None:
            raise ProductionWiringError(
                "un runtime réutilisé doit porter variant=null"
            )
        variant = None
    else:
        raw_variant = _object(raw_variant_value, "sections.runtime.data.variant")
        _closed(raw_variant, _VARIANT_KEYS, "sections.runtime.data.variant")
        variant = runtime_resolver.ArtifactVariant(
            source=_string(raw_variant["source"], "variant.source"),
            backend=_string(raw_variant["backend"], "variant.backend"),
            platform=_string(raw_variant["platform"], "variant.platform"),
            evidence=_string(raw_variant["evidence"], "variant.evidence"),
            evidence_note=_string(raw_variant["evidence_note"], "variant.evidence_note"),
            reference=_string(raw_variant["reference"], "variant.reference"),
            artifact_sha256=_optional_str(
                raw_variant["artifact_sha256"], "variant.artifact_sha256"
            ),
            container_digest=_optional_str(
                raw_variant["container_digest"], "variant.container_digest"
            ),
            approx_bytes=_optional_int(raw_variant["approx_bytes"], "variant.approx_bytes"),
        )

    raw_manifest_value = data["manifest"]
    if raw_manifest_value is None:
        if not reuse_existing:
            raise ProductionWiringError(
                "un runtime à installer doit porter un manifeste de provenance"
            )
        manifest = None
    else:
        raw_manifest = _object(raw_manifest_value, "sections.runtime.data.manifest")
        _closed(raw_manifest, _MANIFEST_KEYS, "sections.runtime.data.manifest")
        build_options = raw_manifest["build_options"]
        if not isinstance(build_options, dict):
            raise ProductionWiringError("manifest.build_options doit être un objet")
        manifest = runtime_resolver.ProvenanceManifest(
            project=_string(raw_manifest["project"], "manifest.project"),
            version=_string(raw_manifest["version"], "manifest.version"),
            commit=_string(raw_manifest["commit"], "manifest.commit"),
            source=_string(raw_manifest["source"], "manifest.source"),
            backend=_string(raw_manifest["backend"], "manifest.backend"),
            platform=_string(raw_manifest["platform"], "manifest.platform"),
            artifact_sha256=_optional_str(
                raw_manifest["artifact_sha256"], "manifest.artifact_sha256"
            ),
            container_digest=_optional_str(
                raw_manifest["container_digest"], "manifest.container_digest"
            ),
            build_options=build_options,
            installed_at=_string(raw_manifest["installed_at"], "manifest.installed_at"),
        )

    if variant is not None and manifest is not None:
        variant_identity = (
            variant.source, variant.backend, variant.platform, variant.artifact_sha256,
            variant.container_digest,
        )
        manifest_identity = (
            manifest.source, manifest.backend, manifest.platform, manifest.artifact_sha256,
            manifest.container_digest,
        )
        if variant_identity != manifest_identity:
            raise ProductionWiringError(
                "la variante et le manifeste runtime divergent "
                "(source, backend, plateforme ou empreinte)"
            )
    candidates = data["backend_candidates"]
    if not isinstance(candidates, list) or any(not isinstance(v, str) for v in candidates):
        raise ProductionWiringError("runtime.backend_candidates doit être une liste de chaînes")
    rejected = data["rejected"]
    if not isinstance(rejected, list) or any(not isinstance(v, str) for v in rejected):
        raise ProductionWiringError("runtime.rejected doit être une liste de chaînes")
    politique = _object(data["policy"], "sections.runtime.data.policy")
    _closed(politique, _POLICY_KEYS, "sections.runtime.data.policy")
    findings_raw = section.get("findings")
    if not isinstance(findings_raw, list):
        raise ProductionWiringError("sections.runtime.findings doit être une liste")
    findings = tuple(
        schema.Finding(
            code=_string(_object(item, "runtime.finding").get("code"), "finding.code"),
            level=_string(item.get("level"), "finding.level"),  # type: ignore[arg-type]
            message=_string(item.get("message"), "finding.message"),
        )
        for item in findings_raw
    )
    profile = runtime_resolver.HardwareProfile(
        platform=_string(data["platform"], "runtime.platform"),
        backend_candidates=tuple(candidates),
        gpu_vendor=_optional_str(data["gpu_vendor"], "runtime.gpu_vendor"),
        driver_version=_optional_str(data["driver_version"], "runtime.driver_version"),
        cuda_major=_optional_int(data["cuda_major"], "runtime.cuda_major"),
        gpu_count=_strict_int(data["gpu_count"], "runtime.gpu_count"),
    )
    resolution = runtime_resolver.RuntimeResolution(
        profile=profile,
        min_build=_strict_int(data["min_build"], "runtime.min_build"),
        resolved=_strict_bool(data["resolved"], "runtime.resolved"),
        reuse_existing=reuse_existing,
        degraded=_strict_bool(data["degraded"], "runtime.degraded"),
        targeted_backend=_optional_str(data["targeted_backend"], "runtime.targeted_backend"),
        variant=variant,
        manifest=manifest,
        observed_build=_optional_int(data["observed_build"], "runtime.observed_build"),
        summary=_string(section.get("summary"), "sections.runtime.summary"),
        findings=findings,
        rejected=tuple(rejected),
        allow_container=_strict_bool(
            politique["allow_container"], "runtime.policy.allow_container"
        ),
        allow_local_build=_strict_bool(
            politique["allow_local_build"], "runtime.policy.allow_local_build"
        ),
        allow_cpu_fallback=_strict_bool(
            politique["allow_cpu_fallback"], "runtime.policy.allow_cpu_fallback"
        ),
    )
    if resolution.to_data() != data:
        raise ProductionWiringError(
            "la décision runtime reconstruite ne reproduit pas exactement le plan "
            "(backend, manifeste ou profil incohérent)"
        )
    return resolution


def runtime_installer_from_plan(
    document: Mapping[str, Any], install_root: Path
) -> runtime_installer.RuntimeInstaller:
    """Construit l'installateur uniquement depuis la décision relue."""
    resolution = runtime_resolution_from_plan(document)
    variant = resolution.variant
    if variant is None:
        raise ProductionWiringError("la décision runtime ne porte aucune variante")
    # `variant.reference` sert d'URL de téléchargement. Le contrôle initial se
    # limitait au schéma et à l'autorité : la page de releases GitHub que porte
    # toute variante `official-release` de la matrice livrée le passait sans être
    # un artefact. L'installateur téléchargeait alors une page HTML pour la
    # rejeter au contrôle d'empreinte — un diagnostic trompeur, après coup et
    # après la bande passante. Le contrôle est désormais celui d'AUT-018 : URL
    # publique HTTPS *désignant une archive*.
    try:
        archive_url = runtime_variants.validate_archive_url(
            variant.reference, "variant.reference"
        )
    except runtime_variants.RuntimeVariantsError as exc:
        raise ProductionWiringError(
            f"{exc} Régénérez le plan avec la référence exacte de l'archive "
            "(--runtime-variants)."
        ) from exc
    request = runtime_installer.RuntimeInstallRequest(
        resolution=resolution,
        archive_url=archive_url,
        install_root=install_root,
    )
    refusals = runtime_installer.refusal_reasons(request)
    if refusals:
        raise ProductionWiringError("runtime non installable : " + " ; ".join(refusals))
    return runtime_installer.RuntimeInstaller(request)


def read_admin_secret(
    *, path: Path | None = None, environ: Mapping[str, str] | None = None
) -> str:
    """Lit ADMIN_SECRET sans jamais accepter sa valeur comme argument de CLI."""
    if path is not None:
        target = Path(path)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags)
        except OSError as exc:
            raise ProductionWiringError(
                f"fichier ADMIN_SECRET illisible ({target}) : {exc}"
            ) from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ProductionWiringError("le fichier ADMIN_SECRET doit être un fichier régulier")
            if info.st_uid not in {0, os.geteuid()}:
                raise ProductionWiringError(
                    "le fichier ADMIN_SECRET n'appartient ni à root ni à l'utilisateur "
                    "qui exécute la commande"
                )
            if info.st_mode & 0o077:
                raise ProductionWiringError(
                    "le fichier ADMIN_SECRET est lisible par le groupe ou les autres ; "
                    "appliquez chmod 600"
                )
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                value = handle.read().strip()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    else:
        source = os.environ if environ is None else environ
        value = str(source.get("ADMIN_SECRET") or "").strip()
    if not value:
        raise ProductionWiringError(
            "ADMIN_SECRET absent : fournissez --admin-secret-file ou la variable "
            "d'environnement ADMIN_SECRET (jamais la valeur en argv)"
        )
    return value


def license_acceptances(
    catalogue: catalog_mod.Catalog,
    entry_ids: Sequence[str],
    *,
    operator_reference: str,
    accepted_at: str | None = None,
) -> tuple[Any, ...]:
    """Matérialise uniquement les acceptations explicitement nommées par l'opérateur."""
    from . import downloader

    if not operator_reference.strip():
        raise ProductionWiringError(
            "--license-reference est obligatoire avec --accept-license"
        )
    if len(set(entry_ids)) != len(entry_ids):
        raise ProductionWiringError("--accept-license contient un identifiant en double")
    timestamp = accepted_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    result: list[downloader.LicenseAcceptance] = []
    for entry_id in entry_ids:
        entry = catalogue.get(entry_id)
        if entry is None:
            raise ProductionWiringError(
                f"--accept-license vise {entry_id!r}, absent du catalogue relu"
            )
        if not entry.license.operator_acceptance_required:
            raise ProductionWiringError(
                f"{entry_id!r} ne demande pas d'acceptation opérateur dans le catalogue relu"
            )
        result.append(downloader.LicenseAcceptance(
            entry_id=entry.id,
            base_model_license=entry.license.base_model.id,
            fine_tune_license=entry.license.fine_tune.id,
            operator_reference=operator_reference.strip(),
            accepted_at=timestamp,
            accepted=True,
        ))
    return tuple(result)


def _validate_http_url(url: str, label: str) -> str:
    try:
        parts = urllib.parse.urlsplit(url)
        hostname = parts.hostname
        parts.port  # force la validation (entier et plage 0..65535)
    except ValueError as exc:
        raise ProductionWiringError(f"{label} porte un hôte ou un port invalide") from exc
    if parts.scheme not in {"http", "https"} or not hostname:
        raise ProductionWiringError(f"{label} doit être une URL HTTP(S) absolue")
    if parts.username is not None or parts.password is not None:
        raise ProductionWiringError(
            f"{label} ne doit pas porter d'identifiants dans l'URL"
        )
    return url.rstrip("/")


def _validate_http_origin(url: str, label: str) -> str:
    """Valide une origin HTTP(S), sans chemin, paramètres ni fragment."""
    _validate_http_url(url, label)
    parts = urllib.parse.urlsplit(url)
    if parts.path not in {"", "/"}:
        raise ProductionWiringError(
            f"{label} doit être une origin HTTP(S), sans chemin"
        )
    if parts.query or parts.fragment or "?" in url or "#" in url:
        raise ProductionWiringError(
            f"{label} doit être une origin HTTP(S), sans query ni fragment"
        )
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def validate_gateway_urls(*, base_url: str, admin_url: str) -> tuple[str, str]:
    """Ferme la portée du secret admin et exige TLS sur le chemin public distant."""
    public = _validate_http_origin(base_url, "base_url")
    control = _validate_http_origin(admin_url, "admin_url")
    public_parts = urllib.parse.urlsplit(public)
    control_parts = urllib.parse.urlsplit(control)

    def loopback(hostname: str | None) -> bool:
        if not hostname:
            return False
        if hostname.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            return False

    if not loopback(control_parts.hostname):
        raise ProductionWiringError(
            "admin_url doit viser loopback (127.0.0.0/8, ::1 ou localhost) : "
            "ADMIN_SECRET ne quitte jamais l'hôte"
        )
    if public_parts.scheme != "https" and not loopback(public_parts.hostname):
        raise ProductionWiringError(
            "base_url distante doit utiliser HTTPS ; HTTP n'est admis que sur loopback"
        )
    return public, control


@dataclass
class _HttpxStream:
    response: httpx.Response

    @property
    def status(self) -> int:
        return self.response.status_code

    @property
    def headers(self) -> Mapping[str, str]:
        return self.response.headers

    def aiter_lines(self) -> AsyncIterator[str]:
        return self.response.aiter_lines()


class AsyncHttpClient:
    """Client concret partagé par AUT-009 et AUT-010, sans état secret."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float,
    ) -> first_token.HttpResponse:
        _validate_http_url(url, "url")
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
            response = await client.request(
                method, url, json=json, headers=headers, timeout=timeout
            )
        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError):
            body = None
        return first_token.HttpResponse(
            status=response.status_code,
            body=body,
            headers=dict(response.headers),
        )

    @asynccontextmanager
    async def stream(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float,
    ) -> AsyncIterator[_HttpxStream]:
        _validate_http_url(url, "url")
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
            async with client.stream(
                method, url, json=json, headers=headers, timeout=timeout
            ) as response:
                yield _HttpxStream(response)


class LiveRegistrySyncClient:
    """Client loopback du protocole d'activation éphémère du bootstrap."""

    def __init__(
        self,
        *,
        admin_url: str,
        admin_secret: str,
        client: AsyncHttpClient,
        timeout_seconds: float,
        lease_seconds: int,
    ) -> None:
        _, control = validate_gateway_urls(
            base_url=admin_url,
            admin_url=admin_url,
        )
        if not isinstance(admin_secret, str) or not admin_secret:
            raise ProductionWiringError(
                "la synchronisation live exige un ADMIN_SECRET non vide"
            )
        if timeout_seconds <= 0:
            raise ProductionWiringError("live registry timeout doit être > 0")
        if (
            not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or not 30 <= lease_seconds <= 3600
        ):
            raise ProductionWiringError(
                "live registry lease doit être un entier entre 30 et 3600 s"
            )
        self.admin_url = control
        self.admin_secret = admin_secret
        self.client = client
        self.timeout_seconds = float(timeout_seconds)
        self.lease_seconds = lease_seconds

    async def activate(self, model_id: str, vram_gb: float, digest: str) -> None:
        await self._sync(
            model_id,
            {
                "action": "activate",
                "digest": digest,
                "vram_gb": vram_gb,
                "lease_seconds": self.lease_seconds,
            },
        )

    async def rollback(self, model_id: str, digest: str) -> None:
        await self._sync(model_id, {"action": "rollback", "digest": digest})

    async def confirm(self, model_id: str, digest: str) -> None:
        await self._sync(model_id, {"action": "confirm", "digest": digest})

    async def _sync(self, model_id: str, payload: Mapping[str, Any]) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,62}", model_id):
            raise ProductionWiringError("identifiant de modèle live invalide")
        response = await self.client.request(
            "POST",
            f"{self.admin_url}/admin/models/{model_id}/bootstrap-sync",
            json=dict(payload),
            headers={"Authorization": f"Bearer {self.admin_secret}"},
            timeout=self.timeout_seconds,
        )
        if response.status != 200:
            raise ProductionWiringError(
                f"synchronisation live {payload.get('action')} refusée pour "
                f"« {model_id} » (HTTP {response.status})"
            )


def derive_live_registry_lease_seconds(
    settings: first_token.FirstTokenSettings,
    *,
    sync_timeout_seconds: float,
    safety_seconds: float = 60.0,
) -> int:
    """Borne le bail par le pire chemin séquentiel de la recette ciblée.

    Le bail commence à l'activation live et ne doit pas expirer pendant un
    chargement ou un flux encore dans leurs propres timeouts. La recette ciblée
    effectue deux probes readiness, quatre mutations d'identité admin, le load,
    le stream, puis une attente de log d'usage qui peut commencer un dernier
    appel juste avant sa deadline. La confirmation live et une marge explicite
    ferment le calcul.
    """
    for label, value in (
        ("sync_timeout_seconds", sync_timeout_seconds),
        ("safety_seconds", safety_seconds),
    ):
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ProductionWiringError(f"{label} doit être > 0")
    total = (
        2 * settings.ready_timeout_s
        + 4 * settings.admin_timeout_s
        + settings.load_timeout_s
        + settings.stream_timeout_s
        + settings.usage_timeout_s
        + settings.admin_timeout_s
        + settings.usage_poll_interval_s
        + float(sync_timeout_seconds)
        + float(safety_seconds)
    )
    if not math.isfinite(total):
        raise ProductionWiringError(
            "les timeouts de la recette doivent tous être finis pour dériver le bail live"
        )
    lease = max(30, math.ceil(total))
    if lease > 3600:
        raise ProductionWiringError(
            "les timeouts de la recette exigent un bail live de "
            f"{lease} s, au-delà du maximum sûr de 3600 s ; réduisez notamment "
            "MODEL_LOAD_TIMEOUT_SECONDS"
        )
    return lease


@dataclass(frozen=True)
class CalibrationTarget:
    """Artefact et paramètres effectifs d'un modèle de calibration."""

    model_path: Path
    params: calibration.CalibrationParams
    mmproj_path: Path | None = None


def _assert_loopback_port_available(port: int) -> None:
    """Réserve brièvement le port pour refuser une collision avant le fork."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))
    except OSError as exc:
        raise ProductionWiringError(
            f"port loopback de calibration {port} indisponible : {exc}"
        ) from exc


def _advertised_model_ids(body: Any) -> frozenset[str]:
    if not isinstance(body, Mapping) or not isinstance(body.get("data"), list):
        return frozenset()
    return frozenset(
        item["id"]
        for item in body["data"]
        if isinstance(item, Mapping)
        and isinstance(item.get("id"), str)
        and item["id"]
    )


class LlamaServerCalibrationProbes:
    """Sondes réelles, isolées de la gateway et bornées à un port loopback."""

    def __init__(
        self,
        *,
        binary: Path,
        targets: Mapping[str, CalibrationTarget],
        port: int,
        load_timeout_seconds: float,
        visible_gpu_indices: Sequence[int] | None = None,
        visible_gpu_uuids: Sequence[str] | None = None,
        http: AsyncHttpClient | None = None,
    ) -> None:
        if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
            raise ProductionWiringError("calibration_port doit être compris entre 1024 et 65535")
        if load_timeout_seconds <= 0:
            raise ProductionWiringError("calibration_load_timeout doit être > 0")
        self.binary = Path(binary)
        self.targets = dict(targets)
        self.port = port
        self.load_timeout_seconds = float(load_timeout_seconds)
        self.visible_gpu_indices = (
            frozenset(visible_gpu_indices) if visible_gpu_indices is not None else None
        )
        if self.visible_gpu_indices is not None and (
            not self.visible_gpu_indices
            or any(
                not isinstance(index, int) or isinstance(index, bool) or index < 0
                for index in self.visible_gpu_indices
            )
        ):
            raise ProductionWiringError(
                "visible_gpu_indices doit contenir au moins un index GPU entier >= 0"
            )
        self.visible_gpu_uuids = (
            tuple(visible_gpu_uuids) if visible_gpu_uuids is not None else None
        )
        if self.visible_gpu_uuids is not None and (
            not self.visible_gpu_uuids
            or len(set(self.visible_gpu_uuids)) != len(self.visible_gpu_uuids)
            or any(
                not isinstance(uuid, str) or not uuid.strip()
                for uuid in self.visible_gpu_uuids
            )
        ):
            raise ProductionWiringError(
                "visible_gpu_uuids doit contenir au moins un UUID GPU non vide"
            )
        self.http = http or AsyncHttpClient()
        self._process: asyncio.subprocess.Process | None = None
        self._drains: list[asyncio.Task[None]] = []
        self._tail: deque[str] = deque(maxlen=20)
        self._loaded_model = ""

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def read_vram(self) -> calibration.MemoryReading:
        command = (
            "nvidia-smi", "--query-gpu=index,uuid,memory.used,memory.total",
            "--format=csv,noheader,nounits",
        )
        code, output, detail = await _subprocess_output(command, timeout=5.0)
        if code != 0:
            return calibration.MemoryReading(ok=False, detail=detail)
        used = 0
        total = 0
        matched = 0
        try:
            rows = [line.strip() for line in output.splitlines() if line.strip()]
            if not rows:
                raise ValueError("aucun GPU rendu")
            for row in rows:
                values = [part.strip() for part in row.split(",")]
                if len(values) != 4:
                    raise ValueError(f"ligne inattendue : {row!r}")
                index = int(values[0])
                uuid = values[1]
                if self.visible_gpu_uuids is not None:
                    if uuid not in self.visible_gpu_uuids:
                        continue
                elif (
                    self.visible_gpu_indices is not None
                    and index not in self.visible_gpu_indices
                ):
                    continue
                matched += 1
                used += int(values[2]) * _MIB
                total += int(values[3]) * _MIB
            if (
                self.visible_gpu_uuids is not None
                or self.visible_gpu_indices is not None
            ) and matched == 0:
                raise ValueError(
                    "aucun GPU du plan n'est visible dans la sortie nvidia-smi"
                )
        except (ValueError, IndexError) as exc:
            return calibration.MemoryReading(ok=False, detail=f"nvidia-smi illisible : {exc}")
        return calibration.MemoryReading(ok=True, used_bytes=used, total_bytes=total)

    async def read_ram(self) -> calibration.MemoryReading:
        return await asyncio.to_thread(_read_ram)

    async def validate_environment(
        self, identity: calibration.CalibrationIdentity
    ) -> None:
        """Atteste le binaire et les GPU actuels avant mesure ou réutilisation."""
        version = await probe_llama_version(self.binary)
        current_runtime = f"b{version.build}" if version.build is not None else None
        if current_runtime != identity.runtime_version:
            raise calibration.CalibrationError(
                "runtime courant différent de l'identité de calibration : "
                f"{current_runtime!r} contre {identity.runtime_version!r}"
            )
        if self.visible_gpu_uuids is None:
            raise calibration.CalibrationError(
                "UUID GPU absents du raccord de production : impossible d'attester "
                "le matériel courant"
            )

        command = (
            "nvidia-smi",
            "--query-gpu=index,uuid,name,memory.total,driver_version,compute_cap",
            "--format=csv,noheader,nounits",
        )
        code, output, detail = await _subprocess_output(command, timeout=5.0)
        if code != 0:
            raise calibration.CalibrationError(
                "inventaire GPU courant impossible : " + (detail or f"code {code}")
            )
        expected_uuids = set(self.visible_gpu_uuids)
        current_uuids: set[str] = set()
        descriptors: list[dict[str, Any]] = []
        try:
            for raw in output.splitlines():
                if not raw.strip():
                    continue
                values = [part.strip() for part in raw.split(",")]
                if len(values) != 6:
                    raise ValueError(f"ligne inattendue : {raw!r}")
                uuid = values[1]
                if uuid not in expected_uuids:
                    continue
                if uuid in current_uuids:
                    raise ValueError(f"UUID GPU dupliqué : {uuid}")
                current_uuids.add(uuid)
                descriptors.append({
                    "name": values[2],
                    "vram_total_mib": int(values[3]),
                    "driver_version": values[4],
                    "compute_cap": values[5],
                })
        except (ValueError, IndexError) as exc:
            raise calibration.CalibrationError(
                f"inventaire GPU courant illisible : {exc}"
            ) from exc
        if current_uuids != expected_uuids:
            raise calibration.CalibrationError(
                "UUID GPU visibles différents du plan : "
                f"courants={sorted(current_uuids)}, attendus={sorted(expected_uuids)}"
            )
        current_fingerprint = calibration.hardware_fingerprint(descriptors)
        if current_fingerprint != identity.hardware_fingerprint:
            raise calibration.CalibrationError(
                "empreinte GPU courante différente du plan "
                "(modèle, VRAM, pilote ou compute capability)"
            )

    async def load_model(self, request: calibration.LoadRequest) -> calibration.LoadOutcome:
        if self._process is not None:
            return calibration.LoadOutcome(ok=False, detail="un modèle de calibration est déjà chargé")
        target = self.targets.get(request.model_id)
        if target is None:
            return calibration.LoadOutcome(ok=False, detail="modèle absent du raccord de calibration")
        if not target.model_path.is_file() or not self.binary.is_file():
            return calibration.LoadOutcome(
                ok=False, detail="binaire llama-server ou fichier GGUF absent"
            )
        version = await probe_llama_version(self.binary)
        if version.build is None:
            return calibration.LoadOutcome(
                ok=False, detail="version du binaire llama-server illisible"
            )
        try:
            _assert_loopback_port_available(self.port)
        except ProductionWiringError as exc:
            return calibration.LoadOutcome(ok=False, detail=str(exc))
        args = self._command(target, request)
        try:
            self._process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                env=self._process_env(),
            )
        except (OSError, PermissionError) as exc:
            self._process = None
            return calibration.LoadOutcome(ok=False, detail=f"lancement impossible : {exc}")
        process = self._process
        self._drains = [
            asyncio.create_task(self._drain(process.stdout)),
            asyncio.create_task(self._drain(process.stderr)),
        ]
        deadline = time.monotonic() + self.load_timeout_seconds
        try:
            while time.monotonic() < deadline:
                if process.returncode is not None:
                    detail = " ; ".join(self._tail) or f"code {process.returncode}"
                    await self._stop_process()
                    return calibration.LoadOutcome(ok=False, detail=detail)
                try:
                    response = await self.http.request(
                        "GET", f"{self.base_url}/health", timeout=3.0
                    )
                except httpx.TransportError:
                    await asyncio.sleep(1.0)
                    continue
                if response.status == 200:
                    if process.returncode is not None:
                        await self._stop_process()
                        return calibration.LoadOutcome(
                            ok=False,
                            detail="llama-server s'est arrêté après /health",
                        )
                    try:
                        models = await self.http.request(
                            "GET", f"{self.base_url}/v1/models", timeout=3.0
                        )
                    except httpx.TransportError:
                        await asyncio.sleep(1.0)
                        continue
                    if process.returncode is not None:
                        await self._stop_process()
                        return calibration.LoadOutcome(
                            ok=False,
                            detail="llama-server s'est arrêté pendant /v1/models",
                        )
                    advertised = _advertised_model_ids(models.body)
                    if models.status != 200 or request.model_id not in advertised:
                        await self._stop_process()
                        return calibration.LoadOutcome(
                            ok=False,
                            detail=(
                                "identité du serveur de calibration inattendue : "
                                f"alias {request.model_id!r} absent de /v1/models"
                            ),
                        )
                    self._loaded_model = request.model_id
                    return calibration.LoadOutcome(
                        ok=True, runtime_version=f"b{version.build}"
                    )
                await asyncio.sleep(1.0)
        except BaseException:
            # Une annulation ne doit jamais laisser le processus isolé occuper
            # la VRAM. `shield` laisse le nettoyage aller au bout avant de
            # propager CancelledError/KeyboardInterrupt.
            cleanup = asyncio.create_task(self._stop_process())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
            raise
        await self._stop_process()
        return calibration.LoadOutcome(
            ok=False,
            detail=f"health absente après {self.load_timeout_seconds:g} s",
        )

    async def unload_model(self, model_id: str) -> calibration.UnloadOutcome:
        if self._process is None:
            return calibration.UnloadOutcome(ok=True)
        if self._loaded_model and self._loaded_model != model_id:
            return calibration.UnloadOutcome(
                ok=False, detail="le processus chargé ne correspond pas au modèle demandé"
            )
        try:
            await self._stop_process()
        except OSError as exc:
            return calibration.UnloadOutcome(ok=False, detail=str(exc))
        return calibration.UnloadOutcome(ok=True)

    async def run_prompt(self, model_id: str, prompt: str) -> calibration.PromptOutcome:
        if self._process is None or self._loaded_model != model_id:
            return calibration.PromptOutcome(ok=False, detail="modèle non chargé")
        started = time.monotonic()
        first_content: float | None = None
        prompt_tokens = 0
        completion_tokens = 0
        try:
            async with asyncio.timeout(60.0):
                async with self.http.stream(
                    "POST",
                    f"{self.base_url}/v1/chat/completions",
                    json={
                        "model": model_id,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 16,
                        "temperature": 0,
                        "stream": True,
                        "stream_options": {"include_usage": True},
                    },
                    timeout=60.0,
                ) as response:
                    if response.status != 200:
                        return calibration.PromptOutcome(
                            ok=False, detail=f"génération HTTP {response.status}"
                        )
                    async for raw in response.aiter_lines():
                        line = raw.strip()
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            continue
                        try:
                            chunk = json.loads(payload)
                        except ValueError:
                            continue
                        usage = chunk.get("usage") if isinstance(chunk, dict) else None
                        if isinstance(usage, dict):
                            prompt_tokens = _positive_int(
                                usage.get("prompt_tokens"), prompt_tokens
                            )
                            completion_tokens = _positive_int(
                                usage.get("completion_tokens"), completion_tokens
                            )
                        choices = chunk.get("choices") if isinstance(chunk, dict) else None
                        for choice in choices if isinstance(choices, list) else ():
                            delta = choice.get("delta") if isinstance(choice, dict) else None
                            content = delta.get("content") if isinstance(delta, dict) else None
                            if isinstance(content, str) and content and first_content is None:
                                first_content = time.monotonic()
        except Exception as exc:
            return calibration.PromptOutcome(
                ok=False,
                detail=execution.redact_for_log(f"{type(exc).__name__}: {exc}"),
            )
        finished = time.monotonic()
        if first_content is None or prompt_tokens <= 0 or completion_tokens <= 0:
            return calibration.PromptOutcome(
                ok=False, detail="flux sans contenu ou compteurs d'usage absents"
            )
        prompt_seconds = first_content - started
        generation_seconds = finished - first_content
        return calibration.PromptOutcome(
            ok=prompt_seconds > 0 and generation_seconds > 0,
            ttft_ms=max(int(round(prompt_seconds * 1000)), 1),
            prompt_tokens=prompt_tokens,
            prompt_seconds=prompt_seconds,
            generation_tokens=completion_tokens,
            generation_seconds=generation_seconds,
            detail="" if generation_seconds > 0 else "durée de génération nulle",
        )

    def as_probes(self) -> calibration.CalibrationProbes:
        return calibration.CalibrationProbes(
            read_vram=self.read_vram,
            read_ram=self.read_ram,
            load_model=self.load_model,
            unload_model=self.unload_model,
            run_prompt=self.run_prompt,
            sleep=asyncio.sleep,
            validate_environment=self.validate_environment,
        )

    def _command(
        self, target: CalibrationTarget, request: calibration.LoadRequest
    ) -> list[str]:
        params = target.params
        command = [
            str(self.binary), "--model", str(target.model_path),
            "--alias", request.model_id,
            "--host", "127.0.0.1", "--port", str(self.port),
            "-ngl", str(params.n_gpu_layers), "-c", str(request.ctx_size),
            "--parallel", str(request.parallel),
            "-b", str(params.batch_size), "-ub", str(params.ubatch_size),
            "-ctk", params.cache_type_k, "-ctv", params.cache_type_v,
            "-t", str(params.threads), "--threads-http", str(params.threads_http),
            "--cont-batching", "--cache-prompt", "--metrics",
        ]
        if params.flash_attention:
            command.extend(("-fa", "on"))
        if params.cpu_moe:
            command.append("--cpu-moe")
        if target.mmproj_path is not None:
            command.extend(("--mmproj", str(target.mmproj_path)))
        return command

    def _process_env(self) -> dict[str, str]:
        # Même règle que le pool de production (`server_manager.build_child_env`) :
        # ce llama-server parse un GGUF, il n'hérite d'aucun secret de l'opérateur.
        environment = inherited_child_env("CUDA_VISIBLE_DEVICES")
        if self.visible_gpu_uuids is not None:
            environment["CUDA_VISIBLE_DEVICES"] = ",".join(self.visible_gpu_uuids)
        return environment

    async def _drain(self, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                return
            self._tail.append(line.decode("utf-8", errors="replace").rstrip()[:500])

    async def _stop_process(self) -> None:
        process = self._process
        self._process = None
        self._loaded_model = ""
        if process is not None and process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
        if self._drains:
            await asyncio.gather(*self._drains, return_exceptions=True)
            self._drains.clear()


async def _subprocess_output(
    command: Sequence[str], *, timeout: float
) -> tuple[int, str, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        return 127, "", f"{command[0]} indisponible : {exc}"
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return 124, "", f"{command[0]} a dépassé {timeout:g} s"
    output = stdout.decode("utf-8", errors="replace")
    detail = stderr.decode("utf-8", errors="replace").strip()[:500]
    return int(process.returncode or 0), output, detail


def _read_ram() -> calibration.MemoryReading:
    try:
        values: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, _, raw = line.partition(":")
            if key in {"MemTotal", "MemAvailable"}:
                values[key] = int(raw.strip().split()[0]) * 1024
        total = values["MemTotal"]
        available = values["MemAvailable"]
    except (OSError, KeyError, ValueError, IndexError) as exc:
        return calibration.MemoryReading(ok=False, detail=f"/proc/meminfo illisible : {exc}")
    if total <= 0 or not 0 <= available <= total:
        return calibration.MemoryReading(ok=False, detail="mesure RAM incohérente")
    return calibration.MemoryReading(ok=True, used_bytes=total - available, total_bytes=total)


def _positive_int(value: Any, fallback: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return fallback
    return value


def hardware_fingerprint_from_plan(document: Mapping[str, Any]) -> str:
    """Adapte les noms AUT-002 vers le contrat d'empreinte AUT-008."""
    hardware = _object(_section(document, schema.SECTION_HARDWARE).get("data"), "hardware.data")
    raw_gpus = hardware.get("gpus")
    if not isinstance(raw_gpus, list):
        raise ProductionWiringError("hardware.data.gpus doit être une liste")
    gpus = []
    for index, raw in enumerate(raw_gpus):
        gpu = _object(raw, f"hardware.data.gpus[{index}]")
        if gpu.get("visible") is not True:
            continue
        total = _strict_int(gpu.get("vram_total_bytes"), f"gpus[{index}].vram_total_bytes")
        gpus.append({
            "name": _string(gpu.get("model"), f"gpus[{index}].model"),
            "vram_total_mib": total // _MIB,
            "driver_version": _string(
                gpu.get("driver_version"), f"gpus[{index}].driver_version"
            ),
            "compute_cap": _string(
                gpu.get("compute_capability"), f"gpus[{index}].compute_capability"
            ),
        })
    return calibration.hardware_fingerprint(gpus)


def visible_gpu_indices_from_plan(document: Mapping[str, Any]) -> tuple[int, ...]:
    """Index nvidia-smi des seuls GPU exposés lors de l'inventaire relu."""
    hardware = _object(_section(document, schema.SECTION_HARDWARE).get("data"), "hardware.data")
    raw_gpus = hardware.get("gpus")
    if not isinstance(raw_gpus, list):
        raise ProductionWiringError("hardware.data.gpus doit être une liste")
    indices: list[int] = []
    for position, raw in enumerate(raw_gpus):
        gpu = _object(raw, f"hardware.data.gpus[{position}]")
        if gpu.get("visible") is not True:
            continue
        indices.append(_strict_int(gpu.get("index"), f"gpus[{position}].index"))
    if not indices:
        raise ProductionWiringError(
            "le plan ne porte aucun index GPU visible : la calibration VRAM refuse "
            "de sommer les GPU masqués par CUDA_VISIBLE_DEVICES"
        )
    return tuple(indices)


def visible_gpu_uuids_from_plan(document: Mapping[str, Any]) -> tuple[str, ...]:
    """UUID stables à imposer au processus de calibration relu."""
    hardware = _object(_section(document, schema.SECTION_HARDWARE).get("data"), "hardware.data")
    raw_gpus = hardware.get("gpus")
    if not isinstance(raw_gpus, list):
        raise ProductionWiringError("hardware.data.gpus doit être une liste")
    uuids = tuple(
        _string(_object(raw, f"hardware.data.gpus[{position}]").get("uuid"),
                f"gpus[{position}].uuid")
        for position, raw in enumerate(raw_gpus)
        if isinstance(raw, dict) and raw.get("visible") is True
    )
    if not uuids or len(set(uuids)) != len(uuids):
        raise ProductionWiringError(
            "le plan doit porter des UUID GPU visibles, non vides et uniques"
        )
    return uuids


def calibration_targets(
    catalogue: catalog_mod.Catalog, models_dir: Path, model_ids: Sequence[str]
) -> dict[str, CalibrationTarget]:
    """Dérive les paramètres effectifs des seules cibles du plan."""
    targets: dict[str, CalibrationTarget] = {}
    for model_id in model_ids:
        entry = catalogue.get(model_id)
        if entry is None or not entry.plannable:
            raise ProductionWiringError(f"modèle de calibration inconnu ou bloqué : {model_id!r}")
        weights = [item for item in entry.files if item.role == "weights"]
        shards = [item for item in entry.files if item.role == "weights_shard"]
        mmproj = [item for item in entry.files if item.role == "mmproj"]
        if weights and shards:
            raise ProductionWiringError(
                f"{model_id!r} mélange poids monolithiques et shards"
            )
        if weights:
            principals = weights
        else:
            principals = [
                item for item in shards
                if re.search(r"-00001-of-\d{5}\.gguf$", item.name)
            ]
        if len(principals) != 1 or len(mmproj) > 1:
            raise ProductionWiringError(
                f"ensemble de fichiers inexploitable pour la calibration de {model_id!r}"
            )
        defaults = entry.runtime.defaults
        params = calibration.CalibrationParams(
            ctx_size=defaults.ctx_size,
            parallel=defaults.parallel,
            cache_type_k=defaults.cache_type_k,
            cache_type_v=defaults.cache_type_v,
            n_gpu_layers=999,
            flash_attention=True,
            batch_size=4096,
            ubatch_size=512,
            threads=8,
            threads_http=4,
            cpu_moe=False,
            reduced_ctx_size=min(calibration.DEFAULT_REDUCED_CTX_SIZE, defaults.ctx_size),
            reduced_parallel=min(calibration.DEFAULT_REDUCED_PARALLEL, defaults.parallel),
        )
        targets[model_id] = CalibrationTarget(
            model_path=Path(models_dir) / principals[0].name,
            mmproj_path=(Path(models_dir) / mmproj[0].name) if mmproj else None,
            params=params,
        )
    return targets


def generation_probe_from_recipe(
    *,
    settings: first_token.FirstTokenSettings,
    client: AsyncHttpClient,
    admin_secret: str,
) -> warmup.GenerationProbe:
    """Réutilise la recette AUT-009 au lieu d'inventer une seconde génération."""

    async def probe() -> warmup.ProbeOutcome:
        report = await first_token.run_first_token_recipe(
            settings=settings,
            client=client,
            admin_secret=admin_secret,
            context=execution.ExecutionContext(execution.ExecutionMode.APPLY),
            sleep=asyncio.sleep,
        )
        stream = report.stream
        return warmup.ProbeOutcome(
            served=report.served,
            reason=report.reason,
            ttft_ms=stream.ttft_ms if stream is not None else -1,
            detail="" if report.served else "la recette complète n'a pas autorisé le trafic",
        )

    return probe


def generation_probe_factory_from_recipe(
    *,
    settings: first_token.FirstTokenSettings,
    client: AsyncHttpClient,
    admin_secret: str,
):
    """Fabrique une sonde liée à chaque ``warmup_model`` d'un plan multi-modèle."""

    def factory(model_id: str) -> warmup.GenerationProbe:
        if not isinstance(model_id, str) or not model_id.strip():
            raise ProductionWiringError("cible de préchauffage vide")
        return generation_probe_from_recipe(
            settings=replace(settings, model_id=model_id),
            client=client,
            admin_secret=admin_secret,
        )

    return factory


__all__ = [
    "AsyncHttpClient",
    "CalibrationTarget",
    "derive_live_registry_lease_seconds",
    "LlamaServerCalibrationProbes",
    "LiveRegistrySyncClient",
    "ProductionWiringError",
    "calibration_targets",
    "generation_probe_factory_from_recipe",
    "generation_probe_from_recipe",
    "hardware_fingerprint_from_plan",
    "license_acceptances",
    "read_admin_secret",
    "runtime_installer_from_plan",
    "runtime_resolution_from_plan",
    "validate_gateway_urls",
    "visible_gpu_indices_from_plan",
    "visible_gpu_uuids_from_plan",
]
