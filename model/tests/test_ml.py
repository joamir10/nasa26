import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ml"))
import custom_model as cm  # noqa: E402
import model_adapter as ma  # noqa: E402
import train  # noqa: E402


def site_payload(seed=0):
    """Same construction the web page uses for 'Run tracking model': noiseless linear motion, 24 epochs."""
    r = np.random.default_rng(seed)
    ep = np.linspace(2025.35, 2026.85, 24)
    st = [(r.uniform(-120, 120), r.uniform(-120, 120), r.uniform(.3, 2)) for _ in range(120)]
    src = []
    for e in ep:
        src.append(dict(epoch=e, x=(e - 2026.1) * -8.12, y=(e - 2026.1) * .67, flux=1.2))
        src += [dict(epoch=e, x=x, y=y, flux=f) for x, y, f in st]
    return dict(ra=133.76, dec=-7.24, fov=240.0, sources=src, ref_epoch=2026.1, max_pm=12.0)


class MLModel(unittest.TestCase):
    def test_model_file_loads_and_probabilities_valid(self):
        p = cm.prob([0.5, 1.0, 0.2, 0.9, 12, 0.0, 0.5])
        self.assertTrue(0 <= p <= 1)

    def test_site_payload_finds_the_mover(self):
        out = cm.predict(site_payload())
        self.assertEqual(len(out["tracks"]), 1)
        t = out["tracks"][0]
        self.assertAlmostEqual(t["pm_arcsec_yr"], math.hypot(8.12, .67), delta=.3)
        self.assertGreaterEqual(t["ml_prob"], 0.5)

    def test_beats_rule_on_false_positives_with_noise(self):
        rng = np.random.default_rng(777)
        fp_ml = fp_rule = tp_ml = 0
        for _ in range(12):
            pay, truth = train.make_field(rng, 0.8)
            for c in cm.candidates(pay)["tracks"]:
                y = train.label(c, truth)
                ml = cm.prob(cm.feat(c, len(pay["sources"]), 240)) >= cm._load()["thr"]
                rule = c["pm_arcsec_yr"] >= .5 and c["pm_snr"] >= 5
                fp_ml += int(ml and not y); fp_rule += int(rule and not y); tp_ml += int(ml and y)
        self.assertLess(fp_ml, fp_rule)       # fewer false alarms than the rule-based gate
        self.assertGreater(tp_ml, 0)          # and it still finds real movers

    def test_too_few_epochs_is_a_model_error(self):
        pay = site_payload()
        pay["sources"] = [s for s in pay["sources"] if s["epoch"] < 2025.45]
        with self.assertRaises(ma.ModelError):
            cm.predict(pay)

    def test_missing_model_file_is_a_clear_error(self):
        old, cm._W, cm.MODEL_PATH = cm.MODEL_PATH, None, "/nonexistent.json"
        try:
            with self.assertRaises(ma.ModelError):
                cm._load()
        finally:
            cm.MODEL_PATH, cm._W = old, None


if __name__ == "__main__":
    unittest.main()
