"""Screenshot the Grafana dashboard over the benchmark window (anonymous viewer, kiosk mode)."""
import os
import time

from playwright.sync_api import sync_playwright

os.makedirs("results", exist_ok=True)
start = int(os.path.getmtime("results/summary.json") * 1000) if os.path.exists("results/summary.json") else None
url = "http://localhost:30030/d/aiserve/ai-serving-platform?orgId=1&kiosk&from=now-120m&to=now"
with sync_playwright() as p:
    b = p.chromium.launch()
    for theme in ("light", "dark"):
        pg = b.new_page(viewport={"width": 1600, "height": 1300})
        pg.goto(url + f"&theme={theme}", wait_until="networkidle")
        time.sleep(8)
        pg.screenshot(path=f"results/grafana-{theme}.png", full_page=True)
    b.close()
print("ok")
