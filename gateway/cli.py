"""
CLI d'administration — utilisation en ligne de commande sur le serveur.

Usage :
  python cli.py add-user alice --email alice@univ-pau.fr --rpm 30
  python cli.py create-key alice --name "these-2025"
  python cli.py revoke-key llmgw-abc12345
  python cli.py list-users
  python cli.py anonymize-user alice --yes   # RGPD, irréversible
  python cli.py usage-report --month 2025-03
  python cli.py usage-report --from 2025-01-01 --to 2025-03-31
  python cli.py status
"""
from __future__ import annotations

import asyncio
import sys
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

import database as db
from config import settings
from model_registry import ModelRegistry

app = typer.Typer(
    name="llm-gateway",
    help="CLI d'administration du LLM Inference Gateway UPPA",
    no_args_is_help=True,
)
console = Console()


def _run(coro):
    """Helper pour exécuter un coroutine depuis le CLI synchrone."""
    return asyncio.run(coro)


# ── Bootstrap DB ──────────────────────────────────────────────────────────────

async def _ensure_db():
    await db.init_db()


# ── Utilisateurs ──────────────────────────────────────────────────────────────

@app.command("add-user")
def add_user(
    username: str = typer.Argument(..., help="Nom d'utilisateur (alphanumérique)"),
    email: Optional[str] = typer.Option(None, "--email", "-e", help="Email institutionnel"),
    rpm: Optional[int] = typer.Option(None, "--rpm", help="Limite req/minute (défaut: config)"),
    monthly_tokens: Optional[int] = typer.Option(None, "--monthly-tokens", help="Quota tokens/mois (0=illimité)"),
    notes: Optional[str] = typer.Option(None, "--notes", help="Notes libres"),
):
    """Crée un nouvel utilisateur."""
    async def _do():
        await _ensure_db()
        try:
            user = await db.create_user(
                username=username,
                email=email,
                rpm_limit=rpm,
                monthly_token_limit=monthly_tokens,
                notes=notes,
            )
            console.print(f"[green]Utilisateur créé :[/green] {user['username']} (ID: {user['id']})")
            console.print(f"  RPM limit     : {user['rpm_limit']}")
            console.print(f"  Token quota   : {user['monthly_token_limit'] or 'illimité'}")
        except Exception as exc:
            if "UNIQUE" in str(exc):
                console.print(f"[red]Erreur :[/red] Un utilisateur '{username}' existe déjà.")
                raise typer.Exit(1)
            raise

    _run(_do())


@app.command("list-users")
def list_users(
    show_inactive: bool = typer.Option(False, "--all", "-a", help="Inclure les utilisateurs désactivés"),
):
    """Liste tous les utilisateurs."""
    async def _do():
        await _ensure_db()
        users = await db.list_users()
        if not show_inactive:
            users = [u for u in users if u["is_active"]]

        table = Table(title="Utilisateurs", show_header=True, header_style="bold cyan")
        table.add_column("ID", style="dim", width=4)
        table.add_column("Username", min_width=16)
        table.add_column("Email", min_width=24)
        table.add_column("Actif", width=6)
        table.add_column("RPM", width=5)
        table.add_column("Créé le", width=12)

        for u in users:
            active = "[green]oui[/green]" if u["is_active"] else "[red]non[/red]"
            table.add_row(
                str(u["id"]),
                u["username"],
                u["email"] or "—",
                active,
                str(u["rpm_limit"]),
                u["created_at"][:10],
            )

        console.print(table)

    _run(_do())


@app.command("disable-user")
def disable_user(username: str = typer.Argument(...)):
    """Désactive un utilisateur (toutes ses clés deviennent invalides immédiatement)."""
    async def _do():
        await _ensure_db()
        user = await db.get_user_by_username(username)
        if not user:
            console.print(f"[red]Utilisateur '{username}' introuvable.[/red]")
            raise typer.Exit(1)
        await db.update_user(user["id"], is_active=False)
        console.print(f"[yellow]Utilisateur '{username}' désactivé.[/yellow]")

    _run(_do())


@app.command("enable-user")
def enable_user(username: str = typer.Argument(...)):
    """Réactive un utilisateur désactivé."""
    async def _do():
        await _ensure_db()
        user = await db.get_user_by_username(username)
        if not user:
            console.print(f"[red]Utilisateur '{username}' introuvable.[/red]")
            raise typer.Exit(1)
        await db.update_user(user["id"], is_active=True)
        console.print(f"[green]Utilisateur '{username}' réactivé.[/green]")

    _run(_do())


@app.command("anonymize-user")
def anonymize_user(
    username: str = typer.Argument(..., help="Nom d'utilisateur à anonymiser"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirmer sans demander"),
):
    """
    Anonymise un utilisateur — IRRÉVERSIBLE. Droit à l'effacement RGPD.

    Efface définitivement le nom d'utilisateur, l'e-mail, les notes et le nom
    des clés API ; désactive le compte et révoque toutes ses clés.
    CONSERVE la ligne utilisateur et tout l'historique usage_log, pour que la
    facturation et l'audit restent exploitables (politique DEC-001).

    Ce n'est PAS une suppression de ligne : l'historique d'usage reste agrégeable
    sous un pseudonyme, la personne n'est plus ré-identifiable.
    """
    async def _do():
        await _ensure_db()
        user = await db.get_user_by_username(username)
        if not user:
            console.print(f"[red]Utilisateur '{username}' introuvable.[/red]")
            raise typer.Exit(1)

        if not yes:
            console.print(
                "[yellow]Anonymisation irréversible :[/yellow] nom, e-mail, notes et "
                "noms de clés seront effacés définitivement ; les clés seront "
                "révoquées. L'historique d'usage est conservé sous un pseudonyme."
            )
            if not typer.confirm(f"Anonymiser '{username}' ?", default=False):
                console.print("Annulé.")
                return

        result = await db.anonymize_user(user["id"])
        if result is None:
            console.print(f"[red]Utilisateur '{username}' introuvable.[/red]")
            raise typer.Exit(1)

        if result["already_anonymized"]:
            console.print(
                f"[yellow]Déjà anonymisé le {result['anonymized_at']}[/yellow] — "
                "horodatage initial conservé, clés revérifiées."
            )
        else:
            console.print("[green]Utilisateur anonymisé.[/green]")
        console.print(f"  Pseudonyme      : {result['username']}")
        console.print(f"  Anonymisé le    : {result['anonymized_at']}")
        console.print(f"  Clés révoquées  : {result['keys_revoked']} / {result['keys_total']}")
        console.print("  Effacé          : username, email, notes, nom des clés")
        console.print("  Conservé        : id, created_at, journal usage_log")

    _run(_do())


# ── Clés API ──────────────────────────────────────────────────────────────────

@app.command("create-key")
def create_key(
    username: str = typer.Argument(..., help="Nom de l'utilisateur"),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Nom de la clé (ex: 'these-2025')"),
    expires: Optional[str] = typer.Option(None, "--expires", help="Date d'expiration ISO 8601"),
):
    """
    Génère une nouvelle clé API pour un utilisateur.
    La clé brute est affichée UNE SEULE FOIS — copiez-la maintenant.
    """
    async def _do():
        await _ensure_db()
        user = await db.get_user_by_username(username)
        if not user:
            console.print(f"[red]Utilisateur '{username}' introuvable.[/red]")
            raise typer.Exit(1)

        raw_key, key_row = await db.create_api_key(
            user_id=user["id"],
            name=name,
            expires_at=expires,
        )

        console.print()
        console.print("[bold green]Clé API créée avec succès[/bold green]")
        console.print(f"  Utilisateur : [cyan]{username}[/cyan]")
        console.print(f"  Nom         : {key_row['name'] or '—'}")
        console.print(f"  Préfixe     : {key_row['key_prefix']}")
        console.print(f"  Expire le   : {key_row['expires_at'] or 'jamais'}")
        console.print()
        console.print("[bold yellow]CLEF API (à copier maintenant — non récupérable) :[/bold yellow]")
        console.print(f"  [bold white]{raw_key}[/bold white]")
        console.print()

    _run(_do())


@app.command("list-keys")
def list_keys(username: str = typer.Argument(...)):
    """Liste les clés API d'un utilisateur."""
    async def _do():
        await _ensure_db()
        user = await db.get_user_by_username(username)
        if not user:
            console.print(f"[red]Utilisateur '{username}' introuvable.[/red]")
            raise typer.Exit(1)

        keys = await db.list_keys_for_user(user["id"])
        if not keys:
            console.print(f"Aucune clé pour '{username}'.")
            return

        table = Table(title=f"Clés API — {username}", header_style="bold cyan")
        table.add_column("Préfixe", width=16)
        table.add_column("Nom", min_width=14)
        table.add_column("Active", width=7)
        table.add_column("Dernière utilisation", width=20)
        table.add_column("Expire le", width=12)

        for k in keys:
            active = "[green]oui[/green]" if k["is_active"] else "[red]non[/red]"
            table.add_row(
                k["key_prefix"],
                k["name"] or "—",
                active,
                k["last_used"] or "jamais",
                k["expires_at"] or "jamais",
            )

        console.print(table)

    _run(_do())


@app.command("revoke-key")
def revoke_key(
    key_prefix: str = typer.Argument(..., help="Préfixe de la clé (ex: llmgw-abc12345)"),
):
    """Révoque une clé API. Immédiatement effectif."""
    async def _do():
        await _ensure_db()
        ok = await db.revoke_key(key_prefix)
        if ok:
            console.print(f"[yellow]Clé '{key_prefix}' révoquée.[/yellow]")
        else:
            console.print(f"[red]Aucune clé active avec le préfixe '{key_prefix}'.[/red]")
            raise typer.Exit(1)

    _run(_do())


# ── Rapports d'usage ──────────────────────────────────────────────────────────

@app.command("usage-report")
def usage_report(
    username: Optional[str] = typer.Option(None, "--user", "-u", help="Filtrer par utilisateur"),
    from_date: Optional[str] = typer.Option(None, "--from", help="Date début (ex: 2025-01-01)"),
    to_date: Optional[str] = typer.Option(None, "--to", help="Date fin (ex: 2025-12-31)"),
    month: Optional[str] = typer.Option(None, "--month", "-m", help="Mois YYYY-MM (ex: 2025-03)"),
    summary: bool = typer.Option(False, "--summary", "-s", help="Vue agrégée par utilisateur"),
):
    """Rapport d'usage (tokens consommés, requêtes, etc.)."""
    async def _do():
        await _ensure_db()

        # Convertir --month en --from/--to
        _from = from_date
        _to = to_date
        if month:
            import calendar
            year, mon = int(month.split("-")[0]), int(month.split("-")[1])
            last_day = calendar.monthrange(year, mon)[1]
            _from = f"{year:04d}-{mon:02d}-01"
            _to = f"{year:04d}-{mon:02d}-{last_day:02d}"

        if summary:
            rows = await db.get_usage_summary(from_date=_from, to_date=_to)
            table = Table(title="Résumé d'usage", header_style="bold cyan")
            table.add_column("Utilisateur", min_width=16)
            table.add_column("Requêtes", width=10, justify="right")
            table.add_column("Tokens prompt", width=14, justify="right")
            table.add_column("Tokens réponse", width=15, justify="right")
            table.add_column("Total tokens", width=13, justify="right")
            table.add_column("Durée moy. (ms)", width=16, justify="right")
            table.add_column("Dernière req.", width=14)

            for r in rows:
                avg = f"{r['avg_duration_ms']:.0f}" if r["avg_duration_ms"] else "—"
                table.add_row(
                    r["username"],
                    str(r["request_count"]),
                    f"{r['total_prompt_tokens']:,}",
                    f"{r['total_completion_tokens']:,}",
                    f"{r['total_tokens']:,}",
                    avg,
                    (r["last_request"] or "—")[:16],
                )
            console.print(table)
        else:
            user_id = None
            if username:
                user = await db.get_user_by_username(username)
                if not user:
                    console.print(f"[red]Utilisateur '{username}' introuvable.[/red]")
                    raise typer.Exit(1)
                user_id = user["id"]

            rows = await db.get_usage_report(
                user_id=user_id,
                from_date=_from,
                to_date=_to,
                limit=500,
            )

            table = Table(title="Journal d'usage", header_style="bold cyan")
            table.add_column("Date", width=18)
            table.add_column("Utilisateur", min_width=14)
            table.add_column("Modèle", min_width=16)
            table.add_column("Prompt", width=8, justify="right")
            table.add_column("Réponse", width=8, justify="right")
            table.add_column("Total", width=8, justify="right")
            table.add_column("ms", width=6, justify="right")
            table.add_column("HTTP", width=4, justify="right")

            for r in rows:
                table.add_row(
                    r["timestamp"][:16],
                    r["username"],
                    r["model"],
                    str(r["prompt_tokens"]),
                    str(r["completion_tokens"]),
                    str(r["total_tokens"]),
                    str(r["duration_ms"] or "—"),
                    str(r["status_code"] or "—"),
                )
            console.print(table)
            console.print(f"  {len(rows)} entrée(s)")

    _run(_do())


# ── Rétention / purge ─────────────────────────────────────────────────────────

@app.command("purge-usage")
def purge_usage(
    older_than_days: int = typer.Option(
        ..., "--older-than-days", help="Supprime les entrées usage_log plus anciennes que N jours"
    ),
):
    """
    Purge de rétention MANUELLE du journal d'usage puis VACUUM.
    À exécuter hors ligne : VACUUM verrouille la base.
    """
    if older_than_days < 0:
        console.print("[red]--older-than-days doit être >= 0.[/red]")
        raise typer.Exit(1)

    async def _do():
        await _ensure_db()
        deleted = await db.purge_usage_older_than(older_than_days)
        console.print(
            f"[green]Purge terminée :[/green] {deleted} entrée(s) usage_log "
            f"supprimée(s) (> {older_than_days} jours)."
        )

    _run(_do())


# ── Statut ────────────────────────────────────────────────────────────────────

@app.command("status")
def status():
    """Affiche le registre des modèles et la configuration VRAM."""
    try:
        registry = ModelRegistry(
            config_path=settings.models_config_path,
            allowed_model_dirs=settings.allowed_model_dirs if settings.allowed_model_dirs else None,
        )
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]Erreur registre :[/red] {exc}")
        raise typer.Exit(1)

    budget = settings.effective_vram_budget_gb()
    console.print()
    console.print("[bold cyan]Configuration VRAM[/bold cyan]")
    console.print(f"  Total GPU       : {settings.total_vram_gb:.1f} GB")
    console.print(f"  Overhead        : {settings.vram_overhead_gb:.1f} GB")
    console.print(f"  Marge sécurité  : {settings.vram_safety_margin * 100:.0f}%")
    console.print(f"  Budget net      : [bold green]{budget:.1f} GB[/bold green]")
    console.print(f"  Max modèles     : {settings.max_loaded_models}")
    console.print(f"  Pool de ports   : {settings.base_llama_port}–{settings.base_llama_port + settings.max_loaded_models - 1}")
    console.print(f"  Idle timeout    : {settings.idle_timeout_seconds}s")
    console.print()

    models = registry.list_all()
    table = Table(title="Registre des modèles", header_style="bold cyan")
    table.add_column("ID", min_width=20)
    table.add_column("VRAM", width=8, justify="right")
    table.add_column("Activé", width=8)
    table.add_column("Capacités", min_width=24)
    table.add_column("Chemin", min_width=30)

    for m in models:
        enabled = "[green]oui[/green]" if m.enabled else "[red]non[/red]"
        caps = ", ".join(m.capabilities)
        table.add_row(m.id, f"{m.vram_gb:.1f} GB", enabled, caps, str(m.path))

    console.print(table)
    console.print()
    console.print("[dim]Note : L'état live (READY/LOADING) n'est visible que via GET /admin/status[/dim]")
    console.print()


# ── Diagnostic préflight (AUT-012) ────────────────────────────────────────────

@app.command("doctor")
def doctor(
    json_output: bool = typer.Option(
        False, "--json", help="Rapport JSON (schéma stable) au lieu du texte"
    ),
    env_file: Optional[str] = typer.Option(
        None, "--env-file",
        help="EnvironmentFile à valider (défaut : EnvironmentFile= de l'unité systemd, sinon /etc/llm-gateway/env)",
    ),
    nginx_conf: Optional[str] = typer.Option(
        None, "--nginx-conf",
        help="Configuration nginx à contrôler (défaut : /etc/nginx/sites-available/llm-gateway)",
    ),
    systemd_unit: Optional[str] = typer.Option(
        None, "--systemd-unit",
        help="Unité systemd à contrôler (défaut : /etc/systemd/system/llm-gateway.service)",
    ),
    verify_hashes: bool = typer.Option(
        False, "--verify-hashes",
        help="Vérifie les empreintes SHA-256 des GGUF — COÛTEUX (lecture intégrale, plusieurs centaines de Go)",
    ),
    strict: bool = typer.Option(
        False, "--strict", help="Traite les avertissements comme des échecs bloquants"
    ),
):
    """
    Diagnostic préflight de l'hôte et de la configuration (aucun service requis).

    Exit codes : 0 conforme, 1 échec bloquant, 3 avertissements seulement,
    4 erreur interne de doctor (2 est réservé aux erreurs d'usage de la CLI).
    Aucun secret n'apparaît dans le rapport. Détails : docs/admin.md.
    """
    from pathlib import Path as _Path

    import doctor as doctor_module

    try:
        options = doctor_module.DoctorOptions(
            env_file=_Path(env_file) if env_file else None,
            nginx_conf=_Path(nginx_conf) if nginx_conf else None,
            systemd_unit=_Path(systemd_unit) if systemd_unit else None,
            verify_hashes=verify_hashes,
        )
        report = _run(doctor_module.run_doctor(options))
    except Exception as exc:
        console.print(f"[red]doctor a échoué :[/red] {type(exc).__name__}: {exc}")
        raise typer.Exit(doctor_module.EXIT_ERROR)

    if json_output:
        # Écriture brute : le rendu rich reformaterait le document JSON.
        sys.stdout.write(doctor_module.render_json(report, strict=strict) + "\n")
    else:
        console.print(
            doctor_module.render_human(report, strict=strict),
            markup=False,
            highlight=False,
            soft_wrap=True,
        )

    raise typer.Exit(report.exit_code(strict=strict))


# ── Planificateur de bootstrap (AUT-001 → AUT-005, AUT-013) ───────────────────

@app.command("bootstrap-plan")
def bootstrap_plan(
    json_output: bool = typer.Option(
        False, "--json", help="Plan JSON (schéma versionné) au lieu du texte"
    ),
    mode: str = typer.Option("local", "--mode", help="Topologie visée : local ou cluster"),
    catalog: Optional[str] = typer.Option(
        None, "--catalog", help="Catalogue de modèles approuvés (défaut : celui du dépôt)"
    ),
    hardware_profile: Optional[str] = typer.Option(
        None, "--hardware-profile",
        help="Profil matériel DÉCLARÉ (JSON §5) au lieu de sonder — VM, passthrough, hôte où les outils constructeur échouent",
    ),
    models_dir: Optional[str] = typer.Option(
        None, "--models-dir", help="Volume où atterriraient les GGUF (défaut : /models)"
    ),
    model: Optional[list[str]] = typer.Option(
        None, "--model", help="Restreindre à ces identifiants de catalogue (répétable)"
    ),
    max_models: int = typer.Option(1, "--max-models", help="Nombre maximal de modèles retenus"),
    llama_bin: Optional[str] = typer.Option(
        None, "--llama-bin", help="Binaire llama-server déjà en place, à évaluer (`--version` sera exécuté)"
    ),
    pin_version: Optional[str] = typer.Option(
        None, "--pin-version", help="Version llama.cpp épinglée, au format « bNNNNN »"
    ),
    pin_commit: Optional[str] = typer.Option(
        None, "--pin-commit", help="Commit git correspondant à --pin-version"
    ),
    min_build: int = typer.Option(
        0, "--min-build",
        help="Premier build patché connu — plancher de sécurité d'où LLAMA_SERVER_MIN_BUILD est généré",
    ),
    runtime_variants: Optional[str] = typer.Option(
        None, "--runtime-variants",
        help="Matrice d'artefacts llama-server épinglée par l'opérateur (YAML) — REMPLACE la "
             "matrice livrée ; modèle dans deploy/runtime-variants.yaml.example",
    ),
    allow_container: bool = typer.Option(
        False, "--allow-container",
        help="Accepter une image conteneur épinglée par digest (étape 2 de §6). "
             "`server_manager` ne sait pas encore lancer de conteneur : le plan est descriptible, "
             "son application ne l'est pas",
    ),
    allow_local_build: bool = typer.Option(
        True, "--allow-local-build/--no-local-build",
        help="Accepter le build local reproductible (étape 4 de §6). Le refuser sur la matrice "
             "livrée ne laisse AUCUNE variante éligible, puisqu'elle ne porte aucune empreinte",
    ),
    allow_cpu_fallback: bool = typer.Option(
        False, "--allow-cpu-fallback",
        help="ASSUMER la dégradation GPU → CPU si aucune variante GPU sûre n'existe. Refusé par "
             "défaut : une installation CPU démarre, répond et passe le smoke test, avec un TTFT "
             "inacceptable qui ne se verra qu'en production",
    ),
    llmfit_bin: Optional[str] = typer.Option(
        None, "--llmfit-bin", help="Binaire LLMfit à consulter (défaut : recherché dans le PATH)"
    ),
    llmfit_version: Optional[str] = typer.Option(
        None, "--llmfit-version", help="Version LLMfit attendue — va de pair avec --llmfit-sha256"
    ),
    llmfit_sha256: Optional[str] = typer.Option(
        None, "--llmfit-sha256", help="Empreinte SHA-256 attendue du binaire LLMfit (64 hex minuscules)"
    ),
    llmfit_timeout: float = typer.Option(
        20.0, "--llmfit-timeout", help="Délai maximal accordé à LLMfit, en secondes"
    ),
    llmfit_profile: Optional[str] = typer.Option(
        None, "--llmfit-profile",
        help="Profil de recommandation écrit à la main, à la place de LLMfit — même validation",
    ),
    no_llmfit: bool = typer.Option(
        False, "--no-llmfit", help="Ne pas consulter LLMfit du tout"
    ),
    strict: bool = typer.Option(
        False, "--strict", help="Traite les avertissements comme des blocages"
    ),
):
    """
    Calcule le plan d'amorçage de cet hôte — SANS RIEN APPLIQUER.

    Inventorie le matériel, résout le runtime llama-server, consulte LLMfit s'il
    est présent, filtre le catalogue de modèles approuvés et rend la séquence
    d'étapes qu'une application exécuterait, avec ses décisions et leurs raisons.
    Aucun téléchargement, aucune compilation, aucune écriture : le seul
    sous-processus possible est `llama-server --version`, et seulement si
    `--llama-bin` est fourni.

    Sans `--pin-version`/`--pin-commit`, le runtime ne peut pas être résolu et le
    plan sort bloqué : le planificateur refuse d'inventer un numéro de build, qui
    se propagerait dans les manifestes de provenance avec l'apparence d'un fait.

    `--runtime-variants` fournit la matrice d'artefacts épinglée par l'opérateur.
    La matrice livrée avec EVARuntime ne porte AUCUNE empreinte — aucune n'a été
    vérifiée dans ce dépôt, et en inventer une serait pire que de ne pas en avoir
    — de sorte que seules ses variantes `local-build` sont éligibles. Le fichier
    fourni REMPLACE cette matrice ; modèle et mode d'emploi dans
    `deploy/runtime-variants.yaml.example`. Un fichier malformé fait refuser la
    commande, jamais retomber sur la matrice livrée.

    LLMfit est un conseiller OPTIONNEL : sans `--llmfit-version`/`--llmfit-sha256`,
    son binaire n'est pas exécuté et la section sort en `skip` — un binaire non
    épinglé n'est pas un binaire de confiance. `--llmfit-profile` fournit une
    recommandation écrite à la main, qui passe par la même validation.

    Le mode `cluster` n'est pas planifiable au jalon M1 et est refusé
    explicitement : le plan produit inventorierait l'hôte gateway alors que le
    binaire et les modèles vivent sur les nœuds.

    Exit codes : 0 applicable, 1 bloqué, 3 avertissements seulement,
    4 erreur interne (2 est réservé aux erreurs d'usage de la CLI).
    """
    from datetime import datetime, timezone
    from pathlib import Path as _Path

    from bootstrap import llmfit as _llmfit
    from bootstrap import planner as planner_module
    from bootstrap import runtime_resolver as _runtime
    from bootstrap import runtime_variants as _variants
    from bootstrap import schema as _schema

    if (llmfit_version is None) != (llmfit_sha256 is None):
        console.print(
            "[red]--llmfit-version et --llmfit-sha256 vont ensemble :[/red] une version seule se "
            "déclare, une empreinte seule ne dit pas ce qu'on croyait installer."
        )
        raise typer.Exit(_schema.EXIT_USAGE)
    if (pin_version is None) != (pin_commit is None):
        console.print("[red]--pin-version et --pin-commit vont ensemble : l'un sans l'autre n'épingle rien.[/red]")
        raise typer.Exit(_schema.EXIT_USAGE)

    release = None
    if pin_version is not None:
        try:
            release = _runtime.ReleasePolicy(
                pinned_version=pin_version,
                pinned_commit=pin_commit or "",
                security_floor_build=min_build,
            )
        except _runtime.ProvenanceError as exc:
            console.print(f"[red]Politique de release refusée :[/red] {exc}")
            raise typer.Exit(_schema.EXIT_USAGE)

    # AUT-018 — la matrice d'artefacts fournie par l'opérateur. Sans cette option,
    # `ResolverPolicy.variants` — l'échappatoire que le résolveur documente comme
    # « l'opérateur ou la CI fournit les variantes épinglées » — n'était
    # atteignable que depuis du code Python, et la matrice livrée ne porte aucune
    # empreinte : en configuration par défaut, l'installateur n'installait rien.
    # AUT-019 — les trois drapeaux de `ResolverPolicy` sont pilotables ici.
    #
    # Sans eux, un opérateur pouvait fournir une variante `official-container`
    # correctement épinglée par digest (AUT-018) et la voir SYSTÉMATIQUEMENT
    # écartée — « mode conteneur non accepté par la politique ». Il avait le moyen
    # de l'épingler et aucun moyen de l'autoriser. Quatrième occurrence du motif
    # « code juste, inatteignable depuis le parcours réel ».
    politique_non_defaut = (
        allow_container or not allow_local_build or allow_cpu_fallback
    )

    resolver = None
    if runtime_variants is not None or politique_non_defaut:
        if release is None:
            # La matrice dit QUOI installer, l'épinglage dit QUELLE version. Sans
            # `ReleasePolicy`, `ResolverPolicy` n'est pas constructible et le
            # fichier serait lu pour rien : mieux vaut le dire que produire un
            # plan bloqué dont la cause affichée serait ailleurs. Même raison pour
            # les drapeaux : autoriser une branche de §6 sans épingler de version
            # ne résout rien et laisserait croire que l'option a été prise en compte.
            console.print(
                "[red]--runtime-variants et les options de politique (--allow-container, "
                "--no-local-build, --allow-cpu-fallback) exigent --pin-version et "
                "--pin-commit :[/red] la matrice dit quel artefact installer, l'épinglage dit "
                "quelle version il porte. Le manifeste de provenance a besoin des deux."
            )
            raise typer.Exit(_schema.EXIT_USAGE)
        try:
            variants = (
                _variants.load_variants(_Path(runtime_variants))
                if runtime_variants is not None
                else _runtime.DEFAULT_VARIANTS
            )
            resolver = _runtime.ResolverPolicy(
                release=release,
                variants=variants,
                allow_container=allow_container,
                allow_local_build=allow_local_build,
                allow_cpu_fallback=allow_cpu_fallback,
            )
        except _variants.RuntimeVariantsError as exc:
            # Refus, jamais repli sur la matrice livrée : un opérateur qui fournit
            # un fichier attend que ce fichier soit employé, pas qu'il soit ignoré.
            console.print(f"[red]Matrice d'artefacts refusée :[/red] {exc}")
            raise typer.Exit(_schema.EXIT_USAGE)

    try:
        llmfit_config = _llmfit.LLMfitConfig(
            enabled=not no_llmfit,
            binary_path=_Path(llmfit_bin) if llmfit_bin else None,
            pin=(
                _llmfit.LLMfitPin(version=llmfit_version, sha256=llmfit_sha256)
                if llmfit_version is not None else None
            ),
            timeout_seconds=llmfit_timeout,
            manual_profile_path=_Path(llmfit_profile) if llmfit_profile else None,
        )
    except _llmfit.LLMfitError as exc:
        console.print(f"[red]Réglage LLMfit refusé :[/red] {exc}")
        raise typer.Exit(_schema.EXIT_USAGE)

    options = planner_module.PlannerOptions(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        mode=mode,
        catalog_path=_Path(catalog) if catalog else None,
        hardware_profile_path=_Path(hardware_profile) if hardware_profile else None,
        models_dir=_Path(models_dir) if models_dir else planner_module.PlannerOptions.models_dir,
        selected_ids=tuple(model) if model else None,
        max_models=max_models,
        existing_binary=_Path(llama_bin) if llama_bin else None,
        release_policy=release,
        resolver_policy=resolver,
        llmfit_config=llmfit_config,
    )

    try:
        plan = _run(planner_module.build_plan(options))
        rendered = (
            _schema.render_json(plan, strict=strict) if json_output
            else _schema.render_human(plan, strict=strict)
        )
    except planner_module.PlannerUsageError as exc:
        # Faute de saisie de l'opérateur — pas une panne du planificateur.
        console.print(f"[red]Demande impossible à honorer :[/red] {exc}")
        raise typer.Exit(_schema.EXIT_USAGE)
    except _schema.PlanError as exc:
        console.print(f"[red]Plan refusé :[/red] {exc}")
        raise typer.Exit(_schema.EXIT_ERROR)
    except Exception as exc:
        console.print(f"[red]bootstrap-plan a échoué :[/red] {type(exc).__name__}: {exc}")
        raise typer.Exit(_schema.EXIT_ERROR)

    if json_output:
        # Écriture brute : le rendu rich reformaterait le document JSON.
        sys.stdout.write(rendered + "\n")
    else:
        console.print(rendered, markup=False, highlight=False, soft_wrap=True)

    raise typer.Exit(plan.exit_code(strict=strict))


# ── Applicateur de bootstrap (AUT-006 → AUT-011, AUT-015) ─────────────────────

@app.command("bootstrap-apply")
def bootstrap_apply(
    plan: str = typer.Argument(..., help="Plan JSON produit par `bootstrap-plan --json`"),
    apply_for_real: bool = typer.Option(
        False, "--apply",
        help="APPLIQUER RÉELLEMENT. Sans ce drapeau, la commande se contente de simuler.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Rapport d'installation JSON au lieu du texte"
    ),
    allowed_root: Optional[list[str]] = typer.Option(
        None, "--allowed-root",
        help="Répertoire que l'application a le droit de toucher (répétable, obligatoire)",
    ),
    catalog: Optional[str] = typer.Option(
        None, "--catalog", help="Catalogue de modèles approuvés (défaut : celui du dépôt)"
    ),
    models_dir: Optional[str] = typer.Option(
        None, "--models-dir", help="Volume où atterrissent les GGUF"
    ),
    registry: Optional[str] = typer.Option(
        None, "--registry", help="models.yaml à écrire — va avec --runtime-version, "
        "--hardware-fingerprint et --vram-budget-gb",
    ),
    runtime_version: Optional[str] = typer.Option(
        None, "--runtime-version", help="Build de llama-server en service (ex. « b6042 »)"
    ),
    hardware_fingerprint: Optional[str] = typer.Option(
        None, "--hardware-fingerprint", help="Empreinte matérielle de l'hôte (§9)"
    ),
    vram_budget_gb: float = typer.Option(
        0.0, "--vram-budget-gb", help="Budget VRAM net de l'hôte, en Go"
    ),
    runtime_root: Optional[str] = typer.Option(
        None, "--runtime-root", help="Racine de releases du runtime (ex. /opt/llama.cpp)"
    ),
    llama_server_bin: Optional[str] = typer.Option(
        None, "--llama-server-bin",
        help="Binaire à employer pour la calibration (défaut : runtime installé ou config)",
    ),
    calibration_report_dir: Optional[str] = typer.Option(
        None, "--calibration-report-dir", help="Répertoire des preuves de calibration"
    ),
    calibration_port: int = typer.Option(
        19091, "--calibration-port", help="Port loopback réservé au llama-server de calibration"
    ),
    calibration_load_timeout: Optional[float] = typer.Option(
        None, "--calibration-load-timeout",
        help="Borne de chargement de calibration (défaut : MODEL_LOAD_TIMEOUT_SECONDS)",
    ),
    base_url: Optional[str] = typer.Option(
        None, "--base-url", help="URL publique traversée par la recette (nginx en production)"
    ),
    admin_url: Optional[str] = typer.Option(
        None, "--admin-url", help="URL directe de la gateway pour /ready et /admin"
    ),
    admin_secret_file: Optional[str] = typer.Option(
        None, "--admin-secret-file",
        help="Fichier privé contenant ADMIN_SECRET (sinon variable ADMIN_SECRET ; jamais argv)",
    ),
    env_file: Optional[str] = typer.Option(
        None, "--env-file",
        help="EnvironmentFile du service à recouper et durcir (production : /etc/llm-gateway/env)",
    ),
    accept_license: Optional[list[str]] = typer.Option(
        None, "--accept-license", help="ID de modèle dont la licence est acceptée (répétable)"
    ),
    license_reference: Optional[str] = typer.Option(
        None, "--license-reference",
        help="Référence technique commune aux acceptations (ticket/changement, jamais un nom)",
    ),
    ttft_threshold_ms: int = typer.Option(
        0, "--ttft-threshold-ms", help="Seuil TTFT en ms (0 = mesure sans seuil)"
    ),
    ttft_gate: bool = typer.Option(
        False, "--ttft-gate", help="Faire du dépassement TTFT un échec"
    ),
):
    """
    Applique un plan d'amorçage relu — EN SIMULATION PAR DÉFAUT.

    La simulation est le défaut et l'application demande `--apply` : un mode
    d'exécution ne s'obtient jamais par omission d'argument. Une simulation
    complète sort en 3, jamais en 0 — rien n'a été appliqué, et un script
    d'exploitation ne doit pas pouvoir confondre les deux.

    Le plan est relu par `execution.load_plan_file()`, qui refuse un document
    d'une autre version de schéma, incohérent, non applicable, porteur de
    bloqueurs ou exposant un secret. Aucune de ces barrières n'est réimplémentée
    ici.

    Le câblage est explicite : ce que les options ne fournissent pas n'est pas
    exécutable, et la commande refuse avant de commencer. Les secrets viennent
    d'un fichier privé ou de l'environnement, jamais d'argv. La calibration
    lance son propre llama-server sur loopback : elle ne rend pas publiquement
    servable une entrée encore désactivée.

    Une pose réelle du runtime exige l'EnvironmentFile du service et un plancher
    de build positif. Après succès complet uniquement, les deux réglages runtime
    de ce fichier sont remplacés atomiquement sans changer ni afficher ses secrets.

    Exit codes : 0 installation complète, 1 échec ou plan inapplicable,
    2 erreur d'usage / câblage incomplet, 3 partiel (dont toute simulation),
    4 erreur interne de l'applicateur.
    """
    from pathlib import Path as _Path

    from bootstrap import applier as applier_module
    from bootstrap import catalog as _catalog
    from bootstrap import downloader as _downloader
    from bootstrap import execution as _execution
    from bootstrap import install_report as _install_report
    from bootstrap import production as _production
    from bootstrap import registry_writer as _writer
    from bootstrap import schema as _schema
    from bootstrap import service_env as _service_env
    from doctor import check_env_file as _check_env_file
    from doctor import collect_secret_values as _collect_secret_values
    from doctor import load_settings_from_env_file as _load_settings_from_env_file
    from doctor import parse_env_file as _parse_env_file
    from doctor import redact as _redact_known_secrets

    service_env_path = _Path(env_file) if env_file else None
    service_secret_values = (
        _collect_secret_values(None, _parse_env_file(service_env_path))
        if service_env_path is not None
        else ()
    )

    def _safe_message(message: str) -> str:
        return _execution.redact_for_log(
            _redact_known_secrets(message, service_secret_values)
        )

    roots = [r for r in (allowed_root or []) if r.strip()]
    if not roots:
        console.print(
            "[red]--allowed-root est obligatoire :[/red] une liste vide n'autorise rien, et "
            "elle ne signifie pas « pas de contrainte ». Déclarez explicitement les "
            "répertoires que l'application a le droit de toucher."
        )
        raise typer.Exit(_schema.EXIT_USAGE)

    if registry is None and any(
        option is not None for option in (runtime_version, hardware_fingerprint)
    ):
        console.print(
            "[red]--runtime-version et --hardware-fingerprint n'ont de sens "
            "qu'avec --registry.[/red] Quand ils sont omis, le runtime et le matériel "
            "sont dérivés puis recoupés depuis le plan relu."
        )
        raise typer.Exit(_schema.EXIT_USAGE)

    try:
        # Relecture avant le câblage : la configuration runtime est reconstruite
        # depuis CE document validé, jamais depuis une seconde décision.
        loaded_plan = _execution.load_plan_file(_Path(plan))
    except _execution.PlanRefused as exc:
        console.print("[red]Plan refusé :[/red] " + _safe_message(str(exc)))
        raise typer.Exit(_schema.EXIT_BLOCKED)

    mode = (
        _execution.ExecutionMode.APPLY if apply_for_real
        else _execution.ExecutionMode.DRY_RUN
    )

    service_settings = settings
    admin_secret_environ = None
    if service_env_path is not None:
        env_check = _check_env_file(service_env_path, explicit=True)
        if env_check.is_blocking:
            console.print(
                "[red]EnvironmentFile refusé :[/red] "
                + _safe_message(env_check.message)
            )
            raise typer.Exit(_schema.EXIT_USAGE)
        try:
            service_settings = _load_settings_from_env_file(service_env_path)
        except Exception as exc:
            console.print(
                "[red]EnvironmentFile invalide :[/red] "
                + _safe_message(str(exc))
            )
            raise typer.Exit(_schema.EXIT_USAGE)
        admin_secret_environ = {"ADMIN_SECRET": service_settings.admin_secret}

    try:
        config = _build_applier_config(
            applier_module=applier_module,
            catalog_module=_catalog,
            downloader_module=_downloader,
            production_module=_production,
            writer_module=_writer,
            loaded_plan=loaded_plan,
            catalog_path=_Path(catalog) if catalog else None,
            models_dir=_Path(models_dir) if models_dir else None,
            registry_path=_Path(registry) if registry else None,
            runtime_version=runtime_version,
            hardware_fingerprint=hardware_fingerprint,
            vram_budget_gb=vram_budget_gb,
            runtime_root=_Path(runtime_root) if runtime_root else None,
            llama_server_binary=_Path(llama_server_bin) if llama_server_bin else None,
            calibration_report_dir=(
                _Path(calibration_report_dir) if calibration_report_dir else None
            ),
            calibration_port=calibration_port,
            calibration_load_timeout=calibration_load_timeout,
            base_url=base_url,
            admin_url=admin_url,
            admin_secret_file=_Path(admin_secret_file) if admin_secret_file else None,
            service_env_path=service_env_path,
            service_settings=service_settings,
            admin_secret_environ=admin_secret_environ,
            accepted_license_ids=tuple(accept_license or ()),
            license_reference=license_reference,
            ttft_threshold_ms=ttft_threshold_ms,
            ttft_gate=ttft_gate,
            dry_run=mode is _execution.ExecutionMode.DRY_RUN,
        )
    except (_schema.PlanError, OSError, ValueError) as exc:
        console.print(
            "[red]Câblage refusé :[/red] " + _safe_message(str(exc))
        )
        raise typer.Exit(_schema.EXIT_USAGE)

    try:
        outcome = _run(applier_module.apply_loaded_plan(
            loaded_plan, config, mode=mode, allowed_roots=[_Path(r) for r in roots],
        ))
        if (
            mode is _execution.ExecutionMode.APPLY
            and outcome.exit_code() == _schema.EXIT_OK
            and service_env_path is not None
            and config.runtime is not None
        ):
            runtime_resolution = _production.runtime_resolution_from_plan(
                loaded_plan.document
            )
            _service_env.harden_runtime_environment(
                service_env_path,
                binary=config.runtime.published_binary,
                min_build=runtime_resolution.min_build,
            )
        rendered = (
            _install_report.render_install_json(outcome.install) if json_output
            else _install_report.render_install_human(outcome.install)
        )
    except applier_module.ApplierUsageError as exc:
        console.print(
            "[red]Demande impossible à honorer :[/red] "
            + _safe_message(str(exc))
        )
        raise typer.Exit(_schema.EXIT_USAGE)
    except _execution.PlanRefused as exc:
        # Expurgé, et ce n'est pas de la prudence de principe : `PlanRefused`
        # porte l'ORIGINE du plan, c'est-à-dire un chemin de fichier fourni par
        # l'opérateur. Un plan déposé sous un nom qui contient un jeton faisait
        # ressortir ce jeton dans le message de refus.
        console.print("[red]Plan refusé :[/red] " + _safe_message(str(exc)))
        raise typer.Exit(_schema.EXIT_BLOCKED)
    except Exception as exc:
        # Ni le message ni le type ne sont recopiés d'un objet susceptible de
        # porter un secret : `redact_for_log` expurge avant que quoi que ce soit
        # n'atteigne la sortie ou le journal.
        console.print(
            "[red]bootstrap-apply a échoué :[/red] "
            + _safe_message(f"{type(exc).__name__}: {exc}")
        )
        raise typer.Exit(_schema.EXIT_ERROR)

    if json_output:
        # Écriture brute : le rendu rich reformaterait le document JSON.
        sys.stdout.write(rendered + "\n")
    else:
        console.print(rendered, markup=False, highlight=False, soft_wrap=True)
        for finding in outcome.findings:
            console.print(
                f"  · {finding.level} [{finding.code}] {finding.message}",
                markup=False, highlight=False, soft_wrap=True,
            )

    raise typer.Exit(outcome.exit_code())


def _service_allowed_model_dirs(effective_settings, models_dir, production_module):
    """
    Allowlist de modèles avec laquelle l'écrivain valide `models.yaml`.

    C'est celle du SERVICE (`ALLOWED_MODEL_DIRS` de `--env-file`, sinon de
    l'environnement de la CLI), jamais `(--models-dir,)` : la gateway rechargera
    le fichier écrit avec SA liste. Une liste plus large accepterait une entrée
    que la gateway refusera au prochain démarrage ; une liste plus étroite
    refusait tout registre dont une entrée existante vit ailleurs — le
    `models.yaml` livré déclare à la fois `/models` et `/data/models`.

    Vide, elle n'est pas « sans contrainte » : refus, il faut la déclarer.
    `--models-dir` doit être dedans, sinon chaque GGUF téléchargé produirait
    une entrée que la gateway refuserait.
    """
    from pathlib import Path

    from bootstrap import execution as _execution

    declared = tuple(
        Path(d) for d in (effective_settings.allowed_model_dirs or ()) if str(d).strip()
    )
    relatives = [str(d) for d in declared if not d.is_absolute()]
    if relatives:
        # La CLI résoudrait ces entrées depuis SON répertoire courant, la
        # gateway depuis son WorkingDirectory : la validation ne prouverait
        # plus rien sur ce que le service acceptera.
        raise production_module.ProductionWiringError(
            "ALLOWED_MODEL_DIRS contient des chemins relatifs "
            f"({', '.join(relatives)}) : la CLI et la gateway ne les résolvent pas "
            "depuis le même répertoire. Déclarez des chemins absolus."
        )
    if not declared:
        raise production_module.ProductionWiringError(
            "--registry exige ALLOWED_MODEL_DIRS dans la configuration du service "
            "(--env-file, ou l'environnement de la CLI) : models.yaml est validé avec "
            "l'allowlist que la gateway appliquera en le rechargeant. Une liste vide "
            "n'est pas « sans contrainte »."
        )
    if models_dir is not None:
        try:
            _execution.ensure_within_allowed_roots(models_dir, declared)
        except _execution.ExecutionError as exc:
            raise production_module.ProductionWiringError(
                f"--models-dir n'est pas sous ALLOWED_MODEL_DIRS du service : {exc}. "
                "La gateway refuserait chaque modèle installé là."
            ) from exc
    return declared


def _build_applier_config(
    *,
    applier_module,
    catalog_module,
    downloader_module,
    production_module,
    writer_module,
    loaded_plan,
    catalog_path,
    models_dir,
    registry_path,
    runtime_version,
    hardware_fingerprint,
    vram_budget_gb,
    runtime_root,
    llama_server_binary,
    calibration_report_dir,
    calibration_port,
    calibration_load_timeout,
    base_url,
    admin_url,
    admin_secret_file,
    service_env_path,
    service_settings,
    admin_secret_environ,
    accepted_license_ids,
    license_reference,
    ttft_threshold_ms,
    ttft_gate,
    dry_run,
):
    """
    Construit le câblage à partir des options. Ce qui manque reste `None`.

    Aucun repli : une option absente ne produit pas une configuration par défaut
    qui ferait croire au câblage. C'est le contrôle de pré-vol de l'applicateur
    qui refusera, en nommant les actions concernées.
    """
    from pathlib import Path

    from bootstrap import calibration as _calibration
    from bootstrap import first_token as _first_token
    from bootstrap import schema as _schema
    from bootstrap import warmup as _warmup

    effective_settings = service_settings or settings

    actions = {step.action for step in loaded_plan.steps}
    section_names = {
        section.get("name") for section in loaded_plan.document.get("sections", ())
        if isinstance(section, dict)
    }
    model_ids = tuple(dict.fromkeys(
        step.target for step in loaded_plan.steps
        if step.action == _schema.ACTION_CALIBRATE_MODEL
    ))

    runtime = None
    if _schema.ACTION_INSTALL_RUNTIME in actions and runtime_root is not None:
        runtime = production_module.runtime_installer_from_plan(
            loaded_plan.document, runtime_root
        )

    download = None
    catalog_entries: dict = {}
    catalogue = None
    if accepted_license_ids and models_dir is None:
        raise production_module.ProductionWiringError(
            "--accept-license exige --models-dir, où l'acceptation sera enregistrée"
        )
    if license_reference and not accepted_license_ids:
        raise production_module.ProductionWiringError(
            "--license-reference sans --accept-license n'accepte aucune licence"
        )
    if models_dir is not None:
        catalogue = catalog_module.load_catalog(catalog_path)
        catalog_entries = {e.id: e.to_dict() for e in catalogue.plannable_entries()}
        required_acceptance_ids = tuple(dict.fromkeys(
            downloader_module.resolve_entry(step, catalogue).id
            for step in loaded_plan.steps
            if step.action == _schema.ACTION_ACCEPT_LICENSE
        ))
        unexpected_acceptances = sorted(
            set(accepted_license_ids) - set(required_acceptance_ids)
        )
        if unexpected_acceptances:
            raise production_module.ProductionWiringError(
                "--accept-license porte des acceptations hors plan : "
                + ", ".join(unexpected_acceptances)
            )
        acceptances = production_module.license_acceptances(
            catalogue,
            accepted_license_ids,
            operator_reference=license_reference or "",
        ) if accepted_license_ids else ()
        download = downloader_module.DownloadConfig(
            catalog=catalogue, models_dir=models_dir, acceptances=acceptances
        )

    runtime_resolution = None
    if (
        _schema.ACTION_INSTALL_RUNTIME in actions
        or (
            _schema.ACTION_CALIBRATE_MODEL in actions
            and _schema.SECTION_RUNTIME in section_names
        )
    ):
        runtime_resolution = production_module.runtime_resolution_from_plan(
            loaded_plan.document
        )
    planned_runtime_version = (
        runtime_resolution.manifest.version
        if runtime_resolution is not None and runtime_resolution.manifest is not None
        else None
    )
    effective_runtime_version = runtime_version or planned_runtime_version
    if (
        runtime_version is not None
        and planned_runtime_version is not None
        and runtime_version != planned_runtime_version
    ):
        raise production_module.ProductionWiringError(
            "--runtime-version diverge de la version publiée dans le plan"
        )

    planned_hardware_fingerprint = None
    if (
        _schema.ACTION_CALIBRATE_MODEL in actions
        and _schema.SECTION_HARDWARE in section_names
        and any(
            isinstance(section, dict)
            and section.get("name") == _schema.SECTION_HARDWARE
            and isinstance(section.get("data"), dict)
            and isinstance(section["data"].get("gpus"), list)
            for section in loaded_plan.document.get("sections", ())
        )
    ):
        planned_hardware_fingerprint = production_module.hardware_fingerprint_from_plan(
            loaded_plan.document
        )
    effective_hardware_fingerprint = (
        hardware_fingerprint or planned_hardware_fingerprint
    )
    if (
        hardware_fingerprint is not None
        and planned_hardware_fingerprint is not None
        and hardware_fingerprint != planned_hardware_fingerprint
    ):
        raise production_module.ProductionWiringError(
            "--hardware-fingerprint diverge de l'inventaire publié dans le plan"
        )

    writer = None
    if registry_path is not None:
        allowed_model_dirs = _service_allowed_model_dirs(
            effective_settings, models_dir, production_module
        )
        writer = writer_module.WriterConfig(
            registry_path=registry_path,
            # Sans --models-dir, aucun catalogue n'est câblé : l'écrivain ne crée
            # aucune entrée et n'emploie ce répertoire que pour son contrôle
            # d'appartenance. Le premier répertoire autorisé y satisfait.
            models_dir=models_dir if models_dir is not None else allowed_model_dirs[0],
            allowed_model_dirs=allowed_model_dirs,
            runtime_version=effective_runtime_version,
            hardware_fingerprint=effective_hardware_fingerprint,
            vram_budget_gb=vram_budget_gb,
            catalog_entries=catalog_entries,
        )

    calibration_options = None
    if (
        _schema.ACTION_CALIBRATE_MODEL in actions
        and catalogue is not None
        and models_dir is not None
        and calibration_report_dir is not None
        and effective_runtime_version is not None
        and effective_hardware_fingerprint is not None
    ):
        binary = llama_server_binary
        if binary is None and runtime is not None:
            binary = runtime.published_binary
        if binary is None:
            binary = effective_settings.llama_server_bin
        targets = production_module.calibration_targets(
            catalogue, models_dir, model_ids
        )
        probe_host = production_module.LlamaServerCalibrationProbes(
            binary=binary,
            targets=targets,
            port=calibration_port,
            visible_gpu_indices=production_module.visible_gpu_indices_from_plan(
                loaded_plan.document
            ),
            visible_gpu_uuids=production_module.visible_gpu_uuids_from_plan(
                loaded_plan.document
            ),
            load_timeout_seconds=(
                calibration_load_timeout
                if calibration_load_timeout is not None
                else effective_settings.model_load_timeout_seconds
            ),
        )
        calibration_options = _calibration.CalibrationOptions(
            probes=probe_host.as_probes(),
            runtime_version=effective_runtime_version,
            hardware_fingerprint=effective_hardware_fingerprint,
            report_dir=calibration_report_dir,
            params={model_id: target.params for model_id, target in targets.items()},
        )

    needs_http = bool(actions & {
        _schema.ACTION_SMOKE_TEST, _schema.ACTION_WARMUP_MODEL,
    })
    if runtime is not None and not dry_run:
        if service_env_path is None:
            raise production_module.ProductionWiringError(
                "--env-file est obligatoire pour une installation runtime réelle : "
                "le binaire publié et LLAMA_SERVER_MIN_BUILD doivent être raccordés "
                "à l'unité systemd"
            )
        if runtime_resolution is None or runtime_resolution.min_build <= 0:
            raise production_module.ProductionWiringError(
                "--min-build doit être strictement positif avant --apply : un runtime "
                "sans plancher de sécurité ne peut pas être publié en production"
            )

    if needs_http and service_env_path is not None and registry_path is not None:
        configured_registry = Path(effective_settings.models_config_path).absolute()
        requested_registry = Path(registry_path).absolute()
        if configured_registry != requested_registry:
            raise production_module.ProductionWiringError(
                "--registry diverge de MODELS_CONFIG_PATH dans --env-file : "
                f"{requested_registry} contre {configured_registry}. La gateway vivante "
                "ne relirait pas le snapshot écrit par bootstrap-apply"
            )
    if needs_http and service_env_path is not None and runtime is not None:
        configured_binary = Path(effective_settings.llama_server_bin).absolute()
        published_binary = runtime.published_binary.absolute()
        if configured_binary != published_binary:
            raise production_module.ProductionWiringError(
                "LLAMA_SERVER_BIN dans --env-file diverge du runtime publié : "
                f"{configured_binary} contre {published_binary}. Corrigez le fichier et "
                "redémarrez la gateway avant --apply"
            )

    first_token_wiring = None
    warmup_wiring = None
    registry_sync_wiring = None
    if needs_http and base_url and admin_url:
        base_url, admin_url = production_module.validate_gateway_urls(
            base_url=base_url, admin_url=admin_url
        )
        secret = (
            "dry-run-secret-not-used"
            if dry_run
            else production_module.read_admin_secret(
                path=admin_secret_file,
                environ=admin_secret_environ,
            )
        )
        smoke_settings = _first_token.FirstTokenSettings(
            base_url=base_url,
            admin_url=admin_url,
            model_id=None,
            ttft_threshold_ms=ttft_threshold_ms,
            fail_on_ttft=ttft_gate,
            load_timeout_s=float(effective_settings.model_load_timeout_seconds + 10),
        )
        client = production_module.AsyncHttpClient()
        sync_timeout_seconds = float(
            effective_settings.admin_unload_drain_timeout_seconds + 10
        )
        live_registry = production_module.LiveRegistrySyncClient(
            admin_url=admin_url,
            admin_secret=secret,
            client=client,
            timeout_seconds=sync_timeout_seconds,
            lease_seconds=production_module.derive_live_registry_lease_seconds(
                smoke_settings,
                sync_timeout_seconds=sync_timeout_seconds,
            ),
        )
        registry_sync_wiring = applier_module.RegistrySyncWiring(
            activate=live_registry.activate,
            rollback=live_registry.rollback,
            confirm=live_registry.confirm,
        )
        first_token_wiring = applier_module.FirstTokenWiring(
            settings=smoke_settings,
            client=client,
            admin_secret=secret,
            sleep=asyncio.sleep,
        )
        if model_ids:
            warmup_settings = _warmup.WarmupSettings(
                admin_url=admin_url,
                # L'applicateur AUT-015 remplace cette cible par step.target.
                model_id=model_ids[0],
                timeout_seconds=_warmup.derive_warmup_timeout_seconds(
                    model_load_timeout_seconds=None,
                    default_load_timeout_seconds=effective_settings.model_load_timeout_seconds,
                ),
            )
            warmup_wiring = applier_module.WarmupWiring(
                settings=warmup_settings,
                client=client,
                admin_secret=secret,
                generation_probe_factory=production_module.generation_probe_factory_from_recipe(
                    settings=smoke_settings,
                    client=client,
                    admin_secret=secret,
                ),
                sleep=asyncio.sleep,
            )

    return applier_module.ApplierConfig(
        runtime=runtime,
        download=download,
        writer=writer,
        registry_sync=registry_sync_wiring,
        calibration=calibration_options,
        first_token=first_token_wiring,
        warmup=warmup_wiring,
    )


if __name__ == "__main__":
    app()
