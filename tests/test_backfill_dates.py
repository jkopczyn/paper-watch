"""The summary formatting used by the publication-date backfill script.

The script self-executes under __main__, so load it by path (the guard keeps
main from running on import) and exercise only the pure helper. `main` itself
touches the DB and the network, so it is left untested like the other backfill
scripts.
"""

import importlib.util
from pathlib import Path

from paper_watch.runtime import DateFillResult

_MOD_PATH = Path(__file__).resolve().parents[1] / "deploy" / "backfill_pubdates_v2.py"


def _summary_lines():
    spec = importlib.util.spec_from_file_location("backfill_pubdates_v2", _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.summary_lines


def test_summary_lines_report_every_method():
    result = DateFillResult(url=4, graphql=3, html_meta=2, pdf_meta=1, llm=5, unfilled=6)
    text = "\n".join(_summary_lines()(result))
    assert "URL" in text and "4" in text
    assert "GraphQL" in text and "3" in text
    assert "HTML meta" in text and "2" in text
    assert "PDF meta" in text and "1" in text
    assert "LLM" in text and "5" in text
    assert "unfilled" in text and "6" in text
    assert "15" in text  # total filled


def test_summary_lines_handle_an_empty_run():
    lines = _summary_lines()(DateFillResult())
    assert lines
    assert all(isinstance(line, str) for line in lines)
    assert "0" in "\n".join(lines)
