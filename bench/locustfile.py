"""Steady traffic for the canary rollout: Locust users at a constant per-user throughput.
Every response's model version is counted so the canary progression is visible.

locust -f bench/locustfile.py --headless -u 60 -r 60 -t 180s --host http://localhost:30080 --csv results/canary"""
import json
import os
import random
import time
from collections import Counter

from locust import FastHttpUser, constant_throughput, events, task

ROWS = json.load(open("data/sample_features.json"))["rows"]
KEY = os.environ.get("API_KEY", "key-tenant-b")
SERIES = []           # (second since start, version or status)
T0 = time.time()


class EtaUser(FastHttpUser):
    wait_time = constant_throughput(1)

    @task
    def eta(self):
        with self.client.post("/v1/eta", json={"rows": [random.choice(ROWS)]}, headers={"X-API-Key": KEY},
                              catch_response=True, name="eta") as r:
            if r.status_code == 200:
                SERIES.append((int(time.time() - T0), r.json().get("version", "?")))
                r.success()
            else:
                SERIES.append((int(time.time() - T0), f"error:{r.status_code}"))
                r.failure(f"status {r.status_code}")


@events.quitting.add_listener
def dump(environment, **_):
    out = os.environ.get("SERIES_OUT")
    if out:
        json.dump({"t0": T0, "series": SERIES, "by_label": Counter(v for _, v in SERIES)}, open(out, "w"))
