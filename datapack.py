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
import traceback
import urllib.request

import numpy as np
import rasterio
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
FLOOD_API = os.environ.get("FLOOD_API", "https://archeve-flood.onrender.com")

# WorldCover class -> overland-flow Manning's n (sheet flow, screening)
WC_MANNING = {10: 0.40, 20: 0.40, 30: 0.35, 40: 0.35, 50: 0.02,
              60: 0.05, 70: 0.01, 80: 0.03, 90: 0.10, 95: 0.14, 100: 0.10}


def _cop_dem_urls(w, s, e, n):
    urls = []
    for lat in range(int(math.floor(s)), int(math.floor(n)) + 1):
        for lon in range(int(math.floor(w)), int(math.floor(e)) + 1):
            ns, ew = ("N" if lat >= 0 else "S"), ("E" if lon >= 0 else "W")
            tile = "Copernicus_DSM_COG_10_%s%02d_00_%s%03d_00_DEM" % (ns, abs(lat), ew, abs(lon))
            urls.append("/vsicurl/https://copernicus-dem-30m.s3.amazonaws.com/%s/%s.tif" % (tile, tile))
    return urls


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
    dsum = (dem + depth)[wet]
    dwet = depth[wet]
    thr = np.percentile(dwet, 90)
    deep = dsum[dwet >= thr]
    if deep.size == 0:
        return None, None
    level = float(np.median(deep))
    spread = float(np.percentile(dsum, 75) - np.percentile(dsum, 25))
    return round(level, 3), round(spread, 3)


def terrain(geom, size=96, levels=None, pad_frac=0.6):
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

    dem, dem_tf = _mosaic(_cop_dem_urls(*bounds), bounds)
    if dem is None:
        return {"ok": False, "error": "DEM unavailable for this extent"}
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

    # still-water level per requested return period, on the FULL-resolution DEM
    water = []
    for rp, scen in levels:
        lvl = None
        res = _fetch_flood_depth(bounds[0], bounds[1], bounds[2], bounds[3], rp=rp, scenario=scen)
        if res is not None:
            d_arr, d_tf, d_crs = res
            depth = _reproject_to(d_arr, d_tf, d_crs, dem_tf, dem.shape, "EPSG:4326",
                                  Resampling.bilinear, src_nodata=-9999.0)
            wet = np.isfinite(depth) & (depth > 0) & np.isfinite(dem)
            lvl, spread = _still_water_level(dem, depth, wet)
            water.append({"rp": rp, "scenario": scen, "level_m": lvl,
                          "level_spread_m": spread,
                          "max_depth_m": round(float(np.nanmax(depth[wet])), 2) if wet.any() else None})
            continue
        water.append({"rp": rp, "scenario": scen, "level_m": None,
                      "level_spread_m": None, "max_depth_m": None})

    valid = small[np.isfinite(small)]
    if valid.size == 0:
        return {"ok": False, "error": "no valid DEM pixels in this extent"}
    # NaN is not valid JSON; send the nodata sentinel and let the client mask it
    out = np.where(np.isfinite(small), np.round(small, 2), NODATA_F)
    ring = list(g.exterior.coords) if g.geom_type == "Polygon" else []
    return {
        "ok": True,
        "width": int(Wc), "height": int(Hc),
        "bounds": [round(b, 6) for b in bounds],
        "cell_deg": [abs(grid_tf.a), abs(grid_tf.e)],
        "dem": [float(v) for v in out.ravel()],
        "nodata": NODATA_F,
        "dem_min": round(float(valid.min()), 2), "dem_max": round(float(valid.max()), 2),
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
