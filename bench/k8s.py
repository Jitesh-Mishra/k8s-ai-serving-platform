"""Thin wrappers over kubectl, helm, the Prometheus HTTP API and PostgreSQL, used by the experiments."""
from __future__ import annotations

import json
import subprocess
import threading
import time

import httpx

NS = "ai"
PROM = "http://localhost:30090"
GW = "http://localhost:30080"
PG = "postgresql://aiserve:aiserve@localhost:30432/aiserve"
CHART = "charts/aiserve"


def sh(cmd: str, check=True, capture=True) -> str:
    p = subprocess.run(cmd, shell=True, text=True, capture_output=capture)
    if check and p.returncode != 0:
        raise RuntimeError(f"{cmd}\n{p.stdout}\n{p.stderr}")
    return (p.stdout or "").strip()


def helm(values: dict, wait=True):
    sets = " ".join(f"--set {k}={json.dumps(v) if isinstance(v, bool) else v}" for k, v in values.items())
    sh(f"helm upgrade --install aiserve {CHART} -n {NS} --create-namespace -f deploy/aiserve-ci.yaml {sets} "
       f"{'--wait --timeout 10m' if wait else ''}")


def rollout_status(kind_name: str, timeout="300s"):
    sh(f"kubectl -n {NS} rollout status {kind_name} --timeout={timeout}")


def ready_replicas(kind: str = "deployment", name: str = "eta") -> int:
    out = sh(f"kubectl -n {NS} get {kind} {name} -o jsonpath='{{.status.readyReplicas}}'", check=False)
    return int(out or 0)


def wait_for(fn, timeout=300, every=2.0, what=""):
    t = time.time()
    while time.time() - t < timeout:
        if fn():
            return time.time() - t
        time.sleep(every)
    raise TimeoutError(f"timed out waiting for {what}")


def prom(query: str, t: float | None = None) -> list[dict]:
    params = {"query": query}
    if t:
        params["time"] = t
    r = httpx.get(f"{PROM}/api/v1/query", params=params, timeout=20).json()
    return r["data"]["result"]


def prom_value(query: str, t: float | None = None) -> float:
    res = prom(query, t)
    return float(res[0]["value"][1]) if res else 0.0


class Poller:
    """Background sampler of replica counts (for scale-out timing)."""

    def __init__(self, kind="deployment", name="eta", every=2.0):
        self.kind, self.name, self.every = kind, name, every
        self.samples, self._stop = [], threading.Event()

    def start(self):
        self.t0 = time.time()
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def _run(self):
        while not self._stop.is_set():
            out = sh(f"kubectl -n {NS} get {self.kind} {self.name} -o jsonpath='{{.status.readyReplicas}} {{.spec.replicas}}'",
                     check=False).split()
            ready = int(out[0]) if out and out[0].isdigit() else 0
            desired = int(out[-1]) if out and out[-1].isdigit() else 0
            self.samples.append({"t": round(time.time() - self.t0, 1), "ready": ready, "desired": desired})
            self._stop.wait(self.every)

    def stop(self):
        self._stop.set()
        return self.samples


def pg():
    import psycopg
    return psycopg.connect(PG, autocommit=True)
