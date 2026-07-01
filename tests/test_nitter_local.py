from pathlib import Path

from paper_watch.nitter_local import ensure_local_nitter

LOCAL = "http://localhost:8080"
PUBLIC = "https://nitter.net"
INSTANCES = [LOCAL, PUBLIC]


def _sleep_noop(_):  # never actually wait in tests
    pass


def test_no_local_instance_is_a_noop():
    called = []
    result = ensure_local_nitter(
        [PUBLIC],
        dry_run=False,
        reachable=lambda u: called.append(u) or True,
        start=lambda cf: (_ for _ in ()).throw(AssertionError("should not start")),
        sleep=_sleep_noop,
    )
    assert result == [PUBLIC]
    assert called == []  # no probe when nothing local is configured


def test_reachable_local_passes_through_untouched():
    result = ensure_local_nitter(
        INSTANCES,
        dry_run=False,
        reachable=lambda u: True,
        start=lambda cf: (_ for _ in ()).throw(AssertionError("should not start")),
        sleep=_sleep_noop,
    )
    assert result == INSTANCES


def test_dry_run_warns_and_proceeds_without_starting():
    starts = []
    result = ensure_local_nitter(
        INSTANCES,
        dry_run=True,
        reachable=lambda u: False,
        start=lambda cf: starts.append(cf) or True,
        sleep=_sleep_noop,
    )
    assert result == INSTANCES  # list unchanged; run proceeds
    assert starts == []  # dry run never tries to start Nitter


def test_real_run_starts_and_uses_local_once_up():
    # down until started, then reachable
    state = {"up": False}
    starts = []

    def start(cf):
        state["up"] = True
        starts.append(cf)
        return True

    result = ensure_local_nitter(
        INSTANCES,
        dry_run=False,
        reachable=lambda u: state["up"],
        start=start,
        sleep=_sleep_noop,
    )
    assert result == INSTANCES
    assert len(starts) == 1  # one start attempt sufficed


def test_real_run_retries_then_succeeds():
    # stays down for the first two start attempts, comes up on the third
    attempts = {"n": 0}

    def start(cf):
        attempts["n"] += 1
        return True

    def reachable(u):
        return attempts["n"] >= 3

    result = ensure_local_nitter(
        INSTANCES,
        dry_run=False,
        reachable=reachable,
        start=start,
        sleep=_sleep_noop,
    )
    assert result == INSTANCES
    assert attempts["n"] == 3


def test_three_failures_drops_local_and_runs_without_it():
    starts = []
    result = ensure_local_nitter(
        INSTANCES,
        dry_run=False,
        reachable=lambda u: False,  # never comes up
        start=lambda cf: starts.append(cf) or True,
        sleep=_sleep_noop,
    )
    assert result == [PUBLIC]  # local instance dropped
    assert len(starts) == 3  # gave up after three attempts


def test_start_command_failure_also_counts_as_a_failed_attempt():
    result = ensure_local_nitter(
        INSTANCES,
        dry_run=False,
        reachable=lambda u: False,
        start=lambda cf: False,  # docker itself fails every time
        sleep=_sleep_noop,
    )
    assert result == [PUBLIC]


def test_dropping_only_local_leaves_empty_list():
    result = ensure_local_nitter(
        [LOCAL],
        dry_run=False,
        reachable=lambda u: False,
        start=lambda cf: True,
        sleep=_sleep_noop,
    )
    assert result == []  # nothing left; Nitter source simply yields nothing


def test_default_compose_file_points_at_deploy_nitter():
    from paper_watch.nitter_local import default_compose_file

    p = default_compose_file()
    assert p.name == "docker-compose.yml"
    assert p.parent.name == "nitter"
    assert isinstance(p, Path)
