import asyncio
import json

import pytest
import redis.asyncio as aioredis

from aiserve.activator import Activator
from aiserve.model import ETAModel
from aiserve.ratelimit import TokenBucket

ROWS = json.load(open("data/sample_features.json"))["rows"]


def test_model_v1_v2_and_validation():
    v1, v2 = ETAModel("v1"), ETAModel("v2")
    a, b = v1.predict(ROWS[:5]), v2.predict(ROWS[:5])
    assert len(a) == 5 and all(p["p90_min"] >= p["p50_min"] >= 0 for p in a + b)
    assert a != b                                  # v2 is a different model version
    with pytest.raises(ValueError):
        v1.predict([[1.0, 2.0]])


def test_token_bucket_limits_rate(redis_url):
    async def go():
        r = aioredis.from_url(redis_url, decode_responses=True)
        await r.delete("rl:t1")
        tb = TokenBucket(r)
        ok = [await tb.allow("t1", rate=10, burst=5, now=1000.0) for _ in range(8)]
        assert ok == [True] * 5 + [False] * 3      # burst of 5, then empty
        assert await tb.allow("t1", 10, 5, now=1000.25)   # 0.25 s refills 2.5 tokens
        await r.aclose()
    asyncio.run(go())


class FakeScaler:
    def __init__(self, n):
        self.n, self.calls = n, []

    def get(self):
        return self.n

    def set(self, n):
        self.calls.append(n)
        self.n = n


def test_activator_cold_start_and_reaper(redis_url):
    async def go():
        r = aioredis.from_url(redis_url, decode_responses=True)
        await r.delete("llm:last_request", "llm:inflight")
        sc = FakeScaler(0)
        healthy = {"after": 3}

        async def health():
            healthy["after"] -= 1 if sc.n > 0 else 0
            return sc.n > 0 and healthy["after"] <= 0

        act = Activator(sc, r, health, idle_seconds=60, enabled=True)
        cold = await act.begin()
        assert sc.calls == [1] and cold > 0
        assert not await act.reap_once()            # request still in flight
        await act.end()
        assert not await act.reap_once()            # not idle yet
        import time
        assert await act.reap_once(now=time.time() + 61) and sc.n == 0
        await r.aclose()
    asyncio.run(go())
