"""Build the course hub from hub/src/, in one of two shapes.

    python hub/build_hub.py                      # hub/course-hub.html: ONE self-contained file
    python hub/build_hub.py --target web         # hub/dist/: the website (what Cloudflare Pages serves)
    python hub/build_hub.py --target web --ref v1.1 --site-url https://example.org

Sources live in hub/src/ (index.html, course.js, lessons/NN.js, aids/lNN.js, labguides/NN.js). Edit
those, then rebuild.

The single file inlines every script and font, so it opens straight from disk (file://) with no
server and no network. The website keeps the files separate, with content hashes in their names so
browsers cache them for good, and loads each unit's scripts only when the unit is opened.

Both builds carry a code map: for every lab and project, which files it has and on which line each
function of its mine.py starts. The hub turns it into links to GitHub, pinned to --ref (a release
tag in the website build, so the site and the code it links to always match).
Progress (stations done, quiz scores) is kept in the browser's localStorage, per browser.
"""
from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SRC = HERE / "src"
OUT = HERE / "course-hub.html"
DIST = HERE / "dist"
FONTS = SRC / "fonts"
LATIN = "U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD"
HEBREW = "U+0307-0308,U+0590-05FF,U+200C-2010,U+20AA,U+25CC,U+FB1D-FB4F"
FACES = [("IBM Plex Sans Hebrew", "ibm-plex-sans-hebrew-hebrew-{w}-normal.woff2", HEBREW, (400, 500, 700)),
         ("IBM Plex Sans Hebrew", "ibm-plex-sans-hebrew-latin-{w}-normal.woff2", LATIN, (400, 500, 700)),
         ("IBM Plex Mono", "ibm-plex-mono-latin-{w}-normal.woff2", LATIN, (400, 600))]
GOOGLE_FONTS = re.compile(r'<link rel="stylesheet" href="https://fonts.googleapis.com/[^"]*">')
TAG = re.compile(r'<script src="([^"?]+)(?:\?[^"]*)?"(?: defer)?></script>\n?')
TITLE = "מסלול AI Engineer"
DESCRIPTION = ("קורס AI Engineer בעברית, ללמידה עצמית: 28 נושאים, מ-Prompt Engineering ו-RAG ועד סוכנים, "
               "MCP, הערכה, אבטחה ופריסה לענן. שקופיות, סימולציות ומבחנים באתר, ומעבדות עם טסטים בריפו.")

# Texts that only make sense inside the claude.ai artifact version of the hub.
REPLACE = {
    "ההורדה לא זמינה בתצוגה הזו. הקובץ ai-engineer-course.zip נמצא גם בצ'אט.":
        "הריפו מגיע יחד עם ה-hub: זו התיקייה שבה נמצא הקובץ הזה (hub/course-hub.html).",
    "בודק אם אפשר להוריד מכאן…": "",
    "<b>הגשה:</b> שלח לי בצ'אט": "<b>הגשה:</b> שלח ל-Claude בצ'אט",
}


# ---------------------------------------------------------------- the code map
def code_map() -> dict:
    """{lesson id: {"files": [...], "fns": {name: [path, line]}}} for every unit with a repoPath."""
    out = {}
    for f in sorted((SRC / "lessons").glob("*.js")):
        m = re.search(r'repoPath:\s*"([^"]+)"', f.read_text(encoding="utf-8"))
        if not m:
            continue
        rel = m.group(1).strip("/")
        d = ROOT / rel
        if not d.is_dir():
            raise SystemExit(f"{f.name}: repoPath {rel} does not exist")
        entry = {"files": [n for n in ("README.md", "mine.py", "HINTS.md", "tests") if (d / n).exists()], "fns": {}}
        mine = d / "mine.py"
        if mine.exists():
            for node in ast.parse(mine.read_text(encoding="utf-8")).body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                    entry["fns"][node.name] = [f"{rel}/mine.py", node.lineno]
        out[f.stem] = entry
    return out


def map_links() -> tuple[dict, list[str]]:
    """Where each path in the lab guides' file maps should link. A plain path that exists links to
    itself. A compound entry ("agentkit/agent.py · Agent.run") links to its first path that exists.
    Entries with no path in the repo (created when a lab runs, e.g. results/cache/) are not linked."""
    link, nolink = {}, set()
    for f in (SRC / "labguides").glob("*.js"):
        for path in re.findall(r'"path":\s*"([^"]+)"', f.read_text(encoding="utf-8")):
            if (ROOT / path.rstrip("/")).exists():
                continue
            first = re.split(r"\s+|·|→|,|\+|\(", path.strip())[0].rstrip("/")
            if first and (ROOT / first).exists():
                link[path] = first
            else:
                nolink.add(path)
    return link, sorted(nolink)


def config_script(ref: str, web: bool, extra: dict | None = None) -> str:
    link, nolink = map_links()
    cfg = {"HUB_REF": ref, "CODEMAP": code_map(), "MAPLINK": link, "NOLINK": nolink}
    if web:
        cfg["HUB_WEB"] = 1
    cfg.update(extra or {})
    body = ";".join(f"window.{k}={json.dumps(v, ensure_ascii=False, separators=(',', ':'))}" for k, v in cfg.items())
    return f"<script>/* build config */\n{body};\n</script>\n"


# ---------------------------------------------------------------- the single file
def embedded_fonts() -> str:
    """@font-face rules with the woff2 files inlined (IBM Plex, SIL Open Font License 1.1, see fonts/OFL-IBM-Plex.txt)."""
    rules = []
    for family, pattern, rng, weights in FACES:
        for w in weights:
            f = FONTS / pattern.format(w=w)
            if f.exists():
                b64 = base64.b64encode(f.read_bytes()).decode()
                rules.append(f"@font-face{{font-family:'{family}';font-style:normal;font-display:swap;font-weight:{w};"
                             f"src:url(data:font/woff2;base64,{b64}) format('woff2');unicode-range:{rng};}}")
    return "<style>/* IBM Plex, SIL OFL 1.1 */" + "".join(rules) + "</style>" if rules else ""


def inline(match: re.Match) -> str:
    src = match.group(1)
    if src.startswith(("http:", "https:")) or src == "repo.js":
        return ""  # repo.js held a zip of lesson 1 for the artifact version; not needed here
    path = SRC / src
    if not path.exists():
        return ""
    code = path.read_text(encoding="utf-8").replace("</script", "<\\/script")
    return f"<script>/* {src} */\n{code}\n</script>\n"


def build_single(ref: str) -> None:
    html = (SRC / "index.html").read_text(encoding="utf-8")
    first = TAG.search(html)
    html = html[:first.start()] + config_script(ref, web=False) + html[first.start():]
    html = TAG.sub(inline, html)
    fonts = embedded_fonts()
    if fonts:  # no network needed: drop the Google Fonts link and inline the same fonts
        html = GOOGLE_FONTS.sub(fonts, html)
    for old, new in REPLACE.items():
        html = html.replace(old, new)
    if "<!doctype" not in html[:200].lower():
        html = ("<!doctype html>\n<html>\n<head><meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
                f"<title>{TITLE}</title></head>\n<body>\n" + html + "\n</body></html>\n")
    OUT.write_text(html, encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size / 1e6:.1f} MB)")


# ---------------------------------------------------------------- the website
def lesson_stubs() -> dict:
    """Title, subtitle, hours and label of every unit, read by running its script in node.
    The home page and the side menu need only these; the rest of a unit loads when it is opened."""
    js = r"""
const fs = require("fs"), vm = require("vm"), path = require("path");
const dir = process.argv[1], out = {};
for (const f of fs.readdirSync(dir).filter((x) => x.endsWith(".js")).sort()) {
  const window = {}; vm.runInNewContext(fs.readFileSync(path.join(dir, f), "utf8"), { window, console });
  for (const [id, L] of Object.entries(window.LESSONS || {}))
    out[id] = { n: L.n, title: L.title, he: L.he, hours: L.hours, label: L.label, stub: true };
}
process.stdout.write(JSON.stringify(out));
"""
    res = subprocess.run(["node", "-e", js, str(SRC / "lessons")], capture_output=True, text=True, check=True)
    return json.loads(res.stdout)


def hashed(src: Path, sub: str, assets: Path) -> str:
    data = src.read_bytes()
    name = f"{src.stem}.{hashlib.sha256(data).hexdigest()[:10]}{src.suffix}"
    (assets / sub).mkdir(parents=True, exist_ok=True)
    (assets / sub / name).write_bytes(data)
    return f"assets/{sub}/{name}"


def build_web(ref: str, site_url: str) -> None:
    if DIST.exists():
        shutil.rmtree(DIST)
    assets = DIST / "assets"
    assets.mkdir(parents=True)
    html = (SRC / "index.html").read_text(encoding="utf-8")

    # fonts as cacheable files
    rules = []
    for family, pattern, rng, weights in FACES:
        for w in weights:
            f = FONTS / pattern.format(w=w)
            if f.exists():
                url = hashed(f, "fonts", assets)
                rules.append(f"@font-face{{font-family:'{family}';font-style:normal;font-display:swap;font-weight:{w};"
                             f"src:url({url}) format('woff2');unicode-range:{rng};}}")
    shutil.copy(FONTS / "OFL-IBM-Plex.txt", assets / "fonts" / "OFL-IBM-Plex.txt")
    html = GOOGLE_FONTS.sub("<style>/* IBM Plex, SIL OFL 1.1 */" + "".join(rules) + "</style>", html)

    # scripts: shared ones stay in the page as <script src>; per-unit ones are loaded on demand
    per_unit: dict[str, list[str]] = {}
    shared: list[str] = []
    for m in TAG.finditer(html):
        src = m.group(1)
        if src.startswith(("http:", "https:")) or src == "repo.js" or not (SRC / src).exists():
            continue
        url = hashed(SRC / src, src.split("/")[0] if "/" in src else "js", assets)
        u = re.match(r"(?:lessons/(\d\d)|aids/l(\d\d)|labguides/(\d\d))\.js$", src)
        if u:
            per_unit.setdefault(next(g for g in u.groups() if g), []).append(url)
        else:
            shared.append(url)
    order = {"lessons": 0, "aids": 1, "labguides": 2}  # the lesson, then its aids, then its lab guide
    for files in per_unit.values():
        files.sort(key=lambda u: order[u.split("/")[1]])
    first = TAG.search(html)
    head_scripts = config_script(ref, web=True, extra={"LESSON_FILES": per_unit, "LESSONS": lesson_stubs()})
    head_scripts += "".join(f'<script src="{u}"></script>\n' for u in shared)
    html = html[:first.start()] + head_scripts + TAG.sub("", html[first.start():])
    html = re.sub(r"<title>.*?</title>\n?", "", html)
    html = re.sub(r'<meta name="claude:[^"]*" content="[^"]*">\n?', "", html)

    page = f"""<!doctype html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{TITLE}</title>
<meta name="description" content="{DESCRIPTION}">
<link rel="canonical" href="{site_url}/">
<link rel="icon" href="favicon.svg" type="image/svg+xml">
<meta property="og:type" content="website">
<meta property="og:locale" content="he_IL">
<meta property="og:title" content="{TITLE}">
<meta property="og:description" content="{DESCRIPTION}">
<meta property="og:url" content="{site_url}/">
<meta property="og:image" content="{site_url}/og.png">
<meta name="twitter:card" content="summary_large_image">
<meta name="theme-color" content="#2446c4">
</head>
<body>
{html}
</body>
</html>
"""
    (DIST / "index.html").write_text(page, encoding="utf-8")
    (DIST / "favicon.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#2446c4"/>'
        '<text x="32" y="43" font-family="Arial, sans-serif" font-size="28" font-weight="700" fill="#fff" '
        'text-anchor="middle">AI</text></svg>\n', encoding="utf-8")
    if (SRC / "og.png").exists():
        shutil.copy(SRC / "og.png", DIST / "og.png")
    # Cloudflare Pages reads _headers: hashed files never change, the page always revalidates
    (DIST / "_headers").write_text(
        "/assets/*\n  Cache-Control: public, max-age=31536000, immutable\n"
        "/\n  Cache-Control: public, max-age=0, must-revalidate\n"
        "/*\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: strict-origin-when-cross-origin\n", encoding="utf-8")
    (DIST / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {site_url}/sitemap.xml\n", encoding="utf-8")
    (DIST / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>{site_url}/</loc></url></urlset>\n", encoding="utf-8")
    size = sum(p.stat().st_size for p in DIST.rglob("*") if p.is_file())
    first_load = (DIST / "index.html").stat().st_size + sum((DIST / u).stat().st_size for u in shared)
    print(f"wrote {DIST} ({size / 1e6:.1f} MB in all; first load {first_load / 1e3:.0f} KB before compression, "
          f"{len(per_unit)} units loaded on demand, links pinned to {ref})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--target", choices=("single", "web"), default="single")
    ap.add_argument("--ref", default="main", help="git ref the code links point to (a release tag for the website)")
    ap.add_argument("--site-url", default="https://learn-ai-engineering-2hn.pages.dev")
    a = ap.parse_args()
    if a.target == "web":
        build_web(a.ref, a.site_url.rstrip("/"))
    else:
        build_single(a.ref)


if __name__ == "__main__":
    main()
