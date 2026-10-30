"""Batch inference worker: Redis Streams consumer group, at-least-once.

Each worker reads jobs with XREADGROUP, predicts, writes the result to PostgreSQL (idempotent on job_id),
then XACKs. If a worker dies mid-job the message stays pending; live workers XAUTOCLAIM anything idle for
CLAIM_IDLE_MS and finish it. So a job can run twice but is recorded once."""
from __future__ import annotations

import json
import logging
import os
import socket
import time

import psycopg
import redis
from prometheus_client import Counter, Gauge, start_http_server

from .meter import SCHEMA
from .model import ETAModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("worker")
STREAM, GROUP = "jobs", "workers"
NAME = os.environ.get("POD_NAME", socket.gethostname())
CLAIM_IDLE_MS = int(os.environ.get("CLAIM_IDLE_MS", "15000"))
WORK_MS = float(os.environ.get("WORK_MS", "0"))   # optional extra per-job time, to widen the kill window in tests

DONE = Counter("worker_jobs_total", "jobs processed", ["result"])
RECLAIMED = Counter("worker_jobs_reclaimed_total", "jobs claimed from a dead consumer")
DEPTH = Gauge("batch_queue_depth", "jobs waiting or in flight", ["kind"])


def update_depth(r):
    for g in r.xinfo_groups(STREAM):
        if g["name"] == GROUP:
            DEPTH.labels("pending").set(g.get("pending") or 0)
            DEPTH.labels("lag").set(g.get("lag") or 0)


def ensure_group(r):
    try:
        r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except redis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


def process(r, db, model, msg_id, fields):
    rows = json.loads(fields["rows"])
    model.predict(rows)
    if WORK_MS:
        time.sleep(WORK_MS / 1000)
    with db.cursor() as cur:
        cur.execute("INSERT INTO batch_results (job_id, tenant, rows, worker) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (job_id) DO UPDATE SET attempts = batch_results.attempts + 1",
                    (fields["job_id"], fields["tenant"], len(rows), NAME))
    r.xack(STREAM, GROUP, msg_id)
    DONE.labels("ok").inc()


def main():
    start_http_server(int(os.environ.get("METRICS_PORT", "9100")))
    r = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)
    db = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    with db.cursor() as cur:
        cur.execute(SCHEMA)
    ensure_group(r)
    model = ETAModel(os.environ.get("MODEL_VERSION", "v1"))
    log.info("worker %s ready", NAME)
    last_claim = 0.0
    while True:
        if time.time() - last_claim > 5:
            last_claim = time.time()
            update_depth(r)
            _, claimed, _ = r.xautoclaim(STREAM, GROUP, NAME, min_idle_time=CLAIM_IDLE_MS, start_id="0-0", count=50)
            for msg_id, fields in claimed:
                if fields:
                    RECLAIMED.inc()
                    process(r, db, model, msg_id, fields)
        resp = r.xreadgroup(GROUP, NAME, {STREAM: ">"}, count=10, block=2000)
        for _, msgs in resp or []:
            for msg_id, fields in msgs:
                process(r, db, model, msg_id, fields)


if __name__ == "__main__":
    main()
