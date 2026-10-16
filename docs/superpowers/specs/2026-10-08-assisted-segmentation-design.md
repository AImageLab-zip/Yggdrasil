# Assisted segmentation (phase 1) — design

Bring back the laparoscopy "Magic Tool": click points on the frame being annotated and a
SAM2-family model running on the GPU worker (`~/Desktop/DjangoWorker`) paints the mask.
Decided with the maintainer on 2026-10-08, one problem at a time.

## Scope

**In:** include/exclude points on the current frame of Annotation Mode, refinement of the
label's existing mask, result written straight into the selected label.

**Out (later, if needed):** propagation across frames, box prompts, SAM3 text prompts,
several users on one video, admission control when RAM runs out, persisting prompt points.

## Architecture

```
browser ──HTTPS──▶ Yggdrasil ──Bearer token, internal network──▶ worker API ─Redis─▶ GPU
browser ◀──WSS─── Yggdrasil relay (Channels) ◀──────WS─────────── worker
```

- The browser only ever talks to Yggdrasil. Yggdrasil authorizes, picks the video, and
  forwards with `ASSIST_WORKER_TOKEN`. The worker's Traefik route (`djangoworker`,
  `/api/session` + `/ws/session` on zip-dgx) stays, because production Yggdrasil runs on
  another machine; every worker request now needs the bearer token.
- The relay follows `common.consumers.LiveTranscriptionConsumer` (Whisper): server-side
  upstream connection, token added server-side, typed close codes.
- Feature is off unless `ASSIST_WORKER_URL` and `ASSIST_WORKER_TOKEN` are set, and is
  enabled per project by a new annotation method `assisted_segmentation` (seeded like
  `common/migrations/0061_image_segmentation_method.py`, laparoscopy projects get it).
- Permission: **write** on the patient's own project (`get_patient_for(..., "write")`) —
  annotators and admins. Viewers never start GPU work.

## Session lifecycle

1. The laparoscopy patient page opens → the browser opens the relay WebSocket for that
   patient. Yggdrasil:
   - takes the `fileId` of the video the page is annotating and checks it belongs to that
     patient and is its **subsampled** video (`FileRegistry` `video_processed` /
     `subsampled`); the storage key is looked up server-side, never sent by the browser;
   - opens the worker WS for a new session id `lap-<file id>-<8 hex>` (one per connection,
     so a page reload's late close cannot evict the new session) **before** calling `/ready`
     (the worker's `session_ready` is pub/sub and is lost if nobody is subscribed yet);
   - `POST /api/session/ready/ {video_id, video_source: <storage key>}`.
2. The worker downloads the video from Garage **with its own keys**, loads it, and
   precomputes every frame's embedding (batches on GPU → CPU RAM, as today), then
   `session_ready` is relayed to the browser ("Preparing assistant…" until then).
3. The page closes (or the socket drops) → Yggdrasil sends
   `DELETE /api/session/<id>/`: precompute stops and memory is freed **immediately**.

One user per video is assumed (a second `/ready` on the same video evicts the first,
unchanged worker behaviour). Precompute RAM is ~28 MiB/frame: a 5,608-frame video is
~150 GiB of the host's 251 GiB — one such session fits, two do not (accepted for now).

## Prompting (Annotation Mode)

- New **Assist** tool in the segmentation toolbar, with a **+/−** toggle deciding
  whether the next click is an include or exclude point. Clicking an existing point
  removes it. Right/middle drag keep zoom/pan.
- Points live per (frame, label) in the browser and reset when the frame or label changes.
- Every click sends, over the same relay socket (no separate HTTP endpoint, so permission
  is checked once, when the socket opens), one region for the selected label:
  all its points on this frame (frame pixels, `normalized: false`), plus the label's
  current plane on this frame as `mask_b64` (`bitpack_u1_v1`) when it has any pixels,
  `frame_index` = the frame Annotation Mode is on, window 0.
- The returned mask **replaces** the label's plane on that frame
  (`labelmapStack.writePlane`), as one undoable edit, autosaved like a brush stroke.
- While a request is in flight further clicks are queued (last one wins); worker errors
  show in the existing status line.

## Worker changes (`~/Desktop/DjangoWorker`)

- `Authorization: Bearer <token>` required on every HTTP endpoint and on the WS
  (shared secret from env); `DEBUG` off; exception text no longer sent to clients.
- `video_source` accepts only storage keys of subsampled laparoscopy videos — the local
  path and `http(s)` branches of `worker/video_loader.py` are removed (no SSRF, no
  arbitrary files).
- Prompt accepts an optional `frame_index` (used instead of `int(seconds × fps)`) and a
  window of 0 (just that frame).
- Model: **SurgiSAM2** (`SurgiSAM2/Curated400_checkpoint_26.pt`) first. To verify at the
  start: it loads with the worker's SAM2 video predictor and config; it needs a volume
  mount (`SurgiSAM2/` is in `.dockerignore`). If it does not load, ask before falling back
  to MedSAM2.
- Hard-coded secrets removed from `test.py` / `download.sh` and both added to
  `.dockerignore`. (Rotating the leaked Garage key and HF token is the maintainer's.)
- Deployment: the worker API joins the network Traefik and the dev Yggdrasil share
  (`proxy-net`, alias `djangoworker-api`). Dev Yggdrasil calls it internally; production
  calls `https://zip-dgx.ing.unimore.it` through Traefik.

## Yggdrasil changes

- Settings `ASSIST_WORKER_URL`, `ASSIST_WORKER_TOKEN`; `.env.example` documented.
- Data migration: `assisted_segmentation` annotation method, on laparoscopy projects.
- Relay WebSocket consumer + route (write permission, checked on connect): validates each
  prompt's frame index, points and mask size against the video's probe, forwards it to the
  worker, never forwards a client-supplied key or URL; closes the worker session on
  disconnect.
- Page payload: `canAssist` (method enabled, worker configured, write permission).
- Frontend (`frontend/imaging/segment/`): Assist tool, +/− toggle, point overlay,
  bitpack encode/decode, write-in + undo; bundle rebuilt and committed.
- Tutorial: one step for the Assist tool.

## Testing

- Django: permission matrix (viewer refused, annotator/admin allowed, other project 404),
  the worker is never sent a client-chosen key, relay closes the worker session on
  disconnect, feature hidden when unconfigured — worker mocked.
- JS: bitpack codec round trip (LSB-first, matches `worker/mask_codec.py`), point
  bookkeeping per (frame, label).
- Worker: token required on HTTP and WS, rejected `video_source` forms, `frame_index`
  and zero window.
- Manual: end-to-end on the dev stack with the real worker and GPU.

## Phase 1b — propagation after each click (decided 2026-10-08, after the first E2E)

UI changes already made: Assist has its own card under Labels ("Segment by clicking",
Add / Remove from mask); the toolbar label dropdown is gone; prompt points are dropped once
their mask is painted (the mask, not the old points, steers the next click).

- **Every click propagates forward**: the Assist card has "Propagate over N frames"
  (default 5; 0 = this frame only). The prompt goes to the worker with window N frames
  (`window_seconds = (N + 0.5) / fps`, so the worker's `int(window·fps)` is exactly N).
- **Frames already annotated are kept**: a tracked mask lands only on frames where that
  label is still empty (per the stored manifest); the clicked frame is always updated.
- **Tracked frames are saved right away**, one revision per batch, and undo covers only the
  clicked frame.
- **One writer**: tracked frames go through the page's autosaver (the same revision chain
  as brush strokes, so no 409s between the two). Because a save replaces a frame wholesale,
  a tracked frame is saved as its stored planes plus the tracked label's plane.
