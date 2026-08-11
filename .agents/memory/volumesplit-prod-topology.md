---
name: VolumeSplit prod topology
description: Production request path — nginx reverse proxy fronts the Docker container; where upload limits and temp-spool constraints live.
---

# VolumeSplit production topology

Browser → nginx reverse proxy (`led-c.trinity.local`, port 80) → Docker container (uvicorn, port 8080) on the user's VMware VM.

**Rule:** any request-size, timeout, or connection symptom must be checked at *every* hop, not just the app. Symptom signature of a proxy-level kill: upload dies mid-transfer, browser may silently retry, and **nothing appears in uvicorn logs** (the request never completes into the app).

**Why:** a 4 GB upload failure took several rounds to diagnose because two independent ceilings existed: (1) the container's `/tmp` was a 2 G RAM tmpfs and FastAPI spools multipart uploads to TMPDIR — fixed by `TMPDIR=/data/tmp` in the Dockerfile + startup mkdir (the /data volume mount hides image-time mkdirs); (2) nginx's default body limit — fixed by the user with `client_max_body_size 100G`, `proxy_request_buffering off`, and 3600 s timeouts.

**How to apply:** for upload/streaming issues in prod, bisect with (a) `docker logs` — no POST logged means the request died before the app; (b) local `curl -F` on the VM against `localhost:8080` to bypass the proxy; (c) check what answers on port 80 of the hostname the browser actually uses. Also remember: an upload transiently needs ~2× its size free on /data (spool + final copy).
