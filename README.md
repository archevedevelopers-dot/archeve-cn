# GCN250 Curve Number API

Authoritative **Curve Number** for a site polygon, read from the **GCN250** global
gridded CN raster (Jaafar, Ahmad & El Beyrouthy 2019 — 250 m, worldwide, ARC II).

A published CN that complements the web tool's land-cover × SoilGrids composite:
it answers *"what does the global GCN250 say the CN here is"* as a cross-check or
alternative source. Screening only — not a substitute for a site survey.

## What it is (honest)

| | |
|---|---|
| **Source** | GCN250 (Jaafar et al. 2019), 250 m global, Antecedent Runoff Condition **II** |
| **Output** | area-weighted CN(II) over the polygon + NEH-630 CN(I)/CN(III) |
| **NOT** | a hydraulic model, a soil survey, or a design basis |

## The raster (auto-fetched — no upload)

GCN250 ARC II is ~640 MB and is **not** in this repo or the image. The service
**downloads it itself on first boot** from the public figshare release
(`GCN250_URL`, default `https://ndownloader.figshare.com/files/15377363`) to
`GCN250_PATH`. So you deploy without uploading or hosting the raster.

- Mount a small (1 GB) volume and set `GCN250_PATH=/data/GCN250_ARCII.tif` to
  **cache** it across restarts (recommended).
- No volume → it downloads to `/tmp` on each cold start (fine, just slower).
- Air-gapped / already have the file → set `GCN250_PATH` to it and the download
  is skipped. (ARC I/III are derived from II via NEH-630, so only ARC II is needed.)

## Run

```bash
pip install -r cn/requirements.txt fastapi "uvicorn[standard]"
export GCN250_PATH=/path/to/GCN250_ARCII.tif
python3 cn/server.py                 # http://localhost:8810
# quick raster test (no server):
GCN250_PATH=/path/to/GCN250_ARCII.tif python3 cn/gcn_zonal.py
```

## API

```
GET  /health         -> { status, raster_present }
POST /gcn            body: { "geometry": <GeoJSON geometry> }   (Feature / FC also accepted)
                     -> { ok, CN_II, CN_I, CN_III, n_pixels, area_km2, cn_min, cn_max, source }
```

## Deploy

The raster auto-downloads, so deploy is just: ship `cn/`, the app fetches GCN250.

**Railway (no GitHub repo needed):**
```bash
npm i -g @railway/cli       # or: brew install railway
cd cn
railway login               # browser OAuth (only you can do this)
railway init                # name the project
railway up                  # builds the Dockerfile, deploys; app fetches the raster
railway domain              # → https://<name>.up.railway.app
```

**Render (Blueprint):** push a repo containing `cn/`, then Render → New → Blueprint →
pick the repo. `render.yaml` provisions the Docker service + a 1 GB cache disk.
→ `https://archeve-cn-gcn250.onrender.com`

**Docker (anywhere):**
```bash
docker build -t archeve-cn cn/ && docker run -p 8810:8810 archeve-cn   # fetches raster on boot
```

Then point the site at it (`flood-screening.html`):
```js
window.ARCHEVE_CN_API = 'https://<your-service>';   // STEP 2
```
…or, to test against the live site with no redeploy, open
`flood-screening.html?cn_api=https://<your-service>` (persists to localStorage).

CORS already allows `*.archeve.in`, Netlify/Vercel previews and localhost. The
site CSP `connect-src` already pre-allows `*.onrender.com` and `*.up.railway.app`.

## Why a backend (not in the browser)

GCN250 is a 610 MB raster — it cannot ship to the client. A tiny zonal-read service
keeps the global dataset server-side and returns just the CN for the drawn polygon.
The web tool's in-browser LULC×HSG CN remains the default and the fallback.
