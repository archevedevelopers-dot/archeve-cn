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
MAX_DEG = 0.5  # ~55 km — screening + memory guard for the 30 m stack
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
    reproject(source=src_arr.astype("float32"), destination=dst,
              src_transform=src_transform, src_crs=src_crs,
              dst_transform=dst_transform, dst_crs=dst_crs,
              src_nodata=src_nodata, dst_nodata=NODATA_F, resampling=resampling)
    return dst


def _fetch_flood_depth(w, s, e, n):
    """GeoTIFF depth clip from the flood service; return (arr, transform, crs) or None."""
    url = "%s/download?bbox=%.4f,%.4f,%.4f,%.4f&rp=100&scenario=today&name=site" % (FLOOD_API, w, s, e, n)
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".tif", delete=False).name
        req = urllib.request.Request(url, headers={"User-Agent": "archeve-datapack"})
        with urllib.request.urlopen(req, timeout=90) as r, open(tmp, "wb") as f:
            f.write(r.read())
        with rasterio.open(tmp) as ds:
            return ds.read(1).astype("float32"), ds.transform, ds.crs
    except Exception:
        return None


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
    wc, wc_tf = _mosaic(_worldcover_urls(*bounds), bounds)
    if wc is not None:
        n_src = np.full(wc.shape, NODATA_F, dtype="float32")
        for cls, mn in WC_MANNING.items():
            n_src[wc == cls] = mn
        mann = _reproject_to(n_src, wc_tf, crs, dst_tf, dst_shape, crs, Resampling.nearest, src_nodata=NODATA_F)
        add("mannings_n.tif", mann)

    # ── flood depth (fetch) -> hazard + water level ──
    depth_res = _fetch_flood_depth(*bounds)
    if depth_res is not None:
        d_arr, d_tf, d_crs = depth_res
        depth = _reproject_to(d_arr, d_tf, d_crs, dst_tf, dst_shape, crs, Resampling.bilinear, src_nodata=-9999.0)
        wet = np.isfinite(depth) & (depth > 0)
        haz = np.full(dst_shape, NODATA_F, dtype="float32")
        haz[wet & (depth <= 0.3)] = 1
        haz[wet & (depth > 0.3) & (depth <= 0.6)] = 2
        haz[wet & (depth > 0.6) & (depth <= 1.2)] = 3
        haz[wet & (depth > 1.2)] = 4
        add("flood_hazard.tif", haz)
        if premium and dem_grid is not None:
            wse = np.where(wet, dem_grid + depth, NODATA_F)
            add("water_level_wse.tif", wse)

    # ── write + zip ──
    tmpdir = tempfile.mkdtemp(prefix="datapack_")
    for name, arr in layers.items():
        with rasterio.open(os.path.join(tmpdir, name), "w", driver="GTiff",
                           height=arr.shape[0], width=arr.shape[1], count=1, dtype="float32",
                           crs=crs, transform=dst_tf, nodata=NODATA_F, compress="deflate") as dst:
            dst.write(arr, 1)

    readme = (
        "Archeve — Site Data Pack\n========================\n\n"
        "Co-registered screening grids (EPSG:4326, ~30 m; GCN250 upsampled). NoData = -9999.\n\n"
        "cn_arcii.tif                Curve Number, ARC II (GCN250, CC BY 4.0)\n"
        "retention_S.tif             S = 25400/CN - 254 (mm)\n"
        "ia_initial_abstraction.tif  Ia = 0.2 S (mm)\n"
        "dem_30m.tif                 Elevation (Copernicus GLO-30, m)\n"
        "mannings_n.tif              Overland Manning's n (ESA WorldCover 2021)\n"
        "flood_hazard.tif            Banded hazard: 1 low(<=0.3m) 2 mod 3 high 4 severe(>1.2m)\n"
        "water_level_wse.tif         Approx water surface = DEM + coastal depth (m) [premium]\n\n"
        "Flood layers use Deltares coastal depth (~1 km). Screening-grade — verify before design.\n"
        "Generated by Archeve AIP — aip.archeve.in\n"
    )
    open(os.path.join(tmpdir, "README.txt"), "w").write(readme)

    zip_path = os.path.join(tmpdir, "archeve_site_datapack.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in list(layers) + ["README.txt"]:
            z.write(os.path.join(tmpdir, name), name)

    return zip_path, {"ok": True, "layers": produced, "premium": bool(premium and "water_level_wse.tif" in layers),
                      "grid": "EPSG:4326 ~30 m" if dem_grid is not None else "EPSG:4326 ~250 m (DEM unavailable)"}
