"""Open-loop load generator: requests are sent on a fixed schedule whether or not earlier ones returned,
so an overloaded service shows up as latency and errors (no coordinated omission)."""
from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field

import httpx

ROWS = json.load(open("data/sample_features.json"))["rows"]


@dataclass
class Result:
    t: float          # send time, seconds since experiment start
    tenant: str
    ms: float
    status: int       # 0 = timeout / connection error
    version: str = ""


@dataclass
class Stream:
    tenant: str
    key: str
    rps: float
    duration: float
    start: float = 0.0              # offset from experiment start
    path: str = "/v1/eta"
    rows: int = 1
    results: list = field(default_factory=list)


async def _one(client, base, s: Stream, t_send, t0, sem):
    async with sem:
        body = {"rows": random.sample(ROWS, s.rows)}
        t = time.perf_counter()
        try:
            r = await client.post(base + s.path, json=body, headers={"X-API-Key": s.key})
            status = r.status_code
            ver = r.json().get("version", "") if status == 200 and s.path == "/v1/eta" else ""
        except Exception:
            status, ver = 0, ""
        s.results.append(Result(t_send - t0, s.tenant, (time.perf_counter() - t) * 1000, status, ver))


async def run(base: str, streams: list[Stream], timeout: float = 10.0, max_inflight: int = 2000) -> list[Result]:
    sem = asyncio.Semaphore(max_inflight)
    async with httpx.AsyncClient(timeout=timeout, limits=httpx.Limits(max_connections=max_inflight,
                                                                       max_keepalive_connections=500)) as client:
        t0 = time.perf_counter()
        tasks = []

        async def schedule(s: Stream):
            n = int(s.rps * s.duration)
            for i in range(n):
                due = t0 + s.start + i / s.rps
                delay = due - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)
                tasks.append(asyncio.create_task(_one(client, base, s, time.perf_counter(), t0, sem)))

        await asyncio.gather(*(schedule(s) for s in streams))
        await asyncio.gather(*tasks)
    return [r for s in streams for r in s.results]


def go(base, streams, **kw):
    return asyncio.run(run(base, streams, **kw))
