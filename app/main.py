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
