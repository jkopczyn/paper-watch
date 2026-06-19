"""Persist Twitter handles into config.yaml.

The authoritative handle list comes from the AGI Safety Core X list, whose
members page requires an authenticated session. That extraction is an assisted,
one-time step (run via the web-browser skill); this module just merges the
resulting handles into the config.
"""

from __future__ import annotations

from pathlib import Path

import yaml


def merge_handles(config_path: str | Path, new_handles: list[str]) -> list[str]:
    """Union `new_handles` into config.yaml's handles list.

    Strips a leading '@', dedups, sorts, and preserves other config keys.
    Returns the handles that were newly added.
    """
    path = Path(config_path)
    data = yaml.safe_load(path.read_text()) or {}
    existing = set(data.get("handles") or [])

    cleaned = {h.lstrip("@").strip() for h in new_handles if h.strip()}
    added = sorted(cleaned - existing)

    data["handles"] = sorted(existing | cleaned)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return added
