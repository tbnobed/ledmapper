"""AI plate generation via fal.ai (Seedream 4).

Plain stdlib HTTPS — no extra dependencies, works identically in Replit
dev and the Docker deployment. Needs FAL_KEY in the environment.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

FAL_URL = "https://fal.run/fal-ai/bytedance/seedream/v4/text-to-image"
FAL_QUEUE = "https://queue.fal.run"
ANIMATE_MODEL = "fal-ai/bytedance/seedance/v1/pro/image-to-video"
# queue status/result live under the model ROOT (owner/alias), not the full subpath
ANIMATE_ROOT = "/".join(ANIMATE_MODEL.split("/")[:2])

# Target sizes (Seedream native ceiling is 4096; min side 1024).
# center: 2.4:1 — lands on the 6144x2560 center wall at 1.5x, within tolerance.
# canvas: 4:1  — widest allowed; slightly tighter than the 4.4:1 unwrap,
#                cover fit crops a sliver top/bottom.
SIZES = {
    "center": (4096, 1712),
    "canvas": (4096, 1024),
}

# AI upscale pass after generation: reconstructs real texture instead of the
# soft blur plain resampling gives when the plate is stretched to wall res.
# The upscaler rejects outputs much past ~30 MP (422 "image is too large"),
# so the factor is derived per plate: the largest whole multiple that stays
# under the cap, at most the ideal for the target wall size.
UPSCALE_URL = "https://fal.run/fal-ai/clarity-upscaler"
UPSCALE_IDEAL = {"center": 2, "canvas": 3}
UPSCALE_MAX_MP = 29_000_000


def safe_upscale_factor(w: int, h: int, target: str) -> int:
    ideal = UPSCALE_IDEAL.get(target, 2)
    f = ideal
    while f > 1 and w * h * f * f > UPSCALE_MAX_MP:
        f -= 1
    return f


def generate_plate(prompt: str, target: str = "center") -> bytes:
    key = os.environ.get("FAL_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "FAL_KEY is not set. Add your fal.ai API key to the environment "
            "(docker-compose environment block, or Replit Secrets in dev).")

    w, h = SIZES.get(target, SIZES["center"])
    body = json.dumps({
        "prompt": prompt,
        "image_size": {"width": w, "height": h},
        "num_images": 1,
        "enable_safety_checker": True,
    }).encode()

    req = urllib.request.Request(FAL_URL, data=body, headers={
        "Authorization": f"Key {key}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf8", "replace")[:300]
        raise RuntimeError(f"fal.ai returned {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach fal.ai: {e.reason}")

    imgs = d.get("images") or []
    if not imgs or not imgs[0].get("url"):
        raise RuntimeError("Model returned no image.")
    src_url = imgs[0]["url"]

    # upscale from the fal-hosted URL — no re-upload needed. If the
    # upscaler fails we keep the raw plate rather than losing a paid
    # generation; the caller surfaces upscale_error to the operator.
    upscale_error = None
    fetch_url = src_url
    factor = safe_upscale_factor(w, h, target)
    if factor > 1:
        try:
            fetch_url = upscale_image(src_url, factor)
        except Exception as e:
            upscale_error = str(e)[:300]

    with urllib.request.urlopen(fetch_url, timeout=300) as r:
        data = r.read()
    if not data:
        raise RuntimeError("Downloaded image was empty.")
    return data, upscale_error


def upscale_image(image_url: str, factor: int) -> str:
    """Run the clarity upscaler on a hosted image; returns the result URL.
    creativity kept low / resemblance high: sharpen and add texture, do not
    reinvent the composition."""
    d = _fal_json(UPSCALE_URL, {
        "image_url": image_url,
        "upscale_factor": factor,
        "creativity": 0.2,
        "resemblance": 0.8,
    }, timeout=600)
    url = (d.get("image") or {}).get("url")
    if not url:
        raise RuntimeError("Upscaler returned no image.")
    return url


# ------------------------------------------------------------- animation ----

def _key() -> str:
    key = os.environ.get("FAL_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "FAL_KEY is not set. Add your fal.ai API key to the environment "
            "(docker-compose environment block, or Replit Secrets in dev).")
    return key


def _fal_json(url: str, payload: dict | None = None,
              timeout: int = 120) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Key {_key()}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf8", "replace")[:300]
        raise RuntimeError(f"fal.ai returned {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach fal.ai: {e.reason}")


def _black_png(w: int = 1344, h: int = 576) -> bytes:
    """A pure black PNG built with stdlib only (start/end frame for overlays)."""
    import struct, zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)   # 8-bit grayscale
    raw = zlib.compress(b"".join(b"\x00" * (w + 1) for _ in range(h)), 9)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def submit_overlay(prompt: str, duration: int = 5) -> str:
    """Queue generation of an ambient OVERLAY layer: particles on pure black.

    The plate itself never goes through the video model (which would cap it
    at 1080p). Instead the model animates bright elements on a black frame;
    the app screen-blends the result over the full-resolution plate in
    ffmpeg. Start and end frames are both black, so the layer fades in/out
    of nothing — a mathematically clean loop.
    """
    uri = "data:image/png;base64," + base64.b64encode(_black_png()).decode()
    d = _fal_json(f"{FAL_QUEUE}/{ANIMATE_MODEL}", {
        "prompt": ("A pure black background — no scenery, no objects, no "
                   "people, no light sources, no gradients. The only visible "
                   "content is bright ambient atmosphere floating against "
                   "the black void, filling the whole frame edge to edge: "
                   + prompt +
                   ". Locked-off static camera, elements drift gently."),
        "image_url": uri,
        "end_image_url": uri,       # fade in from black, fade out to black
        "resolution": "1080p",
        "aspect_ratio": "21:9",     # matches the 2.4:1 center wall closely
        "duration": str(duration),
        "camera_fixed": True,
    })
    rid = d.get("request_id")
    if not rid:
        raise RuntimeError(f"fal.ai did not return a request id: {str(d)[:200]}")
    return rid


def poll_animation(rid: str) -> dict:
    """One status check. Returns {'status': ...} or {'status':'done','data': bytes}."""
    st = _fal_json(f"{FAL_QUEUE}/{ANIMATE_ROOT}/requests/{rid}/status")
    status = st.get("status", "UNKNOWN")
    if status in ("IN_QUEUE", "IN_PROGRESS"):
        return {"status": status.lower()}
    if status != "COMPLETED":
        raise RuntimeError(f"Animation failed: {str(st)[:300]}")
    d = _fal_json(f"{FAL_QUEUE}/{ANIMATE_ROOT}/requests/{rid}")
    url = (d.get("video") or {}).get("url")
    if not url:
        raise RuntimeError("Model returned no video.")
    with urllib.request.urlopen(url, timeout=300) as r:
        data = r.read()
    if not data:
        raise RuntimeError("Downloaded video was empty.")
    return {"status": "done", "data": data}
