"""Usage metering: every request the gateway answers is written to PostgreSQL (batched, async).
Invoices are SQL over that log; reconcile() compares them with the Prometheus counters."""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("meter")

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_log (
  id BIGSERIAL PRIMARY KEY,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  request_id TEXT NOT NULL,
  tenant TEXT NOT NULL,
  model TEXT NOT NULL,
  units BIGINT NOT NULL,            -- rows for eta, generated tokens for llm, rows for batch
  latency_ms DOUBLE PRECISION NOT NULL,
  status INT NOT NULL,
  gateway_pod TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_log_tenant_ts ON usage_log (tenant, ts);
CREATE TABLE IF NOT EXISTS batch_results (
  job_id TEXT PRIMARY KEY,
  tenant TEXT NOT NULL,
  rows INT NOT NULL,
  worker TEXT NOT NULL,
  attempts INT NOT NULL DEFAULT 1,
  completed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS prices (
  model TEXT PRIMARY KEY,
  unit TEXT NOT NULL,
  usd_per_unit DOUBLE PRECISION NOT NULL
);
CREATE OR REPLACE VIEW daily_invoice AS
  SELECT date_trunc('day', u.ts) AS day, u.tenant, u.model,
         count(*) FILTER (WHERE u.status < 400) AS billable_requests,
         count(*) FILTER (WHERE u.status = 429) AS throttled_requests,
         coalesce(sum(u.units) FILTER (WHERE u.status < 400), 0) AS units,
         round((coalesce(sum(u.units) FILTER (WHERE u.status < 400), 0) * coalesce(max(p.usd_per_unit), 0))::numeric, 6) AS amount_usd
  FROM usage_log u LEFT JOIN prices p ON p.model = u.model
  GROUP BY 1, 2, 3;
"""


class Meter:
    """Buffers usage rows and flushes them in batches; flush() runs on shutdown so a graceful stop loses nothing."""

    def __init__(self, dsn: str | None, pod: str, flush_every: float = 0.2, max_batch: int = 500):
        self.dsn, self.pod, self.flush_every, self.max_batch = dsn, pod, flush_every, max_batch
        self.q: asyncio.Queue = asyncio.Queue()
        self.conn = None
        self.task = None
        self.dropped = 0

    async def start(self):
        if not self.dsn:
            return
        import psycopg
        for _ in range(60):
            try:
                self.conn = await psycopg.AsyncConnection.connect(self.dsn, autocommit=True)
                break
            except Exception as e:  # postgres may still be starting
                log.warning("postgres not ready: %s", e)
                await asyncio.sleep(1)
        async with self.conn.cursor() as cur:
            await cur.execute(SCHEMA)
        self.task = asyncio.create_task(self._loop())

    def record(self, request_id, tenant, model, units, latency_ms, status):
        self.q.put_nowait((request_id, tenant, model, int(units), float(latency_ms), int(status), self.pod))

    async def _loop(self):
        while True:
            await asyncio.sleep(self.flush_every)
            await self.flush()

    async def flush(self):
        if self.conn is None:
            return
        while not self.q.empty():
            batch = []
            while not self.q.empty() and len(batch) < self.max_batch:
                batch.append(self.q.get_nowait())
            try:
                async with self.conn.cursor() as cur:
                    await cur.executemany(
                        "INSERT INTO usage_log (request_id, tenant, model, units, latency_ms, status, gateway_pod) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)", batch)
            except Exception as e:
                self.dropped += len(batch)
                log.error("usage write failed (%d rows dropped): %s", len(batch), e)

    async def stop(self):
        if self.task:
            self.task.cancel()
        await self.flush()
