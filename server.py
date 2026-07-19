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


# ── Stripe (premium water-level map) config ──────────────────────────────────
# STRIPE_SECRET_KEY is set on the host (never in code). Without it, the paid
# endpoints return 503 and the free data pack is unaffected.
STRIPE_KEY = os.environ.get("STRIPE_SECRET_KEY")
PREMIUM_PRICE_CENTS = int(os.environ.get("DATAPACK_PREMIUM_CENTS", "1000"))  # $10.00
SUCCESS_URL = os.environ.get(
    "DATAPACK_SUCCESS_URL", "https://aip.archeve.in/tool/flood-screening.html")
CANCEL_URL = os.environ.get("DATAPACK_CANCEL_URL", SUCCESS_URL)
PREMIUM_DIR = os.environ.get("DATAPACK_PREMIUM_DIR", "/tmp/archeve_premium")


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
    import traceback
    from fastapi.responses import FileResponse
    import datapack as dp
    geom = _extract_geom(req)
    if not geom:
        raise HTTPException(status_code=400, detail="No geometry/feature in request body.")
    try:
        zip_path, meta = dp.build(geom, premium=bool(req.premium))
    except Exception as ex:  # surface the real cause instead of an opaque 500
        tb = traceback.format_exc()
        print("[datapack] build crashed:\n" + tb, flush=True)
        raise HTTPException(status_code=500, detail="datapack build error: %s: %s"
                            % (type(ex).__name__, ex))
    if zip_path is None:
        raise HTTPException(status_code=422, detail=meta.get("error", "data pack failed"))
    return FileResponse(zip_path, media_type="application/zip", filename="archeve_site_datapack.zip",
                        headers={"X-Datapack-Meta": str(meta)})


@app.post("/datapack/checkout")
def datapack_checkout(req: Req):
    """Build the premium pack; if a water-level layer is actually available for
    this site, open a $10 Stripe Checkout for it and stash the built pack under a
    token so it can be served after payment. Returns {available, checkout_url|reason}."""
    import json
    import secrets
    import shutil
    import traceback
    import datapack as dp
    if not STRIPE_KEY:
        raise HTTPException(status_code=503, detail="Payments are not configured yet.")
    geom = _extract_geom(req)
    if not geom:
        raise HTTPException(status_code=400, detail="No geometry/feature in request body.")
    try:
        zip_path, meta = dp.build(geom, premium=True)
    except Exception as ex:
        print("[checkout] build crashed:\n" + traceback.format_exc(), flush=True)
        raise HTTPException(status_code=500, detail="datapack build error: %s: %s"
                            % (type(ex).__name__, ex))
    if zip_path is None:
        raise HTTPException(status_code=422, detail=meta.get("error", "data pack failed"))
    if not meta.get("premium"):
        # inland / no coastal flood reaches the site — never sell an empty layer
        return {"available": False,
                "reason": "No coastal flood reaches this site — the water-level map is a "
                          "coastal product and does not apply to an inland location."}
    # stash geometry + the built pack under a random token
    os.makedirs(PREMIUM_DIR, exist_ok=True)
    token = secrets.token_urlsafe(16)
    with open(os.path.join(PREMIUM_DIR, token + ".json"), "w") as f:
        json.dump(geom, f)
    shutil.copy(zip_path, os.path.join(PREMIUM_DIR, token + ".zip"))

    import stripe
    stripe.api_key = STRIPE_KEY
    sep = "&" if "?" in SUCCESS_URL else "?"
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{
                "quantity": 1,
                "price_data": {
                    "currency": "usd",
                    "unit_amount": PREMIUM_PRICE_CENTS,
                    "product_data": {
                        "name": "Archeve — Site Water-Level Map (WSE)",
                        "description": "Approx coastal water-surface elevation clipped to your "
                                       "site, 30 m GeoTIFF (EPSG:4326).",
                    },
                },
            }],
            metadata={"token": token},
            success_url=SUCCESS_URL + sep + "premium_token=" + token + "&session_id={CHECKOUT_SESSION_ID}",
            cancel_url=CANCEL_URL,
        )
    except Exception as ex:
        print("[checkout] stripe error: %s" % ex, flush=True)
        raise HTTPException(status_code=502, detail="Could not open checkout: %s" % ex)
    return {"available": True, "checkout_url": session.url,
            "price_usd": PREMIUM_PRICE_CENTS / 100.0}


@app.get("/datapack/premium")
def datapack_premium(token: str, session_id: str):
    """Serve the paid water-level pack — only after Stripe confirms the session is paid."""
    import json
    from fastapi.responses import FileResponse
    if not STRIPE_KEY:
        raise HTTPException(status_code=503, detail="Payments are not configured yet.")
    import stripe
    stripe.api_key = STRIPE_KEY
    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception as ex:
        raise HTTPException(status_code=400, detail="Could not verify payment: %s" % ex)
    if session.get("payment_status") != "paid":
        raise HTTPException(status_code=402, detail="Payment not completed.")
    if (session.get("metadata") or {}).get("token") != token:
        raise HTTPException(status_code=403, detail="Token does not match this payment.")
    zpath = os.path.join(PREMIUM_DIR, token + ".zip")
    if not os.path.exists(zpath):
        # container recycled between checkout and return — rebuild from stored geometry
        gpath = os.path.join(PREMIUM_DIR, token + ".json")
        if not os.path.exists(gpath):
            raise HTTPException(status_code=410,
                                detail="This purchase expired — email info@archeve.in with your receipt.")
        import datapack as dp
        zpath, meta = dp.build(json.load(open(gpath)), premium=True)
        if zpath is None:
            raise HTTPException(status_code=500,
                                detail="Rebuild failed — email info@archeve.in with your receipt.")
    return FileResponse(zpath, media_type="application/zip",
                        filename="archeve_site_datapack_premium.zip")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8810)))
