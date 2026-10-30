"""README figures (light + dark) from reports/*.json.   python scripts/make_figures.py"""
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

R = Path("reports")
OUT = Path("docs/img")
OUT.mkdir(parents=True, exist_ok=True)
TH = {"light": dict(bg="#ffffff", ink="#1f2328", ink2="#59636e", grid="#e6e9ed", c=["#2a78d6", "#eb6834", "#1baf7a"]),
      "dark": dict(bg="#0d1117", ink="#e6edf3", ink2="#9198a1", grid="#21262d", c=["#3987e5", "#d95926", "#199e70"])}


def style(ax, t):
    ax.set_facecolor(t["bg"]); ax.grid(color=t["grid"], lw=0.8); ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)


e1 = json.loads((R / "e1_autoscale.json").read_text())
for name, t in TH.items():
    plt.rcParams.update({"font.size": 10, "axes.edgecolor": t["ink2"], "axes.labelcolor": t["ink"], "xtick.color": t["ink2"],
                         "ytick.color": t["ink2"], "text.color": t["ink"]})
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 3.8), facecolor=t["bg"], gridspec_kw={"width_ratios": [1.15, 1]})
    for i, (k, lab) in enumerate([("single_pod", "1 pod, no autoscaling"), ("hpa", "HPA (req/s metric)")]):
        rows = e1[k]
        a1.plot([r["target_rps"] for r in rows], [r["p99_ms"] or float("nan") for r in rows], "o-", color=t["c"][i], label=lab, lw=2)
    a1.axhline(100, color=t["ink2"], ls="--", lw=1)
    a1.text(e1["stages_rps"][0], 104, "SLO 100 ms", color=t["ink2"], fontsize=9)
    a1.set_yscale("log"); a1.set_xlabel("offered load (req/s, open loop)"); a1.set_ylabel("p99 latency (ms)")
    a1.set_title("ETA p99 through the gateway", loc="left"); a1.legend(frameon=False); style(a1, t)
    tl = e1["replica_timeline"]
    a2.step([s["t"] for s in tl], [s["ready"] for s in tl], where="post", color=t["c"][1], lw=2, label="ready replicas")
    ax3 = a2.twinx()
    xs, ys = [], []
    for i, rps in enumerate(e1["stages_rps"]):
        xs += [i * 40, (i + 1) * 40]; ys += [rps, rps]
    ax3.plot(xs, ys, color=t["ink2"], lw=1, ls=":", label="offered req/s")
    ax3.set_ylabel("offered req/s", color=t["ink2"]); ax3.tick_params(colors=t["ink2"])
    for s in ("top",):
        ax3.spines[s].set_visible(False)
    a2.set_xlabel("seconds into the step test"); a2.set_ylabel("ready ETA pods")
    a2.set_title("HPA scale-out under the step load", loc="left"); style(a2, t)
    fig.tight_layout(); fig.savefig(OUT / f"autoscale-{name}.png", dpi=150, facecolor=t["bg"]); plt.close(fig)
print("ok")
