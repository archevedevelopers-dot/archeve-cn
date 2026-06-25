#!/usr/bin/env python3
"""
Archeve — GCN250 zonal Curve Number.

Reads the GCN250 global gridded Curve Number raster (Jaafar, Ahmad & El Beyrouthy
2019 — 250 m, worldwide, Antecedent Runoff Condition II) and returns the
area-weighted CN over a site polygon, plus the NEH-630 AMC I / III conversions.

This is an AUTHORITATIVE, published CN — a cross-check (or alternative source)
for the web tool's land-cover × SoilGrids composite. Screening only.

The 610 MB raster is NOT bundled. Point GCN250_PATH at it (mounted volume, or a
copy downloaded at deploy time). Get GCN250 from the authors' open release.

Windowed read via rasterio.mask → only the tiles under the polygon are decoded,
so this stays fast even on the global raster.
"""
import os
import math

import numpy as np
import rasterio
from rasterio.mask import mask
from shapely.geometry import shape, mapping

GCN250_PATH = os.environ.get("GCN250_PATH", "/tmp/GCN250_ARCII.tif")
# Public GCN250 ARC II GeoTIFF (figshare, Jaafar & Ahmad 2019, 640 MB). Lets the
# service fetch the raster itself — no manual upload / persistent disk needed.
GCN250_URL = os.environ.get("GCN250_URL", "https://ndownloader.figshare.com/files/15377363")
NODATA = 255
MAX_DEG = 1.0  # ~110 km bbox guard — screening scale

_ensured = False


def ensure_raster():
    """Download GCN250 to GCN250_PATH if absent and a URL is configured. Idempotent.
    Streams to a .part file then renames, so a partial download is never used."""
    global _ensured
    if _ensured or os.path.exists(GCN250_PATH):
        _ensured = True
        return os.path.exists(GCN250_PATH)
    if not GCN250_URL:
        return False
    import urllib.request
    import shutil
    tmp = GCN250_PATH + ".part"
    os.makedirs(os.path.dirname(GCN250_PATH) or ".", exist_ok=True)
    req = urllib.request.Request(GCN250_URL, headers={"User-Agent": "archeve-cn/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, length=1024 * 1024)
    os.replace(tmp, GCN250_PATH)
    _ensured = True
    return True


def _amc(cn2):
    """CN(II) → CN(I) dry / CN(III) wet, NEH-630 (Hawkins) — matches the JS engine."""
    cn1 = cn2 / (2.281 - 0.01281 * cn2)
    cn3 = cn2 / (0.427 + 0.00573 * cn2)
    return round(cn1, 1), round(cn3, 1)


def zonal_cn(geom, path=None):
    """geom: a GeoJSON geometry dict (WGS84 lon/lat). Returns a result dict."""
    path = path or GCN250_PATH
    if not os.path.exists(path):
        try:
            ensure_raster()
        except Exception as e:
            return {"ok": False, "error": "GCN250 raster fetch failed: %s" % e}
    if not os.path.exists(path):
        return {"ok": False, "error": "GCN250 raster not available at %s — set GCN250_PATH or GCN250_URL." % path}
    try:
        g = shape(geom)
    except Exception as e:
        return {"ok": False, "error": "bad geometry: %s" % e}
    if g.is_empty:
        return {"ok": False, "error": "empty geometry"}

    minx, miny, maxx, maxy = g.bounds
    if (maxx - minx) > MAX_DEG or (maxy - miny) > MAX_DEG:
        return {"ok": False, "error": "bbox too large for screening (> %.1f deg)" % MAX_DEG}

    with rasterio.open(path) as ds:
        try:
            out, _ = mask(ds, [mapping(g)], crop=True, nodata=NODATA, filled=True)
        except Exception as e:
            return {"ok": False, "error": "raster mask failed: %s" % e}
        band = out[0].astype("float32")
        valid = band[(band > 0) & (band <= 100)]

        if valid.size == 0:
            # polygon smaller than a 250 m pixel, or all water/no-data → nearest pixel
            c = g.centroid
            try:
                v = list(ds.sample([(c.x, c.y)]))[0][0]
                if 0 < v <= 100:
                    cn2 = float(v)
                    cn1, cn3 = _amc(cn2)
                    return {"ok": True, "CN_II": round(cn2, 1), "CN_I": cn1, "CN_III": cn3,
                            "n_pixels": 1, "method": "centroid pixel (polygon < 250 m)",
                            "source": "GCN250 (Jaafar et al. 2019), ARC II, 250 m"}
            except Exception:
                pass
            return {"ok": False, "error": "no valid CN pixels under the polygon (water / no-data)"}

        cn2 = float(valid.mean())
        cn1, cn3 = _amc(cn2)
        res = ds.res  # degrees/pixel
        meanlat = (miny + maxy) / 2.0
        px_km2 = (res[0] * 111.32 * math.cos(math.radians(meanlat))) * (res[1] * 110.57)
        return {
            "ok": True,
            "CN_II": round(cn2, 1), "CN_I": cn1, "CN_III": cn3,
            "n_pixels": int(valid.size),
            "area_km2": round(valid.size * px_km2, 3),
            "cn_min": int(valid.min()), "cn_max": int(valid.max()),
            "method": "area-weighted mean of GCN250 pixels within the polygon",
            "source": "GCN250 global gridded Curve Number (Jaafar, Ahmad & El Beyrouthy 2019), ARC II, 250 m",
        }


if __name__ == "__main__":
    import json
    import sys
    # quick CLI test: pass a GeoJSON geometry file, or use a default Riyadh box
    if len(sys.argv) > 1:
        geom = json.load(open(sys.argv[1]))
        if geom.get("type") == "Feature":
            geom = geom["geometry"]
    else:
        geom = {"type": "Polygon", "coordinates": [[
            [46.66, 24.68], [46.78, 24.68], [46.78, 24.78], [46.66, 24.78], [46.66, 24.68]]]}
    print(json.dumps(zonal_cn(geom), indent=2))
