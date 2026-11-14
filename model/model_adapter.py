"""
نقطة ربط الموديل (Model adapter).

الباك اند بيكلم الموديل من هنا بس، فاستبدال الموديل = تغيير MODEL_BACKEND من غير ما تلمس باقي الكود.

MODEL_BACKEND =
  builtin  (الافتراضي) تراكر فيزيائي بسيط: بيربط النقط بين الـ epochs ويلاقي اللي بيتحرك.
  custom   بيستورد custom_model.py (جنب app.py) ويستدعي predict(payload) -> dict
  http     بيبعت الـ payload كـ JSON لـ MODEL_URL (سيرفس الموديل بتاعك) ويرجّع ردّه

الـ payload (بعد التحقق في app.py):
  ra, dec          مركز الحقل بالدرجات
  fov              حجم الحقل بالثواني القوسية
  sources          [{epoch, x, y, flux}]  x = ثواني قوسية ناحية الغرب (RA بيقل)، y = ناحية الشمال
  tol, max_pm, min_pm, min_snr, ref_epoch   اختيارية

الرد المطلوب من أي backend:
  {"tracks": [{"ra":.., "dec":.., "pm_arcsec_yr":.., ...}], "n_static": int}
"""
import importlib
import json
import math
import os
import urllib.error
import urllib.request

import numpy as np


class ModelError(Exception):
    pass


BACKEND = os.getenv("MODEL_BACKEND", "builtin").lower()


def info():
    if BACKEND == "builtin":
        return {"name": "planetx-linear-tracker", "version": "1.0", "backend": "builtin",
                "description": "Linear-motion tracklet linking over all epochs (baseline, no ML)"}
    if BACKEND == "custom":
        mod = _custom()
        return {"backend": "custom", **getattr(mod, "INFO", {"name": "custom_model"})}
    if BACKEND == "http":
        return {"backend": "http", "name": "remote-model", "url_set": bool(os.getenv("MODEL_URL"))}
    return {"backend": BACKEND, "name": "unknown"}


def analyze(payload):
    if BACKEND == "builtin":
        out = track(payload)
    elif BACKEND == "custom":
        out = _custom().predict(payload)
    elif BACKEND == "http":
        out = _http(payload)
    else:
        raise ModelError(f"MODEL_BACKEND غير معروف: {BACKEND}")
    return _normalize(out)


# ---------------------------------------------------------------- plug-ins
def _custom():
    try:
        mod = importlib.import_module("custom_model")
    except ImportError as e:
        raise ModelError("MODEL_BACKEND=custom لكن custom_model.py مش موجود أو فيه خطأ استيراد: %s" % e)
    if not hasattr(mod, "predict"):
        raise ModelError("custom_model.py لازم يعرّف predict(payload)")
    return mod


def _http(payload):
    url = os.getenv("MODEL_URL")
    if not url:
        raise ModelError("MODEL_URL غير مضبوط")
    headers = {"Content-Type": "application/json"}
    if os.getenv("MODEL_API_KEY"):
        headers["Authorization"] = "Bearer " + os.environ["MODEL_API_KEY"]
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=float(os.getenv("MODEL_TIMEOUT", "30"))) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise ModelError("سيرفس الموديل مردّش بشكل صحيح: %s" % e)


def _normalize(out):
    if not isinstance(out, dict) or not isinstance(out.get("tracks"), list):
        raise ModelError("الموديل لازم يرجّع dict فيه قائمة tracks")
    for t in out["tracks"]:
        if not isinstance(t, dict) or not all(isinstance(t.get(k), (int, float)) for k in ("ra", "dec")):
            raise ModelError("كل track لازم يحتوي ra و dec رقميين")
    out.setdefault("n_static", 0)
    return out


# ---------------------------------------------------------------- builtin tracker
def track(p):
    """
    Tracklet linking: لكل نقطة في أول epoch بنجرب فرضيات حركة خطية (أول→آخر epoch)،
    نعدّ الـ epochs اللي فيها نقطة قريبة من التوقع، نعمل least-squares ونعيد التوقع.
    أي مسار سرعته أقل من min_pm بيتحسب ثابت (نجم).
    الحدود: لازم المصدر يظهر في أول وآخر epoch (anchors).
    """
    tol = float(p.get("tol", 2.0))
    max_pm = float(p.get("max_pm", 20.0))
    min_pm = float(p.get("min_pm", 0.5))
    min_snr = float(p.get("min_snr", 5.0))
    min_frac = 0.7
    S = p["sources"]
    if not S:
        return {"tracks": [], "n_static": 0}

    raw = np.array([[s["epoch"], s["x"], s["y"], s.get("flux", 1.0)] for s in S], float)
    t_uni, ei = np.unique(np.round(raw[:, 0], 4), return_inverse=True)
    K = len(t_uni)
    if K < 3:
        raise ModelError("محتاج 3 epochs على الأقل")
    t, xy, flux = raw[:, 0], raw[:, 1:3], raw[:, 3]
    tk = t_uni  # وقت كل epoch
    base = tk[-1] - tk[0]
    tref = float(p.get("ref_epoch", tk.mean()))
    avail = np.ones(len(raw), bool)
    first = np.where(ei == 0)[0]
    last = np.where(ei == K - 1)[0]
    max_disp = max_pm * base

    def match(pos0, vel, mask):
        """أقرب نقطة متاحة لكل epoch للمسار المتوقع -> (indices)"""
        pred = pos0 + vel * (tk[ei] - tk[0])[:, None]
        d = np.hypot(*(xy - pred).T)
        ok = np.where(mask & (d <= tol))[0]
        if ok.size == 0:
            return ok
        order = np.lexsort((d[ok], ei[ok]))
        ok = ok[order]
        _, firsts = np.unique(ei[ok], return_index=True)
        return ok[firsts]

    def fit(idx):
        tt = tk[ei[idx]] - tref
        A = np.vstack([np.ones_like(tt), tt]).T
        cx = np.linalg.lstsq(A, xy[idx, 0], rcond=None)[0]
        cy = np.linalg.lstsq(A, xy[idx, 1], rcond=None)[0]
        res = np.hypot(xy[idx, 0] - A @ cx, xy[idx, 1] - A @ cy)
        return cx, cy, res, tt

    tracks, n_static = [], 0
    for i in first:
        if not avail[i]:
            continue
        best = None
        d_last = np.hypot(*(xy[last] - xy[i]).T)
        for j in last[(d_last <= max_disp) & avail[last]]:
            vel = (xy[j] - xy[i]) / base
            idx = match(xy[i], vel, avail)
            if best is None or len(idx) > len(best):
                best = idx
        if best is None or len(best) < min_frac * K:
            continue
        for _ in range(2):  # refine: least-squares → re-match
            cx, cy, res, tt = fit(best)
            pos0 = np.array([cx[0] - cx[1] * (tk[0] - tref), cy[0] - cy[1] * (tk[0] - tref)])
            nb = match(pos0, np.array([cx[1], cy[1]]), avail)
            if len(nb) >= len(best):
                best = nb
        cx, cy, res, tt = fit(best)
        avail[best] = False
        pm = math.hypot(cx[1], cy[1])
        rms = float(np.sqrt(np.mean(res ** 2)))
        sig_v = max(rms, 0.05) / max(math.sqrt(float(np.sum((tt - tt.mean()) ** 2))), 1e-6)
        if pm < min_pm or pm / sig_v < min_snr:  # بطيء أو الحركة جوه الضوضاء => نجم ثابت
            n_static += 1
            continue
        x, y = float(cx[0]), float(cy[0])
        ra0, dec0 = float(p.get("ra", 0.0)), float(p.get("dec", 0.0))
        tracks.append({
            "x": round(x, 3), "y": round(y, 3),
            "ra": round((ra0 - x / 3600 / (math.cos(math.radians(dec0)) or 1)) % 360, 6),
            "dec": round(dec0 + y / 3600, 6),
            "pm_x": round(float(cx[1]), 3), "pm_y": round(float(cy[1]), 3),
            "pm_arcsec_yr": round(pm, 3),
            "pa_deg": round((math.degrees(math.atan2(-cx[1], cy[1])) + 360) % 360, 1),
            "n_epochs": int(len(best)), "n_total_epochs": K,
            "rms_arcsec": round(rms, 3),
            "pm_snr": round(pm / sig_v, 1),
            "flux": round(float(np.median(flux[best])), 3),
            "score": round(len(best) / K / (1 + rms / tol), 3),
            "ref_epoch": round(tref, 3),
            "label": "mover",
        })
    tracks.sort(key=lambda r: -r["pm_arcsec_yr"])
    for n, r in enumerate(tracks, 1):
        r["id"] = f"M{n}"
    return {"tracks": tracks, "n_static": n_static,
            "params": {"tol": tol, "max_pm": max_pm, "min_pm": min_pm, "min_snr": min_snr, "epochs": K}}
