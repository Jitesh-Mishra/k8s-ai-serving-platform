# Probe prep

Questions an interviewer is likely to ask, with answers grounded in this repo's measured results.

## Architecture

**Why a gateway in front of the model instead of calling the model service directly?**
The tenant concerns live in one place: API key auth, quotas, metering and the scale-to-zero activator. The model pods stay stateless and scale on their own. Cost: the gateway is 2.29 of the 3.70 CPU-seconds per 1K predictions (E7), more than the model.

**Why HPA on requests per second and not CPU?**
Two reasons, both observed. The CPU spike while a pod loads the model tripped the CPU target, so the HPA test started at 2 pods. And the CPU requests had to be small to fit 6 ETA pods, the LLM and monitoring on one node, which makes CPU utilisation percentages noisy. Requests/s maps directly to what was measured: one pod is fine at 150 req/s, so a 40 req/s target leaves headroom.

**Why does it take 28 to 40 s to get a new pod?**
prometheus-adapter computes the rate over a 30 s window, the HPA syncs every 15 s, and the pod has to start and load the model. To react faster: shorter rate window, predictive scaling, or a warm spare replica (min replicas 2).

**Why does one pod stop at about 150 req/s?**
Its 200m CPU limit. E7 measured 1.1 CPU-ms per prediction in the ETA container, so 150 req/s is about 0.17 cores and 200 req/s about 0.22, over the limit. That's an inference; I'd confirm it with `container_cpu_cfs_throttled_periods_total`.

**What breaks first at 250 req/s?**
The node. 4 vCPUs shared by everything, including the load generator. Six ETA pods can't help when there's no CPU left to give them. Adding nodes, not pods, is the fix.

## Multi-tenancy

**Did the quotas work?**
As a limiter, yes; as isolation, no. At a 120 req/s flood they throttled 1,637 of A's requests, at 200 req/s 3,522. B and C's p99 still went from about 13 ms to 72-94 s, quotas on or off. The odd part: with quotas on at 120 req/s, the model pod should only have seen about 110 req/s, under the 150 one pod held in E1. So the backlog formed upstream or across the shared node (phases ran 110-160 s instead of 60). I haven't isolated it. Next: load generator off the node, CFS throttling metrics per pod, and rejecting at the ingress so a 429 is nearly free.

**How would you fix isolation properly?**
Reject earlier and cheaper (at the ingress or load balancer, rate limit by key before Python runs). Give tenants separate capacity (per-tenant Deployments or node pools, priority classes). Use admission control in the model server: shed load by tenant priority when queue depth rises.

**Why a Redis Lua script for the token bucket?**
Atomic read-modify-write across all gateway replicas in one round trip. A per-replica in-memory bucket would let a tenant get N times its quota with N replicas.

## Metering

**How do you know billing is right?**
Every request writes a usage row to PostgreSQL and increments a Prometheus counter. Reconciliation compares them per gateway pod, tenant, model and status: 6,300 vs 6,300, 0 mismatched. The crash tests compare client-observed successes with billed rows: 2,000/2,000 with a graceful restart and with SIGKILL.

**Can a crash lose billing rows?**
Yes, up to one 200 ms buffer on SIGKILL. The test didn't lose any, which says the window is small, not zero. For exact billing: write-ahead to Redis Streams, or bill from the counter and use the log for detail.

## Batch

**What does at-least-once mean here, and how do you avoid double-processing?**
A job can run twice (worker dies after computing, before XACK), but it's recorded once: the insert is `ON CONFLICT (job_id) DO NOTHING`. E4: 3,000 jobs, 2 workers killed, 12 reclaimed, 0 lost, 0 recorded twice.

## LLM and cost

**How did you get dollars per 1M tokens?**
CPU-seconds the llama.cpp container used during the load test (Prometheus `container_cpu_usage_seconds_total`), divided by tokens generated: 46,806 CPU-s per 1M tokens. Times $0.0446 per vCPU-hour (c7i.large on-demand list price / 2) gives $0.58. It's CPU time used, not reserved capacity, so it assumes the node is otherwise fully used.

**Why is 2 replicas more expensive per token?**
Both share the same 4 cores, so throughput rose only 20% (50.3 to 60.2 tok/s) while CPU used rose 38%. More replicas only help with more cores.

**What does the 3 s cold start include?**
Scale API call, scheduling, container start, model load and llama.cpp health. Not image pull or model download (preloaded). In a real cluster with a 0.5B model on a cold node, add image pull and the GGUF download.

**Would this transfer to GPUs?**
The control plane would (gateway, quotas, metering, autoscaling signals). The numbers would not. On GPUs you'd scale on queue depth or KV-cache use, batch requests (vLLM continuous batching), and cold start is dominated by loading weights into GPU memory.

## Weak spots to own
- One node, shared with the load generator: absolute capacity numbers are low and contended.
- Canary ablation is n=1 (1 vs 5 failures of 9,000).
- Gateway quota isolation failed at both flood rates and the root cause is not isolated; say so and describe the next experiment.
- Grafana dashboard exists but screenshots from the headless runner rendered blank lines.
