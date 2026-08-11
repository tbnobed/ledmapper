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

from . import genai as G
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


class GenIn(BaseModel):
    prompt: str
    target: str = "center"      # "center" (2.4:1) or "canvas" (4:1)


@app.post("/api/generate")
def generate(body: GenIn):
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(400, "Prompt is empty.")
    if body.target not in G.SIZES:
        raise HTTPException(400, "Unknown target.")
    try:
        data = G.generate_plate(prompt, body.target)
    except RuntimeError as e:
        raise HTTPException(502, str(e))

    sid = uuid.uuid4().hex[:12]
    ext = ".png" if data[:4] == b"\x89PNG" else ".jpg"
    dest = J.UPLOADS / f"{sid}{ext}"
    dest.write_bytes(data)
    try:
        meta = J.probe(dest)
    except Exception as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(502, f"Generated file was not readable. {e}")

    name = "AI - " + (prompt[:48] + ("…" if len(prompt) > 48 else ""))
    meta.update({"id": sid, "name": name, "ext": ext, "generated": True,
                 "prompt": prompt})
    (J.UPLOADS / f"{sid}.json").write_text(__import__("json").dumps(meta))
    return meta


class AnimateIn(BaseModel):
    source_id: str
    prompt: str
    duration: int = 5


_animations: dict = {}          # fal request id -> {"prompt": ..., "source_name": ...}


@app.post("/api/animate")
def animate(body: AnimateIn):
    """Generate an ambient overlay layer (dust/flares on black) for a still.

    The plate itself never goes through the video model — it stays at full
    native resolution. The layer is screen-blended over the unwrap in ffmpeg.
    """
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(400, "Prompt is empty.")
    if body.duration not in (5, 10):
        raise HTTPException(400, "Duration must be 5 or 10 seconds.")
    try:
        J.source_path(body.source_id)
    except FileNotFoundError:
        raise HTTPException(404, "That plate is no longer on the server.")
    meta = J.source_meta(body.source_id)
    if meta.get("kind") == "video":
        raise HTTPException(400, "Pick a still plate — layers go on stills.")

    try:
        rid = G.submit_overlay(prompt, body.duration)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    _animations[rid] = {"prompt": prompt, "source_id": body.source_id}
    return {"rid": rid}


@app.get("/api/animate/{rid}")
def animate_status(rid: str):
    if rid not in _animations:
        raise HTTPException(404, "Unknown animation request.")
    try:
        res = G.poll_animation(rid)
    except RuntimeError as e:
        _animations.pop(rid, None)
        raise HTTPException(502, str(e))
    if res["status"] != "done":
        return {"status": res["status"]}

    info = _animations.pop(rid)
    sid = info["source_id"]
    tmp = J.OVERLAYS / f".{sid}.{uuid.uuid4().hex[:8]}.mp4"
    tmp.write_bytes(res["data"])
    try:
        J.make_loopable(tmp)            # crossfade tail into head — seamless loop
        ometa = J.probe(tmp)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise HTTPException(502, f"Generated layer was not readable. {e}")

    # publish under the overlay lock: if the source was deleted while the
    # layer was generating, discard the layer instead of resurrecting the
    # source's metadata sidecar
    with J._overlay_lock:
        meta = J.source_meta(sid)
        try:
            J.source_path(sid)
        except FileNotFoundError:
            meta = None
        if not meta:
            tmp.unlink(missing_ok=True)
            raise HTTPException(404, "That plate was removed while the layer "
                                     "was generating.")
        os.replace(tmp, J.overlay_path(sid))
        meta["overlay"] = {"prompt": info["prompt"],
                           "duration": ometa["duration"], "fps": ometa["fps"],
                           "width": ometa["width"], "height": ometa["height"]}
        (J.UPLOADS / f"{sid}.json").write_text(__import__("json").dumps(meta))
    return {"status": "done", "source": meta}


@app.post("/api/sources/{sid}/overlay")
async def upload_overlay(sid: str, file: UploadFile = File(...)):
    """Attach an uploaded video (e.g. stock dust/flare footage shot on black)
    as the ambient layer for a still plate. Blended with screen blend, so the
    layer's black stays invisible; the plate keeps full resolution."""
    meta = J.source_meta(sid)
    if not meta:
        raise HTTPException(404, "That plate is no longer on the server.")
    if meta.get("kind") == "video":
        raise HTTPException(400, "Layers go on still plates.")

    tmp = J.OVERLAYS / f".{sid}.{uuid.uuid4().hex[:8]}{Path(file.filename or '.mp4').suffix or '.mp4'}"
    with tmp.open("wb") as f:
        while chunk := await file.read(1 << 22):
            f.write(chunk)
    try:
        ometa = J.probe(tmp)
        if ometa["kind"] != "video":
            raise ValueError("that file is a still image, not a video")
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, f"Could not use that file as a layer. {e}")

    with J._overlay_lock:
        if not J.source_meta(sid):
            tmp.unlink(missing_ok=True)
            raise HTTPException(404, "That plate was removed.")
        os.replace(tmp, J.overlay_path(sid))
        meta = J.source_meta(sid)
        meta["overlay"] = {"prompt": file.filename or "uploaded layer",
                           "duration": ometa["duration"], "fps": ometa["fps"],
                           "width": ometa["width"], "height": ometa["height"],
                           "uploaded": True}
        (J.UPLOADS / f"{sid}.json").write_text(__import__("json").dumps(meta))
    return {"ok": True, "source": meta}


@app.delete("/api/sources/{sid}/overlay")
def rm_overlay(sid: str):
    with J._overlay_lock:
        meta = J.source_meta(sid)
        if not meta:
            raise HTTPException(404, "That plate is no longer on the server.")
        J.overlay_path(sid).unlink(missing_ok=True)
        meta.pop("overlay", None)
        (J.UPLOADS / f"{sid}.json").write_text(__import__("json").dumps(meta))
    return {"ok": True, "source": meta}


@app.get("/api/sources")
def sources():
    return J.list_sources()


@app.delete("/api/sources/{sid}")
def rm_source(sid: str):
    J.delete_source(sid)
    return {"ok": True}


@app.delete("/api/sources")
def rm_unused_sources():
    """Delete all source plates that are not referenced by any queued or running job."""
    active_sids = {j["source_id"] for j in J.list_jobs()
                   if j["status"] in ("queued", "running")}
    removed = 0
    for src in J.list_sources():
        if src["id"] not in active_sids:
            J.delete_source(src["id"])
            removed += 1
    return {"ok": True, "removed": removed}


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


@app.post("/api/jobs/prune")
def prune_jobs():
    """Remove encoded outputs for all done/failed/cancelled jobs; keep job records."""
    count = J.prune_done_jobs()
    return {"ok": True, "pruned": count}


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
