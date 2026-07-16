#!/usr/bin/env python3
"""
Archeve — GCN250 zonal Curve Number API (FastAPI).

POST a site polygon → get the authoritative GCN250 Curve Number (ARC II + AMC
I/III) over it. The flood-screening page calls this as a CN cross-check / source.

Run:
  pip install -r cn/requirements.txt fastapi "uvicorn[standard]"
  export GCN250_PATH=/path/to/GCN250_ARCII.tif
  uvicorn server:app --host 0.0.0.0 --port 8810      # from inside cn/
  # or:  python3 cn/server.py

Then point the web page at it:  window.ARCHEVE_CN_API = 'http://localhost:8810'

Endpoints:
  GET  /health
  POST /gcn   body: { "geometry": <GeoJSON geometry> }  (also Feature / FeatureCollection)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gcn_zonal as gz  # noqa: E402

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from typing import Any, Optional  # noqa: E402

app = FastAPI(title="Archeve GCN250 Curve Number", version="1.0")

# production site + subdomains, common preview hosts, local dev
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?"
    r"|https://([a-z0-9-]+\.)*archeve\.in"
    r"|https://[a-z0-9-]+\.(netlify\.app|vercel\.app|pages\.dev)",
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


class Req(BaseModel):
    geometry: Optional[dict] = None
    type: Optional[str] = None
    features: Optional[list] = None
    premium: Optional[bool] = False


def _extract_geom(req: Req):
    if req.geometry:
        return req.geometry
    if req.type == "Feature" and isinstance(req.dict().get("geometry"), dict):
        return req.dict()["geometry"]
    if req.type == "FeatureCollection" and req.features:
        for f in req.features:
            if isinstance(f, dict) and f.get("geometry"):
                return f["geometry"]
    return None


@app.on_event("startup")
def _prefetch_raster():
    # fetch GCN250 in the background so the first /gcn isn't blocked on a 640 MB download
    import threading
    if not os.path.exists(gz.GCN250_PATH) and gz.GCN250_URL:
        threading.Thread(target=lambda: gz.ensure_raster(), daemon=True).start()


@app.get("/health")
def health():
    ok = os.path.exists(gz.GCN250_PATH)
    return {"status": "ok" if ok else "fetching_raster",
            "gcn250_path": gz.GCN250_PATH, "raster_present": ok,
            "gcn250_url": gz.GCN250_URL or None}


@app.post("/gcn")
def gcn(req: Req):
    geom = _extract_geom(req)
    if not geom:
        raise HTTPException(status_code=400, detail="No geometry/feature in request body.")
    res = gz.zonal_cn(geom)
    if not res.get("ok"):
        raise HTTPException(status_code=422, detail=res.get("error", "zonal CN failed"))
    return res


@app.post("/datapack")
def datapack(req: Req):
    """Site polygon -> zip of SCS-CN GeoTIFFs (CN, retention S, initial abstraction Ia)."""
    from fastapi.responses import FileResponse
    import datapack as dp
    geom = _extract_geom(req)
    if not geom:
        raise HTTPException(status_code=400, detail="No geometry/feature in request body.")
    zip_path, meta = dp.build(geom, premium=bool(req.premium))
    if zip_path is None:
        raise HTTPException(status_code=422, detail=meta.get("error", "data pack failed"))
    return FileResponse(zip_path, media_type="application/zip", filename="archeve_site_datapack.zip",
                        headers={"X-Datapack-Meta": str(meta)})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8810)))
