"""VolumeSplit — LED wall plate splitter service."""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import jobs as J
from . import mapping as M

log = logging.getLogger("uvicorn.error")

app = FastAPI(title="VolumeSplit", version="1.0")
STATIC = Path(__file__).parent / "static"


@app.on_event("startup")
def _startup():
    workers = int(os.environ.get("VS_WORKERS", "1"))
    log.info(
        "VolumeSplit starting — data=%s  uploads=%s  jobs=%s  workers=%d",
        J.DATA, J.UPLOADS, J.JOBS, workers,
    )
    J.start_workers(workers)


# ------------------------------------------------------------------ models ---

class ParamsIn(BaseModel):
    mode: str = "center"
    fit: str = "cover"
    zoom: float = 1.0
    pan_x: float = 0.0
    pan_y: float = 0.0
    valign: str = "bottom"
    extend: str = "mirror"
    extend_width: int = 1024
    outputs: str = "machine"
    grid: bool = False

    def to_params(self) -> M.Params:
        return M.Params(**self.model_dump()).validate()


class PreviewIn(BaseModel):
    source_id: str
    params: ParamsIn
    timecode: float = 0.0
    width: int = 1408


class JobIn(BaseModel):
    source_id: str
    params: ParamsIn
    codec: str = "hapq"
    image_format: str = "png"
    fps: float | None = None
    trim_start: str | None = None
    trim_duration: str | None = None


class PresetIn(BaseModel):
    name: str
    params: ParamsIn


# ------------------------------------------------------------------- config --

@app.get("/api/config")
def config():
    return {
        "canvas": {"w": M.CANVAS_W, "h": M.CANVAS_H, "cab": M.CAB},
        "walls": M.WALLS,
        "order": M.ORDER,
        "nodes": M.NODE,
        "codecs": {k: {"label": v["label"], "note": v["note"], "ext": v["ext"]}
                   for k, v in M.CODECS.items()},
        "image_formats": {k: v["label"] for k, v in M.IMAGE_FORMATS.items()},
    }


# ------------------------------------------------------------------ sources --

@app.post("/api/sources")
async def upload(file: UploadFile = File(...)):
    sid = uuid.uuid4().hex[:12]
    ext = Path(file.filename or "plate").suffix.lower() or ".bin"
    dest = J.UPLOADS / f"{sid}{ext}"

    with dest.open("wb") as f:
        while chunk := await file.read(1 << 22):     # 4 MB chunks
            f.write(chunk)

    try:
        meta = J.probe(dest)
    except Exception as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, f"Could not read that file. {e}")

    meta.update({"id": sid, "name": file.filename or dest.name, "ext": ext})
    (J.UPLOADS / f"{sid}.json").write_text(__import__("json").dumps(meta))
    return meta


@app.get("/api/sources")
def sources():
    return J.list_sources()


@app.delete("/api/sources/{sid}")
def rm_source(sid: str):
    J.delete_source(sid)
    return {"ok": True}


# ------------------------------------------------------------------ preview --

@app.post("/api/preview")
def preview(body: PreviewIn):
    try:
        data = J.render_preview(body.source_id, body.params.to_params(),
                                body.timecode, max(320, min(body.width, 2400)))
    except FileNotFoundError:
        raise HTTPException(404, "That plate is no longer on the server.")
    except Exception as e:
        raise HTTPException(400, str(e))
    return Response(content=data, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------- jobs --

@app.post("/api/jobs")
def create_job(body: JobIn):
    try:
        J.source_path(body.source_id)
    except FileNotFoundError:
        raise HTTPException(404, "That plate is no longer on the server.")
    if body.codec not in M.CODECS:
        raise HTTPException(400, "Unknown codec.")
    if body.image_format not in M.IMAGE_FORMATS:
        raise HTTPException(400, "Unknown image format.")

    enc = {"codec": body.codec, "image_format": body.image_format,
           "fps": body.fps, "trim_start": body.trim_start,
           "trim_duration": body.trim_duration}
    job = J.submit(body.source_id, body.params.to_params(), enc)
    return job.to_dict()


@app.get("/api/jobs")
def all_jobs():
    return J.list_jobs()


@app.get("/api/jobs/{jid}")
def one_job(jid: str):
    j = J.get_job(jid)
    if not j:
        raise HTTPException(404, "No job with that id.")
    return j.to_dict()


@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: str):
    if not J.cancel(jid):
        raise HTTPException(404, "No job with that id.")
    return {"ok": True}


@app.get("/api/jobs/{jid}/files/{name}")
def download(jid: str, name: str):
    j = J.get_job(jid)
    if not j:
        raise HTTPException(404, "No job with that id.")
    safe = Path(name).name
    f = j.dir() / safe
    if not f.exists():
        raise HTTPException(404, "That file isn't in this job.")
    return FileResponse(f, filename=f"{j.id}_{safe}",
                        media_type="application/octet-stream")


@app.get("/api/jobs/{jid}/zip")
def download_zip(jid: str):
    j = J.get_job(jid)
    if not j or not (j.dir() / "outputs.zip").exists():
        raise HTTPException(404, "Nothing to download yet.")
    return FileResponse(j.dir() / "outputs.zip",
                        filename=f"volume_walls_{j.id}.zip",
                        media_type="application/zip")


@app.delete("/api/jobs/{jid}")
def rm_job(jid: str):
    j = J.get_job(jid)
    if j:
        shutil.rmtree(j.dir(), ignore_errors=True)
        J._jobs.pop(jid, None)
    return {"ok": True}


# ----------------------------------------------------------------- presets ---

@app.get("/api/presets")
def presets():
    return J.load_presets()


@app.post("/api/presets")
def add_preset(body: PresetIn):
    return J.save_preset(body.name.strip()[:60], body.params.model_dump())


@app.delete("/api/presets/{name}")
def rm_preset(name: str):
    return J.delete_preset(name)


# -------------------------------------------------------------------- misc ---

@app.get("/healthz")
def healthz():
    return {"ok": True, "ffmpeg": bool(shutil.which("ffmpeg"))}


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
