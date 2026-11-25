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
