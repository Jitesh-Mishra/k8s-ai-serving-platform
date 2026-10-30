# Where each result file comes from

All runs: `.github/workflows/bench.yml` on a GitHub-hosted runner (4 vCPU, 16 GB), one kind node. Raw output is on the `bench-results` branch under `runs/<sha>-<run id>/`; copies are in `reports_runs/`.

| File | Run | Commit | Notes |
|---|---|---|---|
| e3_metering.json, e4_batch.json, e5_canary.json, canary_*, e6_llm.json, e7_unit_costs.json | run 2, Actions run 41014776993 | 69f1d9b | gateway 2 replicas |
| e1_autoscale.json | run 3, Actions run 41021647564 | 816da63 | 60 s idle before the HPA run, so it starts at 1 pod |
| e2_noisy_neighbour_flood200.json | run 3, Actions run 41021647564 | 816da63 | flood 200 req/s, gateway 3 replicas |
| e2_noisy_neighbour_flood120.json | run 4, Actions run 41026519763 | 43fa976 | e2 only, flood 120 req/s, gateway 3 replicas |

Run 1 found the bugs in `docs/decisions.md` D9; none of its numbers are used. Run 2's E1 started the HPA at 2 pods (D11) and is superseded by run 3.

Known correction: `e6_llm.json` reports `reserved_vcpu_per_replica: 1.0` and $1.07 per idle day. The chart requests 0.5 vCPU for the LLM, so the README uses 0.5 vCPU, $0.0223 per hour, $0.54 per idle day.
