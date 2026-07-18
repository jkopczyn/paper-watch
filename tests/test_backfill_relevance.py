"""The one-off 0-4 -> 0-10 relevance remap used by the backfill script.

The script self-executes under __main__, so load it by path (the guard keeps the
migration from running on import) and exercise only the pure mapping function.
"""

import importlib.util
from pathlib import Path

_MOD_PATH = Path(__file__).resolve().parents[1] / "deploy" / "backfill_relevance_scale.py"


def _rescale():
    spec = importlib.util.spec_from_file_location("backfill_relevance_scale", _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.rescale_relevance


def test_rescale_relevance_maps_old_scale_onto_new():
    rescale = _rescale()
    assert rescale(0) == 0
    assert rescale(1) == 3
    assert rescale(2) == 5
    assert rescale(3) == 8
    assert rescale(4) == 10


def test_rescale_relevance_is_monotonic_and_bounded():
    rescale = _rescale()
    values = [rescale(v) for v in range(5)]
    assert values == sorted(values)
    assert values[0] == 0 and values[-1] == 10
