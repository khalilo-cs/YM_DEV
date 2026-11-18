import pytest

from yemendev.security.ratelimit import TokenBucketLimiter


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_burst_then_deny_then_refill():
    c = Clock()
    lim = TokenBucketLimiter(rate_per_minute=60, burst=3, clock=c)
    assert [lim.allow("k")[0] for _ in range(4)] == [True, True, True, False]
    ok, wait = lim.allow("k")
    assert not ok and wait == pytest.approx(1.0)
    c.t += 1.0
    assert lim.allow("k")[0] and not lim.allow("k")[0]


def test_keys_are_independent_and_peek_does_not_consume():
    c = Clock()
    lim = TokenBucketLimiter(rate_per_minute=60, burst=1, clock=c)
    assert lim.peek("a")[0] and lim.peek("a")[0]
    assert lim.allow("a")[0] and not lim.allow("a")[0]
    assert lim.allow("b")[0]


def test_memory_is_bounded():
    lim = TokenBucketLimiter(rate_per_minute=60, burst=1, max_keys=100, clock=Clock())
    for i in range(1000):
        lim.allow(f"k{i}")
    assert len(lim) == 100


def test_invalid_config():
    with pytest.raises(ValueError):
        TokenBucketLimiter(rate_per_minute=0, burst=1)
