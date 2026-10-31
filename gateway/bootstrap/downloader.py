"""
AUT-006 — téléchargement sûr des artefacts du catalogue (jalon M2).

Ce que ce module est
--------------------
L'exécuteur de trois actions du contrat de `schema` : `download_model`,
`verify_artifact` et `accept_license`. Il applique littéralement la politique de
téléchargement :

    révision figée · liste exacte de fichiers · reprise · prévision de l'espace
    disque · jeton hors argv · fichier temporaire · vérification SHA-256 ·
    renommage atomique · manifeste de provenance.

L'invariant central est le suivant, et tout le reste en découle :

    **À aucun moment un fichier portant son nom définitif n'existe sans avoir
    été vérifié contre l'empreinte du catalogue.**

C'est le défaut que ce module existe pour empêcher : un `llama-server` qui
charge un GGUF à moitié téléchargé. Le téléchargement écrit donc dans
`<nom>.part`, calcule le SHA-256 en flux, et n'appelle `os.replace()` — atomique
sur un même système de fichiers — qu'après confrontation réussie. Un écart
d'empreinte détruit le `.part` : il n'est jamais promu, jamais « réparé ».

Ce que ce module n'est pas
--------------------------
Il ne planifie rien, ne choisit aucun modèle, ne décide d'aucune licence et
n'écrit aucun registre. Il n'importe que la bibliothèque standard, les contrats
`schema`/`execution`, `catalog` — parce que c'est *son* contenu qu'on télécharge
— et la frontière réseau partagée `public_https`. `CatalogEntry.download_fileset()`
reste le seul point d'entrée qui rende l'ensemble split GGUF + `mmproj`
indivisible par construction. Aucun autre exécuteur M2 n'est importé.

Arbitrage : pourquoi pas `huggingface_hub`
------------------------------------------
§8 recommande le client officiel `huggingface_hub`/`hf`. Évaluation faite,
conclusion : **non retenu**, et `catalog.yaml` devra être corrigé en
conséquence (il annonce encore `huggingface_hub` comme téléchargeur — cf. le
constat `provenance_telechargeur_divergent` émis plus bas, qui rend l'écart
visible au lieu de le taire).

Quatre raisons, dans l'ordre de leur poids opérationnel.

1. **Ordre d'amorçage.** `huggingface_hub` tire `requests`, `filelock`,
   `fsspec`, `tqdm`, `packaging` et `typing-extensions` dans le chemin d'un
   outil qui doit tourner sur une machine vierge, avant que le venv de la
   gateway n'existe. C'est exactement l'argument qui a fait écarter le paquet
   `gguf` officiel en vague 5 (cf. `gguf_meta.py`) : le coût d'une dépendance
   ici n'est pas son téléchargement, c'est le fait qu'il faille déjà savoir
   installer des paquets pour pouvoir commencer à installer.

2. **L'invariant central deviendrait celui d'un tiers.** `hf_hub_download()`
   gère lui-même le fichier temporaire, le renommage et le cache
   (`blobs/` + `snapshots/` + liens symboliques), et vérifie l'`ETag` renvoyé
   par le serveur — c'est-à-dire ce que la source affirme, pas ce que *notre*
   catalogue a épinglé après revue. Nous voulons l'inverse : le SHA-256 du
   catalogue fait foi, il est calculé en flux par nous, et rien n'est promu
   sans lui. Déléguer le renommage à une bibliothèque rendrait cet invariant
   invérifiable depuis nos tests.

3. **La disposition sur disque n'est pas la nôtre.** Le planificateur attend
   les fichiers à plat dans `models_dir` (`planner._model_steps`,
   `inventory`), pas dans un cache à liens symboliques dont le format a changé
   plusieurs fois. `local_dir=` existe mais sa sémantique a bougé d'une version
   à l'autre : notre épinglage fail-closed dépendrait alors de la version
   installée du client.

4. **Testabilité sans réseau.** Le transport doit être injectable pour que la
   suite ne touche jamais le réseau. Avec `huggingface_hub` il faudrait
   détourner ses internes ; ici le transport est un `Protocol` de sept lignes.

Ce que nous perdons, et qu'il faut assumer : le cache partagé entre dépôts, la
déduplication par blob, `xet`/le transfert accéléré, la barre de progression, et
la gestion des dépôts *gated*. Les trois premiers ne nous servent pas (une
installation, un jeu de fichiers) ; le dernier n'est pas un manque, car une
entrée `gated` est déjà refusée en amont par `catalog.CatalogEntry.plannable`.

**À réévaluer** si le bootstrap doit un jour gérer les dépôts gated, le
transfert Xet, ou mutualiser un cache entre plusieurs hôtes.

Ce que le module ne fait volontairement pas
-------------------------------------------
Il ne lance **aucun sous-processus**. Ce n'est pas un détail de style : c'est la
raison pour laquelle « jeton jamais dans argv » n'est pas une consigne mais une
impossibilité structurelle. Le jeton ne circule que dans un en-tête
`Authorization`, il ne quitte jamais l'hôte d'origine (`authorization_headers()`
le retire à tout saut de redirection changeant d'hôte), et il est expurgé de
tout message par `_scrub()` avant de rejoindre un journal, une erreur ou une
preuve.
"""
from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import os
import re
import shutil
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Mapping, Protocol, Sequence

from . import catalog as catalog_mod
from . import execution, public_https, schema

# Version du manifeste de provenance. Indépendante du plan et du rapport : la
# provenance survit à l'exécution qui l'a produite et se relit des mois après.
MANIFEST_VERSION = 1

# Nom du téléchargeur réellement employé. Recopié dans chaque manifeste : c'est
# la réponse à « d'où vient ce fichier, et par quel outil ».
DOWNLOADER_NAME = "eva-bootstrap.downloader"

DEFAULT_ENDPOINT = "https://huggingface.co"

# Suffixes. `.part` vit dans le MÊME répertoire que sa cible, condition
# nécessaire pour que `os.replace()` soit atomique (même système de fichiers).
PART_SUFFIX = ".part"
RESUME_SUFFIX = ".part.json"
PROVENANCE_SUFFIX = ".provenance.json"
ACCEPTANCE_SUFFIX = ".license-accepted.json"

DEFAULT_CHUNK_BYTES = 1024 * 1024

# Marge d'espace disque. Un disque rempli à 100 % par un GGUF de 40 Gio est un
# incident d'exploitation (journaux bloqués, SQLite en échec d'écriture), pas
# une simple erreur de téléchargement : la marge est donc exigée EN PLUS du
# volume à écrire, et le refus est prononcé avant le premier octet.
DEFAULT_DISK_MARGIN_RATIO = 0.05
DEFAULT_DISK_MARGIN_MIN_BYTES = 1024 ** 3

MAX_REDIRECTS = 5

DEFAULT_TIMEOUT_SECONDS = 60.0

_GIB = float(1024 ** 3)

# Révision : contrôlée ici AUSSI, indépendamment de `catalog`. Une branche
# (« main ») n'est pas une révision, et ce module est le dernier à pouvoir le
# dire avant qu'une URL ne parte.
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_CONTENT_RANGE_RE = re.compile(r"^bytes\s+(?P<start>\d+)-(?P<end>\d+)/(?P<total>\d+)$")

# Variables d'environnement consultées, dans l'ordre. `EVA_HF_TOKEN` d'abord :
# elle est propre à cet outil et permet de ne pas exposer le jeton personnel de
# l'opérateur au reste de son shell.
TOKEN_ENV_VARS: tuple[str, ...] = ("EVA_HF_TOKEN", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN")

# Credential store : le fichier écrit par `hf auth login`. Lu seulement si
# aucune variable d'environnement ne répond.
DEFAULT_TOKEN_FILE = Path("~/.cache/huggingface/token")

_TOKEN_PLACEHOLDER = "[jeton expurgé]"


class DownloadError(execution.ExecutionError):
    """Le téléchargement ne peut pas avoir lieu, ou son résultat n'est pas prouvable."""


# ── Transport HTTP injectable ─────────────────────────────────────────────────

class HttpResponse(Protocol):
    """
    Une réponse en cours de lecture. Volontairement minimale.

    `headers` est indexé en minuscules par le transport : la casse des en-têtes
    HTTP n'est pas significative, et laisser chaque appelant s'en souvenir
    finirait par produire un `Content-Range` lu comme absent.
    """

    status: int
    headers: Mapping[str, str]

    def chunks(self) -> AsyncIterator[bytes]:
        ...

    async def close(self) -> None:
        ...


class HttpTransport(Protocol):
    """
    Le seul point du module qui touche au réseau — donc le seul à remplacer en test.

    Ne suit AUCUNE redirection : c'est le téléchargeur qui les suit, un saut à
    la fois, en re-décidant à chaque hop s'il a le droit d'envoyer le jeton
    (`authorization_headers()`). Un transport qui redirigerait tout seul
    emporterait l'en-tête `Authorization` vers le CDN sans que personne
    ne l'ait décidé.
    """

    async def open(self, url: str, headers: Mapping[str, str]) -> HttpResponse:
        ...


class _UrllibResponse:
    """Adaptateur autour d'une réponse `http.client`. Lecture par blocs, hors boucle."""

    def __init__(
        self,
        raw: Any,
        chunk_bytes: int,
        connection: public_https.HTTPSConnectionLike,
    ) -> None:
        self._raw = raw
        self._chunk_bytes = chunk_bytes
        self._connection = connection
        self._closed = False
        self.status = int(getattr(raw, "status", 0) or getattr(raw, "code", 0) or 0)
        self.headers = {str(k).lower(): str(v) for k, v in raw.headers.items()}

    async def chunks(self) -> AsyncIterator[bytes]:
        while True:
            block = await asyncio.to_thread(self._raw.read, self._chunk_bytes)
            if not block:
                return
            yield block

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await asyncio.to_thread(self._raw.close)
        finally:
            await asyncio.to_thread(self._connection.close)


class UrllibTransport:
    """
    Transport par défaut : `http.client`, zéro dépendance ajoutée.

    Le choix de la bibliothèque standard plutôt que `httpx` (pourtant déjà
    requis par la gateway) tient au même argument que l'arbitrage
    `huggingface_hub` : ce module doit pouvoir tourner avant que le venv de la
    gateway n'existe. `http.client` est là par définition.

    Chaque hôte est résolu une seule fois. Toute réponse DNS privée ou mixte est
    refusée, puis la connexion TCP emploie exactement les adresses contrôlées en
    conservant le nom original pour SNI et le certificat. Aucun proxy issu de
    l'environnement n'est consulté.

    Les appels bloquants passent par `asyncio.to_thread` : la boucle reste libre,
    conformément à la règle « async de bout en bout » du dépôt.
    """

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
        resolver: public_https.Resolver | None = None,
        connection_factory: public_https.ConnectionFactory | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout doit être > 0")
        if chunk_bytes < 1:
            raise ValueError("chunk_bytes doit être >= 1")
        self._timeout = timeout
        self._chunk_bytes = chunk_bytes
        self._resolver = resolver if resolver is not None else public_https.system_resolve
        self._connection_factory = (
            connection_factory
            if connection_factory is not None
            else public_https.pinned_connection_factory
        )

    async def open(self, url: str, headers: Mapping[str, str]) -> HttpResponse:
        return await asyncio.to_thread(self._open, url, dict(headers))

    def _open(self, url: str, headers: dict[str, str]) -> _UrllibResponse:
        try:
            validated = public_https.validate_url(url)
            parts = urllib.parse.urlsplit(validated)
            host = parts.hostname
            if host is None:  # couvert par validate_url, garde de typage
                raise public_https.PublicHttpsError("URL d'artefact sans hôte")
            port = parts.port or 443
            addresses = public_https.resolve_public_addresses(host, port, self._resolver)
            connection = self._connection_factory(host, port, addresses, self._timeout)
        except public_https.PublicHttpsError as exc:
            raise DownloadError(str(exc)) from exc

        try:
            target = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
            connection.request("GET", target, headers=headers)
            raw = connection.getresponse()
            return _UrllibResponse(raw, self._chunk_bytes, connection)
        except (OSError, http.client.HTTPException) as exc:
            connection.close()
            raise DownloadError(
                f"connexion HTTPS impossible vers la source d'artefact {host!r}"
            ) from exc


# ── Jeton : obtention, portée, expurgation ────────────────────────────────────

def default_token_provider() -> str | None:
    """
    Jeton depuis l'environnement, sinon depuis le credential store. Jamais argv.

    Aucun repli sur une saisie interactive ni sur un fichier du dépôt : un jeton
    qui n'est pas là est un jeton absent, et le téléchargement d'un dépôt public
    n'en a pas besoin.
    """
    for name in TOKEN_ENV_VARS:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    try:
        text = DEFAULT_TOKEN_FILE.expanduser().read_text(encoding="utf-8")
    except OSError:
        return None
    return text.strip() or None


def authorization_headers(url: str, *, origin_host: str, token: str | None) -> dict[str, str]:
    """
    Décide si le jeton a le droit de partir vers cette URL. Fonction pure.

    Extraite du chemin de téléchargement pour être testable seule, parce que
    c'est la règle la plus facile à casser sans que rien ne rougisse : Hugging
    Face redirige `resolve/` vers un CDN tiers, et une bibliothèque HTTP qui
    recopie les en-têtes à travers la redirection livre le jeton de
    l'organisation à ce tiers.

    Deux refus, tous deux silencieux côté jeton mais explicites côté appelant :
    hôte différent de l'origine, ou schéma autre que HTTPS.
    """
    if not token:
        return {}
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() != "https":
        return {}
    if (parts.hostname or "").lower() != origin_host.lower():
        return {}
    return {"Authorization": f"Bearer {token}"}


def _scrub(message: str, token: str | None) -> str:
    """
    Expurge un message avant qu'il ne devienne journal, erreur ou preuve.

    Deux filets, pour la même raison que `schema` en a deux : le premier
    (remplacement littéral) attrape un jeton de forme quelconque — un jeton
    d'entreprise n'a pas forcément le préfixe `hf_` ; le second
    (`execution.redact_for_log`) attrape ce que nous ne savions pas être un
    secret. Retirer l'un des deux laisse passer la moitié des cas.
    """
    if token:
        message = message.replace(token, _TOKEN_PLACEHOLDER)
    return execution.redact_for_log(message)


# ── Acceptation de licence ────────────────────────────────────────────────────

@dataclass(frozen=True)
class LicenseAcceptance:
    """
    Une acceptation FOURNIE par l'opérateur. Jamais déduite, jamais par défaut.

    §4 range l'acceptation des licences de modèles parmi « ce qui doit rester
    une décision humaine ». Ce type en est la matérialisation : `accepted` n'a
    pas de valeur par défaut, donc personne ne construit une acceptation par
    omission d'argument — même piège que `ExecutionMode`, fermé de la même
    façon. Le caractère permissif d'une licence du catalogue ne produit JAMAIS
    une instance de cette classe.

    `operator_reference` est une référence technique — numéro de changement,
    ticket, identifiant de compte de service. Ce n'est pas un nom de personne :
    la règle du dépôt interdit d'en journaliser un, et ce champ finit dans un
    fichier conservé indéfiniment.
    """
    entry_id: str
    base_model_license: str
    fine_tune_license: str
    operator_reference: str
    accepted_at: str
    accepted: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "base_model_license": self.base_model_license,
            "fine_tune_license": self.fine_tune_license,
            "operator_reference": self.operator_reference,
            "accepted_at": self.accepted_at,
            "accepted": self.accepted,
        }


def _acceptance_problems(
    acceptance: LicenseAcceptance, entry: catalog_mod.CatalogEntry
) -> tuple[str, ...]:
    """
    Toutes les raisons pour lesquelles cette acceptation ne vaut pas pour cette entrée.

    Confronter les identifiants de licence n'est pas du zèle : une acceptation
    signée pour `apache-2.0` ne couvre pas un dépôt repassé sous `llama3.1`
    entre-temps. L'acceptation porte sur des CONDITIONS, pas sur un nom
    d'entrée.
    """
    problems: list[str] = []
    if acceptance.entry_id != entry.id:
        problems.append(
            f"l'acceptation vise « {acceptance.entry_id} », pas « {entry.id} »"
        )
    if not acceptance.accepted:
        problems.append(
            "l'acceptation est explicitement REFUSÉE (accepted=False) — "
            "le téléchargement ne peut pas avoir lieu"
        )
    if acceptance.base_model_license != entry.license.base_model.id:
        problems.append(
            f"licence du modèle de base acceptée « {acceptance.base_model_license} », "
            f"le catalogue déclare « {entry.license.base_model.id} »"
        )
    if acceptance.fine_tune_license != entry.license.fine_tune.id:
        problems.append(
            f"licence du fine-tune acceptée « {acceptance.fine_tune_license} », "
            f"le catalogue déclare « {entry.license.fine_tune.id} »"
        )
    if not acceptance.operator_reference.strip():
        problems.append("operator_reference est vide — une acceptation anonyme n'est pas traçable")
    if not acceptance.accepted_at.strip():
        problems.append("accepted_at est vide — une acceptation sans date n'est pas auditable")
    return tuple(problems)


def _parse_stored_acceptance(document: dict[str, Any]) -> LicenseAcceptance:
    """Relit une acceptation sans jamais coercer les types issus du JSON."""
    string_fields = (
        "entry_id",
        "base_model_license",
        "fine_tune_license",
        "operator_reference",
        "accepted_at",
    )
    values: dict[str, str] = {}
    for field_name in string_fields:
        value = document[field_name]
        if type(value) is not str:
            raise TypeError(f"{field_name} doit être une chaîne JSON")
        values[field_name] = value

    accepted = document["accepted"]
    if type(accepted) is not bool:
        raise TypeError("accepted doit être un booléen JSON")

    return LicenseAcceptance(
        entry_id=values["entry_id"],
        base_model_license=values["base_model_license"],
        fine_tune_license=values["fine_tune_license"],
        operator_reference=values["operator_reference"],
        accepted_at=values["accepted_at"],
        accepted=accepted,
    )


# ── Configuration ─────────────────────────────────────────────────────────────

def _default_disk_free(path: Path) -> int:
    """Espace libre réel. Remplaçable en test : on ne remplit pas un disque pour vérifier un refus."""
    return shutil.disk_usage(path).free


@dataclass(frozen=True)
class DownloadConfig:
    """
    Tout ce dont les exécuteurs ont besoin, et rien qui soit un secret.

    Le jeton n'est PAS un champ : c'est un appelable (`token_provider`). Un
    champ porterait la valeur dans un objet susceptible d'être `repr()`-é dans
    une trace d'exception ; un appelable ne rend le jeton que lorsqu'une requête
    est sur le point de partir, et sa valeur ne survit pas à la fonction.

    `transport`, `token_provider` et `disk_free` ont tous les trois un défaut
    RÉEL : la configuration de production ne se distingue pas de celle des
    tests par ce qu'elle active, mais seulement par ce qu'elle ne remplace pas.
    """
    catalog: catalog_mod.Catalog
    models_dir: Path
    transport: HttpTransport = field(default_factory=UrllibTransport)
    token_provider: Callable[[], str | None] = default_token_provider
    disk_free: Callable[[Path], int] = _default_disk_free
    acceptances: tuple[LicenseAcceptance, ...] = ()
    endpoint: str = DEFAULT_ENDPOINT
    chunk_bytes: int = DEFAULT_CHUNK_BYTES
    disk_margin_ratio: float = DEFAULT_DISK_MARGIN_RATIO
    disk_margin_min_bytes: int = DEFAULT_DISK_MARGIN_MIN_BYTES

    def __post_init__(self) -> None:
        try:
            public_https.validate_url(self.endpoint)
        except public_https.PublicHttpsError as exc:
            raise DownloadError(
                f"endpoint invalide : {self.endpoint!r} — {exc}"
            ) from exc
        if self.chunk_bytes < 1:
            raise DownloadError(f"chunk_bytes doit être >= 1, reçu {self.chunk_bytes!r}")
        if self.disk_margin_ratio < 0 or self.disk_margin_min_bytes < 0:
            raise DownloadError("la marge d'espace disque ne peut pas être négative")

    @property
    def origin_host(self) -> str:
        return urllib.parse.urlsplit(self.endpoint).hostname or ""

    def acceptance_for(self, entry_id: str) -> LicenseAcceptance | None:
        for item in self.acceptances:
            if item.entry_id == entry_id:
                return item
        return None


# ── Résolution étape → entrée de catalogue ────────────────────────────────────

def resolve_entry(step: schema.PlanStep, catalog: catalog_mod.Catalog) -> catalog_mod.CatalogEntry:
    """
    Retrouve l'entrée que cette étape désigne, ou refuse. Jamais « au mieux ».

    La grammaire de `target` est celle que `planner._model_steps` écrit, et elle
    diffère par action (`repo_id@revision`, `entry_id`, `entry_id — licence`).
    Ce couplage est une fragilité réelle et assumée : le plan est un document
    JSON, pas une API. Il est ici concentré en un seul endroit, contrôlé, et il
    échoue bruyamment — une correspondance absente ou ambiguë est un refus, pas
    un choix arbitraire parmi les candidats.
    """
    if step.action == schema.ACTION_DOWNLOAD_MODEL:
        repo_id, separator, revision = step.target.rpartition("@")
        if not separator:
            raise DownloadError(
                f"cible de téléchargement illisible : {step.target!r} — attendu "
                "« repo_id@revision »"
            )
        matches = [
            e for e in catalog.entries if e.repo_id == repo_id and e.revision == revision
        ]
    elif step.action == schema.ACTION_VERIFY_ARTIFACT:
        matches = [e for e in catalog.entries if e.id == step.target]
    elif step.action == schema.ACTION_ACCEPT_LICENSE:
        entry_id = step.target.split("—", 1)[0].strip()
        matches = [e for e in catalog.entries if e.id == entry_id]
    else:
        raise DownloadError(f"action non prise en charge par ce module : {step.action!r}")

    if not matches:
        raise DownloadError(
            f"aucune entrée du catalogue ne correspond à la cible {step.target!r} de "
            f"l'action « {step.action} » — le plan et le catalogue divergent ; "
            "régénérez le plan avec le catalogue courant"
        )
    if len(matches) > 1:
        raise DownloadError(
            f"cible ambiguë {step.target!r} : {len(matches)} entrées du catalogue "
            f"y répondent ({', '.join(e.id for e in matches)})"
        )
    return matches[0]


# ── URL et chemins ────────────────────────────────────────────────────────────

def build_file_url(entry: catalog_mod.CatalogEntry, filename: str, *, endpoint: str) -> str:
    """
    URL d'un fichier À RÉVISION FIGÉE. Aucune résolution dynamique n'est possible.

    Trois contrôles, tous fail-closed, et tous redondants avec `catalog` — c'est
    voulu : ce module est le dernier maillon avant qu'une requête ne parte, et
    il ne délègue à personne la preuve que la révision est un commit et que le
    fichier fait partie de l'ensemble déclaré.
    """
    if entry.revision is None or not _REVISION_RE.match(entry.revision):
        raise DownloadError(
            f"« {entry.id} » : révision {entry.revision!r} — attendu un commit de 40 "
            "hexadécimaux. Une branche bouge : elle ne peut pas servir de révision figée"
        )
    if filename not in {f.name for f in entry.files}:
        raise DownloadError(
            f"« {entry.id} » : {filename!r} ne fait pas partie de la liste exacte de "
            "fichiers du catalogue — aucun fichier non listé n'est téléchargé"
        )
    return (
        f"{endpoint.rstrip('/')}/{entry.repo_id}/resolve/{entry.revision}/"
        + urllib.parse.quote(filename)
    )


def _final_path(models_dir: Path, name: str) -> Path:
    """
    Chemin définitif d'un fichier. Refuse tout ce qui sortirait du répertoire.

    `catalog` interdit déjà `/` et un nom commençant par `.`, mais un contrôle
    au moment d'écrire coûte une ligne et couvre le cas où le catalogue serait
    un jour assoupli.
    """
    candidate = (models_dir / name).resolve()
    if candidate.parent != models_dir.resolve():
        raise DownloadError(
            f"nom de fichier refusé : {name!r} — il écrirait hors de {models_dir}"
        )
    return candidate


# ── Prévision d'espace disque ─────────────────────────────────────────────────

@dataclass(frozen=True)
class DiskForecast:
    """Prévision AVANT écriture. Les chiffres sont dans le message, pas seulement dans le verdict."""
    required_bytes: int
    margin_bytes: int
    free_bytes: int

    @property
    def needed_bytes(self) -> int:
        return self.required_bytes + self.margin_bytes

    @property
    def sufficient(self) -> bool:
        return self.free_bytes >= self.needed_bytes

    @property
    def message(self) -> str:
        return (
            f"{_gib(self.required_bytes)} Gio à écrire + {_gib(self.margin_bytes)} Gio de "
            f"marge = {_gib(self.needed_bytes)} Gio requis ; "
            f"{_gib(self.free_bytes)} Gio disponibles"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_bytes": self.required_bytes,
            "margin_bytes": self.margin_bytes,
            "needed_bytes": self.needed_bytes,
            "free_bytes": self.free_bytes,
            "sufficient": self.sufficient,
            "message": self.message,
        }


def _gib(value: int) -> float:
    return round(value / _GIB, 2)


def forecast_disk(required_bytes: int, free_bytes: int, config: DownloadConfig) -> DiskForecast:
    """
    Marge = max(part du volume, plancher absolu). Les deux servent à des choses différentes.

    La part proportionnelle protège des gros téléchargements (5 % de 40 Gio =
    2 Gio) ; le plancher protège des petits, où 5 % ne représenteraient rien
    alors qu'un disque à quelques mégaoctets de la saturation est déjà en
    incident.
    """
    margin = max(int(required_bytes * config.disk_margin_ratio), config.disk_margin_min_bytes)
    return DiskForecast(required_bytes=required_bytes, margin_bytes=margin, free_bytes=free_bytes)


# ── Empreintes ────────────────────────────────────────────────────────────────

def _hash_file(path: Path, chunk_bytes: int, hasher: Any | None = None) -> str:
    """SHA-256 d'un fichier, lu par blocs bornés — un GGUF ne tient pas en mémoire."""
    digest = hasher if hasher is not None else hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk_bytes)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


async def _hash_file_async(path: Path, chunk_bytes: int) -> str:
    return await asyncio.to_thread(_hash_file, path, chunk_bytes)


# ── Reprise : la preuve d'origine du fichier partiel ──────────────────────────

@dataclass(frozen=True)
class ResumeState:
    """
    Ce qui accompagne un `.part` et permet d'affirmer d'où il vient.

    Reprendre un téléchargement sur des octets dont on ne peut pas prouver
    qu'ils viennent de la même source est PIRE que recommencer : le SHA final
    échouera, mais après avoir consommé la bande passante et le temps du
    téléchargement complet, et un opérateur pressé conclura à une corruption
    réseau plutôt qu'à un `.part` étranger. D'où ce fichier voisin : sans lui,
    ou s'il désigne autre chose, le `.part` est jeté avant le premier octet.
    """
    repo_id: str
    revision: str
    file_name: str
    sha256: str
    size_bytes: int
    etag: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "resume_version": 1,
            "repo_id": self.repo_id,
            "revision": self.revision,
            "file_name": self.file_name,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "etag": self.etag,
        }

    def matches(self, other: ResumeState) -> bool:
        return (
            self.repo_id == other.repo_id
            and self.revision == other.revision
            and self.file_name == other.file_name
            and self.sha256 == other.sha256
            and self.size_bytes == other.size_bytes
        )


def _read_resume_state(path: Path) -> ResumeState | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("resume_version") != 1:
        return None
    try:
        return ResumeState(
            repo_id=str(document["repo_id"]),
            revision=str(document["revision"]),
            file_name=str(document["file_name"]),
            sha256=str(document["sha256"]),
            size_bytes=int(document["size_bytes"]),
            etag=document.get("etag") if isinstance(document.get("etag"), str) else None,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _discard_partial(part: Path, sidecar: Path) -> None:
    """Jette un partiel non prouvable. Aucun octet douteux ne survit à cet appel."""
    for target in (part, sidecar):
        try:
            target.unlink()
        except OSError:
            pass


# ── Téléchargement d'un fichier ───────────────────────────────────────────────

@dataclass
class _FileOutcome:
    """Ce qu'un fichier a produit. `verified` est la seule chose qui autorise la promotion."""
    name: str
    verified: bool
    bytes_downloaded: int
    resumed_from: int
    status: str
    detail: str
    findings: tuple[schema.Finding, ...] = ()


async def _download_one(
    entry: catalog_mod.CatalogEntry,
    cfile: catalog_mod.CatalogFile,
    models_dir: Path,
    config: DownloadConfig,
    token: str | None,
) -> _FileOutcome:
    """
    Télécharge un fichier, le vérifie, puis seulement alors lui donne son nom.

    Ordre non négociable : écrire dans `.part` → vérifier le SHA-256 → renommer.
    Toute sortie anticipée laisse le `.part` (reprenable) ou le détruit (empreinte
    fausse), jamais un fichier définitif.
    """
    final = _final_path(models_dir, cfile.name)
    part = final.with_name(final.name + PART_SUFFIX)
    sidecar = final.with_name(final.name + RESUME_SUFFIX)

    if cfile.sha256 is None or not _SHA256_RE.match(cfile.sha256):
        raise DownloadError(
            f"« {entry.id} » / {cfile.name} : empreinte SHA-256 absente ou non conforme "
            "— sans elle, aucune vérification n'est possible et rien n'est téléchargé"
        )
    if cfile.size_bytes is None:
        raise DownloadError(
            f"« {entry.id} » / {cfile.name} : taille absente du catalogue — l'espace "
            "disque ne peut pas être prévu avant écriture, le téléchargement est refusé"
        )

    expected = ResumeState(
        repo_id=entry.repo_id,
        revision=str(entry.revision),
        file_name=cfile.name,
        sha256=cfile.sha256,
        size_bytes=cfile.size_bytes,
    )

    findings: list[schema.Finding] = []
    offset = 0
    hasher = hashlib.sha256()

    if part.exists():
        recorded = _read_resume_state(sidecar)
        partial_size = part.stat().st_size
        reason = None
        if recorded is None:
            reason = "aucune preuve d'origine (fichier voisin absent ou illisible)"
        elif not recorded.matches(expected):
            reason = "la preuve d'origine désigne un autre dépôt, une autre révision ou un autre fichier"
        elif partial_size >= cfile.size_bytes:
            reason = (
                f"le partiel fait {partial_size} octets pour une cible de "
                f"{cfile.size_bytes} — il ne peut pas être une reprise valide"
            )
        if reason is not None:
            findings.append(schema.Finding(
                code="reprise_refusee",
                level="warn",
                message=(
                    f"{cfile.name} : téléchargement partiel écarté et repris de zéro — "
                    f"{reason}. Reprendre sur des octets non prouvés reviendrait à "
                    "consommer le téléchargement complet pour échouer à la fin."
                ),
            ))
            _discard_partial(part, sidecar)
        else:
            offset = partial_size
            # Le hasher est amorcé sur les octets déjà présents : le SHA final
            # porte donc sur la totalité du fichier, reprise comprise, et non
            # sur le seul segment téléchargé cette fois-ci.
            await asyncio.to_thread(_hash_file, part, config.chunk_bytes, hasher)

    sidecar.write_text(json.dumps(expected.to_dict(), ensure_ascii=False), encoding="utf-8")

    url = build_file_url(entry, cfile.name, endpoint=config.endpoint)
    response, resolved_url = await _open_following_redirects(url, config, token, range_start=offset)

    written = 0
    try:
        if offset and response.status == 200:
            # Le serveur a IGNORÉ le `Range`. Le corps est le fichier entier :
            # l'ajouter aux octets déjà présents produirait un fichier plus
            # grand que la cible, dont le SHA échouerait après un téléchargement
            # complet. On repart de zéro, explicitement.
            findings.append(schema.Finding(
                code="reprise_non_honoree",
                level="warn",
                message=(
                    f"{cfile.name} : le serveur a répondu 200 à une demande de reprise "
                    f"(octet {offset}) — la reprise n'est pas honorée, le fichier est "
                    "retéléchargé depuis le début."
                ),
            ))
            offset = 0
            hasher = hashlib.sha256()
        elif offset and response.status == 206:
            problem = _content_range_problem(response.headers.get("content-range"), offset, cfile.size_bytes)
            if problem is not None:
                _discard_partial(part, sidecar)
                return _FileOutcome(
                    name=cfile.name, verified=False, bytes_downloaded=0, resumed_from=offset,
                    status="failed",
                    detail=f"reprise refusée par incohérence de Content-Range : {problem}",
                    findings=tuple(findings),
                )
        elif response.status not in (200, 206):
            return _FileOutcome(
                name=cfile.name, verified=False, bytes_downloaded=0, resumed_from=offset,
                status="failed",
                detail=_scrub(
                    f"réponse HTTP {response.status} pour {resolved_url}"
                    + (" — dépôt gated ou jeton requis" if response.status in (401, 403) else ""),
                    token,
                ),
                findings=tuple(findings),
            )

        mode = "ab" if offset else "wb"
        with open(part, mode) as handle:
            async for block in response.chunks():
                handle.write(block)
                hasher.update(block)
                written += len(block)
                if offset + written > cfile.size_bytes:
                    # Un corps plus long que la taille annoncée par le catalogue
                    # est une divergence de source : on arrête tout de suite au
                    # lieu d'écrire des dizaines de gigaoctets pour rien.
                    break
    finally:
        await response.close()

    total = offset + written
    if total != cfile.size_bytes:
        return _FileOutcome(
            name=cfile.name, verified=False, bytes_downloaded=written, resumed_from=offset,
            status="failed",
            detail=(
                f"taille obtenue {total} octets, catalogue {cfile.size_bytes} — "
                "le partiel est conservé pour une reprise ultérieure"
                if total < cfile.size_bytes else
                f"taille obtenue {total} octets, catalogue {cfile.size_bytes} — "
                "la source ne correspond plus au catalogue"
            ),
            findings=tuple(findings),
        )

    digest = hasher.hexdigest()
    if digest != cfile.sha256:
        # Le fichier est complet mais faux. Le conserver, même sous son nom
        # temporaire, permettrait à une exécution ultérieure de le « reprendre »
        # comme s'il était sain : il est détruit ici, et jamais renommé.
        _discard_partial(part, sidecar)
        return _FileOutcome(
            name=cfile.name, verified=False, bytes_downloaded=written, resumed_from=offset,
            status="failed",
            detail=(
                f"empreinte SHA-256 obtenue {digest}, catalogue {cfile.sha256} — "
                "le fichier temporaire a été détruit et n'a jamais porté son nom définitif"
            ),
            findings=tuple(findings),
        )

    os.replace(part, final)
    try:
        sidecar.unlink()
    except OSError:
        pass

    return _FileOutcome(
        name=cfile.name, verified=True, bytes_downloaded=written, resumed_from=offset,
        status="downloaded",
        detail=f"{written} octet(s) téléchargé(s), empreinte vérifiée, renommage atomique effectué",
        findings=tuple(findings),
    )


def _content_range_problem(header: str | None, offset: int, expected_size: int) -> str | None:
    """Un `206` dont le `Content-Range` ne dit pas ce qu'on a demandé n'est pas une reprise."""
    if not header:
        return "en-tête Content-Range absent"
    match = _CONTENT_RANGE_RE.match(header.strip())
    if not match:
        return f"Content-Range illisible : {header!r}"
    if int(match.group("start")) != offset:
        return f"le serveur reprend à l'octet {match.group('start')}, {offset} demandé"
    if int(match.group("total")) != expected_size:
        return (
            f"le serveur annonce un fichier de {match.group('total')} octets, "
            f"le catalogue en déclare {expected_size}"
        )
    return None


async def _open_following_redirects(
    url: str, config: DownloadConfig, token: str | None, *, range_start: int = 0
) -> tuple[HttpResponse, str]:
    """
    Suit les redirections nous-mêmes, en re-décidant l'autorisation à chaque saut.

    C'est le cœur de la non-divulgation du jeton : `resolve/` renvoie vers un
    CDN tiers, et `authorization_headers()` cesse d'émettre l'en-tête dès que
    l'hôte change. Une bibliothèque qui suit les redirections seule recopie
    généralement les en-têtes — et livre alors le jeton au CDN.

    `Accept-Encoding: identity` n'est pas une coquetterie : une réponse
    compressée en transit rendrait les octets reçus différents des octets du
    fichier, donc le `Range` et le SHA-256 incalculables sur le flux.
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        try:
            current = public_https.validate_url(current)
        except public_https.PublicHttpsError as exc:
            raise DownloadError(str(exc)) from exc
        headers = {"Accept-Encoding": "identity"}
        if range_start:
            headers["Range"] = f"bytes={range_start}-"
        headers.update(authorization_headers(current, origin_host=config.origin_host, token=token))
        response = await config.transport.open(current, headers)
        if response.status not in (301, 302, 303, 307, 308):
            return response, current
        location = response.headers.get("location")
        await response.close()
        if not location:
            raise DownloadError(f"redirection {response.status} sans en-tête Location")
        current = urllib.parse.urljoin(current, location)
    raise DownloadError(f"plus de {MAX_REDIRECTS} redirections — chaîne refusée")


# ── Manifeste de provenance ───────────────────────────────────────────────────

def provenance_path(models_dir: Path, entry_id: str) -> Path:
    return models_dir / f"{entry_id}{PROVENANCE_SUFFIX}"


def acceptance_path(models_dir: Path, entry_id: str) -> Path:
    return models_dir / f"{entry_id}{ACCEPTANCE_SUFFIX}"


def build_manifest(
    entry: catalog_mod.CatalogEntry,
    models_dir: Path,
    *,
    downloaded_at: str,
    token_used: bool,
    acceptance: LicenseAcceptance | None,
    catalog: catalog_mod.Catalog,
) -> dict[str, Any]:
    """
    Le manifeste répond à « d'où vient ce fichier, et qui l'a autorisé ».

    Il est écrit **en dernier**, une fois tous les fichiers de l'ensemble
    vérifiés. C'est ce qui fait de l'indivisibilité une propriété du disque et
    pas seulement du modèle de données : un ensemble à moitié téléchargé n'a pas
    de manifeste, et `verify_artifact` refuse de déclarer utilisable un ensemble
    sans manifeste. Les fichiers déjà vérifiés restent en place — ils sont
    individuellement sains et évitent de tout retélécharger — mais l'ensemble
    n'est jamais présenté comme utilisable.
    """
    return {
        "manifest_version": MANIFEST_VERSION,
        "entry_id": entry.id,
        "downloaded_at": downloaded_at,
        "downloader": {
            "name": DOWNLOADER_NAME,
            "declared_in_catalog": catalog.downloader.to_dict(),
        },
        "source": {
            "provider": entry.provider,
            "repo_id": entry.repo_id,
            "repo_url": entry.repo_url,
            "revision": entry.revision,
            "revision_recorded_on": entry.revision_recorded_on,
        },
        "files": [
            {
                "name": f.name,
                "role": f.role,
                "sha256": f.sha256,
                "size_bytes": f.size_bytes,
                "path": str(models_dir / f.name),
            }
            for f in entry.files
        ],
        "total_bytes": entry.files.total_bytes,
        "license": entry.license.to_dict(),
        "license_acceptance": acceptance.to_dict() if acceptance is not None else None,
        # Nom de champ volontairement sensible et valeur volontairement booléenne :
        # c'est la façon RECOMMANDÉE par `schema` de signaler un secret sans
        # l'exposer, et `find_secret_leaks()` refuserait toute autre valeur ici.
        "token_used": token_used,
    }


def _write_json(path: Path, document: dict[str, Any]) -> None:
    """Écrit un document JSON après contrôle de non-divulgation, par fichier temporaire."""
    schema.assert_no_secrets(document)
    temporary = path.with_name(path.name + PART_SUFFIX)
    temporary.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def manifest_problems(
    document: dict[str, Any] | None, entry: catalog_mod.CatalogEntry
) -> tuple[str, ...]:
    """
    Confronte un manifeste relu au catalogue COURANT. Toutes les raisons, pas la première.

    Un manifeste qui parle d'une autre révision décrit des fichiers que le
    catalogue n'approuve plus : le laisser attester l'ensemble reviendrait à
    faire signer une provenance périmée.
    """
    if document is None:
        return ("manifeste de provenance absent ou illisible",)
    problems: list[str] = []
    if document.get("manifest_version") != MANIFEST_VERSION:
        problems.append(
            f"version de manifeste {document.get('manifest_version')!r}, attendu {MANIFEST_VERSION}"
        )
    if document.get("entry_id") != entry.id:
        problems.append(f"le manifeste vise « {document.get('entry_id')} », pas « {entry.id} »")
    source = document.get("source")
    source = source if isinstance(source, dict) else {}
    if source.get("revision") != entry.revision:
        problems.append(
            f"révision du manifeste {source.get('revision')!r}, catalogue {entry.revision!r}"
        )
    if source.get("repo_id") != entry.repo_id:
        problems.append(
            f"dépôt du manifeste {source.get('repo_id')!r}, catalogue {entry.repo_id!r}"
        )
    declared = document.get("files")
    declared = declared if isinstance(declared, list) else []
    expected = {f.name: f.sha256 for f in entry.files}
    seen = {
        str(f.get("name")): f.get("sha256")
        for f in declared
        if isinstance(f, dict)
    }
    if seen != expected:
        problems.append(
            f"l'ensemble de fichiers du manifeste ({sorted(seen)}) ne correspond pas à "
            f"celui du catalogue ({sorted(expected)}) ou leurs empreintes divergent"
        )
    return tuple(problems)


# ── État local d'un ensemble ──────────────────────────────────────────────────

@dataclass(frozen=True)
class FileState:
    """Ce qu'un fichier de l'ensemble est déjà, sur ce disque."""
    name: str
    present: bool
    size_matches: bool
    digest_matches: bool | None
    size_on_disk: int | None

    @property
    def satisfied(self) -> bool:
        return self.present and self.size_matches and self.digest_matches is True

    @property
    def conflicting(self) -> bool:
        """Présent mais faux. N'est JAMAIS réparé silencieusement."""
        return self.present and (not self.size_matches or self.digest_matches is False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "present": self.present,
            "size_matches": self.size_matches,
            "digest_matches": self.digest_matches,
            "size_on_disk": self.size_on_disk,
            "satisfied": self.satisfied,
        }


async def inspect_fileset(
    entry: catalog_mod.CatalogEntry,
    models_dir: Path,
    *,
    chunk_bytes: int,
    verify_digest: bool,
) -> tuple[FileState, ...]:
    """
    État de chaque fichier de l'ensemble. `verify_digest=False` en simulation.

    En simulation on ne lit pas 40 Gio pour produire un aperçu : `digest_matches`
    vaut alors `None`, et le résumé le DIT. Un « déjà présent » qui laisserait
    croire à une empreinte vérifiée serait un mensonge coûteux.
    """
    states: list[FileState] = []
    for cfile in entry.files:
        final = _final_path(models_dir, cfile.name)
        if not final.is_file():
            states.append(FileState(cfile.name, False, False, None, None))
            continue
        size = final.stat().st_size
        size_ok = cfile.size_bytes is not None and size == cfile.size_bytes
        digest_ok: bool | None = None
        if verify_digest and size_ok and cfile.sha256 is not None:
            digest_ok = await _hash_file_async(final, chunk_bytes) == cfile.sha256
        elif verify_digest and not size_ok:
            digest_ok = False
        states.append(FileState(cfile.name, True, size_ok, digest_ok, size))
    return tuple(states)


# ── Exécuteurs ────────────────────────────────────────────────────────────────

def _finding(code: str, level: schema.FindingLevel, message: str) -> schema.Finding:
    return schema.Finding(code=code, level=level, message=message)


def _resolved_models_dir(context: execution.ExecutionContext, config: DownloadConfig) -> Path:
    """Répertoire de destination, prouvé dans les racines autorisées du contexte."""
    return context.resolve_path(config.models_dir)


def _license_gate(
    entry: catalog_mod.CatalogEntry, models_dir: Path, config: DownloadConfig
) -> tuple[LicenseAcceptance | None, str | None]:
    """
    L'acceptation de licence, ou la raison pour laquelle il n'y en a pas.

    Cherchée d'abord dans la configuration (l'opérateur vient de la fournir),
    puis sur disque (une exécution antérieure l'a enregistrée). Jamais déduite
    du caractère permissif de la licence : §4 en fait une décision humaine, et
    une licence permissive assortie de `operator_acceptance_required: true` dans
    le catalogue reste bloquante — c'est précisément le cas que ce code ne doit
    pas « simplifier ».
    """
    if not entry.license.operator_acceptance_required:
        return None, None

    acceptance = config.acceptance_for(entry.id)
    if acceptance is not None:
        problems = _acceptance_problems(acceptance, entry)
        if problems:
            return None, "acceptation fournie mais invalide : " + " ; ".join(problems)
        return acceptance, None

    stored = _read_json(acceptance_path(models_dir, entry.id))
    if stored is not None:
        try:
            recorded = _parse_stored_acceptance(stored)
        except (KeyError, TypeError, ValueError):
            return None, "acceptation enregistrée illisible — refaites l'étape accept_license"
        problems = _acceptance_problems(recorded, entry)
        if problems:
            return None, "acceptation enregistrée périmée : " + " ; ".join(problems)
        return recorded, None

    return None, (
        f"« {entry.id} » exige une acceptation explicite de licence "
        f"({entry.license.base_model.id} / {entry.license.fine_tune.id}) et aucune n'a été "
        "fournie. Le bootstrap ne peut pas accepter à la place de l'opérateur : exécutez "
        "l'étape accept_license avec une acceptation explicite."
    )


async def _execute_download(
    step: schema.PlanStep, context: execution.ExecutionContext, config: DownloadConfig
) -> execution.StepResult:
    """Télécharge l'ensemble indivisible d'une entrée. Tout, ou rien d'utilisable."""
    entry = resolve_entry(step, config.catalog)
    models_dir = _resolved_models_dir(context, config)
    # `download_fileset()` refuse une entrée non épinglée ou gated : l'appeler
    # ici, plutôt que de lire `entry.files`, c'est laisser le catalogue tenir
    # l'indivisibilité et le fail-closed au lieu de les réimplémenter.
    fileset = entry.download_fileset()

    acceptance, refusal = _license_gate(entry, models_dir, config)
    if refusal is not None:
        return execution.StepResult.for_step(
            step,
            status=execution.STEP_FAILED,
            summary=f"licence non acceptée pour « {entry.id} » — rien n'a été téléchargé",
            findings=(_finding("licence_non_acceptee", "fail", refusal),),
            error=refusal,
        )

    verify = context.applying
    states = await inspect_fileset(
        entry, models_dir, chunk_bytes=config.chunk_bytes, verify_digest=verify
    )

    conflicting = [s for s in states if s.conflicting]
    if conflicting:
        detail = ", ".join(s.name for s in conflicting)
        message = (
            f"« {entry.id} » : {detail} — un fichier porte déjà le nom définitif mais ne "
            "correspond pas au catalogue. Il n'est NI écrasé NI réparé : déplacez-le ou "
            "supprimez-le vous-même après avoir compris d'où il vient."
        )
        return execution.StepResult.for_step(
            step,
            status=execution.STEP_FAILED,
            summary=f"fichier existant divergent pour « {entry.id} »",
            evidence={"files": [s.to_dict() for s in states]},
            findings=(_finding("artefact_existant_divergent", "fail", message),),
            error=message,
        )

    todo = [f for f in fileset if not _state_of(states, f.name).satisfied]
    required = sum(f.size_bytes or 0 for f in todo)
    missing_size = [f.name for f in todo if f.size_bytes is None]
    if missing_size:
        message = (
            f"« {entry.id} » : taille absente du catalogue pour {missing_size} — l'espace "
            "disque ne peut pas être prévu avant écriture, le téléchargement est refusé"
        )
        return execution.StepResult.for_step(
            step,
            status=execution.STEP_FAILED,
            summary=f"prévision d'espace disque impossible pour « {entry.id} »",
            findings=(_finding("taille_inconnue", "fail", message),),
            error=message,
        )

    forecast = forecast_disk(required, config.disk_free(models_dir), config)

    if context.dry_run:
        return _dry_run_result(step, entry, states, todo, forecast, acceptance)

    if todo and not forecast.sufficient:
        message = (
            f"« {entry.id} » : espace disque insuffisant sur {models_dir} — "
            f"{forecast.message}. Rien n'a été écrit."
        )
        return execution.StepResult.for_step(
            step,
            status=execution.STEP_FAILED,
            summary=f"espace disque insuffisant pour « {entry.id} »",
            evidence={"disk": forecast.to_dict()},
            findings=(_finding("espace_disque_insuffisant", "fail", message),),
            error=message,
        )

    if not todo:
        # Rien à télécharger. Reste la question du manifeste : s'il est déjà là
        # et cohérent, l'étape n'a strictement rien à faire (`already_satisfied`) ;
        # s'il manque ou parle d'autre chose, l'écrire EST une modification de
        # l'hôte, et le rapporter `already_satisfied` mentirait sur ce point.
        target = provenance_path(models_dir, entry.id)
        stale = manifest_problems(_read_json(target), entry)
        evidence = {
            "files": [s.to_dict() for s in states],
            "provenance_path": str(target),
            "downloaded_bytes": 0,
        }
        if not stale:
            return execution.StepResult.for_step(
                step,
                status=execution.STEP_ALREADY_SATISFIED,
                summary=(
                    f"« {entry.id} » : les {len(states)} fichier(s) de l'ensemble sont présents "
                    "et à la bonne empreinte, manifeste cohérent — aucun octet retéléchargé"
                ),
                evidence=evidence,
            )
        _write_json(target, build_manifest(
            entry, models_dir, downloaded_at=context.now(), token_used=False,
            acceptance=acceptance, catalog=config.catalog,
        ))
        evidence["provenance_rewritten_because"] = list(stale)
        return execution.StepResult.for_step(
            step,
            status=execution.STEP_DONE,
            summary=(
                f"« {entry.id} » : fichiers déjà présents et vérifiés, manifeste de "
                "provenance (ré)écrit"
            ),
            evidence=evidence,
        )

    token = config.token_provider()
    outcomes: list[_FileOutcome] = []
    findings: list[schema.Finding] = []
    failure: str | None = None

    for cfile in fileset:
        if _state_of(states, cfile.name).satisfied:
            outcomes.append(_FileOutcome(
                name=cfile.name, verified=True, bytes_downloaded=0, resumed_from=0,
                status="already_present", detail="présent et vérifié avant cette exécution",
            ))
            continue
        try:
            outcome = await _download_one(entry, cfile, models_dir, config, token)
        # Volontairement large, et c'est le seul endroit du module qui l'est.
        # Le transport est injectable, donc arbitraire : rien n'empêche une
        # implémentation de lever une exception dont le message recopie les
        # en-têtes de la requête — et donc le jeton. Laisser remonter cette
        # exception la ferait consigner par `execute_plan()`, qui n'expurge
        # qu'avec les motifs de `schema` et ne connaît pas la valeur du jeton.
        # `CancelledError` dérive de `BaseException` et continue de remonter :
        # une annulation n'est pas un échec métier.
        except Exception as exc:
            outcome = _FileOutcome(
                name=cfile.name, verified=False, bytes_downloaded=0, resumed_from=0,
                status="failed",
                detail=_scrub(f"{type(exc).__name__}: {exc}", token),
            )
        outcomes.append(outcome)
        findings.extend(outcome.findings)
        context.journaliser(f"{entry.id}/{cfile.name} → {outcome.status}")
        if not outcome.verified:
            # L'ensemble est indivisible : dès qu'un fichier manque à l'appel,
            # poursuivre reviendrait à télécharger des gigaoctets pour un
            # ensemble qui ne sera de toute façon pas déclaré utilisable.
            failure = f"{cfile.name} : {outcome.detail}"
            break

    evidence = {
        "files": [
            {
                "name": o.name,
                "status": o.status,
                "verified": o.verified,
                "bytes_downloaded": o.bytes_downloaded,
                "resumed_from": o.resumed_from,
                "detail": o.detail,
            }
            for o in outcomes
        ],
        "downloaded_bytes": sum(o.bytes_downloaded for o in outcomes),
        "disk": forecast.to_dict(),
        "token_used": token is not None,
    }

    if failure is not None:
        findings.append(_finding(
            "ensemble_incomplet", "fail",
            (
                f"« {entry.id} » : l'ensemble indivisible n'est pas complet, aucun manifeste "
                "de provenance n'a été écrit. Les fichiers déjà vérifiés sont conservés pour "
                "la reprise, mais l'ensemble n'est PAS utilisable en l'état."
            ),
        ))
        return execution.StepResult.for_step(
            step,
            status=execution.STEP_FAILED,
            summary=f"téléchargement incomplet de « {entry.id} »",
            evidence=evidence,
            findings=tuple(findings),
            error=_scrub(failure, token),
        )

    manifest = build_manifest(
        entry, models_dir, downloaded_at=context.now(), token_used=token is not None,
        acceptance=acceptance, catalog=config.catalog,
    )
    _write_json(provenance_path(models_dir, entry.id), manifest)
    evidence["provenance_path"] = str(provenance_path(models_dir, entry.id))

    if config.catalog.downloader.name != DOWNLOADER_NAME:
        findings.append(_finding(
            "provenance_telechargeur_divergent", "warn",
            (
                f"Le catalogue déclare « {config.catalog.downloader.name} » comme téléchargeur, "
                f"mais ces fichiers ont été récupérés par « {DOWNLOADER_NAME} ». Le manifeste "
                "porte les deux mentions ; corrigez le bloc `downloader` de catalog.yaml pour "
                "que la licence déclarée soit celle du logiciel réellement employé."
            ),
        ))

    return execution.StepResult.for_step(
        step,
        status=execution.STEP_DONE,
        summary=(
            f"« {entry.id} » : ensemble de {len(outcomes)} fichier(s) téléchargé et vérifié, "
            f"manifeste de provenance écrit"
        ),
        evidence=evidence,
        findings=tuple(findings),
    )


def _state_of(states: Sequence[FileState], name: str) -> FileState:
    for state in states:
        if state.name == name:
            return state
    return FileState(name, False, False, None, None)


def _dry_run_result(
    step: schema.PlanStep,
    entry: catalog_mod.CatalogEntry,
    states: Sequence[FileState],
    todo: Sequence[catalog_mod.CatalogFile],
    forecast: DiskForecast,
    acceptance: LicenseAcceptance | None,
) -> execution.StepResult:
    """
    Simulation : aucun octet écrit, aucune requête émise, et le volume annoncé.

    Le statut reste `would_apply` même quand tout semble déjà présent : en
    simulation les empreintes ne sont pas recalculées, donc affirmer
    `already_satisfied` serait affirmer plus que ce qui a été vérifié.
    """
    return execution.StepResult.for_step(
        step,
        status=execution.STEP_WOULD_APPLY,
        summary=(
            f"« {entry.id} » : {len(todo)}/{len(states)} fichier(s) à télécharger, "
            f"{_gib(forecast.required_bytes)} Gio — {forecast.message}"
            + ("" if forecast.sufficient else " — INSUFFISANT")
        ),
        evidence={
            "entry_id": entry.id,
            "repo_id": entry.repo_id,
            "revision": entry.revision,
            "would_download": [
                {"name": f.name, "role": f.role, "size_bytes": f.size_bytes} for f in todo
            ],
            "files": [s.to_dict() for s in states],
            "disk": forecast.to_dict(),
            "license_acceptance_present": acceptance is not None,
            "avertissement": (
                "Simulation : les empreintes des fichiers déjà présents n'ont PAS été "
                "recalculées, et aucune requête réseau n'a été émise."
            ),
        },
        findings=(
            ()
            if forecast.sufficient or not todo
            else (_finding(
                "espace_disque_insuffisant", "fail",
                f"« {entry.id} » : {forecast.message} — l'application échouerait.",
            ),)
        ),
    )


async def _execute_verify(
    step: schema.PlanStep, context: execution.ExecutionContext, config: DownloadConfig
) -> execution.StepResult:
    """
    Vérifie l'ensemble complet et son manifeste. Ne modifie jamais rien.

    Le statut rendu en application est `already_satisfied`, jamais `done` : une
    vérification qui réussit n'a rien changé sur l'hôte, et `ExecutionReport.changed()`
    ne doit pas devenir vrai à cause d'une lecture.
    """
    entry = resolve_entry(step, config.catalog)
    models_dir = _resolved_models_dir(context, config)
    fileset = entry.download_fileset()

    if context.dry_run:
        return execution.StepResult.for_step(
            step,
            status=execution.STEP_WOULD_APPLY,
            summary=(
                f"« {entry.id} » : {len(fileset)} empreinte(s) SHA-256 seraient confrontées "
                "au catalogue, ainsi que le manifeste de provenance"
            ),
            evidence={
                "entry_id": entry.id,
                "files": [f.name for f in fileset],
            },
        )

    states = await inspect_fileset(
        entry, models_dir, chunk_bytes=config.chunk_bytes, verify_digest=True
    )
    manifest = _read_json(provenance_path(models_dir, entry.id))
    problems = list(manifest_problems(manifest, entry))

    for state in states:
        if not state.present:
            problems.append(f"{state.name} : absent")
        elif not state.size_matches:
            problems.append(f"{state.name} : taille {state.size_on_disk} ≠ catalogue")
        elif state.digest_matches is not True:
            problems.append(f"{state.name} : empreinte SHA-256 différente de celle du catalogue")

    evidence = {
        "entry_id": entry.id,
        "files": [s.to_dict() for s in states],
        "provenance_present": manifest is not None,
        "problems": problems,
    }

    if problems:
        message = (
            f"« {entry.id} » : l'ensemble n'est pas utilisable — " + " ; ".join(problems)
        )
        return execution.StepResult.for_step(
            step,
            status=execution.STEP_FAILED,
            summary=f"vérification de « {entry.id} » en échec",
            evidence=evidence,
            findings=(_finding("artefact_non_verifiable", "fail", message),),
            error=message,
        )

    return execution.StepResult.for_step(
        step,
        status=execution.STEP_ALREADY_SATISFIED,
        summary=(
            f"« {entry.id} » : {len(states)} fichier(s) vérifié(s) contre le catalogue, "
            "manifeste de provenance cohérent"
        ),
        evidence=evidence,
    )


async def _execute_accept_license(
    step: schema.PlanStep, context: execution.ExecutionContext, config: DownloadConfig
) -> execution.StepResult:
    """
    Enregistre une acceptation FOURNIE. N'en invente jamais aucune.

    L'acceptation est contrôlée avant tout, y compris en simulation : une
    simulation qui tairait l'absence d'acceptation ferait croire l'application
    possible et déplacerait la découverte du blocage au pire moment.
    """
    entry = resolve_entry(step, config.catalog)
    models_dir = _resolved_models_dir(context, config)

    if not entry.license.operator_acceptance_required:
        message = (
            f"« {entry.id} » : le plan demande une acceptation de licence que le catalogue "
            "courant ne requiert pas (`operator_acceptance_required: false`). Le plan et le "
            "catalogue divergent — régénérez le plan plutôt que d'appliquer celui-ci."
        )
        return execution.StepResult.for_step(
            step,
            status=execution.STEP_FAILED,
            summary=f"plan et catalogue divergents pour « {entry.id} »",
            findings=(_finding("licence_plan_divergent", "fail", message),),
            error=message,
        )

    acceptance = config.acceptance_for(entry.id)
    if acceptance is None:
        stored, refusal = _license_gate(entry, models_dir, config)
        if stored is not None:
            return execution.StepResult.for_step(
                step,
                status=execution.STEP_ALREADY_SATISFIED,
                summary=f"acceptation de licence déjà enregistrée pour « {entry.id} »",
                evidence={
                    "entry_id": entry.id,
                    "acceptance_path": str(acceptance_path(models_dir, entry.id)),
                    "base_model_license": entry.license.base_model.id,
                    "fine_tune_license": entry.license.fine_tune.id,
                },
            )
        message = refusal or (
            f"« {entry.id} » : aucune acceptation de licence fournie."
        )
        return execution.StepResult.for_step(
            step,
            status=execution.STEP_FAILED,
            summary=f"acceptation de licence manquante pour « {entry.id} »",
            findings=(_finding("licence_non_acceptee", "fail", message),),
            error=message,
        )

    problems = _acceptance_problems(acceptance, entry)
    if problems:
        message = (
            f"« {entry.id} » : l'acceptation fournie ne vaut pas pour cette entrée — "
            + " ; ".join(problems)
        )
        return execution.StepResult.for_step(
            step,
            status=execution.STEP_FAILED,
            summary=f"acceptation de licence invalide pour « {entry.id} »",
            findings=(_finding("licence_acceptation_invalide", "fail", message),),
            error=message,
        )

    if context.dry_run:
        return execution.StepResult.for_step(
            step,
            status=execution.STEP_WOULD_APPLY,
            summary=(
                f"« {entry.id} » : l'acceptation fournie ({entry.license.base_model.id} / "
                f"{entry.license.fine_tune.id}) serait enregistrée"
            ),
            evidence={
                "entry_id": entry.id,
                "acceptance_path": str(acceptance_path(models_dir, entry.id)),
                "base_model_license": entry.license.base_model.id,
                "fine_tune_license": entry.license.fine_tune.id,
            },
        )

    document = dict(acceptance.to_dict())
    document["recorded_at"] = context.now()
    document["license"] = entry.license.to_dict()
    _write_json(acceptance_path(models_dir, entry.id), document)

    return execution.StepResult.for_step(
        step,
        status=execution.STEP_DONE,
        summary=(
            f"« {entry.id} » : acceptation de licence enregistrée "
            f"({entry.license.base_model.id} / {entry.license.fine_tune.id})"
        ),
        evidence={
            "entry_id": entry.id,
            "acceptance_path": str(acceptance_path(models_dir, entry.id)),
            "base_model_license": entry.license.base_model.id,
            "fine_tune_license": entry.license.fine_tune.id,
        },
        findings=(
            ()
            if entry.license.permissive
            else (_finding(
                "licence_non_permissive_acceptee", "warn",
                (
                    f"« {entry.id} » est sous licence non permissive "
                    f"({entry.license.base_model.id} / {entry.license.fine_tune.id}). "
                    "L'acceptation enregistrée engage l'organisation sur ses conditions "
                    "d'usage et de redistribution."
                ),
            ),)
        ),
    )


# ── Enregistrement ────────────────────────────────────────────────────────────

def make_executors(config: DownloadConfig) -> dict[str, execution.StepExecutor]:
    """Les trois exécuteurs, liés à une configuration. Utile pour les tester un par un."""

    async def download(step: schema.PlanStep, context: execution.ExecutionContext):
        return await _execute_download(step, context, config)

    async def verify(step: schema.PlanStep, context: execution.ExecutionContext):
        return await _execute_verify(step, context, config)

    async def accept(step: schema.PlanStep, context: execution.ExecutionContext):
        return await _execute_accept_license(step, context, config)

    return {
        schema.ACTION_DOWNLOAD_MODEL: download,
        schema.ACTION_VERIFY_ARTIFACT: verify,
        schema.ACTION_ACCEPT_LICENSE: accept,
    }


def register_executors(registry: execution.ExecutorRegistry, config: DownloadConfig) -> None:
    """
    Branche les trois exécuteurs dans un registre.

    `ExecutorRegistry.register()` refuse un second enregistrement pour la même
    action : appeler cette fonction deux fois sur le même registre lève, et
    c'est voulu — deux configurations concurrentes pour la même action rendraient
    l'applicateur imprévisible.
    """
    for action, executor in make_executors(config).items():
        registry.register(action, executor)
