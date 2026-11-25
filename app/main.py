"""Fast Pinterest media resolver with HTTP-first extraction and reusable Chromium."""
import asyncio
import json
import logging
import mimetypes
import os
import re
import time
from collections import deque
from contextlib import asynccontextmanager
from html import unescape
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, HttpUrl
from playwright.async_api import async_playwright

APP_NAME = "Pinterest Media Resolver"
VERSION = "1.5.0"
LOG = logging.getLogger("pinterest-resolver")

ALLOWED_PIN_HOSTS = {
    "pinterest.com", "www.pinterest.com", "pin.it",
    "pinterest.co.uk", "www.pinterest.co.uk", "pinterest.ca", "www.pinterest.ca",
    "pinterest.de", "www.pinterest.de", "pinterest.fr", "www.pinterest.fr",
    "pinterest.es", "www.pinterest.es", "pinterest.it", "www.pinterest.it",
    "pinterest.com.au", "www.pinterest.com.au", "pinterest.jp", "www.pinterest.jp",
}
ALLOWED_CDN_HOSTS = {"i.pinimg.com", "v1.pinimg.com", "v2.pinimg.com", "v3.pinimg.com", "v.pinimg.com"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

RATE_WINDOW = 60
RATE_MAX = 30
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "13"))
SHORT_URL_TIMEOUT = float(os.getenv("SHORT_URL_TIMEOUT", "10"))
BROWSER_TIMEOUT_MS = int(os.getenv("BROWSER_TIMEOUT_MS", "22000"))
BROWSER_SETTLE_MS = int(os.getenv("BROWSER_SETTLE_MS", "1100"))
RESOLVE_TIMEOUT = float(os.getenv("RESOLVE_TIMEOUT", "48"))
BROWSER_CONCURRENCY = int(os.getenv("BROWSER_CONCURRENCY", "2"))
RESOLVE_CONCURRENCY = int(os.getenv("RESOLVE_CONCURRENCY", "4"))
_rate_hits: dict[str, deque[float]] = {}


class ResolveBody(BaseModel):
    url: HttpUrl


def clean_url(value: str) -> str:
    return value.replace("\\/", "/").strip().rstrip("\\,;)")


def host_of(value: str) -> str:
    try:
        return (urlparse(value).hostname or "").lower().rstrip(".")
    except Exception:
        return ""


def is_allowed_pinterest_url(value: str) -> bool:
    p = urlparse(value)
    return p.scheme in {"http", "https"} and host_of(value) in ALLOWED_PIN_HOSTS


def is_allowed_media_url(value: str) -> bool:
    p = urlparse(value)
    return p.scheme in {"http", "https"} and host_of(value) in ALLOWED_CDN_HOSTS


def normalize_pin_url(value: str) -> str:
    p = urlparse(value)
    return urlunparse(("https", p.netloc.lower(), p.path, "", "", ""))


def looks_like_mp4(value: str) -> bool:
    v = clean_url(value).lower()
    return "pinimg.com/videos/" in v and (".mp4" in v or "/mp4/" in v or "720p" in v or "1080p" in v) and ".m3u8" not in v


def quality_score(url: str) -> tuple:
    v = url.lower()
    score = 1000 if ".mp4" in v else 0
    if any(x in v for x in ("720p", "v_720p", "_720w")): score += 700
    if "1080" in v: score += 900
    if "1440" in v or "2160" in v: score += 1100
    if "480" in v: score += 450
    if "360" in v: score += 300
    if "hls" in v or ".m3u8" in v: score -= 10000
    if "mobile" in v or "hevc" in v: score -= 100
    return score, len(v)


def balanced_json_strings(text: str) -> list[str]:
    found: list[str] = []
    markers = ("__PWS_RELAY_REGISTER_COMPLETED_REQUEST__", "__PWS_RELAY_REGISTER_FALBACK_REQUEST__", "__PWS_RELAY_REGISTER_REQUEST__")
    for marker in markers:
        start = 0
        while (idx := text.find(marker, start)) >= 0:
            brace = text.find("{", idx)
            if brace < 0: break
            depth = 0; in_string = False; escaped = False; end = None
            for i in range(brace, min(len(text), brace + 2_500_000)):
                ch = text[i]
                if in_string:
                    if escaped: escaped = False
                    elif ch == "\\": escaped = True
                    elif ch == '"': in_string = False
                    continue
                if ch == '"': in_string = True
                elif ch == "{": depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0: end = i + 1; break
            if end:
                found.append(text[brace:end]); start = end
            else: break
    return found


def walk(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values(): yield from walk(value)
    elif isinstance(obj, list):
        for value in obj: yield from walk(value)


def collect_urls_from_obj(obj: Any) -> list[str]:
    urls = []
    for node in walk(obj):
        for value in node.values():
            if isinstance(value, str):
                value = clean_url(unescape(value))
                if "pinimg.com/" in value: urls.append(value)
    return urls


def extract_media(html: str, network_urls: list[str] | None = None) -> dict:
    network_urls = network_urls or []
    videos: set[str] = set(); images: set[str] = set(); primary: set[str] = set(); posters: set[str] = set()
    for url in network_urls:
        url = clean_url(url)
        if looks_like_mp4(url) and is_allowed_media_url(url): videos.add(url)
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try: data = json.loads(script.string or script.get_text())
        except Exception: continue
        for node in walk(data):
            for key in ("contentUrl", "embedUrl", "url"):
                value = node.get(key)
                if isinstance(value, str) and looks_like_mp4(value): videos.add(clean_url(unescape(value)))
            thumb = node.get("thumbnailUrl")
            if isinstance(thumb, str) and is_allowed_media_url(thumb): posters.add(clean_url(thumb))
    for raw in balanced_json_strings(html):
        try: data = json.loads(raw)
        except Exception: continue
        for url in collect_urls_from_obj(data):
            if looks_like_mp4(url): videos.add(url)
            elif is_allowed_media_url(url): images.add(url)
    patterns = (
        r'https?://v\d?\.pinimg\.com/videos/[^"\'<>\s\\]+',
        r'https?:\\/\\/v\d?\.pinimg\.com\\/videos\\/[^"\'<>\s]+',
    )
    for pattern in patterns:
        for match in re.findall(pattern, html):
            url = clean_url(unescape(match))
            if looks_like_mp4(url): videos.add(url)
    for tag in soup.find_all(["video", "source"]):
        for attr in ("src", "data-src", "data-video-url"):
            value = tag.get(attr)
            if isinstance(value, str) and looks_like_mp4(value): videos.add(clean_url(unescape(value)))
    for tag in soup.find_all("meta"):
        prop = (tag.get("property") or tag.get("name") or "").lower()
        content = tag.get("content")
        if not isinstance(content, str): continue
        content = clean_url(unescape(content))
        if "og:image" in prop and is_allowed_media_url(content): primary.add(content); posters.add(content)
        if "og:video" in prop and looks_like_mp4(content): videos.add(content)
    for tag in soup.find_all("img"):
        for attr in ("src", "data-src"):
            value = tag.get(attr)
            if isinstance(value, str):
                value = clean_url(unescape(value))
                if is_allowed_media_url(value): images.add(value)
    for pattern in (r'https?://i\.pinimg\.com/[^"\'<>\s\\]+', r'https?:\\/\\/i\\.pinimg\\.com\\/[^"\'<>\s]+'):
        for match in re.findall(pattern, html):
            value = clean_url(unescape(match))
            if is_allowed_media_url(value): images.add(value)
    return {
        "videos": sorted((u for u in videos if is_allowed_media_url(u)), key=quality_score, reverse=True)[:12],
        "primary_images": list(dict.fromkeys(u for u in primary if is_allowed_media_url(u)))[:5],
        "images": list(dict.fromkeys(u for u in images if is_allowed_media_url(u)))[:20],
        "posters": list(dict.fromkeys(u for u in posters if is_allowed_media_url(u)))[:10],
    }


async def resolve_short_url(client: httpx.AsyncClient, url: str) -> str:
    response = await client.get(url, timeout=SHORT_URL_TIMEOUT, headers={"User-Agent": UA, "Accept": "text/html,*/*;q=0.8"})
    final = str(response.url)
    if not is_allowed_pinterest_url(final): raise ValueError("The URL did not resolve to Pinterest.")
    return final


async def http_extract(url: str) -> tuple[str, dict]:
    headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
               "Accept-Language": "en-US,en;q=0.9", "Referer": "https://www.pinterest.com/"}
    client: httpx.AsyncClient = app.state.http
    response = await client.get(url, headers=headers, timeout=HTTP_TIMEOUT, follow_redirects=True)
    response.raise_for_status()
    final = str(response.url)
    if not is_allowed_pinterest_url(final): raise ValueError("Pinterest redirect validation failed.")
    return final, extract_media(response.text)


async def browser_extract(url: str) -> tuple[str, dict]:
    browser = app.state.browser
    if browser is None or not browser.is_connected():
        raise RuntimeError("Browser fallback is unavailable.")
    async with app.state.browser_semaphore:
        context = await browser.new_context(user_agent=UA, locale="en-US", viewport={"width": 1365, "height": 850})
        page = await context.new_page()
        network_urls: list[str] = []
        async def intercept(route):
            request_url = route.request.url
            if "pinimg.com/videos/" in request_url:
                network_urls.append(request_url)
                await route.abort()
            elif route.request.resource_type in {"font", "media"}:
                await route.abort()
            else:
                await route.continue_()
        await page.route("**/*", intercept)
        try:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT_MS)
            except Exception as exc:
                # Pinterest may continue loading after the useful document has arrived.
                if not page.url or page.url == "about:blank": raise exc
            await page.wait_for_timeout(BROWSER_SETTLE_MS)
            html = await page.content()
            final_url = page.url
            try:
                dom_urls = await page.evaluate("""() => Array.from(document.querySelectorAll('video,source')).map(x => x.currentSrc || x.src || x.getAttribute('data-src')).filter(Boolean)""")
                network_urls.extend(dom_urls or [])
            except Exception: pass
            return final_url, extract_media(html, network_urls)
        finally:
            await context.close()


def build_result(final_url: str, media: dict) -> dict:
    if media.get("videos"):
        poster = (media.get("posters") or media.get("images") or [None])[0]
        unique = []
        for video in media["videos"]:
            unique.append({"type": "video", "format": "mp4", "quality": quality_label(video), "url": video,
                           "download_url": "/api/download?url=" + quote(video, safe=""), "thumbnail": poster})
        return {"success": True, "type": "video", "pin_url": final_url, "media": unique[:12],
                "message": "Pinterest video found with the highest available progressive MP4 options returned by Pinterest."}
    images = list(dict.fromkeys(media.get("primary_images") or media.get("images") or media.get("posters") or []))
    images.sort(key=lambda u: ("/originals/" in u.lower(), "/736x/" in u.lower(), "/564x/" in u.lower(), len(u)), reverse=True)
    items = [{"type": "image", "format": image_format(url),
              "quality": "Original / best available" if "/originals/" in url.lower() else "Available source image",
              "url": url, "download_url": "/api/download?url=" + quote(url, safe=""), "thumbnail": url} for url in images[:12]]
    return {"success": bool(items), "type": "image", "pin_url": final_url, "media": items,
            "message": "Pinterest image media found." if items else "No downloadable image was exposed by this public Pin."}


async def resolve(url: str) -> dict:
    errors = []
    client: httpx.AsyncClient = app.state.http
    canonical = url
    if host_of(url) == "pin.it":
        canonical = await resolve_short_url(client, url)
    try:
        final, media = await http_extract(canonical)
        # A public pin image is a valid fast-path result; do not launch Chromium unnecessarily.
        if media["videos"] or media["primary_images"] or media["images"] or media["posters"]:
            return build_result(final, media)
    except Exception as exc:
        LOG.info("HTTP extraction did not finish successfully: %s", exc)
        errors.append("http: " + str(exc)[:180])
    try:
        final, media = await browser_extract(canonical)
        if media["videos"] or media["primary_images"] or media["images"] or media["posters"]:
            return build_result(final, media)
    except Exception as exc:
        LOG.warning("Browser extraction failed: %s", exc)
        errors.append("browser: " + str(exc)[:180])
    raise RuntimeError("No public downloadable media was detected." + (" " + " | ".join(errors[-2:]) if errors else ""))


def quality_label(url: str) -> str:
    v = url.lower()
    for token, label in (("2160", "2160p (4K)"), ("4k", "2160p (4K)"), ("1440", "1440p"),
                         ("1080", "1080p (Full HD)"), ("720", "720p (HD)"), ("480", "480p"), ("360", "360p")):
        if token in v: return label
    return "Best Available MP4"


def image_format(url: str) -> str:
    match = re.search(r"\.(jpg|jpeg|png|webp|gif)(?:\?|$)", url, re.I)
    return (match.group(1) if match else "jpg").upper()


def rate_limit(request: Request):
    ip = request.client.host if request.client else "unknown"
    now = time.time(); hits = _rate_hits.setdefault(ip, deque())
    while hits and now - hits[0] > RATE_WINDOW: hits.popleft()
    if len(hits) >= RATE_MAX: raise HTTPException(429, "Too many requests. Please try again later.")
    hits.append(now)


@asynccontextmanager
async def lifespan(application: FastAPI):
    application.state.http = httpx.AsyncClient(follow_redirects=True, timeout=HTTP_TIMEOUT,
        limits=httpx.Limits(max_connections=40, max_keepalive_connections=20, keepalive_expiry=30))
    application.state.resolve_semaphore = asyncio.Semaphore(RESOLVE_CONCURRENCY)
    application.state.browser_semaphore = asyncio.Semaphore(BROWSER_CONCURRENCY)
    application.state.playwright = None
    application.state.browser = None
    try:
        application.state.playwright = await async_playwright().start()
        application.state.browser = await application.state.playwright.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled",
            "--disable-background-networking", "--disable-background-timer-throttling", "--disable-renderer-backgrounding"])
        LOG.info("Reusable Chromium started")
    except Exception:
        LOG.exception("Chromium startup failed; HTTP extraction will remain available")
    try:
        yield
    finally:
        if application.state.browser:
            await application.state.browser.close()
        if application.state.playwright:
            await application.state.playwright.stop()
        await application.state.http.aclose()


app = FastAPI(title=APP_NAME, version=VERSION, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False,
                   allow_methods=["*"], allow_headers=["*"], max_age=86400)


@app.get("/")
async def root():
    return {"service": APP_NAME, "version": VERSION, "status": "ok", "endpoint": "/api/resolve",
            "download": "/api/download?url=<pinimg-url>"}


@app.get("/health")
async def health():
    return {"status": "ok", "version": VERSION, "chromium": bool(getattr(app.state, "browser", None) and app.state.browser.is_connected())}


@app.get("/api/cors-test")
async def cors_test():
    return {"success": True, "status": "ok", "cors": "enabled", "version": VERSION,
            "message": "Browser requests to this public API are allowed."}


@app.post("/api/resolve")
async def api_resolve(body: ResolveBody, request: Request):
    rate_limit(request)
    url = str(body.url)
    if not is_allowed_pinterest_url(url): raise HTTPException(400, "Only public Pinterest and pin.it URLs are supported.")
    try:
        async with app.state.resolve_semaphore:
            result = await asyncio.wait_for(resolve(url), timeout=RESOLVE_TIMEOUT)
        return JSONResponse(result)
    except asyncio.TimeoutError:
        raise HTTPException(504, "Pinterest took too long to respond. Please try again.")
    except Exception as exc:
        raise HTTPException(422, str(exc)[:500])


async def stream_cdn(url: str, headers: dict, chunk_size: int):
    client = httpx.AsyncClient(follow_redirects=True, timeout=None, headers=headers)
    try:
        async with client.stream("GET", url) as upstream:
            if upstream.status_code >= 400:
                raise HTTPException(upstream.status_code, "Pinterest CDN refused the media request.")
            async for chunk in upstream.aiter_bytes(chunk_size): yield chunk
    finally:
        await client.aclose()


@app.get("/api/preview")
async def api_preview(url: str, request: Request):
    rate_limit(request)
    if not is_allowed_media_url(url): raise HTTPException(400, "Only Pinterest CDN media URLs are allowed.")
    headers = {"User-Agent": UA, "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
               "Referer": "https://www.pinterest.com/"}
    # One streamed request replaces the old Range probe plus second full request.
    async def checked_iterator():
        client = httpx.AsyncClient(follow_redirects=True, timeout=20, headers=headers)
        try:
            async with client.stream("GET", url) as upstream:
                if upstream.status_code >= 400: raise HTTPException(upstream.status_code, "Pinterest CDN refused the image request.")
                content_type = upstream.headers.get("content-type", "").split(";")[0].strip().lower()
                if not content_type.startswith("image/"): raise HTTPException(415, "The selected Pinterest media URL did not return an image.")
                async for chunk in upstream.aiter_bytes(256 * 1024): yield chunk
        finally: await client.aclose()
    guessed_type = mimetypes.guess_type(urlparse(url).path)[0] or "image/jpeg"
    return StreamingResponse(checked_iterator(), media_type=guessed_type, headers={"Content-Disposition": 'inline; filename="pinterest-image"',
        "Cache-Control": "public, max-age=300", "X-Content-Type-Options": "nosniff"})


@app.get("/api/download")
async def api_download(url: str, request: Request):
    rate_limit(request)
    if not is_allowed_media_url(url): raise HTTPException(400, "Only Pinterest CDN media URLs are allowed.")
    headers = {"User-Agent": UA, "Accept": "*/*", "Referer": "https://www.pinterest.com/"}
    if request.headers.get("range"): headers["Range"] = request.headers["range"]
    client = httpx.AsyncClient(follow_redirects=True, timeout=20, headers=headers)
    try:
        probe = await client.head(url)
        if probe.status_code in {405, 501}:
            probe = await client.get(url, headers={**headers, "Range": "bytes=0-0"})
        if probe.status_code >= 400: raise HTTPException(probe.status_code, "Pinterest CDN refused the media request.")
        content_type = probe.headers.get("content-type", "application/octet-stream")
        content_length = probe.headers.get("content-length")
        accept_ranges = probe.headers.get("accept-ranges", "bytes")
    finally:
        await client.aclose()
    response_headers = {"Content-Type": content_type, "Content-Disposition": 'attachment; filename="pinterest-media"',
                        "Cache-Control": "private, max-age=300", "Accept-Ranges": accept_ranges}
    # Do not forward the probe's Content-Length: the streaming response is a new request.
    return StreamingResponse(stream_cdn(url, headers, 1024 * 1024), headers=response_headers)
