"""ETA model service.  uvicorn aiserve.eta:app"""
from __future__ import annotations

import os
import time

from fastapi import FastAPI, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel

from .common import LATENCY_BUCKETS
from .model import ETAModel

VERSION = os.environ.get("MODEL_VERSION", "v1")
app = FastAPI(title="eta-service")
REQS = Counter("eta_requests_total", "ETA requests", ["version", "status"])
ROWS = Counter("eta_predictions_total", "rows predicted", ["version"])
LAT = Histogram("eta_request_seconds", "ETA compute time", ["version"], buckets=LATENCY_BUCKETS)
state = {"model": None, "draining": False}


class PredictIn(BaseModel):
    rows: list[list[float | None]]


@app.on_event("startup")
def load():
    state["model"] = ETAModel(VERSION)
    # graceful removal is done by the pod's preStop sleep: the pod leaves the Service endpoints while it still serves


@app.get("/ready")
def ready():
    if state["model"] is None or state["draining"]:
        raise HTTPException(503, "not ready")
    return {"ok": True, "version": VERSION}


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/predict")
def predict(body: PredictIn):
    t = time.perf_counter()
    try:
        out = state["model"].predict(body.rows)
    except ValueError as e:
        REQS.labels(VERSION, "400").inc()
        raise HTTPException(400, str(e))
    LAT.labels(VERSION).observe(time.perf_counter() - t)
    REQS.labels(VERSION, "200").inc()
    ROWS.labels(VERSION).inc(len(out))
    return {"version": VERSION, "predictions": out}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
