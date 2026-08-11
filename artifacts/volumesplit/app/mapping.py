"""Wall geometry and ffmpeg filtergraph construction.

Single source of truth for the mapping. Stills and video both run through
ffmpeg with the same graph, so there is only one thing to get right.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Tuple

# ---------------------------------------------------------------- geometry ---

CAB = 256  # cabinet pixel pitch (500mm cabinet)

WALLS: Dict[str, dict] = {
    "left":   {"w": 2560, "h": 2048, "cw": 10, "ch": 8,  "sx40": 1},
    "center": {"w": 6144, "h": 2560, "cw": 24, "ch": 10, "sx40": 4},
    "right":  {"w": 2560, "h": 2048, "cw": 10, "ch": 8,  "sx40": 1},
}
ORDER = ["left", "center", "right"]
CANVAS_W = sum(WALLS[n]["w"] for n in ORDER)   # 11264
CANVAS_H = max(WALLS[n]["h"] for n in ORDER)   # 2560

# which render node each output belongs to
NODE = {
    "center": "RTX PRO 4500 · 4x SX40",
    "sides":  "RTX PRO 4000 · 2x SX40",
    "left":   "RTX PRO 4000 · 1x SX40",
    "right":  "RTX PRO 4000 · 1x SX40",
}


def wall_boxes(valign: str = "bottom") -> Dict[str, Tuple[int, int, int, int]]:
    """{name: (x, y, w, h)} on the full unwrap."""
    boxes, x = {}, 0
    for n in ORDER:
        w, h = WALLS[n]["w"], WALLS[n]["h"]
        slack = CANVAS_H - h
        y = 0 if valign == "top" else (slack if valign == "bottom" else slack // 2)
        boxes[n] = (x, y, w, h)
        x += w
    return boxes


def even(v: float) -> int:
    i = int(round(v))
    return i if i % 2 == 0 else i + 1


# ------------------------------------------------------------------ params ---

@dataclass
class Params:
    mode: str = "center"          # center | canvas
    fit: str = "cover"            # cover | contain | stretch | none
    zoom: float = 1.0
    pan_x: float = 0.0
    pan_y: float = 0.0
    valign: str = "bottom"        # bottom | center | top
    extend: str = "mirror"        # mirror | blur | edge | black
    extend_width: int = 1024
    outputs: str = "machine"      # machine | walls | both
    grid: bool = False

    def dict(self):
        return asdict(self)

    def validate(self):
        if self.mode not in ("center", "canvas"):
            raise ValueError("mode must be center or canvas")
        if self.fit not in ("cover", "contain", "stretch", "none"):
            raise ValueError("bad fit")
        if self.valign not in ("bottom", "center", "top"):
            raise ValueError("bad valign")
        if self.extend not in ("mirror", "blur", "edge", "black"):
            raise ValueError("bad extend")
        if self.outputs not in ("machine", "walls", "both"):
            raise ValueError("bad outputs")
        self.zoom = max(0.2, min(4.0, float(self.zoom)))
        self.pan_x = max(-1.0, min(1.0, float(self.pan_x)))
        self.pan_y = max(-1.0, min(1.0, float(self.pan_y)))
        self.extend_width = max(16, min(int(self.extend_width), WALLS["center"]["w"]))
        return self


# ------------------------------------------------------------ filtergraph ----

def fit_chain(iw: int, ih: int, tw: int, th: int, p: Params) -> List[str]:
    """Filters mapping a iw x ih source onto a tw x th rectangle."""
    if p.fit == "stretch":
        return [f"scale={tw}:{th}:flags=lanczos"]

    if p.fit == "none":
        scale = 1.0
    elif p.fit == "contain":
        scale = min(tw / iw, th / ih)
    else:
        scale = max(tw / iw, th / ih)
    scale *= p.zoom

    nw, nh = even(iw * scale), even(ih * scale)
    f = [f"scale={nw}:{nh}:flags=lanczos"]

    if nw > tw or nh > th:
        cw, chh = min(nw, tw), min(nh, th)
        sx, sy = (nw - tw) / 2.0, (nh - th) / 2.0
        cx = int(round(sx + p.pan_x * abs(sx))) if nw > tw else 0
        cy = int(round(sy + p.pan_y * abs(sy))) if nh > th else 0
        f.append(f"crop={cw}:{chh}:{max(0, min(cx, nw - cw))}:"
                 f"{max(0, min(cy, nh - chh))}")
        nw, nh = cw, chh

    if nw < tw or nh < th:
        px = int(round((tw - nw) / 2.0 - p.pan_x * (tw - nw) / 2.0)) if nw < tw else 0
        py = int(round((th - nh) / 2.0 - p.pan_y * (th - nh) / 2.0)) if nh < th else 0
        f.append(f"pad={tw}:{th}:{max(0, min(px, tw - nw))}:"
                 f"{max(0, min(py, th - nh))}:black")

    return f


def _mirror_panel(src: str, out: str, sample: int, panel_w: int, panel_h: int,
                  seam: str, blur: int = 0) -> str:
    """Reflect a sample strip repeatedly to fill a panel, seamless at the seam."""
    n = max(1, math.ceil(panel_w / sample))
    tiles = [f"{out}t{i}" for i in range(n)]
    parts = ["[%s]split=%d%s;" % (src, n, "".join(f"[{t}a]" for t in tiles))]
    for i, t in enumerate(tiles):
        flip = (i % 2 == 0) if seam == "left" else ((n - 1 - i) % 2 == 0)
        parts.append(f"[{t}a]{'hflip' if flip else 'null'}[{t}];")
    parts.append("".join(f"[{t}]" for t in tiles) + f"hstack=inputs={n}[{out}w];")
    cx = 0 if seam == "left" else n * sample - panel_w
    parts.append(f"[{out}w]crop={panel_w}:{panel_h}:{cx}:0"
                 + (f",gblur=sigma={blur}" if blur else "") + f"[{out}];")
    return "".join(parts)


def build_unwrap(iw: int, ih: int, p: Params, s: float = 1.0,
                 out: str = "full") -> str:
    """Graph fragment producing [out] = the unwrap (11264x2560 at s=1).

    s < 1 composes the whole graph at reduced scale — used by previews so the
    mirror/blur/hstack work happens on ~10x fewer pixels. Encodes use s=1.
    """
    g: List[str] = []
    if p.mode == "canvas":
        g.append("[0:v]" + ",".join(
            fit_chain(iw, ih, even(CANVAS_W * s), even(CANVAS_H * s), p)) + f"[{out}];")
        return "".join(g)

    cw, ch = even(WALLS["center"]["w"] * s), even(WALLS["center"]["h"] * s)
    g.append("[0:v]" + ",".join(fit_chain(iw, ih, cw, ch, p)) + "[cen0];")
    g.append("[cen0]split=3[cenA][cenL][cenR];")

    lw, rw = even(WALLS["left"]["w"] * s), even(WALLS["right"]["w"] * s)
    samp = max(16, min(even(p.extend_width * s), cw))

    if p.extend == "black":
        g.append("[cenL]nullsink;[cenR]nullsink;")
        g.append(f"color=c=black:s={lw}x{ch}[lp];")
        g.append(f"color=c=black:s={rw}x{ch}[rp];")
    elif p.extend == "edge":
        es = max(2, even(8 * s))        # 8px strip at s=1, scaled for previews
        g.append(f"[cenL]crop={es}:{ch}:0:0,scale={lw}:{ch}[lp];")
        g.append(f"[cenR]crop={es}:{ch}:{cw - es}:0,scale={rw}:{ch}[rp];")
    else:
        blur = max(2, round(90 * s)) if p.extend == "blur" else 0
        g.append(f"[cenL]crop={samp}:{ch}:0:0[lstrip];")
        g.append(f"[cenR]crop={samp}:{ch}:{cw - samp}:0[rstrip];")
        g.append(_mirror_panel("lstrip", "lp", samp, lw, ch, "right", blur))
        g.append(_mirror_panel("rstrip", "rp", samp, rw, ch, "left", blur))

    g.append(f"[lp][cenA][rp]hstack=inputs=3[{out}];")
    return "".join(g)


def overlay_blend(n: int, s: float = 1.0, src: str = "pre", out: str = "full",
                  loop_base: bool = False, fps: float | None = None,
                  opacities: list | None = None) -> str:
    """Screen-blend n ambient layers (inputs 1..n, dust/flares on black) over
    [src], in order. Screen blend is associative: each layer adds only its
    bright content, black stays invisible, the plate is untouched. Runs
    INSIDE the shared graph at scale s — preview and encode stay identical
    by construction.

    loop_base: src is a single pre-flattened unwrap frame; decode it once and
    repeat it in-graph (decoding a 11264-wide still per frame is what OOMs).
    fps: normalize base and every layer to one CFR (layers can have mixed
    frame rates; blend's framesync needs a common timeline).
    opacities: per-layer 0..1 multiplier applied BEFORE the screen blend —
    dimming the layer scales exactly how much light it adds to the plate.
    """
    w, h = even(CANVAS_W * s), even(CANVAS_H * s)
    base = f"[{src}]"
    if loop_base:
        base += "loop=loop=-1:size=1,"
    if fps:
        base += f"fps={fps},"
    # setpts=PTS-STARTPTS on every branch: blend's framesync pairs frames by
    # timestamp, and a seeked (-ss) layer input starts at a nonzero pts —
    # without normalization the blend silently passes the plate through
    # normalize the base to the same rounded size as the layers — unwrap
    # padding can round differently at odd preview scales (blend requires
    # equal sizes). At s=1 this is exactly canvas size: a no-op, parity safe.
    g = base + f"setpts=PTS-STARTPTS,scale={w}:{h}:flags=bilinear,setsar=1,format=gbrp[ob0];"
    for i in range(1, n + 1):
        o = opacities[i - 1] if opacities else 1.0
        dim = (f"colorchannelmixer=rr={o:.4f}:gg={o:.4f}:bb={o:.4f},"
               if o < 0.9995 else "")
        g += (f"[{i}:v]scale={w}:{h}:flags=bilinear,setsar=1,"
              "setpts=PTS-STARTPTS,"
              + (f"fps={fps}," if fps else "") + f"format=gbrp,{dim}"
              f"setsar=1[ol{i}];"
              f"[ob{i - 1}][ol{i}]blend=all_mode=screen[ob{i}];")
    return g + f"[ob{n}]format=yuv420p[{out}];"


def _grid_overlay(label: str, w: int, h: int, out: str) -> str:
    """Draw the cabinet grid over a wall stream."""
    lines = []
    for gx in range(0, w + 1, CAB):
        lines.append(f"drawbox=x={min(gx, w - 2)}:y=0:w=2:h={h}:color=lime@0.6:t=fill")
    for gy in range(0, h + 1, CAB):
        lines.append(f"drawbox=x=0:y={min(gy, h - 2)}:w={w}:h=2:color=lime@0.6:t=fill")
    return f"[{label}]" + ",".join(lines) + f"[{out}];"


def build_outputs(p: Params) -> Tuple[str, Dict[str, Tuple[int, int]]]:
    """Graph fragment splitting [full] into named output streams."""
    boxes = wall_boxes(p.valign)
    want_walls = p.outputs in ("walls", "both")
    want_sides = p.outputs in ("machine", "both")

    g = ["[full]split=3[t_l][t_c][t_r];"]
    for tap, n in (("t_l", "left"), ("t_c", "center"), ("t_r", "right")):
        x, y, w, h = boxes[n]
        g.append(f"[{tap}]crop={w}:{h}:{x}:{y}[W_{n}];")

    sizes: Dict[str, Tuple[int, int]] = {}
    cx, cy, cwd, chd = boxes["center"]

    if p.grid:
        g.append(_grid_overlay("W_center", cwd, chd, "center"))
    else:
        g.append("[W_center]null[center];")
    sizes["center"] = (cwd, chd)

    for n in ("left", "right"):
        x, y, w, h = boxes[n]
        uses = (1 if want_walls else 0) + (1 if want_sides else 0)
        base = f"W_{n}"
        if p.grid:
            g.append(_grid_overlay(base, w, h, f"G_{n}"))
            base = f"G_{n}"
        if uses == 2:
            g.append(f"[{base}]split=2[{n}][{n}_s];")
        elif want_walls:
            g.append(f"[{base}]null[{n}];")
        else:
            g.append(f"[{base}]null[{n}_s];")
        if want_walls:
            sizes[n] = (w, h)

    if want_sides:
        lw, lh = boxes["left"][2], boxes["left"][3]
        rw = boxes["right"][2]
        g.append("[left_s][right_s]hstack=inputs=2[sides];")
        sizes["sides"] = (lw + rw, lh)

    return "".join(g), sizes


def full_graph(iw: int, ih: int, p: Params, n_overlays: int = 0,
               opacities: list | None = None) -> Tuple[str, Dict[str, Tuple[int, int]]]:
    if n_overlays:
        a = (build_unwrap(iw, ih, p, out="pre") +
             overlay_blend(n_overlays, 1.0, opacities=opacities))
    else:
        a = build_unwrap(iw, ih, p)
    b, sizes = build_outputs(p)
    graph = a + b
    return graph.rstrip(";"), sizes


def preview_graph(iw: int, ih: int, p: Params, width: int = 1408,
                  n_overlays: int = 0, opacities: list | None = None,
                  fps: float | None = None) -> str:
    """Graph producing a single downscaled unwrap for the framing preview."""
    k = width / CANVAS_W
    ph = even(CANVAS_H * k)
    if n_overlays:
        a = (build_unwrap(iw, ih, p, s=k, out="pre") +
             overlay_blend(n_overlays, k, opacities=opacities, fps=fps))
    else:
        a = build_unwrap(iw, ih, p, s=k)
    return (a + f"[full]scale={even(width)}:{ph}:flags=bilinear[pv]").rstrip(";")


# ------------------------------------------------------------------ codecs ---

CODECS = {
    "hapq":   {"args": ["-c:v", "hap", "-format", "hap_q", "-chunks", "8"],
               "ext": "mov", "label": "Hap Q",
               "note": "GPU decode, ~2.1 MB/frame at 6144x2560"},
    "hap":    {"args": ["-c:v", "hap", "-format", "hap", "-chunks", "8"],
               "ext": "mov", "label": "Hap",
               "note": "smaller, lighter chroma"},
    "prores": {"args": ["-c:v", "prores_ks", "-profile:v", "3",
                        "-pix_fmt", "yuv422p10le", "-vendor", "apl0"],
               "ext": "mov", "label": "ProRes 422 HQ", "note": "10-bit, CPU decode"},
    "dnxhr":  {"args": ["-c:v", "dnxhd", "-profile:v", "dnxhr_hq",
                        "-pix_fmt", "yuv422p"],
               "ext": "mov", "label": "DNxHR HQ", "note": "10-bit, CPU decode"},
    # rc-lookahead/ref trimmed: at 6144x2560 the x264 defaults buffer ~40
    # frames per encoder (~1 GB each, two encoders per job) — the difference
    # between finishing and the OOM killer on a modest Docker host.
    "h264":   {"args": ["-c:v", "libx264", "-crf", "18", "-preset", "fast",
                        "-x264-params", "rc-lookahead=12:ref=2:threads=4",
                        "-pix_fmt", "yuv420p"],
               "ext": "mp4", "label": "H.264 (preview only)",
               "note": "most decoders cap below 6144 wide"},
}

IMAGE_FORMATS = {
    "png": {"args": [], "ext": "png", "label": "PNG"},
    "jpg": {"args": ["-q:v", "2"], "ext": "jpg", "label": "JPG"},
    "tif": {"args": [], "ext": "tif", "label": "TIFF"},
}
