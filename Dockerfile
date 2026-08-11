FROM python:3.12-slim

# ffmpeg does all the mapping work; nothing else needs compiling
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY artifacts/volumesplit/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY artifacts/volumesplit/app ./app

ENV VS_DATA=/data \
    VS_WORKERS=1 \
    VS_ENCODE_THREADS=4 \
    VS_FILTER_THREADS=8 \
    PYTHONUNBUFFERED=1 \
    # large uploads spool to a temp file before landing in /data/uploads;
    # the container's /tmp is a small RAM tmpfs, so spool to the data disk
    TMPDIR=/data/tmp

RUN mkdir -p /data/uploads /data/jobs /data/tmp

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--timeout-keep-alive", "300"]
