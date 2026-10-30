"""Multi-tenant model API gateway.  uvicorn aiserve.gateway:app

POST /v1/eta        {"rows": [[19 features], ...]}      -> eta-service
POST /v1/generate   {"prompt": str, "max_tokens": int}  -> llm-service (llama.cpp), scale-from-zero
POST /v1/batch      {"rows": [...]}                     -> Redis Streams job, 202 + job_id
GET  /v1/batch/{id}                                      -> result status from PostgreSQL
Every request: API key -> tenant, per-tenant token bucket (Redis), Prometheus counters, usage row in PostgreSQL."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel

from .activator import Activator, K8sScaler
from .common import ENV, LATENCY_BUCKETS, env_float, tenants
from .meter import Meter
from .ratelimit import TokenBucket

logging.basicConfig(level=logging.INFO)
POD = ENV("POD_NAME", "local")
ETA_URL = ENV("ETA_URL", "http://eta:8000")
LLM_URL = ENV("LLM_URL", "http://llm:8080")
QUOTAS = ENV("QUOTAS_ENABLED", "true").lower() == "true"

REQS = Counter("gateway_requests_total", "requests answered", ["tenant", "model", "status"])
UNITS = Counter("gateway_units_total", "billable units (rows or tokens)", ["tenant", "model"])
LAT = Histogram("gateway_request_seconds", "end-to-end latency at the gateway", ["tenant", "model"], buckets=LATENCY_BUCKETS)
COLD = Histogram("gateway_llm_cold_start_seconds", "time to bring the LLM from 0 to ready",
                 buckets=(1, 2, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 300))
INFLIGHT = Gauge("gateway_inflight", "requests in flight", ["model"])
QUEUED = Counter("gateway_batch_jobs_enqueued_total", "batch jobs enqueued", ["tenant"])

app = FastAPI(title="ai-gateway")
S: dict = {}


class EtaIn(BaseModel):
    rows: list[list[float | None]]


class GenIn(BaseModel):
    prompt: str
    max_tokens: int = 64


@app.on_event("startup")
async def startup():
    S["tenants"] = tenants()
    S["redis"] = aioredis.from_url(ENV("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)
    S["bucket"] = TokenBucket(S["redis"])
    S["http"] = httpx.AsyncClient(limits=httpx.Limits(max_connections=200, max_keepalive_connections=100),
                                  timeout=httpx.Timeout(10.0, read=180.0))
    S["meter"] = Meter(ENV("DATABASE_URL"), POD)
    await S["meter"].start()

    async def health():
        try:
            return (await S["http"].get(f"{LLM_URL}/health", timeout=1.0)).status_code == 200
        except Exception:
            return False

    enabled = ENV("SCALE_TO_ZERO", "false").lower() == "true"
    scaler = K8sScaler(ENV("POD_NAMESPACE", "default"), ENV("LLM_DEPLOYMENT", "llm")) if enabled else None
    S["activator"] = Activator(scaler, S["redis"], health, env_float("LLM_IDLE_SECONDS", "60"), enabled)
    if enabled:
        S["reaper"] = asyncio.create_task(S["activator"].run())


@app.on_event("shutdown")
async def shutdown():
    await S["meter"].stop()
    await S["http"].aclose()


async def admit(api_key: str | None, model: str) -> str:
    t = S["tenants"].get(api_key or "")
    if t is None:
        raise HTTPException(401, "unknown API key")
    if QUOTAS and not await S["bucket"].allow(t["tenant"], t["rps"], t["burst"]):
        finish(uuid.uuid4().hex, t["tenant"], model, 0, 0.0, 429)
        raise HTTPException(429, f"tenant {t['tenant']} over quota ({t['rps']} req/s)")
    return t["tenant"]


def finish(rid, tenant, model, units, seconds, status):
    REQS.labels(tenant, model, str(status)).inc()
    if status < 400:
        UNITS.labels(tenant, model).inc(units)
        LAT.labels(tenant, model).observe(seconds)
    S["meter"].record(rid, tenant, model, units, seconds * 1000, status)


@app.post("/v1/eta")
async def eta(body: EtaIn, x_api_key: str | None = Header(None)):
    tenant = await admit(x_api_key, "eta")
    rid, t0 = uuid.uuid4().hex, time.perf_counter()
    INFLIGHT.labels("eta").inc()
    try:
        r = await S["http"].post(f"{ETA_URL}/predict", json={"rows": body.rows})
        status = r.status_code
    except httpx.HTTPError:
        status = 502
    finally:
        INFLIGHT.labels("eta").dec()
    finish(rid, tenant, "eta", len(body.rows), time.perf_counter() - t0, status)
    if status != 200:
        raise HTTPException(status, "upstream error")
    return {"request_id": rid, **r.json()}


@app.post("/v1/generate")
async def generate(body: GenIn, x_api_key: str | None = Header(None)):
    tenant = await admit(x_api_key, "llm")
    rid, t0 = uuid.uuid4().hex, time.perf_counter()
    act: Activator = S["activator"]
    INFLIGHT.labels("llm").inc()
    try:
        cold = await act.begin()
        if cold > 0:
            COLD.observe(cold)
        r = await S["http"].post(f"{LLM_URL}/completion",
                                 json={"prompt": body.prompt, "n_predict": body.max_tokens, "temperature": 0.0,
                                       "cache_prompt": False})
        status = r.status_code
        out = r.json() if status == 200 else {}
    except (httpx.HTTPError, TimeoutError):
        status, out, cold = 502, {}, 0.0
    finally:
        INFLIGHT.labels("llm").dec()
        await act.end()
    tokens = int(out.get("tokens_predicted", 0))
    finish(rid, tenant, "llm", tokens, time.perf_counter() - t0, status)
    if status != 200:
        raise HTTPException(status, "llm error")
    tm = out.get("timings", {})
    return {"request_id": rid, "text": out.get("content", ""), "tokens": tokens, "cold_start_s": round(cold, 3),
            "tokens_per_second": tm.get("predicted_per_second"), "prompt_tokens": out.get("tokens_evaluated")}


@app.post("/v1/batch", status_code=202)
async def batch(body: EtaIn, x_api_key: str | None = Header(None)):
    tenant = await admit(x_api_key, "batch")
    rid, t0 = uuid.uuid4().hex, time.perf_counter()
    await S["redis"].xadd("jobs", {"job_id": rid, "tenant": tenant, "rows": json.dumps(body.rows)})
    QUEUED.labels(tenant).inc()
    finish(rid, tenant, "batch", len(body.rows), time.perf_counter() - t0, 202)
    return {"job_id": rid}


@app.get("/v1/batch/{job_id}")
async def batch_status(job_id: str, x_api_key: str | None = Header(None)):
    if S["meter"].conn is None:
        raise HTTPException(503, "no database")
    async with S["meter"].conn.cursor() as cur:
        await cur.execute("SELECT worker, attempts, completed_at FROM batch_results WHERE job_id=%s", (job_id,))
        row = await cur.fetchone()
    return {"job_id": job_id, "done": row is not None, **({"worker": row[0], "attempts": row[1]} if row else {})}


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.exception_handler(HTTPException)
async def http_exc(_: Request, e: HTTPException):
    return JSONResponse({"error": e.detail}, status_code=e.status_code)
