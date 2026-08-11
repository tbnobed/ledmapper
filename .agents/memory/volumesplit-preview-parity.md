---
name: VolumeSplit preview/encode parity
description: Why the framing preview is rendered by ffmpeg and rules for touching that path
---

The preview must stay pixel-faithful to the encode: both run the same `build_unwrap` filtergraph. Previews pass a scale factor `s<1` and render from a cached downscaled proxy; encodes always use `s=1` and the full-res plate.

**Why:** operators frame shots against the preview; a client-side reimplementation of the mapping math (mirror/blur extends, cover/contain crops) would silently diverge from what ffmpeg encodes.

**How to apply:** when changing `build_unwrap`/`fit_chain`, keep every fixed pixel constant (strip widths, blur radii) expressed in terms of `s` so `s=1` output is bit-identical to before, and verify each extend mode (mirror/blur/edge/black) at preview scale. Proxy files live in `data/uploads/proxies/` and are published atomically under a lock — keep it that way, concurrent preview requests are the norm while sliders are dragged.

User rule (also in replit.md): never propose project tasks — fix issues directly.
