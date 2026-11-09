"""The public site: every paper paper-watch has put in a digest, with the
reading group's ratings where they exist.

The site is built from the `shown` table, so a paper that never appeared in a
digest never appears here. The internal `score`, the `why` text, the stored
`abstract`, source labels, and everything in `mentions` are never published,
and no field identifies an individual voter: the store holds only per-paper
aggregates. Only http and https links are published; a paper whose first
display link is anything else is shown without a link.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import signal
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment

from paper_watch.config import SiteConfig
from paper_watch.runtime import _prefer_alignmentforum, _pub_display

_log = logging.getLogger(__name__)

_ALLOWED_SCHEMES = frozenset({"http", "https"})


def public_url(url: str | None) -> str | None:
    """`url` if it is a plain http or https link, otherwise None.

    This is the gate on everything that reaches an `href`. It rejects
    `javascript:`, `data:` and `slack://` links, protocol-relative URLs, URLs
    with no host, and any string carrying whitespace or a control character.
    The accepted string is returned unchanged, so the published link is the
    one the digest email shows.
    """
    if not url:
        return None
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        return None
    try:
        host = parts.hostname
    except ValueError:
        return None
    if not host:
        return None
    return url


@dataclass(frozen=True)
class SiteRecord:
    """One appearance of one paper in one digest.

    `entry_id` is internal: it is how the renderers tell two appearances of the
    same paper apart, and `to_json` never serializes it.
    """

    entry_id: int
    digest_at: str
    rank: int | None
    resurfaced: bool
    title: str
    authors: list[str]
    url: str | None
    arxiv_id: str | None
    published: str
    is_estimate: bool
    tags: list[str]
    tldr: str | None
    group_rating: int | None
    picked: bool
    read: bool

    def to_json(self) -> dict:
        """The published form of this record. The key set here is the public
        contract of `papers.json`."""
        return {
            "digest_at": self.digest_at,
            "rank": self.rank,
            "resurfaced": self.resurfaced,
            "title": self.title,
            "authors": list(self.authors),
            "url": self.url,
            "arxiv_id": self.arxiv_id,
            "published": self.published,
            "is_estimate": self.is_estimate,
            "tags": list(self.tags),
            "tldr": self.tldr,
            "group_rating": self.group_rating,
            "picked": self.picked,
            "read": self.read,
        }


def entry_link(store, entry_id: int, links: dict[str, str]) -> str | None:
    """The one link the site shows for an entry, or None.

    Only links the entry owns are used. When the entry has stored links, the
    first one is taken after the same alignmentforum.org preference the digest
    email applies, and it is published only if it passes `public_url`. When it
    has none, the first http or https URL the entry answers to is used. The
    email's last fallback, a mention's URL, is never used here, because a
    mention URL points at where the paper was discussed, which is often a
    private Slack message.
    """
    if links:
        preferred = _prefer_alignmentforum(store, entry_id, links)
        return public_url(next(iter(preferred.values()), None))
    for url in store.get_entry_urls(entry_id):
        if public_url(url):
            return url
    return None


def load_records(store) -> list[SiteRecord]:
    """One record per `shown` row, newest digest first and in rank order.

    The publication date comes from the same helper the digest email uses, so
    the site and the email agree on it. The link is chosen by `entry_link`.
    """
    records: list[SiteRecord] = []
    for row in store.shown_history():
        entry_id = int(row["id"])
        link = entry_link(store, entry_id, json.loads(row["links_json"]))
        published, is_estimate = _pub_display(store, row)
        records.append(
            SiteRecord(
                entry_id=entry_id,
                digest_at=row["digest_at"],
                rank=row["rank"],
                resurfaced=bool(row["resurfaced"]),
                title=row["title"],
                authors=json.loads(row["authors_json"]),
                url=link,
                arxiv_id=row["arxiv_id"],
                published=published,
                is_estimate=is_estimate,
                tags=json.loads(row["tags_json"]),
                tldr=row["tldr"],
                group_rating=row["latest_rating"],
                picked=bool(row["picked"]),
                read=bool(row["read"]),
            )
        )
    return records


def group_by_digest(records: list[SiteRecord]) -> list[tuple[str, list[SiteRecord]]]:
    """`(digest_at, records)` for each digest, keeping the query's order: newest
    digest first, and within a digest the order the email used."""
    groups: list[tuple[str, list[SiteRecord]]] = []
    for record in records:
        if groups and groups[-1][0] == record.digest_at:
            groups[-1][1].append(record)
        else:
            groups.append((record.digest_at, [record]))
    return groups


@dataclass(frozen=True)
class TableRow:
    """One paper in the single table, under the first digest it appeared in."""

    record: SiteRecord
    first_digest_at: str
    times_shown: int


def table_rows(records: list[SiteRecord]) -> list[TableRow]:
    """One row per paper rather than per appearance.

    `record` is the paper's first appearance, so its rank is the rank it had in
    the digest the row is filed under, and `times_shown` counts every
    appearance including that first one. Rows keep the order of the first
    appearances, which is by digest newest first and then by rank.
    """
    first_seen: dict[int, SiteRecord] = {}
    counts: dict[int, int] = {}
    for record in records:
        counts[record.entry_id] = counts.get(record.entry_id, 0) + 1
        # Records are newest first, so the last one seen is the earliest digest.
        first_seen[record.entry_id] = record

    rows = [
        TableRow(
            record=first_seen[record.entry_id],
            first_digest_at=first_seen[record.entry_id].digest_at,
            times_shown=counts[record.entry_id],
        )
        for record in records
        if record is first_seen[record.entry_id]
    ]
    return rows


# The name and the one-sentence description shown at the top of the index.
# Both are placeholders for the user to confirm, kept here so that the wording
# can be changed in one place.
SITE_TITLE = "paper-watch"
SITE_DESCRIPTION = (
    "Papers suggested by paper-watch, a scan of new AI safety research, with "
    "the reading group's ratings where they exist."
)

# Labels on content that a reader could otherwise mistake for the group's own
# words or for a rating of our own.
TLDR_LABEL = "AI summary"
RATING_FORMAT = "group rating {n}/5"
RATINGS_NOTE = "Ratings come from the reading group's weekly poll."

# Every file publish writes into the site repo. Nothing else in that repo is
# touched, and a file listed here is overwritten on every publish.
MANAGED_FILES = (
    "index.html",
    "all.html",
    "papers.json",
    "style.css",
    "CNAME",
    "README.md",
)

STYLE_CSS = """\
:root {
  --bg: #ffffff;
  --fg: #1a1a1a;
  --muted: #666666;
  --rule: #e5e7eb;
  --chip-bg: #eeeef8;
  --chip-fg: #333344;
  --flag-bg: #fde68a;
  --flag-fg: #92400e;
  --link: #1d4ed8;
}

body {
  font-family: -apple-system, Helvetica, Arial, sans-serif;
  max-width: 760px;
  margin: 0 auto;
  padding: 0 16px 48px;
  background: var(--bg);
  color: var(--fg);
  line-height: 1.45;
}

a { color: var(--link); }
h1 { font-size: 22px; margin-bottom: 4px; }
h2 { font-size: 15px; margin: 32px 0 8px; color: var(--muted); }
h3 { font-size: 16px; margin: 0 0 2px; }
p.lede { color: var(--muted); margin-top: 0; }
p.authors { color: var(--muted); font-size: 13px; margin: 0 0 6px; }
p.meta { margin: 6px 0; font-size: 12px; }
article.paper { border-top: 1px solid var(--rule); padding: 12px 0; }
details { font-size: 14px; }
summary { color: var(--muted); cursor: pointer; }

.chip {
  display: inline-block;
  background: var(--chip-bg);
  color: var(--chip-fg);
  padding: 1px 6px;
  border-radius: 3px;
  margin-right: 4px;
  font-size: 12px;
}
.chip.flag { background: var(--flag-bg); color: var(--flag-fg); }
.chip.tag[data-tag] { cursor: pointer; }
.chip.tag.active { outline: 1px solid var(--link); }

footer {
  border-top: 1px solid var(--rule);
  margin-top: 32px;
  padding-top: 12px;
  font-size: 13px;
  color: var(--muted);
}

table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--rule); vertical-align: top; }
th { color: var(--muted); font-weight: 600; }
td.num { text-align: right; white-space: nowrap; }
input.filter { width: 100%; padding: 6px 8px; font-size: 14px; margin: 12px 0; }

@media (prefers-color-scheme: dark) {
  :root {
    --bg: #15171a;
    --fg: #e8e8ea;
    --muted: #9ba1a8;
    --rule: #2c3034;
    --chip-bg: #262b33;
    --chip-fg: #c8cfd8;
    --flag-bg: #4a3a12;
    --flag-fg: #f3d58a;
    --link: #7aa2f7;
  }
}
"""

SITE_README = """\
# paper-watch site

Every file in this repository is generated by paper-watch and served from the
default branch at https://{domain}/. Hand edits are overwritten on the next
publish, so a change belongs in the paper-watch source instead.
"""


def rating_markers(record: SiteRecord) -> list[str]:
    """The short markers shown next to a paper.

    A rating and a reading are independent, so both can show at once. "picked"
    is shown only when the group chose the paper but it has neither been read
    nor rated, because in the other cases those markers say more.
    """
    markers: list[str] = []
    if record.group_rating is not None:
        markers.append(RATING_FORMAT.format(n=record.group_rating))
    if record.read:
        markers.append("read")
    if record.picked and not record.read and record.group_rating is None:
        markers.append("picked")
    return markers


def latest_digest_at(records: list[SiteRecord]) -> str | None:
    """The newest `digest_at` in `records`, or None when nothing was ever shown.

    The site carries this instead of a wall-clock build time, so that a publish
    that changes nothing produces no diff.
    """
    return records[0].digest_at if records else None


_INDEX_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ site_title }}</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<h1>{{ site_title }}</h1>
<p class="lede">{{ site_description }}</p>
{% for digest_at, records in groups %}
<section>
<h2>{{ digest_at[:10] }}</h2>
{% for r in records %}
<article class="paper">
<h3>{% if r.url %}<a href="{{ r.url }}">{{ r.title }}</a>{% else %}{{ r.title }}{% endif %}</h3>
{% if r.authors %}<p class="authors">{{ r.authors|join(", ") }}</p>{% endif %}
<p class="meta">
{%- if r.published %}<span class="chip">{{ "~" if r.is_estimate }}{{ r.published }}</span>{% endif %}
{%- if r.resurfaced %}<span class="chip flag">resurfaced</span>{% endif %}
{%- for t in r.tags %}<span class="chip tag">{{ t }}</span>{% endfor %}
{%- for m in markers(r) %}<span class="chip">{{ m }}</span>{% endfor %}
</p>
{% if r.tldr %}<details><summary>{{ tldr_label }}</summary><p>{{ r.tldr }}</p></details>{% endif %}
</article>
{% endfor %}
</section>
{% endfor %}
<footer>
<p><a href="all.html">All papers in one table</a> · <a href="papers.json">papers.json</a></p>
<p>{{ ratings_note }}</p>
{% if latest %}<p>Latest digest: {{ latest[:10] }}</p>{% endif %}
</footer>
</body>
</html>
"""

_ALL_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ site_title }}: all papers</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<h1>{{ site_title }}: all papers</h1>
<p class="lede">{{ site_description }}</p>
<p><a href="index.html">Back to the digests</a></p>
<input class="filter" id="filter" type="search" placeholder="Filter by title or author">
<table>
<thead>
<tr><th>title</th><th>authors</th><th>published</th><th>tags</th><th>first digest</th><th>rank</th><th>times shown</th><th>rating</th></tr>
</thead>
<tbody>
{% for row in rows %}
<tr data-tags="{{ row.record.tags|join("|") }}" data-text="{{ (row.record.title ~ " " ~ row.record.authors|join(" "))|lower }}">
<td>{% if row.record.url %}<a href="{{ row.record.url }}">{{ row.record.title }}</a>{% else %}{{ row.record.title }}{% endif %}</td>
<td>{{ row.record.authors|join(", ") }}</td>
<td>{{ "~" if row.record.is_estimate }}{{ row.record.published }}</td>
<td>{% for t in row.record.tags %}<span class="chip tag" data-tag="{{ t }}">{{ t }}</span>{% endfor %}</td>
<td>{{ row.first_digest_at[:10] }}</td>
<td class="num">{{ row.record.rank if row.record.rank is not none }}</td>
<td class="num">{{ row.times_shown }}</td>
<td>{{ markers(row.record)|join(", ") }}</td>
</tr>
{% endfor %}
</tbody>
</table>
<footer>
<p><a href="index.html">Back to the digests</a> · <a href="papers.json">papers.json</a></p>
<p>{{ ratings_note }}</p>
{% if latest %}<p>Latest digest: {{ latest[:10] }}</p>{% endif %}
</footer>
<script>
(function () {
  var box = document.getElementById("filter");
  var rows = Array.prototype.slice.call(document.querySelectorAll("tbody tr"));
  var chips = Array.prototype.slice.call(document.querySelectorAll(".chip.tag"));
  var activeTag = null;
  function apply() {
    var needle = box.value.trim().toLowerCase();
    rows.forEach(function (row) {
      var text = row.getAttribute("data-text") || "";
      var tags = (row.getAttribute("data-tags") || "").split("|");
      var textOk = !needle || text.indexOf(needle) !== -1;
      var tagOk = !activeTag || tags.indexOf(activeTag) !== -1;
      row.hidden = !(textOk && tagOk);
    });
    chips.forEach(function (chip) {
      var on = chip.getAttribute("data-tag") === activeTag;
      chip.className = on ? "chip tag active" : "chip tag";
    });
  }
  box.addEventListener("input", apply);
  chips.forEach(function (chip) {
    chip.addEventListener("click", function () {
      var tag = chip.getAttribute("data-tag");
      activeTag = activeTag === tag ? null : tag;
      apply();
    });
  });
  apply();
})();
</script>
</body>
</html>
"""


def _render(template: str, **context) -> str:
    env = Environment(autoescape=True)
    return env.from_string(template).render(
        site_title=SITE_TITLE,
        site_description=SITE_DESCRIPTION,
        tldr_label=TLDR_LABEL,
        ratings_note=RATINGS_NOTE,
        markers=rating_markers,
        **context,
    )


def render_index(
    groups: list[tuple[str, list[SiteRecord]]], *, latest_digest_at: str | None
) -> str:
    """The landing page: one section per digest, newest first."""
    return _render(_INDEX_TEMPLATE, groups=groups, latest=latest_digest_at)


def render_all(rows: list[TableRow], *, latest_digest_at: str | None) -> str:
    """The single filterable table, one row per paper."""
    return _render(_ALL_TEMPLATE, rows=rows, latest=latest_digest_at)


def render_json(
    groups: list[tuple[str, list[SiteRecord]]], *, latest_digest_at: str | None
) -> str:
    """`papers.json`: the same records the pages show, as data."""
    payload = {
        "latest_digest_at": latest_digest_at,
        "digests": [
            {
                "digest_at": digest_at,
                "entries": [record.to_json() for record in records],
            }
            for digest_at, records in groups
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=1) + "\n"


def build_site(store, *, domain: str) -> dict[str, str]:
    """Every managed file's content, built from the store in one pass.

    The result depends only on the store and the domain, so building twice
    without new data gives identical files and publish has nothing to commit.
    """
    return _build_files(load_records(store), domain=domain)


def _build_files(records: list[SiteRecord], *, domain: str) -> dict[str, str]:
    """`build_site` for a caller that already loaded the records."""
    latest = latest_digest_at(records)
    groups = group_by_digest(records)
    return {
        "index.html": render_index(groups, latest_digest_at=latest),
        "all.html": render_all(table_rows(records), latest_digest_at=latest),
        "papers.json": render_json(groups, latest_digest_at=latest),
        "style.css": STYLE_CSS,
        "CNAME": f"{domain}\n",
        "README.md": SITE_README.format(domain=domain),
    }


# ---------------------------------------------------------------------------
# Publishing to the site repo
# ---------------------------------------------------------------------------

# Every git call publish makes is given this timeout, so a hung network or a
# credential prompt cannot stall the systemd tick.
GIT_TIMEOUT_SECONDS = 120


class SitePublishError(RuntimeError):
    """A publish could not be completed.

    The message is short enough to put in an operator notice and in the source
    health row. When the failure came from git, the full standard error is kept
    on `stderr` for the log instead.
    """

    def __init__(self, message: str, *, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr


@dataclass
class PublishResult:
    """What one publish did.

    On a dry run, `files` holds the managed files that would be added or
    changed. On an ordinary publish it holds the paths git reported as changed.
    """

    changed: bool
    committed: bool
    pushed: bool
    files: list[str] = field(default_factory=list)
    message: str | None = None


def _deploy_key(environ: Mapping[str, str]) -> str:
    """The path in `SITE_DEPLOY_KEY`, with a leading `~` expanded, or "".

    python-dotenv stores the value as written, and ssh does not expand a tilde
    in the argument to `-i`, so the expansion happens here. Both the existence
    check and `GIT_SSH_COMMAND` use this, so they always name the same file.
    """
    key = environ.get("SITE_DEPLOY_KEY") or ""
    if not key:
        return ""
    home = environ.get("HOME")
    if key.startswith("~") and home:
        return str(Path(key.replace("~", home, 1)))
    return str(Path(key).expanduser())


# The ssh options every push and fetch uses. ConnectTimeout bounds the TCP
# connect separately from GIT_TIMEOUT_SECONDS, so an unreachable host fails in
# seconds rather than holding the whole git timeout.
_SSH_BASE = "ssh -o BatchMode=yes -o ConnectTimeout=30"


def _git_env(environ: Mapping[str, str]) -> dict[str, str]:
    """`environ` with the ssh and prompt settings every git call needs.

    `BatchMode=yes` and `GIT_TERMINAL_PROMPT=0` mean a missing or unusable
    credential fails rather than waiting for a keyring or a password, which
    matters because publish normally runs from the systemd timer. When
    `SITE_DEPLOY_KEY` names a key, only that key is offered; when it is unset
    or empty, the ambient agent and default keys are still used, but without
    prompting.
    """
    env = dict(environ)
    key = _deploy_key(env)
    if key:
        env["GIT_SSH_COMMAND"] = (
            f"{_SSH_BASE} -i {shlex.quote(key)} -o IdentitiesOnly=yes"
        )
    else:
        env["GIT_SSH_COMMAND"] = _SSH_BASE
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _subcommand(args: tuple[str, ...]) -> str:
    """The git subcommand in `args`, for an error message."""
    pending = list(args)
    while pending:
        arg = pending.pop(0)
        if arg == "-c":
            if pending:
                pending.pop(0)
            continue
        if arg.startswith("-"):
            continue
        return arg
    return "git"


def _run_git(
    repo: Path, args: tuple[str, ...], env: Mapping[str, str]
) -> subprocess.CompletedProcess:
    """Run git in `repo`, raising only when it times out.

    Callers that need a non-zero exit to be an error use `_git` instead. This
    one exists for the checks where a non-zero exit is an answer rather than a
    failure, such as asking whether a ref exists.

    git runs as the leader of a new session, and a timeout kills that whole
    process group. Killing only git would leave an ssh child it started
    running, holding the output pipes open, so the timeout would not end the
    call.
    """
    cmd = ["git", "-C", str(repo), *args]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(env),
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise SitePublishError(
            f"git {_subcommand(args)} timed out after {GIT_TIMEOUT_SECONDS} seconds"
        ) from exc
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _git(repo: Path, *args: str, env: Mapping[str, str]) -> str:
    """Run git in `repo` and return its standard output, or raise."""
    proc = _run_git(repo, args, env)
    if proc.returncode != 0:
        stderr = proc.stderr or ""
        lines = [line.strip() for line in stderr.strip().splitlines() if line.strip()]
        first = lines[0] if lines else "no error output"
        raise SitePublishError(
            f"git {_subcommand(args)} failed (exit {proc.returncode}): {first}",
            stderr=stderr,
        )
    return proc.stdout


def _status_paths(status: str) -> list[str]:
    """The paths in `git status --porcelain` output."""
    paths = []
    for line in status.splitlines():
        if not line.strip():
            continue
        path = line[3:] if len(line) > 3 else line
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path.strip().strip('"'))
    return paths


def _committer_email(repo: Path, site: SiteConfig, env: Mapping[str, str]) -> str:
    """The email to commit under: the configured one, else the checkout's own."""
    if site.committer_email:
        return site.committer_email
    proc = _run_git(repo, ("config", "user.email"), env)
    email = proc.stdout.strip() if proc.returncode == 0 else ""
    if not email:
        raise SitePublishError(
            "no commit email for the site publish: set committer_email in the "
            f"site config block, or set user.email in {repo}"
        )
    return email


def _check_preconditions(repo: Path, branch: str, env: Mapping[str, str]) -> None:
    """Fail before anything is written if the checkout is not as expected."""
    if not repo.exists():
        raise SitePublishError(f"the site repo_path does not exist: {repo}")
    proc = _run_git(repo, ("rev-parse", "--show-toplevel"), env)
    if proc.returncode != 0:
        raise SitePublishError(f"the site repo_path is not a git checkout: {repo}")
    toplevel = Path(proc.stdout.strip()).resolve()
    if toplevel != repo.resolve():
        raise SitePublishError(
            f"the site repo_path {repo} is inside a git checkout rooted at "
            f"{toplevel}, not at the path itself"
        )
    proc = _run_git(repo, ("symbolic-ref", "--short", "HEAD"), env)
    if proc.returncode != 0:
        raise SitePublishError(
            f"the site checkout {repo} has no branch checked out, so publish "
            f"cannot tell whether it is on {branch}"
        )
    current = proc.stdout.strip()
    if current != branch:
        raise SitePublishError(
            f"the site checkout {repo} is on branch {current}, but the site "
            f"config says {branch}"
        )
    foreign = sorted(
        set(_status_paths(_git(repo, "status", "--porcelain", env=env)))
        - set(MANAGED_FILES)
    )
    if foreign:
        raise SitePublishError(
            "the site checkout has changes to files publish does not generate, "
            f"so it was left alone: {', '.join(foreign)}"
        )


def _is_ahead(repo: Path, remote: str, branch: str, env: Mapping[str, str]) -> bool:
    """Whether there is a local commit the remote does not have.

    This reads local refs only and never fetches, so it says yes when the
    remote-tracking ref is missing, which is the state after a failed push and
    after a fresh clone of an empty repo.
    """
    if _run_git(repo, ("rev-parse", "--verify", "--quiet", "HEAD"), env).returncode != 0:
        return False
    tracking = f"refs/remotes/{remote}/{branch}"
    if (
        _run_git(repo, ("rev-parse", "--verify", "--quiet", tracking), env).returncode
        != 0
    ):
        return True
    count = _git(repo, "rev-list", "--count", f"{remote}/{branch}..HEAD", env=env)
    return int(count.strip() or "0") > 0


def _check_not_behind(
    repo: Path, remote: str, branch: str, env: Mapping[str, str]
) -> None:
    """Fetch the remote branch and fail if the checkout is missing commits from it.

    Publish only fast-forwards the remote, so a checkout that is behind or has
    diverged would end in a rejected push on every tick. Failing here, before
    any file is written, gives the operator one clear message and leaves the
    checkout ready for a manual pull.

    A failed fetch is tolerated only while the remote-tracking ref does not
    exist, which is the state of a brand new, empty remote that has no branch
    to fetch. Once the ref exists, a failed fetch is an error, because the
    comparison below would otherwise use stale data.
    """
    tracking = f"refs/remotes/{remote}/{branch}"
    refspec = f"+refs/heads/{branch}:{tracking}"
    proc = _run_git(repo, ("fetch", "--quiet", remote, refspec), env)
    has_tracking = (
        _run_git(repo, ("rev-parse", "--verify", "--quiet", tracking), env).returncode
        == 0
    )
    if proc.returncode != 0:
        if not has_tracking:
            return
        stderr = proc.stderr or ""
        lines = [line.strip() for line in stderr.strip().splitlines() if line.strip()]
        first = lines[0] if lines else "no error output"
        raise SitePublishError(
            f"git fetch failed (exit {proc.returncode}): {first}", stderr=stderr
        )
    if not has_tracking:
        return
    has_head = (
        _run_git(repo, ("rev-parse", "--verify", "--quiet", "HEAD"), env).returncode == 0
    )
    if has_head:
        missing = _git(repo, "rev-list", "--count", f"HEAD..{tracking}", env=env)
        behind = int(missing.strip() or "0") > 0
    else:
        behind = True
    if behind:
        raise SitePublishError(
            f"the site checkout {repo} is behind the remote {remote}/{branch}, so "
            "it needs a manual pull before publish can push"
        )


def _dry_run_diff(repo: Path, files: Mapping[str, str]) -> list[str]:
    """The managed files whose content on disk differs from what we would write.

    The files are rendered into a temporary directory rather than into the
    checkout, so a dry run never changes the site repo.
    """
    diff = []
    with tempfile.TemporaryDirectory() as tmp:
        for name, content in files.items():
            rendered = Path(tmp) / name
            rendered.write_text(content, encoding="utf-8")
            current = repo / name
            if not current.exists() or current.read_bytes() != rendered.read_bytes():
                diff.append(name)
    return sorted(diff)


def publish(
    store,
    site: SiteConfig,
    *,
    now: datetime,
    dry_run: bool = False,
    environ: Mapping[str, str] | None = None,
) -> PublishResult:
    """Render the site into its checkout, commit the changes, and push them.

    Every precondition is checked before the first file is written, so a
    publish that cannot finish leaves the checkout as it was. A clean tree
    still pushes when there is a local commit the remote does not have, which
    is how a publish recovers from an earlier push failure. An ordinary publish
    fetches the remote branch first and stops if the checkout is behind it.
    """
    source_environ = environ if environ is not None else os.environ
    env = _git_env(source_environ)
    repo = Path(site.repo_path)
    _check_preconditions(repo, site.branch, env)

    key = _deploy_key(source_environ)
    if key and not Path(key).exists():
        raise SitePublishError(
            "SITE_DEPLOY_KEY names a file that does not exist, so the site "
            "cannot be pushed"
        )
    committer_email = _committer_email(repo, site, env)

    records = load_records(store)
    files = _build_files(records, domain=site.domain)

    if dry_run:
        diff = _dry_run_diff(repo, files)
        return PublishResult(
            changed=bool(diff), committed=False, pushed=False, files=diff
        )

    _check_not_behind(repo, site.remote, site.branch, env)

    for name, content in files.items():
        (repo / name).write_text(content, encoding="utf-8")

    changed = sorted(set(_status_paths(_git(repo, "status", "--porcelain", env=env))))
    if not changed:
        if _is_ahead(repo, site.remote, site.branch, env):
            _git(repo, "push", site.remote, site.branch, env=env)
            return PublishResult(changed=False, committed=False, pushed=True)
        _log.info("site unchanged")
        return PublishResult(changed=False, committed=False, pushed=False)

    digests = len({record.digest_at for record in records})
    papers = len({record.entry_id for record in records})
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    message = f"site: {digests} digests, {papers} papers ({stamp})"

    _git(repo, "add", "-A", env=env)
    _git(
        repo,
        "-c",
        f"user.name={site.committer_name}",
        "-c",
        f"user.email={committer_email}",
        "commit",
        "-m",
        message,
        env=env,
    )
    _git(repo, "push", site.remote, site.branch, env=env)
    return PublishResult(
        changed=True, committed=True, pushed=True, files=changed, message=message
    )
