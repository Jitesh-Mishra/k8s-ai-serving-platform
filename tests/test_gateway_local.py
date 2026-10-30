"""Gateway + ETA service end to end on real HTTP (uvicorn subprocesses) with a real Redis; no Postgres."""
import json
import os
import subprocess
import sys
import time

import httpx
import pytest

ROWS = json.load(open("data/sample_features.json"))["rows"]


def wait(url):
    for _ in range(100):
        try:
            if httpx.get(url, timeout=0.5).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"{url} did not come up")


@pytest.fixture(scope="module")
def stack(redis_url):
    tenants = {"key-a": {"tenant": "tenant-a", "rps": 5, "burst": 5},
               "key-b": {"tenant": "tenant-b", "rps": 1000, "burst": 1000}}
    env = {**os.environ, "TENANTS_JSON": json.dumps(tenants), "REDIS_URL": redis_url,
           "ETA_URL": "http://127.0.0.1:18001", "QUOTAS_ENABLED": "true"}
    import redis
    redis.Redis.from_url(redis_url).flushdb()
    procs = [subprocess.Popen([sys.executable, "-m", "uvicorn", "aiserve.eta:app", "--port", "18001"], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
             subprocess.Popen([sys.executable, "-m", "uvicorn", "aiserve.gateway:app", "--port", "18000"], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)]
    wait("http://127.0.0.1:18001/ready")
    wait("http://127.0.0.1:18000/healthz")
    yield "http://127.0.0.1:18000"
    for p in procs:
        p.terminate()


def test_eta_through_gateway(stack):
    r = httpx.post(f"{stack}/v1/eta", json={"rows": ROWS[:3]}, headers={"X-API-Key": "key-b"})
    assert r.status_code == 200 and len(r.json()["predictions"]) == 3 and r.json()["version"] == "v1"


def test_unknown_key_rejected(stack):
    assert httpx.post(f"{stack}/v1/eta", json={"rows": ROWS[:1]}, headers={"X-API-Key": "nope"}).status_code == 401


def test_quota_isolates_tenants(stack):
    codes = [httpx.post(f"{stack}/v1/eta", json={"rows": ROWS[:1]}, headers={"X-API-Key": "key-a"}).status_code
             for _ in range(12)]
    assert codes.count(200) <= 7 and codes.count(429) >= 5
    assert httpx.post(f"{stack}/v1/eta", json={"rows": ROWS[:1]}, headers={"X-API-Key": "key-b"}).status_code == 200


def test_batch_enqueues_to_stream(stack, redis_url):
    import redis
    r = httpx.post(f"{stack}/v1/batch", json={"rows": ROWS[:4]}, headers={"X-API-Key": "key-b"})
    assert r.status_code == 202
    entries = redis.Redis.from_url(redis_url, decode_responses=True).xrange("jobs")
    assert entries[-1][1]["job_id"] == r.json()["job_id"]


def test_metrics_exposed(stack):
    m = httpx.get(f"{stack}/metrics").text
    assert 'gateway_requests_total{model="eta",status="429",tenant="tenant-a"}' in m
    assert 'gateway_units_total{model="eta",tenant="tenant-b"}' in m
