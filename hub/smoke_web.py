"""Smoke test of the hub in a real browser: every unit, every station, phone width, no script errors.

    python hub/build_hub.py --target web && python hub/smoke_web.py            # the website (hub/dist)
    python hub/build_hub.py && python hub/smoke_web.py --single                # the single file

Needs Playwright with Chromium (pip install playwright && playwright install chromium). It serves
hub/dist on a local port, opens the home page, then each of the 33 units and each of its 7 stations,
and fails on any page error, console error, or station that renders empty. It also checks that the
code links in the lab stations point to files that exist in this checkout. Free and offline, except
that intro stations may try to load external video thumbnails (those failures are ignored).
"""
from __future__ import annotations

import argparse
import functools
import http.server
import re
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
IGNORE = re.compile(r"youtube|ytimg|googlevideo|doubleclick|fonts\.g|Failed to load resource|ERR_|net::", re.I)


def serve(directory: Path) -> tuple[http.server.ThreadingHTTPServer, str]:
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass
    handler = functools.partial(Quiet, directory=str(directory))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--single", action="store_true", help="test hub/course-hub.html instead of hub/dist")
    ap.add_argument("--units", default="", help="comma-separated unit ids (default: all)")
    a = ap.parse_args()
    from playwright.sync_api import sync_playwright

    if a.single:
        srv, base = serve(HERE)
        start = base + "course-hub.html"
    else:
        if not (HERE / "dist" / "index.html").exists():
            sys.exit("build first: python hub/build_hub.py --target web")
        srv, base = serve(HERE / "dist")
        start = base
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=1)
        where = {"at": "home"}
        page.on("pageerror", lambda e: problems.append(f"{where['at']}: page error: {e}"))
        page.on("console", lambda m: m.type == "error" and not IGNORE.search(m.text)
                and problems.append(f"{where['at']}: console: {m.text}"))
        page.route(re.compile(r"^https?://(?!127\.0\.0\.1)"), lambda r: r.abort())  # stay offline
        page.goto(start)
        page.wait_for_selector(".topic.ready")
        ids = a.units.split(",") if a.units else page.eval_on_selector_all(
            ".topic.ready", "els => els.map(e => e.getAttribute('href').slice(2))")
        links: set[str] = set()
        for uid in ids:
            for s in range(1, 8):
                where["at"] = f"unit {uid} station {s}"
                page.goto(f"{start}#l{uid}s{s}")
                page.wait_for_selector(".station .stack", timeout=15000)
                if not page.inner_text(".station .stack").strip():
                    problems.append(f"{where['at']}: empty station")
                if s == 5:
                    links |= set(page.eval_on_selector_all(
                        ".station a[href*='github.com/davidelka/']", "els => els.map(e => e.href)"))
        where["at"] = "home again"
        page.goto(start + "#home")
        page.wait_for_selector("#pg-save")
        browser.close()
    srv.shutdown()
    bad = []
    for url in sorted(links):
        m = re.search(r"/(?:blob|tree)/[^/]+/([^#?]*)", url)
        if m and m.group(1) and not (ROOT / m.group(1)).exists():
            bad.append(url)
    problems += [f"code link to a missing file: {u}" for u in bad]
    print(f"{len(ids)} units x 7 stations, {len(links)} distinct code links checked")
    for pr in problems:
        print("  PROBLEM", pr)
    print("OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
