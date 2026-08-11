"""Media probing, job queue, and ffmpeg execution with progress."""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import mapping as M

DATA = Path(os.environ.get("VS_DATA", "/data"))
UPLOADS = DATA / "uploads"
JOBS = DATA / "jobs"
PRESETS = DATA / "presets.json"
for d in (UPLOADS, JOBS):
    d.mkdir(parents=True, exist_ok=True)

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp", ".exr"}


# ------------------------------------------------------------------ probe ----

def probe(path: Path) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise ValueError(f"not a readable media file: {r.stderr.strip()[:200]}")
    d = json.loads(r.stdout)
    if not d.get("streams"):
        raise ValueError("no video stream found")
    st = d["streams"][0]

    fps = 0.0
    rate = st.get("r_frame_rate") or "0/1"
    try:
        num, den = rate.split("/")
        fps = float(num) / float(den) if float(den) else 0.0
    except Exception:
        pass

    dur = float(d.get("format", {}).get("duration") or 0)
    is_image = path.suffix.lower() in IMAGE_EXT or st.get("codec_name") in (
        "png", "mjpeg", "tiff", "bmp", "webp", "exr")

    return {
        "width": int(st["width"]), "height": int(st["height"]),
        "fps": round(fps, 3), "duration": round(dur, 3),
        "codec": st.get("codec_name", "?"), "pix_fmt": st.get("pix_fmt", "?"),
        "kind": "image" if is_image else "video",
        "size_bytes": path.stat().st_size,
    }


# ---------------------------------------------------------------- sources ----

def source_path(sid: str) -> Path:
    for p in sorted(UPLOADS.glob(f"{sid}.*")):
        if p.suffix.lower() != ".json":     # skip our own metadata sidecar
            return p
    raise FileNotFoundError(sid)


def source_meta(sid: str) -> dict:
    p = UPLOADS / f"{sid}.json"
    return json.loads(p.read_text()) if p.exists() else {}


def list_sources() -> List[dict]:
    out = []
    for j in sorted(UPLOADS.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            out.append(json.loads(j.read_text()))
        except Exception:
            pass
    return out


def delete_source(sid: str):
    for p in UPLOADS.glob(f"{sid}.*"):
        p.unlink(missing_ok=True)


# --------------------------------------------------------------- previews ----

def render_preview(sid: str, p: M.Params, timecode: float = 0.0,
                   width: int = 1408) -> bytes:
    """One downscaled unwrap frame, straight to stdout. Never touches disk."""
    src = source_path(sid)
    meta = source_meta(sid)
    graph = M.preview_graph(meta["width"], meta["height"], p, width)

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if meta.get("kind") == "video" and timecode > 0:
        cmd += ["-ss", f"{timecode:.3f}"]
    cmd += ["-i", str(src), "-filter_complex", graph,
            "-map", "[pv]", "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg",
            "-q:v", "4", "pipe:1"]

    r = subprocess.run(cmd, capture_output=True, timeout=120)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError(r.stderr.decode("utf8", "replace")[-600:])
    return r.stdout


# ------------------------------------------------------------------- jobs ----

@dataclass
class Job:
    id: str
    source_id: str
    params: dict
    encode: dict
    status: str = "queued"          # queued running done failed cancelled
    progress: float = 0.0
    created: float = field(default_factory=time.time)
    started: Optional[float] = None
    finished: Optional[float] = None
    outputs: List[dict] = field(default_factory=list)
    error: str = ""
    log: List[str] = field(default_factory=list)
    source_name: str = ""

    def dir(self) -> Path:
        return JOBS / self.id

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["log"] = self.log[-40:]
        d["elapsed"] = round((self.finished or time.time()) - (self.started or self.created), 1)
        return d

    def save(self):
        self.dir().mkdir(parents=True, exist_ok=True)
        (self.dir() / "job.json").write_text(json.dumps(self.to_dict(), indent=2))


_jobs: Dict[str, Job] = {}
_q: "queue.Queue[str]" = queue.Queue()
_lock = threading.Lock()
_procs: Dict[str, subprocess.Popen] = {}


def load_jobs():
    for jd in sorted(JOBS.glob("*/job.json")):
        try:
            d = json.loads(jd.read_text())
            j = Job(id=d["id"], source_id=d["source_id"], params=d["params"],
                    encode=d["encode"])
            for k in ("status", "progress", "created", "started", "finished",
                      "outputs", "error", "source_name"):
                if k in d:
                    setattr(j, k, d[k])
            if j.status in ("queued", "running"):
                j.status = "failed"
                j.error = "interrupted by restart"
            _jobs[j.id] = j
        except Exception:
            pass


def list_jobs() -> List[dict]:
    with _lock:
        return [j.to_dict() for j in
                sorted(_jobs.values(), key=lambda x: x.created, reverse=True)]


def get_job(jid: str) -> Optional[Job]:
    return _jobs.get(jid)


def submit(source_id: str, params: M.Params, encode: dict) -> Job:
    jid = uuid.uuid4().hex[:12]
    meta = source_meta(source_id)
    j = Job(id=jid, source_id=source_id, params=params.dict(), encode=encode,
            source_name=meta.get("name", source_id))
    with _lock:
        _jobs[jid] = j
    j.save()
    _q.put(jid)
    return j


def cancel(jid: str) -> bool:
    j = _jobs.get(jid)
    if not j:
        return False
    proc = _procs.get(jid)
    if proc and proc.poll() is None:
        proc.terminate()
    if j.status in ("queued", "running"):
        j.status = "cancelled"
        j.save()
    return True


TIME_RE = re.compile(rb"out_time_us=(\d+)")
FRAME_RE = re.compile(rb"frame=(\d+)")


def parse_tc(v) -> float:
    """Accept 12.5, '12.5', '00:00:12.5', '1:02:03'. Returns seconds."""
    if v is None:
        return 0.0
    s = str(v).strip()
    if not s:
        return 0.0
    try:
        if ":" not in s:
            return float(s)
        parts = [float(x) for x in s.split(":")]
        total = 0.0
        for x in parts:
            total = total * 60 + x
        return total
    except ValueError:
        return 0.0


def _run(j: Job):
    j.status = "running"
    j.started = time.time()
    j.save()

    src = source_path(j.source_id)
    meta = source_meta(j.source_id)
    p = M.Params(**j.params).validate()
    enc = j.encode
    outdir = j.dir()
    outdir.mkdir(parents=True, exist_ok=True)

    graph, sizes = M.full_graph(meta["width"], meta["height"], p)
    is_video = meta.get("kind") == "video"

    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-y",
           "-progress", "pipe:1", "-loglevel", "warning"]

    trim_start = enc.get("trim_start")
    trim_dur = enc.get("trim_duration")
    if is_video and trim_start:
        cmd += ["-ss", str(trim_start)]
    if is_video and trim_dur:
        cmd += ["-t", str(trim_dur)]

    cmd += ["-i", str(src), "-filter_complex", graph]

    if is_video:
        spec = M.CODECS[enc.get("codec", "hapq")]
        ext = spec["ext"]
        for label in sizes:
            cmd += ["-map", f"[{label}]"] + spec["args"] + ["-an"]
            if enc.get("fps"):
                cmd += ["-r", str(enc["fps"])]
            cmd += [str(outdir / f"{label}.{ext}")]
    else:
        spec = M.IMAGE_FORMATS[enc.get("image_format", "png")]
        ext = spec["ext"]
        for label in sizes:
            cmd += ["-map", f"[{label}]", "-frames:v", "1"] + spec["args"]
            cmd += [str(outdir / f"{label}.{ext}")]

    (outdir / "command.txt").write_text(" ".join(cmd))

    total_us = 0.0
    if is_video:
        d = parse_tc(trim_dur) if trim_dur else float(meta.get("duration", 0) or 0)
        total_us = d * 1_000_000

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _procs[j.id] = proc

    def drain_err():
        for line in iter(proc.stderr.readline, b""):
            s = line.decode("utf8", "replace").rstrip()
            if s:
                j.log.append(s)
                del j.log[:-200]
    threading.Thread(target=drain_err, daemon=True).start()

    last = 0.0
    for line in iter(proc.stdout.readline, b""):
        m = TIME_RE.search(line)
        if m and total_us:
            j.progress = min(0.99, int(m.group(1)) / total_us)
        elif FRAME_RE.search(line) and not total_us:
            j.progress = 0.5
        if time.time() - last > 0.5:
            last = time.time()
            j.save()

    rc = proc.wait()
    _procs.pop(j.id, None)

    if j.status == "cancelled":
        shutil.rmtree(outdir, ignore_errors=True)
        return

    if rc != 0:
        j.status = "failed"
        j.error = "\n".join(j.log[-12:]) or f"ffmpeg exited {rc}"
        j.progress = 0.0
    else:
        outs = []
        for label, (w, h) in sizes.items():
            f = outdir / f"{label}.{ext}"
            if f.exists():
                outs.append({"name": f.name, "label": label, "w": w, "h": h,
                             "bytes": f.stat().st_size,
                             "node": M.NODE.get(label, "")})
        j.outputs = outs
        j.progress = 1.0
        j.status = "done"

        zpath = outdir / "outputs.zip"
        try:
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
                for o in outs:
                    z.write(outdir / o["name"], o["name"])
                z.writestr("mapping.txt", _manifest(j, sizes))
        except Exception as e:
            j.log.append(f"zip failed: {e}")

    j.finished = time.time()
    j.save()


def _manifest(j: Job, sizes) -> str:
    p = j.params
    lines = [
        "Volume wall split",
        f"job         {j.id}",
        f"source      {j.source_name}",
        f"unwrap      {M.CANVAS_W} x {M.CANVAS_H}",
        f"mode        {p['mode']}   fit {p['fit']}",
        f"zoom        {p['zoom']}   pan {p['pan_x']}, {p['pan_y']}",
        f"deck line   {p['valign']}",
        f"side fill   {p['extend']} @ {p['extend_width']}px sample",
        f"grid        {'on' if p['grid'] else 'off'}",
        "",
    ]
    for label, (w, h) in sizes.items():
        lines.append(f"{label:<8} {w} x {h}   {M.NODE.get(label,'')}")
    return "\n".join(lines) + "\n"


def _worker():
    while True:
        jid = _q.get()
        j = _jobs.get(jid)
        if not j or j.status == "cancelled":
            continue
        try:
            _run(j)
        except Exception as e:
            j.status = "failed"
            j.error = str(e)
            j.finished = time.time()
            j.save()


def start_workers(n: int = 1):
    load_jobs()
    for _ in range(max(1, n)):
        threading.Thread(target=_worker, daemon=True).start()


# ---------------------------------------------------------------- presets ----

def load_presets() -> dict:
    if PRESETS.exists():
        try:
            return json.loads(PRESETS.read_text())
        except Exception:
            return {}
    return {}


def save_preset(name: str, params: dict):
    d = load_presets()
    d[name] = params
    PRESETS.write_text(json.dumps(d, indent=2))
    return d


def delete_preset(name: str):
    d = load_presets()
    d.pop(name, None)
    PRESETS.write_text(json.dumps(d, indent=2))
    return d
