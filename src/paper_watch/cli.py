"""paper-watch command-line interface.

Commands are wired incrementally as milestones land. M1 provides the skeleton:
`init` scaffolds a config file, and the other commands are registered stubs.
"""

from __future__ import annotations

from pathlib import Path

import click

from paper_watch import __version__

EXAMPLE_CONFIG = """\
# paper-watch configuration. Secrets go in .env, not here.
db_path: paper_watch.db
top_n: 15
lookback: 7d
candidate_window_days: 7
resurface_window_days: 21

schedule:        # local run times the cron installer reads
  - "08:00"
  - "16:00"

authors: []      # arXiv author names (replaces Google Scholar alerts)
feeds: []        # - {name: ML Safety, url: https://newsletter.mlsafety.org/feed}
handles: []      # Twitter usernames (seeded via `paper-watch seed-handles`)

nitter_instances:
  - https://nitter.net

slack:           # #papers channels; see README. Fill ids via `paper-watch slack-channels`.
  workspaces: [] # - {name: mats, token_env: SLACK_TOKEN_MATS, channels: [{id: C0, name: papers}]}

scoring:
  overlap: 1.0
  velocity: 1.0
  feedback: 1.0
  resurface_boost: 2.0

smtp:
  host: smtp.gmail.com
  port: 587
  username: ""
  from_addr: ""
  to_addr: ""

llm:
  model: claude-haiku-4-5   # or claude-opus-4-8 for higher-quality enrichment
  max_enrich_per_run: 50
"""


@click.group()
@click.version_option(__version__)
def cli() -> None:
    """Scan AI-safety paper sources and email a ranked digest."""


@cli.command()
@click.option(
    "--path",
    type=click.Path(dir_okay=False, path_type=Path),
    default="config.yaml",
    show_default=True,
    help="Where to write the config file.",
)
@click.option("--force", is_flag=True, help="Overwrite an existing config.")
def init(path: Path, force: bool) -> None:
    """Write an example config file to get started."""
    if path.exists() and not force:
        raise click.ClickException(f"{path} already exists (use --force).")
    path.write_text(EXAMPLE_CONFIG)
    click.echo(f"Wrote {path}")


@cli.command()
@click.option("--config", "config_path", default="config.yaml", show_default=True)
@click.option("--dry-run", is_flag=True, help="Render the digest but don't send.")
@click.option("--since", default=None, help="Override lookback window, e.g. 7d.")
def run(config_path: str, dry_run: bool, since: str | None) -> None:
    """Fetch, score, and send (or render) the digest."""
    from paper_watch import runtime

    result = runtime.run(config_path, dry_run=dry_run, since=since)
    click.echo(
        f"Ingested {result.new_count} new, enriched {result.enriched_count}, "
        f"selected {len(result.chosen_ids)} for the digest."
    )
    if result.digest_path is not None:
        click.echo(f"Dry run: wrote {result.digest_path}")
    elif result.sent:
        click.echo("Digest sent.")
    else:
        click.echo("Nothing to send.")


@cli.command()
@click.option("--config", "config_path", default="config.yaml", show_default=True)
def sources(config_path: str) -> None:
    """List configured sources."""
    from paper_watch.config import Config

    cfg = Config.load(config_path)
    click.echo(f"arXiv authors: {len(cfg.authors)}")
    click.echo(f"RSS feeds:     {len(cfg.feeds)}")
    click.echo(f"Twitter handles: {len(cfg.handles)} (via {len(cfg.nitter_instances)} nitter instance(s))")
    workspaces = cfg.slack.workspaces if cfg.slack else []
    n_channels = sum(len(w.channels) for w in workspaces)
    click.echo(f"Slack:         {len(workspaces)} workspace(s), {n_channels} channel(s)")


@cli.command("slack-channels")
@click.option("--config", "config_path", default="config.yaml", show_default=True)
@click.option(
    "--workspace", required=True, help="Workspace `name` from config.slack.workspaces."
)
def slack_channels(config_path: str, workspace: str) -> None:
    """List channel ids + names for a Slack workspace, to fill in config.

    Uses the workspace's user token (from the env var named by `token_env`) and
    `conversations.list`. Copy the relevant ids into `config.yaml`.
    """
    import os

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    from paper_watch.config import Config
    from paper_watch.sources.slack import list_channels

    cfg = Config.load(config_path)
    workspaces = cfg.slack.workspaces if cfg.slack else []
    ws = next((w for w in workspaces if w.name == workspace), None)
    if ws is None:
        raise click.ClickException(
            f"workspace {workspace!r} not in config.slack.workspaces"
        )
    token = os.environ.get(ws.token_env)
    if not token:
        raise click.ClickException(f"no token in env var {ws.token_env}")

    channels = list_channels(token)
    for ch in channels:
        click.echo(f"{ch['id']}\t{ch['name']}")
    click.echo(f"{len(channels)} channel(s)")


@cli.group()
def feedback() -> None:
    """Export/import reading-group feedback."""


@feedback.command("export")
@click.option("--config", "config_path", default="config.yaml", show_default=True)
@click.option("--since", default="7d", show_default=True, help="Lookback window, e.g. 7d.")
@click.option(
    "--out",
    type=click.Path(dir_okay=False, path_type=Path),
    default="candidates.csv",
    show_default=True,
)
def feedback_export(config_path: str, since: str, out: Path) -> None:
    """Write recently-shown papers to a CSV to fill in picks + 1-5 ratings."""
    from paper_watch.config import Config
    from paper_watch.dates import since_to_iso
    from paper_watch.feedback import export_candidates
    from paper_watch.store import Store

    cfg = Config.load(config_path)
    store = Store(cfg.db_path)
    try:
        n = export_candidates(store, since=since_to_iso(since), path=out)
    finally:
        store.close()
    click.echo(f"Wrote {n} candidate(s) to {out}")


@feedback.command("import")
@click.option("--config", "config_path", default="config.yaml", show_default=True)
@click.option(
    "--file",
    "in_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default="candidates.csv",
    show_default=True,
)
@click.option("--week", default=None, help="Week label (default: current ISO week).")
def feedback_import(config_path: str, in_file: Path, week: str | None) -> None:
    """Import a filled candidates CSV and update feedback weights."""
    from datetime import date

    from paper_watch.config import Config
    from paper_watch.feedback import import_feedback
    from paper_watch.store import Store

    if week is None:
        iso = date.today().isocalendar()
        week = f"{iso.year}-W{iso.week:02d}"

    cfg = Config.load(config_path)
    store = Store(cfg.db_path)
    try:
        n = import_feedback(store, path=in_file, week=week)
    finally:
        store.close()
    click.echo(f"Imported {n} feedback row(s) for {week}")


@cli.command("seed-handles")
@click.option("--config", "config_path", default="config.yaml", show_default=True)
@click.option(
    "--from-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Newline-separated handles (e.g. extracted from the AGI Safety Core list).",
)
@click.option("--handle", "handles", multiple=True, help="Add a single handle (repeatable).")
def seed_handles(config_path: str, from_file: Path | None, handles: tuple[str, ...]) -> None:
    """Merge Twitter handles into the config.

    The AGI Safety Core list members page needs an authenticated session, so
    extract handles with the web-browser skill into a file, then:
    `paper-watch seed-handles --from-file handles.txt`
    """
    from paper_watch.handles import merge_handles

    collected = list(handles)
    if from_file is not None:
        collected += [
            line.strip() for line in from_file.read_text().splitlines() if line.strip()
        ]
    if not collected:
        raise click.ClickException("provide --from-file and/or --handle")

    added = merge_handles(config_path, collected)
    if added:
        click.echo(f"Added {len(added)} handle(s): {', '.join(added)}")
    else:
        click.echo("No new handles (all already present).")


if __name__ == "__main__":
    cli()
