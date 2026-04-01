"""
CLI d'administration — utilisation en ligne de commande sur le serveur.

Usage :
  python cli.py add-user alice --email alice@univ-pau.fr --rpm 30
  python cli.py create-key alice --name "these-2025"
  python cli.py revoke-key llmgw-abc12345
  python cli.py list-users
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


if __name__ == "__main__":
    app()
