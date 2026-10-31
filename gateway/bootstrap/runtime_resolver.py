"""
AUT-003 — résolution de `llama-server` et manifeste de provenance (§6).

Ce que ce module décide
-----------------------
Étant donné un profil matériel abstrait, il répond à une seule question :
« quel binaire `llama-server` installerait-on ici, d'où viendrait-il, et
pourquoi ? ». Il produit une **décision motivée** et le **manifeste attendu**.
Conformément au jalon M1, il fonctionne en dry-run intégral : il ne télécharge
rien, ne compile rien et ne pose aucun fichier.

Ordre de résolution (§6, « Stratégie de résolution »)
-----------------------------------------------------
1. artefact natif officiel `llama.cpp` si le backend visé existe ;
2. image officielle GHCR **épinglée par digest**, si le mode conteneur est
   accepté ;
3. artefact EVARuntime construit en CI depuis un tag/commit officiel figé ;
4. build local reproductible et mis en cache ;
5. **refus explicite** si aucune variante sûre ne correspond.

Cet ordre est appliqué *par source*, mais la recherche est menée **backend
d'abord** : tous les backends GPU visés sont éprouvés à travers les quatre
sources avant que le CPU ne soit seulement envisagé. Boucler dans l'autre sens
ferait gagner une archive officielle CPU contre une image CUDA officielle — soit
exactement la bascule silencieuse GPU → CPU que §6 interdit.

Pourquoi le CPU n'est jamais un repli tacite
--------------------------------------------
Un `llama-server` CPU s'installe, démarre, répond, et passe le smoke test. Il
donne donc toutes les apparences d'une installation réussie tout en produisant un
TTFT inacceptable sur un hôte équipé d'un GPU. Le défaut ne se voit qu'en
production, des semaines plus tard. Deux issues seulement sont admises ici :

- **refus** (défaut) : `allow_cpu_fallback=False`, la résolution échoue et dit
  quel backend GPU était visé ;
- **dégradation assumée** : `allow_cpu_fallback=True`, la variante CPU est
  retenue mais `RuntimeResolution.degraded` est vrai et un constat `warn`
  énonce ce que l'opérateur perd.

Il n'existe pas de troisième chemin, et aucun chemin où le drapeau se perd.

Épinglage obligatoire
---------------------
Une variante n'est éligible que si elle est **vérifiable avant installation** :
`artifact_sha256` pour une archive, `container_digest` pour une image. Une
variante non épinglée est écartée avec sa raison, pas installée « en attendant ».
C'est la moitié « SHA vérifié » du critère d'acceptation. Le build local fait
exception à la lettre, pas à l'esprit : son empreinte n'existe qu'après la
construction, sa reproductibilité repose sur le couple tag + commit figé de la
politique de release et sur ses options de build, toutes deux inscrites au
manifeste.

Ce que ce module n'invente pas
------------------------------
`DEFAULT_VARIANTS` décrit la matrice d'artefacts **sans digest**, parce qu'aucun
digest n'est vérifié dans ce dépôt à ce jour. La conséquence est voulue : avec la
politique par défaut, seules les variantes `local-build` sont éligibles, et les
autres apparaissent dans les motifs de rejet avec la mention « non épinglé ».
L'opérateur — ou la CI — fournit les variantes épinglées via `ResolverPolicy`.

Chaque entrée porte son niveau de preuve : `EVIDENCE_SPEC` quand §6 l'affirme,
`EVIDENCE_ASSUMPTION` quand elle est plausible mais non vérifiée ici (pas d'accès
réseau). Une variante retenue sur hypothèse déclenche un `warn` : le plan ne doit
jamais présenter une supposition comme un fait.
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from llama_version import LlamaVersion, probe_llama_version

from . import schema

SECTION_NAME = schema.SECTION_RUNTIME
SECTION_VERSION = 1

# Dépôt amont. Constant : le manifeste doit désigner sans ambiguïté le projet
# dont le binaire est issu, y compris quand l'artefact est reconstruit par nous.
PROJECT = "ggml-org/llama.cpp"

# ── Sources, dans l'ordre de préférence de §6 ─────────────────────────────────

SOURCE_OFFICIAL_RELEASE = "official-release"
SOURCE_OFFICIAL_CONTAINER = "official-container"
SOURCE_EVARUNTIME_BUILD = "evaruntime-build"
SOURCE_LOCAL_BUILD = "local-build"

SOURCE_ORDER: tuple[str, ...] = (
    SOURCE_OFFICIAL_RELEASE,
    SOURCE_OFFICIAL_CONTAINER,
    SOURCE_EVARUNTIME_BUILD,
    SOURCE_LOCAL_BUILD,
)
SOURCES: frozenset[str] = frozenset(SOURCE_ORDER)

# Sources qui produisent l'artefact chez nous : elles seules doivent porter des
# options de build au manifeste, et elles seules peuvent mentir sur le backend
# réellement compilé — d'où les invariants de cohérence plus bas.
BUILD_SOURCES: frozenset[str] = frozenset({SOURCE_EVARUNTIME_BUILD, SOURCE_LOCAL_BUILD})

# ── Backends ──────────────────────────────────────────────────────────────────

BACKEND_CUDA12 = "cuda12"
BACKEND_CUDA13 = "cuda13"
BACKEND_ROCM = "rocm"
BACKEND_VULKAN = "vulkan"
BACKEND_SYCL = "sycl"
BACKEND_METAL = "metal"
BACKEND_CPU = "cpu"

BACKENDS: frozenset[str] = frozenset({
    BACKEND_CUDA12,
    BACKEND_CUDA13,
    BACKEND_ROCM,
    BACKEND_VULKAN,
    BACKEND_SYCL,
    BACKEND_METAL,
    BACKEND_CPU,
})

# Tout ce qui n'est pas `cpu`. `metal` en fait partie : sur macOS, retomber sur
# le CPU est la même régression de TTFT qu'ailleurs.
GPU_BACKENDS: frozenset[str] = BACKENDS - {BACKEND_CPU}

# Option CMake qui prouve, dans un manifeste de build, que le backend annoncé a
# effectivement été compilé. Un `backend: cuda12` sans `GGML_CUDA: true` décrit
# un binaire CPU portant une étiquette GPU : le manifeste le refuse.
_BACKEND_BUILD_FLAG: dict[str, str] = {
    BACKEND_CUDA12: "GGML_CUDA",
    BACKEND_CUDA13: "GGML_CUDA",
    BACKEND_ROCM: "GGML_HIP",
    BACKEND_VULKAN: "GGML_VULKAN",
    BACKEND_SYCL: "GGML_SYCL",
    BACKEND_METAL: "GGML_METAL",
}

# ── Niveaux de preuve de la matrice d'artefacts ───────────────────────────────

# Affirmé par la spécification de la matrice, sans preuve d'exécution.
EVIDENCE_SPEC = schema.EVIDENCE_SPEC
# Plausible, non vérifié : ce module n'a pas d'accès réseau et ne consulte donc
# aucune page de release. À confirmer contre la matrice amont avant de s'en
# servir en production.
EVIDENCE_ASSUMPTION = schema.EVIDENCE_ASSUMPTION
# Relevé par l'opérateur — ou par une CI — puis fourni via `ResolverPolicy`
# (AUT-018, chargeur `runtime_variants`). Ce n'est ni une affirmation de §6, qui
# ne connaît aucune empreinte, ni une hypothèse de ce module : c'est un constat
# EXTERNE, dont le niveau de preuve appartient à celui qui l'a relevé. Le
# chargeur impose ce niveau et interdit à un fichier de se réclamer de §6 : une
# empreinte fournie ne devient pas une affirmation de la spécification.
EVIDENCE_OPERATOR = schema.EVIDENCE_OPERATOR

_EVIDENCE_LEVELS: frozenset[str] = frozenset({
    EVIDENCE_SPEC, EVIDENCE_ASSUMPTION, EVIDENCE_OPERATOR,
})

# ── Signature du clone superficiel (§0.10) ────────────────────────────────────
#
# Un `git clone --depth 1` de llama.cpp prive le build du compte de révisions :
# le binaire se déclare `version: 1`. Le symptôme observé est « build trop
# ancien », la cause réelle est un clone tronqué. Confondre les deux envoie
# l'opérateur mettre à jour un dépôt déjà à jour.
SHALLOW_CLONE_MAX_BUILD = 1

_VERSION_RE = re.compile(r"^b(\d+)$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
_PLATFORM_RE = re.compile(r"^[a-z0-9]+-[a-z0-9_]+$")

# Nom du paquet, tel que la gateway l'importe (`from bootstrap import …`). Sert à
# ramener les trois écritures d'un import de module frère — `from . import x`,
# `from .x import y`, `from bootstrap import x` — à une seule forme canonique.
PACKAGE = "bootstrap"

# Modules dont l'import prouverait que le résolveur sort de son bac à sable :
# réseau ou sous-processus arbitraire. Le seul sous-processus admissible est
# `llama-server --version`, et il n'est pas lancé ici : il est délégué à
# `llama_version.probe_llama_version`, injectable pour les tests.
FORBIDDEN_IMPORTS: frozenset[str] = frozenset({
    "subprocess", "socket", "ssl", "urllib", "http", "httpx", "requests", "ftplib",
})

# TST-007 — la même interdiction, par la bande.
#
# Interdire `socket` sans interdire `from . import public_https` ne protège de
# rien : le module frère apporte exactement la capacité qu'on refuse, et il
# l'apporte sous un nom qui n'est dans aucune liste stdlib. C'est la porte par
# laquelle AUT-018 a failli entrer.
#
# Cet ensemble est la fermeture transitive « modules du paquet qui atteignent un
# `FORBIDDEN_IMPORTS` », et il n'a pas à être tenu à jour à la main : un test de
# complétude le recalcule depuis les sources et échoue si un frère nouvellement
# doté de réseau ou de sous-processus n'y figure pas.
FORBIDDEN_SIBLING_IMPORTS: frozenset[str] = frozenset({
    f"{PACKAGE}.applier",
    f"{PACKAGE}.downloader",
    f"{PACKAGE}.inventory",
    f"{PACKAGE}.llmfit",
    f"{PACKAGE}.planner",
    f"{PACKAGE}.production",
    f"{PACKAGE}.public_https",
    f"{PACKAGE}.runtime_installer",
    f"{PACKAGE}.runtime_variants",
})


class ProvenanceError(schema.PlanError):
    """Manifeste, variante ou politique incohérents — refusés à la construction."""


# ── Manifeste de provenance (§6) ──────────────────────────────────────────────

@dataclass(frozen=True)
class ProvenanceManifest:
    """
    Le YAML `runtime:` de §6, sous une forme qui ne peut pas être incohérente.

    Toutes les règles sont appliquées à la construction : il n'existe aucun
    chemin produisant un manifeste invalide qu'on validerait « plus tard ». Un
    manifeste est la seule trace de ce qu'un hôte exécute réellement ; un
    manifeste approximatif vaut moins que pas de manifeste, parce qu'on lui fait
    confiance.

    Invariants :

    - `version` est un tag de build llama.cpp (`bNNNNN`) — c'est ce qui rend
      `LLAMA_SERVER_MIN_BUILD` comparable à ce que rapporte `--version` ;
    - une archive (`official-release`, `evaruntime-build`) exige `artifact_sha256`
      et interdit `container_digest` ; une image (`official-container`) exige
      l'inverse. Un manifeste ne peut pas être les deux, ni ni l'un ni l'autre ;
    - `local-build` est la seule source admise sans empreinte : elle n'existe
      qu'après la construction. En échange, ses `build_options` sont obligatoires,
      sans quoi le build n'est pas reproductible et la source est un mensonge ;
    - une source de build annonçant un backend GPU doit porter l'option CMake
      correspondante à `true`. C'est le repli CPU silencieux attrapé au niveau du
      document plutôt qu'au niveau du code.
    """
    version: str
    commit: str
    source: str
    backend: str
    platform: str
    installed_at: str
    artifact_sha256: str | None = None
    container_digest: str | None = None
    build_options: Mapping[str, bool | int | str] = field(default_factory=dict)
    project: str = PROJECT

    def __post_init__(self) -> None:
        errors = _validate_manifest_fields(
            project=self.project,
            version=self.version,
            commit=self.commit,
            source=self.source,
            backend=self.backend,
            platform=self.platform,
            installed_at=self.installed_at,
            artifact_sha256=self.artifact_sha256,
            container_digest=self.container_digest,
            build_options=self.build_options,
        )
        if errors:
            raise ProvenanceError("manifeste de provenance incohérent : " + " ; ".join(errors))
        # Copie défensive : un `dict` passé par l'appelant reste mutable même
        # dans un dataclass gelé.
        object.__setattr__(self, "build_options", dict(self.build_options))

    @property
    def build_number(self) -> int:
        """Numéro de build entier, comparable à ce que rend `--version`."""
        match = _VERSION_RE.match(self.version)
        if match is None:  # garanti par __post_init__ — mais `python -O` retirerait un assert
            raise ProvenanceError(
                f"manifeste de provenance incohérent : version {self.version!r} n'est pas un tag "
                "de build llama.cpp « bNNNNN » et son numéro de build est donc indéterminable"
            )
        return int(match.group(1))

    @property
    def is_verifiable(self) -> bool:
        """Vrai si une empreinte permet de contrôler l'artefact avant installation."""
        return bool(self.artifact_sha256 or self.container_digest)

    def to_dict(self) -> dict[str, Any]:
        """Contenu du bloc `runtime:`, dans l'ordre de champs de §6."""
        return {
            "project": self.project,
            "version": self.version,
            "commit": self.commit,
            "source": self.source,
            "backend": self.backend,
            "platform": self.platform,
            "artifact_sha256": self.artifact_sha256,
            "container_digest": self.container_digest,
            "build_options": dict(self.build_options),
            "installed_at": self.installed_at,
        }

    def to_document(self) -> dict[str, Any]:
        """Document complet, tel qu'il est écrit sur disque : `{"runtime": {…}}`."""
        return {"runtime": self.to_dict()}

    def to_yaml(self) -> str:
        """Rend le manifeste en YAML §6. `sort_keys=False` préserve l'ordre lisible."""
        return yaml.safe_dump(self.to_document(), sort_keys=False, allow_unicode=True)


def _validate_manifest_fields(
    *,
    project: Any,
    version: Any,
    commit: Any,
    source: Any,
    backend: Any,
    platform: Any,
    installed_at: Any,
    artifact_sha256: Any,
    container_digest: Any,
    build_options: Any,
) -> list[str]:
    """Règles du manifeste, partagées par le dataclass et par la relecture YAML."""
    errors: list[str] = []

    if not isinstance(project, str) or not project:
        errors.append("project doit être une chaîne non vide")

    if not isinstance(version, str) or not _VERSION_RE.match(version):
        errors.append(f"version doit être un tag de build llama.cpp « bNNNNN », reçu {version!r}")

    if not isinstance(commit, str) or not _COMMIT_RE.match(commit):
        errors.append(f"commit doit être un SHA git hexadécimal (7 à 40 caractères), reçu {commit!r}")

    if source not in SOURCES:
        errors.append(f"source inconnue : {source!r} (attendu parmi {sorted(SOURCES)})")

    if backend not in BACKENDS:
        errors.append(f"backend inconnu : {backend!r} (attendu parmi {sorted(BACKENDS)})")

    if not isinstance(platform, str) or not _PLATFORM_RE.match(platform):
        errors.append(f"platform doit avoir la forme « os-arch » en minuscules, reçu {platform!r}")

    if not isinstance(installed_at, str) or not installed_at:
        errors.append("installed_at doit être une date ISO-8601 non vide")

    if not isinstance(build_options, Mapping):
        errors.append("build_options doit être un objet")
        build_options = {}
    else:
        for key, value in build_options.items():
            if not isinstance(key, str) or not key:
                errors.append(f"build_options : clé invalide {key!r}")
            if not isinstance(value, (bool, int, str)):
                errors.append(
                    f"build_options[{key!r}] doit être un booléen, un entier ou une chaîne, "
                    f"reçu {type(value).__name__}"
                )

    if artifact_sha256 is not None and (
        not isinstance(artifact_sha256, str) or not _SHA256_RE.match(artifact_sha256)
    ):
        errors.append("artifact_sha256 doit être 64 caractères hexadécimaux minuscules")

    if container_digest is not None and (
        not isinstance(container_digest, str) or not _DIGEST_RE.match(container_digest)
    ):
        errors.append("container_digest doit avoir la forme « sha256:<64 hex> »")

    if source == SOURCE_OFFICIAL_CONTAINER:
        if container_digest is None:
            errors.append(
                "source « official-container » sans container_digest : une image non épinglée "
                "par digest peut être remplacée sous le même tag (§6)"
            )
        if artifact_sha256 is not None:
            errors.append("source « official-container » ne doit pas porter d'artifact_sha256")
    elif source in SOURCES:
        if container_digest is not None:
            errors.append(f"source « {source} » ne doit pas porter de container_digest")
        if source != SOURCE_LOCAL_BUILD and artifact_sha256 is None:
            errors.append(
                f"source « {source} » sans artifact_sha256 : l'artefact serait installé "
                "sans contrôle d'intégrité (§6)"
            )

    if source == SOURCE_LOCAL_BUILD and not build_options:
        errors.append(
            "source « local-build » sans build_options : un build local sans ses options "
            "n'est pas reproductible et sa provenance n'est pas attestable"
        )

    if source in BUILD_SOURCES and backend in GPU_BACKENDS:
        flag = _BACKEND_BUILD_FLAG.get(str(backend), "")
        options = build_options if isinstance(build_options, Mapping) else {}
        if not options.get(flag):
            errors.append(
                f"backend « {backend} » construit sans {flag}=true : le binaire serait un "
                "binaire CPU portant une étiquette GPU"
            )

    return errors


def validate_manifest_document(document: Any) -> tuple[str, ...]:
    """
    Contrôle un manifeste relu depuis un YAML. Retourne les erreurs, vide si sain.

    Existe pour le chemin inverse de `to_yaml()` : un fichier posé sur un hôte est
    relu par `doctor` ou par une mise à jour, et rien ne garantit qu'il ait été
    écrit par cette version du code — ni même par nous.
    """
    if not isinstance(document, dict):
        return (f"le manifeste doit être un objet, reçu {type(document).__name__}",)
    runtime = document.get("runtime")
    if not isinstance(runtime, dict):
        return ("le manifeste doit contenir un objet « runtime »",)
    return tuple(_validate_manifest_fields(
        project=runtime.get("project"),
        version=runtime.get("version"),
        commit=runtime.get("commit"),
        source=runtime.get("source"),
        backend=runtime.get("backend"),
        platform=runtime.get("platform"),
        installed_at=runtime.get("installed_at"),
        artifact_sha256=runtime.get("artifact_sha256"),
        container_digest=runtime.get("container_digest"),
        build_options=runtime.get("build_options") or {},
    ))


def attested_binary_sha256(document: Any) -> str | None:
    """
    Empreinte du binaire consignée par `runtime_installer` dans le bloc `install:`.

    C'est la seule valeur du manifeste qui parle du **binaire posé** : le
    `artifact_sha256` du bloc `runtime:` décrit l'archive téléchargée, pas ce qui
    a été extrait puis publié. Retourne None si le manifeste n'a pas été écrit par
    notre installateur, ou si le bloc a été retiré — auquel cas il n'atteste rien
    et `_judge_existing_binary` refuse la réutilisation (SEC-009).
    """
    if not isinstance(document, Mapping):
        return None
    bloc = document.get("install")
    if not isinstance(bloc, Mapping):
        return None
    empreinte = bloc.get("binary_sha256")
    if isinstance(empreinte, str) and _SHA256_RE.match(empreinte):
        return empreinte
    return None


# Nom du manifeste §6, posé À CÔTÉ du binaire par `runtime_installer` (AUT-016).
#
# SEC-017 — pourquoi ce nom vit ici et non seulement chez l'installateur : c'est
# le résolveur qui DÉFINIT le manifeste (`ProvenanceManifest`, sa validation, son
# empreinte de binaire). Le lire est donc une opération de décision, pas
# d'exécution. Le sens de dépendance de `bootstrap/__init__.py` interdit au
# résolveur d'importer `runtime_installer` — ce serait le décideur qui dépend de
# l'exécuteur, et le garde-fou d'isolation (TST-007) le refuse. Un test recoupe
# les deux constantes, de sorte qu'elles ne puissent pas diverger en silence.
MANIFEST_FILENAME = "provenance.yaml"


@dataclass(frozen=True)
class ExistingAttestation:
    """
    Ce que le manifeste posé à côté d'un binaire permet — ou non — d'affirmer.

    SEC-017 — fail-closed : `manifest` vaut `None` dès que le document est
    absent, illisible ou incohérent. Une attestation partielle n'existe pas ; un
    manifeste qu'on n'a pas su relire ne vaut pas mieux qu'aucun manifeste, et
    `_judge_existing_binary` en tire alors le refus de réutilisation, pas une
    réutilisation silencieuse.

    `finding` est le **constat nommé** de ce qui a été trouvé sur disque : c'est
    lui qui distingue « pas de manifeste » de « manifeste illisible » de
    « manifeste incohérent », trois causes qui appellent trois gestes différents
    et que le refus aval, à lui seul, confondrait.
    """
    manifest: ProvenanceManifest | None
    binary_sha256: str | None
    finding: schema.Finding | None


def provenance_path(binary: Path) -> Path:
    """Chemin du manifeste §6 attendu à côté d'un binaire `llama-server`."""
    return binary.parent / MANIFEST_FILENAME


def read_existing_attestation(binary: Path) -> ExistingAttestation:
    """
    Relit le manifeste posé à côté du binaire. Ne lève jamais.

    SEC-017 — sans cette lecture, le recoupement manifeste ↔ binaire livré par
    SEC-009 n'était exercé que par les tests : aucun appelant ne fournissait
    jamais `existing_manifest`, et le parcours opérateur accordait donc
    `reuse_existing` sans qu'aucune attestation soit confrontée à quoi que ce
    soit. Le trou était réel, mais dormant.

    Les quatre issues, toutes nommées, aucune fatale :

    - **absent** — cas nominal sur un binaire compilé à la main. Constat `info`
      qui dit **où** le manifeste était attendu ; le refus de réutilisation, lui,
      vient de `_judge_existing_binary` (`runtime_provenance_unknown`) ;
    - **illisible** — fichier absent de droits, tronqué, ou YAML invalide ;
    - **incohérent** — relu, mais refusé par les règles de §6 ;
    - **sain** — le manifeste et, s'il la porte, l'empreinte du binaire posé.

    Dans les trois premiers cas `manifest` est `None` : l'absence de manifeste ne
    vaut jamais attestation.
    """
    chemin = provenance_path(binary)

    try:
        texte = chemin.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ExistingAttestation(None, None, schema.Finding(
            code="runtime_manifest_absent",
            level="info",
            message=(
                f"Aucun manifeste de provenance §6 à côté de {binary} (attendu : {chemin}). "
                "Le binaire en place ne peut donc pas être attesté. C'est le cas nominal "
                "d'un binaire compilé à la main ; `runtime_installer` en écrit un à chaque "
                "installation."
            ),
        ))
    except OSError as exc:
        return ExistingAttestation(None, None, schema.Finding(
            code="runtime_manifest_unreadable",
            level="warn",
            message=(
                f"Le manifeste de provenance {chemin} existe mais n'a pas pu être lu "
                f"({exc.__class__.__name__}). Un manifeste illisible ne vaut pas attestation : "
                "le binaire en place sera traité comme dépourvu de provenance et remplacé. "
                "Vérifiez ses droits de lecture."
            ),
        ))

    try:
        document = yaml.safe_load(texte)
    except yaml.YAMLError as exc:
        return ExistingAttestation(None, None, schema.Finding(
            code="runtime_manifest_unreadable",
            level="warn",
            message=(
                f"Le manifeste de provenance {chemin} n'est pas un YAML valide "
                f"({str(exc).splitlines()[0][:120]}). Il ne vaut pas attestation et le binaire "
                "en place sera remplacé."
            ),
        ))

    erreurs = validate_manifest_document(document)
    if erreurs:
        return ExistingAttestation(None, None, schema.Finding(
            code="runtime_manifest_invalid",
            level="warn",
            message=(
                f"Le manifeste de provenance {chemin} est incohérent : {' ; '.join(erreurs)}. "
                "Un manifeste approximatif vaut moins que pas de manifeste, parce qu'on lui "
                "fait confiance : il est écarté et le binaire sera remplacé."
            ),
        ))

    return ExistingAttestation(
        manifest=manifest_from_document(document),
        binary_sha256=attested_binary_sha256(document),
        finding=None,
    )


def manifest_from_document(document: Any) -> ProvenanceManifest:
    """Relit un manifeste YAML. Lève `ProvenanceError` s'il est incohérent."""
    errors = validate_manifest_document(document)
    if errors:
        raise ProvenanceError("manifeste de provenance incohérent : " + " ; ".join(errors))
    runtime = document["runtime"]
    return ProvenanceManifest(
        project=runtime.get("project", PROJECT),
        version=runtime["version"],
        commit=runtime["commit"],
        source=runtime["source"],
        backend=runtime["backend"],
        platform=runtime["platform"],
        artifact_sha256=runtime.get("artifact_sha256"),
        container_digest=runtime.get("container_digest"),
        build_options=runtime.get("build_options") or {},
        installed_at=runtime["installed_at"],
    )


# ── Matrice d'artefacts ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class ArtifactVariant:
    """
    Une variante de `llama-server` disponible pour un couple (backend, plateforme).

    `evidence` est obligatoire et n'est pas décoratif : la matrice mélange ce que
    §6 affirme et ce qui est seulement plausible. Sans ce champ, une hypothèse
    entrerait dans le plan avec l'autorité d'un fait.
    """
    source: str
    backend: str
    platform: str
    evidence: str
    evidence_note: str
    reference: str = ""
    artifact_sha256: str | None = None
    container_digest: str | None = None
    approx_bytes: int | None = None

    def __post_init__(self) -> None:
        errors: list[str] = []
        if self.source not in SOURCES:
            errors.append(f"source inconnue : {self.source!r}")
        if self.backend not in BACKENDS:
            errors.append(f"backend inconnu : {self.backend!r}")
        if not _PLATFORM_RE.match(self.platform):
            errors.append(f"platform invalide : {self.platform!r}")
        if self.evidence not in _EVIDENCE_LEVELS:
            errors.append(f"evidence doit valoir {sorted(_EVIDENCE_LEVELS)}, reçu {self.evidence!r}")
        if not self.evidence_note:
            errors.append("evidence_note est obligatoire : une entrée de matrice doit dire d'où elle vient")
        if self.artifact_sha256 is not None and not _SHA256_RE.match(self.artifact_sha256):
            errors.append("artifact_sha256 doit être 64 caractères hexadécimaux minuscules")
        if self.container_digest is not None and not _DIGEST_RE.match(self.container_digest):
            errors.append("container_digest doit avoir la forme « sha256:<64 hex> »")
        if errors:
            raise ProvenanceError("variante d'artefact incohérente : " + " ; ".join(errors))

    @property
    def requires_container(self) -> bool:
        return self.source == SOURCE_OFFICIAL_CONTAINER

    @property
    def is_pinned(self) -> bool:
        """Vrai si la variante est vérifiable avant installation."""
        if self.source == SOURCE_OFFICIAL_CONTAINER:
            return self.container_digest is not None
        if self.source == SOURCE_LOCAL_BUILD:
            # Rien à épingler : l'empreinte naît de la construction. La
            # reproductibilité vient du tag + commit figés de la politique.
            return True
        return self.artifact_sha256 is not None

    def label(self) -> str:
        return f"{self.source}/{self.backend}/{self.platform}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "backend": self.backend,
            "platform": self.platform,
            "evidence": self.evidence,
            "evidence_note": self.evidence_note,
            "reference": self.reference,
            "artifact_sha256": self.artifact_sha256,
            "container_digest": self.container_digest,
            "approx_bytes": self.approx_bytes,
        }


# Matrice par défaut. Aucun digest n'y figure : rien n'est épinglé dans ce dépôt
# à ce jour, et inventer une empreinte serait pire que de ne pas en avoir. La
# conséquence assumée est que seules les variantes `local-build` sont éligibles
# par défaut ; les autres apparaissent dans les motifs de rejet avec « non
# épinglé », ce qui est l'information utile pour l'opérateur.
DEFAULT_VARIANTS: tuple[ArtifactVariant, ...] = (
    # 1. Artefacts natifs officiels.
    #
    # §6 affirme une ABSENCE : « les archives natives de release Linux ne
    # couvrent pas nécessairement CUDA ». Il n'y a donc, délibérément, aucune
    # entrée `official-release` + `cuda12`/`cuda13` sur linux-*. C'est ce trou
    # qui fait basculer un hôte NVIDIA Linux vers le conteneur ou le build.
    ArtifactVariant(
        source=SOURCE_OFFICIAL_RELEASE, backend=BACKEND_CPU, platform="linux-x86_64",
        evidence=EVIDENCE_ASSUMPTION,
        evidence_note="Archive CPU linux-x86_64 supposée publiée par le projet ; non vérifiée ici.",
        reference="https://github.com/ggml-org/llama.cpp/releases",
    ),
    ArtifactVariant(
        source=SOURCE_OFFICIAL_RELEASE, backend=BACKEND_CPU, platform="linux-arm64",
        evidence=EVIDENCE_ASSUMPTION,
        evidence_note="Archive CPU linux-arm64 supposée publiée ; non vérifiée ici.",
        reference="https://github.com/ggml-org/llama.cpp/releases",
    ),
    ArtifactVariant(
        source=SOURCE_OFFICIAL_RELEASE, backend=BACKEND_METAL, platform="macos-arm64",
        evidence=EVIDENCE_ASSUMPTION,
        evidence_note="Archive macOS arm64 avec Metal supposée publiée ; non vérifiée ici.",
        reference="https://github.com/ggml-org/llama.cpp/releases",
    ),
    ArtifactVariant(
        source=SOURCE_OFFICIAL_RELEASE, backend=BACKEND_CPU, platform="windows-x86_64",
        evidence=EVIDENCE_ASSUMPTION,
        evidence_note="Archive CPU windows-x86_64 supposée publiée ; non vérifiée ici.",
        reference="https://github.com/ggml-org/llama.cpp/releases",
    ),

    # 2. Images officielles GHCR.
    ArtifactVariant(
        source=SOURCE_OFFICIAL_CONTAINER, backend=BACKEND_CUDA12, platform="linux-x86_64",
        evidence=EVIDENCE_SPEC,
        evidence_note="§6 « Cas NVIDIA Linux » : le projet officiel publie une image server-cuda.",
        reference="ghcr.io/ggml-org/llama.cpp:server-cuda",
    ),
    ArtifactVariant(
        source=SOURCE_OFFICIAL_CONTAINER, backend=BACKEND_CUDA13, platform="linux-x86_64",
        evidence=EVIDENCE_SPEC,
        evidence_note="§6 « Cas NVIDIA Linux » : le projet officiel publie une image server-cuda13.",
        reference="ghcr.io/ggml-org/llama.cpp:server-cuda13",
    ),
    ArtifactVariant(
        source=SOURCE_OFFICIAL_CONTAINER, backend=BACKEND_CPU, platform="linux-x86_64",
        evidence=EVIDENCE_ASSUMPTION,
        evidence_note="Image CPU « server » supposée publiée aux côtés des variantes CUDA ; non vérifiée ici.",
        reference="ghcr.io/ggml-org/llama.cpp:server",
    ),

    # 3. Artefacts EVARuntime construits en CI.
    #
    # Aucune entrée : §6 les présente comme un objectif « à moyen terme ». Aucune
    # CI de ce dépôt ne publie de binaire `llama-server`. Peupler cette source
    # ici décrirait un monde qui n'existe pas.

    # 4. Build local reproductible.
    ArtifactVariant(
        source=SOURCE_LOCAL_BUILD, backend=BACKEND_CUDA12, platform="linux-x86_64",
        evidence=EVIDENCE_SPEC,
        evidence_note="§6 : « à court terme, le build local depuis un tag officiel figé » est la voie retenue "
                      "pour CUDA sur Linux.",
    ),
    ArtifactVariant(
        source=SOURCE_LOCAL_BUILD, backend=BACKEND_CUDA13, platform="linux-x86_64",
        evidence=EVIDENCE_SPEC,
        evidence_note="§6 : même voie que cuda12 ; suppose une toolchain CUDA 13 présente sur l'hôte.",
    ),
    ArtifactVariant(
        source=SOURCE_LOCAL_BUILD, backend=BACKEND_ROCM, platform="linux-x86_64",
        evidence=EVIDENCE_ASSUMPTION,
        evidence_note="Build ROCm supposé possible ; dépend d'une toolchain HIP non vérifiée ici.",
    ),
    ArtifactVariant(
        source=SOURCE_LOCAL_BUILD, backend=BACKEND_VULKAN, platform="linux-x86_64",
        evidence=EVIDENCE_ASSUMPTION,
        evidence_note="Build Vulkan supposé possible ; dépend du SDK Vulkan, non vérifié ici.",
    ),
    ArtifactVariant(
        source=SOURCE_LOCAL_BUILD, backend=BACKEND_CPU, platform="linux-x86_64",
        evidence=EVIDENCE_SPEC,
        evidence_note="Build CPU exercé réellement lors du déploiement du 2026-07-30 (§0.10, GGML_CUDA=OFF).",
    ),
    ArtifactVariant(
        source=SOURCE_LOCAL_BUILD, backend=BACKEND_CPU, platform="linux-arm64",
        evidence=EVIDENCE_ASSUMPTION,
        evidence_note="Build CPU arm64 supposé possible ; jamais exercé dans ce dépôt.",
    ),
    ArtifactVariant(
        source=SOURCE_LOCAL_BUILD, backend=BACKEND_METAL, platform="macos-arm64",
        evidence=EVIDENCE_ASSUMPTION,
        evidence_note="Build Metal supposé possible avec Xcode ; jamais exercé dans ce dépôt.",
    ),
)


# ── Politique ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ReleasePolicy:
    """
    Politique de release : ce qu'on installe, et le plancher de sécurité exigé.

    §6 : « `LLAMA_SERVER_MIN_BUILD` doit être généré à partir de la politique de
    release ». C'est ici que ce nombre naît, et `derive_min_build()` est la seule
    façon correcte de l'obtenir.

    Aucune valeur par défaut n'est fournie pour `pinned_version` / `pinned_commit` :
    inscrire ici un numéro de build inventé le ferait passer pour un fait vérifié
    dans tous les manifestes produits. L'appelant fournit la politique.

    Invariant : on ne peut pas épingler un build strictement inférieur au plancher
    de sécurité — ce serait installer sciemment un binaire que la gateway
    refuserait ensuite de démarrer.
    """
    pinned_version: str
    pinned_commit: str
    security_floor_build: int = 0

    def __post_init__(self) -> None:
        errors: list[str] = []
        if not _VERSION_RE.match(self.pinned_version or ""):
            errors.append(f"pinned_version doit être « bNNNNN », reçu {self.pinned_version!r}")
        if not _COMMIT_RE.match(self.pinned_commit or ""):
            errors.append(f"pinned_commit doit être un SHA git hexadécimal, reçu {self.pinned_commit!r}")
        if not isinstance(self.security_floor_build, int) or self.security_floor_build < 0:
            errors.append("security_floor_build doit être un entier >= 0")
        if not errors and self.security_floor_build > 0 and self.pinned_build < self.security_floor_build:
            errors.append(
                f"pinned_version {self.pinned_version} (build {self.pinned_build}) est inférieure au "
                f"plancher de sécurité {self.security_floor_build} : la politique installerait un binaire "
                "que LLAMA_SERVER_MIN_BUILD refuserait"
            )
        if errors:
            raise ProvenanceError("politique de release incohérente : " + " ; ".join(errors))

    @property
    def pinned_build(self) -> int:
        match = _VERSION_RE.match(self.pinned_version or "")
        return int(match.group(1)) if match else -1


def derive_min_build(policy: ReleasePolicy) -> int:
    """
    Valeur de `LLAMA_SERVER_MIN_BUILD` générée depuis la politique de release (§6).

    C'est le **plancher de sécurité**, pas le build épinglé : la gateway doit
    accepter tout binaire au moins aussi patché que le premier build corrigé
    connu, pas seulement celui que le bootstrap a posé. Exiger le build épinglé
    reviendrait à refuser de démarrer après toute mise à jour manuelle vers un
    build plus récent que la politique, ou plus ancien mais patché.
    """
    return policy.security_floor_build


@dataclass(frozen=True)
class ResolverPolicy:
    """
    Ce que l'opérateur accepte. Chaque drapeau ouvre une branche de l'ordre de §6.

    `allow_cpu_fallback` est faux par défaut, et c'est le défaut qui compte : la
    dégradation GPU → CPU doit être demandée, jamais subie.
    """
    release: ReleasePolicy
    variants: tuple[ArtifactVariant, ...] = DEFAULT_VARIANTS
    allow_container: bool = False
    allow_local_build: bool = True
    allow_cpu_fallback: bool = False


# ── Profil matériel (entrée) ──────────────────────────────────────────────────

@dataclass(frozen=True)
class HardwareProfile:
    """
    Entrée minimale du résolveur — projection du profil d'inventaire de §5.

    Volontairement plus pauvre que l'inventaire complet (AUT-002) : le résolveur
    n'a besoin ni de la RAM, ni du disque, ni des flags CPU. Réduire la surface
    d'entrée, c'est réduire le nombre de façons dont un producteur amont peut
    casser la résolution.

    - `platform` : « os-arch » normalisé (`linux-x86_64`, `macos-arm64`…) ;
    - `backend_candidates` : backends supportés par l'hôte, **du plus souhaitable
      au moins souhaitable**. L'ordre est porteur de sens, il vient de
      l'inventaire ;
    - `gpu_vendor` / `driver_version` / `cuda_major` : diagnostic, et
      détermination de ce qui est réellement visé ;
    - `gpu_count` : nombre de GPU exposés. À zéro, un backend GPU présent dans
      `backend_candidates` n'est pas « visé » — c'est du bruit d'inventaire, et
      retenir le CPU n'est alors pas une dégradation.

    `hardware_profile_from_mapping()` projette le dictionnaire de §5 vers ce type.
    """
    platform: str
    backend_candidates: tuple[str, ...]
    gpu_vendor: str | None = None
    driver_version: str | None = None
    cuda_major: int | None = None
    gpu_count: int = 0

    @property
    def gpu_present(self) -> bool:
        return self.gpu_count > 0 or bool(self.gpu_vendor)

    @property
    def known_backends(self) -> tuple[str, ...]:
        """Candidats reconnus, dans l'ordre annoncé, sans doublon."""
        seen: set[str] = set()
        out: list[str] = []
        for backend in self.backend_candidates:
            if backend in BACKENDS and backend not in seen:
                seen.add(backend)
                out.append(backend)
        return tuple(out)

    @property
    def targeted_backend(self) -> str | None:
        """Backend GPU le plus souhaitable réellement visé, ou None sur un hôte CPU."""
        if not self.gpu_present:
            return None
        for backend in self.known_backends:
            if backend in GPU_BACKENDS:
                return backend
        return None


_OS_FAMILY: dict[str, str] = {
    "ubuntu": "linux", "debian": "linux", "rhel": "linux", "centos": "linux",
    "fedora": "linux", "rocky": "linux", "almalinux": "linux", "arch": "linux",
    "opensuse": "linux", "suse": "linux", "linux": "linux",
    "darwin": "macos", "macos": "macos", "osx": "macos",
    "windows": "windows", "win32": "windows",
}

_ARCH_ALIAS: dict[str, str] = {
    "x86_64": "x86_64", "amd64": "x86_64", "x64": "x86_64",
    "aarch64": "arm64", "arm64": "arm64",
}


def normalize_platform(os_name: str, arch: str) -> str:
    """
    Normalise (`os`, `arch`) de l'inventaire vers l'identifiant de plateforme.

    Une distribution inconnue est rendue telle quelle plutôt que devinée : elle ne
    trouvera aucune variante et le refus dira exactement quelle plateforme n'a pas
    été reconnue. Deviner « linux » produirait un artefact potentiellement faux.
    """
    family = _OS_FAMILY.get((os_name or "").strip().lower(), (os_name or "").strip().lower())
    machine = _ARCH_ALIAS.get((arch or "").strip().lower(), (arch or "").strip().lower())
    return f"{family}-{machine}" if family and machine else f"{family}{machine}"


def hardware_profile_from_mapping(raw: Mapping[str, Any]) -> HardwareProfile:
    """
    Projette le profil d'inventaire de §5 vers l'entrée du résolveur.

    Tolérante par construction : l'inventaire (AUT-002) est un producteur
    indépendant qui peut échouer partiellement. Un champ absent devient `None` ou
    zéro, et c'est la résolution — pas la projection — qui décide si ce manque est
    bloquant.
    """
    platform = str(raw.get("platform") or "").strip().lower()
    if not platform:
        platform = normalize_platform(str(raw.get("os") or ""), str(raw.get("arch") or ""))

    candidates = tuple(
        str(b).strip().lower() for b in (raw.get("backend_candidates") or ()) if str(b).strip()
    )

    gpus = [g for g in (raw.get("gpus") or ()) if isinstance(g, Mapping)]
    vendor = str(gpus[0].get("vendor") or "").strip().lower() or None if gpus else None
    driver = str(gpus[0].get("driver_version") or "").strip() or None if gpus else None

    cuda_major = raw.get("cuda_major")
    if not isinstance(cuda_major, int) or isinstance(cuda_major, bool):
        cuda_major = None
        for backend in candidates:
            match = re.match(r"^cuda(\d+)$", backend)
            if match:
                cuda_major = int(match.group(1))
                break

    return HardwareProfile(
        platform=platform,
        backend_candidates=candidates,
        gpu_vendor=vendor,
        driver_version=driver,
        cuda_major=cuda_major,
        gpu_count=len(gpus),
    )


# ── Résultat ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RuntimeResolution:
    """
    Décision motivée du résolveur. Un refus en est une, pas une exception avalée.

    `degraded` est le drapeau que le critère d'acceptation surveille : vrai
    signifie « on installe du CPU là où un GPU était visé ». Il ne peut être vrai
    sans qu'un constat `warn` l'accompagne — c'est vérifié par les tests.
    """
    profile: HardwareProfile
    min_build: int
    resolved: bool
    reuse_existing: bool
    degraded: bool
    targeted_backend: str | None
    variant: ArtifactVariant | None
    manifest: ProvenanceManifest | None
    observed_build: int | None
    summary: str
    findings: tuple[schema.Finding, ...]
    rejected: tuple[str, ...]

    # AUT-019 — la politique effectivement retenue, recopiée dans la décision.
    #
    # Ces trois drapeaux gouvernent quelles branches de §6 ont été ouvertes. Les
    # laisser dans `ResolverPolicy` seulement, c'est-à-dire dans l'invocation,
    # rendait le plan non interprétable : « aucune variante GPU sûre » ne veut pas
    # dire la même chose selon que le conteneur était accepté ou non, et un repli
    # CPU autorisé n'est pas un détail de ligne de commande.
    allow_container: bool = False
    allow_local_build: bool = True
    allow_cpu_fallback: bool = False

    @property
    def backend(self) -> str | None:
        return self.variant.backend if self.variant else None

    @property
    def status(self) -> schema.SectionStatus:
        if not self.resolved or any(f.level == "fail" for f in self.findings):
            return "fail"
        if any(f.level == "warn" for f in self.findings):
            return "warn"
        return "ok"

    def to_data(self) -> dict[str, Any]:
        """Contenu de `PlanSection.data` : JSON-sérialisable et sans secret."""
        return {
            "resolved": self.resolved,
            "reuse_existing": self.reuse_existing,
            "degraded": self.degraded,
            "platform": self.profile.platform,
            "backend_candidates": list(self.profile.backend_candidates),
            "targeted_backend": self.targeted_backend,
            "selected_backend": self.backend,
            "gpu_vendor": self.profile.gpu_vendor,
            "gpu_count": self.profile.gpu_count,
            "driver_version": self.profile.driver_version,
            "cuda_major": self.profile.cuda_major,
            "min_build": self.min_build,
            "observed_build": self.observed_build,
            "variant": self.variant.to_dict() if self.variant else None,
            "manifest": self.manifest.to_dict() if self.manifest else None,
            "rejected": list(self.rejected),
            "policy": {
                "allow_container": self.allow_container,
                "allow_local_build": self.allow_local_build,
                "allow_cpu_fallback": self.allow_cpu_fallback,
            },
        }


# ── Résolution ────────────────────────────────────────────────────────────────

ProbeCallable = Callable[[Path], Awaitable[LlamaVersion]]
DigestCallable = Callable[[Path], Awaitable[str | None]]


async def sha256_binary(path: Path) -> str | None:
    """
    Empreinte SHA-256 du binaire en place, lue en flux et hors boucle d'événements.

    Retourne None si le fichier est illisible : c'est un fait à constater, pas une
    exception à propager — l'appelant en tire un refus nommé.
    """
    def _lire() -> str | None:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as flux:
                for bloc in iter(lambda: flux.read(1024 * 1024), b""):
                    digest.update(bloc)
        except OSError:
            return None
        return digest.hexdigest()

    return await asyncio.to_thread(_lire)


async def resolve_runtime(
    profile: HardwareProfile,
    policy: ResolverPolicy,
    *,
    installed_at: str,
    existing_binary: Path | None = None,
    existing_manifest: ProvenanceManifest | None = None,
    existing_binary_sha256: str | None = None,
    probe: ProbeCallable | None = None,
    digest: DigestCallable | None = None,
) -> RuntimeResolution:
    """
    Décide quel `llama-server` serait installé sur cet hôte, et pourquoi.

    Ne télécharge rien, ne compile rien, n'écrit rien. Le seul sous-processus
    possible est `llama-server --version` sur un binaire **déjà présent**, délégué
    à `llama_version.probe_llama_version` (réutilisé, pas réimplémenté) et
    injectable par `probe` pour les tests.

    `existing_binary_sha256` est l'empreinte que l'installation avait **consignée**
    à côté du manifeste (`attested_binary_sha256()` l'extrait du document YAML).
    Elle n'est pas décorative : sans elle, un manifeste ne peut pas être recoupé
    contre le binaire qu'il prétend décrire, et il ne vaut donc pas attestation
    (SEC-009). L'empreinte réellement observée est calculée par `digest`,
    injectable pour les tests — le binaire n'est lu que si un manifeste est fourni.

    `installed_at` est fourni par l'appelant, comme `generated_at` dans `schema` :
    le module ne lit pas l'horloge, pour que le manifeste produit soit
    reproductible en test.
    """
    findings: list[schema.Finding] = []
    rejected: list[str] = []
    min_build = derive_min_build(policy.release)

    # AUT-019 — la politique voyage avec la décision, sur TOUS les chemins de
    # sortie. Un refus doit être aussi interprétable qu'un succès : « aucune
    # variante GPU sûre » ne veut pas dire la même chose selon que le conteneur
    # était accepté ou non.
    politique = {
        "allow_container": policy.allow_container,
        "allow_local_build": policy.allow_local_build,
        "allow_cpu_fallback": policy.allow_cpu_fallback,
    }

    # AUT-019 — un repli CPU AUTORISÉ se voit, qu'il soit emprunté ou non.
    #
    # §6 interdit le repli CPU silencieux. Le constat `cpu_fallback_degraded`
    # couvre le cas où il est effectivement pris ; il ne couvre pas le cas, tout
    # aussi important pour un relecteur, où l'autorisation était debout et n'a pas
    # servi. Un plan relu six mois plus tard doit dire sous quelle politique il a
    # été calculé, sans quoi on le compare à un autre plan sans savoir que les
    # règles n'étaient pas les mêmes.
    if policy.allow_cpu_fallback:
        findings.append(schema.Finding(
            code="cpu_fallback_authorized",
            level="info",
            message=(
                "Le repli CPU est AUTORISÉ pour cette résolution (allow_cpu_fallback=True). "
                "Ce n'est pas le défaut : §6 exige que la dégradation GPU → CPU soit demandée, "
                "jamais subie. Si aucune variante GPU sûre n'est trouvée, une variante CPU sera "
                "retenue et le plan sortira DÉGRADÉ — vérifiez le drapeau « degraded » de cette "
                "section avant d'appliquer."
            ),
        ))

    if min_build == 0:
        findings.append(schema.Finding(
            code="min_build_not_enforced",
            level="warn",
            message=(
                "La politique de release ne fixe aucun plancher de sécurité "
                "(security_floor_build=0) : LLAMA_SERVER_MIN_BUILD sera généré à 0 et le "
                "garde-fou supply-chain restera inerte (cf. GHSA-8947-pfff-2f3c). Renseignez "
                "le premier build patché connu."
            ),
        ))

    # Origine de la matrice employée (AUT-018). Un lecteur du plan ne doit jamais
    # confondre la matrice LIVRÉE avec EVARuntime — qui ne porte aucune empreinte —
    # et une matrice FOURNIE par l'opérateur. Le constat n'est émis que si des
    # variantes opérateur sont présentes : sans lui, tous les plans par défaut
    # gagneraient un constat qui ne leur apprend rien.
    operator_variants = tuple(v for v in policy.variants if v.evidence == EVIDENCE_OPERATOR)
    if operator_variants:
        findings.append(schema.Finding(
            code="runtime_variants_operator_supplied",
            level="info",
            message=(
                f"Matrice d'artefacts fournie par l'opérateur : {len(operator_variants)} variante(s) "
                f"sur {len(policy.variants)} portent un constat opérateur et non la matrice livrée "
                f"avec EVARuntime ({', '.join(v.label() for v in operator_variants)}). Leur empreinte "
                "sera recontrôlée à l'installation ; en revanche, rien ici ne prouve que l'archive "
                "désignée contient un llama-server compilé pour le backend annoncé — `--version` ne "
                "le dit pas."
            ),
        ))

    # ── Étape 0 : que vaut le binaire déjà en place ? ─────────────────────────
    observed_build: int | None = None
    reuse_existing = False

    if existing_binary is not None:
        probe_fn = probe or probe_llama_version
        version = await probe_fn(existing_binary)
        observed_build = version.build
        # L'empreinte n'est calculée que s'il y a un manifeste à recouper : sans
        # attestation à confronter, lire des centaines de Mo ne prouverait rien.
        observed_sha256: str | None = None
        if existing_manifest is not None:
            observed_sha256 = await (digest or sha256_binary)(existing_binary)
        verdict = _judge_existing_binary(
            binary=existing_binary,
            version=version,
            min_build=min_build,
            existing_manifest=existing_manifest,
            profile=profile,
            attested_sha256=existing_binary_sha256,
            observed_sha256=observed_sha256,
        )
        findings.extend(verdict.findings)
        if verdict.fatal:
            return RuntimeResolution(
                profile=profile,
                min_build=min_build,
                resolved=False,
                reuse_existing=False,
                degraded=False,
                targeted_backend=profile.targeted_backend,
                variant=None,
                manifest=None,
                observed_build=observed_build,
                summary=verdict.summary,
                findings=schema.merge_findings(findings),
                rejected=tuple(rejected),
                **politique,
            )
        reuse_existing = verdict.reuse

    if reuse_existing:
        attested = (
            f"provenance attestée ({existing_manifest.source} · {existing_manifest.backend})"
            if existing_manifest is not None
            else "provenance inconnue, faute de manifeste"
        )
        return RuntimeResolution(
            profile=profile,
            min_build=min_build,
            resolved=True,
            reuse_existing=True,
            degraded=False,
            targeted_backend=profile.targeted_backend,
            variant=None,
            manifest=existing_manifest,
            observed_build=observed_build,
            summary=(
                f"llama-server déjà en place (build {observed_build}) conservé : plancher "
                f"{min_build} satisfait, {attested}."
            ),
            findings=schema.merge_findings(findings),
            rejected=tuple(rejected),
            **politique,
        )

    # ── Étapes 1 à 5 : sélection d'une variante ───────────────────────────────
    selection = _select_variant(profile, policy, rejected)
    findings.extend(selection.findings)

    if selection.variant is None:
        return RuntimeResolution(
            profile=profile,
            min_build=min_build,
            resolved=False,
            reuse_existing=False,
            degraded=False,
            targeted_backend=profile.targeted_backend,
            variant=None,
            manifest=None,
            observed_build=observed_build,
            summary=selection.summary,
            findings=schema.merge_findings(findings),
            rejected=tuple(rejected),
            **politique,
        )

    variant = selection.variant
    manifest = ProvenanceManifest(
        version=policy.release.pinned_version,
        commit=policy.release.pinned_commit,
        source=variant.source,
        backend=variant.backend,
        platform=variant.platform,
        artifact_sha256=variant.artifact_sha256,
        container_digest=variant.container_digest,
        build_options=_build_options_for(variant),
        installed_at=installed_at,
    )

    if variant.evidence == EVIDENCE_ASSUMPTION:
        findings.append(schema.Finding(
            code="variant_evidence_assumed",
            level="warn",
            message=(
                f"La variante retenue ({variant.label()}) repose sur une hypothèse, pas sur un "
                f"constat vérifié : {variant.evidence_note} Confirmez-la contre la matrice de "
                "release amont avant d'appliquer ce plan en production."
            ),
        ))

    if variant.requires_container:
        findings.append(schema.Finding(
            code="container_backend_unsupported",
            level="warn",
            message=(
                "La variante retenue est une image conteneur, mais `server_manager` ne sait "
                "lancer que des sous-processus natifs : §6 prévoit d'« ajouter un backend "
                "conteneur au gestionnaire de serveurs », ce qui n'est pas fait. Le plan est "
                "descriptible, son application ne l'est pas encore."
            ),
        ))

    if not manifest.is_verifiable:
        findings.append(schema.Finding(
            code="local_build_manifest_pending",
            level="warn",
            message=(
                f"Build local depuis {policy.release.pinned_version} ({policy.release.pinned_commit}) : "
                "l'empreinte de l'artefact n'existera qu'après la construction. Le manifeste devra "
                "être complété par `artifact_sha256` à l'installation, sinon la provenance du binaire "
                "posé restera invérifiable."
            ),
        ))

    if observed_build is not None and min_build > 0 and observed_build < min_build:
        findings.append(schema.Finding(
            code="llama_server_replaced",
            level="warn",
            message=(
                f"Le binaire en place (build {observed_build}) est sous le plancher {min_build} : "
                f"il sera remplacé par {policy.release.pinned_version}. Les modèles chargés devront "
                "être rechargés."
            ),
        ))

    degraded = selection.degraded
    summary = (
        f"{variant.source} · {variant.backend} · {variant.platform} · "
        f"{policy.release.pinned_version}"
    )
    if degraded:
        summary += " — DÉGRADÉ : CPU retenu alors qu'un GPU était visé"

    return RuntimeResolution(
        profile=profile,
        min_build=min_build,
        resolved=True,
        reuse_existing=False,
        degraded=degraded,
        targeted_backend=profile.targeted_backend,
        variant=variant,
        manifest=manifest,
        observed_build=observed_build,
        summary=summary,
        findings=schema.merge_findings(findings),
        rejected=tuple(rejected),
        **politique,
    )


@dataclass(frozen=True)
class _BinaryVerdict:
    """Ce que vaut le binaire déjà installé : réutilisable, à remplacer, ou bloquant."""
    reuse: bool
    fatal: bool
    summary: str
    findings: tuple[schema.Finding, ...]


def _commits_agree(declared: str, observed: str) -> bool:
    """
    Le commit du manifeste et celui rendu par `--version` désignent-ils la même révision ?

    `--version` rend un SHA court (7 caractères), le manifeste un SHA de 7 à 40 :
    la comparaison se fait par préfixe, dans les deux sens, sans distinction de casse.
    """
    a, b = declared.lower(), observed.lower()
    return a.startswith(b) or b.startswith(a)


def _judge_existing_binary(
    *,
    binary: Path,
    version: LlamaVersion,
    min_build: int,
    existing_manifest: ProvenanceManifest | None,
    profile: HardwareProfile,
    attested_sha256: str | None = None,
    observed_sha256: str | None = None,
) -> _BinaryVerdict:
    """
    Applique la politique `LLAMA_SERVER_MIN_BUILD` au binaire présent.

    Fail-closed (§6) : si un plancher est exigé et que la version est illisible, la
    résolution échoue. Elle ne « suppose pas que ça ira » — on ne peut pas prouver
    que le binaire est patché, donc on refuse.

    Cas particulier du clone superficiel (§0.10) : un `git clone --depth 1` de
    llama.cpp produit un binaire qui se déclare `version: 1`. Le message doit
    désigner cette cause, sans quoi l'opérateur passe des heures à mettre à jour un
    dépôt déjà à jour.

    SEC-009 (volet b) : quand un manifeste accompagne le binaire, il est **recoupé
    contre le binaire** avant de valoir attestation — version, commit et empreinte.
    Le manifeste est un fichier texte posé à côté d'un exécutable : rien n'empêche
    de le recopier d'un autre hôte, ni de le laisser survivre à un remplacement
    manuel du binaire. Non recoupé, il n'est pas une attestation, seulement une
    affirmation. `runtime_installer._deja_satisfait` fait ce recoupement depuis
    AUT-016 ; le résolveur ne le faisait pas et accordait `reuse_existing` sur
    parole.
    """
    if version.build is None:
        if min_build > 0:
            return _BinaryVerdict(
                reuse=False,
                fatal=True,
                summary=(
                    f"Version de {binary} illisible alors qu'un build minimal {min_build} est exigé "
                    "— politique fail-closed."
                ),
                findings=(schema.Finding(
                    code="llama_server_version_unreadable",
                    level="fail",
                    message=(
                        f"LLAMA_SERVER_MIN_BUILD={min_build} est exigé mais la version de {binary} est "
                        f"illisible ({version.raw[:120]}). Impossible de prouver que ce binaire est patché "
                        "(cf. GHSA-8947-pfff-2f3c) : la résolution refuse plutôt que de supposer. "
                        "Réinstallez un llama-server dont `--version` répond, ou retirez le plancher en "
                        "connaissance de cause."
                    ),
                ),),
            )
        return _BinaryVerdict(
            reuse=False,
            fatal=False,
            summary="",
            findings=(schema.Finding(
                code="llama_server_version_unreadable",
                level="warn",
                message=(
                    f"Version de {binary} illisible ({version.raw[:120]}) et aucun plancher exigé : le "
                    "binaire en place ne peut pas être attesté, il sera remplacé."
                ),
            ),),
        )

    if version.build <= SHALLOW_CLONE_MAX_BUILD:
        shallow = (
            f"{binary} se déclare « version: {version.build} ». Ce n'est pas un build ancien : "
            "c'est la signature d'un `git clone --depth 1` de llama.cpp, qui prive la "
            "construction du compte de révisions (§0.10). Reconstruisez depuis un clone complet "
            "(`git clone` sans `--depth`, ou `git fetch --unshallow`) — mettre à jour le dépôt ne "
            "changera rien."
        )
        if min_build > SHALLOW_CLONE_MAX_BUILD:
            return _BinaryVerdict(
                reuse=False,
                fatal=True,
                summary=(
                    f"{binary} se déclare « version: {version.build} » : signature d'un clone "
                    "superficiel de llama.cpp, pas d'un binaire ancien."
                ),
                findings=(schema.Finding(
                    code="llama_server_shallow_clone",
                    level="fail",
                    message=(
                        f"{shallow} Aucun LLAMA_SERVER_MIN_BUILD > {SHALLOW_CLONE_MAX_BUILD} "
                        f"(ici {min_build}) ne pourra jamais être satisfait par ce binaire."
                    ),
                ),),
            )
        return _BinaryVerdict(
            reuse=False,
            fatal=False,
            summary="",
            findings=(schema.Finding(
                code="llama_server_shallow_clone",
                level="warn",
                message=(
                    f"{shallow} Aucun plancher n'est exigé pour l'instant, mais tout "
                    "LLAMA_SERVER_MIN_BUILD futur deviendrait insatisfiable : le binaire sera remplacé."
                ),
            ),),
        )

    if min_build > 0 and version.build < min_build:
        return _BinaryVerdict(
            reuse=False,
            fatal=False,
            summary="",
            findings=(schema.Finding(
                code="llama_server_build_too_old",
                level="warn",
                message=(
                    f"llama-server build {version.build} < plancher {min_build} : binaire "
                    "potentiellement vulnérable (GHSA-8947-pfff-2f3c). Il sera remplacé par la "
                    "version épinglée."
                ),
            ),),
        )

    # Le binaire satisfait le plancher. Reste la question du backend : `--version`
    # ne dit pas avec quel backend il a été compilé. Sans manifeste, un binaire CPU
    # sur un hôte GPU serait conservé en silence — précisément le repli interdit.
    if existing_manifest is None:
        if profile.targeted_backend is not None:
            return _BinaryVerdict(
                reuse=False,
                fatal=False,
                summary="",
                findings=(schema.Finding(
                    code="runtime_provenance_unknown",
                    level="warn",
                    message=(
                        f"{binary} satisfait le plancher (build {version.build}) mais n'a pas de manifeste "
                        f"de provenance : rien ne prouve qu'il a été compilé pour « "
                        f"{profile.targeted_backend} ». Le conserver reviendrait à accepter un éventuel "
                        "binaire CPU sur un hôte GPU sans le savoir ; il sera donc remplacé. Fournissez "
                        "son manifeste (§6) pour éviter cette réinstallation."
                    ),
                ),),
            )
        return _BinaryVerdict(
            reuse=True,
            fatal=False,
            summary="",
            findings=(schema.Finding(
                code="runtime_provenance_unknown",
                level="warn",
                message=(
                    f"{binary} satisfait le plancher (build {version.build}) et l'hôte n'a pas de GPU : "
                    "il est conservé. Sa provenance reste toutefois inconnue — aucun manifeste §6 ne "
                    "l'accompagne."
                ),
            ),),
        )

    # ── Recoupement manifeste ↔ binaire (SEC-009) ─────────────────────────────
    #
    # Trois confrontations, dans l'ordre du moins cher au plus cher, et toutes
    # nécessaires : le manifeste doit décrire CE binaire-ci, pas un binaire.

    if existing_manifest.build_number != version.build:
        return _BinaryVerdict(
            reuse=False,
            fatal=False,
            summary="",
            findings=(schema.Finding(
                code="runtime_manifest_build_mismatch",
                level="warn",
                message=(
                    f"Le manifeste de {binary} déclare {existing_manifest.version} "
                    f"(build {existing_manifest.build_number}) alors que le binaire rend le build "
                    f"{version.build}. Le manifeste ne décrit pas ce binaire : il a été recopié "
                    "d'un autre hôte, ou il a survécu à un remplacement du binaire. Il ne vaut "
                    "donc pas attestation de provenance et le binaire sera remplacé."
                ),
            ),),
        )

    if version.commit is not None and not _commits_agree(existing_manifest.commit, version.commit):
        return _BinaryVerdict(
            reuse=False,
            fatal=False,
            summary="",
            findings=(schema.Finding(
                code="runtime_manifest_commit_mismatch",
                level="warn",
                message=(
                    f"Le manifeste de {binary} déclare le commit {existing_manifest.commit} alors "
                    f"que le binaire rend {version.commit}, pour un même numéro de build "
                    f"{version.build}. Deux révisions différentes derrière une même étiquette : la "
                    "provenance annoncée est fausse, le binaire sera remplacé."
                ),
            ),),
        )

    if existing_manifest.backend not in profile.known_backends:
        return _BinaryVerdict(
            reuse=False,
            fatal=False,
            summary="",
            findings=(schema.Finding(
                code="runtime_backend_mismatch",
                level="warn",
                message=(
                    f"Le manifeste du binaire en place annonce le backend « {existing_manifest.backend} », "
                    f"absent des candidats de cet hôte ({', '.join(profile.known_backends) or 'aucun'}). "
                    "Le conserver produirait un TTFT dégradé sans que rien ne le signale : il sera remplacé."
                ),
            ),),
        )

    if attested_sha256 is None:
        return _BinaryVerdict(
            reuse=False,
            fatal=False,
            summary="",
            findings=(schema.Finding(
                code="runtime_binary_unattested",
                level="warn",
                message=(
                    f"Le manifeste de {binary} ne consigne aucune empreinte du binaire posé "
                    "(bloc « install.binary_sha256 »). Version et commit concordent, mais rien ne "
                    "distingue ce binaire d'un autre build de la même révision : la provenance "
                    "n'est pas vérifiable et le binaire sera réinstallé. Réinstallez-le par "
                    "`runtime_installer`, qui consigne cette empreinte à chaque passe."
                ),
            ),),
        )

    if observed_sha256 is None:
        return _BinaryVerdict(
            reuse=False,
            fatal=False,
            summary="",
            findings=(schema.Finding(
                code="runtime_binary_unreadable",
                level="warn",
                message=(
                    f"L'empreinte de {binary} n'a pas pu être calculée (fichier illisible) : le "
                    "manifeste ne peut pas être recoupé contre le binaire. Sans ce recoupement, "
                    "la provenance n'est pas attestée et le binaire sera remplacé."
                ),
            ),),
        )

    if observed_sha256 != attested_sha256:
        return _BinaryVerdict(
            reuse=False,
            fatal=False,
            summary="",
            findings=(schema.Finding(
                code="runtime_binary_tampered",
                level="warn",
                message=(
                    f"L'empreinte de {binary} ({observed_sha256[:16]}…) ne correspond pas à celle "
                    f"consignée à l'installation ({attested_sha256[:16]}…), alors que version et "
                    "commit concordent. Le binaire a été remplacé ou altéré sous une attestation "
                    "restée en place : anomalie de chaîne d'approvisionnement, il sera réinstallé "
                    "depuis la variante épinglée."
                ),
            ),),
        )

    return _BinaryVerdict(reuse=True, fatal=False, summary="", findings=())


@dataclass(frozen=True)
class _Selection:
    """Issue de la traversée de l'ordre de §6."""
    variant: ArtifactVariant | None
    degraded: bool
    summary: str
    findings: tuple[schema.Finding, ...]


def _select_variant(
    profile: HardwareProfile,
    policy: ResolverPolicy,
    rejected: list[str],
) -> _Selection:
    """
    Traverse l'ordre de §6 : backends GPU visés d'abord, CPU seulement ensuite.

    `rejected` est enrichi au passage — un refus n'a de valeur que s'il dit ce qui
    a été écarté et pourquoi.
    """
    candidates = profile.known_backends
    unknown = [b for b in profile.backend_candidates if b not in BACKENDS]
    for backend in unknown:
        rejected.append(f"backend « {backend} » : inconnu du résolveur")

    if not candidates:
        return _Selection(
            variant=None,
            degraded=False,
            summary=(
                f"Aucun backend exploitable pour {profile.platform} : l'inventaire n'a proposé "
                "aucun candidat reconnu."
            ),
            findings=(schema.Finding(
                code="no_backend_candidate",
                level="fail",
                message=(
                    f"L'inventaire ne propose aucun backend reconnu pour {profile.platform} "
                    f"(reçu : {', '.join(profile.backend_candidates) or 'liste vide'}). Sans backend "
                    "visé, aucune variante de llama-server ne peut être choisie."
                ),
            ),),
        )

    targeted = profile.targeted_backend
    gpu_targets = tuple(b for b in candidates if b in GPU_BACKENDS) if targeted else ()

    if gpu_targets:
        found = _first_match(gpu_targets, profile.platform, policy, rejected)
        if found is not None:
            return _Selection(variant=found, degraded=False, summary="", findings=())

    if BACKEND_CPU in candidates:
        cpu_variant = _first_match((BACKEND_CPU,), profile.platform, policy, rejected)
    else:
        cpu_variant = None

    # Hôte sans GPU visé : le CPU est le choix nominal, pas un repli.
    if not gpu_targets:
        if cpu_variant is not None:
            return _Selection(variant=cpu_variant, degraded=False, summary="", findings=())
        return _Selection(
            variant=None,
            degraded=False,
            summary=f"Aucune variante sûre de llama-server pour {profile.platform}.",
            findings=(_no_variant_finding(profile, candidates, rejected),),
        )

    # À partir d'ici, un backend GPU était visé et aucune variante GPU sûre n'existe.
    lost = (
        f"Un backend GPU était visé ({', '.join(gpu_targets)}) sur "
        f"{profile.gpu_vendor or 'GPU'} / driver {profile.driver_version or 'inconnu'}."
    )

    if cpu_variant is None:
        return _Selection(
            variant=None,
            degraded=False,
            summary=f"Aucune variante sûre de llama-server pour {profile.platform}.",
            findings=(_no_variant_finding(profile, candidates, rejected),),
        )

    if not policy.allow_cpu_fallback:
        rejected.append(
            f"{cpu_variant.label()} : repli CPU refusé (allow_cpu_fallback=False)"
        )
        return _Selection(
            variant=None,
            degraded=False,
            summary=f"Aucune variante GPU sûre pour {profile.platform} ; repli CPU refusé.",
            findings=(schema.Finding(
                code="cpu_fallback_refused",
                level="fail",
                message=(
                    f"{lost} Aucune variante GPU sûre ne correspond, et seule une variante CPU "
                    f"({cpu_variant.label()}) reste disponible. Basculer sur elle donnerait une "
                    "installation qui démarre, répond et passe le smoke test, avec un TTFT "
                    "inacceptable en production — §6 interdit ce repli silencieux. La résolution "
                    "refuse. Épinglez une variante GPU (digest ou sha256), autorisez le conteneur, "
                    "ou assumez le CPU explicitement avec allow_cpu_fallback=True."
                ),
            ),),
        )

    return _Selection(
        variant=cpu_variant,
        degraded=True,
        summary="",
        findings=(schema.Finding(
            code="cpu_fallback_degraded",
            level="warn",
            message=(
                f"{lost} Aucune variante GPU sûre n'a été trouvée : la variante CPU "
                f"{cpu_variant.label()} est retenue sur décision explicite (allow_cpu_fallback=True). "
                "Ce que l'opérateur perd : le GPU ne sera pas utilisé, le TTFT et le débit seront "
                "d'un ordre de grandeur inférieurs, la VRAM déclarée dans models.yaml n'aura plus de "
                "sens et les modèles dimensionnés pour le GPU pourront saturer la RAM hôte. "
                "L'installation paraîtra pourtant saine."
            ),
        ),),
    )


def _first_match(
    backends: tuple[str, ...],
    platform: str,
    policy: ResolverPolicy,
    rejected: list[str],
) -> ArtifactVariant | None:
    """
    Premier couple (source, backend) éligible, sources dans l'ordre de §6.

    La source est la boucle externe : c'est elle que §6 ordonne. Le backend est la
    boucle interne, dans l'ordre de préférence de l'inventaire.
    """
    for source in SOURCE_ORDER:
        for backend in backends:
            for variant in policy.variants:
                if (variant.source, variant.backend, variant.platform) != (source, backend, platform):
                    continue
                reason = _ineligible_reason(variant, policy)
                if reason is None:
                    return variant
                rejected.append(f"{variant.label()} : {reason}")
    return None


def _ineligible_reason(variant: ArtifactVariant, policy: ResolverPolicy) -> str | None:
    """Pourquoi cette variante ne peut pas être retenue, ou `None` si elle le peut."""
    if variant.requires_container and not policy.allow_container:
        return "mode conteneur non accepté par la politique (allow_container=False)"
    if variant.source == SOURCE_LOCAL_BUILD and not policy.allow_local_build:
        return "build local non accepté par la politique (allow_local_build=False)"
    if not variant.is_pinned:
        if variant.requires_container:
            return "image non épinglée par digest — un tag peut être réécrit à tout moment (§6)"
        return "artefact non épinglé : aucun sha256 connu, l'intégrité serait invérifiable (§6)"
    return None


def _no_variant_finding(
    profile: HardwareProfile,
    candidates: tuple[str, ...],
    rejected: list[str],
) -> schema.Finding:
    detail = " ; ".join(rejected) if rejected else "aucune entrée de matrice pour cette plateforme"
    return schema.Finding(
        code="runtime_unresolved",
        level="fail",
        message=(
            f"Aucune variante sûre de llama-server ne correspond à {profile.platform} pour les "
            f"backends {', '.join(candidates)}. Étape 5 de §6 : refus explicite plutôt "
            f"qu'installation approximative. Écarté : {detail}."
        ),
    )


def _build_options_for(variant: ArtifactVariant) -> dict[str, bool | int | str]:
    """
    Options de build inscrites au manifeste, pour les sources qu'on construit.

    Vide pour un artefact ou une image officiels : nous ne les avons pas compilés,
    et prétendre connaître leurs options serait une invention.

    `GGML_NATIVE: False` n'est pas cosmétique : `-march=native` rend le binaire
    non reproductible et non redistribuable entre machines du même parc.
    """
    if variant.source not in BUILD_SOURCES:
        return {}
    options: dict[str, bool | int | str] = {
        "LLAMA_BUILD_SERVER": True,
        "GGML_NATIVE": False,
    }
    flag = _BACKEND_BUILD_FLAG.get(variant.backend)
    if flag:
        options[flag] = True
    return options


# ── Projections vers le contrat du plan ───────────────────────────────────────

def to_plan_section(resolution: RuntimeResolution) -> schema.PlanSection:
    """Projette la résolution vers la section « runtime » du plan."""
    return schema.PlanSection(
        name=SECTION_NAME,
        version=SECTION_VERSION,
        status=resolution.status,
        summary=resolution.summary or "Résolution du runtime llama-server.",
        data=resolution.to_data(),
        findings=resolution.findings,
    )


def to_decision(resolution: RuntimeResolution) -> schema.Decision:
    """
    Projette la résolution vers la décision motivée exigée par la sortie de M1.

    Un refus produit une décision, lui aussi : « aucun runtime sûr » est un choix
    argumenté, et l'opérateur doit lire pourquoi.
    """
    if not resolution.resolved:
        return schema.Decision(
            topic="runtime llama-server",
            choice="aucun — refus explicite",
            rationale=resolution.summary or "aucune variante sûre ne correspond à cet hôte",
            rejected=resolution.rejected,
        )

    if resolution.reuse_existing:
        return schema.Decision(
            topic="runtime llama-server",
            choice=f"conserver le binaire en place (build {resolution.observed_build})",
            rationale=(
                f"il satisfait le plancher {resolution.min_build} et aucune raison de le remplacer "
                "n'a été trouvée"
            ),
            rejected=resolution.rejected,
        )

    variant = resolution.variant
    if variant is None:  # garanti par resolved and not reuse_existing — sauf sous `python -O`
        raise ProvenanceError(
            "résolution incohérente : résolue et sans réutilisation, mais sans variante "
            "retenue — aucune décision de runtime ne peut être motivée"
        )
    rationale = (
        f"première variante sûre dans l'ordre de §6 pour {resolution.profile.platform} "
        f"({variant.evidence} : {variant.evidence_note})"
    )
    if resolution.degraded:
        rationale = (
            f"aucune variante GPU sûre pour {resolution.profile.platform} ; repli CPU assumé "
            f"explicitement par la politique — dégradation de performance acceptée ({variant.evidence})"
        )
    return schema.Decision(
        topic="runtime llama-server",
        choice=variant.label(),
        rationale=rationale,
        rejected=resolution.rejected,
    )


def to_plan_steps(
    resolution: RuntimeResolution,
    *,
    start_order: int = 1,
    include_verify: bool = False,
) -> tuple[schema.PlanStep, ...]:
    """
    Étapes que l'application du plan exécuterait pour poser ce runtime.

    Ne construit **pas** de plan : l'assemblage et la renumérotation globale
    appartiennent au planificateur. `start_order` permet de découper la
    numérotation continue exigée par `schema._validate_steps`.

    La vérification précède l'installation, jamais l'inverse : contrôler
    l'empreinte après avoir posé le binaire ne protège de rien.

    COR-030 — pourquoi `verify_artifact` n'est plus émise par défaut
    ---------------------------------------------------------------
    Ce module émettait, avant `install_runtime`, une étape `verify_artifact`
    décrivant le contrôle d'empreinte de l'archive. Elle ne pouvait rien
    vérifier : **à ce numéro d'étape l'archive n'est pas encore téléchargée**.
    C'est `install_runtime` qui la récupère ET confronte son empreinte avant
    d'extraire quoi que ce soit (AUT-016). `applier._verify_dispatcher` le
    constatait déjà et rendait `STEP_SKIPPED` avec cette raison exacte — refuser
    d'affirmer un contrôle qui n'a pas eu lieu était le bon geste.

    Le coût de cette étape vide n'était pas cosmétique. `install_report`
    considère — à juste titre — qu'une étape sautée ne prouve rien, donc la
    **condition n°1 du jalon M2** (« runtime installé et vérifié ») ressortait
    `unsatisfied` alors que l'installation avait réellement réussi : archive
    récupérée, empreinte confrontée, build relu sur le binaire posé, manifeste §6
    écrit. Le jalon ne pouvait pas être prononcé, et — conséquence plus large que
    le seul rapport — `bootstrap-apply` sortait en `partial`, **code 3**, sur
    chaque succès. Tout script d'exploitation testant le code 0 lisait un
    demi-échec.

    Deux voies existaient. Requalifier le résultat en `already_satisfied` a été
    écarté : `execution` définit `STEP_SKIPPED` comme « décision délibérée de ne
    pas exécuter », ce qui est exactement le cas, et l'autre domaine de la même
    action — `downloader._execute_verify_artifact` — ne rend
    `already_satisfied` qu'**après avoir relu les octets**. Verdir le statut
    aurait déplacé la complaisance au lieu de supprimer le défaut. C'est donc
    l'émetteur qui est corrigé : une étape qui ne peut rien vérifier n'est pas
    émise.

    Rien n'est perdu côté lisibilité : l'empreinte ou le digest attendus sont
    désormais inscrits dans le détail de l'étape `install_runtime`, qui est celle
    qui les confronte réellement.

    `include_verify` reste et devient **opt-in**. Il n'est correct que pour un
    appelant qui poserait l'archive AVANT ce numéro d'étape — ce qu'aucun ne fait
    aujourd'hui. Le passer à vrai réintroduit le défaut.
    """
    if not resolution.resolved or resolution.reuse_existing or resolution.variant is None:
        return ()

    variant = resolution.variant
    manifest = resolution.manifest
    steps: list[schema.PlanStep] = []
    order = start_order

    if include_verify and manifest is not None and manifest.is_verifiable:
        if manifest.container_digest:
            detail = (
                f"Résoudre {variant.reference or variant.label()} par digest "
                f"{manifest.container_digest} et refuser tout autre contenu."
            )
        else:
            detail = (
                f"Contrôler l'archive {variant.reference or variant.label()} contre "
                f"sha256:{manifest.artifact_sha256} avant toute extraction."
            )
        steps.append(schema.PlanStep(
            order=order,
            action=schema.ACTION_VERIFY_ARTIFACT,
            target=f"llama-server {manifest.version} ({variant.backend})",
            detail=detail,
            requires_root=False,
            reversible=True,
            estimated_bytes=variant.approx_bytes,
        ))
        order += 1

    if variant.source == SOURCE_LOCAL_BUILD:
        detail = (
            f"Construire llama-server depuis {PROJECT} au tag "
            f"{manifest.version if manifest else '?'} "
            f"(commit {manifest.commit if manifest else '?'}), options "
            f"{_format_options(manifest.build_options if manifest else {})}. "
            "Cloner en profondeur complète : un clone superficiel produit un binaire « version: 1 » "
            "et rend LLAMA_SERVER_MIN_BUILD insatisfiable (§0.10). Écrire le manifeste de provenance "
            "avec l'empreinte calculée."
        )
    elif variant.requires_container:
        detail = (
            f"Déployer l'image {variant.reference or variant.label()} épinglée par digest "
            f"{manifest.container_digest if manifest else '?'} et refuser tout autre contenu, "
            "puis écrire le manifeste de provenance. Nécessite un backend conteneur côté "
            "server_manager."
        )
    else:
        # COR-030 : l'empreinte attendue est inscrite ICI, sur l'étape qui la
        # confronte réellement, et non sur une `verify_artifact` qui la précédait
        # sans pouvoir rien lire.
        detail = (
            f"Récupérer l'artefact {variant.reference or variant.label()}, contrôler son "
            f"empreinte contre sha256:{manifest.artifact_sha256 if manifest else '?'} AVANT "
            f"toute extraction, installer le binaire {variant.backend} et écrire le manifeste "
            "de provenance à côté de lui. L'installation est annulée si l'empreinte diverge."
        )
    if resolution.degraded:
        detail += " ATTENTION : runtime CPU sur un hôte GPU — dégradation assumée."

    steps.append(schema.PlanStep(
        order=order,
        action=schema.ACTION_INSTALL_RUNTIME,
        target=f"llama-server {manifest.version if manifest else '?'} ({variant.backend})",
        detail=detail,
        requires_root=True,
        reversible=True,
        estimated_bytes=variant.approx_bytes,
    ))
    return tuple(steps)


def _format_options(options: Mapping[str, Any]) -> str:
    return ", ".join(f"{key}={str(value).upper()}" for key, value in options.items()) or "aucune"


# ── Garde-fou d'isolation ─────────────────────────────────────────────────────

def module_toplevel_imports(source_path: Path | None = None) -> frozenset[str]:
    """
    Modules qu'un fichier peut tirer, par analyse statique de ses imports.

    Sert au test qui prouve que le résolveur ne peut ni parler au réseau ni lancer
    un build : il n'importe aucun de `FORBIDDEN_IMPORTS`, ni aucun module frère de
    `FORBIDDEN_SIBLING_IMPORTS`. Une assertion d'absence doit pouvoir échouer —
    d'où cette fonction, qui rend l'ensemble observable au lieu de laisser le test
    croire sur parole.

    TST-007 — ce que cette fonction ne voyait pas
    ---------------------------------------------
    Elle filtrait sur `node.level == 0`, donc `from . import public_https` lui
    était **invisible**. Le garde-fou ne pouvait pas attraper l'ajout qu'il existe
    précisément pour empêcher : importer un frère qui, lui, parle au réseau.
    `from bootstrap import public_https` — la forme employée par `planner` et
    `applier` — passait tout aussi bien, puisqu'elle ne rendait que « bootstrap ».

    Les trois écritures sont désormais ramenées à une forme canonique unique,
    `bootstrap.<frère>`, quelle que soit la syntaxe employée :

    - `from . import public_https`        → `bootstrap.public_https`
    - `from .public_https import fetch`   → `bootstrap.public_https`
    - `from bootstrap import public_https`→ `bootstrap.public_https`
    - `import bootstrap.public_https`     → `bootstrap.public_https`

    Un import relatif remontant au-delà du paquet (`from .. import x`) est rendu
    verbatim, avec ses points : il sort du périmètre canonique, et le laisser
    visible sous une forme qui ne ressemble à aucun nom autorisé vaut mieux que le
    normaliser à tort.

    Le parcours est un `ast.walk` : un import différé dans le corps d'une fonction
    est vu lui aussi. Le garde-fou perdrait tout son sens si déplacer `import
    socket` de trois lignes suffisait à le contourner.
    """
    path = source_path or Path(__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                names.add(parts[0])
                if parts[0] == PACKAGE and len(parts) > 1:
                    names.add(f"{PACKAGE}.{parts[1]}")
        elif isinstance(node, ast.ImportFrom):
            if node.level == 1:
                # `from .x import y` désigne le frère x ; `from . import x` aussi,
                # mais c'est alors l'alias qui porte son nom.
                if node.module:
                    names.add(f"{PACKAGE}.{node.module.split('.')[0]}")
                else:
                    names.update(f"{PACKAGE}.{alias.name}" for alias in node.names)
            elif node.level > 1:
                prefix = "." * node.level
                if node.module:
                    names.add(f"{prefix}{node.module.split('.')[0]}")
                else:
                    names.update(f"{prefix}{alias.name}" for alias in node.names)
            elif node.module:
                base = node.module.split(".")[0]
                names.add(base)
                # `from bootstrap import public_https` nomme le frère dans l'alias,
                # pas dans le module : sans cette branche, il ne resterait que
                # « bootstrap », qui n'est interdit nulle part.
                if base == PACKAGE:
                    names.update(f"{PACKAGE}.{alias.name}" for alias in node.names)
    return frozenset(names)
