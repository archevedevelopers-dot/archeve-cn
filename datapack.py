#!/usr/bin/env python3
"""
Archeve — Site Data Pack (v2).

Given a site polygon, build a co-registered stack of screening grids clipped to
the site and zipped as GeoTIFFs for GIS (QGIS / ArcGIS). All layers are resampled
onto a common ~30 m grid (EPSG:4326) defined by the Copernicus DEM:

  cn_arcii.tif            Curve Number, ARC II            (GCN250, 250 m -> 30 m)
  retention_S.tif         S = 25400/CN - 254   (mm)
  ia_initial_abstraction.tif  Ia = 0.2 S       (mm)
  dem_30m.tif             elevation            (Copernicus GLO-30, m)
  mannings_n.tif          overland Manning's n (from ESA WorldCover land cover)
  flood_hazard.tif        banded hazard 1-4    (from Deltares coastal depth)
  water_level_wse.tif     approx water surface = DEM + depth  (m)   [premium]

Sources are open (CC BY 4.0 / public domain). Screening-grade, not surveyed.
External layers are best-effort: if a source is unavailable the pack still builds
with the layers that succeeded, and README lists what was produced.
"""
import os
import zipfile
import tempfile
import math
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import traceback
import urllib.request

import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.mask import mask as rio_mask
from rasterio.merge import merge as rio_merge
from rasterio.warp import reproject, Resampling
from rasterio.features import geometry_mask
from rasterio.windows import from_bounds
from shapely.geometry import shape, mapping

import gcn_zonal as gz

NODATA_F = -9999.0
MAX_DEG = float(os.environ.get("DATAPACK_MAX_DEG", "0.5"))  # ~55 km — screening scale guard
# Cell budget for the 10 m WorldCover read (see the Manning's-n block). ~8 M cells is
# comfortably within the 512 MB instance once the reprojection buffers are counted.
MAX_WORLDCOVER_CELLS = float(os.environ.get("DATAPACK_MAX_WC_CELLS", "8e6"))
# Minimum window for reading the ~1 km coastal depth grid. Smaller windows return one or
# two cells that are often entirely nodata, which reads as "dry" on a flooded site.
FLOOD_WINDOW_DEG = float(os.environ.get("FLOOD_WINDOW_DEG", "0.09"))   # ~10 km
FLOOD_API = os.environ.get("FLOOD_API", "https://archeve-flood.onrender.com")

# WorldCover class -> overland-flow Manning's n (sheet flow, screening)
WC_MANNING = {10: 0.40, 20: 0.40, 30: 0.35, 40: 0.35, 50: 0.02,
              60: 0.05, 70: 0.01, 80: 0.03, 90: 0.10, 95: 0.14, 100: 0.10}


def _cop_dem_urls(w, s, e, n, res=30):
    """Copernicus DEM COG tiles. GLO-30 tiles are named COG_10, GLO-90 COG_30 — the number
    is the tile's arc-second grid spacing, not the metre resolution, which trips people up."""
    bucket = "copernicus-dem-30m" if res == 30 else "copernicus-dem-90m"
    code = "10" if res == 30 else "30"
    urls = []
    for lat in range(int(math.floor(s)), int(math.floor(n)) + 1):
        for lon in range(int(math.floor(w)), int(math.floor(e)) + 1):
            ns, ew = ("N" if lat >= 0 else "S"), ("E" if lon >= 0 else "W")
            tile = "Copernicus_DSM_COG_%s_%s%02d_00_%s%03d_00_DEM" % (code, ns, abs(lat), ew, abs(lon))
            urls.append("/vsicurl/https://%s.s3.amazonaws.com/%s/%s.tif" % (bucket, tile, tile))
    return urls


# ── open elevation sources the user can choose between ───────────────────────────
#
# Every field here is shown to the user, so every field has to be true. The accuracy
# figures are the published LE90 vertical accuracies for each programme; they are
# approximate and vary by terrain, which is stated rather than hidden.
#
# `commercial` matters because this tool produces deliverables that get sold. A dataset
# that is free for research but not for commercial use must never appear as a plain
# download here — that is a licensing problem for the user, not for us.
DEM_SOURCES = {
    "cop30": {
        "id": "cop30", "name": "Copernicus GLO-30", "res_m": 30, "kind": "DSM",
        "accuracy_m": 4, "coverage": "global", "epoch": "2011-2015",
        "licence": "Free, attribution (ESA/Copernicus)", "commercial": True, "key": False,
        "note": "Best-in-class free 30 m surface model. The default here.",
    },
    "cop90": {
        "id": "cop90", "name": "Copernicus GLO-90", "res_m": 90, "kind": "DSM",
        "accuracy_m": 4, "coverage": "global", "epoch": "2011-2015",
        "licence": "Free, attribution (ESA/Copernicus)", "commercial": True, "key": False,
        "note": "Same source as GLO-30, coarser grid. Useful for large catchments.",
    },
    "srtm": {
        "id": "srtm", "name": "SRTM GL1", "res_m": 30, "kind": "DSM",
        "accuracy_m": 9, "coverage": "60N-56S", "epoch": "2000",
        "licence": "Public domain (NASA/USGS)", "commercial": True, "key": True,
        "note": "The 2000 shuttle mission. Ages badly where terrain has changed since.",
    },
    "nasadem": {
        "id": "nasadem", "name": "NASADEM", "res_m": 30, "kind": "DSM",
        "accuracy_m": 6, "coverage": "60N-56S", "epoch": "2000 (reprocessed 2020)",
        "licence": "Public domain (NASA)", "commercial": True, "key": True,
        "note": "SRTM reprocessed with better voids and geolocation. Prefer over raw SRTM.",
    },
    "alos": {
        "id": "alos", "name": "ALOS AW3D30", "res_m": 30, "kind": "DSM",
        "accuracy_m": 5, "coverage": "global", "epoch": "2006-2011",
        "licence": "Free, attribution (JAXA)", "commercial": True, "key": True,
        "note": "JAXA. Comparable to Copernicus; a useful independent check.",
    },
}

# OpenTopography dataset codes for the sources that come through their API
_OT_CODE = {"srtm": "SRTMGL1", "nasadem": "NASADEM", "alos": "AW3D30"}
_OT_URL = "https://portal.opentopography.org/API/globaldem"


def dem_sources():
    """The registry, with each source marked available or not from THIS deployment.

    A source that needs an API key we do not have is reported unavailable with the reason,
    rather than being hidden — the user should be able to see what the platform could offer
    and what is missing to enable it."""
    have_key = bool(os.environ.get("OPENTOPOGRAPHY_API_KEY"))
    out = []
    for src in DEM_SOURCES.values():
        d = dict(src)
        d["available"] = (not src["key"]) or have_key
        d["unavailable_reason"] = None if d["available"] else \
            "needs OPENTOPOGRAPHY_API_KEY on the service"
        out.append(d)
    return {"ok": True, "sources": out, "default": "cop30"}


def _opentopo_dataset(source, w, s, e, n):
    """Fetch a source served by OpenTopography and open it with rasterio, in memory.

    Their endpoint returns a GeoTIFF for the bbox. It is not range-request friendly, so it
    is pulled whole rather than read through /vsicurl."""
    key = os.environ.get("OPENTOPOGRAPHY_API_KEY")
    if not key:
        raise RuntimeError("OPENTOPOGRAPHY_API_KEY is not set on this service")
    code = _OT_CODE.get(source)
    if not code:
        raise RuntimeError("unknown OpenTopography source %r" % source)
    qs = urlencode({"demtype": code, "south": s, "north": n, "west": w, "east": e,
                    "outputFormat": "GTiff", "API_Key": key})
    req = Request(_OT_URL + "?" + qs, headers={"User-Agent": "archeve-aip"})
    with urlopen(req, timeout=90) as r:
        body = r.read()
    if body[:2] not in (b"II", b"MM"):
        # the API reports errors as text/XML with a 200 in some cases
        raise RuntimeError("OpenTopography returned no raster: %s"
                           % body[:180].decode("utf-8", "replace"))
    return MemoryFile(body)


def _worldcover_urls(w, s, e, n):
    urls = []
    for lat in range(int(math.floor(s / 3) * 3), int(math.floor(n / 3) * 3) + 1, 3):
        for lon in range(int(math.floor(w / 3) * 3), int(math.floor(e / 3) * 3) + 1, 3):
            ns, ew = ("N" if lat >= 0 else "S"), ("E" if lon >= 0 else "W")
            name = "ESA_WorldCover_10m_2021_v200_%s%02d%s%03d_Map" % (ns, abs(lat), ew, abs(lon))
            urls.append("/vsicurl/https://esa-worldcover.s3.amazonaws.com/v200/2021/map/%s.tif" % name)
    return urls


def _open_ok(url):
    try:
        return rasterio.open(url)
    except Exception:
        return None


def _mosaic(urls, bounds):
    dss = [d for d in (_open_ok(u) for u in urls) if d is not None]
    if not dss:
        return None, None
    try:
        arr, transform = rio_merge(dss, bounds=bounds)
        return arr[0], transform
    except Exception:
        return None, None
    finally:
        for d in dss:
            try: d.close()
            except Exception: pass


def _reproject_to(src_arr, src_transform, src_crs, dst_transform, dst_shape, dst_crs,
                  resampling=Resampling.nearest, src_nodata=None):
    dst = np.full(dst_shape, NODATA_F, dtype="float32")
    # asarray, not astype: astype ALWAYS copies, so an already-float32 source was being
    # duplicated in full. On a 10 m WorldCover mosaic that is a needless ~144 MB at the
    # 0.5 deg limit and was a direct contributor to OOM-kills on the 512 MB instance.
    src_f32 = np.asarray(src_arr, dtype="float32")
    reproject(source=src_f32, destination=dst,
              src_transform=src_transform, src_crs=src_crs,
              dst_transform=dst_transform, dst_crs=dst_crs,
              src_nodata=src_nodata, dst_nodata=NODATA_F, resampling=resampling)
    return dst


def _fetch_flood_depth(w, s, e, n, rp=100, scenario="today"):
    """GeoTIFF depth clip from the flood service; return (arr, transform, crs) or None."""
    url = ("%s/download?bbox=%.4f,%.4f,%.4f,%.4f&rp=%d&scenario=%s&name=site"
           % (FLOOD_API, w, s, e, n, int(rp), scenario))
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".tif", delete=False).name
        req = urllib.request.Request(url, headers={"User-Agent": "archeve-datapack"})
        with urllib.request.urlopen(req, timeout=90) as r, open(tmp, "wb") as f:
            f.write(r.read())
        with rasterio.open(tmp) as ds:
            return ds.read(1).astype("float32"), ds.transform, ds.crs
    except Exception:
        return None


def slope_percent(geom):
    """Median TERRAIN slope (%) over the polygon, from the Copernicus DEM, coarsened to
    ~120 m so building/canopy facets of the GLO-30 surface model don't inflate it.
    Returns {"ok": True, "slope_pct": float, "n": int, "source": str} or {"ok": False, "error": str}.
    Lets the screening use real parcel slope instead of a national default in Tc/peak-flow."""
    try:
        g = shape(geom)
    except Exception as ex:
        return {"ok": False, "error": "bad geometry: %s" % ex}
    if g.is_empty:
        return {"ok": False, "error": "empty geometry"}
    w, s, e, n = g.bounds
    if (e - w) > MAX_DEG or (n - s) > MAX_DEG:
        return {"ok": False, "error": "bbox too large (> %.1f deg)" % MAX_DEG}
    pad = 0.006
    bounds = (w - pad, s - pad, e + pad, n + pad)
    dem, dem_tf = _mosaic(_cop_dem_urls(*bounds), bounds)
    if dem is None:
        return {"ok": False, "error": "DEM unavailable"}
    dem = np.where(dem.astype("float32") > -1000, dem.astype("float32"), np.nan)
    # cell size in metres from the geographic transform (dx scaled by latitude)
    lat0 = math.radians((s + n) / 2.0)
    dy = abs(dem_tf.e) * 110540.0
    dx = abs(dem_tf.a) * 111320.0 * math.cos(lat0)
    # GLO-30 is a SURFACE model (buildings/canopy). A raw 30 m gradient reads building
    # facets, not terrain — over dense cities that inflates slope 5-10x. Coarsen ~30 m ->
    # ~120 m (block mean) so buildings wash out and we recover the drainage/terrain slope.
    k = 4
    H, W = dem.shape
    Hc, Wc = H // k, W // k
    if Hc >= 2 and Wc >= 2:
        demc = np.nanmean(dem[:Hc * k, :Wc * k].reshape(Hc, k, Wc, k), axis=(1, 3))
        tf = rasterio.Affine(dem_tf.a * k, dem_tf.b, dem_tf.c, dem_tf.d, dem_tf.e * k, dem_tf.f)
        cy, cx = dy * k, dx * k
    else:
        demc, tf, cy, cx = dem, dem_tf, dy, dx
    gy, gx = np.gradient(demc, cy, cx)            # rows = latitude, cols = longitude
    slope = np.sqrt(gx * gx + gy * gy)            # m/m
    mask = geometry_mask([mapping(g)], out_shape=demc.shape, transform=tf, invert=True)
    vals = slope[mask & np.isfinite(slope)]
    if vals.size == 0:
        return {"ok": False, "error": "no DEM pixels under polygon"}
    slope_pct = min(60.0, float(np.median(vals) * 100.0))   # cap absurd cliff/artifact medians
    return {"ok": True, "slope_pct": round(slope_pct, 3), "n": int(vals.size),
            "source": "Copernicus GLO-30, terrain-coarsened ~120 m (EGM2008)"}


def _still_water_level(dem, depth, wet):
    """Estimate the still-water surface elevation (m, DEM datum) from a coarse depth grid.

    A still-water surface is flat, so in principle any wet cell gives the level as
    DEM + depth. In practice it does not: the Deltares depth is ~1 km and was solved on
    NASADEM, while the DEM here is 30 m Copernicus GLO-30 — different resolution, different
    geoid. Inside one 1 km depth cell the 30 m elevations vary by several metres, so
    DEM + depth inherits the terrain's variance rather than the water's. Measured on a
    Sundarbans extent: DEM + depth spanned -1.3 to 11.3 m (sd 2.2 m) against a DEM sd of
    2.15 m, and the median was NON-MONOTONIC in return period (rp10 above rp100) — i.e. the
    statistic was tracking terrain noise, not the flood.

    Restricting to the deepest decile fixes this: those cells are unambiguously inundated
    and lie on the low ground where the two terrain models agree most closely. On the same
    extent that estimator is monotonic in return period (3.998 / 4.218 / 4.465 / 4.612 m for
    rp 10 / 50 / 100 / 250).

    Returns (level_m, spread_m) or (None, None). `spread` is the interquartile range of
    DEM + depth over ALL wet cells — an honest indicator of how much the coarse-depth /
    fine-DEM mismatch is limiting the estimate, not a confidence interval on the flood.
    """
    if not wet.any():
        return None, None
    dwet = depth[wet]
    demwet = dem[wet]
    # Anchor on DEPTH, not on a transferred elevation. Deltares publishes depth relative to
    # its OWN ~1 km terrain; that depth is the meaningful product. Adding it to a different
    # 30 m DEM and treating the sum as an absolute elevation is ill-posed — it produced a
    # 9.1 m "water level" on ground the local DEM puts at 0.0 m, against a published max
    # depth of 4.5 m. Instead: take a robust depth for the wet area, and reference it to the
    # LOW ground of the same local DEM the mesh is drawn from, so the rendered depth matches
    # the published depth and the surface stays flat.
    depth_rep = float(np.median(dwet[dwet >= np.percentile(dwet, 90)]))
    ground_ref = float(np.percentile(demwet, 10))
    level = ground_ref + depth_rep
    # How much the local terrain varies under the flooded area — the honest indicator of how
    # coarse the ~1 km depth is relative to the 30 m mesh, not a confidence interval.
    spread = float(np.percentile(demwet, 75) - np.percentile(demwet, 25))
    return round(level, 3), round(spread, 3)


def terrain(geom, size=96, levels=None, pad_frac=0.6, source="cop30"):
    """Downsampled DEM grid + still-water surface levels, for the 3D view.

    Returns a compact JSON-able payload: a `size` x `size` elevation grid over the parcel
    (padded outward so the surrounding terrain is visible), plus, for each requested return
    period / scenario, the STILL-WATER LEVEL in the same vertical datum as the DEM.

    The level is the median of (DEM + coastal depth) over the wet cells — the same
    still-water plane the premium WSE layer uses — NOT `DEM + depth` per cell, which is not
    a water surface. `null` means no modelled coastal water reaches this extent at that
    return period; the viewer must show no water rather than a zero-depth sheet.

    This supports a rising-water-level visualisation only. There is no hydrodynamic
    solution behind it: no velocity, no wave, no routing, no timing.
    """
    try:
        g = shape(geom)
    except Exception as ex:
        return {"ok": False, "error": "bad geometry: %s" % ex}
    if g.is_empty:
        return {"ok": False, "error": "empty geometry"}
    size = max(32, min(160, int(size)))
    levels = levels or [(10, "today"), (50, "today"), (100, "today"),
                        (250, "today"), (100, "2050"), (250, "2050")]

    w, s, e, n = g.bounds
    # pad outward so the parcel sits in visible context, and keep the extent square in
    # ground distance so the rendered mesh is not stretched
    cx, cy = (w + e) / 2.0, (s + n) / 2.0
    half = max((e - w), (n - s) * 1.0) * (0.5 + pad_frac)
    half = max(half, 0.004)                       # ~450 m floor for very small parcels
    coslat = max(0.1, math.cos(math.radians(cy)))
    bounds = (cx - half, cy - half * coslat, cx + half, cy + half * coslat)
    if (bounds[2] - bounds[0]) > MAX_DEG or (bounds[3] - bounds[1]) > MAX_DEG:
        return {"ok": False, "error": "extent too large for the 3D view"}

    src = DEM_SOURCES.get(source) or DEM_SOURCES["cop30"]
    try:
        if src["id"] in ("cop30", "cop90"):
            res = 30 if src["id"] == "cop30" else 90
            dem, dem_tf = _mosaic(_cop_dem_urls(*bounds, res=res), bounds)
        else:
            mf = _opentopo_dataset(src["id"], *bounds)
            with mf.open() as ds:
                dem, dem_tf = ds.read(1), ds.transform
    except Exception as ex:
        # name the source that failed: "DEM unavailable" on a five-source picker tells
        # the user nothing about which one to try instead
        return {"ok": False, "error": "%s unavailable: %s" % (src["name"], ex)}
    if dem is None:
        return {"ok": False, "error": "%s has no coverage for this extent" % src["name"]}
    dem = np.where(dem.astype("float32") > -1000, dem.astype("float32"), np.nan)

    # block-mean down to the render grid (keeps it small on the wire and smooths the
    # building facets of the GLO-30 surface model, as the slope endpoint does)
    H, W = dem.shape
    ky, kx = max(1, H // size), max(1, W // size)
    Hc, Wc = H // ky, W // kx
    if Hc < 2 or Wc < 2:
        return {"ok": False, "error": "extent too small for a terrain mesh"}
    with np.errstate(invalid="ignore"):
        small = np.nanmean(dem[:Hc * ky, :Wc * kx].reshape(Hc, ky, Wc, kx), axis=(1, 3))
    grid_tf = rasterio.Affine(dem_tf.a * kx, dem_tf.b, dem_tf.c,
                              dem_tf.d, dem_tf.e * ky, dem_tf.f)

    # ── still-water level: a REGIONAL quantity, read from a deliberately wider window ──
    # The coastal depth grid is ~1 km. Over a parcel-sized extent it returns one or two
    # cells, which are frequently all nodata — so sampling it at the render extent reported
    # "dry" for sites that are demonstrably in the floodplain (measured: a 1.9 km window gave
    # a 1x2 all-nodata grid, while the same return periods over a ~9 km window gave max
    # depths of 4.46 m and 4.61 m). A still-water level cannot be resolved inside a single
    # cell of the grid that defines it. So: take the LEVEL from a window large enough to
    # contain real cells, then apply it to the local 30 m terrain — extent and depth stay
    # local, only the water-surface elevation is regional.
    fw = max(FLOOD_WINDOW_DEG, (bounds[2] - bounds[0]))
    fh = max(FLOOD_WINDOW_DEG * coslat, (bounds[3] - bounds[1]))
    fbounds = (cx - fw / 2, cy - fh / 2, cx + fw / 2, cy + fh / 2)
    fdem, fdem_tf = _mosaic(_cop_dem_urls(*fbounds), fbounds)
    if fdem is not None:
        fdem = np.where(fdem.astype("float32") > -1000, fdem.astype("float32"), np.nan)

    # elevations under the parcel itself, on the render grid — the anchor for water level
    try:
        pmask = geometry_mask([mapping(g)], out_shape=small.shape, transform=grid_tf, invert=True)
        parcel_elev = small[pmask & np.isfinite(small)]
    except Exception:
        parcel_elev = np.array([], dtype="float32")

    water = []
    for rp, scen in levels:
        entry = {"rp": rp, "scenario": scen, "level_m": None,
                 "level_spread_m": None, "max_depth_m": None}
        res = _fetch_flood_depth(fbounds[0], fbounds[1], fbounds[2], fbounds[3],
                                 rp=rp, scenario=scen)
        if res is not None and fdem is not None:
            d_arr, d_tf, d_crs = res
            depth = _reproject_to(d_arr, d_tf, d_crs, fdem_tf, fdem.shape, "EPSG:4326",
                                  Resampling.bilinear, src_nodata=-9999.0)
            wet = np.isfinite(depth) & (depth > 0) & np.isfinite(fdem)
            if wet.any():
                # DEPTH comes from the wide window (the only scale at which the ~1 km grid
                # resolves); the GROUND REFERENCE comes from the local mesh being drawn, so
                # the rendered water depth equals the published depth on this terrain.
                dwet = depth[wet]
                depth_rep = float(np.median(dwet[dwet >= np.percentile(dwet, 90)]))
                # Reference the depth to the low ground OF THE PARCEL — the site actually
                # being screened — so the parcel floods to the published depth. Anchoring on
                # the whole padded extent instead put the surface 4.6 m over the lowest
                # ground where Deltares publishes 1.4 m, because the extent contained 16 m of
                # relief that the ~1 km cell averages away.
                ground_ref = (float(np.percentile(parcel_elev, 10)) if parcel_elev.size
                              else (float(np.percentile(small[np.isfinite(small)], 10))
                                    if np.isfinite(small).any() else 0.0))
                entry["level_m"] = round(ground_ref + depth_rep, 3)
                entry["depth_m"] = round(depth_rep, 2)
                entry["ground_ref_m"] = round(ground_ref, 2)
                entry["level_spread_m"] = round(float(np.percentile(parcel_elev, 75) -
                                                      np.percentile(parcel_elev, 25)), 3) if parcel_elev.size else None
                entry["max_depth_m"] = round(float(np.nanmax(dwet)), 2)
        water.append(entry)

    valid = small[np.isfinite(small)]
    if valid.size == 0:
        return {"ok": False, "error": "no valid DEM pixels in this extent"}
    # NaN is not valid JSON; send the nodata sentinel and let the client mask it
    out = np.where(np.isfinite(small), np.round(small, 2), NODATA_F)
    ring = list(g.exterior.coords) if g.geom_type == "Polygon" else []
    return {
        "ok": True,
        # which source actually produced this grid — the client must never have to assume
        "dem_source": {"id": src["id"], "name": src["name"], "res_m": src["res_m"],
                       "kind": src["kind"], "accuracy_m": src["accuracy_m"],
                       "licence": src["licence"]},
        "width": int(Wc), "height": int(Hc),
        "bounds": [round(b, 6) for b in bounds],
        "cell_deg": [abs(grid_tf.a), abs(grid_tf.e)],
        "dem": [float(v) for v in out.ravel()],
        "nodata": NODATA_F,
        "dem_min": round(float(valid.min()), 2), "dem_max": round(float(valid.max()), 2),
        "flat": bool(float(valid.max()) - float(valid.min()) < 0.5),
        "flood_window_deg": round(fw, 4),
        "parcel": [[round(c[0], 6), round(c[1], 6)] for c in ring],
        "water": water,
        "datum": "EGM2008 orthometric (Copernicus GLO-30)",
        "source": "Copernicus GLO-30 DEM; still-water levels from Deltares Global Flood Maps (~1 km)",
        "note": ("Still-water levels only. No hydrodynamic model: no velocity, wave, routing "
                 "or timing is represented."),
    }


def build(geom, path=None, premium=False):
    """geom: GeoJSON geometry (WGS84). Returns (zip_path, meta) or (None, error)."""
    path = path or gz.GCN250_PATH
    if not os.path.exists(path):
        try: gz.ensure_raster()
        except Exception as ex: return None, {"ok": False, "error": "GCN250 fetch failed: %s" % ex}
    if not os.path.exists(path):
        return None, {"ok": False, "error": "GCN250 raster not available."}
    try:
        g = shape(geom)
    except Exception as ex:
        return None, {"ok": False, "error": "bad geometry: %s" % ex}
    if g.is_empty:
        return None, {"ok": False, "error": "empty geometry"}
    w, s, e, n = g.bounds
    if (e - w) > MAX_DEG or (n - s) > MAX_DEG:
        return None, {"ok": False, "error": "bbox too large for screening (> %.1f deg)" % MAX_DEG}
    # small pad so edge pixels are captured
    pad = 0.003
    bounds = (w - pad, s - pad, e + pad, n + pad)
    crs = "EPSG:4326"

    # ── target grid: Copernicus DEM 30 m over the bbox (defines the stack) ──
    dem, dem_tf = _mosaic(_cop_dem_urls(*bounds), bounds)
    if dem is not None:
        dst_tf, dst_shape = dem_tf, dem.shape
        dem_grid = dem.astype("float32")
    else:
        # fallback: GCN250 grid (250 m) if DEM unavailable
        with rasterio.open(path) as ds:
            win = from_bounds(*bounds, ds.transform)
            gcn_full = ds.read(1, window=win)
            dst_tf = ds.window_transform(win)
        dst_shape = gcn_full.shape
        dem_grid = None

    # polygon mask on the target grid
    inside = geometry_mask([mapping(g)], out_shape=dst_shape, transform=dst_tf, invert=True)

    layers = {}
    produced = []
    skipped = []      # layers deliberately omitted, reported to the caller and in the README

    def add(name, arr):
        arr = np.where(inside & np.isfinite(arr), arr, NODATA_F).astype("float32")
        layers[name] = arr
        produced.append(name)

    # ── CN / S / Ia (GCN250 -> target grid) ──
    with rasterio.open(path) as ds:
        win = from_bounds(*bounds, ds.transform)
        cn_src = ds.read(1, window=win).astype("float32")
        cn_tf = ds.window_transform(win)
    cn = _reproject_to(cn_src, cn_tf, crs, dst_tf, dst_shape, crs, Resampling.nearest, src_nodata=255)
    cn_valid = (cn > 0) & (cn <= 100)
    if not cn_valid.any():
        return None, {"ok": False, "error": "no valid CN pixels under the polygon"}
    cn = np.where(cn_valid, cn, NODATA_F)
    S = np.where(cn_valid, 25400.0 / np.where(cn_valid, cn, 1.0) - 254.0, NODATA_F)
    Ia = np.where(cn_valid, 0.2 * S, NODATA_F)
    add("cn_arcii.tif", cn); add("retention_S.tif", S); add("ia_initial_abstraction.tif", Ia)

    # ── DEM ──
    if dem_grid is not None:
        add("dem_30m.tif", np.where(dem_grid > -1000, dem_grid, NODATA_F))

    # ── Manning's n (WorldCover -> n -> target grid) ──
    # WorldCover is 10 m, so its mosaic is ~9x the cell count of the 30 m target grid and is
    # by far the largest allocation in the pack. Beyond the budget below the read alone
    # exhausted the 512 MB instance and the worker was OOM-killed mid-request, which the
    # caller saw as an empty 502 with no explanation. Skip the layer instead: the pack is
    # explicitly best-effort, so degrading is correct where crashing is not.
    wc_cells = ((bounds[2] - bounds[0]) * 12000.0) * ((bounds[3] - bounds[1]) * 12000.0)
    if wc_cells > MAX_WORLDCOVER_CELLS:
        wc, wc_tf = None, None
        skipped.append("mannings_n.tif (area too large for the 10 m land-cover read: "
                       "%.1f M cells > %.1f M budget)" % (wc_cells / 1e6, MAX_WORLDCOVER_CELLS / 1e6))
    else:
        wc, wc_tf = _mosaic(_worldcover_urls(*bounds), bounds)
    if wc is not None:
        n_src = np.full(wc.shape, NODATA_F, dtype="float32")
        for cls, mn in WC_MANNING.items():
            n_src[wc == cls] = mn
        mann = _reproject_to(n_src, wc_tf, crs, dst_tf, dst_shape, crs, Resampling.nearest, src_nodata=NODATA_F)
        add("mannings_n.tif", mann)

    # ── flood depth (fetch) -> hazard + water level ──
    # Only ship the coastal-flood layers where water actually reaches the site.
    # An inland site has no coastal flood, so an empty hazard / water-level raster
    # would mislead — and the premium water-level layer must never be sold empty.
    premium_delivered = False
    wse_meta = None
    depth_res = _fetch_flood_depth(*bounds)
    if depth_res is not None:
        d_arr, d_tf, d_crs = depth_res
        depth = _reproject_to(d_arr, d_tf, d_crs, dst_tf, dst_shape, crs, Resampling.bilinear, src_nodata=-9999.0)
        wet = np.isfinite(depth) & (depth > 0) & inside
        if wet.any():
            haz = np.full(dst_shape, NODATA_F, dtype="float32")
            haz[wet & (depth <= 0.3)] = 1
            haz[wet & (depth > 0.3) & (depth <= 0.6)] = 2
            haz[wet & (depth > 0.6) & (depth <= 1.2)] = 3
            haz[wet & (depth > 1.2)] = 4
            add("flood_hazard.tif", haz)
            if premium and dem_grid is not None:
                # Downscale to a PHYSICAL still-water surface instead of DEM+depth.
                # The coarse (~1 km) coastal depth implies a near-flat water level; estimate
                # that level as the median of (DEM30 + depth) over the wet cells, then re-derive
                # inundation against the 30 m terrain. This removes the non-physical "bumpy"
                # surface a raw add produces and yields a 30 m-consistent extent + depth.
                # Level from the deepest decile, not the median over all wet cells: the
                # coarse (~1 km, NASADEM-based) depth added to a 30 m DEM inherits the
                # terrain's variance, which made the median track topography instead of the
                # flood and non-monotonic in return period. See _still_water_level().
                level, level_spread = _still_water_level(dem_grid, depth, wet)
                if level is not None:
                    ref = inside & (dem_grid > -1000) & np.isfinite(dem_grid) & (dem_grid < level)
                    wse = np.where(ref, level, NODATA_F).astype("float32")
                    depth_ds = np.where(ref, level - dem_grid, NODATA_F).astype("float32")
                    add("water_level_wse.tif", wse)
                    add("depth_downscaled.tif", depth_ds)
                    premium_delivered = True
                    wse_meta = {"level_m": level, "level_spread_m": level_spread}

    # ── write + zip ──
    tmpdir = tempfile.mkdtemp(prefix="datapack_")
    for name, arr in layers.items():
        with rasterio.open(os.path.join(tmpdir, name), "w", driver="GTiff",
                           height=arr.shape[0], width=arr.shape[1], count=1, dtype="float32",
                           crs=crs, transform=dst_tf, nodata=NODATA_F, compress="deflate") as dst:
            dst.write(arr, 1)

    readme = (
        "Archeve - Site Data Pack\n========================\n\n"
        "Co-registered screening grids (EPSG:4326, ~30 m; GCN250 upsampled). NoData = -9999.\n"
        "Vertical datum: Copernicus GLO-30 is EGM2008 orthometric. Deltares coastal depth is\n"
        "derived on ~1 km NASADEM (EGM96); the geoids differ by up to ~0.5 m in this region, so\n"
        "treat absolute water-surface elevations as screening-grade (+/- ~0.5 m).\n\n"
        "cn_arcii.tif                Curve Number, ARC II (GCN250, CC BY 4.0)\n"
        "retention_S.tif             S = 25400/CN - 254 (mm)\n"
        "ia_initial_abstraction.tif  Ia = 0.2 S (mm)\n"
        "dem_30m.tif                 Elevation (Copernicus GLO-30, m, EGM2008)\n"
        "mannings_n.tif              Overland Manning's n (ESA WorldCover 2021)\n"
        "flood_hazard.tif            Banded DEPTH (still water): 1 <=0.3m 2 <=0.6m 3 <=1.2m 4 >1.2m\n"
        "water_level_wse.tif         [premium] Still-water surface elevation (m). Coarse coastal\n"
        "                            depth implies a near-flat level; taken as the median of\n"
        "                            (DEM + depth) over wet cells = a single still-water plane.\n"
        "depth_downscaled.tif        [premium] Depth at 30 m = WSE - DEM, re-derived against the\n"
        "                            fine terrain (extent/depth refined from the ~1 km input).\n\n"
        "Notes / limits:\n"
        " - Coastal (surge + SLR) only. Riverine and pluvial / wadi flooding are NOT included.\n"
        " -2050 scenario is sea-level rise only; land subsidence (large in deltas) is excluded.\n"
        " - Still-water plane assumes one connected pool in the parcel; may over-fill disconnected lows.\n"
        " - Hazard band is depth-only; people/vehicle hazard needs depth x velocity (not provided).\n"
        " - Deltares coastal depth is ~1 km native. Screening-grade - verify before design.\n"
        "\nHOW TO OPEN THESE FILES:\n"
        " - QGIS or ArcGIS (desktop).\n"
        " - No GIS installed? GeoLibre runs in the browser with nothing to install:\n"
        "   https://web.geolibre.app/  - drag the .tif files onto the map, then use\n"
        "   single-band pseudocolor to style depth/hazard and Identify to read pixel values.\n"
        " - All layers are EPSG:4326 and co-registered, so they overlay directly.\n"
        + ("\nLAYERS OMITTED FOR THIS REQUEST:\n" + "".join(" - %s\n" % x for x in skipped) if skipped else "")
        + "\nLayers actually included: " + ", ".join(produced) + "\n"
        "Generated by Archeve AIP - aip.archeve.in\n"
    )
    open(os.path.join(tmpdir, "README.txt"), "w").write(readme)

    zip_path = os.path.join(tmpdir, "archeve_site_datapack.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in list(layers) + ["README.txt"]:
            z.write(os.path.join(tmpdir, name), name)

    return zip_path, {"ok": True, "layers": produced, "skipped": skipped, "wse": wse_meta,
                      "premium": premium_delivered,                 # water-level actually delivered
                      "coastal": "flood_hazard.tif" in layers,      # site reached by coastal flood -> premium applies
                      "grid": "EPSG:4326 ~30 m" if dem_grid is not None else "EPSG:4326 ~250 m (DEM unavailable)"}
