"""LightGBM pickup-ETA quantile models (P50, P90) from github.com/Jitesh-Mishra/nyc-rideshare-pickup-eta."""
from __future__ import annotations

import os

import lightgbm as lgb
import numpy as np

MODEL_DIR = os.environ.get("MODEL_DIR", os.path.join(os.path.dirname(__file__), "..", "models"))
N_FEATURES = 19


class ETAModel:
    def __init__(self, version: str = "v1", model_dir: str = MODEL_DIR):
        self.version = version
        self.q50 = lgb.Booster(model_file=os.path.join(model_dir, "lgb_q50.txt"))
        self.q90 = lgb.Booster(model_file=os.path.join(model_dir, "lgb_q90.txt"))
        # v2 is a stand-in "new model version" for the canary: the same boosters truncated to 200 trees
        self.n_iter = None if version == "v1" else 200

    def predict(self, rows) -> list[dict]:
        # JSON has no NaN; missing features arrive as null and LightGBM treats NaN as missing
        x = np.array([[np.nan if v is None else v for v in r] for r in rows], dtype=np.float32)
        if x.ndim != 2 or x.shape[1] != N_FEATURES:
            raise ValueError(f"expected rows of {N_FEATURES} features")
        p50 = self.q50.predict(x, num_iteration=self.n_iter, num_threads=1)
        p90 = self.q90.predict(x, num_iteration=self.n_iter, num_threads=1)
        return [{"p50_min": round(max(float(a), 0.0), 3), "p90_min": round(max(float(b), float(a), 0.0), 3)}
                for a, b in zip(p50, p90)]
