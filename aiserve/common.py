"""Shared settings and Prometheus helpers."""
from __future__ import annotations

import json
import os

ENV = os.environ.get


def env_int(k, d):
    return int(ENV(k, d))


def env_float(k, d):
    return float(ENV(k, d))


def tenants() -> dict[str, dict]:
    """API key -> {"tenant": name, "rps": quota, "burst": bucket size}. Mounted from a Kubernetes Secret."""
    raw = ENV("TENANTS_JSON")
    if raw:
        return json.loads(raw)
    path = ENV("TENANTS_FILE", "/etc/aiserve/tenants.json")
    if os.path.exists(path):
        return json.load(open(path))
    return {"dev-key": {"tenant": "dev", "rps": 1000, "burst": 2000}}


LATENCY_BUCKETS = (0.002, 0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
