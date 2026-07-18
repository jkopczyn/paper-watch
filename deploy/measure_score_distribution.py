"""Measure the recomputed score distribution to tune the 0-10 weight rescale.

Works on a throwaway COPY of the DB (never mutates prod): rescales relevance to
the new 0-10 scale, then for every historical `shown` row rebuilds the paper's
ScoreFeatures as-of its digest time and recomputes the score under the base
weights multiplied by a candidate factor. Reports how much of the distribution
lands outside [0, 10] and [-1, 11] (the tuning guideline: <=5% and <=1%).

    uv run python deploy/measure_score_distribution.py [MULT]   # default 2.0
"""

import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from paper_watch.config import Config
from paper_watch.score import (
    ScoreFeatures,
    best_source_prior,
    compute_score,
    has_tracked_author,
    normalize_tracked_authors,
)
from paper_watch.store import Store

_RELEVANCE_0_4_TO_0_10 = {0: 0, 1: 3, 2: 5, 3: 8, 4: 10}
_ISO = "%Y-%m-%dT%H:%M:%SZ"


def _pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p / 100 * len(xs)))]


def main(mult: float) -> None:
    config = Config.load("config.yaml")
    window = config.candidate_window_days
    priors = config.source_priors
    tracked = normalize_tracked_authors(config.authors)
    weights = config.scoring.model_copy(
        update={
            f: getattr(config.scoring, f) * mult
            for f in ("relevance", "source", "overlap", "velocity", "feedback",
                      "author", "resurface_boost")
        }
    )

    src = config.db_path
    work = str(Path(tempfile.mkdtemp()) / "measure.db")
    shutil.copy2(src, work)
    store = Store(work)
    # Rescale relevance onto the new 0-10 scale (mirrors the backfill).
    for r in store.conn.execute("SELECT id, relevance FROM entries WHERE relevance IS NOT NULL").fetchall():
        store.conn.execute(
            "UPDATE entries SET relevance = ? WHERE id = ?",
            (_RELEVANCE_0_4_TO_0_10.get(r["relevance"], r["relevance"]), r["id"]),
        )
    store.conn.commit()

    scores = []
    for s in store.conn.execute(
        "SELECT entry_id, digest_at, resurfaced FROM shown"
    ).fetchall():
        entry_id = s["entry_id"]
        row = store.get_entry(entry_id)
        if row is None:
            continue
        cand_start = (datetime.strptime(s["digest_at"], _ISO) - timedelta(days=window)).strftime(_ISO)
        sources = {m["source"] for m in store.get_mentions(entry_id)}
        metrics = store.latest_metrics(entry_id)
        import json
        authors = json.loads(row["authors_json"])
        f = ScoreFeatures(
            distinct_sources=len(sources),
            citation_count=metrics["citation_count"] if metrics else None,
            citation_count_prev=metrics["citation_count_prev"] if metrics else None,
            new_mentions_in_window=store.count_mentions_since(entry_id, cand_start),
            feedback_affinity=0.0,  # no feedback data yet
            resurfaced=bool(s["resurfaced"]),
            relevance=row["relevance"],
            source_prior=best_source_prior(sources, priors),
            tracked_author=has_tracked_author(authors, tracked),
        )
        scores.append(compute_score(f, weights))

    store.close()
    shutil.rmtree(Path(work).parent, ignore_errors=True)

    n = len(scores)
    out_10 = sum(1 for x in scores if x < 0 or x > 10)
    out_11 = sum(1 for x in scores if x < -1 or x > 11)
    print(f"mult={mult}  weights: " + ", ".join(
        f"{f}={getattr(weights, f):.2f}" for f in
        ("relevance", "source", "overlap", "velocity", "feedback", "author", "resurface_boost")))
    print(f"n={n}  min={min(scores):.2f}  p50={_pct(scores,50):.2f}  "
          f"p95={_pct(scores,95):.2f}  p99={_pct(scores,99):.2f}  max={max(scores):.2f}")
    print(f"outside [0,10]: {out_10} ({100*out_10/n:.1f}%, guideline <=5%)")
    print(f"outside [-1,11]: {out_11} ({100*out_11/n:.1f}%, guideline <=1%)")


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 2.0)
