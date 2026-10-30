# Decisions

**D1 Where it runs: kind in GitHub Actions**
- The build sandbox has no Docker daemon, so the cluster runs on a GitHub-hosted runner: 4 vCPU and 16 GB RAM, one kind node.
- The same runner also drives the load, so every number is from a contended single machine. The numbers show relative behaviour (single pod vs HPA, quotas on vs off), not cloud capacity.
- `bench.yml` installs everything from scratch with Helm and commits raw results to the `bench-results` branch.

**D2 The model**
- The LightGBM P50/P90 pickup-ETA boosters come from github.com/Jitesh-Mishra/nyc-rideshare-pickup-eta.
- The load test sends 2,000 real feature rows sampled from that project's test window (NYC TLC HVFHV, March 2025).
- Serving here is stateless: each request carries its feature row.
- "v2" for the canary is the same boosters truncated to 200 trees. It is a stand-in model change, not a better model.

**D3 Open-loop load generation**
- `bench/loadgen.py` schedules every request at a fixed time, whether or not earlier ones returned. An overloaded pod therefore shows up as latency and errors, with no coordinated omission.
- Locust (closed loop, constant throughput per user) is used only for the canary, where the point is counting failed requests during a rollout.

**D4 HPA on requests per second, not CPU**
- `prometheus-adapter` exposes `eta_requests_per_second` from the pods' own counters. The target is 40 req/s per pod, min 1, max 6, with no scale-up stabilization.
- CPU was dropped as a scaling signal for two reasons:
  - The model-load CPU spike at pod start tripped the CPU target, so the first run started the HPA test at 2 pods.
  - Small CPU requests are needed to fit 6 ETA pods, the LLM and the monitoring stack on one 4-vCPU node.

**D5 Multi-tenancy**
- API keys map to tenants (a Kubernetes Secret).
- A per-tenant token bucket lives in Redis as one atomic Lua script, shared by every gateway replica.
- Over-quota requests get 429 and are still logged (billed as throttled, 0 units).
- Isolation is at the gateway. Tenants share the serving pods, the realistic setup for a shared model API.

**D6 Metering and billing**
- Every answered request writes one row to PostgreSQL `usage_log` (tenant, model, units, latency, status, gateway pod). Rows are buffered and flushed every 200 ms.
- Invoices are a SQL view over this log.
- Reconciliation compares the SQL counts with the Prometheus counters per gateway pod, tenant, model and status. Both cover the same pod lifetime, so they must match exactly.
- The SIGKILL test checks what a crash can lose, comparing client-observed successes with billed rows.

**D7 At-least-once batch inference**
- Jobs go to a Redis Streams consumer group. Workers write the result to PostgreSQL with `ON CONFLICT (job_id)`, then `XACK`.
- A worker that dies leaves its message pending, and live workers `XAUTOCLAIM` anything idle for 15 s.
- So a job can run twice but is recorded once, and `attempts` counts the re-runs.

**D8 Scale-to-zero without Knative**
- The gateway is a small activator. If the LLM Deployment has 0 replicas, it patches the scale to 1 (Role limited to `deployments/scale` on `llm`) and waits for llama.cpp `/health`.
- A reaper scales back to 0 after 45 s idle with nothing in flight. Its state is in Redis, so any gateway replica can do it.
- Cold start therefore includes scheduling, container start and model load. The image is preloaded into kind and the GGUF is on a hostPath, so it excludes image pull and download.

**D9 Bugs the first full run found**
1. 53 of 2,000 sample rows had NaN features. httpx refuses NaN in JSON, so 2.65% of requests returned 500, and about 12% of 5-row batch jobs. Missing values now travel as null.
2. The LLM pod was Pending: CPU requests did not fit the single node. Requests were cut; limits are unchanged.
3. The 400 req/s noisy-neighbour flood saturated the whole runner, so even quota-protected tenants saw multi-second p99. The test now uses 300 req/s against 1 ETA pod.

**D10 Prices**
- Unit costs convert measured CPU-seconds at $0.0892/h for a 2-vCPU AWS c7i.large (on-demand, us-east-1, Linux, third-party calculator, checked 2026-10-30). That is $0.0446 per vCPU-hour.
- This is an assumption for scale, not a quote. Invoice prices ($0.10 per 1K ETA rows, and so on) are made-up examples.

**D11 Runs 2 to 4**
- Run 2's HPA test still started at 2 pods after CPU was dropped as a signal: the adapter's 30 s rate window still held the previous 300 req/s step from the single-pod test. Run 3 adds a 60 s idle gap and starts at 1 pod.
- The token-bucket "overshoot" in the local test was not float truncation (Redis 7 keeps the fraction). Queued requests were admitted over a longer wall time than the test assumed. The test was loosened, the code kept.
- Noisy neighbour at 200 req/s (run 3) and 120 req/s (run 4): quotas throttled tenant A but B and C still went to 70-90 s p99, quotas on or off. Not isolated yet. Candidate causes: the load generator sharing the node, the 3 gateway replicas, CFS throttling of the 200m ETA limit under bursts.
- Grafana screenshots from the headless runner render panels without lines, even after a 25 s wait. The dashboard ships; screenshots are not used.
