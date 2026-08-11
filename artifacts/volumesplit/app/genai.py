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

    with urllib.request.urlopen(imgs[0]["url"], timeout=180) as r:
        data = r.read()
    if not data:
        raise RuntimeError("Downloaded image was empty.")
    return data


# ------------------------------------------------------------- animation ----

def _key() -> str:
    key = os.environ.get("FAL_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "FAL_KEY is not set. Add your fal.ai API key to the environment "
            "(docker-compose environment block, or Replit Secrets in dev).")
    return key


def _fal_json(url: str, payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Key {_key()}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf8", "replace")[:300]
        raise RuntimeError(f"fal.ai returned {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach fal.ai: {e.reason}")


def submit_animation(prompt: str, image_data: bytes, mime: str,
                     duration: int = 5) -> str:
    """Queue an image-to-video job on fal. Returns the fal request id.

    The plate goes up as a data URI so this works without a public URL —
    required for the Docker deployment behind a private network.
    Camera is locked: plates must not drift behind the actors.
    """
    uri = f"data:{mime};base64," + base64.b64encode(image_data).decode()
    d = _fal_json(f"{FAL_QUEUE}/{ANIMATE_MODEL}", {
        "prompt": prompt,
        "image_url": uri,
        "resolution": "1080p",
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
