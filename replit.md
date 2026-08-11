# VolumeSplit

Splits image and video plates across the OBTV LED volume's three walls (left 2560×2048 · center 6144×2560 · right 2560×2048) using ffmpeg. Drop a plate, frame it in the browser, and the server renders the wall feeds as Hap Q, ProRes, DNxHR, or H.264.

## Run & Operate

- `cd /home/runner/workspace/artifacts/volumesplit && VS_DATA=./data uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload` — run the dev server (managed via the "artifacts/api-server: API Server" workflow)
- `docker compose up -d --build` — run via Docker (production path, port 8080)

## Stack

- Python 3.12 + FastAPI + uvicorn
- ffmpeg for all rendering (preview frames, Hap Q / ProRes / DNxHR / H.264 encode)
- Vanilla JS single-page frontend served as static files by FastAPI

## Where things live

- `artifacts/volumesplit/app/` — Python application
  - `main.py` — FastAPI routes and app startup
  - `jobs.py` — media probing, ffmpeg job queue, worker threads
  - `mapping.py` — wall geometry, filtergraph construction, codec definitions
  - `app/static/index.html` — complete single-file frontend
- `artifacts/volumesplit/data/` — uploads, jobs, presets (dev; not committed)
- `Dockerfile` — production image (python:3.12-slim + ffmpeg, port 8080)
- `docker-compose.yml` — production stack with named volume for `/data`

## Wall geometry

```
LEFT              CENTER                     RIGHT
2560 x 2048       6144 x 2560                2560 x 2048
\_________________ RTX PRO 4000 _______________/   sides.mov  5120 x 2048
                  RTX PRO 4500                     center.mov 6144 x 2560

unwrap 11264 x 2560   ·   400 cabinets @ 256 px
```

## API

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/sources` | upload a plate (multipart) |
| `GET`  | `/api/sources` | list plates |
| `POST` | `/api/preview` | one framing preview frame (JPEG) |
| `POST` | `/api/jobs` | queue a split |
| `GET`  | `/api/jobs/{id}` | status and progress |
| `GET`  | `/api/jobs/{id}/zip` | all outputs |
| `GET`  | `/healthz` | liveness |

## Docker deployment

```bash
docker compose up -d --build
# open http://<server>:8080
```

Mount your plate storage over `/data/uploads` in `docker-compose.yml` to pull sources off a share without uploading through the browser.

## Environment variables

| Variable     | Default | Notes |
|--------------|---------|-------|
| `VS_DATA`    | `/data` | uploads, jobs, presets |
| `VS_WORKERS` | `1`     | concurrent ffmpeg jobs — raise only with core headroom |

## Architecture decisions

- All rendering (preview and full encode) goes through ffmpeg filtergraphs defined in `mapping.py` — one source of truth for both still and video paths.
- Preview frames render straight to stdout (`pipe:1`) so nothing touches disk; full encodes write to `VS_DATA/jobs/<id>/`.
- Worker threads pull from a queue; `VS_WORKERS` controls parallelism. ffmpeg saturates cores quickly so the default of 1 is safe.
- The Replit dev server runs out of `artifacts/volumesplit/` with `VS_DATA=./data` (local to that directory). The Docker image uses `/data` mapped to a named volume.

## User preferences

- NEVER propose or suggest project tasks. Fix issues directly instead of creating tasks for them.

## Gotchas

- Always `cd` into `artifacts/volumesplit/` before running uvicorn directly — the `app` package is relative to that directory.
- Hap Q at 6144×2560 is ~2.1 MB/frame (129 MB/s at 60p). Mount `/data` on NVMe and prune finished jobs regularly.
- No auth built in — put it behind a reverse proxy or Authentik for production.
