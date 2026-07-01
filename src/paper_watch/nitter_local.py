"""Ensure the self-hosted (local) Nitter instance is up before a real run.

The Twitter source reads per-handle RSS from a Nitter instance. Public instances
are effectively dead, so `config.nitter_instances` normally lists a locally
hosted one (`http://localhost:8080`, run via `deploy/nitter/docker-compose.yml`).

`ensure_local_nitter` checks that local instance and, on a real run, tries to
start it if it's down; on a dry run it just warns and proceeds. All the moving
parts (reachability probe, start command, sleep) are injectable so the policy is
testable without Docker or the network.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# How long docker's `up -d` returns before Nitter actually serves; wait before
# each reachability re-check, and how many start attempts before giving up.
DEFAULT_DELAY = 5.0
DEFAULT_ATTEMPTS = 3


def _is_local(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() in _LOCAL_HOSTS


def is_reachable(url: str, *, timeout: float = 3.0) -> bool:
    """True if `url` answers at all (any HTTP status = the server is listening)."""
    try:
        httpx.get(url, timeout=timeout, follow_redirects=True)
        return True
    except httpx.HTTPError:
        return False


def default_compose_file() -> Path:
    """`deploy/nitter/docker-compose.yml` relative to the repo root."""
    return Path(__file__).resolve().parents[2] / "deploy" / "nitter" / "docker-compose.yml"


def start_local_nitter(compose_file: Path) -> bool:
    """Run `docker compose up -d` for the local Nitter; True if the command ran ok.

    A True return only means Docker accepted the command, not that Nitter is
    serving yet — the caller re-probes reachability after a delay.
    """
    try:
        proc = subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "up", "-d"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        log.error("docker not found on PATH; cannot start local Nitter")
        return False
    if proc.returncode != 0:
        log.error("docker compose up failed: %s", (proc.stderr or proc.stdout).strip())
        return False
    return True


def ensure_local_nitter(
    instances: list[str],
    *,
    dry_run: bool,
    reachable: Callable[[str], bool] = is_reachable,
    start: Callable[[Path], bool] = start_local_nitter,
    sleep: Callable[[float], None] = time.sleep,
    compose_file: Path | None = None,
    attempts: int = DEFAULT_ATTEMPTS,
    delay: float = DEFAULT_DELAY,
) -> list[str]:
    """Return the Nitter instance list to actually use for this run.

    If the list has no localhost entry (nothing local to manage), or the local
    one is already reachable, the list is returned unchanged. Otherwise:

    - dry run: warn and proceed with the list unchanged (the local instance will
      just fail per-handle and fall back to any public instances).
    - real run: try to start the local instance, retrying up to `attempts` times
      with `delay` seconds between tries. If it never comes up, drop the local
      instance(s) and return the rest so the run continues without it.
    """
    target = next((u for u in instances if _is_local(u)), None)
    if target is None:
        return list(instances)  # no localhost entry -> nothing local to manage

    if reachable(target):
        log.info("Local Nitter is up at %s", target)
        return list(instances)

    if dry_run:
        log.warning(
            "Local Nitter (%s) is not reachable; dry run proceeding without it.",
            target,
        )
        return list(instances)

    compose_file = compose_file or default_compose_file()
    for attempt in range(1, attempts + 1):
        log.warning(
            "Local Nitter (%s) not reachable; starting it (attempt %d/%d).",
            target,
            attempt,
            attempts,
        )
        started = start(compose_file)
        sleep(delay)
        if started and reachable(target):
            log.info("Local Nitter is up at %s", target)
            return list(instances)
        log.warning("Local Nitter did not come up (attempt %d/%d).", attempt, attempts)

    log.error(
        "Local Nitter failed to start after %d attempts; running without it.", attempts
    )
    return [u for u in instances if not _is_local(u)]
