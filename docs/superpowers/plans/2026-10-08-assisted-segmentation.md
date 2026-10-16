# Assisted Segmentation (phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In Annotation Mode, the annotator clicks include/exclude points on the frame and a SAM2-family model on the GPU worker paints the selected label's mask.

**Architecture:** The browser keeps one WebSocket to Yggdrasil (`/ws/assist/<domain>/<patient>/?fileId=`) for the whole page visit. Yggdrasil authorizes it, opens the worker session with a bearer token over the internal Docker network, relays the worker's results, and forwards prompts sent on the same socket. The worker downloads the subsampled video from Garage with its own keys and precomputes every frame's embedding; closing the page closes the worker session at once.

**Tech Stack:** Yggdrasil — Django 5.2, Channels (`AsyncWebsocketConsumer`), `websockets` 15, Cornerstone3D 5.8 (esbuild bundle), `node:test`. Worker (`~/Desktop/DjangoWorker`) — Django 4.2 + DRF, Daphne, Channels, Redis pub/sub, MedSAM2's SAM2 video predictor, pytest.

**Spec:** `docs/superpowers/specs/2026-10-08-assisted-segmentation-design.md`

## Global Constraints

- Two repos: **Yggdrasil** (`/media/raid0/Yggdrasil`, work on a new branch `feat/assisted-segmentation` created from the current HEAD) and the **worker** (`/home/nicola/Desktop/DjangoWorker`, put under git in Task A1).
- Commits are SSH-signed with the maintainer's key and this session has no SSH agent: at every "Commit" step, run `git add`, then `git commit`; if signing fails, stop and ask the maintainer to commit the staged change. Never pass `-c commit.gpgsign=false`.
- The browser never talks to the worker and never names a storage key or URL: Yggdrasil looks the key up from the `fileId` after checking it is **this patient's subsampled video** (`file_type="video_processed"`, `subtype="subsampled"`).
- Permission: **write** on the patient's own project (`user_can_write_patient_annotations`), the project's `assisted_segmentation` annotation method, and both `ASSIST_WORKER_URL` and `ASSIST_WORKER_TOKEN` set. Otherwise the Assist tool is not rendered.
- Worker session id: `lap-<fileId>-<8 hex chars>`, new per relay connection (a page reload's late close must not evict the new session).
- Frames are addressed by `frame_index` (0-based, the subsampled video's), prompts use a window of **0** (that frame only), coordinates in frame pixels (`normalized: false`).
- Mask wire format both ways: `bitpack_u1_v1` — row-major, **LSB-first** within each byte, base64, `mask_shape: [H, W]` (worker `worker/mask_codec.py`, `np.packbits(..., bitorder="little")`).
- Each click sends every point of this (frame, label) plus the label's current plane on this frame when it has pixels; the result **replaces** that label's plane on that frame as one undoable, autosaved edit. Points reset when the frame or the label changes.
- Include/exclude is chosen with a **+/− toggle button** (not Shift-click). Right-drag stays zoom, middle-drag stays pan.
- Precompute starts when the page opens; leaving the page sends `DELETE /api/session/<id>/` immediately.
- Model: SurgiSAM2 (`SurgiSAM2/Curated400_checkpoint_26.pt`) first; if it cannot be loaded or does not fit in RAM, **stop and ask** before falling back to MedSAM2.
- Out of scope: propagation, box prompts, SAM3 text prompts, several users per video, RAM admission control, persisting prompt points.
- CLAUDE.md rules apply in Yggdrasil: `common/` never imports a domain app or `annotations`; touching `frontend/` means `npm run build` and committing `static/vendor/cornerstone/`; no wholesale reformatting.

## Review Focus

1. **Page reload while the worker is still precomputing** — the old socket's `DELETE` arrives after the new socket's `/ready`; the new session must survive. Pinned by `test_each_connection_gets_its_own_worker_session` (Task B3).
2. **A click before the worker is ready** — nothing reaches the worker and the annotator is told why. Pinned by `test_a_prompt_before_the_worker_is_ready_is_refused` (Task B3) and the disabled Assist button (Task B6 manual check).
3. **The annotator changes frame or label while a mask is in flight** — the late mask must not be painted on the new frame/label. Pinned by `maskTarget` tests (Task B4).
4. **A worker mask of another size or orientation** — never written over the frame. Pinned by `unpackMask refuses ...` (Task B4).
5. **The worker dies mid-session** — the browser socket closes with 4502, Assist is disabled and switches back to Brush, painting keeps working. Pinned by `test_a_dead_worker_closes_the_browser_socket` (Task B3) and the Task B7 manual check.

---

## Part A — the worker (`/home/nicola/Desktop/DjangoWorker`)

Run worker tests with the existing image (it is 18 GB and already built; rebuild only when `requirements.txt` or `Dockerfile` change):

```bash
cd /home/nicola/Desktop/DjangoWorker
docker run --rm -v "$PWD":/app -w /app djangoworker-api:latest pytest -q <paths>
```

### Task A1: Secrets out, worker under version control

**Files:**
- Modify: `test.py:4-7`, `download.sh:17`, `.dockerignore`
- Create: `.gitignore`, `medsam2-local.patch`

- [ ] **Step 1: Read the S3 settings from the environment in `test.py`**

```bash
cd /home/nicola/Desktop/DjangoWorker
python3 - <<'EOF'
import re, pathlib
p = pathlib.Path("test.py")
s = p.read_text()
s = re.sub(r'^ENDPOINT_URL = .*$', 'ENDPOINT_URL = os.environ["OBJECT_STORAGE_ENDPOINT_URL"]', s, flags=re.M)
s = re.sub(r'^BUCKET_NAME = .*$', 'BUCKET_NAME = os.environ["OBJECT_STORAGE_BUCKET"]', s, flags=re.M)
s = re.sub(r'^ACCESS_KEY = .*$', 'ACCESS_KEY = os.environ["OBJECT_STORAGE_ACCESS_KEY_ID"]', s, flags=re.M)
s = re.sub(r'^SECRET_KEY = .*$', 'SECRET_KEY = os.environ["OBJECT_STORAGE_SECRET_ACCESS_KEY"]', s, flags=re.M)
s = "import os\n" + s
p.write_text(s)
EOF
sed -n 1,8p test.py
```

Expected: lines 1-8 show `import os` and four `os.environ[...]` lookups, no literal key.

- [ ] **Step 2: Drop the hard-coded token from `download.sh`**

```bash
sed -i "/^HF_TOKEN='hf_/d" download.sh
grep -n "HF_TOKEN" download.sh
```

Expected: only the existing `if [ -z "$HF_TOKEN" ]` check and its uses remain (the script already reads it from the environment).

- [ ] **Step 3: Keep scratch files, notebooks and checkpoints out of the image**

Append to `.dockerignore`:

```
test.py
download.sh
*.ipynb
checkpoints/
```

- [ ] **Step 4: Create `.gitignore`**

```
.env
__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
.code-review-graph/
.claude/
.vscode/
db.sqlite3
*.ipynb
checkpoints/
SurgiSAM2/*.pt
# Upstream clone (bowang-lab/MedSAM2) with its own .git; local edits live in medsam2-local.patch.
MedSAM2/
```

- [ ] **Step 5: Record the local MedSAM2 fork as a patch**

```bash
git -C MedSAM2 diff > medsam2-local.patch
git -C MedSAM2 status --short
```

Expected: the patch touches `sam2/sam2_video_predictor.py`; status also lists the untracked `sam2/configs/sam2.1/` (left in place, mounted at runtime).

- [ ] **Step 6: Initialise the repository and check nothing secret is staged**

```bash
git init -b main
git add -A
git status --short | grep -E '\.env$|\.pt$|\.ipynb$|^.. MedSAM2/' && echo "STOP: something ignored got staged"
git grep -nE "GK[0-9a-f]{20,}|hf_[A-Za-z0-9]{20,}" --cached && echo "STOP: a secret is staged"
```

Expected: neither STOP line prints.

- [ ] **Step 7: Run the worker suite as a baseline**

Run: `docker run --rm -v "$PWD":/app -w /app djangoworker-api:latest pytest -q`
Expected: all tests pass except the integration test, which skips without Docker-in-Docker. Record the counts.

- [ ] **Step 8: Commit**

```bash
git commit -m "chore: worker under version control; no secrets in the tree or the image"
```

Tell the maintainer, once: the Garage key that was in `test.py` (Garage at `brainstorming.ing.unimo.it:3900`, bucket `toothfairy4m`) and the Hugging Face token that was in `download.sh` are in every image built so far and must be rotated.

### Task A2: SurgiSAM2 loads, and fits (decision gate)

**Files:**
- Create: `configs/surgisam2.yaml`

**Interfaces:**
- Produces: `configs/surgisam2.yaml`, the config the worker mounts as `MEDSAM2_CONFIG_PATH` in Task A6, and a recorded per-frame RAM figure.

- [ ] **Step 1: Identify the SAM2 variant from the checkpoint**

```bash
cd /home/nicola/Desktop/DjangoWorker
docker run --rm -i -v "$PWD/SurgiSAM2:/surgi:ro" djangoworker-worker:latest python - <<'EOF'
import torch
ckpt = torch.load("/surgi/Curated400_checkpoint_26.pt", map_location="cpu", weights_only=False)
sd = ckpt["model"] if isinstance(ckpt, dict) and "model" in ckpt else ckpt
print("top-level keys:", list(ckpt)[:8] if isinstance(ckpt, dict) else type(ckpt))
embed = sd["image_encoder.trunk.patch_embed.proj.weight"].shape[0]
blocks = 1 + max(int(k.split(".")[3]) for k in sd if k.startswith("image_encoder.trunk.blocks."))
print("embed_dim", embed, "blocks", blocks)
EOF
```

Map the output: `96/12 → t`, `96/16 → s`, `112/24 → b+`, `144/48 → l`. Anything else: **stop and ask the maintainer**.

- [ ] **Step 2: Write `configs/surgisam2.yaml` for that variant**

The vendored `MedSAM2/sam2/configs/sam2.1/sam2.1_hiera_b+.yaml` is the template; only the trunk and neck differ between variants (values from facebookresearch/sam2's `sam2.1_hiera_{t,s,b+,l}.yaml`). Replace `VARIANT` with Step 1's answer:

```bash
mkdir -p configs
docker run --rm -i --user "$(id -u):$(id -g)" -v "$PWD":/w -w /w djangoworker-worker:latest python - VARIANT <<'EOF'
import sys, yaml
variant = sys.argv[1]
TRUNK = {
    "t": dict(embed_dim=96, num_heads=1, stages=[1, 2, 7, 2], global_att_blocks=[5, 7, 9], window_pos_embed_bkg_spatial_size=[7, 7]),
    "s": dict(embed_dim=96, num_heads=1, stages=[1, 2, 11, 2], global_att_blocks=[7, 10, 13], window_pos_embed_bkg_spatial_size=[7, 7]),
    "b+": dict(embed_dim=112, num_heads=2),
    "l": dict(embed_dim=144, num_heads=2, stages=[2, 6, 36, 4], global_att_blocks=[23, 33, 43], window_pos_embed_bkg_spatial_size=[7, 7], window_spec=[8, 4, 16, 8]),
}
NECK = {"t": [768, 384, 192, 96], "s": [768, 384, 192, 96], "b+": [896, 448, 224, 112], "l": [1152, 576, 288, 144]}
cfg = yaml.safe_load(open("MedSAM2/sam2/configs/sam2.1/sam2.1_hiera_b+.yaml"))
encoder = cfg["model"]["image_encoder"]
encoder["trunk"].update(TRUNK[variant])
encoder["neck"]["backbone_channel_list"] = NECK[variant]
with open("configs/surgisam2.yaml", "w") as out:
    out.write("# @package _global_\n# SurgiSAM2 (Curated400_checkpoint_26.pt): SAM2.1 hiera-" + variant + ".\n\n")
    yaml.safe_dump(cfg, out, sort_keys=False)
EOF
head -3 configs/surgisam2.yaml
```

Expected: the file starts with `# @package _global_` (Hydra needs that header).

- [ ] **Step 3: Load it and measure RAM per frame with precompute**

```bash
docker run --rm -i --gpus '"device=0"' \
  -v "$PWD/SurgiSAM2:/surgi:ro" -v "$PWD/configs:/cfg:ro" \
  -e MEDSAM2_CHECKPOINT_PATH=/surgi/Curated400_checkpoint_26.pt \
  -e MEDSAM2_CONFIG_PATH=/cfg/surgisam2.yaml \
  djangoworker-worker:latest python - <<'EOF'
import subprocess, psutil
subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=30",
                "-t", "4", "-pix_fmt", "yuv420p", "/tmp/clip.mp4"], check=True)
from segmentation.medsam2_video import load_medsam2_video
model = load_medsam2_video("cuda:0")
rss = lambda: psutil.Process().memory_info().rss / 2**20
before = rss()
session = model.init_session_base("/tmp/clip.mp4")
loaded = rss()
model.precompute_session(session)
after = rss()
n = session["total_frames"]
per_frame = (after - before) / n
print(f"frames={n} decode={(loaded - before) / n:.1f} precompute={(after - loaded) / n:.1f} total={per_frame:.1f} MiB/frame")
print(f"projected for the 5,608-frame test video: {per_frame * 5608 / 1024:.0f} GiB (host has 251 GiB)")
EOF
```

Expected: the log line `Loaded MedSAM2 predictor successfully (config=surgisam2, ...)` with **no** missing/unexpected-key error, then the two figures.

- [ ] **Step 4: Gate**

If loading failed, or the projection is above **200 GiB**, **stop and ask the maintainer** (options: MedSAM2 instead; a lower `image_size`; precompute off). Otherwise continue and put the measured MiB/frame in the commit message.

- [ ] **Step 5: Commit**

```bash
git add configs/surgisam2.yaml
git commit -m "feat: SurgiSAM2 config (hiera-VARIANT), loads; precompute ~N MiB/frame"
```

### Task A3: Bearer token on every HTTP and WebSocket request

**Files:**
- Create: `config/auth.py`, `tests/test_auth.py`
- Modify: `config/settings.py`, `config/asgi.py`, `ws/consumers.py`, `tests/conftest.py`, `tests/ws/test_consumer.py`, `tests/test_integration.py`

**Interfaces:**
- Produces: env var `WORKER_API_TOKEN`; requests must carry `Authorization: Bearer <token>`. WebSocket refusal close code **4401**.

- [ ] **Step 1: Write the failing tests** — `tests/test_auth.py`

```python
from unittest.mock import patch

import pytest
from channels.testing import WebsocketCommunicator
from django.test import Client
from django.urls import reverse

from config.asgi import application

pytestmark = pytest.mark.django_db


def _delete(client):
    with patch("api.views.publish_command") as publish:
        response = client.delete(reverse("session-delete", args=["v1"]))
    return response, publish


def test_http_without_a_token_is_401_and_publishes_nothing():
    response, publish = _delete(Client())
    assert response.status_code == 401
    publish.assert_not_called()


def test_http_with_the_wrong_token_is_401():
    response, _ = _delete(Client(HTTP_AUTHORIZATION="Bearer nope"))
    assert response.status_code == 401


def test_http_with_the_token_passes(client):
    response, publish = _delete(client)
    assert response.status_code == 204
    publish.assert_called_once()


def test_an_unset_token_refuses_everything(settings):
    settings.WORKER_API_TOKEN = ""
    response, _ = _delete(Client(HTTP_AUTHORIZATION="Bearer "))
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_a_websocket_without_the_token_is_refused():
    communicator = WebsocketCommunicator(application, "/ws/session/v1/")
    connected, code = await communicator.connect()
    assert not connected
    assert code == 4401
```

- [ ] **Step 2: Give every existing test the token** — append to `tests/conftest.py`

```python
TOKEN = "test-token"


@pytest.fixture(autouse=True)
def worker_api_token(settings):
    settings.WORKER_API_TOKEN = TOKEN


@pytest.fixture
def client():
    """pytest-django's ``client``, carrying the worker's bearer token."""
    from django.test import Client

    return Client(HTTP_AUTHORIZATION=f"Bearer {TOKEN}")


def ws_communicator(path):
    from channels.testing import WebsocketCommunicator
    from config.asgi import application

    return WebsocketCommunicator(
        application, path, headers=[(b"authorization", f"Bearer {TOKEN}".encode())]
    )
```

Then route the existing WebSocket tests through it:

```bash
sed -i "s/WebsocketCommunicator(application, /ws_communicator(/" tests/ws/test_consumer.py tests/test_integration.py
sed -i "0,/^from config.asgi import application/s//from config.asgi import application\nfrom tests.conftest import ws_communicator/" tests/ws/test_consumer.py tests/test_integration.py
grep -n "ws_communicator\|WebsocketCommunicator(" tests/ws/test_consumer.py tests/test_integration.py
```

Expected: three calls in `test_consumer.py` and one in `test_integration.py` use `ws_communicator(`; the import line is present in both files. If a file imports `application` inside a function (the `sed` then adds nothing), add `from tests.conftest import ws_communicator` to its top-level imports by hand.

- [ ] **Step 3: Run to verify the new tests fail**

Run: `docker run --rm -v "$PWD":/app -w /app djangoworker-api:latest pytest -q tests/test_auth.py`
Expected: FAIL — the 401 tests get 204/202 and the WebSocket connects.

- [ ] **Step 4: Implement** — `config/auth.py`

```python
"""The worker is reached only by Yggdrasil's server, with a shared bearer token.

HTTP goes through ``BearerTokenMiddleware``; the WebSocket consumer checks the same token
with ``token_ok`` before it accepts. An unset ``WORKER_API_TOKEN`` refuses everything: a
worker that lost its token must not fall open.
"""

import hmac

from django.conf import settings
from django.http import JsonResponse


def token_ok(header_value):
    token = settings.WORKER_API_TOKEN
    if not token or not header_value:
        return False
    if isinstance(header_value, bytes):
        header_value = header_value.decode("latin-1")
    return hmac.compare_digest(header_value, f"Bearer {token}")


class BearerTokenMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not token_ok(request.headers.get("Authorization")):
            return JsonResponse({"error": "unauthorized"}, status=401)
        return self.get_response(request)
```

`config/settings.py` — `DEBUG` defaults off, CORS goes (no browser calls the worker any more), the middleware goes in:

```python
DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() == "true"
```

```python
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "config.auth.BearerTokenMiddleware",
    "django.middleware.common.CommonMiddleware",
]

WORKER_API_TOKEN = os.environ.get("WORKER_API_TOKEN", "")
```

Delete the `CORS_ALLOW_ALL_ORIGINS = True` line.

`config/asgi.py` — delete the whole `CorsMiddleware` class and route WebSockets straight to the router:

```python
application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": URLRouter(websocket_urlpatterns),
    }
)
```

`ws/consumers.py` — check the token first, and make `disconnect` safe when `connect` refused:

```python
from config.auth import token_ok


class SessionConsumer(AsyncWebsocketConsumer):
    _listener_task = None

    async def connect(self):
        headers = dict(self.scope.get("headers", []))
        if not token_ok(headers.get(b"authorization")):
            await self.close(code=4401)
            return
        self.video_id = self.scope['url_route']['kwargs']['video_id']
        # ...the rest of connect() unchanged...

    async def disconnect(self, close_code):
        if self._listener_task is None:
            return
        # ...the rest of disconnect() unchanged...
```

- [ ] **Step 5: Run the whole suite**

Run: `docker run --rm -v "$PWD":/app -w /app djangoworker-api:latest pytest -q`
Expected: PASS (same counts as the A1 baseline + 5 new).

- [ ] **Step 6: Commit**

```bash
git add config/auth.py config/settings.py config/asgi.py ws/consumers.py tests/
git commit -m "feat: bearer token on HTTP and WebSocket; DEBUG off by default; no CORS"
```

### Task A4: Only subsampled laparoscopy videos; no exception text to clients

**Files:**
- Modify: `worker/video_loader.py`, `worker/redis_handler.py:146,229`
- Test: `tests/worker/test_video_loader.py`, `tests/worker/test_redis_handler.py`

**Interfaces:**
- Produces: `fetch_video(key)` accepts only `^(?:[A-Za-z0-9_.-]+/)*laparoscopy/processed/video/job_\d+/subsampled\.mp4$` without `..` segments; error messages published to clients are exactly `session_expired`, `load_failed` or `inference_failed`.

- [ ] **Step 1: Write the failing tests**

In `tests/worker/test_video_loader.py`, delete `test_fetch_video_local_path_returned_as_is` and `test_fetch_video_bare_key_delegates_to_object_storage`, and add:

```python
KEY = "laparoscopy/processed/video/job_12/subsampled.mp4"


def test_a_subsampled_laparoscopy_key_is_downloaded():
    with patch("worker.video_loader.object_storage.download_to_tempfile", return_value="/tmp/x.mp4") as download:
        assert fetch_video(KEY) == "/tmp/x.mp4"
    download.assert_called_once_with(KEY)


def test_a_key_under_a_storage_prefix_is_accepted():
    with patch("worker.video_loader.object_storage.download_to_tempfile", return_value="/tmp/x.mp4"):
        assert fetch_video("prod/" + KEY) == "/tmp/x.mp4"


@pytest.mark.parametrize("source", [
    "/etc/passwd",
    "/dataset/anything.mp4",
    "http://169.254.169.254/latest/meta-data",
    "https://example.com/video.mp4",
    "maxillo/processed/video/job_1/subsampled.mp4",
    "laparoscopy/processed/video/job_1/compressed.mp4",
    "laparoscopy/processed/video/job_1/../../../secret/subsampled.mp4",
    "../laparoscopy/processed/video/job_1/subsampled.mp4",
])
def test_anything_else_is_refused(source):
    with patch("worker.video_loader.object_storage.download_to_tempfile") as download:
        with pytest.raises(ValueError):
            fetch_video(source)
    download.assert_not_called()
```

Append to `tests/worker/test_redis_handler.py`:

```python
@pytest.mark.asyncio
async def test_a_failed_prompt_tells_the_client_nothing_about_why(handler, mock_store, mock_redis):
    mock_store.get.return_value = {"gpu_id": 0, "fps": 25.0, "total_frames": 10, "inference_state": {}}
    handler._precompute = MagicMock(request_cancel=MagicMock(return_value=None))
    handler._run_inference_streaming = AsyncMock(side_effect=RuntimeError("/app/secret/path exploded"))
    await handler.handle("v1", {"type": "prompt", "video_id": "v1", "job_id": "j1", "frame_index": 0,
                                "window_seconds": 0, "regions": []})
    await asyncio.sleep(0.05)
    published = json.loads(mock_redis.publish.call_args[0][1])
    assert published == {"type": "error", "message": "inference_failed"}
```

- [ ] **Step 2: Run to verify they fail**

Run: `docker run --rm -v "$PWD":/app -w /app djangoworker-api:latest pytest -q tests/worker/test_video_loader.py tests/worker/test_redis_handler.py`
Expected: FAIL — local paths and URLs are still fetched; the error message is the exception text.

- [ ] **Step 3: Implement** — replace `worker/video_loader.py` from the imports through `_download_http` with:

```python
import re

from worker import object_storage

# The only videos this worker loads: Yggdrasil's subsampled laparoscopy derivatives, under
# any storage prefix. Not a local path, not a URL, not another key in the bucket -- a
# worker that fetches whatever it is told is a file-read and SSRF primitive.
ALLOWED_KEY = re.compile(r"^(?:[A-Za-z0-9_.-]+/)*laparoscopy/processed/video/job_\d+/subsampled\.mp4$")


def fetch_video(video_source: str) -> str:
    """Download one subsampled laparoscopy video from object storage; return its local path."""
    if ".." in video_source.split("/") or not ALLOWED_KEY.match(video_source):
        raise ValueError("video_source is not a subsampled laparoscopy video key")
    return object_storage.download_to_tempfile(video_source)
```

Keep `get_video_fps` as it is.

In `worker/redis_handler.py`, the load failure (line 146) and the prompt failure (line 229) publish fixed messages; the exception is still logged by the `log.exception` calls already there:

```python
                await self._publish(video_id, {"type": "error", "message": "load_failed"})
```

```python
                        await self._publish(
                            video_id, {"type": "error", "message": "inference_failed"}
                        )
```

- [ ] **Step 4: Run the whole suite**

Run: `docker run --rm -v "$PWD":/app -w /app djangoworker-api:latest pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add worker/video_loader.py worker/redis_handler.py tests/worker/
git commit -m "fix: load only subsampled laparoscopy keys; never send exception text to clients"
```

### Task A5: `frame_index` and a zero window

**Files:**
- Modify: `api/serializers.py` (`SessionPromptSerializer`), `api/views.py` (`SessionPromptView.post`), `worker/inference_runner.py:46-48`
- Test: `tests/api/test_prompt.py`, `tests/worker/test_inference.py`

**Interfaces:**
- Produces: `POST /api/session/prompt/` accepts `frame_index` (int ≥ 0) instead of, or besides, `frame_timestamp` (one of the two required) and `window_seconds` ≥ 0. The published command carries both keys (`None` when absent); `frame_index` wins.

- [ ] **Step 1: Write the failing tests**

Append to `tests/api/test_prompt.py`:

```python
def test_a_frame_index_and_a_zero_window_are_accepted_and_published(client, video_id):
    payload = {"video_id": video_id, "frame_index": 57, "window_seconds": 0,
               "regions": [{"region_id": "1", "points": [[1.0, 2.0]], "point_labels": [1]}]}
    with patch("api.views.publish_command") as mock_pub:
        response = client.post(reverse("session-prompt"), payload, content_type="application/json")
    assert response.status_code == 202
    cmd = mock_pub.call_args[0][1]
    assert (cmd["frame_index"], cmd["frame_timestamp"], cmd["window_seconds"]) == (57, None, 0.0)


def test_a_prompt_needs_a_frame(client, video_id):
    payload = {"video_id": video_id, "points": [[1.0, 2.0]], "point_labels": [1]}
    with patch("api.views.publish_command"):
        response = client.post(reverse("session-prompt"), payload, content_type="application/json")
    assert response.status_code == 400
```

Append to `tests/worker/test_inference.py`:

```python
from unittest.mock import MagicMock

from worker.inference_runner import InferenceRunner
from worker.mask_codec import MaskCodec


def test_frame_index_wins_over_seconds_and_a_zero_window_is_one_frame():
    model = MagicMock()
    model.propagate_regions.return_value = iter(
        [(57, [{"mask": np.ones((2, 3), np.uint8), "region_id": "1"}])]
    )
    gpu = MagicMock()
    gpu.get_model.return_value = model
    session = {"gpu_id": 0, "fps": 100.0, "total_frames": 200, "inference_state": {}}
    # 0.57 s x 100 fps floors to 56: the bug frame_index exists to avoid.
    cmd = {"frame_index": 57, "frame_timestamp": 0.57, "window_seconds": 0,
           "regions": [{"region_id": "1", "points": [[1.0, 1.0]], "point_labels": [1]}]}
    results = list(InferenceRunner(gpu, MaskCodec()).run("v", session, cmd))
    model.propagate_regions.assert_called_once_with({}, 57, 57)
    assert [result.frame_index for result in results] == [57]
```

- [ ] **Step 2: Run to verify they fail**

Run: `docker run --rm -v "$PWD":/app -w /app djangoworker-api:latest pytest -q tests/api/test_prompt.py tests/worker/test_inference.py`
Expected: FAIL — `frame_timestamp` is required, `window_seconds` min is 0.1, the runner uses 56.

- [ ] **Step 3: Implement**

`api/serializers.py`, in `SessionPromptSerializer`:

```python
    frame_timestamp = serializers.FloatField(min_value=0.0, required=False)
    frame_index = serializers.IntegerField(min_value=0, required=False)
```

```python
    window_seconds = serializers.FloatField(min_value=0.0, default=5.0)
```

and as the first lines of its `validate`:

```python
        if "frame_index" not in attrs and "frame_timestamp" not in attrs:
            raise serializers.ValidationError("frame_index or frame_timestamp is required")
```

`api/views.py`, in the command published by `SessionPromptView.post`, replace `"frame_timestamp": data["frame_timestamp"],` with:

```python
                "frame_timestamp": data.get("frame_timestamp"),
                "frame_index": data.get("frame_index"),
```

`worker/inference_runner.py`, replace line 47:

```python
        frame_index = cmd.get("frame_index")
        start_frame = (
            int(frame_index) if frame_index is not None else int(cmd["frame_timestamp"] * fps)
        )
```

- [ ] **Step 4: Run the whole suite**

Run: `docker run --rm -v "$PWD":/app -w /app djangoworker-api:latest pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/ worker/inference_runner.py tests/
git commit -m "feat: prompts by frame_index; a zero window is that frame only"
```

### Task A6: Internal-only deployment with SurgiSAM2

**Files:**
- Modify: `docker-compose.yml`, `docker-compose.local.yml`, `Makefile`, `.env.example`, `.env` (local, not committed)
- Delete: `docker-compose.traefik.yml`
- Outside both repos, with the maintainer's OK: `~/Desktop/traefik-zip-dgx/traefik/dynamic/routes.yml`

**Interfaces:**
- Produces: the worker API reachable as `http://djangoworker-api:8000` on the Docker network `${YGG_NETWORK:-proxy-net}` (the dev `web` and `garage` are on `proxy-net`), and nowhere public.

- [ ] **Step 1: Compose** — in `docker-compose.yml`, the `api` service's environment lines for debug and hosts become, plus the token:

```yaml
      DJANGO_DEBUG: ${DJANGO_DEBUG:-false}
      DJANGO_ALLOWED_HOSTS: ${DJANGO_ALLOWED_HOSTS:-localhost,127.0.0.1,api,djangoworker-api}
      WORKER_API_TOKEN: ${WORKER_API_TOKEN:?set WORKER_API_TOKEN in .env}
```

and it joins Yggdrasil's network under a stable name:

```yaml
    networks:
      default: {}
      ygg:
        aliases: [djangoworker-api]
```

In the `worker` service replace `- app-net-zip-dgx` with `- ygg`, and replace the top-level `networks:` block with:

```yaml
networks:
  # The network Yggdrasil's web container and Garage are on:
  # proxy-net on the dev machine, app-net-<suffix> in production.
  ygg:
    external: true
    name: ${YGG_NETWORK:-proxy-net}
```

- [ ] **Step 2: Nothing published on the host** — in `docker-compose.local.yml` delete the `api:` block (its `8000:8000` collides with Yggdrasil's dev server and is no longer needed); keep the `redis` port.

- [ ] **Step 3: Remove the Traefik overlay** — `git rm docker-compose.traefik.yml`, and in `Makefile` delete `COMPOSE_TRAEFIK_FILE`, `DCT`, the `build-traefik`, `up-traefik`, `up-traefik-build`, `down-traefik` targets, their four `help` lines and their names in `.PHONY`.

- [ ] **Step 4: Document the new variables** — append to `.env.example`:

```
# Shared with Yggdrasil's ASSIST_WORKER_TOKEN. Required: the API refuses everything without it.
WORKER_API_TOKEN=
# Docker network shared with Yggdrasil (proxy-net on the dev machine).
YGG_NETWORK=proxy-net
# SurgiSAM2 (see configs/surgisam2.yaml)
# MEDSAM2_CHECKPOINT_HOST_PATH=./SurgiSAM2/Curated400_checkpoint_26.pt
# MEDSAM2_CONFIG_HOST_PATH=./configs/surgisam2.yaml
# MEDSAM2_CHECKPOINT_PATH=/app/medsam2-assets/Curated400_checkpoint_26.pt
# MEDSAM2_CONFIG_PATH=/app/medsam2-assets/surgisam2.yaml
# Object storage keys are Yggdrasil's FileRegistry.file_path. If Yggdrasil has an
# OBJECT_STORAGE_KEY_PREFIX, set this to that prefix followed by "/".
```

- [ ] **Step 5: Local `.env` for the dev machine** (not committed). Generate the token once and keep it for Task B7:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Set in `/home/nicola/Desktop/DjangoWorker/.env`: `WORKER_API_TOKEN=<that value>`, `YGG_NETWORK=proxy-net`, `DJANGO_DEBUG=false`, the four SurgiSAM2 `MEDSAM2_*` lines from Step 4 uncommented, and object storage pointing at the **dev** Garage with the dev credentials from Yggdrasil's `docker-compose.dev.yml` (`web` service): `OBJECT_STORAGE_ENDPOINT_URL=http://garage:3900`, `OBJECT_STORAGE_REGION=garage`, `OBJECT_STORAGE_ACCESS_KEY_ID` / `OBJECT_STORAGE_SECRET_ACCESS_KEY` = the dev pair, `OBJECT_STORAGE_BUCKET=yggdrasil`, `OBJECT_STORAGE_KEY_PREFIX=` (empty), `OBJECT_STORAGE_ADDRESSING_STYLE=path`, `OBJECT_STORAGE_VERIFY_SSL=false`.

- [ ] **Step 6: Bring it up and check it is private**

```bash
make build && make up
docker compose -p djangoworker ps
# From Yggdrasil's web container, which is on proxy-net:
cd /media/raid0/Yggdrasil && docker compose exec -T web python -c "
import urllib.request as u
try:
    u.urlopen(u.Request('http://djangoworker-api:8000/api/session/x/', method='DELETE'), timeout=5)
except Exception as exc:
    print(exc)"
```

Expected: `api`, `worker`, `redis` up; the check prints `HTTP Error 401: Unauthorized` (reachable on the network, refused without the token).

- [ ] **Step 7: Remove the public route** — ask the maintainer first (their infrastructure). Then in `~/Desktop/traefik-zip-dgx/traefik/dynamic/routes.yml` delete the `djangoworker:` router block and the `djangoworker-svc:` service block, and check:

Run: `curl -s -o /dev/null -w '%{http_code}\n' https://zip-dgx.ing.unimore.it/api/session/ready/`
Expected: `404` (Yggdrasil's catch-all), no longer `502`.

- [ ] **Step 8: Commit** (worker repo)

```bash
git add docker-compose.yml docker-compose.local.yml Makefile .env.example
git commit -m "chore: worker API internal-only on Yggdrasil's network; SurgiSAM2 settings"
```

---

## Part B — Yggdrasil (`/media/raid0/Yggdrasil`)

Create the branch first: `git switch -c feat/assisted-segmentation`.

Django tests run without MySQL as `docker compose exec -T web python manage.py test <labels> --settings=yggdrasil.settings_sqlite_test`; the migration task also runs against MySQL. JS tests: `npm test`.

### Task B1: Settings, the annotation method, the page flag

**Files:**
- Create: `common/assist.py` (constants and `is_configured` only; Task B2 fills it in), `common/migrations/0063_assisted_segmentation_method.py`
- Modify: `yggdrasil/settings.py` (after `WHISPER_CONNECT_TIMEOUT`, line 106), `.env.example`, `laparoscopy/management/commands/setup_laparoscopy_modalities.py`, `maxillo/views/patient_detail.py` (imports; the `_image_segment_payload` call at ~869; the function at ~925)
- Test: `laparoscopy/tests.py`

**Interfaces:**
- Produces: `common.assist.METHOD_SLUG = "assisted_segmentation"`, `common.assist.is_configured() -> bool`; settings `ASSIST_WORKER_URL`, `ASSIST_WORKER_TOKEN`, `ASSIST_CONNECT_TIMEOUT`; payload key `imageSegmentData.canAssist: bool`.

- [ ] **Step 1: Write the failing tests** — in `laparoscopy/tests.py` add `from django.test import TestCase, override_settings` (replacing the plain `TestCase` import), decorate the existing `test_annotation_mode_needs_both_the_method_and_a_recorded_probe` with `@override_settings(ASSIST_WORKER_URL="")` and add `"canAssist": False,` after `"canManageLabels": True,` in its expected dict. Then add:

```python
    @override_settings(ASSIST_WORKER_URL="http://djangoworker-api:8000", ASSIST_WORKER_TOKEN="t")
    def test_assist_needs_the_method_a_configured_worker_and_write_access(self):
        from common.models import AnnotationMethod

        self._video(**self.PROBE)
        self._subsampled_video(**self.PROBE)
        self._enable()
        method = AnnotationMethod.objects.get(slug="assisted_segmentation")
        flag = lambda: self._get().context["image_segment_data"]["canAssist"]  # noqa: E731

        self.assertFalse(flag(), "method not enabled on the project")
        self.project.annotation_methods.add(method)
        response = self._get()
        self.assertTrue(response.context["image_segment_data"]["canAssist"])
        self.assertContains(response, 'data-segment-tool="assist"')
        with self.settings(ASSIST_WORKER_TOKEN=""):
            self.assertFalse(flag(), "no token, no worker")
        ProjectAccess.objects.filter(user=self.user).update(role="viewer")
        self.assertFalse(flag(), "viewers never start GPU work")
        self.assertNotContains(self._get(), 'data-segment-tool="assist"')
```

- [ ] **Step 2: Run to verify it fails**

Run: `docker compose exec -T web python manage.py test laparoscopy --settings=yggdrasil.settings_sqlite_test`
Expected: FAIL — `KeyError: 'canAssist'` and no `assisted_segmentation` method.

- [ ] **Step 3: Implement**

`yggdrasil/settings.py`, after `WHISPER_CONNECT_TIMEOUT`:

```python
# Assisted segmentation: the GPU worker (~/Desktop/DjangoWorker), reached only from the
# server (common.consumers.AssistSessionConsumer). Empty hides the Assist tool.
ASSIST_WORKER_URL = config("ASSIST_WORKER_URL", default="")
ASSIST_WORKER_TOKEN = config("ASSIST_WORKER_TOKEN", default="")
ASSIST_CONNECT_TIMEOUT = config("ASSIST_CONNECT_TIMEOUT", default=10, cast=int)
```

`.env.example`, after the Whisper block:

```
# Assisted segmentation (GPU worker). Server-side only; leave empty to hide the Assist tool.
# ASSIST_WORKER_TOKEN is the worker's WORKER_API_TOKEN.
ASSIST_WORKER_URL=
ASSIST_WORKER_TOKEN=
# ASSIST_CONNECT_TIMEOUT=10
```

`common/assist.py`:

```python
"""Assisted segmentation: the GPU worker (``~/Desktop/DjangoWorker``) seen from Yggdrasil.

The browser never talks to the worker. ``common.consumers.AssistSessionConsumer`` holds the
browser's WebSocket for the page visit; everything the worker is told goes through here.
"""

from django.conf import settings

METHOD_SLUG = "assisted_segmentation"


def is_configured():
    return bool(settings.ASSIST_WORKER_URL and settings.ASSIST_WORKER_TOKEN)
```

`common/migrations/0063_assisted_segmentation_method.py`:

```python
"""Register the ``assisted_segmentation`` annotation method and enable it on laparoscopy projects.

The Assist tool is gated on it (and on ``ASSIST_WORKER_URL``/``ASSIST_WORKER_TOKEN``); a fresh
project gets it from ``setup_laparoscopy_modalities``.
"""

from django.db import migrations

SLUG = "assisted_segmentation"


def forwards(apps, schema_editor):
    AnnotationMethod = apps.get_model("common", "AnnotationMethod")
    Project = apps.get_model("common", "Project")
    method, _created = AnnotationMethod.objects.get_or_create(
        slug=SLUG,
        defaults={
            "name": "Assisted Segmentation",
            "description": "Masks from clicked points, painted by a model on the GPU worker.",
            "icon": "fas fa-wand-magic-sparkles",
            "domain": "laparoscopy",
            "is_active": True,
        },
    )
    for project in Project.objects.filter(domain="laparoscopy"):
        project.annotation_methods.add(method)


class Migration(migrations.Migration):

    dependencies = [
        ("common", "0062_userpreference_label_colors"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
```

`laparoscopy/management/commands/setup_laparoscopy_modalities.py`, right after the `image_segmentation` block:

```python
        assist, _ = AnnotationMethod.objects.get_or_create(
            slug='assisted_segmentation',
            defaults={
                'name': 'Assisted Segmentation',
                'description': 'Masks from clicked points, painted by a model on the GPU worker.',
                'icon': 'fas fa-wand-magic-sparkles',
                'domain': 'laparoscopy',
            },
        )
        if project_created:
            project.annotation_methods.add(assist)
```

`maxillo/views/patient_detail.py`: add `from common import assist` to the imports; the call becomes

```python
        context['image_segment_data'] = _image_segment_payload(
            get_namespace(request), patient, annotation_video, can_modify,
            can_manage_labels=user_is_project_admin(request.user, patient.project),
            can_assist=bool(
                can_modify
                and assist.METHOD_SLUG in allowed_annotations
                and assist.is_configured()
            ),
        )
```

the signature `def _image_segment_payload(namespace, patient, video_file, can_modify, can_manage_labels=False, can_assist=False):` and the payload gains `'canAssist': bool(can_assist),` after `'canManageLabels'`.

The template part of the test (`data-segment-tool="assist"`) is satisfied in Task B6; until then expect only that assertion to fail. To keep this task green on its own, add the button now in `templates/laparoscopy/patient_detail_content.html`, inside the `Segmentation tools` group after the Polygon button:

```html
                        {% if image_segment_data.canAssist %}
                        <button type="button" class="btn btn-outline-light" data-segment-tool="assist" disabled
                                title="Preparing the assistant…">
                            <i class="fas fa-wand-magic-sparkles"></i> Assist
                        </button>
                        {% endif %}
```

- [ ] **Step 4: Run the tests (SQLite, then MySQL for the migration)**

Run: `docker compose exec -T web python manage.py test laparoscopy common.tests_cross_project_matrix --settings=yggdrasil.settings_sqlite_test`
Run: `docker compose exec -T web sh -c 'DB_USER=root DB_PASSWORD="$MYSQL_ROOT_PASSWORD" python manage.py test laparoscopy --noinput'`
Expected: PASS both.

- [ ] **Step 5: Commit**

```bash
git add yggdrasil/settings.py .env.example common/assist.py common/migrations/0063_assisted_segmentation_method.py \
        laparoscopy/management/commands/setup_laparoscopy_modalities.py maxillo/views/patient_detail.py \
        templates/laparoscopy/patient_detail_content.html laparoscopy/tests.py
git commit -m "feat(assist): settings, the assisted_segmentation method, canAssist on the page"
```

### Task B2: What Yggdrasil tells the worker

**Files:**
- Modify: `common/assist.py`
- Test: `common/tests_assist.py` (new)

**Interfaces:**
- Consumes: `METHOD_SLUG`, `is_configured()` (B1).
- Produces:
  - `class PromptError(ValueError)`
  - `new_session_id(file_id: int) -> str` → `"lap-<id>-<8 hex>"`
  - `upstream_ws_url(session_id: str) -> str` → `ws(s)://<worker>/ws/session/<id>/`
  - `auth_headers() -> dict` → `{"Authorization": "Bearer <token>"}`
  - `worker_request(method: str, path: str, body: dict | None = None) -> int` (blocking; raises on HTTP errors)
  - `probe_of(file_obj) -> {"width", "height", "frame_count"} | None`
  - `prompt_body(session_id: str, message: dict, probe: dict) -> dict` (raises `PromptError`)

- [ ] **Step 1: Write the failing tests** — `common/tests_assist.py`

```python
import base64

from django.test import SimpleTestCase, override_settings

from common import assist

PROBE = {"width": 16, "height": 8, "frame_count": 100}


class PromptBodyTests(SimpleTestCase):
    def body(self, **message):
        base = {"frameIndex": 3, "points": [[2, 1]], "labels": [1]}
        return assist.prompt_body("lap-1-0a1b2c3d", {**base, **message}, PROBE)

    def test_a_click_becomes_one_region_on_that_frame_only(self):
        self.assertEqual(
            self.body(),
            {
                "video_id": "lap-1-0a1b2c3d",
                "frame_index": 3,
                "window_seconds": 0,
                "normalized": False,
                "regions": [{"region_id": "1", "points": [[2.0, 1.0]], "point_labels": [1]}],
            },
        )

    def test_the_current_mask_rides_along_for_refinement(self):
        mask = base64.b64encode(bytes(16)).decode()  # 16 x 8 pixels = 16 bytes
        region = self.body(mask=mask)["regions"][0]
        self.assertEqual(
            (region["mask_b64"], region["mask_encoding"], region["mask_shape"]),
            (mask, "bitpack_u1_v1", [8, 16]),
        )

    def test_what_the_worker_must_not_see_is_refused(self):
        for message in (
            {"frameIndex": 100}, {"frameIndex": -1}, {"frameIndex": "3"}, {"frameIndex": True},
            {"points": []}, {"points": [[0, 0]] * 65, "labels": [1] * 65},
            {"points": [[16, 0]]}, {"points": [[0, 8]]}, {"points": [[-1, 0]]}, {"points": [[1]]},
            {"points": [["1", 1]]}, {"labels": [2]}, {"labels": [1, 0]}, {"labels": [True]},
            {"mask": "not base64!"}, {"mask": base64.b64encode(bytes(15)).decode()},
        ):
            with self.subTest(message=message), self.assertRaises(assist.PromptError):
                self.body(**message)


class WorkerAddressTests(SimpleTestCase):
    def test_each_session_id_is_new_and_names_the_file(self):
        first, second = assist.new_session_id(7), assist.new_session_id(7)
        self.assertRegex(first, r"^lap-7-[0-9a-f]{8}$")
        self.assertNotEqual(first, second)

    @override_settings(ASSIST_WORKER_URL="https://worker.example:8443/", ASSIST_WORKER_TOKEN="s3cret")
    def test_the_socket_follows_the_http_scheme_and_carries_the_token(self):
        self.assertEqual(
            assist.upstream_ws_url("lap-7-0a1b2c3d"),
            "wss://worker.example:8443/ws/session/lap-7-0a1b2c3d/",
        )
        self.assertEqual(assist.auth_headers(), {"Authorization": "Bearer s3cret"})

    def test_a_probe_must_be_complete(self):
        class File:
            def __init__(self, metadata):
                self.metadata = metadata

        good = {"probe": {"width": 16, "height": 8, "fps": 25.0, "frame_count": 100}}
        self.assertEqual(assist.probe_of(File(good)), PROBE)
        for metadata in (None, {}, {"probe": {"width": 16}}, {"probe": {**good["probe"], "width": 0}}):
            with self.subTest(metadata=metadata):
                self.assertIsNone(assist.probe_of(File(metadata)))
```

- [ ] **Step 2: Run to verify they fail**

Run: `docker compose exec -T web python manage.py test common.tests_assist --settings=yggdrasil.settings_sqlite_test`
Expected: FAIL — `AttributeError: module 'common.assist' has no attribute 'prompt_body'`.

- [ ] **Step 3: Implement** — append to `common/assist.py`

```python
import base64
import binascii
import json
import math
import secrets
import urllib.request

MAX_POINTS = 64


class PromptError(ValueError):
    """A prompt from the browser that the worker must not see."""


def new_session_id(file_id):
    # New per relay connection: a page reload's late DELETE must not evict the new session.
    return f"lap-{file_id}-{secrets.token_hex(4)}"


def _base_url():
    return settings.ASSIST_WORKER_URL.rstrip("/")


def upstream_ws_url(session_id):
    scheme, rest = _base_url().split("://", 1)
    return f"{'wss' if scheme == 'https' else 'ws'}://{rest}/ws/session/{session_id}/"


def auth_headers():
    return {"Authorization": f"Bearer {settings.ASSIST_WORKER_TOKEN}"}


def worker_request(method, path, body=None):
    """One call to the worker's HTTP API. Blocking: from async code, run it in a thread."""
    request = urllib.request.Request(
        _base_url() + path,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={**auth_headers(), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=settings.ASSIST_CONNECT_TIMEOUT) as response:
        return response.status


def probe_of(file_obj):
    """Width, height and frame count of a video with a recorded probe, else ``None``."""
    probe = (file_obj.metadata or {}).get("probe") or {}
    try:
        width, height = int(probe["width"]), int(probe["height"])
        frame_count = int(probe["frame_count"])
    except (KeyError, TypeError, ValueError):
        return None
    if min(width, height, frame_count) <= 0:
        return None
    return {"width": width, "height": height, "frame_count": frame_count}


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def prompt_body(session_id, message, probe):
    """The worker's ``/api/session/prompt/`` body for one browser prompt.

    Checked against the video's own probe: a frame of this video, points inside the frame,
    a mask of exactly this frame's size. Window 0 is the prompted frame only.
    """
    frame_index = message.get("frameIndex")
    if type(frame_index) is not int or not 0 <= frame_index < probe["frame_count"]:
        raise PromptError("That is not a frame of this video.")
    points, labels = message.get("points"), message.get("labels")
    if not isinstance(points, list) or not 1 <= len(points) <= MAX_POINTS:
        raise PromptError(f"Send between 1 and {MAX_POINTS} points.")
    if (
        not isinstance(labels, list)
        or len(labels) != len(points)
        or any(type(label) is not int or label not in (0, 1) for label in labels)
    ):
        raise PromptError("Each point needs an include (1) or exclude (0) label.")
    for point in points:
        if (
            not isinstance(point, list)
            or len(point) != 2
            or not all(_is_number(value) for value in point)
            or not (0 <= point[0] < probe["width"] and 0 <= point[1] < probe["height"])
        ):
            raise PromptError("A point lies outside the frame.")
    region = {
        "region_id": "1",
        "points": [[float(x), float(y)] for x, y in points],
        "point_labels": labels,
    }
    mask = message.get("mask")
    if mask is not None:
        try:
            raw = base64.b64decode(mask, validate=True)
        except (TypeError, ValueError, binascii.Error):
            raise PromptError("The mask is not base64.")
        if len(raw) != math.ceil(probe["width"] * probe["height"] / 8):
            raise PromptError("The mask is not this frame's size.")
        region.update(
            mask_b64=mask,
            mask_encoding="bitpack_u1_v1",
            mask_shape=[probe["height"], probe["width"]],
        )
    return {
        "video_id": session_id,
        "frame_index": frame_index,
        "window_seconds": 0,
        "normalized": False,
        "regions": [region],
    }
```

Move the new imports to the top of the module next to `from django.conf import settings`.

- [ ] **Step 4: Run the tests**

Run: `docker compose exec -T web python manage.py test common.tests_assist --settings=yggdrasil.settings_sqlite_test`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add common/assist.py common/tests_assist.py
git commit -m "feat(assist): prompt validation against the probe, and the worker client"
```

### Task B3: The relay — `AssistSessionConsumer`

**Files:**
- Modify: `common/consumers.py`, `common/routing.py`
- Test: `common/tests_assist.py`

**Interfaces:**
- Consumes: everything B2 produces.
- Produces: WebSocket `ws/assist/<domain>/<int:patient_id>/?fileId=<int>`.
  - Close codes before accept: 4400 bad `fileId`, 4401 not logged in, 4403 no write access or method off, 4404 no such patient / not its subsampled video / no probe, 4503 worker not configured, 4502 worker unreachable.
  - Server → browser: `{"type":"preparing"}`, `{"type":"ready"}`, `{"type":"mask","frameIndex":int,"mask":str,"shape":[H,W]}`, `{"type":"done"}`, `{"type":"error","message":str}`; closes 4502 when the worker goes away.
  - Browser → server: `{"type":"prompt","frameIndex":int,"points":[[x,y],...],"labels":[0|1,...],"mask"?:str}`.

- [ ] **Step 1: Write the failing tests** — append to `common/tests_assist.py`

```python
import asyncio
import json
from unittest import mock

from channels.db import database_sync_to_async
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.apps import apps
from django.contrib.auth.models import AnonymousUser, User
from django.test import TransactionTestCase

from common.models import AnnotationMethod, FileRegistry, Project, ProjectAccess
from common.routing import websocket_urlpatterns

KEY = "laparoscopy/processed/video/job_7/subsampled.mp4"


class FakeWorkerSocket:
    def __init__(self):
        self.messages = asyncio.Queue()
        self.closed = False

    async def push(self, message):
        await self.messages.put(json.dumps(message))

    async def close(self):
        self.closed = True
        await self.messages.put(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.messages.get()
        if message is None:
            raise StopAsyncIteration
        return message


async def _until(predicate):
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


@override_settings(ASSIST_WORKER_URL="http://djangoworker-api:8000", ASSIST_WORKER_TOKEN="s3cret")
class AssistSessionConsumerTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        Patient = apps.get_model("laparoscopy", "Patient")
        Folder = apps.get_model("laparoscopy", "Folder")
        self.project = Project.objects.create(slug="lap-assist", name="Lap assist", domain="laparoscopy")
        self.method, _ = AnnotationMethod.objects.get_or_create(
            slug=assist.METHOD_SLUG, defaults={"name": "Assisted Segmentation", "domain": "laparoscopy"}
        )
        self.project.annotation_methods.add(self.method)
        self.user = User.objects.create_user(username="annotator", password="x")
        ProjectAccess.objects.create(user=self.user, project=self.project, role="annotator")
        folder = Folder.objects.create(name="F", project=self.project)
        self.patient = Patient.objects.create(name="P", folder=folder, project=self.project)
        self.video = FileRegistry.objects.create(
            domain="laparoscopy", file_type="video_processed", subtype="subsampled",
            laparoscopy_patient=self.patient, file_path=KEY, file_size=1, file_hash="a" * 64,
            metadata={"probe": {"width": 16, "height": 8, "fps": 25.0, "frame_count": 100}},
        )
        self.upstreams = []
        self.calls = []

        async def connect(url, **kwargs):
            socket = FakeWorkerSocket()
            socket.url, socket.kwargs = url, kwargs
            self.upstreams.append(socket)
            return socket

        for patcher in (
            mock.patch("common.consumers.websockets.connect", new=connect),
            mock.patch("common.assist.worker_request", side_effect=lambda *call: self.calls.append(call) or 202),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def communicator(self, user=None, file_id=None):
        communicator = WebsocketCommunicator(
            URLRouter(websocket_urlpatterns),
            f"/ws/assist/laparoscopy/{self.patient.patient_id}/?fileId={file_id or self.video.pk}",
        )
        communicator.scope["user"] = user or self.user
        return communicator

    async def connected(self):
        communicator = self.communicator()
        ok, _ = await communicator.connect()
        self.assertTrue(ok)
        self.assertEqual(await communicator.receive_json_from(), {"type": "preparing"})
        return communicator

    async def refused(self, communicator):
        ok, code = await communicator.connect()
        self.assertFalse(ok)
        return code

    async def test_refuses_anonymous(self):
        self.assertEqual(await self.refused(self.communicator(AnonymousUser())), 4401)

    async def test_refuses_a_viewer(self):
        await database_sync_to_async(ProjectAccess.objects.filter(user=self.user).update)(role="viewer")
        self.assertEqual(await self.refused(self.communicator()), 4403)

    async def test_refuses_when_the_project_has_the_method_off(self):
        await database_sync_to_async(self.project.annotation_methods.remove)(self.method)
        self.assertEqual(await self.refused(self.communicator()), 4403)

    async def test_refuses_when_no_worker_is_configured(self):
        with self.settings(ASSIST_WORKER_TOKEN=""):
            self.assertEqual(await self.refused(self.communicator()), 4503)

    async def test_refuses_a_file_that_is_not_this_patients_subsampled_video(self):
        raw = await database_sync_to_async(FileRegistry.objects.create)(
            domain="laparoscopy", file_type="video_raw", laparoscopy_patient=self.patient,
            file_path="laparoscopy/raw/case.mp4", file_size=1, file_hash="b" * 64,
        )
        self.assertEqual(await self.refused(self.communicator(file_id=raw.pk)), 4404)
        self.assertEqual(await self.refused(self.communicator(file_id=999999)), 4404)
        self.assertEqual(self.calls, [], "the worker heard nothing")

    async def test_opens_a_worker_session_on_the_patients_own_video_and_closes_it_on_leave(self):
        communicator = await self.connected()
        method, path, body = self.calls[0]
        self.assertEqual((method, path, body["video_source"]), ("POST", "/api/session/ready/", KEY))
        session_id = body["video_id"]
        self.assertRegex(session_id, rf"^lap-{self.video.pk}-[0-9a-f]{{8}}$")
        upstream = self.upstreams[0]
        self.assertEqual(upstream.url, f"ws://djangoworker-api:8000/ws/session/{session_id}/")
        self.assertEqual(upstream.kwargs["additional_headers"], {"Authorization": "Bearer s3cret"})

        await communicator.disconnect()
        await _until(lambda: self.calls[-1][:2] == ("DELETE", f"/api/session/{session_id}/"))
        self.assertTrue(upstream.closed)

    async def test_each_connection_gets_its_own_worker_session(self):
        first = await self.connected()
        await first.disconnect()
        second = await self.connected()
        ready = [call[2]["video_id"] for call in self.calls if call[1] == "/api/session/ready/"]
        self.assertEqual(len(set(ready)), 2)
        await second.disconnect()

    async def test_a_prompt_before_the_worker_is_ready_is_refused(self):
        communicator = await self.connected()
        await communicator.send_json_to({"type": "prompt", "frameIndex": 4, "points": [[1, 1]], "labels": [1]})
        self.assertEqual(
            await communicator.receive_json_from(),
            {"type": "error", "message": "The assistant is still preparing this video."},
        )
        self.assertFalse(any(call[1] == "/api/session/prompt/" for call in self.calls))
        await communicator.disconnect()

    async def test_relays_a_prompt_and_its_mask(self):
        communicator = await self.connected()
        upstream = self.upstreams[0]
        await upstream.push({"type": "session_ready"})
        self.assertEqual(await communicator.receive_json_from(), {"type": "ready"})

        await communicator.send_json_to({"type": "prompt", "frameIndex": 4, "points": [[1, 1]], "labels": [1]})
        await _until(lambda: self.calls[-1][1] == "/api/session/prompt/")
        sent = self.calls[-1][2]
        self.assertEqual((sent["frame_index"], sent["window_seconds"]), (4, 0))

        await upstream.push({"type": "frame_result", "frame_index": 4, "mask_b64": "AA==", "mask_shape": [8, 16], "region_id": "1"})
        self.assertEqual(
            await communicator.receive_json_from(),
            {"type": "mask", "frameIndex": 4, "mask": "AA==", "shape": [8, 16]},
        )
        await upstream.push({"type": "inference_done", "job_id": "j1"})
        self.assertEqual(await communicator.receive_json_from(), {"type": "done"})
        await communicator.disconnect()

    async def test_a_bad_prompt_is_explained_and_never_forwarded(self):
        communicator = await self.connected()
        await self.upstreams[0].push({"type": "session_ready"})
        await communicator.receive_json_from()
        await communicator.send_json_to({"type": "prompt", "frameIndex": 4, "points": [[99, 1]], "labels": [1]})
        self.assertEqual(
            await communicator.receive_json_from(),
            {"type": "error", "message": "A point lies outside the frame."},
        )
        self.assertFalse(any(call[1] == "/api/session/prompt/" for call in self.calls))
        await communicator.disconnect()

    async def test_worker_errors_are_translated_never_passed_through(self):
        communicator = await self.connected()
        upstream = self.upstreams[0]
        await upstream.push({"type": "error", "message": "inference_failed"})
        self.assertEqual(
            await communicator.receive_json_from(),
            {"type": "error", "message": "The assistant could not segment this frame."},
        )
        await upstream.push({"type": "error", "message": "Traceback: /app/secret/path"})
        self.assertEqual(
            await communicator.receive_json_from(),
            {"type": "error", "message": "The assistant failed."},
        )
        await communicator.disconnect()

    async def test_a_dead_worker_closes_the_browser_socket(self):
        communicator = await self.connected()
        await self.upstreams[0].close()
        self.assertEqual(await communicator.receive_output(), {"type": "websocket.close", "code": 4502})
```

Merge the new imports into the import block at the top of the file (`override_settings` is already imported there from Task B2).

- [ ] **Step 2: Run to verify they fail**

Run: `docker compose exec -T web python manage.py test common.tests_assist --settings=yggdrasil.settings_sqlite_test`
Expected: FAIL — no route matches `/ws/assist/...`.

- [ ] **Step 3: Implement**

`common/consumers.py` — imports:

```python
from common import assist
from common.permissions import project_allows_annotation, user_can_write_patient_annotations
```

and at the end of the module:

```python
# What the browser may be told about a worker error: never the worker's own text.
_WORKER_ERRORS = {
    "session_expired": "The assistant session ended. Reload the page to start it again.",
    "load_failed": "The assistant could not load this video.",
    "inference_failed": "The assistant could not segment this frame.",
}


@database_sync_to_async
def _assist_target(user, domain, patient_id, file_id):
    """``(close_code, None)`` or ``(None, (storage_key, probe))`` for the video to assist on.

    The video is found from the patient, never taken from the browser: ``fileId`` must name
    *this* patient's subsampled derivative, the file Annotation Mode's frames come from.
    """
    if not user or not user.is_authenticated:
        return 4401, None
    if domain not in DOMAINS:
        return 4404, None
    Patient = apps.get_model(domain, "Patient")
    patient = Patient.objects.select_related("project").filter(patient_id=patient_id).first()
    if patient is None:
        return 4404, None
    if not user_can_write_patient_annotations(user, patient):
        return 4403, None
    if not project_allows_annotation(patient, assist.METHOD_SLUG):
        return 4403, None
    video = patient.files.filter(
        pk=file_id, file_type="video_processed", subtype="subsampled"
    ).first()
    probe = assist.probe_of(video) if video else None
    if probe is None:
        return 4404, None
    return None, (video.file_path, probe)


class AssistSessionConsumer(AsyncWebsocketConsumer):
    """One page visit's assisted-segmentation session, relayed to the GPU worker.

    The browser names the patient (URL) and the video (``?fileId=``); everything the worker
    is told -- the storage key, the session id, the token -- is decided here. The worker
    starts precomputing on connect; leaving the page closes this socket, and that closes the
    worker session at once instead of after its grace period.
    """

    upstream = None
    upstream_task = None
    session_id = None
    probe = None
    ready = False

    async def connect(self):
        user = self.scope.get("user")
        kwargs = self.scope["url_route"]["kwargs"]
        query = parse_qs(self.scope.get("query_string", b"").decode())
        try:
            file_id = int(query.get("fileId", [""])[0])
        except ValueError:
            await self._refuse(4400, "fileId is not an integer")
            return
        if not assist.is_configured():
            await self._refuse(4503, "ASSIST_WORKER_URL or ASSIST_WORKER_TOKEN is unset")
            return
        code, target = await _assist_target(user, kwargs["domain"], kwargs["patient_id"], file_id)
        if code:
            await self._refuse(
                code, f"{user} may not assist on {kwargs['domain']} patient {kwargs['patient_id']} file {file_id}"
            )
            return
        key, self.probe = target
        self.session_id = assist.new_session_id(file_id)
        try:
            # Subscribe before /ready: the worker's session_ready is pub/sub, and is lost if
            # nobody is listening when precompute finishes.
            self.upstream = await websockets.connect(
                assist.upstream_ws_url(self.session_id),
                additional_headers=assist.auth_headers(),
                open_timeout=settings.ASSIST_CONNECT_TIMEOUT,
                max_size=8 * 1024 * 1024,
            )
            await asyncio.to_thread(
                assist.worker_request,
                "POST",
                "/api/session/ready/",
                {"video_id": self.session_id, "video_source": key},
            )
        except Exception as exc:
            logger.warning("Could not open an assist session: %s", exc)
            # The socket may be open and /ready may have reached the worker: release both.
            await self.disconnect(4502)
            await self.close(code=4502)
            return
        await self.accept()
        await self.send(text_data=json.dumps({"type": "preparing"}))
        self.upstream_task = asyncio.create_task(self._relay_upstream())

    async def _refuse(self, code, reason):
        logger.warning("Refusing assist session (%s): %s", code, reason)
        await self.close(code=code)

    async def _error(self, message):
        await self.send(text_data=json.dumps({"type": "error", "message": message}))

    async def receive(self, text_data=None, bytes_data=None):
        try:
            message = json.loads(text_data or "")
        except json.JSONDecodeError:
            message = None
        if not isinstance(message, dict) or message.get("type") != "prompt":
            await self._error("Unknown message.")
            return
        if not self.ready:
            await self._error("The assistant is still preparing this video.")
            return
        try:
            body = assist.prompt_body(self.session_id, message, self.probe)
            await asyncio.to_thread(assist.worker_request, "POST", "/api/session/prompt/", body)
        except assist.PromptError as exc:
            await self._error(str(exc))
        except Exception as exc:
            logger.warning("Assist prompt failed: %s", exc)
            await self._error("The assistant could not be reached.")

    async def disconnect(self, close_code):
        current = asyncio.current_task()
        if self.upstream_task and self.upstream_task is not current:
            self.upstream_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.upstream_task
        if self.upstream is not None:
            with suppress(Exception):
                await self.upstream.close()
            self.upstream = None
        if self.session_id:
            # Free the worker's RAM now, not after its grace period.
            with suppress(Exception):
                await asyncio.to_thread(
                    assist.worker_request, "DELETE", f"/api/session/{self.session_id}/"
                )
            self.session_id = None

    async def _relay_upstream(self):
        try:
            async for raw in self.upstream:
                try:
                    message = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                kind = message.get("type")
                if kind == "session_ready":
                    self.ready = True
                    await self.send(text_data=json.dumps({"type": "ready"}))
                elif kind == "frame_result":
                    await self.send(
                        text_data=json.dumps(
                            {
                                "type": "mask",
                                "frameIndex": message.get("frame_index"),
                                "mask": message.get("mask_b64"),
                                "shape": message.get("mask_shape"),
                            }
                        )
                    )
                elif kind == "inference_done":
                    await self.send(text_data=json.dumps({"type": "done"}))
                elif kind == "error":
                    await self._error(_WORKER_ERRORS.get(message.get("message"), "The assistant failed."))
        except asyncio.CancelledError:
            raise
        except websockets.ConnectionClosed:
            pass
        finally:
            await self.close(code=4502)
```

`common/routing.py`:

```python
from common.consumers import AssistSessionConsumer, LiveTranscriptionConsumer


websocket_urlpatterns = [
    path(
        "ws/live-transcription/<str:domain>/<int:patient_id>/",
        LiveTranscriptionConsumer.as_asgi(),
        name="live_transcription",
    ),
    path(
        "ws/assist/<str:domain>/<int:patient_id>/",
        AssistSessionConsumer.as_asgi(),
        name="assist_session",
    ),
]
```

- [ ] **Step 4: Run the tests and the import contract**

Run: `docker compose exec -T web python manage.py test common.tests_assist common.tests_live_transcription --settings=yggdrasil.settings_sqlite_test`
Run: `uvx --from import-linter==2.15 lint-imports && uvx ruff@0.15.20 check .`
Expected: PASS; contracts kept; ruff clean.

- [ ] **Step 5: Commit**

```bash
git add common/consumers.py common/routing.py common/tests_assist.py
git commit -m "feat(assist): the relay socket between Annotation Mode and the GPU worker"
```

### Task B4: Browser side — wire format, points, channel

**Files:**
- Create: `frontend/imaging/segment/assist.js`, `frontend/tests/segmentAssist.test.js`

**Interfaces:**
- Consumes: the relay protocol (B3).
- Produces (exports of `assist.js`):
  - `packMask(plane: Uint8Array) -> string`
  - `unpackMask(b64: string, shape: [number, number], width: number, height: number) -> Uint8Array` (throws on a mismatch)
  - `hasPixels(plane: Uint8Array) -> boolean`
  - `createPromptPoints() -> { for(frameIndex, code) -> Array<{x, y, label}>, add(frameIndex, code, x, y, label), remove(frameIndex, code, index) }`
  - `maskTarget(sent: {frameIndex, code} | null, message: {frameIndex}, current: {frameIndex, code}) -> string | null`
  - `assistSocketUrl(location: {protocol, host}, data: {projectNamespace, patientId, fileId}) -> string`
  - `createAssistChannel({url, WebSocketImpl?, onStatus?, onMask?, onError?}) -> { status, prompt(payload, context), close() }`; statuses `connecting | preparing | ready | busy | closed`; `onMask(message, context)` gets the context passed with the prompt in flight.

- [ ] **Step 1: Write the failing tests** — `frontend/tests/segmentAssist.test.js`

```js
import test from 'node:test';
import assert from 'node:assert/strict';

import {
    assistSocketUrl,
    createAssistChannel,
    createPromptPoints,
    hasPixels,
    maskTarget,
    packMask,
    unpackMask,
} from '../imaging/segment/assist.js';

test('packMask is the worker bitpack_u1_v1: row-major, LSB-first, base64', () => {
    // numpy: np.packbits([1,0,0,0,0,0,0,0, 0,1], bitorder="little") == [1, 2] -> "AQI="
    assert.equal(packMask(Uint8Array.from([1, 0, 0, 0, 0, 0, 0, 0, 0, 1])), 'AQI=');
});

test('unpackMask inverts packMask on a frame that is not a multiple of 8', () => {
    const plane = Uint8Array.from({ length: 37 * 5 }, (_, index) => ((index * 7) % 3 === 0 ? 1 : 0));
    assert.deepEqual(unpackMask(packMask(plane), [5, 37], 37, 5), plane);
});

test('unpackMask refuses a mask for another frame size or orientation', () => {
    const mask = packMask(new Uint8Array(8 * 6));
    assert.throws(() => unpackMask(mask, [8, 6], 8, 6), /frame/, 'transposed: same byte count');
    assert.throws(() => unpackMask(packMask(new Uint8Array(64)), [8, 9], 9, 8), /frame/);
});

test('hasPixels', () => {
    assert.equal(hasPixels(new Uint8Array(4)), false);
    assert.equal(hasPixels(Uint8Array.from([0, 0, 1, 0])), true);
});

test('prompt points start over when the frame or the label changes', () => {
    const points = createPromptPoints();
    points.add(4, 'l1', 10, 20, 1);
    points.add(4, 'l1', 30, 40, 0);
    assert.deepEqual(points.for(4, 'l1'), [{ x: 10, y: 20, label: 1 }, { x: 30, y: 40, label: 0 }]);
    points.remove(4, 'l1', 0);
    assert.deepEqual(points.for(4, 'l1'), [{ x: 30, y: 40, label: 0 }]);
    assert.deepEqual(points.for(5, 'l1'), [], 'another frame');
    assert.deepEqual(points.for(4, 'l1'), [], 'going back does not bring them back');
    points.add(4, 'l1', 1, 1, 1);
    assert.deepEqual(points.for(4, 'l2'), [], 'another label');
});

test('a mask is written only where its prompt was sent from', () => {
    const sent = { frameIndex: 4, code: 'l1' };
    const here = { frameIndex: 4, code: 'l1' };
    assert.equal(maskTarget(sent, { frameIndex: 4 }, here), 'l1');
    assert.equal(maskTarget(sent, { frameIndex: 5 }, here), null, 'a frame the prompt was not for');
    assert.equal(maskTarget(sent, { frameIndex: 4 }, { frameIndex: 5, code: 'l1' }), null, 'moved to another frame');
    assert.equal(maskTarget(sent, { frameIndex: 4 }, { frameIndex: 4, code: 'l2' }), null, 'selected another label');
    assert.equal(maskTarget(null, { frameIndex: 4 }, here), null, 'nothing in flight');
});

test('assistSocketUrl follows the page scheme', () => {
    const data = { projectNamespace: 'laparoscopy', patientId: 3, fileId: 9 };
    assert.equal(assistSocketUrl({ protocol: 'https:', host: 'h' }, data), 'wss://h/ws/assist/laparoscopy/3/?fileId=9');
    assert.equal(assistSocketUrl({ protocol: 'http:', host: 'localhost:8000' }, data), 'ws://localhost:8000/ws/assist/laparoscopy/3/?fileId=9');
});

class FakeSocket {
    constructor(url) {
        this.url = url;
        this.sent = [];
        this.listeners = {};
        FakeSocket.last = this;
    }
    addEventListener(type, listener) {
        (this.listeners[type] ??= []).push(listener);
    }
    send(data) {
        this.sent.push(JSON.parse(data));
    }
    close() {
        this.emit('close', {});
    }
    emit(type, event) {
        for (const listener of this.listeners[type] ?? []) listener(event);
    }
    receive(message) {
        this.emit('message', { data: JSON.stringify(message) });
    }
}

function channel(overrides = {}) {
    const seen = { statuses: [], masks: [], errors: [] };
    const assist = createAssistChannel({
        url: 'ws://x',
        WebSocketImpl: FakeSocket,
        onStatus: (status) => seen.statuses.push(status),
        onMask: (message, context) => seen.masks.push([message.frameIndex, context]),
        onError: (text) => seen.errors.push(text),
        ...overrides,
    });
    return { assist, socket: FakeSocket.last, seen };
}

test('one prompt in flight; the newest waiting one replaces older ones', () => {
    const { assist, socket, seen } = channel();
    assist.prompt({ frameIndex: 1 }, 'a');
    assert.equal(socket.sent.length, 0, 'nothing before the worker is ready');
    socket.receive({ type: 'preparing' });
    socket.receive({ type: 'ready' });
    assert.deepEqual(socket.sent, [{ type: 'prompt', frameIndex: 1 }]);
    assist.prompt({ frameIndex: 2 }, 'b');
    assist.prompt({ frameIndex: 3 }, 'c');
    assert.equal(socket.sent.length, 1, 'still busy');
    socket.receive({ type: 'mask', frameIndex: 1 });
    socket.receive({ type: 'done' });
    assert.deepEqual(socket.sent, [{ type: 'prompt', frameIndex: 1 }, { type: 'prompt', frameIndex: 3 }]);
    assert.deepEqual(seen.masks, [[1, 'a']]);
    assert.deepEqual(seen.statuses.slice(0, 3), ['preparing', 'ready', 'busy']);
});

test('an error ends the prompt in flight and is reported', () => {
    const { assist, socket, seen } = channel();
    socket.receive({ type: 'ready' });
    assist.prompt({ frameIndex: 1 }, 'a');
    assist.prompt({ frameIndex: 2 }, 'b');
    socket.receive({ type: 'error', message: 'The assistant could not segment this frame.' });
    assert.deepEqual(seen.errors, ['The assistant could not segment this frame.']);
    assert.deepEqual(socket.sent.at(-1), { type: 'prompt', frameIndex: 2 });
});

test('a closed socket says so and sends nothing more', () => {
    const { assist, socket, seen } = channel();
    socket.receive({ type: 'ready' });
    socket.close();
    assert.equal(assist.status, 'closed');
    assert.equal(seen.statuses.at(-1), 'closed');
    assist.prompt({ frameIndex: 1 }, 'a');
    assert.equal(socket.sent.length, 0);
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `npm test 2>&1 | grep -E "segmentAssist|^# (pass|fail)"`
Expected: FAIL — `Cannot find module '../imaging/segment/assist.js'`.

- [ ] **Step 3: Implement** — `frontend/imaging/segment/assist.js`

```js
/**
 * Assisted segmentation, the parts that are not Cornerstone: the worker's mask format, the
 * prompt points, and the relay socket (`common.consumers.AssistSessionConsumer`).
 *
 * The socket opens with the page so the worker precomputes while the annotator is still
 * watching the video; closing the page closes the worker session.
 */

/** A 0/1 plane as the worker's `bitpack_u1_v1`: row-major, LSB-first in each byte, base64. */
export function packMask(plane) {
    const bytes = new Uint8Array(Math.ceil(plane.length / 8));
    for (let index = 0; index < plane.length; index += 1) {
        if (plane[index]) {
            bytes[index >> 3] |= 1 << (index & 7);
        }
    }
    let binary = '';
    for (const byte of bytes) {
        binary += String.fromCharCode(byte);
    }
    return btoa(binary);
}

/**
 * The inverse, for a mask the worker says is `shape` = [height, width]. Refuses anything that
 * is not exactly this frame: a transposed mask has the same byte count and would paint
 * plausible nonsense.
 */
export function unpackMask(b64, shape, width, height) {
    const binary = atob(b64);
    if (shape?.[0] !== height || shape?.[1] !== width || binary.length !== Math.ceil((width * height) / 8)) {
        throw new Error('The assistant answered with a mask for another frame size.');
    }
    const plane = new Uint8Array(width * height);
    for (let index = 0; index < plane.length; index += 1) {
        plane[index] = (binary.charCodeAt(index >> 3) >> (index & 7)) & 1;
    }
    return plane;
}

export const hasPixels = (plane) => plane.some((value) => value !== 0);

/** The points of the (frame, label) being worked on. Changing either starts over. */
export function createPromptPoints() {
    let key = null;
    let points = [];
    function pointsFor(frameIndex, code) {
        const next = `${frameIndex}:${code}`;
        if (next !== key) {
            key = next;
            points = [];
        }
        return points;
    }
    return {
        for: pointsFor,
        add(frameIndex, code, x, y, label) {
            pointsFor(frameIndex, code).push({ x, y, label });
        },
        remove(frameIndex, code, index) {
            pointsFor(frameIndex, code).splice(index, 1);
        },
    };
}

/** The label a returned mask belongs on, or null when the annotator has moved on. */
export function maskTarget(sent, message, current) {
    if (!sent || message.frameIndex !== sent.frameIndex) {
        return null;
    }
    if (current.frameIndex !== sent.frameIndex || current.code !== sent.code) {
        return null;
    }
    return sent.code;
}

export function assistSocketUrl({ protocol, host }, { projectNamespace, patientId, fileId }) {
    const scheme = protocol === 'https:' ? 'wss' : 'ws';
    return `${scheme}://${host}/ws/assist/${projectNamespace}/${patientId}/?fileId=${fileId}`;
}

/**
 * The relay socket. One prompt in flight at a time; one sent while busy replaces whichever
 * was waiting, because only the newest set of points is worth an answer.
 */
export function createAssistChannel({
    url,
    WebSocketImpl = globalThis.WebSocket,
    onStatus = () => {},
    onMask = () => {},
    onError = () => {},
}) {
    const socket = new WebSocketImpl(url);
    let status = 'connecting';
    let inFlight = null;
    let waiting = null;

    function setStatus(next) {
        status = next;
        onStatus(next);
    }

    function flush() {
        if (inFlight || !waiting || status !== 'ready') {
            return;
        }
        inFlight = waiting;
        waiting = null;
        socket.send(JSON.stringify({ type: 'prompt', ...inFlight.payload }));
        onStatus('busy');
    }

    socket.addEventListener('message', (event) => {
        let message;
        try {
            message = JSON.parse(event.data);
        } catch {
            return;
        }
        if (message.type === 'preparing') {
            setStatus('preparing');
        } else if (message.type === 'ready') {
            setStatus('ready');
            flush();
        } else if (message.type === 'mask') {
            onMask(message, inFlight?.context ?? null);
        } else if (message.type === 'done' || message.type === 'error') {
            if (message.type === 'error') {
                onError(message.message);
            }
            inFlight = null;
            if (status === 'ready') {
                onStatus('ready');
            }
            flush();
        }
    });
    socket.addEventListener('close', () => {
        inFlight = null;
        waiting = null;
        setStatus('closed');
    });

    return {
        get status() {
            return status;
        },
        prompt(payload, context) {
            waiting = { payload, context };
            flush();
        },
        close() {
            socket.close();
        },
    };
}
```

- [ ] **Step 4: Run the tests**

Run: `npm test 2>&1 | grep -E "^# (pass|fail)"`
Expected: `# fail 0`.

- [ ] **Step 5: Commit**

```bash
git add frontend/imaging/segment/assist.js frontend/tests/segmentAssist.test.js
git commit -m "feat(assist): mask wire format, prompt points and the relay channel (browser)"
```

### Task B5: The session learns the Assist tool

**Files:**
- Modify: `frontend/imaging/segment/tools.js` (after `SEGMENT_TOOLS`), `frontend/imaging/segment/session.js` (`activateTool`, the session object), `frontend/entries/image-segment.js` (`prepare()`'s returned object)

**Interfaces:**
- Produces: `tools.ASSIST_TOOL = 'assist'`; on the session object: `setTool('assist')` disables painting with the primary button; `imagePointAt(clientX, clientY) -> [x, y]` (frame pixels); `canvasPointOf([x, y]) -> [cx, cy]` (element CSS pixels); `applyMask(code, plane)` (replaces the plane as one undoable edit, fires the data-modified event so the saver saves it).

- [ ] **Step 1: `tools.js`** — after `SEGMENT_TOOLS`:

```js
/** The Assist tool: not a Cornerstone tool, its clicks are prompt points (`bootstrap.js`). */
export const ASSIST_TOOL = 'assist';
```

- [ ] **Step 2: `session.js`** — import it next to the existing tools import (`import { ALL_TOOL_NAMES, ASSIST_TOOL, toolNameFor } from './tools.js';`), and in `activateTool`, after the loop that makes every tool passive:

```js
        // Assist paints nothing itself: its clicks are prompt points for the worker.
        if (toolId === ASSIST_TOOL) {
            return;
        }
```

In the session object, after `setColor`:

```js
        /** The frame pixel under a page point (an event's clientX/clientY), at any zoom or pan. */
        imagePointAt(clientX, clientY) {
            const box = element.getBoundingClientRect();
            const world = viewport.canvasToWorld([clientX - box.left, clientY - box.top]);
            return cornerstone.worldToImageCoords(imageId, world);
        },
        /** Where a frame pixel is drawn, in the element's CSS pixels. */
        canvasPointOf([x, y]) {
            return viewport.worldToCanvas(cornerstone.imageToWorldCoords(imageId, [x, y]));
        },
        /**
         * Replace a label's mask with the assistant's, as one undoable edit. `writePlane`
         * fires the data-modified event, so the saver saves it like a brush stroke.
         */
        applyMask(code, plane) {
            const before = stack.readPlane(code);
            stack.writePlane(code, plane);
            HistoryMemo.DefaultHistoryMemo.push({
                restoreMemo: (undo) => stack.writePlane(code, undo ? before : plane),
            });
        },
```

- [ ] **Step 3: `image-segment.js`** — in the object `prepare()` returns, after `HistoryMemo: coreUtilities.HistoryMemo,`:

```js
            worldToImageCoords: coreUtilities.worldToImageCoords,
            imageToWorldCoords: coreUtilities.imageToWorldCoords,
```

- [ ] **Step 4: Run the JS suite** (pure modules still import cleanly)

Run: `npm test 2>&1 | grep -E "^# (pass|fail)"`
Expected: `# fail 0`. (The session itself needs WebGL; it is exercised in Task B7.)

- [ ] **Step 5: Commit**

```bash
git add frontend/imaging/segment/tools.js frontend/imaging/segment/session.js frontend/entries/image-segment.js
git commit -m "feat(assist): the segmentation session's Assist mode, coordinates and undoable mask write"
```

### Task B6: Toolbar, points overlay, wiring, tutorial, bundle

**Files:**
- Modify: `templates/laparoscopy/patient_detail_content.html`, `static/css/patient_detail.css`, `frontend/imaging/segment/bootstrap.js`, `static/js/laparoscopy_tutorial.js`, `static/vendor/cornerstone/` (rebuilt)

**Interfaces:**
- Consumes: B4's exports, B5's session methods, `imageSegmentData.canAssist`.

- [ ] **Step 1: Template** — after the `Segmentation tools` group (the Assist button itself was added in B1):

```html
                    {% if image_segment_data.canAssist %}
                    <button type="button" id="segment-assist-polarity" class="btn btn-sm btn-outline-light d-none"
                            aria-pressed="true" title="What the next click marks: an area to include, or to exclude">
                        + Include
                    </button>
                    {% endif %}
```

and wrap the viewport so the points overlay sits exactly over it:

```html
                <div class="segment-viewport-wrap">
                    <div id="segment-viewport" class="segment-viewport"
                         style="aspect-ratio: {{ image_segment_data.width }} / {{ image_segment_data.height }};"></div>
                    {% if image_segment_data.canAssist %}
                    <svg id="segment-assist-points" class="segment-assist-points" aria-hidden="true"></svg>
                    {% endif %}
                </div>
```

- [ ] **Step 2: CSS** — append to `static/css/patient_detail.css`:

```css
/* Assist (assisted segmentation): the prompt points drawn over the frame. */
.laparoscopy-patient-detail .segment-viewport-wrap {
    position: relative;
}
.laparoscopy-patient-detail .segment-assist-points {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    pointer-events: none;
    overflow: visible;
}
.laparoscopy-patient-detail .segment-assist-points circle {
    stroke: #fff;
    stroke-width: 2;
}
.laparoscopy-patient-detail .segment-assist-points .is-include { fill: var(--ygg-accent2); }
.laparoscopy-patient-detail .segment-assist-points .is-exclude { fill: var(--ygg-danger, #d9534f); }
```

- [ ] **Step 3: `bootstrap.js`** — imports:

```js
import { ASSIST_TOOL, SEGMENT_TOOLS } from './tools.js';
import {
    assistSocketUrl,
    createAssistChannel,
    createPromptPoints,
    hasPixels,
    maskTarget,
    packMask,
    unpackMask,
} from './assist.js';
```

(replacing the existing `import { SEGMENT_TOOLS } from './tools.js';`). Right after `const hidden = new Set();`:

```js
    // Assisted segmentation ---------------------------------------------------------
    // The relay opens with the page, so the worker precomputes while the annotator is
    // still watching the video; leaving the page closes it (and the worker session).
    const assistButton = doc.querySelector(`[data-segment-tool="${ASSIST_TOOL}"]`);
    const assistPolarity = $(doc, 'segment-assist-polarity');
    const assistOverlay = $(doc, 'segment-assist-points');
    const promptPoints = createPromptPoints();
    const ASSIST_STATUS = {
        connecting: 'Connecting to the assistant…',
        preparing: 'Preparing the assistant for this video…',
        ready: 'Assist: click the object, and the model paints the selected label',
        busy: 'The assistant is working…',
        closed: 'The assistant is unavailable',
    };
    const POINT_HIT_PX = 8;
    const SVG_NS = 'http://www.w3.org/2000/svg';
    let includeNext = true;
    const assist =
        data.canAssist && assistButton
            ? createAssistChannel({
                  url: assistSocketUrl(doc.defaultView.location, data),
                  onStatus: describeAssist,
                  onMask: applyAssistMask,
                  onError: (text) => say(text, true),
              })
            : null;

    function describeAssist(status) {
        assistButton.disabled = status !== 'ready' && status !== 'busy';
        assistButton.title = ASSIST_STATUS[status] ?? '';
        if (status === 'closed' && toolId === ASSIST_TOOL) {
            say(ASSIST_STATUS.closed, true);
            doc.querySelector('[data-segment-tool="brush"]')?.click();
        }
    }

    function applyAssistMask(message, sent) {
        if (!session) {
            return;
        }
        const code = maskTarget(sent, message, { frameIndex: session.frame.frameIndex, code: selected });
        if (!code) {
            return;
        }
        try {
            const { width, height } = session.frame;
            session.applyMask(code, unpackMask(message.mask, message.shape, width, height));
            refreshUndoRedoSoon();
        } catch (error) {
            say(error.message, true);
        }
    }

    function renderAssistPoints() {
        if (!assistOverlay) {
            return;
        }
        assistOverlay.replaceChildren();
        if (toolId !== ASSIST_TOOL || !session || !selected) {
            return;
        }
        for (const point of promptPoints.for(session.frame.frameIndex, selected)) {
            const [cx, cy] = session.canvasPointOf([point.x, point.y]);
            const dot = doc.createElementNS(SVG_NS, 'circle');
            dot.setAttribute('cx', String(cx));
            dot.setAttribute('cy', String(cy));
            dot.setAttribute('r', '5');
            dot.setAttribute('class', point.label ? 'is-include' : 'is-exclude');
            assistOverlay.appendChild(dot);
        }
    }

    function sendAssistPrompt() {
        const { frameIndex } = session.frame;
        const points = promptPoints.for(frameIndex, selected);
        renderAssistPoints();
        if (!points.length) {
            return;
        }
        const plane = session.labels.readPlane(selected);
        assist.prompt(
            {
                frameIndex,
                points: points.map((point) => [point.x, point.y]),
                labels: points.map((point) => point.label),
                ...(hasPixels(plane) ? { mask: packMask(plane) } : {}),
            },
            { frameIndex, code: selected }
        );
    }
```

Before `return { enter, leave, ... }` at the end of `bootstrapSegmentation`:

```js
    viewportElement.addEventListener('click', (event) => {
        if (!assist || toolId !== ASSIST_TOOL || !session || !selected || event.button !== 0) {
            return;
        }
        const { frameIndex, width, height } = session.frame;
        const box = viewportElement.getBoundingClientRect();
        const points = promptPoints.for(frameIndex, selected);
        // Clicking an existing point removes it.
        const hit = points.findIndex((point) => {
            const [cx, cy] = session.canvasPointOf([point.x, point.y]);
            return Math.hypot(cx - (event.clientX - box.left), cy - (event.clientY - box.top)) <= POINT_HIT_PX;
        });
        if (hit >= 0) {
            promptPoints.remove(frameIndex, selected, hit);
        } else {
            const [x, y] = session.imagePointAt(event.clientX, event.clientY);
            if (!(x >= 0 && y >= 0 && x < width && y < height)) {
                return;
            }
            promptPoints.add(frameIndex, selected, x, y, includeNext ? 1 : 0);
        }
        sendAssistPrompt();
    });
    // coreEnums.Events.CAMERA_MODIFIED: zoom and pan move the points with the frame.
    viewportElement.addEventListener('CORNERSTONE_CAMERA_MODIFIED', () => renderAssistPoints());
    assistPolarity?.addEventListener('click', () => {
        includeNext = !includeNext;
        assistPolarity.setAttribute('aria-pressed', String(includeNext));
        assistPolarity.textContent = includeNext ? '+ Include' : '− Exclude';
    });
```

In the toolbar loop, iterate the Assist tool too and show the toggle only with it:

```js
    for (const tool of [...SEGMENT_TOOLS, { id: ASSIST_TOOL }]) {
```

and after `sizeRow?.classList.toggle('d-none', !tool.hasSize);`:

```js
            assistPolarity?.classList.toggle('d-none', tool.id !== ASSIST_TOOL);
            renderAssistPoints();
```

At the end of `refreshPanel()` (it runs after every frame open, label change and leave), add `renderAssistPoints();` — the points reset there too, because `promptPoints.for` starts over when the frame or label changed.

- [ ] **Step 4: Tutorial** — in `static/js/laparoscopy_tutorial.js`, insert after the `Label and size` step:

```js
        {
            element: '[data-segment-tool="assist"]',
            popover: {
                title: 'Assist',
                description: 'Click the object and the model paints it into the selected label. ' +
                    '<b>Include</b> / <b>Exclude</b> sets what the next click marks; click a point ' +
                    'again to remove it. Each click refines the mask, and undo works as usual.'
            }
        },
```

(`skipMissingElement` already drops it where Assist is not offered.)

- [ ] **Step 5: Build, verify, run everything**

```bash
npm run build && npm run verify
npm test 2>&1 | grep -E "^# (pass|fail)"
docker compose exec -T web python manage.py test laparoscopy common annotations --settings=yggdrasil.settings_sqlite_test
uvx ruff@0.15.20 check . && uvx --from import-linter==2.15 lint-imports
```

Expected: bundle built and verified, `# fail 0`, Django OK, ruff and contracts clean. A second `npm run build` leaves `git status` unchanged.

- [ ] **Step 6: Commit**

```bash
git add templates/laparoscopy/patient_detail_content.html static/css/patient_detail.css \
        frontend/imaging/segment/bootstrap.js static/js/laparoscopy_tutorial.js static/vendor/cornerstone
git commit -m "feat(assist): Assist tool, include/exclude toggle, prompt points; bundle rebuilt"
```

### Task B7: End to end on the dev stack

**Files:**
- Modify: `.env` (local), `LOCAL_SETUP.md` (git-ignored)

- [ ] **Step 1: Point Yggdrasil at the worker** — in Yggdrasil's `.env`: `ASSIST_WORKER_URL=http://djangoworker-api:8000` and `ASSIST_WORKER_TOKEN=<the WORKER_API_TOKEN from Task A6>`. Then `docker compose up -d` and `docker compose exec -T web python manage.py migrate`.

- [ ] **Step 2: Drive it** (Claude in Chrome, or headless Playwright with `channel="chrome"` as in the tutorial checks) on `http://localhost:8000/laparoscopy/patient/3/`:
  1. Page load → `docker compose -p djangoworker logs worker` shows the load and precompute starting for `lap-<fileId>-…`.
  2. Assist is disabled with "Preparing the assistant…", then enables.
  3. Annotation Mode → Assist → click the object: a green point appears and a mask fills the selected label within a few seconds; Exclude + click outside shrinks it; clicking a point removes it.
  4. Undo restores the previous mask; the save status reaches "Saved".
  5. Change frame or label → the points are gone; a mask still in flight is not painted.
  6. Reload the page → the worker logs a close of the old session id and a new load; the new session reaches ready.
  7. `docker compose -p djangoworker stop api` → Assist disables, the tool switches back to Brush, painting still saves. Start it again afterwards.
  8. Close the tab → the worker logs the session's eviction within seconds, and `free -g` shows the memory back.
  9. Log in as a viewer → no Assist button.

- [ ] **Step 3: Record the machine setup** — add to `LOCAL_SETUP.md`: the worker lives in `~/Desktop/DjangoWorker` (`make up`), joins `proxy-net` as `djangoworker-api`, uses the dev Garage credentials and SurgiSAM2, and its `WORKER_API_TOKEN` equals Yggdrasil's `ASSIST_WORKER_TOKEN`; Traefik no longer routes `/api/session`.

- [ ] **Step 4: Final checks and hand-off** — `git status` clean apart from intended changes in both repos; ask the maintainer to review the branch `feat/assisted-segmentation` and the worker's commits.
