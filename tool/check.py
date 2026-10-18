"""Site checks. Run by build.py after every build; can also be run on its own.

    python tool/check.py                  check the source and the last build in dist/
    python tool/check.py --record-app-ads record the hash of static/app-ads.txt
                                          (only after replacing the file on purpose)

Checks:
  - app-ads.txt: present, not empty, same SHA-256 as app-ads.sha256, dist copy identical
  - git tracks nothing under private/
  - no third-party requests on page load: no external scripts, styles, fonts, images, media
    or iframes in any page, CSS or JS (links are fine; the only allowed external address in
    JavaScript is the tap-to-load youtube-nocookie.com player)
  - no broken internal links, images or anchors
  - every old URL from content/redirects.toml has a redirect page to an existing page
  - every page has a title and a description
  - gameplay loop videos are under 3 MB
  - prints the page weight report
"""

import hashlib
import html
import html.parser
import json
import re
import subprocess
import sys
import tomllib
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
DIST = ROOT / "dist"
# Ad files that must reach the site root byte-for-byte. ads.txt is listed in case it is added later.
AD_FILES = ["app-ads.txt", "ads.txt"]
KEEP_URLS = ["/", "/games/", "/support/", "/privacy-policy/", "/app-ads.txt", "/robots.txt", "/sitemap.xml"]
ALLOWED_JS_URLS = ("https://www.youtube-nocookie.com/embed/",)
LOOP_MAX_BYTES = 3 * 1024 * 1024
# tag -> attributes that make the browser fetch something on page load
LOADING_ATTRS = {
    "script": ["src"], "img": ["src", "srcset"], "source": ["src", "srcset"], "video": ["src", "poster"],
    "audio": ["src"], "iframe": ["src"], "embed": ["src"], "object": ["data"], "track": ["src"],
    "input": ["src"], "image": ["href", "xlink:href"], "use": ["href", "xlink:href"],
}
LOADING_LINK_RELS = {"stylesheet", "icon", "apple-touch-icon", "manifest", "preload", "modulepreload",
                     "prefetch", "preconnect", "dns-prefetch", "mask-icon", "shortcut"}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hash_file(name):
    return ROOT / (Path(name).stem + ".sha256")


def record(name):
    src = STATIC / name
    if not src.is_file() or src.stat().st_size == 0:
        sys.exit(f"{src} is missing or empty")
    hash_file(name).write_text(f"{sha256(src)}  {name}\n", encoding="ascii", newline="\n")
    lines = src.read_bytes().count(b"\n")
    print(f"Recorded {name}: {lines} lines, sha256 {sha256(src)} -> {hash_file(name).name}")


# ---------------------------------------------------------------- source checks

def check_ad_files(errors):
    for name in AD_FILES:
        src, hf = STATIC / name, hash_file(name)
        if not src.exists() and not hf.exists():
            continue  # this file isn't used by the site
        if not src.is_file() or src.stat().st_size == 0:
            errors.append(f"{name}: static/{name} is missing or empty")
            continue
        if not hf.is_file():
            errors.append(f"{name}: {hf.name} is missing (run: python tool/check.py --record-app-ads)")
            continue
        expected = hf.read_text(encoding="ascii").split()[0]
        if sha256(src) != expected:
            errors.append(f"{name}: static/{name} SHA-256 {sha256(src)} differs from {hf.name} ({expected})")
        if DIST.is_dir():
            out = DIST / name
            if not out.is_file() or out.stat().st_size == 0:
                errors.append(f"{name}: dist/{name} is missing or empty")
            elif out.read_bytes() != src.read_bytes():
                errors.append(f"{name}: dist/{name} differs from static/{name}")
            elif sha256(out) != expected:
                errors.append(f"{name}: dist/{name} SHA-256 differs from {hf.name}")


def check_private_untracked(errors):
    try:
        out = subprocess.run(["git", "ls-files", "--", "private"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return  # not a git checkout (e.g. a downloaded copy): nothing can be tracked
    tracked = out.split()
    if tracked:
        errors.append(f"git tracks {len(tracked)} file(s) under private/, e.g. {tracked[0]}")


# ---------------------------------------------------------------- dist checks

class Page(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.description, self.refresh = None, None, None
        self.ids, self.links, self.loads, self.iframes = set(), [], [], 0
        self._in_title = False
        self.lazy_images, self.eager_images = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            self.ids.add(a["id"])
        if tag == "title":
            self._in_title = True
            self.title = ""
        elif tag == "meta" and a.get("name") == "description":
            self.description = a.get("content", "")
        elif tag == "meta" and (a.get("http-equiv") or "").lower() == "refresh":
            self.refresh = a.get("content", "")
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag == "link":
            rels = set((a.get("rel") or "").lower().split())
            if rels & LOADING_LINK_RELS and a.get("href"):
                self.loads.append(("link " + " ".join(sorted(rels)), a["href"]))
        if tag == "iframe":
            self.iframes += 1
        for attr in LOADING_ATTRS.get(tag, []):
            v = a.get(attr)
            if not v:
                continue
            urls = [p.strip().split()[0] for p in v.split(",") if p.strip()] if attr == "srcset" else [v]
            for u in urls:
                self.loads.append((f"{tag} {attr}", u))
            if tag == "img" and attr == "src":
                (self.lazy_images if a.get("loading") == "lazy" else self.eager_images).append(v)
        style = a.get("style") or ""
        for u in re.findall(r"url\(\s*['\"]?([^'\")]+)", style):
            self.loads.append((f"{tag} style", u))

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def is_external(u):
    p = urllib.parse.urlsplit(u)
    return bool(p.scheme and p.scheme not in ("data",)) or u.startswith("//")


def resolve(u, page_file, base):
    """Map a link to a file in dist/. Returns (path or None, fragment)."""
    p = urllib.parse.urlsplit(u)
    path = urllib.parse.unquote(p.path)
    if not path:
        return page_file, p.fragment
    if path.startswith("/"):
        if base:
            if not (path == base or path.startswith(base + "/")):
                return None, p.fragment
            path = path[len(base):] or "/"
        target = DIST / path.lstrip("/")
    else:
        target = (page_file.parent / path)
    if path.endswith("/") or target.is_dir():
        target = target / "index.html"
    return target.resolve(), p.fragment


def page_url(f):
    rel = f.relative_to(DIST).as_posix()
    return "/" + (rel[:-len("index.html")] if rel.endswith("index.html") else rel)


def check_dist(errors, base):
    pages = {f.resolve(): f for f in DIST.rglob("*.html")}
    parsed = {}
    for rf in pages:
        p = Page()
        p.feed(rf.read_text(encoding="utf-8"))
        parsed[rf] = p
    redirect_files = {rf for rf, p in parsed.items() if p.refresh}

    for rf, p in parsed.items():
        where = page_url(rf)
        if not (p.title or "").strip():
            errors.append(f"{where}: no <title>")
        if not (p.description or "").strip():
            errors.append(f"{where}: no meta description")
        if p.iframes:
            errors.append(f"{where}: has an <iframe> on page load (videos must load only on tap)")
        for kind, u in p.loads:
            if is_external(u):
                errors.append(f"{where}: third-party request on page load: <{kind}> {u}")
                continue
            target, _ = resolve(u, rf, base)
            if target is None or not target.is_file():
                errors.append(f"{where}: missing file for <{kind}> {u}")
        for u in p.links:
            if is_external(u) or u.startswith(("mailto:", "tel:")):
                continue
            target, frag = resolve(u, rf, base)
            if target is None or not target.is_file():
                errors.append(f"{where}: broken link {u}")
            elif frag and target in parsed and frag not in parsed[target].ids:
                errors.append(f"{where}: link {u} points to a missing #{frag}")

    for css in DIST.rglob("*.css"):
        text = css.read_text(encoding="utf-8")
        for u in re.findall(r"url\(\s*['\"]?([^'\")]+)", text) + re.findall(r"@import\s+['\"]([^'\"]+)", text):
            if u.startswith("data:"):
                continue
            if is_external(u):
                errors.append(f"{css.relative_to(DIST)}: third-party request {u}")
            elif not (css.parent / u.split("?")[0].split("#")[0]).resolve().is_file():
                errors.append(f"{css.relative_to(DIST)}: missing file {u}")
    for js in DIST.rglob("*.js"):
        for u in re.findall(r"https?://[^\s'\"`)]+", js.read_text(encoding="utf-8")):
            if not u.startswith(ALLOWED_JS_URLS):
                errors.append(f"{js.relative_to(DIST)}: external address in JavaScript: {u}")

    # old URLs: kept pages and redirect pages
    for u in KEEP_URLS:
        target, _ = resolve(base + u, DIST / "index.html", base)
        if target is None or not target.is_file():
            errors.append(f"old URL {u} no longer exists")
    redirects = tomllib.loads((ROOT / "content" / "redirects.toml").read_text(encoding="utf-8"))["redirects"]
    for old, new in redirects.items():
        f, _ = resolve(base + old, DIST / "index.html", base)
        if f not in parsed or not parsed[f].refresh:
            errors.append(f"redirect {old}: no redirect page")
            continue
        m = re.search(r"url=(.+)$", parsed[f].refresh)
        if not m or m.group(1).strip() != base + new:
            errors.append(f"redirect {old}: points to {m.group(1) if m else '?'} instead of {base + new}")
        target, frag = resolve(base + new, f, base)
        if target is None or not target.is_file():
            errors.append(f"redirect {old} -> {new}: target does not exist")
        elif frag and target in parsed and frag not in parsed[target].ids:
            errors.append(f"redirect {old} -> {new}: #{frag} not found")

    for v in list(DIST.glob("media/*/loop.mp4")) + list(DIST.glob("media/*/loop.webm")):
        if v.stat().st_size > LOOP_MAX_BYTES:
            errors.append(f"{v.relative_to(DIST)} is {v.stat().st_size / 1e6:.1f} MB (limit 3 MB)")

    check_policy_text(errors)
    weight_report(parsed, redirect_files, base)


def words(text):
    return re.findall(r"[\w@.'’/:-]+", text.replace("‑", "-"))


def check_policy_text(errors):
    """The published privacy policy must contain the words of content/privacy-policy.md, in order, unchanged."""
    src = (ROOT / "content" / "privacy-policy.md").read_text(encoding="utf-8")
    src = re.sub(r"^---\s*\n.*?\n---\s*\n", "", src, flags=re.S)
    src = re.sub(r"^\|?[\s|:-]+\|?$", " ", src, flags=re.M)          # table separator rows
    src = re.sub(r"[*#|`]|^\s*[-+]\s", " ", src, flags=re.M)           # markdown syntax
    out = (DIST / "privacy-policy" / "index.html").read_text(encoding="utf-8")
    out = re.search(r'<div class="prose prose-doc">(.*?)</div>\s*</div>\s*</article>', out, re.S).group(1)
    out = re.sub(r'<a class="anchor"[^>]*>.*?</a>', " ", out)
    out = re.sub(r"</?(?:a|strong|em|code|time)\b[^>]*>", "", out)  # inline tags: no word break
    out = re.sub(r"<[^>]+>", " ", out)
    a, b = words(src), words(html.unescape(out))
    if a != b:
        i = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
        errors.append(f"privacy policy text differs from content/privacy-policy.md near word {i}: "
                      f"{' '.join(a[max(0, i - 5):i + 5])!r} vs {' '.join(b[max(0, i - 5):i + 5])!r}")


def weight_report(parsed, redirect_files, base):
    rows = []
    for rf, p in parsed.items():
        if rf in redirect_files:
            continue
        initial = rf.stat().st_size
        seen = set()
        for kind, u in p.loads:
            if kind.startswith("img") or kind.startswith("source") or kind.startswith("video"):
                continue  # images counted below
            t, _ = resolve(u, rf, base)
            if t and t.is_file() and t not in seen and "manifest" not in kind and "icon" not in kind:
                seen.add(t)
                initial += t.stat().st_size
        eager = sum(f.stat().st_size for u in set(p.eager_images)
                    if (f := resolve(u, rf, base)[0]) and f.is_file())
        lazy = sum(f.stat().st_size for u in set(p.lazy_images)
                   if (f := resolve(u, rf, base)[0]) and f.is_file())
        rows.append((page_url(rf), initial + eager, initial + eager + lazy))
    rows.sort(key=lambda r: -r[2])
    print("Page weight (HTML + CSS + JS + fonts + images; 'first view' leaves out lazy-loaded images):")
    print(f"  {'page':38} {'first view':>11} {'all images':>11}")
    for path, first, total in rows:
        print(f"  {path:38} {first / 1024:9.0f} KB {total / 1024:9.0f} KB")
    total = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    print(f"  dist/ total: {total / 1e6:.1f} MB in {sum(1 for f in DIST.rglob('*') if f.is_file())} files")


# ---------------------------------------------------------------- entry points

def run(base=None):
    """Run all checks; returns True when everything passed."""
    errors = []
    check_ad_files(errors)
    check_private_untracked(errors)
    if DIST.is_dir():
        if base is None:
            info = ROOT / ".cache" / "last-build.json"
            base = json.loads(info.read_text())["base"] if info.exists() else ""
        check_dist(errors, base)
    for e in errors:
        print("FAIL", e)
    print(f"{len(errors)} check(s) failed" if errors else "All checks passed")
    return not errors


def main():
    if "--record-app-ads" in sys.argv:
        record("app-ads.txt")
        return
    if not run():
        sys.exit(1)


if __name__ == "__main__":
    main()
