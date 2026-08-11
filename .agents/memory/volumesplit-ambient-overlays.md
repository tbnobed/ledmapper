---
name: VolumeSplit ambient overlays
description: Why AI atmosphere is a screen-blended layer on black, never a re-generated plate
---

Ambient motion (dust, flares) is generated as a LAYER on pure black (Seedance i2v, black start+end frame, 21:9) and screen-blended over the full-res plate inside the shared ffmpeg graph. The plate itself must never pass through a video model.

**Why:** i2v caps output at 1080p (destroys a 13k plate), reinvents composition, and animates subjects (a mid-stride figure keeps walking no matter the prompt). Screen blend on black adds only bright particles; the plate stays pixel-identical.

**How to apply:** any new AI-motion feature = layer + blend, not plate regeneration. Overlay encodes are two-pass: flatten the still's unwrap ONCE to PNG, then `loop=-1:size=1` + `fps=` in-graph — per-frame 13k PNG decode OOMs even 8 GB. ffmpeg gotchas: `xfade`/`blend` need CFR — re-stamp `fps=` after `trim`; fal queue status URLs use the model root (owner/alias), not the full subpath; this dev container's ffmpeg lacks Hap `-chunks` (Docker prod has it) — test encodes with prores here. Overlay publish/removal and source deletion serialize on one lock; jobs snapshot overlay state at submission.
