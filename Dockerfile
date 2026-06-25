# Archeve GCN250 Curve Number API — container image.
# Build context = this cn/ directory.
#
# The 640 MB GCN250 raster is fetched by the app at first boot from the public
# figshare URL (GCN250_URL) — no manual upload needed. Mount a small volume at
# /data and set GCN250_PATH=/data/GCN250_ARCII.tif to cache it across restarts;
# otherwise it downloads to /tmp on each cold start.
FROM python:3.11-slim

WORKDIR /app

# rasterio's bundled GDAL still needs libexpat at runtime (not in python:3.11-slim)
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

# rasterio ships manylinux wheels with GDAL bundled — no system GDAL/compiler needed
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt fastapi "uvicorn[standard]"

COPY gcn_zonal.py server.py ./

ENV PORT=8810
ENV GCN250_PATH=/tmp/GCN250_ARCII.tif
# GCN250_URL defaults to the public figshare ARC II GeoTIFF (see gcn_zonal.py)
EXPOSE 8810

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8810}"]
