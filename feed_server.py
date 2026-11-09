#!/usr/bin/env python3
"""
feed_server.py - turns web pages into a Roku-ready data.json feed.

For each page URL it looks for video links the page already exposes
in its HTML, in this order:
  1. <video src> / <source src> tags (with <video poster> as thumbnail)
  2. Open Graph / Twitter meta tags (og:video, twitter:player:stream)
  3. JSON-LD VideoObject blocks (contentUrl, thumbnailUrl, name)
  4. Fallback: any .m3u8 / .mp4 URL found in the page source

It does NOT log in, run JavaScript, or defeat DRM/encryption. Pages whose
player builds the link in JavaScript will simply return no items.
Only use it on content you own or are permitted to stream.

Standard library only - no pip installs needed (Python 3.8+).

Usage:
  python feed_server.py serve                  # http://0.0.0.0:8000/data.json
  python feed_server.py serve --port 8080 --config pages.json
  python feed_server.py build --config pages.json > data.json
  python feed_server.py build --url https://example.com/video-page
"""
import argparse
import hashlib
import html
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import Request, urlopen

USER_AGENT = "RokuFeedBuilder/1.0"
FETCH_TIMEOUT = 15          # seconds per page
MAX_PAGE_BYTES = 5_000_000  # don't download huge pages
STREAM_EXTS = (".m3u8", ".mp4")

# Matches http(s) URLs ending in .m3u8/.mp4, including JSON-escaped "https:\/\/..."
STREAM_RE = re.compile(
    r'https?:(?:\\?/){2}[^\s"\'<>()]+?\.(?:m3u8|mp4)(?:\?[^\s"\'<>()]*)?',
    re.IGNORECASE,
)


# ---------------------------------------------------------------- parsing
class PageParser(HTMLParser):
    """Collects meta tags, <video>/<source> tags, <title> and JSON-LD."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.videos = []        # (src, poster, mime type)
        self.title = ""
        self.ld_blocks = []
        self._in_title = False
        self._in_ld = False
        self._ld_buf = []
        self._poster = None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content") and key not in self.meta:
                self.meta[key] = a["content"]
        elif tag == "video":
            self._poster = a.get("poster") or None
            if a.get("src"):
                self.videos.append((a["src"], self._poster, a.get("type", "")))
        elif tag == "source" and a.get("src"):
            self.videos.append((a["src"], self._poster, a.get("type", "")))
        elif tag == "title":
            self._in_title = True
        elif tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._in_ld, self._ld_buf = True, []

    def handle_endtag(self, tag):
        if tag == "video":
            self._poster = None
        elif tag == "title":
            self._in_title = False
        elif tag == "script" and self._in_ld:
            self._in_ld = False
            self.ld_blocks.append("".join(self._ld_buf))

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_ld:
            self._ld_buf.append(data)


def is_stream(url, mime=""):
    path = urlparse(url).path.lower()
    mime = mime.lower()
    return path.endswith(STREAM_EXTS) or "mpegurl" in mime or mime == "video/mp4"


def stream_format(url):
    return "hls" if urlparse(url).path.lower().endswith(".m3u8") else "mp4"


def first_str(value):
    """JSON-LD fields can be a string, a list, or an object with a url."""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value:
        return first_str(value[0])
    if isinstance(value, dict):
        return first_str(value.get("url") or value.get("contentUrl"))
    return None


def walk_video_objects(node):
    """Yield every JSON-LD object whose @type includes VideoObject."""
    if isinstance(node, list):
        for child in node:
            yield from walk_video_objects(child)
    elif isinstance(node, dict):
        t = node.get("@type")
        types = t if isinstance(t, list) else [t]
        if "VideoObject" in types:
            yield node
        for child in node.values():
            if isinstance(child, (dict, list)):
                yield from walk_video_objects(child)


def extract_items(page_url, page_html):
    """Return Roku feed items found in one page's HTML."""
    p = PageParser()
    p.feed(page_html)
    m = p.meta

    page_title = (m.get("og:title") or m.get("twitter:title")
                  or p.title.strip() or urlparse(page_url).hostname or "Untitled")
    page_desc = m.get("og:description") or m.get("description") or ""
    page_thumb = m.get("og:image") or m.get("og:image:url") or m.get("twitter:image") or ""

    found = []  # dicts with url, and optional thumb/title/desc

    for src, poster, mime in p.videos:
        if is_stream(src, mime):
            found.append({"url": src, "thumb": poster})

    for key in ("og:video:secure_url", "og:video:url", "og:video", "twitter:player:stream"):
        if m.get(key) and is_stream(m[key], m.get("og:video:type", "")):
            found.append({"url": m[key]})

    for block in p.ld_blocks:
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for vo in walk_video_objects(data):
            url = first_str(vo.get("contentUrl"))
            if url and is_stream(url, vo.get("encodingFormat", "")):
                found.append({
                    "url": url,
                    "thumb": first_str(vo.get("thumbnailUrl")),
                    "title": first_str(vo.get("name")),
                    "desc": first_str(vo.get("description")),
                })

    if not found:  # last resort: raw links anywhere in the source
        for raw in STREAM_RE.findall(page_html):
            found.append({"url": raw.replace("\\/", "/")})

    items, seen = [], set()
    for f in found:
        url = urljoin(page_url, html.unescape(f["url"].strip()))
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        n = len(items) + 1
        title = f.get("title") or (page_title if n == 1 else f"{page_title} ({n})")
        thumb = f.get("thumb") or page_thumb
        items.append({
            "id": hashlib.sha1(url.encode()).hexdigest()[:10],
            "title": html.unescape(title).strip(),
            "description": html.unescape(f.get("desc") or page_desc).strip(),
            "thumbnail": urljoin(page_url, html.unescape(thumb)) if thumb else "",
            "streamUrl": url,
            "streamFormat": stream_format(url),
        })
    return items


# ---------------------------------------------------------------- fetching
def fetch_html(url):
    if urlparse(url).scheme not in ("http", "https"):
        raise ValueError("only http(s) URLs are allowed")
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
    with urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read(MAX_PAGE_BYTES).decode(charset, errors="replace")


def scrape_page(url):
    try:
        items = extract_items(url, fetch_html(url))
        log(f"OK   {len(items):>2} item(s)  {url}")
        return items
    except Exception as exc:  # one bad page shouldn't break the feed
        log(f"FAIL {url}: {exc}")
        return []


def build_feed(rows):
    """rows = [{"title": "...", "pages": [url, ...]}, ...] -> data.json dict"""
    feed = {"rows": []}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for row in rows:
            results = pool.map(scrape_page, row.get("pages", []))
            items = [item for page_items in results for item in page_items]
            if items:
                feed["rows"].append({"title": row.get("title", "Videos"), "items": items})
    return feed


def load_config(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data["rows"]


def log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- server
class FeedCache:
    def __init__(self, ttl):
        self.ttl, self.lock, self.store = ttl, threading.Lock(), {}

    def get(self, key, builder):
        with self.lock:
            hit = self.store.get(key)
            if hit and time.time() - hit[0] < self.ttl:
                return hit[1]
        body = json.dumps(builder(), indent=2).encode("utf-8")
        with self.lock:
            self.store[key] = (time.time(), body)
        return body


def make_handler(config_path, cache):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path not in ("/data.json", "/feed"):
                return self.send_text(404, "Try /data.json or /data.json?url=<page>\n")

            target = parse_qs(parsed.query).get("url", [None])[0]
            try:
                if target:   # one-off: a single page as a single row
                    body = cache.get(target, lambda: build_feed(
                        [{"title": "Results", "pages": [target]}]))
                else:        # the rows listed in pages.json
                    body = cache.get("__config__", lambda: build_feed(
                        load_config(config_path)))
            except Exception as exc:
                return self.send_text(500, f"Error: {exc}\n")

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_text(self, code, text):
            data = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            log(f"{self.client_address[0]} {fmt % args}")

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Build a Roku data.json feed from web pages.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run an HTTP feed server")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--host", default="0.0.0.0", help="0.0.0.0 = reachable from your Roku")
    s.add_argument("--config", default="pages.json")
    s.add_argument("--cache", type=int, default=600, help="seconds to reuse a built feed")

    b = sub.add_parser("build", help="print data.json once and exit")
    g = b.add_mutually_exclusive_group(required=True)
    g.add_argument("--config")
    g.add_argument("--url")

    args = ap.parse_args()

    if args.cmd == "build":
        rows = load_config(args.config) if args.config else [{"title": "Results", "pages": [args.url]}]
        print(json.dumps(build_feed(rows), indent=2))
        return

    if not Path(args.config).exists():
        log(f"Warning: {args.config} not found - only /data.json?url=... will work")
    server = ThreadingHTTPServer((args.host, args.port),
                                 make_handler(args.config, FeedCache(args.cache)))
    log(f"Serving feed on http://{args.host}:{args.port}/data.json  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Stopped.")


if __name__ == "__main__":
    main()
