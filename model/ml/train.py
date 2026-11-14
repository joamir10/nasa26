"""Simulate noisy detection fields, train the tracklet classifier, compare with the rule-based tracker.
Run from the project root:  python ml/train.py
"""
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import custom_model as cm  # noqa: E402

FOV = 240.0


def make_field(rng, sigma0=0.8):
    K = int(rng.integers(8, 25))
    ep = np.sort(np.concatenate([[2025.35, 2026.85], rng.uniform(2025.35, 2026.85, K - 2)]))
    ref = float(ep.mean())
    objs = [(rng.uniform(-FOV / 2, FOV / 2), rng.uniform(-FOV / 2, FOV / 2), 0.0, 0.0, math.exp(rng.normal(-0.3, 1.0)), 0)
            for _ in range(int(rng.integers(60, 160)))]
    for _ in range(int(rng.integers(0, 4))):
        sp, th = rng.uniform(1.5, 15), rng.uniform(0, 2 * math.pi)
        objs.append((rng.uniform(-FOV / 2, FOV / 2), rng.uniform(-FOV / 2, FOV / 2), sp * math.cos(th), sp * math.sin(th),
                     math.exp(rng.normal(0.2, 0.7)), 1))
    src, lam = [], rng.uniform(0, 6)
    for t in ep:
        for x0, y0, vx, vy, f, _ in objs:
            if rng.random() > 1 / (1 + math.exp(-3 * (math.log(f) + 0.6))):   # faint sources are missed
                continue
            s = sigma0 * min(3.0, 1 / math.sqrt(f))                           # faint sources: worse astrometry
            src.append(dict(epoch=float(t), x=x0 + vx * (t - ref) + rng.normal(0, s), y=y0 + vy * (t - ref) + rng.normal(0, s),
                            flux=f * math.exp(rng.normal(0, .1))))
        for _ in range(rng.poisson(lam)):                                      # spurious detections
            src.append(dict(epoch=float(t), x=rng.uniform(-FOV / 2, FOV / 2), y=rng.uniform(-FOV / 2, FOV / 2), flux=math.exp(rng.normal(-1.2, .4))))
    truth = [(o[0], o[1], o[2], o[3]) for o in objs if o[5]]
    return dict(ra=180.0, dec=0.0, fov=FOV, sources=src, ref_epoch=ref, max_pm=20.0), truth


def label(c, truth):
    return int(any(math.hypot(c["x"] - x, c["y"] - y) < 3 and math.hypot(c["pm_x"] - vx, c["pm_y"] - vy) < 1.5 for x, y, vx, vy in truth))


def dataset(n, sigmas, seed):
    rng, X, Y, F = np.random.default_rng(seed), [], [], []
    for i in range(n):
        pay, truth = make_field(rng, sigmas[i % len(sigmas)])
        cands = cm.candidates(pay)["tracks"]
        for c in cands:
            X.append(cm.feat(c, len(pay["sources"]), FOV)); Y.append(label(c, truth))
            F.append((i, c["pm_arcsec_yr"] >= .5 and c["pm_snr"] >= 5, len(truth)))
    return np.array(X), np.array(Y), F


def field_metrics(pred, Y, F):
    """precision over passing candidates; recall over true movers (each field counted once per mover)."""
    tp = int(((pred == 1) & (Y == 1)).sum()); fp = int(((pred == 1) & (Y == 0)).sum())
    n_truth = sum({f[0]: f[2] for f in F}.values())
    return dict(precision=round(tp / max(tp + fp, 1), 3), recall=round(tp / max(n_truth, 1), 3), false_positives=fp, true_positives=tp, n_true_movers=n_truth)


if __name__ == "__main__":
    from sklearn.neural_network import MLPClassifier
    from sklearn.preprocessing import StandardScaler
    SIG = [0.5, 0.8, 1.2, 1.8, 2.5, 3.5]
    Xtr, Ytr, _ = dataset(240, SIG, 1); Xva, Yva, _ = dataset(80, SIG, 2)
    sc = StandardScaler().fit(Xtr)
    clf = MLPClassifier((16, 16), max_iter=800, random_state=0, alpha=1e-3).fit(sc.transform(Xtr), Ytr)
    pv = clf.predict_proba(sc.transform(Xva))[:, 1]
    ths = np.linspace(.05, .95, 19); f1 = []
    for t in ths:
        p = pv >= t; tp = (p & (Yva == 1)).sum(); f1.append(2 * tp / max(p.sum() + (Yva == 1).sum(), 1))
    thr = float(ths[int(np.argmax(f1))])
    layers = [[w.tolist(), b.tolist()] for w, b in zip(clf.coefs_, clf.intercepts_)]
    os.makedirs("models", exist_ok=True)
    json.dump(dict(mu=sc.mean_.tolist(), sd=sc.scale_.tolist(), layers=layers, threshold=thr), open("models/tracklet_mlp.json", "w"))
    cm._W = None
    # held-out test: new seeds, including an out-of-distribution noise level (sigma 2.5, never seen in training)
    ev = {"threshold": thr, "n_train_candidates": int(len(Ytr)), "positive_rate_train": round(float(Ytr.mean()), 3), "test": {}}
    for sig in (0.5, 1.2, 2.5, 5.0):   # 5.0 is outside the training range
        X, Y, F = dataset(60, [sig], 100 + int(sig * 10))
        ml = np.array([cm.prob(x) >= thr for x in X]).astype(int); rule = np.array([f[1] for f in F]).astype(int)
        nt = field_metrics(ml, Y, F)["n_true_movers"]
        ev["test"][f"sigma_{sig}"] = {"ml": field_metrics(ml, Y, F), "rule_based": field_metrics(rule, Y, F),
                                      "candidate_recall_ceiling_approx": round(min(1.0, float(Y.sum()) / max(nt, 1)), 3)}
    json.dump(ev, open("models/eval.json", "w"), indent=1)
    print(json.dumps(ev, indent=1))
