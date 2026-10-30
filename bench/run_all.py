"""Run the experiments in order and write results/<name>.json (errors are recorded, not fatal).
python -m bench.run_all --only e1,e2"""
from __future__ import annotations

import argparse
import json
import os
import time
import traceback

from . import experiments as E

ORDER = ["e7_unit_costs", "e1_autoscale", "e2_noisy_neighbour", "e3_metering", "e4_batch", "e5_canary", "e6_llm"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    os.makedirs(E.OUT, exist_ok=True)
    want = [x for x in ORDER if not a.only or any(x.startswith(p) for p in a.only.split(","))]
    summary = {}
    for name in want:
        t = time.time()
        print(f"=== {name}", flush=True)
        try:
            res = getattr(E, name)()
            status = "ok"
        except Exception as e:
            res, status = {"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()}, "error"
        res["_seconds"] = round(time.time() - t, 1)
        json.dump(res, open(f"{E.OUT}/{name}.json", "w"), indent=1, default=str)
        summary[name] = {"status": status, "seconds": res["_seconds"]}
        print(f"=== {name}: {status} in {res['_seconds']} s", flush=True)
    json.dump(summary, open(f"{E.OUT}/summary.json", "w"), indent=1)


if __name__ == "__main__":
    main()
