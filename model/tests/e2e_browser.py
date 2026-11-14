"""E2E بمتصفح حقيقي (Playwright). تشغيل:  python tests/e2e_browser.py"""
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8765
URL = f"http://127.0.0.1:{PORT}"
fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


env = dict(os.environ, PORT=str(PORT), DATABASE_PATH=os.path.join(tempfile.mkdtemp(), "e2e.db"), RATE_LIMIT="0")
srv = subprocess.Popen([sys.executable, "app.py"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(50):
        try:
            urllib.request.urlopen(URL + "/api/health", timeout=1)
            break
        except Exception:
            time.sleep(.2)

    with sync_playwright() as p:
        br = p.chromium.launch()
        ctx = br.new_context(viewport={"width": 1280, "height": 900})
        pg = ctx.new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: m.type == "error" and "favicon" not in m.text and "Failed to load resource" not in m.text and errors.append(m.text))
        api_seen = []
        pg.on("response", lambda r: "/api/" in r.url and api_seen.append((r.request.method, r.url.split("/api/")[1], r.status)))

        # ---- home + articles from DB
        pg.goto(URL + "/")
        pg.click("#fskip")
        pg.wait_for_selector(".article-card")
        check("articles rendered (4)", pg.locator(".article-card").count() == 4)
        check("articles fetched from API", ("GET", "articles", 200) in api_seen)
        pg.click('.tab-btn[data-cat="Challenge"]')
        check("category filter works", pg.locator(".article-card").count() == 2)

        # ---- register
        pg.evaluate("location.hash='#signin'")
        pg.click("#tabUp")
        pg.fill("#aName", "Test User")
        pg.fill("#aMail", "e2e@example.com")
        pg.fill("#aPass", "correct-horse-1")
        pg.click("#authBtn")
        pg.wait_for_selector("#authCard.signed")
        check("register -> signed in", "test user" in pg.inner_text("#doneName").lower())
        check("session cookie is HttpOnly", any(c["name"] == "px_session" and c["httpOnly"] for c in ctx.cookies()))
        check("note says server mode", "server" in pg.inner_text("#authNote").lower())

        # ---- explore: open a simulated field
        pg.evaluate("location.hash='#explore'")
        pg.wait_for_selector("#ra")
        pg.fill("#ra", "200.5")
        pg.fill("#dec", "30.1")
        pg.click("#f button.go")
        pg.wait_for_selector("#md:not([hidden])")
        pg.click('#tabs button[data-t="dis"]')
        check("model button hidden before first click", not pg.locator("#mdl").is_visible())
        box = pg.locator("#big").bounding_box()
        pg.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        pg.wait_for_selector("#cele:not([hidden])", timeout=3000)
        pg.fill("#cnm", "SPX-E2E")
        pg.click("#csv")
        pg.wait_for_timeout(600)
        cands = pg.evaluate("fetch('/api/candidates').then(r=>r.json())")["candidates"]
        check("named candidate saved to DB", [c["name"] for c in cands] == ["SPX-E2E"], cands)

        # ---- model
        check("model button visible after first click", pg.locator("#mdl").is_visible())
        pg.click("#mdl")
        pg.wait_for_selector("#mres table", timeout=15000)
        rows = pg.locator("#mres table tr").count() - 1
        check("model returned 3 tracks", rows == 3, rows)
        check("model flags unmatched candidate", "Unmatched candidate" in pg.inner_text("#mres"))
        check("model call hit API", any(a[1] == "analyze" and a[2] == 200 for a in api_seen))

        # ---- save second candidate (hint path) + favorite
        pg.click("#run")
        pg.click('#tabs button[data-t="view"]')
        pg.click("#fav")
        pg.wait_for_function("document.getElementById('fav').textContent.includes('Saved')")
        check("favorite saved", len(pg.evaluate("fetch('/api/favorites').then(r=>r.json())")["favorites"]) == 1)
        # الترتيب مقصود: نحفظ مرشح بعد المفضلة ونتأكد إن صفحة الحساب مش بتعرض قايمة قديمة
        pg.click('#tabs button[data-t="dis"]')
        pg.click("#sv")
        pg.wait_for_timeout(600)
        check("2 candidates in DB", len(pg.evaluate("fetch('/api/candidates').then(r=>r.json())")["candidates"]) == 2)

        # ---- my data on sign-in page
        pg.evaluate("location.hash='#signin'")
        pg.wait_for_selector("#myData li")
        check("account page lists 1 favorite + 2 candidates", pg.locator("#myData li").count() == 3, pg.locator("#myData li").count())
        pg.locator("#myData button[data-cand]").first.click()
        pg.wait_for_function("document.querySelectorAll('#myData li').length===2")
        check("delete candidate from UI", len(pg.evaluate("fetch('/api/candidates').then(r=>r.json())")["candidates"]) == 1)

        # ---- reload keeps session; logout clears
        pg.reload()
        pg.wait_for_selector("#authCard.signed")
        check("session survives reload", True)
        pg.click("#outBtn")
        pg.wait_for_function("!document.getElementById('authCard').classList.contains('signed')")
        check("logout clears local cache", pg.evaluate("localStorage.getItem('spxc')") is None)
        check("me is null after logout", pg.evaluate("fetch('/api/auth/me').then(r=>r.json())")["user"] is None)

        # ---- login errors + success
        pg.fill("#aMail", "e2e@example.com")
        pg.fill("#aPass", "wrong-password")
        pg.click("#authBtn")
        pg.wait_for_function("document.getElementById('authErr').textContent.length>0")
        check("wrong password shows server error", pg.inner_text("#authErr") != "")
        pg.fill("#aPass", "correct-horse-1")
        pg.click("#authBtn")
        pg.wait_for_selector("#authCard.signed")
        pg.wait_for_selector("#myData li")
        check("login restores saved data (1 cand + 1 fav)", pg.locator("#myData li").count() == 2, pg.locator("#myData li").count())
        check("no JS errors during run", not errors, errors[:3])

        # ---- offline fallback: backend unreachable
        ctx2 = br.new_context()
        ctx2.route("**/api/**", lambda r: r.abort())
        pg2 = ctx2.new_page()
        errs2 = []
        pg2.on("pageerror", lambda e: errs2.append(str(e)))
        pg2.goto(URL + "/#signin")
        pg2.wait_for_selector("#aMail")
        pg2.fill("#aMail", "off@example.com")
        pg2.fill("#aPass", "whatever")
        pg2.click("#authBtn")
        pg2.wait_for_selector("#authCard.signed")
        check("offline fallback signs in locally", "off" in pg2.inner_text("#doneName").lower())
        check("offline note shown", "Offline" in pg2.inner_text("#authNote"))
        pg2.evaluate("location.hash='#home'")
        pg2.wait_for_selector(".article-card")
        check("offline: embedded articles still render", pg2.locator(".article-card").count() == 4)
        check("offline: no JS errors", not errs2, errs2[:3])
        br.close()
finally:
    srv.terminate()

print("\n%d failed" % len(fails) if fails else "\nALL E2E CHECKS PASSED")
sys.exit(1 if fails else 0)
