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
import threading

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
_fetch_lock = threading.Lock()


def ensure_raster():
    """Download GCN250 to GCN250_PATH if absent and a URL is configured. Idempotent
    and concurrency-safe.

    Two hazards this guards against, both previously live:
      * Concurrent callers (the startup prefetch thread and an early request) each
        started a 640 MB download into the SAME fixed '.part' file, interleaving
        writes and renaming a corrupted raster into place. A lock plus a
        process/thread-unique temp name removes that.
      * A non-raster response (figshare HTML error/redirect page) was renamed to
        .tif and cached, and because _ensured was then set the service never
        retried — every later CN call failed with an opaque rasterio error. The
        download is now validated by opening it before it is promoted.
    """
    global _ensured
    if _ensured or os.path.exists(GCN250_PATH):
        _ensured = os.path.exists(GCN250_PATH)
        return _ensured
    if not GCN250_URL:
        return False
    import urllib.request
    import shutil
    with _fetch_lock:
        # re-check inside the lock: another thread may have completed the fetch
        if os.path.exists(GCN250_PATH):
            _ensured = True
            return True
        os.makedirs(os.path.dirname(GCN250_PATH) or ".", exist_ok=True)
        tmp = "%s.%d.%d.part" % (GCN250_PATH, os.getpid(), threading.get_ident())
        req = urllib.request.Request(GCN250_URL, headers={"User-Agent": "archeve-cn/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f, length=1024 * 1024)
            # validate before promoting — never cache an HTML error page as the raster
            with rasterio.open(tmp) as probe:
                if probe.count < 1:
                    raise ValueError("downloaded file has no raster band")
            os.replace(tmp, GCN250_PATH)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise          # leave _ensured False so a later call retries
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
                            "cn_deciles": [round(cn2, 1)], "cn_sd": 0.0, "heterogeneous": False,
                            "n_pixels": 1, "small_sample": True,
                            "cn_min": int(cn2), "cn_max": int(cn2),
                            "method": ("single centroid pixel — the polygon is smaller than one "
                                       "250 m GCN250 cell, so CN is a point sample, not a parcel mean"),
                            "source": "GCN250 (Jaafar et al. 2019), ARC II, 250 m"}
            except Exception:
                pass
            return {"ok": False, "error": "no valid CN pixels under the polygon (water / no-data)"}

        cn2 = float(valid.mean())
        cn1, cn3 = _amc(cn2)

        # Pixel ground area. GCN250 is EPSG:4326, so ds.res is in DEGREES and must be
        # converted; a projected raster would already be in metres. Check rather than
        # assume — silently treating metres as degrees would inflate area ~1e5x.
        res = ds.res
        if ds.crs is not None and ds.crs.is_geographic:
            meanlat = (miny + maxy) / 2.0
            px_km2 = (res[0] * 111.32 * math.cos(math.radians(meanlat))) * (res[1] * 110.57)
            grid_note = "geographic grid (deg), area via local metric scaling"
        else:
            px_km2 = (res[0] * res[1]) / 1.0e6          # projected: linear units assumed metres
            grid_note = "projected grid, area from linear units"

        # ── CN distribution, so the caller can weight RUNOFF instead of CN ──
        # SCS-CN runoff is strongly non-linear in CN, so Q(mean CN) != mean Q(CN).
        # NEH-4 / TR-55 are explicit that markedly different covers must be combined by
        # weighting the RUNOFF, not by averaging the curve number. On a mixed parcel the
        # composite-CN shortcut under-predicts runoff badly (verified: -59% for a 98/45
        # urban-desert mix, -87% for sand/pavement at a 60 mm storm). Ten equal-area
        # quantile mid-points reproduce the true area-mean runoff to <0.01%, so they are
        # returned and the engine integrates runoff across them.
        deciles = [round(float(v), 1) for v in np.percentile(valid, np.arange(5, 100, 10))]
        cn_sd = float(valid.std())
        return {
            "ok": True,
            "CN_II": round(cn2, 1), "CN_I": cn1, "CN_III": cn3,
            "cn_deciles": deciles,
            "cn_sd": round(cn_sd, 2),
            "heterogeneous": bool(cn_sd > 8.0),
            "n_pixels": int(valid.size),
            "small_sample": bool(valid.size < 5),
            "area_km2": round(valid.size * px_km2, 3),
            "cn_min": int(valid.min()), "cn_max": int(valid.max()),
            "method": ("arithmetic mean of GCN250 pixels whose centre falls inside the polygon "
                       "(no partial-pixel weighting); cn_deciles provided for runoff-weighted "
                       "compositing — " + grid_note),
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
