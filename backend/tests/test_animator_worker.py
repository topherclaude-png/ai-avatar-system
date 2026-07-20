"""
Regression test for issue #5 — missing musetalk_worker.py.

musetalk_worker.py is our custom persistent-worker driver, not part of the
upstream MuseTalk repo that setup_musetalk.sh clones. It's gitignored under
models/, so a fresh clone never had it and MuseTalk silently fell back to the
static-image engine for every user. We now ship a tracked copy at
backend/musetalk_worker.py and resolve to it when the clone lacks the script.
"""

from pathlib import Path

from app.services.animator import AvatarAnimator


def test_tracked_worker_script_is_shipped():
    """The tracked worker must exist in the repo (outside gitignored models/)."""
    tracked = Path(__file__).resolve().parent.parent / "musetalk_worker.py"
    assert tracked.is_file(), "backend/musetalk_worker.py is missing — MuseTalk will not start"


def test_resolve_worker_prefers_tracked_copy(tmp_path):
    """
    The TRACKED backend copy wins over the clone snapshot: the clone copy is
    written once by setup_musetalk.sh and silently goes stale when the
    repo's worker is updated without re-running setup (a git pull would
    otherwise deploy old worker code).
    """
    animator = AvatarAnimator()

    # Even when the clone has its own copy, the tracked copy is preferred.
    clone = tmp_path / "MuseTalk"
    (clone / "scripts").mkdir(parents=True)
    (clone / "scripts" / "musetalk_worker.py").write_text("# stale snapshot")
    resolved = animator._resolve_worker_script(clone)
    assert resolved.name == "musetalk_worker.py"
    assert "MuseTalk" not in str(resolved)  # not the clone copy
    assert resolved.is_file()

    # Clone-only layouts still resolve to the clone copy (fallback).
    # (Simulated by pointing at a clone while the tracked copy exists — the
    # fallback branch itself is exercised in environments without the
    # backend tree; here we just assert the tracked copy is the default.)
