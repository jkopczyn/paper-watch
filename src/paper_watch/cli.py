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
resurface_window_days: 21

schedule:        # local run times the cron installer reads
  - "08:00"
  - "16:00"

authors: []      # arXiv author names (replaces Google Scholar alerts)
feeds: []        # - {name: ML Safety, url: https://newsletter.mlsafety.org/feed}
handles: []      # Twitter usernames (seeded via `paper-watch seed-handles`)

nitter_instances:
  - https://nitter.net

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
    """Fetch, score, and send the digest. (Wired in M10.)"""
    raise click.ClickException("run: not implemented yet (M10)")


@cli.command()
@click.option("--config", "config_path", default="config.yaml", show_default=True)
def sources(config_path: str) -> None:
    """List configured sources. (Wired in M3-M5.)"""
    raise click.ClickException("sources: not implemented yet")


@cli.group()
def feedback() -> None:
    """Export/import reading-group feedback. (Wired in M9.)"""


@feedback.command("export")
def feedback_export() -> None:
    raise click.ClickException("feedback export: not implemented yet (M9)")


@feedback.command("import")
def feedback_import() -> None:
    raise click.ClickException("feedback import: not implemented yet (M9)")


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
