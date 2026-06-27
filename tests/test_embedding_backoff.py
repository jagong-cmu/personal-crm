"""Offline unit tests for the Voyage exponential-backoff helper (T7).

No network: we inject a fake embed callable and a fake sleep so the retry policy
is exercised deterministically.
"""
from __future__ import annotations

import pytest

from app.services.embedding import _call_with_backoff


class Transient(Exception):
    """Stand-in for a retryable Voyage error (rate-limit / 5xx / connection)."""


def _recorder():
    sleeps: list[float] = []
    return sleeps, sleeps.append


def test_retries_then_succeeds():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] <= 2:  # fail twice, then succeed
            raise Transient("temporary")
        return "ok"

    sleeps, sleep = _recorder()
    result = _call_with_backoff(
        fn, transient=(Transient,), base_delay=1.0, max_attempts=5, sleep=sleep
    )

    assert result == "ok"
    assert calls["n"] == 3  # 2 failures + 1 success
    assert sleeps == [1.0, 2.0]  # one sleep per retry


def test_gives_up_and_reraises_after_cap():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise Transient("always down")

    sleeps, sleep = _recorder()
    with pytest.raises(Transient):
        _call_with_backoff(
            fn, transient=(Transient,), base_delay=1.0, max_attempts=5, sleep=sleep
        )

    assert calls["n"] == 5  # exactly max_attempts tries
    assert len(sleeps) == 4  # no sleep after the final failure


def test_backoff_grows_exponentially():
    def fn():
        raise Transient("down")

    sleeps, sleep = _recorder()
    with pytest.raises(Transient):
        _call_with_backoff(
            fn, transient=(Transient,), base_delay=1.0, max_attempts=5,
            max_delay=1000.0, sleep=sleep,
        )

    assert sleeps == [1.0, 2.0, 4.0, 8.0]
    for prev, nxt in zip(sleeps, sleeps[1:]):
        assert nxt > prev  # strictly increasing


def test_backoff_is_capped():
    def fn():
        raise Transient("down")

    sleeps, sleep = _recorder()
    with pytest.raises(Transient):
        _call_with_backoff(
            fn, transient=(Transient,), base_delay=1.0, max_attempts=6,
            max_delay=3.0, sleep=sleep,
        )

    assert sleeps == [1.0, 2.0, 3.0, 3.0, 3.0]  # capped at max_delay


def test_non_transient_reraises_immediately():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise ValueError("bad request")  # not in transient tuple

    sleeps, sleep = _recorder()
    with pytest.raises(ValueError):
        _call_with_backoff(fn, transient=(Transient,), sleep=sleep)

    assert calls["n"] == 1  # tried once, no retry
    assert sleeps == []


if __name__ == "__main__":  # offline fallback if pytest is unavailable
    test_retries_then_succeeds()
    test_gives_up_and_reraises_after_cap()
    test_backoff_grows_exponentially()
    test_backoff_is_capped()
    test_non_transient_reraises_immediately()
    print("all backoff tests passed")
