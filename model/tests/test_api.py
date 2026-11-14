import json
import os
import random
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import model_adapter  # noqa: E402
from app import create_app  # noqa: E402


def make(**cfg):
    d = tempfile.mkdtemp()
    cfg.setdefault("RATE_LIMIT", False)
    path = os.path.join(d, "t.db")
    app = create_app({"DATABASE_PATH": path, **cfg})
    app.config["TEST_DB"] = path
    return app


def post(c, url, data=None, **kw):
    return c.post(url, data=json.dumps(data if data is not None else {}), content_type="application/json", **kw)


def sky_payload():
    random.seed(1)
    NE, T0, T1, TR = 24, 2025.35, 2026.85, 2026.1
    obs = [T0 + (T1 - T0) * i / (NE - 1) for i in range(NE)]
    stars = [(random.uniform(-300, 300), random.uniform(-300, 300)) for _ in range(80)]
    src = []
    for e in obs:
        src += [{"epoch": e, "x": x, "y": y, "flux": .3} for x, y in stars]
        src.append({"epoch": e, "x": 55 + (e - TR) * 7, "y": -60 + (e - TR) * -4, "flux": .45})
    return {"ra": 133.79, "dec": -7.24, "fov": 600, "sources": src}


class Auth(unittest.TestCase):
    def setUp(self):
        self.app = make()
        self.c = self.app.test_client()

    def reg(self, c=None, email="a@x.com", pw="password1"):
        return post(c or self.c, "/api/auth/register", {"name": "Ali", "email": email, "password": pw})

    def test_health_and_static(self):
        self.assertTrue(self.c.get("/api/health").get_json()["ok"])
        self.assertEqual(self.c.get("/").status_code, 200)
        self.assertEqual(self.c.get("/api/nope").status_code, 404)
        self.assertIn("error", self.c.get("/api/nope").get_json())

    def test_register_login_logout(self):
        r = self.reg()
        self.assertEqual(r.status_code, 201)
        self.assertIn("HttpOnly", r.headers["Set-Cookie"])
        self.assertEqual(self.c.get("/api/auth/me").get_json()["user"]["email"], "a@x.com")
        post(self.c, "/api/auth/logout")
        self.assertIsNone(self.c.get("/api/auth/me").get_json()["user"])
        self.assertEqual(post(self.c, "/api/auth/login", {"email": "a@x.com", "password": "bad"}).status_code, 401)
        self.assertEqual(post(self.c, "/api/auth/login", {"email": "A@X.com", "password": "password1"}).status_code, 200)

    def test_validation(self):
        self.assertEqual(self.reg(pw="short").status_code, 400)
        self.assertEqual(self.reg(email="nope").status_code, 400)
        self.assertEqual(self.reg().status_code, 201)
        self.assertEqual(self.reg(self.app.test_client()).status_code, 409)
        self.assertEqual(self.c.post("/api/auth/login", data="x").status_code, 415)

    def test_password_not_stored_plain(self):
        self.reg()
        import sqlite3
        row = sqlite3.connect(self.app.config["TEST_DB"]).execute("SELECT pw_hash FROM users").fetchone()
        self.assertNotIn("password1", row[0])
        self.assertTrue(row[0].startswith(("scrypt:", "pbkdf2:")))

    def test_csrf_origin(self):
        r = post(self.c, "/api/auth/login", {"email": "a@x.com", "password": "x"}, headers={"Origin": "https://evil.com"})
        self.assertEqual(r.status_code, 403)

    def test_login_rate_limit(self):
        app = make(RATE_LIMIT=True)
        c = app.test_client()
        codes = [post(c, "/api/auth/login", {"email": "z@x.com", "password": "bad"}).status_code for _ in range(10)]
        self.assertEqual(codes[:8], [401] * 8)
        self.assertEqual(codes[8], 429)


class Data(unittest.TestCase):
    def setUp(self):
        self.app = make()
        self.a, self.b = self.app.test_client(), self.app.test_client()
        post(self.a, "/api/auth/register", {"name": "A", "email": "a@x.com", "password": "password1"})
        post(self.b, "/api/auth/register", {"name": "B", "email": "b@x.com", "password": "password1"})

    cand = {"name": "SPX-1234", "field": "WISE 0855", "source": "U", "ra": 133.79, "dec": -7.24, "status": "Named candidate"}

    def test_requires_login(self):
        c = self.app.test_client()
        for u in ("/api/candidates", "/api/favorites"):
            self.assertEqual(c.get(u).status_code, 401)
        self.assertEqual(post(c, "/api/candidates", self.cand).status_code, 401)

    def test_candidates_crud_dedupe_isolation(self):
        self.assertEqual(post(self.a, "/api/candidates", self.cand).status_code, 201)
        self.assertEqual(post(self.a, "/api/candidates", self.cand).status_code, 200)  # duplicate
        lst = self.a.get("/api/candidates").get_json()["candidates"]
        self.assertEqual(len(lst), 1)
        self.assertEqual(self.b.get("/api/candidates").get_json()["candidates"], [])
        self.assertEqual(self.b.delete(f"/api/candidates/{lst[0]['id']}").status_code, 404)  # مش بتاعه
        self.assertEqual(self.a.delete(f"/api/candidates/{lst[0]['id']}").status_code, 200)
        self.assertEqual(self.a.get("/api/candidates").get_json()["candidates"], [])

    def test_candidate_validation_and_import(self):
        bad = dict(self.cand, ra=999)
        self.assertEqual(post(self.a, "/api/candidates", bad).status_code, 400)
        r = post(self.a, "/api/candidates/import", {"items": [self.cand, bad, "junk", dict(self.cand, source="K")]})
        self.assertEqual(r.get_json()["imported"], 2)

    def test_favorites(self):
        f = {"label": "WISE 0855", "ra": 133.79, "dec": -7.24, "fov": 240, "hash": "#explore?s=t2&k=12&fov=240"}
        self.assertEqual(post(self.a, "/api/favorites", f).status_code, 201)
        self.assertEqual(len(self.a.get("/api/favorites").get_json()["favorites"]), 1)
        for evil in ("javascript:alert(1)", "#explore?s=<script>", "http://evil.com", "#signin"):
            self.assertEqual(post(self.a, "/api/favorites", dict(f, hash=evil)).status_code, 400, evil)
        fid = self.a.get("/api/favorites").get_json()["favorites"][0]["id"]
        self.assertEqual(self.b.delete(f"/api/favorites/{fid}").status_code, 404)
        self.assertEqual(self.a.delete(f"/api/favorites/{fid}").status_code, 200)

    def test_articles(self):
        a = self.a.get("/api/articles").get_json()["articles"]
        self.assertEqual(len(a), 4)
        self.assertIsInstance(a[0]["content"], list)
        self.assertEqual(len(self.a.get("/api/articles?category=Challenge").get_json()["articles"]), 2)


class Model(unittest.TestCase):
    def setUp(self):
        self.c = make().test_client()
        self.old = model_adapter.BACKEND

    def tearDown(self):
        model_adapter.BACKEND = self.old

    def test_builtin_finds_mover(self):
        r = post(self.c, "/api/analyze", sky_payload()).get_json()
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["tracks"]), 1)
        t = r["tracks"][0]
        self.assertAlmostEqual(t["pm_x"], 7, places=1)
        self.assertAlmostEqual(t["pm_y"], -4, places=1)
        self.assertEqual(r["n_static"], 80)

    def test_validation(self):
        p = sky_payload()
        self.assertEqual(post(self.c, "/api/analyze", {}).status_code, 400)
        self.assertEqual(post(self.c, "/api/analyze", dict(p, ra=500)).status_code, 400)
        self.assertEqual(post(self.c, "/api/analyze", dict(p, sources=[{"epoch": "x", "x": 1, "y": 1}])).status_code, 400)
        two = [s for s in p["sources"] if abs(s["epoch"] - 2025.35) < 1e-6 or abs(s["epoch"] - 2026.85) < 1e-6]
        self.assertEqual(post(self.c, "/api/analyze", dict(p, sources=two)).status_code, 502)  # < 3 epochs

    def test_custom_backend(self):
        d = tempfile.mkdtemp()
        open(os.path.join(d, "custom_model.py"), "w").write(
            "INFO={'name':'mine'}\ndef predict(p):\n    return {'tracks':[{'ra':p['ra'],'dec':p['dec'],'score':.9}]}\n")
        sys.path.insert(0, d)
        sys.modules.pop("custom_model", None)
        model_adapter.BACKEND = "custom"
        r = post(self.c, "/api/analyze", sky_payload()).get_json()
        self.assertEqual(r["model"]["name"], "mine")
        self.assertEqual(r["tracks"][0]["score"], .9)
        sys.path.remove(d)

    def test_http_backend(self):
        class H(BaseHTTPRequestHandler):
            def do_POST(s):
                n = json.loads(s.rfile.read(int(s.headers["Content-Length"])))
                out = json.dumps({"tracks": [{"ra": n["ra"], "dec": n["dec"]}], "n_static": 7}).encode()
                s.send_response(200)
                s.send_header("Content-Length", str(len(out)))
                s.end_headers()
                s.wfile.write(out)

            def log_message(*a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        os.environ["MODEL_URL"] = f"http://127.0.0.1:{srv.server_port}/predict"
        model_adapter.BACKEND = "http"
        r = post(self.c, "/api/analyze", sky_payload()).get_json()
        srv.shutdown()
        self.assertEqual(r["n_static"], 7)
        os.environ["MODEL_URL"] = "http://127.0.0.1:1/x"  # سيرفس واقع => 502 مش crash
        self.assertEqual(post(self.c, "/api/analyze", sky_payload()).status_code, 502)

    def test_bad_model_output(self):
        d = tempfile.mkdtemp()
        open(os.path.join(d, "custom_model.py"), "w").write("def predict(p):\n    return {'oops':1}\n")
        sys.path.insert(0, d)
        sys.modules.pop("custom_model", None)
        model_adapter.BACKEND = "custom"
        self.assertEqual(post(self.c, "/api/analyze", sky_payload()).status_code, 502)
        sys.path.remove(d)


if __name__ == "__main__":
    unittest.main()
