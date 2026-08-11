"""AI plate generation via fal.ai (Seedream 4).

Plain stdlib HTTPS — no extra dependencies, works identically in Replit
dev and the Docker deployment. Needs FAL_KEY in the environment.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

FAL_URL = "https://fal.run/fal-ai/bytedance/seedream/v4/text-to-image"

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
