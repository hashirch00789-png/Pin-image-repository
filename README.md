# Pinterest Media Resolver v1.3 — image extraction fix

This update extends the existing resolver to detect public Pinterest image URLs embedded in Relay/SSR JSON and raw HTML, returns available image candidates (not just one), and lets the HTTP extraction path return images without requiring the browser fallback.

## Deploy to Render
1. Back up the current GitHub repository.
2. Upload/replace the repository files with the contents of this folder, keeping `app/main.py`, `Dockerfile`, `requirements.txt`, and `render.yaml` at the repository root / expected paths.
3. Commit the changes and wait for Render to deploy successfully.
4. Confirm `https://YOUR-RENDER-SERVICE.onrender.com/health` returns JSON with version `1.3.0`.
5. In Elementor, replace the old image downloader HTML with `pinterest-image-downloader-elementor-seo-fixed.html`.

The API supports publicly accessible Pins only. Pinterest may block some Pins or expose only limited media. The resolver does not bypass private/restricted access and does not upscale images.
