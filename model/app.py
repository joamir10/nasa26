"""Planet X backend: Flask + SQLite. بيخدّم الموقع (public/) والـ API على نفس الدومين."""
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import model_adapter

BASE = Path(__file__).resolve().parent
COOKIE = "px_session"
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
HASH_RE = re.compile(r"^#explore(\?[A-Za-z0-9=&_.,:%+\-]*)?$")  # مسموح روابط الموقع بس (يمنع javascript:)
DUMMY_HASH = generate_password_hash("timing-equalizer")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL, pw_hash TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS candidates(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL, field_name TEXT NOT NULL, source_id TEXT NOT NULL DEFAULT '',
  ra REAL NOT NULL, dec REAL NOT NULL, status TEXT NOT NULL DEFAULT '', data TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(user_id, field_name, source_id, ra, dec));
CREATE TABLE IF NOT EXISTS favorites(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  label TEXT NOT NULL, ra REAL NOT NULL, dec REAL NOT NULL, fov REAL NOT NULL, hash TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')), UNIQUE(user_id, hash));
CREATE TABLE IF NOT EXISTS articles(
  id INTEGER PRIMARY KEY, category TEXT, date_label TEXT, read_time TEXT, title TEXT, image TEXT,
  excerpt TEXT, content TEXT);
CREATE TABLE IF NOT EXISTS analyses(
  id INTEGER PRIMARY KEY, user_id INTEGER, ra REAL, dec REAL, fov REAL, n_sources INTEGER,
  n_tracks INTEGER, backend TEXT, ms INTEGER, created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE INDEX IF NOT EXISTS ix_cand_user ON candidates(user_id);
CREATE INDEX IF NOT EXISTS ix_fav_user ON favorites(user_id);
"""


class ApiError(Exception):
    def __init__(self, msg, status=400, headers=None):
        self.msg, self.status, self.headers = msg, status, headers or {}


class Limiter:
    """Rate limit في الذاكرة (سيرفر بـ worker واحد؛ لو هتكبّر استخدم Redis)."""

    def __init__(self):
        self.hits = defaultdict(deque)

    def check(self, key, limit, window):
        now, q = time.time(), self.hits[key]
        while q and q[0] < now - window:
            q.popleft()
        if len(q) >= limit:
            raise ApiError("طلبات كتير. جرّب بعد شوية.", 429, {"Retry-After": str(int(window))})
        q.append(now)
        if len(self.hits) > 20000:
            self.hits = defaultdict(deque, {k: v for k, v in self.hits.items() if v and v[-1] > now - 3600})


def num(v, name, lo=None, hi=None):
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ApiError(f"{name} لازم يكون رقم")
    try:
        f = float(v)
    except ValueError:
        raise ApiError(f"{name} لازم يكون رقم")
    if not math.isfinite(f) or (lo is not None and f < lo) or (hi is not None and f > hi):
        raise ApiError(f"{name} خارج المدى المسموح")
    return f


def text(v, name, maxlen, required=True):
    v = "" if v is None else str(v).strip()
    if required and not v:
        raise ApiError(f"{name} مطلوب")
    if len(v) > maxlen:
        raise ApiError(f"{name} أطول من المسموح ({maxlen})")
    return v


def create_app(config=None):
    cfg = {
        "DATABASE_PATH": os.getenv("DATABASE_PATH", str(BASE / "data" / "planetx.db")),
        "COOKIE_SECURE": os.getenv("COOKIE_SECURE", "0") == "1",
        "TRUST_PROXY": os.getenv("TRUST_PROXY", "0") == "1",
        "ALLOWED_ORIGINS": [o for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o],
        "MAX_SOURCES": int(os.getenv("MAX_SOURCES", "6000")),
        "RATE_LIMIT": os.getenv("RATE_LIMIT", "1") == "1",
    }
    cfg.update(config or {})
    app = Flask(__name__, static_folder=str(BASE / "public"), static_url_path="")
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
    if cfg["TRUST_PROXY"]:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    limiter = Limiter()

    def limit(key, n, window):
        if cfg["RATE_LIMIT"]:
            limiter.check(key, n, window)

    # ------------------------------------------------------------- db
    Path(cfg["DATABASE_PATH"]).parent.mkdir(parents=True, exist_ok=True)

    def connect():
        c = sqlite3.connect(cfg["DATABASE_PATH"], timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        return c

    def db():
        if "db" not in g:
            g.db = connect()
        return g.db

    with connect() as c0:
        c0.execute("PRAGMA journal_mode=WAL")
        c0.executescript(SCHEMA)
        seed = BASE / "seed_articles.json"
        if seed.exists():  # بنحدّث المقالات من الملف كل تشغيل: عدّل seed_articles.json وأعد النشر
            for a in json.loads(seed.read_text("utf-8")):
                c0.execute(
                    "INSERT INTO articles(id,category,date_label,read_time,title,image,excerpt,content) "
                    "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET category=excluded.category,"
                    "date_label=excluded.date_label,read_time=excluded.read_time,title=excluded.title,"
                    "image=excluded.image,excerpt=excluded.excerpt,content=excluded.content",
                    (a["id"], a["category"], a["date"], a["readTime"], a["title"], a["image"], a["excerpt"],
                     json.dumps(a["content"], ensure_ascii=False)))
    @app.teardown_appcontext
    def close_db(_):
        c = g.pop("db", None)
        if c:
            c.close()

    # ------------------------------------------------------------- auth helpers
    sha = lambda s: hashlib.sha256(s.encode()).hexdigest()

    def current_user():
        if "user" not in g:
            tok, g.user = request.cookies.get(COOKIE), None
            if tok:
                g.user = db().execute(
                    "SELECT u.id,u.email,u.name FROM sessions s JOIN users u ON u.id=s.user_id "
                    "WHERE s.token_hash=? AND s.expires_at>datetime('now')", (sha(tok),)).fetchone()
        return g.user

    def need_user():
        u = current_user()
        if not u:
            raise ApiError("لازم تسجّل دخول", 401)
        return u

    def body():
        if not request.is_json:
            raise ApiError("Content-Type لازم يكون application/json", 415)
        d = request.get_json(silent=True)
        if not isinstance(d, dict):
            raise ApiError("JSON غير صالح")
        return d

    def public_user(u):
        return {"id": u["id"], "name": u["name"], "email": u["email"]}

    def start_session(user_id, remember):
        tok = secrets.token_urlsafe(32)
        days = 30 if remember else 1
        db().execute("DELETE FROM sessions WHERE expires_at<=datetime('now')")
        db().execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,datetime('now',?))",
                     (sha(tok), user_id, f"+{days} days"))
        db().commit()
        return tok, days

    def with_cookie(resp, tok, days, remember):
        resp.set_cookie(COOKIE, tok, max_age=days * 86400 if remember else None, httponly=True,
                        samesite="Lax", secure=cfg["COOKIE_SECURE"], path="/")
        return resp

    # ------------------------------------------------------------- hooks
    @app.before_request
    def csrf_origin_check():
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.path.startswith("/api/"):
            origin = request.headers.get("Origin")
            if origin and urlparse(origin).netloc != request.host and origin not in cfg["ALLOWED_ORIGINS"]:
                raise ApiError("Origin غير مسموح", 403)

    @app.after_request
    def headers(r):
        r.headers.setdefault("X-Content-Type-Options", "nosniff")
        r.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        r.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if cfg["COOKIE_SECURE"]:
            r.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        if request.path.startswith("/api/"):
            r.headers["Cache-Control"] = "no-store"
        return r

    @app.errorhandler(ApiError)
    def on_api_error(e):
        return jsonify(error=e.msg), e.status, e.headers

    @app.errorhandler(HTTPException)
    def on_http_error(e):
        if request.path.startswith("/api/") or e.code != 404:
            msg = {404: "مش موجود", 405: "Method غير مسموح", 413: "الطلب كبير جدًا"}.get(e.code, e.description)
            return jsonify(error=msg), e.code
        return e

    @app.errorhandler(Exception)
    def on_crash(e):
        app.logger.exception("unhandled error")
        return jsonify(error="خطأ داخلي في السيرفر"), 500

    # ------------------------------------------------------------- routes: system
    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/api/health")
    def health():
        db().execute("SELECT 1")
        return jsonify(ok=True, model=model_adapter.info())

    # ------------------------------------------------------------- routes: auth
    @app.post("/api/auth/register")
    def register():
        d = body()
        limit("reg:" + request.remote_addr, 10, 3600)
        name = text(d.get("name"), "الاسم", 60)
        email = text(d.get("email"), "الإيميل", 254).lower()
        pw = d.get("password")
        if not EMAIL_RE.match(email):
            raise ApiError("إيميل غير صالح")
        if not isinstance(pw, str) or not 8 <= len(pw) <= 128:
            raise ApiError("الباسورد لازم يكون من 8 لـ 128 حرف")
        try:
            cur = db().execute("INSERT INTO users(email,name,pw_hash) VALUES(?,?,?)",
                               (email, name, generate_password_hash(pw)))
            db().commit()
        except sqlite3.IntegrityError:
            raise ApiError("الإيميل ده مسجّل قبل كده", 409)
        remember = bool(d.get("remember", True))
        tok, days = start_session(cur.lastrowid, remember)
        u = {"id": cur.lastrowid, "name": name, "email": email}
        return with_cookie(jsonify(user=u), tok, days, remember), 201

    @app.post("/api/auth/login")
    def login():
        d = body()
        email = text(d.get("email"), "الإيميل", 254).lower()
        limit("login-ip:" + request.remote_addr, 40, 600)
        limit("login-mail:" + email, 8, 600)
        pw = d.get("password") if isinstance(d.get("password"), str) else ""
        row = db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        ok = check_password_hash(row["pw_hash"] if row else DUMMY_HASH, pw)
        if not (row and ok):
            raise ApiError("الإيميل أو الباسورد غلط", 401)
        remember = bool(d.get("remember", True))
        tok, days = start_session(row["id"], remember)
        return with_cookie(jsonify(user=public_user(row)), tok, days, remember)

    @app.post("/api/auth/logout")
    def logout():
        tok = request.cookies.get(COOKIE)
        if tok:
            db().execute("DELETE FROM sessions WHERE token_hash=?", (sha(tok),))
            db().commit()
        r = jsonify(ok=True)
        r.delete_cookie(COOKIE, path="/")
        return r

    @app.get("/api/auth/me")
    def me():
        u = current_user()
        return jsonify(user=public_user(u) if u else None)

    # ------------------------------------------------------------- routes: candidates
    def cand_row(r):
        return {"id": r["id"], "name": r["name"], "field": r["field_name"], "source": r["source_id"],
                "ra": r["ra"], "dec": r["dec"], "status": r["status"], "data": json.loads(r["data"]),
                "created_at": r["created_at"]}

    def insert_candidate(uid, c):
        data = c.get("data") if isinstance(c.get("data"), dict) else {}
        blob = json.dumps(data, ensure_ascii=False)
        if len(blob) > 4000:
            raise ApiError("data كبيرة")
        args = (uid, text(c.get("name"), "الاسم", 60), text(c.get("field"), "اسم الحقل", 120),
                text(c.get("source"), "المصدر", 30, False), round(num(c.get("ra"), "RA", 0, 360), 5),
                round(num(c.get("dec"), "Dec", -90, 90), 5), text(c.get("status"), "الحالة", 60, False), blob)
        cur = db().execute("INSERT OR IGNORE INTO candidates(user_id,name,field_name,source_id,ra,dec,status,data) "
                           "VALUES(?,?,?,?,?,?,?,?)", args)
        return cur.rowcount

    @app.get("/api/candidates")
    def list_candidates():
        u = need_user()
        rows = db().execute("SELECT * FROM candidates WHERE user_id=? ORDER BY id DESC LIMIT 500", (u["id"],))
        return jsonify(candidates=[cand_row(r) for r in rows])

    @app.post("/api/candidates")
    def add_candidate():
        u = need_user()
        d = body()
        if db().execute("SELECT COUNT(*) FROM candidates WHERE user_id=?", (u["id"],)).fetchone()[0] >= 500:
            raise ApiError("وصلت للحد الأقصى (500 مرشح)", 409)
        created = insert_candidate(u["id"], d)
        db().commit()
        return jsonify(ok=True, created=bool(created)), 201 if created else 200

    @app.post("/api/candidates/import")
    def import_candidates():
        u = need_user()
        items = body().get("items")
        if not isinstance(items, list) or len(items) > 100:
            raise ApiError("items لازم يكون قائمة (حد أقصى 100)")
        n = 0
        for c in items:
            try:
                n += insert_candidate(u["id"], c if isinstance(c, dict) else {})
            except ApiError:
                continue  # عنصر بايظ ما يوقفش الباقي
        db().commit()
        return jsonify(ok=True, imported=n)

    @app.delete("/api/candidates/<int:cid>")
    def delete_candidate(cid):
        u = need_user()
        cur = db().execute("DELETE FROM candidates WHERE id=? AND user_id=?", (cid, u["id"]))
        db().commit()
        if not cur.rowcount:
            raise ApiError("مش موجود", 404)
        return jsonify(ok=True)

    # ------------------------------------------------------------- routes: favorites
    @app.get("/api/favorites")
    def list_favorites():
        u = need_user()
        rows = db().execute("SELECT * FROM favorites WHERE user_id=? ORDER BY id DESC LIMIT 200", (u["id"],))
        return jsonify(favorites=[dict(r) for r in rows])

    @app.post("/api/favorites")
    def add_favorite():
        u = need_user()
        d = body()
        h = text(d.get("hash"), "الرابط", 400)
        if not HASH_RE.match(h):
            raise ApiError("رابط غير صالح")
        if db().execute("SELECT COUNT(*) FROM favorites WHERE user_id=?", (u["id"],)).fetchone()[0] >= 200:
            raise ApiError("وصلت للحد الأقصى (200 حقل)", 409)
        cur = db().execute("INSERT OR IGNORE INTO favorites(user_id,label,ra,dec,fov,hash) VALUES(?,?,?,?,?,?)",
                           (u["id"], text(d.get("label"), "الاسم", 120), num(d.get("ra"), "RA", 0, 360),
                            num(d.get("dec"), "Dec", -90, 90), num(d.get("fov"), "FOV", 1, 21600), h))
        db().commit()
        return jsonify(ok=True, created=bool(cur.rowcount)), 201 if cur.rowcount else 200

    @app.delete("/api/favorites/<int:fid>")
    def delete_favorite(fid):
        u = need_user()
        cur = db().execute("DELETE FROM favorites WHERE id=? AND user_id=?", (fid, u["id"]))
        db().commit()
        if not cur.rowcount:
            raise ApiError("مش موجود", 404)
        return jsonify(ok=True)

    # ------------------------------------------------------------- routes: content
    @app.get("/api/articles")
    def articles():
        cat = request.args.get("category")
        q, args = "SELECT * FROM articles", ()
        if cat:
            q, args = q + " WHERE category=?", (cat,)
        rows = db().execute(q + " ORDER BY id", args)
        return jsonify(articles=[{"id": r["id"], "category": r["category"], "date": r["date_label"],
                                  "readTime": r["read_time"], "title": r["title"], "image": r["image"],
                                  "excerpt": r["excerpt"], "content": json.loads(r["content"])} for r in rows])

    # ------------------------------------------------------------- routes: model
    @app.get("/api/model")
    def model_info():
        return jsonify(model_adapter.info())

    @app.post("/api/analyze")
    def analyze():
        limit("analyze:" + request.remote_addr, 30, 60)
        d = body()
        src = d.get("sources")
        if not isinstance(src, list) or not src:
            raise ApiError("sources مطلوبة (قائمة {epoch,x,y,flux})")
        if len(src) > cfg["MAX_SOURCES"]:
            raise ApiError(f"أقصى عدد مصادر {cfg['MAX_SOURCES']}")
        clean = []
        for s in src:
            if not isinstance(s, dict):
                raise ApiError("كل مصدر لازم يكون object")
            clean.append({"epoch": num(s.get("epoch"), "epoch", 1990, 2100), "x": num(s.get("x"), "x", -1e5, 1e5),
                          "y": num(s.get("y"), "y", -1e5, 1e5), "flux": num(s.get("flux", 1.0), "flux", -1e9, 1e9)})
        payload = {"ra": num(d.get("ra"), "RA", 0, 360), "dec": num(d.get("dec"), "Dec", -90, 90),
                   "fov": num(d.get("fov", 240), "fov", 1, 21600), "sources": clean}
        for k, lo, hi in (("tol", .1, 30), ("max_pm", .1, 1000), ("min_pm", 0, 100), ("min_snr", 0, 100),
                          ("ref_epoch", 1990, 2100)):
            if d.get(k) is not None:
                payload[k] = num(d[k], k, lo, hi)
        t0 = time.time()
        try:
            out = model_adapter.analyze(payload)
        except model_adapter.ModelError as e:
            raise ApiError(f"خطأ في الموديل: {e}", 502)
        ms = int((time.time() - t0) * 1000)
        u = current_user()
        db().execute("INSERT INTO analyses(user_id,ra,dec,fov,n_sources,n_tracks,backend,ms) VALUES(?,?,?,?,?,?,?,?)",
                     (u["id"] if u else None, payload["ra"], payload["dec"], payload["fov"], len(clean),
                      len(out["tracks"]), model_adapter.BACKEND, ms))
        db().commit()
        return jsonify(ok=True, model=model_adapter.info(), n_sources=len(clean), elapsed_ms=ms, **out)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=os.getenv("FLASK_DEBUG") == "1")
