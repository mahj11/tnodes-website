"""Build the tnodes.org website into dist/.

    python build.py                 build dist/ and run the checks (tool/check.py)
    python build.py --serve         build, preview at http://localhost:8000 and rebuild on every change
    python build.py --loops         make gameplay loop videos from private/video_source/<game id>/
    python build.py --base /x       build for a sub-path (GitHub Pages preview at <user>.github.io/x/)

Content lives in content/ (site.toml, games.toml, redirects.toml, *.md, games/<id>/ images),
page layouts in templates/, styles/scripts/fonts in assets/, files copied unchanged to the
site root in static/ (app-ads.txt).
"""

import argparse
import datetime
import hashlib
import html
import http.server
import io
import json
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
import urllib.request
from functools import partial
from pathlib import Path

import markdown
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup, escape
from PIL import Image, ImageDraw, ImageOps

ROOT = Path(__file__).resolve().parent
CONTENT = ROOT / "content"
GAMES_DIR = CONTENT / "games"
YT_DIR = CONTENT / "youtube"
TEMPLATES = ROOT / "templates"
ASSETS = ROOT / "assets"
STATIC = ROOT / "static"
DIST = ROOT / "dist"
CACHE = ROOT / ".cache"
VIDEO_SOURCE = ROOT / "private" / "video_source"
VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v")
LOOP_MAX_BYTES = 3 * 1024 * 1024

sys.path.insert(0, str(ROOT / "tool"))
import check  # noqa: E402  (tool/check.py)


class BuildError(Exception):
    pass


# ---------------------------------------------------------------- content

def load_toml(path):
    return tomllib.loads(path.read_text(encoding="utf-8"))


def paragraphs(text):
    parts = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    return Markup("\n".join(f"<p>{escape(' '.join(p.split()))}</p>" for p in parts))


def find_image(folder, stem):
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        if (folder / (stem + ext)).is_file():
            return folder / (stem + ext)
    return None


def load_games():
    data = load_toml(CONTENT / "games.toml")
    games, seen = [], set()
    for g in data["games"]:
        gid = g["id"]
        if gid in seen:
            raise BuildError(f"games.toml: duplicate id {gid}")
        seen.add(gid)
        if g.get("status", "live") not in ("live", "coming_soon", "hidden"):
            raise BuildError(f"games.toml: {gid}: status must be live, coming_soon or hidden")
        folder = GAMES_DIR / gid
        g.setdefault("status", "live")
        g.setdefault("featured", False)
        for key in ("youtube_trailers", "youtube_shorts"):
            g.setdefault(key, [])
        g["icon_file"] = folder / g["icon"] if "icon" in g else find_image(folder, "icon")
        g["cover_file"] = folder / g["cover"] if "cover" in g else find_image(folder, "cover")
        g["poster_file"] = folder / g["poster"] if "poster" in g else find_image(folder, "poster")
        shots_dir = folder / "screenshots"
        g["screenshot_files"] = ([folder / s for s in g["screenshots"]] if "screenshots" in g else
                                 sorted(p for p in shots_dir.glob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
                                 if shots_dir.is_dir() else [])
        if g["cover_file"] is None and g["screenshot_files"]:
            g["cover_file"] = g["screenshot_files"][0]
        for key in ("icon_file", "cover_file"):
            if g[key] is None or not g[key].is_file():
                raise BuildError(f"games.toml: {gid}: missing {key.split('_')[0]} image in content/games/{gid}/")
        loop = folder / "loop.mp4"
        g["loop"] = loop.is_file() and (folder / "loop.webm").is_file() and (folder / "loop.jpg").is_file()
        g["description_html"] = paragraphs(g.get("description", ""))
        g["path"] = f"/games/{gid}/"
        stores = [n for k, n in (("google_play_url", "Google Play"), ("app_store_url", "App Store")) if k in g]
        g["stores_text"] = "Google Play (coming soon)" if g["status"] == "coming_soon" else " and ".join(stores)
        rd = g.get("release_date")
        g["release_text"] = f"{rd:%B} {rd.day}, {rd.year}" if isinstance(rd, datetime.date) else None
        games.append(g)
    return games


def read_markdown_page(path):
    text = path.read_text(encoding="utf-8")
    meta = {}
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"')
        text = text[m.end():]
    md = markdown.Markdown(extensions=["tables", "toc", "sane_lists"],
                           extension_configs={"toc": {"permalink": "#", "permalink_class": "anchor",
                                                      "permalink_title": "Link to this section"}})
    body = md.convert(text)
    body = re.sub(r"<table>", '<div class="table-scroll" tabindex="0" role="region" aria-label="Table">'
                  '<table>', body)
    body = body.replace("</table>", "</table></div>")
    body = autolink(body)
    # "Last updated" from the front matter: show it at the top, unless the text already says it
    shown = re.search(r"Last updated:\s*([A-Z][a-z]+ \d{1,2}, \d{4})", text)
    if "last_updated" in meta:
        fm = datetime.date.fromisoformat(meta["last_updated"])
        meta["last_updated_text"] = f"{fm:%B} {fm.day}, {fm.year}"
        if shown and shown.group(1) != meta["last_updated_text"]:
            raise BuildError(f"{path.name}: front matter last_updated ({meta['last_updated_text']}) "
                             f"differs from the text ({shown.group(1)})")
    meta["show_date"] = "last_updated_text" in meta and not shown
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S)
    meta["has_h1"] = bool(h1)
    first_p = re.search(r"<p>(?!<em>Last updated)(.*?)</p>", body, re.S)
    meta.setdefault("description", re.sub(r"<[^>]+>", "", first_p.group(1))[:160] if first_p else "")
    return meta, Markup(body)


def autolink(body):
    """Turn bare URLs and email addresses in text into links (outside existing links and tags)."""
    out, in_a = [], False
    for part in re.split(r"(<[^>]+>)", body):
        if part.startswith("<"):
            if re.match(r"<a[\s>]", part):
                in_a = True
            elif part.startswith("</a"):
                in_a = False
            out.append(part)
            continue
        if not in_a:
            def url(m):
                u = m.group(0).rstrip(".,;:)")
                return f'<a href="{u}">{u}</a>' + m.group(0)[len(u):]
            part = re.sub(r"https?://[^\s<]+", url, part)
            part = re.sub(r"(?<![\w/.@-])([\w.+-]+@[\w-]+(?:\.[\w-]+)+)",
                          lambda m: f'<a href="mailto:{m.group(1)}">{m.group(1)}</a>', part)
        out.append(part)
    return "".join(out)


# ---------------------------------------------------------------- images

class Images:
    """Makes WebP/JPEG variants at the sizes they are shown; cached in .cache/img by content hash."""

    def __init__(self):
        self.cache = CACHE / "img"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.outputs = {}  # dist-relative path -> cached file
        self._hash = {}

    def _src_hash(self, src):
        st = src.stat()
        k = (str(src), st.st_size, st.st_mtime_ns)
        if k not in self._hash:
            self._hash[k] = hashlib.sha1(src.read_bytes()).hexdigest()
        return self._hash[k]

    def variant(self, src, width, aspect=None, fmt="webp", quality=80, bg=None):
        """Return (dist path, w, h). aspect=(w, h) crops to that shape; never upscales."""
        with Image.open(src) as im:
            sw, sh = im.size
        if aspect:
            ratio = aspect[0] / aspect[1]
            cw, ch = (sw, round(sw / ratio)) if sw / sh <= ratio else (round(sh * ratio), sh)
            w = min(width, cw)
            h = round(w / ratio)
        else:
            w = min(width, sw)
            h = round(sh * w / sw)
        name = re.sub(r"[^a-z0-9]+", "-", str(src.relative_to(ROOT).with_suffix("")).lower())
        name = name.removeprefix("content-games-").removeprefix("content-").removeprefix("assets-").strip("-")
        rel = f"img/{name}-{w}x{h}.{'jpg' if fmt == 'jpeg' else fmt}"
        key = hashlib.sha1(f"{self._src_hash(src)}|{w}|{h}|{fmt}|{quality}|{bg}".encode()).hexdigest()
        cached = self.cache / f"{key}.{fmt}"
        if not cached.exists():
            with Image.open(src) as im:
                im = ImageOps.exif_transpose(im)
                if fmt == "jpeg" or bg:
                    im = im.convert("RGBA")
                    base = Image.new("RGBA", im.size, bg or (0, 0, 0, 255))
                    im = Image.alpha_composite(base, im).convert("RGB")
                elif im.mode not in ("RGB", "RGBA"):
                    im = im.convert("RGBA" if "A" in im.getbands() or "transparency" in im.info else "RGB")
                im = ImageOps.fit(im, (w, h), Image.LANCZOS) if aspect else im.resize((w, h), Image.LANCZOS)
                if fmt == "webp":
                    im.save(cached, "WEBP", quality=quality, method=6)
                else:
                    im.save(cached, "JPEG", quality=quality, optimize=True, progressive=True)
        self.outputs[rel] = cached
        return rel, w, h

    def write(self, dist):
        for rel, cached in self.outputs.items():
            out = dist / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cached, out)


# ---------------------------------------------------------------- YouTube thumbnails

def yt_thumbnail(vid, short=False):
    """Saved once into content/youtube/ (committed), so the site never loads images from YouTube."""
    path = YT_DIR / f"{vid}.jpg"
    if path.is_file():
        return path
    names = ["oar2.jpg", "hq720.jpg", "hqdefault.jpg"] if short else ["maxresdefault.jpg", "sddefault.jpg", "hqdefault.jpg"]
    for n in names:
        try:
            with urllib.request.urlopen(f"https://i.ytimg.com/vi/{vid}/{n}", timeout=30) as r:
                data = r.read()
        except Exception:
            continue
        with Image.open(io.BytesIO(data)) as im:
            if im.width < 320:
                continue
        YT_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        print(f"  saved YouTube thumbnail {vid} ({n})")
        return path
    raise BuildError(f"could not download the YouTube thumbnail for {vid} (is the video public?)")


# ---------------------------------------------------------------- gameplay loops (ffmpeg)

def make_loops(games):
    missing = []
    for g in games:
        src_dir = VIDEO_SOURCE / g["id"]
        sources = sorted(p for p in src_dir.glob("*") if p.suffix.lower() in VIDEO_EXT) if src_dir.is_dir() else []
        if not sources:
            missing.append(g["id"])
            continue
        src, out = sources[0], GAMES_DIR / g["id"]
        start = float(g.get("loop_start", 0))
        secs = max(5, min(12, float(g.get("loop_seconds", 8))))
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "stream=width,height", "-of", "csv=p=0", str(src)],
                               capture_output=True, text=True, check=True).stdout.strip().split(",")
        w, h = int(probe[0]), int(probe[1])
        scale = "scale=960:-2" if w >= h else "scale=-2:960"
        vf = f"{scale}:flags=lanczos,fps=30"
        common = ["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-t", str(secs), "-i", str(src), "-an", "-vf", vf]
        for crf in (24, 27, 30, 33, 36):
            subprocess.run(common + ["-c:v", "libx264", "-profile:v", "high", "-preset", "slow", "-crf", str(crf),
                                     "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out / "loop.mp4")], check=True)
            if (out / "loop.mp4").stat().st_size <= LOOP_MAX_BYTES:
                break
        for crf in (36, 40, 44, 48):
            subprocess.run(common + ["-c:v", "libvpx-vp9", "-crf", str(crf), "-b:v", "0", "-row-mt", "1",
                                     "-deadline", "good", "-cpu-used", "2", str(out / "loop.webm")], check=True)
            if (out / "loop.webm").stat().st_size <= LOOP_MAX_BYTES:
                break
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-i", str(src), "-frames:v", "1",
                        "-vf", scale, "-q:v", "3", str(out / "loop.jpg")], check=True)
        sizes = ", ".join(f"{f} {(out / f).stat().st_size / 1e6:.1f} MB" for f in ("loop.mp4", "loop.webm"))
        print(f"  {g['id']}: {src.name} -> {sizes}")
    if missing:
        print("No original video yet (cover image is shown instead): " + ", ".join(missing))
        print(f"  Put the original gameplay video in private/video_source/<game id>/ and run: python build.py --loops")


# ---------------------------------------------------------------- brand files

def make_brand(images, dist, games):
    logo = ASSETS / "brand" / "logo-square.png"
    icons = dist / "assets" / "icons"
    icons.mkdir(parents=True, exist_ok=True)
    with Image.open(logo) as im:
        im = im.convert("RGBA")
        im.save(dist / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
        for size, name, pad in ((180, "apple-touch-icon.png", 0.12), (192, "icon-192.png", 0.12),
                                (512, "icon-512.png", 0.12), (32, "icon-32.png", 0.0)):
            canvas = Image.new("RGBA", (size, size), (255, 255, 255, 255 if pad else 0))
            inner = round(size * (1 - 2 * pad))
            canvas.alpha_composite(im.resize((inner, inner), Image.LANCZOS), ((size - inner) // 2,) * 2)
            canvas.save(icons / name, optimize=True)
    # default sharing image: game covers behind the logo
    og = CACHE / "og-default.jpg"
    covers = [g["cover_file"] for g in games if g["status"] != "hidden"][:6]
    key = hashlib.sha1("|".join(f"{p}:{p.stat().st_mtime_ns}" for p in covers + [logo]).encode()).hexdigest()
    key_file = CACHE / "og-default.key"
    if not og.exists() or not key_file.exists() or key_file.read_text() != key:
        canvas = Image.new("RGB", (1200, 630), (10, 12, 22))
        for i, p in enumerate(covers):
            with Image.open(p) as c:
                c = ImageOps.fit(c.convert("RGB"), (400, 315), Image.LANCZOS)
            canvas.paste(c, ((i % 3) * 400, (i // 3) * 315))
        canvas = Image.blend(canvas, Image.new("RGB", canvas.size, (10, 12, 22)), 0.55)
        plate = Image.new("RGBA", (420, 420), (255, 255, 255, 235))
        with Image.open(logo) as lg:
            plate.alpha_composite(lg.convert("RGBA").resize((360, 360), Image.LANCZOS), (30, 30))
        mask = Image.new("L", plate.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, 419, 419), radius=60, fill=255)
        canvas.paste(plate.convert("RGB"), (390, 105), mask)
        canvas.save(og, quality=86, optimize=True)
        key_file.write_text(key)
    shutil.copyfile(og, dist / "assets" / "og-default.jpg")


# ---------------------------------------------------------------- build

def clear_dist():
    """Empty dist/ (keep the folder itself: on Windows a running preview server keeps it open)."""
    DIST.mkdir(exist_ok=True)
    for child in DIST.iterdir():
        for attempt in range(10):
            try:
                shutil.rmtree(child) if child.is_dir() else child.unlink()
                break
            except OSError:
                if attempt == 9:
                    raise BuildError(f"cannot delete {child} (is a file open in another program?)")
                time.sleep(0.3)


def asset_version(path):
    return hashlib.sha1(path.read_bytes()).hexdigest()[:8]


def build(base="", run_checks=True):
    t0 = time.time()
    base = base.rstrip("/")
    if base and not base.startswith("/"):
        base = "/" + base
    site = load_toml(CONTENT / "site.toml")
    games = load_games()
    shown = [g for g in games if g["status"] != "hidden"]
    redirects = load_toml(CONTENT / "redirects.toml")["redirects"]
    images = Images()

    clear_dist()

    def url(path):
        return base + path

    def abs_url(path):
        return site["url"].rstrip("/") + path

    def asset(path):
        return Markup(f"{url('/assets/' + path)}?v={asset_version(ASSETS / path)}")

    def picture(src, widths, aspect=None, sizes="100vw", alt="", cls="", eager=False, fmt="webp", quality=80):
        variants = [images.variant(src, w, aspect, fmt, quality) for w in widths]
        variants = sorted({v[1]: v for v in variants}.values(), key=lambda v: v[1])
        largest = variants[-1]
        srcset = ", ".join(f"{url('/' + rel)} {w}w" for rel, w, h in variants)
        mid = variants[len(variants) // 2]
        attrs = [f'src="{url("/" + mid[0])}"', f'srcset="{srcset}"', f'sizes="{sizes}"',
                 f'width="{largest[1]}"', f'height="{largest[2]}"', f'alt="{escape(alt)}"']
        if cls:
            attrs.append(f'class="{cls}"')
        attrs.append('fetchpriority="high"' if eager else 'loading="lazy" decoding="async"')
        return Markup(f"<img {' '.join(attrs)}>")

    def image_url(src, width, aspect=None, fmt="webp", quality=82):
        rel, w, h = images.variant(src, width, aspect, fmt, quality)
        return url("/" + rel)

    def share_image(src):
        """1200x630 JPEG for Open Graph / Twitter cards, as an absolute https://tnodes.org URL."""
        rel, w, h = images.variant(src, 1200, (40, 21), "jpeg", 84)
        return abs_url("/" + rel)

    def yt(vid, short=False):
        return yt_thumbnail(vid, short)

    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True, undefined=StrictUndefined,
                      trim_blocks=True, lstrip_blocks=True)
    env.globals.update(site=site, games=shown, url=url, abs_url=abs_url, asset=asset, picture=picture,
                       image_url=image_url, share_image=share_image, yt=yt, year=datetime.date.today().year, base=base,
                       preview=bool(base), paragraphs=paragraphs)

    pages = []  # (path, in_sitemap)

    def render(template, path, sitemap=True, **ctx):
        out = DIST / (path.lstrip("/") + "index.html" if path.endswith("/") else path.lstrip("/"))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(env.get_template(template).render(path=path, **ctx), encoding="utf-8")
        pages.append((path, sitemap))

    render("home.html", "/", featured=[g for g in shown if g["featured"]])
    render("games.html", "/games/")
    for g in shown:
        others = [o for o in shown if o["id"] != g["id"]]
        i = shown.index(g)
        more = (others[i:] + others[:i])[:3]
        render("game.html", g["path"], game=g, more=more)
    render("support.html", "/support/")
    meta, body = read_markdown_page(CONTENT / "privacy-policy.md")
    render("markdown.html", "/privacy-policy/", page=meta, body=body)
    render("404.html", "/404.html", sitemap=False)
    for old, new in redirects.items():
        if not old.startswith("/") or not old.endswith("/"):
            raise BuildError(f"redirects.toml: old address must start and end with / : {old}")
        render("redirect.html", old, sitemap=False, target=new)

    # assets, brand, static root files
    shutil.copytree(ASSETS, DIST / "assets", ignore=shutil.ignore_patterns("brand"))
    (DIST / "assets" / "brand").mkdir()
    shutil.copyfile(ASSETS / "brand" / "logo-wordmark.png", DIST / "assets" / "brand" / "logo-wordmark.png")
    make_brand(images, DIST, games)
    for g in shown:
        if g["loop"]:
            out = DIST / "media" / g["id"]
            out.mkdir(parents=True, exist_ok=True)
            for f in ("loop.mp4", "loop.webm", "loop.jpg"):
                shutil.copyfile(GAMES_DIR / g["id"] / f, out / f)
    images.write(DIST)
    for f in STATIC.rglob("*"):
        if f.is_file():
            out = DIST / f.relative_to(STATIC)
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, out)  # byte-for-byte (app-ads.txt)
    (DIST / ".nojekyll").write_bytes(b"")
    (DIST / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {abs_url('/sitemap.xml')}\n",
                                     encoding="utf-8", newline="\n")
    (DIST / "site.webmanifest").write_text(json.dumps({
        "name": site["name"], "short_name": site["name"], "start_url": url("/"), "display": "standalone",
        "background_color": "#0a0c16", "theme_color": "#0a0c16",
        "icons": [{"src": url("/assets/icons/icon-192.png"), "sizes": "192x192", "type": "image/png"},
                  {"src": url("/assets/icons/icon-512.png"), "sizes": "512x512", "type": "image/png"}]},
        indent=1), encoding="utf-8")
    urls = "".join(f"  <url><loc>{html.escape(abs_url(p))}</loc></url>\n" for p, s in pages if s)
    (DIST / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
                                      '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                                      f"{urls}</urlset>\n", encoding="utf-8", newline="\n")

    CACHE.mkdir(exist_ok=True)
    (CACHE / "last-build.json").write_text(json.dumps({"base": base}), encoding="utf-8")
    no_loop = [g["id"] for g in shown if not g["loop"]]
    print(f"Built {len(pages)} pages into dist/ in {time.time() - t0:.1f}s" + (f" (base path {base})" if base else ""))
    if no_loop:
        print("No gameplay loop yet (showing cover image): " + ", ".join(no_loop))
    if run_checks:
        return check.run(base=base)
    return True


# ---------------------------------------------------------------- preview server

def watch_files():
    for folder in (CONTENT, TEMPLATES, ASSETS, STATIC):
        yield from (p for p in folder.rglob("*") if p.is_file())
    yield ROOT / "build.py"


def snapshot():
    return {p: p.stat().st_mtime_ns for p in watch_files()}


def serve(port, base):
    build(base)
    state = snapshot()

    def watcher():
        nonlocal state
        while True:
            time.sleep(1)
            try:
                now = snapshot()
            except FileNotFoundError:
                continue
            if now != state:
                state = now
                print("Change detected, rebuilding...")
                try:
                    build(base)
                except Exception as e:  # keep serving the last good build
                    print(f"BUILD FAILED: {e}")

    threading.Thread(target=watcher, daemon=True).start()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):
            if base and path.startswith(base):
                path = path[len(base):] or "/"
            return super().translate_path(path)

        def send_error(self, code, message=None, explain=None):
            page = DIST / "404.html"
            if code == 404 and page.exists():
                body = page.read_bytes()
                self.send_response(404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                super().send_error(code, message, explain)

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=str(DIST)))
    print(f"Preview: http://localhost:{port}{base}/   (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    ap = argparse.ArgumentParser(description="Build the tnodes.org website.")
    ap.add_argument("--serve", action="store_true", help="preview locally and rebuild on changes")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--base", default="", help="sub-path the site is served from, e.g. /tnodes-website")
    ap.add_argument("--loops", action="store_true", help="make gameplay loops from private/video_source/")
    ap.add_argument("--no-check", action="store_true", help="skip tool/check.py")
    args = ap.parse_args()
    try:
        if args.loops:
            make_loops(load_games())
            return
        if args.serve:
            serve(args.port, args.base)
            return
        if not build(args.base, run_checks=not args.no_check):
            sys.exit(1)
    except BuildError as e:
        sys.exit(f"BUILD FAILED: {e}")


if __name__ == "__main__":
    main()
