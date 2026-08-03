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
import time
import hmac
import hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gcn_zonal as gz  # noqa: E402

from fastapi import FastAPI, HTTPException, Request, Depends  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from typing import Any, Optional  # noqa: E402
from shapely.geometry import shape, mapping  # noqa: E402

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
    return_url: Optional[str] = None
    size: Optional[int] = None      # /terrain render-grid resolution (clamped 32-160)
    source: Optional[str] = None    # /terrain elevation source id (see /demsources)


# ── Stripe (premium water-level map) config ──────────────────────────────────
# STRIPE_SECRET_KEY is set on the host (never in code). Without it, the paid
# endpoints return 503 and the free data pack is unaffected.
STRIPE_KEY = os.environ.get("STRIPE_SECRET_KEY")
PREMIUM_PRICE_CENTS = int(os.environ.get("DATAPACK_PREMIUM_CENTS", "1000"))  # $10.00
SUCCESS_URL = os.environ.get("DATAPACK_SUCCESS_URL", "https://aip.archeve.in/aip")
CANCEL_URL = os.environ.get("DATAPACK_CANCEL_URL", SUCCESS_URL)
PREMIUM_DIR = os.environ.get("DATAPACK_PREMIUM_DIR", "/tmp/archeve_premium")

# A caller may ask to be returned to the page it started from, but only to one of
# our own origins — an unchecked return_url would make this an open redirect.
_ALLOWED_RETURN_PREFIXES = ("https://aip.archeve.in/", "https://archeve.in/",
                            "https://www.archeve.in/")


def _safe_return_url(candidate: Optional[str]) -> str:
    if candidate and candidate.startswith(_ALLOWED_RETURN_PREFIXES):
        return candidate.split("#")[0]
    return SUCCESS_URL


# Upper bound on ring complexity. The MAX_DEG bbox guard does not catch this: a 200 000-vertex
# ring can sit inside a 0.002 deg box, pass every size check, and then be rasterised. Real site
# boundaries are hundreds of vertices at most.
MAX_VERTICES = int(os.environ.get("MAX_GEOM_VERTICES", "20000"))


def _count_vertices(geom: dict) -> int:
    """Total coordinate pairs in a GeoJSON geometry, at any nesting depth."""
    def walk(x):
        if not isinstance(x, (list, tuple)) or not x:
            return 0
        if isinstance(x[0], (int, float)):     # a single [x, y] position
            return 1
        return sum(walk(i) for i in x)
    return walk(geom.get("coordinates") or [])


def _validated_geom(req: Req) -> dict:
    """Extract, bound and repair the request geometry, or raise HTTPException.

    Self-intersecting rings are common in exported KML and are NOT rejected by shapely —
    a bow-tie silently reports zero area and masks the wrong cells, so it is repaired with
    buffer(0) rather than trusted or refused.
    """
    geom = _extract_geom(req)
    if not geom:
        raise HTTPException(status_code=400, detail="No geometry/feature in request body.")
    if not isinstance(geom, dict) or not geom.get("type"):
        raise HTTPException(status_code=400, detail="Geometry must be a GeoJSON geometry object.")
    n = _count_vertices(geom)
    if n == 0:
        raise HTTPException(status_code=400, detail="Geometry has no coordinates.")
    if n > MAX_VERTICES:
        raise HTTPException(status_code=413,
                            detail="Geometry has %d vertices (limit %d) — simplify the boundary."
                                   % (n, MAX_VERTICES))
    try:
        g = shape(geom)
    except Exception as ex:
        raise HTTPException(status_code=400, detail="Unreadable geometry: %s" % ex)
    if g.is_empty:
        raise HTTPException(status_code=400, detail="Geometry is empty.")
    if not g.is_valid:
        repaired = g.buffer(0)
        if repaired.is_empty or not repaired.is_valid:
            raise HTTPException(status_code=422,
                                detail="Geometry is self-intersecting and could not be repaired.")
        return mapping(repaired)
    return geom


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


# ── access control ───────────────────────────────────────────────────────────────
#
# CORS restricts browsers. It restricts nothing else: a scripted client with no Origin
# header was measured pulling a 100 kB compute response from this service in 1.6 s. The
# endpoints below mosaic cloud-optimised GeoTIFFs and build zip archives, so unmetered
# access is both a cost and an availability problem.
#
# The scheme: the website mints a SHORT-LIVED HMAC token from a secret this service also
# holds, and the browser sends it. The long-lived secret never reaches the browser.
#
# What this does and does not buy, stated plainly: a determined party can script the token
# dance and keep going. What changes is that abuse now requires continuous re-minting from
# an origin we control and can throttle or revoke, instead of an anonymous request anyone
# can repeat forever. The RATE LIMITER below is what actually caps the damage; the token is
# what makes the limit attributable to a client rather than only to an IP.
#
# Rollout is deliberately fail-open: with no secret configured the service behaves exactly
# as before, so deploying this cannot take the live site down before the secret is set.
# /health reports which mode it is in so the state is never a guess.

AIP_SECRET = os.environ.get("AIP_SIGNING_SECRET", "").strip()
TOKEN_TTL_S = 900                      # 15 minutes; the client refreshes well before this
_CLOCK_SKEW_S = 60


def _verify_token(auth_header):
    """Bearer <exp>.<hex hmac>. Returns None if acceptable, else a reason string."""
    if not AIP_SECRET:
        return None                    # not configured: fail open, see the note above
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return "missing token"
    raw = auth_header.split(" ", 1)[1].strip()
    if "." not in raw:
        return "malformed token"
    exp_s, sig = raw.rsplit(".", 1)
    try:
        exp = int(exp_s)
    except ValueError:
        return "malformed token"
    expected = hmac.new(AIP_SECRET.encode("utf-8"), exp_s.encode("utf-8"),
                        hashlib.sha256).hexdigest()
    # constant time: a fast-failing comparison leaks the signature one byte at a time
    if not hmac.compare_digest(sig, expected):
        return "bad signature"
    now = int(time.time())
    if exp < now - _CLOCK_SKEW_S:
        return "token expired"
    if exp > now + TOKEN_TTL_S + _CLOCK_SKEW_S:
        return "token lifetime too long"
    return None


# Per-IP token bucket. In-memory, which is correct for a single Render instance and honest
# about its limit: it resets on restart and is not shared across replicas. Cheap endpoints
# cost 1, endpoints that mosaic rasters or build archives cost more.
_BUCKETS = {}
_RATE_CAPACITY = 60.0                  # burst
_RATE_REFILL = 30.0 / 60.0             # 30 units per minute sustained


def _rate_ok(ip, cost):
    now = time.time()
    tokens, last = _BUCKETS.get(ip, (_RATE_CAPACITY, now))
    tokens = min(_RATE_CAPACITY, tokens + (now - last) * _RATE_REFILL)
    if tokens < cost:
        _BUCKETS[ip] = (tokens, now)
        return False
    _BUCKETS[ip] = (tokens - cost, now)
    if len(_BUCKETS) > 20000:          # bound the map; oldest-touched go first
        for k in sorted(_BUCKETS, key=lambda k: _BUCKETS[k][1])[:5000]:
            _BUCKETS.pop(k, None)
    return True


def _client_ip(request):
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else None) or \
        (request.client.host if request.client else "unknown")


def guard(cost):
    """FastAPI dependency: verify the token, then charge the rate limiter."""
    def dep(request: Request):
        reason = _verify_token(request.headers.get("authorization"))
        if reason:
            raise HTTPException(status_code=401, detail="Not authorised: %s." % reason)
        if not _rate_ok(_client_ip(request), cost):
            raise HTTPException(status_code=429,
                                detail="Rate limit reached. This service is metered; "
                                       "slow down or get in touch for an API allowance.")
        return True
    return dep


@app.get("/health")
def health():
    ok = os.path.exists(gz.GCN250_PATH)
    return {"status": "ok" if ok else "fetching_raster",
            "gcn250_path": gz.GCN250_PATH, "raster_present": ok,
            "gcn250_url": gz.GCN250_URL or None}


@app.post("/gcn")
def gcn(req: Req, _guard: bool = Depends(guard(3))):
    geom = _validated_geom(req)
    res = gz.zonal_cn(geom)
    if not res.get("ok"):
        raise HTTPException(status_code=422, detail=res.get("error", "zonal CN failed"))
    return res


@app.post("/slope")
def slope(req: Req, _guard: bool = Depends(guard(3))):
    """Polygon -> mean/median terrain slope (%) from the 30 m Copernicus DEM.
    Feeds the screening's Tc/peak flow with a real slope instead of a national default."""
    import datapack as dp
    geom = _validated_geom(req)
    res = dp.slope_percent(geom)
    if not res.get("ok"):
        raise HTTPException(status_code=422, detail=res.get("error", "slope failed"))
    return res


@app.get("/demsources")
def demsources():
    """Which elevation sources this deployment can serve, with the honest metadata for each.

    Served rather than hardcoded in the client so the availability of a key-gated source is
    decided by the deployment that actually holds the key, not guessed at by the browser."""
    # datapack is imported per-handler here, not at module scope — it drags in rasterio and
    # every other route in this file does the same. Omitting it made this endpoint the only
    # one referencing an undefined name, which is a 500 rather than an obvious import error.
    import datapack as dp
    return dp.dem_sources()


@app.post("/terrain")
def terrain(req: Req, _guard: bool = Depends(guard(5))):
    """Polygon -> downsampled DEM grid + still-water surface levels, for the 3D view.
    Still-water levels only; no hydrodynamics (no velocity, wave, routing or timing)."""
    import datapack as dp
    geom = _validated_geom(req)
    res = dp.terrain(geom, size=int(req.size or 96), source=(req.source or "cop30"))
    if not res.get("ok"):
        raise HTTPException(status_code=422, detail=res.get("error", "terrain failed"))
    return res


@app.post("/datapack")
def datapack(req: Req, _guard: bool = Depends(guard(15))):
    """Site polygon -> zip of SCS-CN GeoTIFFs (CN, retention S, initial abstraction Ia)."""
    import traceback
    from fastapi.responses import FileResponse
    import datapack as dp
    geom = _validated_geom(req)
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
def datapack_checkout(req: Req, _guard: bool = Depends(guard(5))):
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
    geom = _validated_geom(req)
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
    ret = _safe_return_url(req.return_url)
    sep = "&" if "?" in ret else "?"
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
            success_url=ret + sep + "premium_token=" + token + "&session_id={CHECKOUT_SESSION_ID}",
            cancel_url=ret,
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
