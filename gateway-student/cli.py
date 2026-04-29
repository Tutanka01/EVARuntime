"""CLI admin pour la gateway étudiante EVA.

Utilisation rapide :
  python cli.py --help
  python cli.py stats
  python cli.py list-students
  python cli.py add-student alice --email alice@univ-pau.fr
  python cli.py create-key alice --expires-at 2026-08-31T23:59:59+00:00
  python cli.py usage-report --days 7
  python cli.py expiring-keys --days 30
  python cli.py set-quota alice --rpm-limit 20
  python cli.py deactivate-student alice
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import database as db


app = typer.Typer(
    help="Gestion admin de la gateway étudiante EVA.",
    no_args_is_help=True,
)
console = Console()
err_console = Console(stderr=True, style="bold red")


# ---------------------------------------------------------------------------
# Helpers affichage
# ---------------------------------------------------------------------------

def _ago(ts_str: str | None) -> str:
    """Transforme un timestamp ISO en 'il y a Xmin' etc."""
    if not ts_str:
        return "[dim]jamais[/]"
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = int((datetime.now(timezone.utc) - dt).total_seconds())
        if secs < 0:
            return "[dim]futur[/]"
        if secs < 60:
            return f"il y a {secs}s"
        if secs < 3600:
            return f"il y a {secs // 60}min"
        if secs < 86400:
            return f"il y a {secs // 3600}h"
        return f"il y a {secs // 86400}j"
    except Exception:
        return ts_str or ""


def _days_until(expires_str: str) -> int:
    try:
        dt = datetime.fromisoformat(expires_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(-1, (dt - datetime.now(timezone.utc)).days)
    except Exception:
        return -999


def _expiry_style(days: int) -> str:
    if days < 0:
        return "red"
    if days <= 7:
        return "bold red"
    if days <= 30:
        return "yellow"
    return "green"


def _active_mark(is_active: int | bool) -> str:
    return "[green]✓[/]" if is_active else "[red]✗[/]"


def _fmt_n(n: int | None) -> str:
    if n is None:
        return "—"
    return f"{int(n):,}"


def _abort(msg: str) -> None:
    err_console.print(f"Erreur : {msg}")
    raise SystemExit(1)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Commandes — base
# ---------------------------------------------------------------------------

@app.command()
def init_db() -> None:
    """Initialise (ou migre) la base de données étudiante."""
    _run(db.init_db())
    console.print("[green]✓[/] Base étudiante initialisée.")


# ---------------------------------------------------------------------------
# Commandes — étudiants
# ---------------------------------------------------------------------------

@app.command()
def add_student(
    username: str = typer.Argument(..., help="Identifiant unique (ex: nom.prénom)"),
    email: str | None = typer.Option(None, "--email", "-e", help="Adresse email UPPA"),
    rpm_limit: int | None = typer.Option(None, "--rpm", help="Requêtes par minute (défaut config)"),
    daily_token_limit: int | None = typer.Option(None, "--daily-tokens", help="Tokens/jour (défaut config)"),
    hourly_token_limit: int | None = typer.Option(None, "--hourly-tokens", help="Tokens/heure, 0=désactivé"),
    concurrent: int | None = typer.Option(None, "--concurrent", help="Streams simultanés (défaut config)"),
    notes: str | None = typer.Option(None, "--notes", help="Notes libres (ex: TER 2026)"),
) -> None:
    """Crée un nouvel étudiant dans la base."""

    async def run() -> None:
        await db.init_db()
        user = await db.create_user(
            username, email,
            rpm_limit=rpm_limit,
            daily_token_limit=daily_token_limit,
            hourly_token_limit=hourly_token_limit,
            concurrent_stream_limit=concurrent,
            notes=notes,
        )
        console.print(f"[green]✓[/] Étudiant créé — id=[cyan]{user['id']}[/] username=[cyan]{user['username']}[/]")

    _run(run())


@app.command()
def list_students() -> None:
    """Liste tous les étudiants avec leurs quotas et dernière activité."""

    async def run() -> list[dict]:
        await db.init_db()
        return await db.list_users()

    users = _run(run())
    if not users:
        console.print("[dim]Aucun étudiant enregistré.[/]")
        return

    t = Table(
        box=box.ROUNDED, show_header=True, header_style="bold cyan",
        title="[bold]Étudiants[/]",
    )
    t.add_column("ID", justify="right", style="dim", no_wrap=True)
    t.add_column("Utilisateur", min_width=12)
    t.add_column("Email", min_width=20)
    t.add_column("Actif", justify="center")
    t.add_column("RPM", justify="right")
    t.add_column("Tok/h", justify="right")
    t.add_column("Tok/j", justify="right")
    t.add_column("Conc.", justify="right")
    t.add_column("Clés actives", justify="right")
    t.add_column("Dernière requête")
    t.add_column("Notes", style="dim")

    for u in users:
        hourly = int(u.get("hourly_token_limit") or 0)
        t.add_row(
            str(u["id"]),
            u["username"],
            u.get("email") or "[dim]—[/]",
            _active_mark(u["is_active"]),
            str(u["rpm_limit"]),
            _fmt_n(hourly) if hourly else "[dim]off[/]",
            _fmt_n(u["daily_token_limit"]),
            str(u["concurrent_stream_limit"]),
            str(u.get("key_count") or 0),
            _ago(u.get("last_api_call")),
            u.get("notes") or "",
        )
    console.print(t)


@app.command()
def deactivate_student(
    username: str = typer.Argument(..., help="Nom d'utilisateur à suspendre"),
) -> None:
    """Suspend un étudiant (sans supprimer ses données). Toutes ses clés sont rejetées."""

    async def run() -> None:
        await db.init_db()
        user = await db.get_user_by_username(username)
        if not user:
            _abort(f"Étudiant introuvable : {username}")
        if not user["is_active"]:
            console.print(f"[yellow]⚠[/] {username} est déjà suspendu.")
            return
        await db.set_user_active(user["id"], False)
        console.print(f"[green]✓[/] [bold]{username}[/] suspendu. Ses requêtes seront rejetées immédiatement.")

    _run(run())


@app.command()
def activate_student(
    username: str = typer.Argument(..., help="Nom d'utilisateur à réactiver"),
) -> None:
    """Réactive un étudiant suspendu."""

    async def run() -> None:
        await db.init_db()
        user = await db.get_user_by_username(username)
        if not user:
            _abort(f"Étudiant introuvable : {username}")
        if user["is_active"]:
            console.print(f"[yellow]⚠[/] {username} est déjà actif.")
            return
        await db.set_user_active(user["id"], True)
        console.print(f"[green]✓[/] [bold]{username}[/] réactivé.")

    _run(run())


@app.command()
def delete_student(
    username: str = typer.Argument(..., help="Nom d'utilisateur à supprimer définitivement"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirmer sans demander"),
) -> None:
    """Supprime un étudiant et toutes ses données (RGPD — irréversible)."""

    async def run() -> None:
        await db.init_db()
        user = await db.get_user_by_username(username)
        if not user:
            _abort(f"Étudiant introuvable : {username}")

        if not yes:
            confirm = typer.confirm(
                f"Supprimer définitivement {username} (clés + logs) ? Cette opération est irréversible.",
                default=False,
            )
            if not confirm:
                console.print("[dim]Annulé.[/]")
                return

        await db.delete_user(user["id"])
        console.print(f"[green]✓[/] [bold]{username}[/] supprimé (clés et logs inclus).")

    _run(run())


@app.command()
def set_quota(
    username: str = typer.Argument(..., help="Nom d'utilisateur"),
    rpm_limit: int | None = typer.Option(None, "--rpm", help="Nouvelles requêtes par minute"),
    daily_token_limit: int | None = typer.Option(None, "--daily-tokens", help="Nouveau quota tokens/jour"),
    hourly_token_limit: int | None = typer.Option(None, "--hourly-tokens", help="Nouveau quota tokens/heure (0=off)"),
    concurrent: int | None = typer.Option(None, "--concurrent", help="Nouveaux streams simultanés"),
) -> None:
    """Modifie les quotas d'un étudiant sans recréer son compte."""

    async def run() -> None:
        await db.init_db()
        user = await db.get_user_by_username(username)
        if not user:
            _abort(f"Étudiant introuvable : {username}")

        changes = {
            "RPM": (user["rpm_limit"], rpm_limit),
            "Tokens/heure": (user.get("hourly_token_limit", 0), hourly_token_limit),
            "Tokens/jour": (user["daily_token_limit"], daily_token_limit),
            "Streams concurrents": (user["concurrent_stream_limit"], concurrent),
        }

        if not any(v is not None for _, v in changes.values()):
            console.print("[yellow]⚠[/] Aucun quota spécifié — rien à modifier.")
            return

        updated = await db.update_user_quotas(
            user["id"],
            rpm_limit=rpm_limit,
            daily_token_limit=daily_token_limit,
            hourly_token_limit=hourly_token_limit,
            concurrent_stream_limit=concurrent,
        )

        t = Table(box=box.SIMPLE, show_header=True, header_style="bold dim")
        t.add_column("Quota")
        t.add_column("Avant", justify="right")
        t.add_column("→", justify="center", style="dim")
        t.add_column("Après", justify="right", style="cyan")

        field_map = {
            "RPM": "rpm_limit",
            "Tokens/heure": "hourly_token_limit",
            "Tokens/jour": "daily_token_limit",
            "Streams concurrents": "concurrent_stream_limit",
        }
        for label, key in field_map.items():
            before = user.get(key, 0)
            after = updated.get(key, 0)
            if before != after:
                t.add_row(label, _fmt_n(before), "→", _fmt_n(after))

        console.print(f"\n[green]✓[/] Quotas mis à jour pour [bold]{username}[/]")
        console.print(t)

    _run(run())


# ---------------------------------------------------------------------------
# Commandes — clés API
# ---------------------------------------------------------------------------

@app.command()
def create_key(
    username: str = typer.Argument(..., help="Étudiant pour qui créer la clé"),
    expires_at: str = typer.Argument(..., help="Date d'expiration ISO 8601 (ex: 2026-08-31T23:59:59+00:00)"),
    name: str | None = typer.Option(None, "--name", "-n", help="Nom lisible (ex: TP-S2-2026)"),
) -> None:
    """Génère une nouvelle clé API pour un étudiant."""

    async def run() -> None:
        await db.init_db()
        # Valider le format de la date
        try:
            dt = datetime.fromisoformat(expires_at)
            if dt.tzinfo is None:
                _abort("expires-at doit inclure un timezone (ex: ...+00:00). Conseil : 2026-08-31T23:59:59+00:00")
            if dt < datetime.now(timezone.utc):
                _abort(f"La date d'expiration {expires_at} est déjà passée.")
        except ValueError:
            _abort(f"Format de date invalide : {expires_at}. Attendu : YYYY-MM-DDTHH:MM:SS+00:00")

        user = await db.get_user_by_username(username)
        if not user:
            _abort(f"Étudiant introuvable : {username}")

        raw_key, key = await db.create_api_key(user["id"], name, expires_at)

        days = _days_until(expires_at)
        console.print(Panel(
            f"[bold]Nouvelle clé pour [cyan]{username}[/][/]\n\n"
            f"  Préfixe  : [dim]{key['key_prefix']}[/]\n"
            f"  Expire   : [cyan]{expires_at}[/] [dim]({days} jours)[/]\n"
            f"  Nom      : [dim]{name or '—'}[/]\n\n"
            f"[bold yellow]Clé API (copier maintenant, non récupérable) :[/]\n\n"
            f"  [bold cyan]{raw_key}[/]",
            title="Clé créée",
            expand=False,
        ))

    _run(run())


@app.command()
def list_keys(
    username: str | None = typer.Option(None, "--user", "-u", help="Filtrer par étudiant"),
) -> None:
    """Affiche toutes les clés API (ou celles d'un étudiant donné)."""

    async def run() -> list[dict]:
        await db.init_db()
        if username:
            user = await db.get_user_by_username(username)
            if not user:
                _abort(f"Étudiant introuvable : {username}")
            return await db.get_user_keys(user["id"])
        return await db.get_all_keys_overview()

    keys = _run(run())
    if not keys:
        console.print("[dim]Aucune clé trouvée.[/]")
        return

    t = Table(
        box=box.ROUNDED, show_header=True, header_style="bold cyan",
        title=f"[bold]Clés API{' — ' + username if username else ''}[/]",
    )
    t.add_column("ID", justify="right", style="dim")
    if not username:
        t.add_column("Étudiant")
    t.add_column("Préfixe")
    t.add_column("Nom", style="dim")
    t.add_column("Statut", justify="center")
    t.add_column("Expiration")
    t.add_column("Reste", justify="right")
    t.add_column("Créée le")
    t.add_column("Dernière utilisation")

    now = datetime.now(timezone.utc)
    for k in keys:
        days = _days_until(k["expires_at"])
        style = _expiry_style(days) if k["is_active"] else "dim"
        active_str = _active_mark(k["is_active"])
        if not k["is_active"]:
            active_str = "[dim]révoquée[/]"
        elif days < 0:
            active_str = "[red]expirée[/]"

        expiry_text = Text(k["expires_at"][:10], style=style)
        remain_text = Text(
            f"{days}j" if days >= 0 else "exp.",
            style=_expiry_style(days) if k["is_active"] else "dim",
        )

        row = [
            str(k["id"]),
            k["key_prefix"],
            k.get("name") or "[dim]—[/]",
            active_str,
            expiry_text,
            remain_text,
            (k.get("created_at") or "")[:10],
            _ago(k.get("last_used")),
        ]
        if not username:
            row.insert(1, k.get("username", "—"))
        t.add_row(*row)

    console.print(t)


@app.command()
def revoke_key(
    key_prefix: str = typer.Argument(..., help="Préfixe de la clé à révoquer (ex: llmstu-abc12)"),
) -> None:
    """Révoque une clé API immédiatement (toute requête en cours sera terminée)."""

    async def run() -> None:
        await db.init_db()
        ok = await db.revoke_key(key_prefix)
        if ok:
            console.print(f"[green]✓[/] Clé [bold]{key_prefix}[/] révoquée.")
        else:
            console.print(f"[yellow]⚠[/] Aucune clé active trouvée pour le préfixe [bold]{key_prefix}[/].")

    _run(run())


@app.command()
def expiring_keys(
    days: int = typer.Option(30, "--days", "-d", help="Fenêtre en jours"),
) -> None:
    """Liste les clés actives qui expirent dans les N prochains jours."""

    async def run() -> list[dict]:
        await db.init_db()
        return await db.get_expiring_keys(within_days=days)

    keys = _run(run())
    if not keys:
        console.print(f"[green]✓[/] Aucune clé n'expire dans les {days} prochains jours.")
        return

    console.print(f"\n[bold yellow]⚠  {len(keys)} clé(s) expirant dans les {days} prochains jours[/]\n")

    t = Table(box=box.ROUNDED, show_header=True, header_style="bold yellow")
    t.add_column("Étudiant")
    t.add_column("Email", style="dim")
    t.add_column("Préfixe")
    t.add_column("Nom", style="dim")
    t.add_column("Expire le")
    t.add_column("Restant", justify="right")

    for k in keys:
        d = _days_until(k["expires_at"])
        style = _expiry_style(d)
        t.add_row(
            k["username"],
            k.get("email") or "—",
            k["key_prefix"],
            k.get("name") or "—",
            Text(k["expires_at"][:10], style=style),
            Text(f"{d}j", style=style),
        )
    console.print(t)
    console.print(f"\n[dim]Conseil : python cli.py create-key <username> --expires-at YYYY-MM-DDTHH:MM:SS+00:00[/]")


# ---------------------------------------------------------------------------
# Commandes — statistiques & rapports
# ---------------------------------------------------------------------------

@app.command()
def stats() -> None:
    """Tableau de bord temps réel : requêtes du jour, tokens, modèles, top étudiants."""

    data = _run(_get_stats())
    today = data["today"]
    week = data["week"]
    now_str = datetime.now().strftime("%Y-%m-%d  %H:%M")

    console.print()
    console.print(Panel(
        f"[bold cyan]LLM Gateway Student — Tableau de bord[/]\n[dim]{now_str}[/]",
        expand=False,
    ))

    # Aujourd'hui
    console.print("\n[bold]Aujourd'hui[/]")
    console.print(f"  Requêtes         : [cyan]{_fmt_n(today.get('requests', 0))}[/]")
    console.print(
        f"  Tokens total     : [cyan]{_fmt_n(today.get('total_tokens', 0))}[/]"
        f"  [dim](prompt: {_fmt_n(today.get('prompt_tokens', 0))} | "
        f"génération: {_fmt_n(today.get('completion_tokens', 0))})[/]"
    )
    console.print(f"  Durée moy.       : [cyan]{today.get('avg_duration_ms', 0):,} ms[/]")
    console.print(
        f"  Étudiants actifs : [cyan]{today.get('active_users', 0)}[/]"
        f" / {data['total_active_users']} inscrits"
    )

    # 7 jours
    console.print("\n[bold]7 derniers jours[/]")
    console.print(f"  Requêtes : [cyan]{_fmt_n(week.get('requests', 0))}[/]")
    console.print(f"  Tokens   : [cyan]{_fmt_n(week.get('total_tokens', 0))}[/]")
    console.print(f"  Étudiants uniques : [cyan]{week.get('active_users', 0)}[/]")

    # Modèles aujourd'hui
    if data["models_today"]:
        console.print()
        t = Table(
            box=box.SIMPLE, show_header=True, header_style="bold dim",
            title="[bold]Modèles — aujourd'hui[/]",
        )
        t.add_column("Modèle")
        t.add_column("Requêtes", justify="right")
        t.add_column("Tokens", justify="right")
        for m in data["models_today"]:
            t.add_row(m["model"], _fmt_n(m["requests"]), _fmt_n(m["total_tokens"]))
        console.print(t)
    else:
        console.print("\n[dim]Aucune requête enregistrée aujourd'hui.[/]")

    console.print()


async def _get_stats() -> dict:
    await db.init_db()
    return await db.get_global_stats()


@app.command()
def usage_report(
    days: int = typer.Option(7, "--days", "-d", help="Nombre de jours à analyser"),
    user: str | None = typer.Option(None, "--user", "-u", help="Filtrer sur un étudiant"),
    top: int = typer.Option(20, "--top", help="Nombre max de lignes"),
) -> None:
    """Rapport d'utilisation par étudiant sur N jours (classé par tokens)."""

    async def run() -> list[dict]:
        await db.init_db()
        user_id = None
        if user:
            u = await db.get_user_by_username(user)
            if not u:
                _abort(f"Étudiant introuvable : {user}")
            user_id = u["id"]
        return await db.get_usage_report(days=days, user_id=user_id)

    rows = _run(run())
    if not rows:
        console.print(f"[dim]Aucune activité sur les {days} derniers jours.[/]")
        return

    title = f"[bold]Utilisation — {days} derniers jours"
    if user:
        title += f" — {user}"
    title += "[/]"

    t = Table(box=box.ROUNDED, show_header=True, header_style="bold cyan", title=title)
    t.add_column("Rang", justify="right", style="dim")
    t.add_column("Étudiant", min_width=12)
    t.add_column("Email", style="dim")
    t.add_column("Requêtes", justify="right")
    t.add_column("Tokens total", justify="right")
    t.add_column("  Prompt", justify="right", style="dim")
    t.add_column("  Génération", justify="right", style="dim")
    t.add_column("Durée moy.", justify="right")
    t.add_column("Dernière req.")

    for i, row in enumerate(rows[:top], 1):
        avg_ms = int(row.get("avg_duration_ms") or 0)
        avg_str = f"{avg_ms / 1000:.1f}s" if avg_ms else "—"
        t.add_row(
            str(i),
            row["username"],
            row.get("email") or "—",
            _fmt_n(row["request_count"]),
            f"[cyan]{_fmt_n(row['total_tokens'])}[/]",
            _fmt_n(row["prompt_tokens"]),
            _fmt_n(row["completion_tokens"]),
            avg_str,
            _ago(row.get("last_seen")),
        )

    console.print(t)

    # Totaux
    total_req = sum(r["request_count"] for r in rows)
    total_tok = sum(r["total_tokens"] for r in rows)
    console.print(
        f"\n[dim]Total : {_fmt_n(total_req)} requêtes | {_fmt_n(total_tok)} tokens"
        f" | {len(rows)} étudiants actifs[/]"
    )


if __name__ == "__main__":
    app()
