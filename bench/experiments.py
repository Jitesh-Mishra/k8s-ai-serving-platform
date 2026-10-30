"""Experiments run against the kind cluster. Each returns a dict that run_all writes to results/<name>.json."""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid

import numpy as np

from . import k8s
from .k8s import GW, NS, Poller, helm, pg, prom, prom_value, sh
from .loadgen import Stream, go

SLO_P99_MS = 100.0
PRICE_PER_VCPU_HOUR = 0.0892 / 2   # AWS c7i.large on-demand, us-east-1, Linux: $0.0892/h for 2 vCPU (assumption, see README)
OUT = "results"


def stats(rs, t_from=None, t_to=None):
    sel = [r for r in rs if (t_from is None or r.t >= t_from) and (t_to is None or r.t < t_to)]
    ok = [r.ms for r in sel if r.status == 200]
    n = len(sel)
    return {"sent": n, "ok": len(ok), "errors": sum(1 for r in sel if r.status not in (200, 202, 429)),
            "throttled": sum(1 for r in sel if r.status == 429),
            "error_rate": round(sum(1 for r in sel if r.status not in (200, 202, 429)) / n, 4) if n else None,
            "p50_ms": round(float(np.percentile(ok, 50)), 1) if ok else None,
            "p99_ms": round(float(np.percentile(ok, 99)), 1) if ok else None}


def eta_values(**over):
    base = {"eta.rollout": False, "eta.version": "v1", "eta.preStopSeconds": 10, "eta.hpa.enabled": False,
            "eta.replicas": 1, "gateway.quotasEnabled": True, "llm.enabled": False}
    return {**base, **over}


# ---------------------------------------------------------------- E1 single pod vs HPA
STAGES = [25, 50, 100, 150, 200, 250, 300]
STAGE_S = 40


def step_load(tag):
    streams, start = [], 0.0
    for rps in STAGES:
        streams.append(Stream("bench", "key-bench", rps, STAGE_S, start=start))
        start += STAGE_S
    rs = go(GW, streams)
    rows = []
    for i, rps in enumerate(STAGES):
        st = stats(rs, i * STAGE_S + 10, (i + 1) * STAGE_S)          # skip the first 10 s of each step
        rows.append({"config": tag, "target_rps": rps, **st,
                     "slo_met": bool(st["p99_ms"] is not None and st["p99_ms"] <= SLO_P99_MS and (st["error_rate"] or 0) < 0.01)})
    return rows, rs


def e1_autoscale():
    out = {"slo": f"p99 <= {SLO_P99_MS} ms and errors < 1% (last 30 s of each 40 s step)", "stages_rps": STAGES}
    helm(eta_values(**{"eta.replicas": 1}))
    go(GW, [Stream("bench", "key-bench", 10, 15)])                       # warm-up
    single, _ = step_load("single_pod")
    helm(eta_values(**{"eta.hpa.enabled": True, "eta.replicas": 1}))
    k8s.wait_for(lambda: k8s.ready_replicas() == 1, 300, what="eta back to 1 replica")
    time.sleep(20)                                                     # let the adapter see the new pod
    go(GW, [Stream("bench", "key-bench", 10, 15)])
    poll = Poller("deployment", "eta").start()
    hpa, _ = step_load("hpa")
    samples = poll.stop()
    reactions = []
    for i, rps in enumerate(STAGES):
        s0 = i * STAGE_S
        before = max([s["ready"] for s in samples if s["t"] <= s0] or [1])
        later = [s for s in samples if s0 < s["t"] <= s0 + STAGE_S and s["ready"] > before]
        if later:
            reactions.append({"target_rps": rps, "from": before, "to": max(s["ready"] for s in samples if s0 < s["t"] <= s0 + STAGE_S),
                              "seconds_to_first_new_ready_pod": later[0]["t"] - s0})
    best = lambda rows: max([r["target_rps"] for r in rows if r["slo_met"]] or [0])
    out |= {"single_pod": single, "hpa": hpa, "replica_timeline": samples, "scale_out_reactions": reactions,
            "max_rps_within_slo": {"single_pod": best(single), "hpa": best(hpa)}}
    helm(eta_values(**{"eta.hpa.enabled": False, "eta.replicas": 2}))
    return out


# ---------------------------------------------------------------- E2 noisy neighbour
def noisy_phase(flood_rps):
    streams = [Stream("tenant-b", "key-tenant-b", 30, 60), Stream("tenant-c", "key-tenant-c", 30, 60)]
    if flood_rps:
        streams.append(Stream("tenant-a", "key-tenant-a", flood_rps, 60))
    t = time.time()
    rs = go(GW, streams)
    res = {"phase_wall_seconds": round(time.time() - t, 1)}
    for t in ("tenant-a", "tenant-b", "tenant-c"):
        sel = [r for r in rs if r.tenant == t and r.t >= 10]
        if sel:
            res[t] = stats(sel)
    return res


def e2_noisy_neighbour(flood_rps=300):
    helm(eta_values(**{"eta.replicas": 1, "gateway.quotasEnabled": True}))
    out = {"setup": "eta fixed at 1 replica (no HPA); tenants B and C at 30 req/s each; tenant A floods at "
                    f"{flood_rps} req/s with a quota of 50 req/s; 60 s per phase, first 10 s skipped"}
    out["baseline_no_flood"] = noisy_phase(0)
    out["flood_quotas_on"] = noisy_phase(flood_rps)
    helm(eta_values(**{"eta.replicas": 1, "gateway.quotasEnabled": False}))
    time.sleep(10)
    out["baseline_no_flood_after_restart"] = noisy_phase(0)
    out["flood_quotas_off"] = noisy_phase(flood_rps)
    helm(eta_values(**{"eta.replicas": 2, "gateway.quotasEnabled": True}))
    time.sleep(10)
    base = {t: out["baseline_no_flood"][t]["p99_ms"] for t in ("tenant-b", "tenant-c")}
    for k in ("flood_quotas_on", "flood_quotas_off"):
        out[k + "_p99_vs_baseline_pct"] = {t: round(100 * (out[k][t]["p99_ms"] / base[t] - 1), 1)
                                          for t in base if out[k][t]["p99_ms"]}
    return out


# ---------------------------------------------------------------- E3 metering + reconciliation
EXAMPLE_PRICES = [("eta", "row", 0.10 / 1000), ("batch", "row", 0.05 / 1000), ("llm", "token", 0.50 / 1_000_000)]


def reconcile():
    time.sleep(20)                       # meter flush (200 ms) + at least two Prometheus scrapes (5 s)
    pods = sh(f"kubectl -n {NS} get pods -l app=gateway -o jsonpath='{{.items[*].metadata.name}}'").split()
    promc = {}
    for s in prom(f'sum by (pod, tenant, model, status) (gateway_requests_total{{namespace="{NS}"}})'):
        m = s["metric"]
        if m.get("pod") in pods:
            promc[(m["pod"], m["tenant"], m["model"], m["status"])] = int(float(s["value"][1]))
    with pg() as c:
        rows = c.execute("SELECT gateway_pod, tenant, model, status::text, count(*) FROM usage_log "
                         "WHERE gateway_pod = ANY(%s) GROUP BY 1, 2, 3, 4", (pods,)).fetchall()
    sqlc = {(a, b, cm, d): n for a, b, cm, d, n in rows}
    keys = set(promc) | set(sqlc)
    diffs = {"|".join(k): sqlc.get(k, 0) - promc.get(k, 0) for k in keys if sqlc.get(k, 0) != promc.get(k, 0)}
    return {"gateway_pods": pods, "prometheus_requests": sum(promc.values()), "logged_requests": sum(sqlc.values()),
            "mismatched_requests": sum(abs(v) for v in diffs.values()), "mismatches": diffs}


def kill_window(kind: str):
    """Tenant C sends 50 req/s for 40 s; at 15 s a gateway pod is removed (forced kill or graceful restart).
    Compare what the client saw succeed with what was billed."""
    t_start = time.time()
    proc = subprocess.Popen(
        "sleep 15 && " + (f"kubectl -n {NS} delete pod $(kubectl -n {NS} get pods -l app=gateway -o jsonpath='{{.items[0].metadata.name}}') --grace-period=0 --force"
                          if kind == "sigkill" else f"kubectl -n {NS} rollout restart deployment/gateway"), shell=True)
    rs = go(GW, [Stream("tenant-c", "key-tenant-c", 50, 40)])
    proc.wait()
    k8s.rollout_status("deployment/gateway")
    time.sleep(5)
    client_ok = sum(1 for r in rs if r.status == 200)
    with pg() as c:
        billed = c.execute("SELECT count(*) FROM usage_log WHERE tenant='tenant-c' AND model='eta' AND status=200 "
                           "AND ts >= to_timestamp(%s)", (t_start - 1,)).fetchone()[0]
    return {"kind": kind, "client_sent": len(rs), "client_succeeded": client_ok,
            "client_failed": sum(1 for r in rs if r.status != 200), "billed_rows": billed,
            "unbilled_successful_requests": client_ok - billed}


def e3_metering():
    with pg() as c:
        for m, u, p in EXAMPLE_PRICES:
            c.execute("INSERT INTO prices VALUES (%s, %s, %s) ON CONFLICT (model) DO UPDATE SET usd_per_unit = EXCLUDED.usd_per_unit", (m, u, p))
        c.execute("DELETE FROM usage_log WHERE tenant='tenant-c'")
    go(GW, [Stream("tenant-a", "key-tenant-a", 80, 45), Stream("tenant-b", "key-tenant-b", 50, 45),
            Stream("tenant-c", "key-tenant-c", 10, 45, path="/v1/batch", rows=5)])
    out = {"reconciliation": reconcile(),
           "graceful_restart": kill_window("graceful"),
           "sigkill": kill_window("sigkill")}
    with pg() as c:
        inv = c.execute("SELECT day::date::text, tenant, model, billable_requests, throttled_requests, units, amount_usd::float "
                        "FROM daily_invoice ORDER BY tenant, model").fetchall()
    out["invoice"] = [dict(zip(["day", "tenant", "model", "billable_requests", "throttled_requests", "units", "amount_usd"], r)) for r in inv]
    out["example_prices"] = {m: f"${p:.8f} per {u}" for m, u, p in EXAMPLE_PRICES}
    return out


# ---------------------------------------------------------------- E4 batch queue + worker kills
def e4_batch(n_jobs=3000, rps=150):
    with pg() as c:
        c.execute("TRUNCATE batch_results")
    ids = []
    killer = subprocess.Popen(
        f"sleep 8 && kubectl -n {NS} delete pod $(kubectl -n {NS} get pods -l app=worker -o jsonpath='{{.items[0].metadata.name}}') --grace-period=0 --force; "
        f"sleep 12 && kubectl -n {NS} delete pod $(kubectl -n {NS} get pods -l app=worker -o jsonpath='{{.items[1].metadata.name}}') --grace-period=0 --force",
        shell=True)
    t0 = time.time()
    rs = go(GW, [Stream("bench", "key-bench", rps, n_jobs / rps, path="/v1/batch", rows=5)])
    accepted = sum(1 for r in rs if r.status == 202)
    killer.wait()

    def done():
        with pg() as c:
            return c.execute("SELECT count(*) FROM batch_results").fetchone()[0] >= accepted
    try:
        k8s.wait_for(done, 600, 3, "all batch jobs")
    except TimeoutError:
        pass
    drain = time.time() - t0
    with pg() as c:
        completed, multi = c.execute("SELECT count(*), count(*) FILTER (WHERE attempts > 1) FROM batch_results").fetchone()
    return {"jobs_sent": len(rs), "jobs_accepted": accepted, "jobs_completed": completed, "jobs_lost": accepted - completed,
            "jobs_processed_more_than_once": multi, "worker_pods_killed": 2,
            "reclaimed_from_dead_workers": int(prom_value(f'sum(worker_jobs_reclaimed_total{{namespace="{NS}"}})')),
            "seconds_from_first_enqueue_to_all_done": round(drain, 1), "workers": 3, "work_ms_per_job": 50}


# ---------------------------------------------------------------- E5 canary under load
def rollout_phase():
    return sh(f"kubectl -n {NS} get rollout eta -o jsonpath='{{.status.phase}} {{.status.currentStepIndex}}'", check=False)


def canary_run(tag, to_version, prestop, users=60, secs=150):
    series = f"{OUT}/canary_{tag}_series.json"
    loc = subprocess.Popen(f"SERIES_OUT={series} API_KEY=key-bench locust -f bench/locustfile.py --headless -u {users} -r {users} "
                           f"-t {secs}s --host {GW} --csv {OUT}/canary_{tag} --only-summary", shell=True)
    time.sleep(20)
    t_change = time.time()
    helm(eta_values(**{"eta.rollout": True, "eta.replicas": 5, "eta.version": to_version, "eta.preStopSeconds": prestop}), wait=False)
    time.sleep(3)
    phases = []
    while time.time() - t_change < secs - 25:
        phases.append({"t": round(time.time() - t_change, 1), "phase": rollout_phase()})
        time.sleep(2)
    loc.wait()
    import csv
    agg = [r for r in csv.DictReader(open(f"{OUT}/canary_{tag}_stats.csv")) if r["Name"] == "Aggregated"][0]
    by_label = json.load(open(series))["by_label"]
    return {"to_version": to_version, "prestop_seconds_on_old_pods": prestop, "requests": int(agg["Request Count"]),
            "failed": int(agg["Failure Count"]), "p99_ms": float(agg["99%"]), "responses_by_version": by_label,
            "rollout_timeline": phases}


def e5_canary():
    helm(eta_values(**{"eta.rollout": True, "eta.replicas": 5, "eta.version": "v1", "eta.preStopSeconds": 10}))
    time.sleep(5)
    k8s.wait_for(lambda: rollout_phase().startswith("Healthy"), 300, what="rollout healthy")
    out = {"steps": "setWeight 20, pause 20 s, setWeight 60, pause 20 s, 100 (replica-weighted, 5 replicas, maxSurge 1, maxUnavailable 0)",
           "load": "Locust, 60 users x 1 req/s, 150 s; rollout triggered at 20 s"}
    out["canary_v1_to_v2_with_prestop"] = canary_run("prestop", "v2", 10)
    time.sleep(5)
    k8s.wait_for(lambda: rollout_phase().startswith("Healthy"), 300, what="rollout healthy")
    # ablation: remove the preStop drain on the pods that will be replaced, then roll again under load
    helm(eta_values(**{"eta.rollout": True, "eta.replicas": 5, "eta.version": "v2", "eta.preStopSeconds": 0}), wait=False)
    time.sleep(8)
    k8s.wait_for(lambda: rollout_phase().startswith("Healthy"), 400, every=3, what="rollout healthy")
    out["ablation_v2_to_v1_without_prestop"] = canary_run("noprestop", "v1", 0)
    time.sleep(5)
    k8s.wait_for(lambda: rollout_phase().startswith("Healthy"), 300, what="rollout healthy")
    return out


# ---------------------------------------------------------------- E6 LLM: tokens/s, replicas, scale-to-zero, cost
PROMPTS = [f"Write two sentences about why ride-hailing pickup times vary in New York City zone {i}." for i in range(50)]


def gen(n, conc, max_tokens=128):
    import asyncio
    import httpx

    async def run():
        sem = asyncio.Semaphore(conc)
        res = []
        async with httpx.AsyncClient(timeout=300) as c:
            async def one(i):
                async with sem:
                    t = time.perf_counter()
                    r = await c.post(f"{GW}/v1/generate", json={"prompt": PROMPTS[i % len(PROMPTS)], "max_tokens": max_tokens},
                                     headers={"X-API-Key": "key-bench"})
                    d = r.json() if r.status_code == 200 else {}
                    res.append({"status": r.status_code, "s": time.perf_counter() - t, "tokens": d.get("tokens", 0),
                                "tps": d.get("tokens_per_second"), "cold_start_s": d.get("cold_start_s", 0)})
            t0 = time.perf_counter()
            await asyncio.gather(*(one(i) for i in range(n)))
            return res, time.perf_counter() - t0
    return asyncio.run(run())


def llm_throughput(replicas):
    helm({**eta_values(**{"eta.replicas": 2}), "llm.enabled": True, "llm.replicas": replicas, "llm.scaleToZero": False})
    gen(2, 1, 16)                                          # warm
    t_end_q = time.time()
    res, wall = gen(24, 4)
    time.sleep(15)
    cpu = prom_value(f'sum(increase(container_cpu_usage_seconds_total{{namespace="{NS}",container="llm"}}[{int(wall) + 15}s]))')
    toks = sum(r["tokens"] for r in res)
    return {"replicas": replicas, "requests": len(res), "concurrency": 4, "failed": sum(1 for r in res if r["status"] != 200),
            "tokens": toks, "wall_s": round(wall, 1), "throughput_tokens_per_s": round(toks / wall, 1),
            "per_request_decode_tokens_per_s_median": round(float(np.median([r["tps"] for r in res if r["tps"]])), 1),
            "latency_p50_s": round(float(np.median([r["s"] for r in res])), 2),
            "llm_cpu_seconds": round(cpu, 1),
            "cpu_seconds_per_1m_tokens": round(cpu / toks * 1e6, 0) if toks else None}


def e6_llm(idle_s=45):
    out = {"model": "Qwen2.5-0.5B-Instruct Q4_K_M (GGUF), llama.cpp server, CPU", "prompts": "synthetic",
           "single_request": None}
    helm({**eta_values(**{"eta.replicas": 2}), "llm.enabled": True, "llm.replicas": 1, "llm.scaleToZero": False})
    gen(2, 1, 16)
    single, _ = gen(5, 1)
    out["single_request"] = {"decode_tokens_per_s_median": round(float(np.median([r["tps"] for r in single])), 1),
                             "latency_s_median_128_tokens": round(float(np.median([r["s"] for r in single])), 2)}
    out["one_replica"] = llm_throughput(1)
    out["two_replicas"] = llm_throughput(2)
    # scale to zero
    helm({**eta_values(**{"eta.replicas": 2}), "llm.enabled": True, "llm.scaleToZero": True, "llm.idleSeconds": idle_s})
    k8s.wait_for(lambda: k8s.ready_replicas("deployment", "llm") == 0, 120, what="llm at zero")
    cycles = []
    for i in range(3):
        if i:
            t_idle = time.time()
            k8s.wait_for(lambda: sh(f"kubectl -n {NS} get deploy llm -o jsonpath='{{.spec.replicas}}'") == "0", 300, 2, "scale to zero")
            scaled_down_after = time.time() - t_idle
        else:
            scaled_down_after = None
        cold, _ = gen(1, 1, 32)
        warm, _ = gen(2, 1, 32)
        cycles.append({"cold_start_s": cold[0]["cold_start_s"], "first_request_total_s": round(cold[0]["s"], 2),
                       "warm_request_s_median": round(float(np.median([w["s"] for w in warm])), 2),
                       "seconds_idle_until_scaled_to_zero": round(scaled_down_after, 1) if scaled_down_after else None,
                       "status": [cold[0]["status"]] + [w["status"] for w in warm]})
    req_cpu = 1.0
    out["scale_to_zero"] = {"idle_seconds_setting": idle_s, "cycles": cycles,
                            "cold_start_s_median": round(float(np.median([c["cold_start_s"] for c in cycles])), 2),
                            "reserved_vcpu_per_replica": req_cpu,
                            "idle_cost_avoided_usd_per_hour": round(req_cpu * PRICE_PER_VCPU_HOUR, 4),
                            "idle_cost_avoided_usd_per_idle_day": round(req_cpu * PRICE_PER_VCPU_HOUR * 24, 3)}
    helm({**eta_values(**{"eta.replicas": 2}), "llm.enabled": False})
    return out


# ---------------------------------------------------------------- E7 unit economics (ETA)
def e7_unit_costs(rps=100, secs=60):
    helm(eta_values(**{"eta.replicas": 2}))
    go(GW, [Stream("bench", "key-bench", 20, 10)])
    time.sleep(15)
    rs = go(GW, [Stream("bench", "key-bench", rps, secs)])
    time.sleep(12)
    w = secs + 12
    cpu = {c: prom_value(f'sum(increase(container_cpu_usage_seconds_total{{namespace="{NS}",container="{c}"}}[{w}s]))')
           for c in ("eta", "gateway", "redis", "postgres")}
    preds = prom_value(f'sum(increase(eta_predictions_total{{namespace="{NS}"}}[{w}s]))')
    ok = sum(1 for r in rs if r.status == 200)
    per1k = {c: round(v / ok * 1000, 3) for c, v in cpu.items()} if ok else {}
    total = sum(per1k.values())
    return {"rps": rps, "seconds": secs, "successful_requests": ok, "eta_predictions_counted": preds,
            "cpu_seconds_per_1k_predictions": per1k, "cpu_seconds_per_1k_predictions_full_path": round(total, 3),
            "usd_per_1m_predictions_full_path": round(total * 1000 / 3600 * PRICE_PER_VCPU_HOUR, 4),
            "price_assumption": f"${PRICE_PER_VCPU_HOUR:.4f} per vCPU-hour (AWS c7i.large on-demand us-east-1 / 2 vCPU)"}
