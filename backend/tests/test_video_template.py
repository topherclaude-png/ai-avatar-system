"""
Tests for video-template support (POC dev task 1).

The MuseTalk worker itself needs GPU + the MuseTalk repo, so it's verified
on the pod. What IS locally testable:

  * avatar_processor.process_video — validation, metadata, thumbnail
  * the /avatars/upload endpoint accepting video (stored unmodified) and
    rejecting oversize/garbage/too-long clips
  * websocket._resolve_local_image preserving the template suffix (an .mp4
    cached as .jpg would silently demote video templates to broken stills)
  * animator simple-fallback building a looped-video ffmpeg command
"""

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from app.services.avatar_processor import avatar_processor  # noqa: E402


def _write_test_video(path, frames=10, fps=25, size=64):
    """Tiny synthetic mp4: moving gradient (content irrelevant, decodability is)."""
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (size, size)
    )
    assert writer.isOpened(), "cv2 cannot write mp4 in this environment"
    for i in range(frames):
        frame = np.full((size, size, 3), (i * 7) % 255, dtype=np.uint8)
        writer.write(frame)
    writer.release()
    return path


# ── avatar_processor.process_video ───────────────────────────────────────────


async def test_process_video_metadata_and_thumbnail(tmp_path):
    video = _write_test_video(tmp_path / "clip.mp4", frames=25, fps=25)
    thumb = tmp_path / "thumb.jpg"

    thumb_path, meta = await avatar_processor.process_video(str(video), str(thumb))

    assert thumb.exists() and thumb.stat().st_size > 0
    assert meta["is_video_template"] is True
    assert meta["fps"] == pytest.approx(25, abs=1)
    assert meta["duration_seconds"] == pytest.approx(1.0, abs=0.2)
    assert meta["face_detected"] is False  # gradient frames have no face
    assert meta["original_size"] == (64, 64)


async def test_process_video_rejects_garbage(tmp_path):
    bogus = tmp_path / "not_a_video.mp4"
    bogus.write_bytes(b"definitely not an mp4")
    with pytest.raises(ValueError):
        await avatar_processor.process_video(str(bogus), str(tmp_path / "t.jpg"))


# ── upload endpoint ──────────────────────────────────────────────────────────


@pytest.fixture
def fake_storage(monkeypatch):
    uploads = {}

    async def fake_upload(data, key, content_type=None, metadata=None):
        uploads[key] = {"bytes": len(data), "content_type": content_type}
        return f"http://test/{key}"

    monkeypatch.setattr(
        "app.api.v1.avatars.storage_service.upload_file", fake_upload
    )
    return uploads


async def test_upload_video_template(client, auth_headers, fake_storage, tmp_path):
    video = _write_test_video(tmp_path / "topher.mp4", frames=25, fps=25)

    resp = await client.post(
        "/api/v1/avatars/upload",
        headers=auth_headers,
        data={"name": "Topher V1"},
        files={"file": ("topher.mp4", video.read_bytes(), "video/mp4")},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    # The pipeline template is the UNMODIFIED video (s3_key is internal —
    # verify through what actually hit storage)…
    template_key = f"avatars/{body['id']}/template.mp4"
    assert template_key in fake_storage
    assert fake_storage[template_key]["content_type"] == "video/mp4"
    assert fake_storage[template_key]["bytes"] == video.stat().st_size
    # …while the UI urls stay displayable images.
    assert body["image_url"].endswith("/image.jpg")
    assert body["thumbnail_url"].endswith("/thumbnail.jpg")
    assert body["avatar_metadata"]["is_video_template"] is True


async def test_upload_video_too_long_rejected(client, auth_headers, fake_storage, tmp_path):
    # 70 frames at 1 fps → 70 s duration
    video = _write_test_video(tmp_path / "long.mp4", frames=70, fps=1)
    resp = await client.post(
        "/api/v1/avatars/upload",
        headers=auth_headers,
        data={"name": "Too Long"},
        files={"file": ("long.mp4", video.read_bytes(), "video/mp4")},
    )
    assert resp.status_code == 400
    assert "60 seconds" in resp.json()["detail"]


async def test_upload_garbage_video_rejected(client, auth_headers, fake_storage):
    resp = await client.post(
        "/api/v1/avatars/upload",
        headers=auth_headers,
        data={"name": "Garbage"},
        files={"file": ("x.mp4", b"not a video at all", "video/mp4")},
    )
    assert resp.status_code == 400


async def test_upload_wrong_type_still_rejected(client, auth_headers, fake_storage):
    resp = await client.post(
        "/api/v1/avatars/upload",
        headers=auth_headers,
        data={"name": "Nope"},
        files={"file": ("doc.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert resp.status_code == 400


# ── template suffix preservation in the session cache ────────────────────────


async def test_resolve_local_image_preserves_video_suffix(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from app import websocket as wsmod

    monkeypatch.setattr(wsmod, "TMPDIR", tmp_path)

    async def fake_download(key):
        return b"video-bytes"

    monkeypatch.setattr(wsmod.storage_service, "download_file", fake_download)

    def no_local(key):
        raise NotImplementedError

    monkeypatch.setattr(wsmod.storage_service, "get_local_path", no_local, raising=False)

    avatar = SimpleNamespace(id="av-1", s3_key="avatars/av-1/template.mp4")
    path = await wsmod.websocket_manager._resolve_local_image(avatar)
    assert path.endswith("av-1.mp4")

    avatar_jpg = SimpleNamespace(id="av-2", s3_key="avatars/av-2/image.jpg")
    path = await wsmod.websocket_manager._resolve_local_image(avatar_jpg)
    assert path.endswith("av-2.jpg")


# ── animator simple fallback: looped video command ───────────────────────────


async def test_simple_fallback_loops_video(monkeypatch, tmp_path):
    from app.services.animator import AvatarAnimator

    animator = AvatarAnimator.__new__(AvatarAnimator)
    animator.fps = 25
    animator.resolution = 512

    captured = {}

    async def fake_exec(*cmd, **kwargs):
        captured["cmd"] = list(cmd)

        class P:
            returncode = 0

            async def communicate(self):
                return b"", b""

        return P()

    import asyncio

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await animator._animate_simple("tpl.mp4", "a.wav", str(tmp_path / "out.mp4"))
    cmd = captured["cmd"]
    assert "-stream_loop" in cmd  # video loops the whole clip
    assert "-tune" not in cmd  # stillimage tuning is image-only

    await animator._animate_simple("tpl.jpg", "a.wav", str(tmp_path / "out2.mp4"))
    cmd = captured["cmd"]
    assert "-loop" in cmd and "-stream_loop" not in cmd
    assert "stillimage" in cmd
