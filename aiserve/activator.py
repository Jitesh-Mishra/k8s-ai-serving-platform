"""Scale-to-zero for the LLM service (a small Knative-style activator inside the gateway).

- A request that finds the LLM Deployment at 0 replicas scales it to 1, waits until llama.cpp reports healthy,
  then forwards. The wait is recorded as the cold start.
- A reaper scales the Deployment back to 0 after IDLE_SECONDS with no LLM traffic and nothing in flight.
State shared across gateway replicas lives in Redis (last request time, in-flight count)."""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("activator")


class K8sScaler:
    def __init__(self, namespace: str, deployment: str):
        from kubernetes import client, config
        config.load_incluster_config()
        self.api = client.AppsV1Api()
        self.ns, self.name = namespace, deployment

    def get(self) -> int:
        return int(self.api.read_namespaced_deployment_scale(self.name, self.ns).spec.replicas or 0)

    def set(self, n: int):
        self.api.patch_namespaced_deployment_scale(self.name, self.ns, {"spec": {"replicas": n}})


class Activator:
    def __init__(self, scaler, redis, health, idle_seconds: float, enabled: bool):
        self.scaler, self.r, self.health = scaler, redis, health   # health: async () -> bool
        self.idle, self.enabled = idle_seconds, enabled
        self.lock = asyncio.Lock()

    async def _call(self, fn, *a):
        res = fn(*a)
        return await res if hasattr(res, "__await__") else res

    async def begin(self) -> float:
        """Mark a request in flight; make sure a replica is serving. Returns cold-start seconds (0 if warm)."""
        await self._call(self.r.set, "llm:last_request", time.time())
        await self._call(self.r.incr, "llm:inflight")
        if not self.enabled:
            return 0.0
        if await self.health():
            return 0.0
        async with self.lock:
            t0 = time.perf_counter()
            if await asyncio.to_thread(self.scaler.get) == 0:
                log.info("LLM at zero replicas: scaling to 1")
                await asyncio.to_thread(self.scaler.set, 1)
            while not await self.health():
                if time.perf_counter() - t0 > 300:
                    raise TimeoutError("LLM did not become ready within 300 s")
                await asyncio.sleep(0.25)
            return time.perf_counter() - t0

    async def end(self):
        await self._call(self.r.decr, "llm:inflight")
        await self._call(self.r.set, "llm:last_request", time.time())

    async def reap_once(self, now: float | None = None) -> bool:
        if not self.enabled:
            return False
        now = now or time.time()
        last = float(await self._call(self.r.get, "llm:last_request") or 0)
        inflight = int(await self._call(self.r.get, "llm:inflight") or 0)
        if last and now - last > self.idle and inflight <= 0 and await asyncio.to_thread(self.scaler.get) > 0:
            log.info("LLM idle for %.0f s: scaling to 0", now - last)
            await asyncio.to_thread(self.scaler.set, 0)
            return True
        return False

    async def run(self, every: float = 5.0):
        while True:
            try:
                await self.reap_once()
            except Exception as e:
                log.warning("reaper error: %s", e)
            await asyncio.sleep(every)
