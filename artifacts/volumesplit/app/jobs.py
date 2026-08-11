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
PROXIES = UPLOADS / "proxies"
OVERLAYS = UPLOADS / "overlays"
JOBS = DATA / "jobs"
PRESETS = DATA / "presets.json"
for d in (UPLOADS, PROXIES, OVERLAYS, JOBS):
    d.mkdir(parents=True, exist_ok=True)

# large uploads spool to TMPDIR before we stream them into UPLOADS; in the
# container TMPDIR points at the data disk (the default /tmp is a 2G tmpfs,
# which silently capped uploads). Make sure the dir exists — /data is a
# volume mount, so the image's mkdir doesn't cover it.
if os.environ.get("TMPDIR"):
    Path(os.environ["TMPDIR"]).mkdir(parents=True, exist_ok=True)

PROXY_W = 2048   # proxy plate width — plenty for a 1408px preview even at high zoom
_proxy_lock = threading.Lock()

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
    if not p.exists():
        return {}
    m = json.loads(p.read_text())
    if "overlay" in m:              # legacy single-layer record -> layer list
        with _overlay_lock:
            if not p.exists():      # deleted while we waited
                return {}
            m = json.loads(p.read_text())   # re-read under the lock
            if "overlay" in m:
                old = OVERLAYS / f"{sid}.mp4"
                ov = m.pop("overlay")
                ov["id"] = "l0"
                if old.exists():
                    os.replace(old, OVERLAYS / f"{sid}.l0.mp4")
                    m["overlays"] = m.get("overlays", []) + [ov]
                tmp = p.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(m))
                os.replace(tmp, p)
    return m


def list_sources() -> List[dict]:
    out = []
    for j in sorted(UPLOADS.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            m = source_meta(j.stem)     # runs legacy layer migration
            if m:
                out.append(m)
        except Exception:
            pass
    return out


# serializes overlay publish/removal, legacy migration, and source deletion,
# so a completed fal request can never resurrect a deleted source's metadata
# and concurrent sidecar rewrites can't drop layers. RLock: holders of the
# lock call source_meta(), which also acquires it for migration.
_overlay_lock = threading.RLock()


def overlay_path(sid: str, lid: str) -> Path:
    return OVERLAYS / f"{sid}.{lid}.mp4"


def valid_overlays(sid: str, meta: dict) -> List[dict]:
    """Layers whose media file actually exists, in stacking order."""
    if meta.get("kind") == "video":
        return []
    return [ov for ov in meta.get("overlays", [])
            if ov.get("id") and overlay_path(sid, ov["id"]).exists()]


def delete_source(sid: str):
    with _overlay_lock:
        for p in UPLOADS.glob(f"{sid}.*"):
            p.unlink(missing_ok=True)
        (PROXIES / f"{sid}.jpg").unlink(missing_ok=True)
        for p in OVERLAYS.glob(f"{sid}.*"):
            p.unlink(missing_ok=True)


def _still_proxy(sid: str, src: Path, meta: dict) -> tuple[Path, int, int]:
    """Downscaled copy of a still plate for fast previews. Lazily built, cached.

    Geometry is safe because fit_chain works in ratios (zoom/pan are relative),
    so a proxy of the same aspect produces an identical preview frame.
    """
    iw, ih = meta["width"], meta["height"]
    if iw <= PROXY_W:
        return src, iw, ih
    pw = PROXY_W - (PROXY_W % 2)
    ph = int(round(ih * pw / iw)) & ~1
    dest = PROXIES / f"{sid}.jpg"
    if not dest.exists():
        with _proxy_lock:
            if not dest.exists():       # re-check: another request may have built it
                tmp = PROXIES / f".{sid}.{uuid.uuid4().hex[:8]}.jpg"
                r = subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                     "-vf", f"scale={pw}:{ph}:flags=lanczos", "-frames:v", "1",
                     "-c:v", "mjpeg", "-q:v", "2", str(tmp)],
                    capture_output=True, timeout=120)
                if r.returncode != 0 or not tmp.exists():
                    tmp.unlink(missing_ok=True)
                    return src, iw, ih  # fall back to the full-res plate
                os.replace(tmp, dest)   # atomic publish — readers never see a partial file
    return dest, pw, ph


# --------------------------------------------------------------- previews ----

def render_preview_clip(sid: str, p: M.Params, width: int = 704,
                        max_dur: float = 20.0) -> bytes:
    """A low-res H.264 loop of the plate in motion — same graph as the
    encode, small scale, so operators can judge layer movement and loop
    seams without waiting for a full-res render."""
    src = source_path(sid)
    meta = source_meta(sid)
    iw, ih = meta["width"], meta["height"]
    is_video = meta.get("kind") == "video"
    if not is_video:
        src, iw, ih = _still_proxy(sid, src, meta)
    ovs = valid_overlays(sid, meta)
    if not is_video and not ovs:
        raise ValueError("This plate is a still — there is no motion to preview.")

    ops = [float(o.get("opacity", 1) or 1) for o in ovs]
    pfps = (max((float(o.get("fps", 0) or 0) for o in ovs), default=0) or 24) \
        if ovs else None
    graph = M.preview_graph(iw, ih, p, width, n_overlays=len(ovs),
                            opacities=ops, fps=pfps)

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if is_video:
        dur = min(float(meta.get("duration", 0) or 0) or max_dur, max_dur)
        cmd += ["-t", f"{dur:.3f}", "-i", str(src)]
    else:
        dur = min(max((float(o.get("duration", 0) or 0) for o in ovs),
                      default=0), max_dur)
        if dur <= 0:
            raise ValueError("Could not determine the layer duration.")
        cmd += ["-loop", "1", "-framerate", f"{pfps}", "-t", f"{dur:.3f}",
                "-i", str(src)]
        for ov in ovs:
            cmd += ["-stream_loop", "-1", "-i", str(overlay_path(sid, ov["id"]))]

    out = OVERLAYS / f".clip.{uuid.uuid4().hex[:8]}.mp4"
    cmd += ["-filter_complex", graph, "-map", "[pv]",
            "-t", f"{dur:.3f}", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "26", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(out)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode("utf8", "replace")[-300:])
        return out.read_bytes()
    finally:
        out.unlink(missing_ok=True)


def render_preview(sid: str, p: M.Params, timecode: float = 0.0,
                   width: int = 1408) -> bytes:
    """One downscaled unwrap frame, straight to stdout. Never touches disk."""
    src = source_path(sid)
    meta = source_meta(sid)
    iw, ih = meta["width"], meta["height"]
    if meta.get("kind") != "video":
        src, iw, ih = _still_proxy(sid, src, meta)
    ovs = valid_overlays(sid, meta)
    ops = [float(o.get("opacity", 1) or 1) for o in ovs]
    # same CFR normalization as the encode graph — parity includes framesync
    pfps = (max((float(o.get("fps", 0) or 0) for o in ovs), default=0) or 24) \
        if ovs else None
    graph = M.preview_graph(iw, ih, p, width, n_overlays=len(ovs),
                            opacities=ops, fps=pfps)

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if meta.get("kind") == "video" and timecode > 0:
        cmd += ["-ss", f"{timecode:.3f}"]
    cmd += ["-i", str(src)]
    for ov in ovs:
        # scrubbing a still+layers source scrubs the layers; shorter layers
        # wrap around (they loop in the encode)
        d = float(ov.get("duration", 0) or 0)
        tc = (timecode % d) if (timecode > 0 and d > 0.05) else 0
        if tc > 0:
            cmd += ["-ss", f"{tc:.3f}"]
        cmd += ["-i", str(overlay_path(sid, ov["id"]))]
    cmd += ["-filter_complex", graph,
            "-map", "[pv]", "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg",
            "-q:v", "4", "pipe:1"]

    r = subprocess.run(cmd, capture_output=True, timeout=120)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError(r.stderr.decode("utf8", "replace")[-600:])
    return r.stdout


def make_loopable(path: Path, fade: float = 1.0) -> None:
    """Crossfade the tail of a clip into its head so it loops seamlessly.

    Output is `fade` seconds shorter: the last `fade` seconds are blended
    over the first `fade` seconds, so the final frame leads exactly back
    to the first. In-place, atomic replace. No-op for stills/short clips.
    """
    meta = probe(path)
    dur = meta.get("duration", 0)
    fps = meta.get("fps") or 24
    if meta.get("kind") != "video" or dur <= fade * 2.5:
        return
    body = dur - fade
    # fps= is required: trim leaves an unknown frame rate and xfade insists on CFR
    fc = (f"[0:v]trim=0:{body},setpts=PTS-STARTPTS,fps={fps}[main];"
          f"[0:v]trim={body},setpts=PTS-STARTPTS,fps={fps}[tail];"
          f"[tail][main]xfade=transition=fade:duration={fade * 0.9}:offset=0[v]")
    tmp = path.with_name(f".{path.stem}.loop{path.suffix}")
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
         "-filter_complex", fc, "-map", "[v]",
         "-c:v", "libx264", "-crf", "16", "-preset", "medium",
         "-pix_fmt", "yuv420p", "-an", str(tmp)],
        capture_output=True, timeout=600)
    if r.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise RuntimeError("Loop pass failed: " +
                           r.stderr.decode("utf8", "replace")[-300:])
    os.replace(tmp, path)


# ------------------------------------------------------------------- jobs ----

@dataclass
class Job:
    id: str
    source_id: str
    params: dict
    encode: dict
    status: str = "queued"          # queued running done failed cancelled
    progress: float = 0.0
    frames_done: int = 0
    created: float = field(default_factory=time.time)
    started: Optional[float] = None
    finished: Optional[float] = None
    outputs: List[dict] = field(default_factory=list)
    error: str = ""
    log: List[str] = field(default_factory=list)
    source_name: str = ""
    overlay: Optional[list] = None      # layer-list snapshot at submission —
                                        # layers added/removed later can't
                                        # change what this job encodes

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
            for k in ("status", "progress", "frames_done", "created", "started", "finished",
                      "outputs", "error", "source_name", "overlay"):
                if k in d:
                    setattr(j, k, d[k])
            if j.status in ("queued", "running"):
                j.status = "failed"
                j.error = "interrupted by restart"
            _jobs[j.id] = j
        except Exception:
            pass
    _auto_prune_if_needed()     # enforce retention on old records at startup


def list_jobs() -> List[dict]:
    with _lock:
        return [j.to_dict() for j in
                sorted(_jobs.values(), key=lambda x: x.created, reverse=True)]


def get_job(jid: str) -> Optional[Job]:
    return _jobs.get(jid)


def submit(source_id: str, params: M.Params, encode: dict) -> Job:
    jid = uuid.uuid4().hex[:12]
    meta = source_meta(source_id)
    ovs = valid_overlays(source_id, meta)
    ovl = [dict(o) for o in ovs] or None
    j = Job(id=jid, source_id=source_id, params=params.dict(), encode=encode,
            source_name=meta.get("name", source_id), overlay=ovl)
    with _lock:
        _jobs[jid] = j
    j.save()
    _q.put(jid)
    return j


# retention: outputs of the newest VS_MAX_JOBS finished jobs are kept on
# disk (older ones are pruned to just their job record); job records beyond
# VS_MAX_JOB_RECORDS are removed entirely so the list can't grow forever.
# 0 = unlimited. Queued/running jobs are never touched.
VS_MAX_JOBS = int(os.environ.get("VS_MAX_JOBS", "15"))
VS_MAX_JOB_RECORDS = int(os.environ.get("VS_MAX_JOB_RECORDS", "50"))


def prune_job_outputs(jid: str) -> bool:
    """Delete encoded files and ZIP for a job, keep job.json. Returns True if job existed."""
    j = _jobs.get(jid)
    if not j:
        return False
    jdir = j.dir()
    if jdir.exists():
        for f in jdir.iterdir():
            if f.name != "job.json":
                try:
                    if f.is_dir():
                        shutil.rmtree(f, ignore_errors=True)
                    else:
                        f.unlink(missing_ok=True)
                except Exception:
                    pass
    j.outputs = []
    j.save()
    return True


def prune_done_jobs() -> int:
    """Remove output files for all done/failed/cancelled jobs. Returns count pruned."""
    with _lock:
        targets = [j.id for j in _jobs.values()
                   if j.status in ("done", "failed", "cancelled")]
    pruned = 0
    for jid in targets:
        if prune_job_outputs(jid):
            pruned += 1
    return pruned


def _auto_prune_if_needed():
    """Enforce retention: prune outputs beyond VS_MAX_JOBS, then drop job
    records (and their directories) beyond VS_MAX_JOB_RECORDS."""
    with _lock:
        done = sorted(
            [j for j in _jobs.values() if j.status in ("done", "failed", "cancelled")],
            key=lambda x: x.finished or x.created,
        )
    if VS_MAX_JOBS > 0:
        for j in done[:max(0, len(done) - VS_MAX_JOBS)]:
            prune_job_outputs(j.id)
    if VS_MAX_JOB_RECORDS > 0:
        for j in done[:max(0, len(done) - VS_MAX_JOB_RECORDS)]:
            with _lock:
                _jobs.pop(j.id, None)
            shutil.rmtree(j.dir(), ignore_errors=True)


def cancel(jid: str) -> bool:
    j = _jobs.get(jid)
    if not j:
        return False
    proc = _procs.get(jid)
    if proc and proc.poll() is None:
        proc.terminate()

        def _force_kill(p=proc):
            # a full-canvas encode can sit in a prores frame for a while and
            # shrug off SIGTERM — escalate so Cancel means now, not "later"
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()

        threading.Thread(target=_force_kill, daemon=True).start()
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

    ovs = j.overlay or []
    ovl = bool(ovs)
    if any(not overlay_path(j.source_id, o["id"]).exists() for o in ovs):
        j.status = "failed"
        j.error = ("An ambient layer this job was queued with has been "
                   "removed. Re-queue the job.")
        j.finished = time.time()
        j.save()
        return
    is_video = meta.get("kind") == "video" or ovl

    # cap filter-graph threading: on a many-core host each filter thread can
    # buffer a full-canvas frame (~86 MB in gbrp) — 30 cores of that plus two
    # encoders is how a 24 GB box hits the OOM killer mid-encode
    ft = os.environ.get("VS_FILTER_THREADS", "8")
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-y",
           "-filter_complex_threads", ft, "-filter_threads", ft,
           "-progress", "pipe:1", "-loglevel", "warning"]

    trim_start = enc.get("trim_start")
    trim_dur = enc.get("trim_duration")
    flat = None
    ov_dur = 0.0
    if ovl:
        # Still + ambient layers, two passes. Pass 1 flattens the unwrap of
        # the still ONCE (decoding a 13k plate per output frame is an OOM).
        # Pass 2 decodes that frame once, repeats it in-graph, and screen-
        # blends every layer. All layers loop (-stream_loop -1); the encode
        # runs to the LONGEST layer (or the trim), set via -t per output.
        flat = outdir / "unwrap_base.png"
        g1 = M.build_unwrap(meta["width"], meta["height"], p).rstrip(";")
        r1 = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(src), "-filter_complex", g1, "-map", "[full]",
             "-frames:v", "1", str(flat)], capture_output=True, timeout=600)
        if r1.returncode != 0 or not flat.exists():
            j.status = "failed"
            j.error = "flatten failed: " + r1.stderr.decode("utf8", "replace")[-400:]
            j.finished = time.time()
            j.save()
            return
        ofps = max((float(o.get("fps", 0) or 0) for o in ovs), default=0) or 24
        ov_dur = (parse_tc(trim_dur) if trim_dur else
                  max((float(o.get("duration", 0) or 0) for o in ovs), default=0))
        if not ov_dur or ov_dur <= 0:
            # every layer loops forever (-stream_loop -1); without a positive
            # -t this encode would never end
            j.status = "failed"
            j.error = ("Could not determine the layer duration for this "
                       "encode. Remove and re-attach the layer.")
            j.finished = time.time()
            j.save()
            return
        ops = [float(o.get("opacity", 1) or 1) for o in ovs]
        graph = (M.overlay_blend(len(ovs), 1.0, src="0:v",
                                 loop_base=True, fps=ofps, opacities=ops) +
                 M.build_outputs(p)[0]).rstrip(";")
        sizes = M.build_outputs(p)[1]
        cmd += ["-i", str(flat)]
        for o in ovs:
            cmd += ["-stream_loop", "-1"]
            if trim_start:
                cmd += ["-ss", str(trim_start)]
            cmd += ["-i", str(overlay_path(j.source_id, o["id"]))]
    else:
        graph, sizes = M.full_graph(meta["width"], meta["height"], p)
        if is_video and trim_start:
            cmd += ["-ss", str(trim_start)]
        if is_video and trim_dur:
            cmd += ["-t", str(trim_dur)]
        cmd += ["-i", str(src)]
    cmd += ["-filter_complex", graph]

    if is_video:
        spec = M.CODECS[enc.get("codec", "hapq")]
        ext = spec["ext"]
        for label in sizes:
            cmd += ["-map", f"[{label}]"] + spec["args"] + ["-an"]
            if ovl and ov_dur > 0:
                cmd += ["-t", f"{ov_dur:.3f}"]   # layers loop; -t ends the encode
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
        if ovl:
            d = ov_dur
        else:
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
        tm = TIME_RE.search(line)
        fm = FRAME_RE.search(line)
        if tm and total_us:
            j.progress = min(0.99, int(tm.group(1)) / total_us)
        elif fm and not total_us:
            j.progress = 0.5
        if fm:
            j.frames_done = int(fm.group(1))
        if time.time() - last > 0.5:
            last = time.time()
            j.save()

    rc = proc.wait()
    _procs.pop(j.id, None)
    if flat is not None:
        flat.unlink(missing_ok=True)

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
    _auto_prune_if_needed()


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
