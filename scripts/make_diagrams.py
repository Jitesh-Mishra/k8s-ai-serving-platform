"""Architecture diagram for the README, light + dark.   python scripts/make_diagrams.py"""
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "docs" / "img"

THEMES = {
    "light": dict(bg="#ffffff", card="#f6f8fa", border="#d0d7de", ink="#1f2328", ink2="#59636e", faint="#e6e9ed",
                  a="#2a78d6", a_soft="#e3eefb", b="#eb6834", b_soft="#fde8de", c="#1baf7a", c_soft="#dcf4ea",
                  muted="#afb8c1"),
    "dark": dict(bg="#0d1117", card="#161b22", border="#30363d", ink="#e6edf3", ink2="#9198a1", faint="#21262d",
                 a="#3987e5", a_soft="#13263d", b="#d95926", b_soft="#3a1d12", c="#199e70", c_soft="#0f2e23",
                 muted="#484f58"),
}
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SVG:
    def __init__(self, w, h, t, label):
        self.w, self.h, self.t, self.label, self.parts = w, h, t, label, []

    def add(self, s):
        self.parts.append(s)

    def text(self, x, y, s, size=13, color=None, weight=400, anchor="start", mono=False, italic=False):
        self.add(f'<text x="{x}" y="{y}" font-family="{MONO if mono else FONT}" font-size="{size}" '
                 f'font-weight="{weight}" fill="{color or self.t["ink"]}" text-anchor="{anchor}"'
                 f'{" font-style=\"italic\"" if italic else ""}>{esc(s)}</text>')

    def rect(self, x, y, w, h, fill, stroke=None, rx=10, dash=None, sw=1.5):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" '
                 f'stroke="{stroke or "none"}" stroke-width="{sw}"{d}/>')

    def line(self, x1, y1, x2, y2, color=None, sw=1.5, dash=None, arrow=False, marker="ar"):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        m = f' marker-end="url(#{marker})"' if arrow else ""
        self.add(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color or self.t["ink2"]}" '
                 f'stroke-width="{sw}" stroke-linecap="round"{d}{m}/>')

    def path(self, d, color=None, sw=1.5, dash=None, arrow=False, marker="ar"):
        da = f' stroke-dasharray="{dash}"' if dash else ""
        m = f' marker-end="url(#{marker})"' if arrow else ""
        self.add(f'<path d="{d}" fill="none" stroke="{color or self.t["ink2"]}" stroke-width="{sw}" '
                 f'stroke-linecap="round" stroke-linejoin="round"{da}{m}/>')

    def circle(self, x, y, r, fill, stroke=None, sw=1.5):
        self.add(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{fill}" stroke="{stroke or "none"}" stroke-width="{sw}"/>')

    def pill(self, x, y, s, fill, color, size=11.5, anchor="middle", pad=9):
        w = len(s) * size * 0.58 + 2 * pad
        x0 = x - w / 2 if anchor == "middle" else x
        self.rect(x0, y - size - 3, w, size + 10, fill, rx=(size + 10) / 2)
        self.text(x0 + w / 2, y + 1, s, size=size, color=color, weight=600, anchor="middle")

    def node(self, x, y, w, h, title, sub=None, accent=None, dash=None, mono_title=True):
        t = self.t
        fill = t[f"{accent}_soft"] if accent else t["card"]
        stroke = t[accent] if accent else t["border"]
        self.rect(x, y, w, h, fill, stroke, dash=dash)
        cy = y + h / 2 + (-4 if sub else 5)
        self.text(x + w / 2, cy, title, size=14, weight=650, anchor="middle", mono=mono_title)
        if sub:
            for i, s in enumerate(sub if isinstance(sub, list) else [sub]):
                self.text(x + w / 2, cy + 19 + i * 16, s, size=12, color=t["ink2"], anchor="middle")

    def render(self):
        t = self.t
        defs = (f'<defs>'
                f'<marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
                f'<path d="M0,0 L10,5 L0,10 z" fill="{t["ink2"]}"/></marker>'
                f'<marker id="ara" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
                f'<path d="M0,0 L10,5 L0,10 z" fill="{t["a"]}"/></marker>'
                f'<marker id="arb" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
                f'<path d="M0,0 L10,5 L0,10 z" fill="{t["b"]}"/></marker></defs>')
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" width="{self.w}" '
                f'height="{self.h}" role="img" aria-label="{esc(self.label)}">{defs}'
                f'<rect width="{self.w}" height="{self.h}" rx="14" fill="{t["bg"]}" stroke="{t["border"]}"/>'
                + "".join(self.parts) + "</svg>")


def header(s, title, sub):
    s.text(32, 42, title, size=19, weight=700)
    s.text(32, 64, sub, size=13, color=s.t["ink2"])


def architecture(t):
    s = SVG(1080, 620, t, "Tenants call one gateway on a kind Kubernetes cluster. The gateway enforces per-tenant quotas in Redis, "
                          "routes to an autoscaled LightGBM ETA service, a scale-to-zero llama.cpp LLM, or a Redis Streams batch queue, "
                          "and writes every request to PostgreSQL for billing. Prometheus, Grafana, the HPA and Argo Rollouts watch and change it.")
    header(s, "One gateway, three serving paths, metered per tenant",
           "kind (Kubernetes in Docker) on a 4-vCPU CI runner; everything installed with Helm; CPU only.")
    s.node(28, 110, 150, 150, "tenants", ["A (noisy, quota 50/s)", "B, C", "bench"], mono_title=False)
    s.rect(206, 92, 600, 500, t["card"], t["border"], rx=14)
    s.text(222, 118, "namespace ai", size=11, weight=700, color=t["ink2"], mono=True)
    s.node(226, 140, 170, 120, "gateway", ["API key -> tenant", "token bucket quota", "metering, activator"], accent="a")
    s.line(178, 185, 222, 195, arrow=True)
    s.node(440, 132, 170, 80, "eta", ["LightGBM P50/P90", "HPA 1-6 pods"], accent="c")
    s.node(440, 226, 170, 80, "llm", ["llama.cpp, Qwen2.5 0.5B", "scale to zero"])
    s.node(440, 320, 170, 80, "worker x3", ["Redis Streams group", "XAUTOCLAIM on crash"])
    s.node(632, 132, 156, 80, "HPA", ["RPS/pod (adapter)", "+ CPU"], mono_title=False)
    s.node(632, 226, 156, 80, "Argo Rollouts", ["canary v1 -> v2", "20% -> 60% -> 100%"], mono_title=False)
    s.node(226, 440, 170, 120, "redis", ["quotas, job stream,", "LLM idle state"])
    s.node(440, 440, 170, 120, "postgres", ["usage_log, invoices,", "batch_results"])
    s.line(396, 190, 436, 172, arrow=True)
    s.line(396, 210, 436, 262, arrow=True)
    s.path("M311,260 L311,436", arrow=True)
    s.path("M396,470 C416,470 416,372 436,372", arrow=True)
    s.path("M380,260 L380,420 L420,420 L420,520 L436,520", dash="4 4", arrow=True)
    s.text(374, 360, "usage rows", size=10.5, color=t["ink2"], anchor="end")
    s.line(525, 400, 525, 436, arrow=True)
    s.text(532, 424, "results", size=10.5, color=t["ink2"])
    s.line(610, 172, 628, 172, t["a"], dash="4 4", arrow=True, marker="ara")
    s.line(628, 266, 610, 172, t["a"], dash="4 4", arrow=True, marker="ara")
    s.text(320, 290, "XADD", size=11, color=t["ink2"])
    s.rect(830, 92, 222, 500, t["card"], t["border"], rx=14)
    s.text(846, 118, "namespace monitoring", size=11, weight=700, color=t["ink2"], mono=True)
    s.node(846, 140, 190, 80, "Prometheus", ["5 s scrapes, cAdvisor"], accent="b", mono_title=False)
    s.node(846, 240, 190, 70, "Grafana", ["dashboard as code"], mono_title=False)
    s.node(846, 330, 190, 70, "prometheus-adapter", ["custom.metrics API"], mono_title=False)
    s.node(846, 420, 190, 70, "metrics-server", ["CPU for HPA"], mono_title=False)
    s.path("M806,340 L826,340", color=t["b"], dash="4 4")
    s.text(940, 528, "reconcile: usage_log", size=11.5, color=t["ink2"], anchor="middle")
    s.text(940, 546, "vs Prometheus counters", size=11.5, color=t["ink2"], anchor="middle")
    s.path("M610,500 C720,520 760,537 860,537", color=t["b"], dash="4 4", arrow=True, marker="arb")
    return s.render()


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, t in THEMES.items():
        (OUT / f"architecture-{name}.svg").write_text(architecture(t))
    print("ok")
