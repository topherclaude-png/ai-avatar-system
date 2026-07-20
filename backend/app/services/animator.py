import asyncio
import hashlib
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

import torch

from app.config import settings

TMPDIR = Path(tempfile.gettempdir())

logger = logging.getLogger(__name__)


class AvatarAnimator:
    """
    Avatar Animation Service.
    Supported engines (set AVATAR_ENGINE in .env):
      - musetalk : MuseTalk V1.5 — persistent worker (models loaded once)
      - simple   : ffmpeg static image + audio, no lip-sync
    """

    def __init__(self):
        self.engine = settings.AVATAR_ENGINE
        self.resolution = settings.AVATAR_RESOLUTION
        self.fps = settings.AVATAR_FPS
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.use_float16 = self.device == "cuda"  # float16 on GPU = ~2× faster via Tensor Cores
        self._initialised = False
        self._musetalk_dir: Optional[Path] = None

        # Persistent worker handles
        self._worker_proc: Optional[asyncio.subprocess.Process] = None
        self._worker_lock = asyncio.Lock()
        self._worker_env: dict = {}
        # Worker stderr goes to a FILE, not a pipe: an unread stderr pipe
        # fills up and freezes the child mid-load, and reading it for
        # diagnostics while the child is alive blocks forever (no EOF).
        self._worker_stderr_path = TMPDIR / "musetalk_worker.stderr.log"
        self._worker_stderr_file = None
        # Templates the current worker process has already prepared. First
        # job per template pays one-time prep (video frame extraction +
        # landmarks + VAE) and gets a long timeout; later jobs are fast.
        self._prepared_templates: set = set()

        if self.device == "cuda":
            gpu_name = torch.cuda.get_device_name(0)
            vram_gb = torch.cuda.get_device_properties(0).total_memory / 1024**3
            logger.info(
                f"AvatarAnimator: engine={self.engine}, device=cuda "
                f"({gpu_name}, {vram_gb:.1f} GB VRAM), float16={self.use_float16}"
            )
        else:
            logger.info(
                f"AvatarAnimator: engine={self.engine}, device=cpu "
                f"(no GPU — consider AWS g5/g6 instance for real-time performance)"
            )

    # ── initialisation ────────────────────────────────────────────────────────

    async def initialize(self):
        if self._initialised:
            return

        if self.engine == "musetalk":
            self._musetalk_dir = self._find_dir(settings.MUSETALK_PATH, "scripts/inference.py")
            if self._musetalk_dir is None:
                logger.warning(
                    "MuseTalk not found at '%s'. "
                    "Run scripts/setup_musetalk.sh to install it. "
                    "Falling back to simple animation.",
                    settings.MUSETALK_PATH,
                )
                self.engine = "simple"
            else:
                logger.info(f"MuseTalk found at: {self._musetalk_dir}")
                # Build env once
                existing = os.environ.get("PYTHONPATH", "")
                self._worker_env = os.environ.copy()
                self._worker_env["PYTHONPATH"] = str(self._musetalk_dir) + (
                    ":" + existing if existing else ""
                )

        elif self.engine not in ("simple",):
            logger.warning(f"Unknown engine '{self.engine}', using simple animation.")
            self.engine = "simple"

        self._initialised = True

    def _resolve_worker_script(self, musetalk_dir: Path) -> Path:
        """
        Locate the persistent-worker script.

        `musetalk_worker.py` is OUR custom driver, not part of the upstream
        MuseTalk repo that setup_musetalk.sh clones — so a fresh clone won't
        have it under models/MuseTalk/scripts/. We ship a tracked copy at
        backend/musetalk_worker.py and prefer whichever exists, so MuseTalk
        works even if setup hasn't copied the file into the clone yet.
        The process still runs with cwd=musetalk_dir + PYTHONPATH set to the
        clone, so its `from musetalk.utils …` imports resolve regardless of
        where the script file physically lives.
        """
        # Prefer the TRACKED copy: the clone copy is a snapshot made by
        # setup_musetalk.sh and silently goes stale when the repo's worker is
        # updated without re-running setup — a `git pull` then deploys old
        # worker code. The clone copy remains a fallback for layouts where
        # the backend tree isn't present.
        # backend/app/services/animator.py → backend/musetalk_worker.py
        tracked = Path(__file__).resolve().parent.parent.parent / "musetalk_worker.py"
        if tracked.exists():
            return tracked
        in_clone = musetalk_dir / "scripts" / "musetalk_worker.py"
        if in_clone.exists():
            logger.info(f"Using in-clone MuseTalk worker at {in_clone}")
            return in_clone
        raise FileNotFoundError(
            f"musetalk_worker.py not found in {tracked} or {in_clone}. "
            "Re-run scripts/setup_musetalk.sh."
        )

    def _find_dir(self, config_path: str, marker_file: str) -> Optional[Path]:
        candidates = [
            Path(config_path),
            Path(__file__).resolve().parent.parent.parent / config_path,
        ]
        for p in candidates:
            if (p / marker_file).exists():
                return p.resolve()
        return None

    # ── persistent worker management ─────────────────────────────────────────

    def _stderr_tail(self, max_bytes: int = 4000) -> str:
        """Last chunk of the worker's stderr log, for error messages."""
        try:
            data = self._worker_stderr_path.read_bytes()
            return data[-max_bytes:].decode(errors="replace")
        except Exception:
            return "<no stderr captured>"

    async def _await_worker_line(self, proc, timeout: float, *, ready: bool):
        """
        Read worker stdout until the line we want, SKIPPING chatter: the
        MuseTalk libs print loader/progress text to stdout ("reading
        images...", model-load messages), which otherwise corrupts the
        READY handshake and the JSON job protocol. `ready=True` waits for
        the READY sentinel and returns it; `ready=False` waits for a JSON
        object line and returns it parsed. Bounded by `timeout` overall —
        raises asyncio.TimeoutError past the deadline, RuntimeError (with
        the stderr tail) if the worker exits.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            if not line:  # EOF — worker died
                raise RuntimeError(
                    f"MuseTalk worker exited unexpectedly. stderr tail:\n{self._stderr_tail()}"
                )
            text = line.decode(errors="replace").strip()
            if not text:
                continue
            if ready and text.startswith("READY"):
                return text
            if not ready and text.startswith("{"):
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    pass  # chatter that merely looks like JSON — fall through
            logger.info(f"[musetalk-worker] {text[:200]}")

    async def _ensure_worker(self) -> asyncio.subprocess.Process:
        """Start the persistent worker if not already running."""
        if self._worker_proc is not None and self._worker_proc.returncode is None:
            return self._worker_proc

        musetalk_dir: Path = self._musetalk_dir  # type: ignore[assignment]
        worker_script = self._resolve_worker_script(musetalk_dir)

        logger.info("Starting persistent MuseTalk worker (loading models once)…")
        # Fresh process → its in-memory template prep is empty again.
        self._prepared_templates.clear()
        # stderr → file (append): see __init__ note on why never a pipe.
        if self._worker_stderr_file is not None:
            try:
                self._worker_stderr_file.close()
            except Exception:
                pass
        self._worker_stderr_file = open(self._worker_stderr_path, "ab")
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(worker_script),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=self._worker_stderr_file,
            cwd=str(musetalk_dir),
            env=self._worker_env,
        )

        try:
            # Send init config — include float16 flag so worker can optimise for GPU
            init_msg = (
                json.dumps(
                    {
                        "unet_model_path": str(
                            musetalk_dir / "models" / "musetalkV15" / "unet.pth"
                        ),
                        "unet_config": str(
                            musetalk_dir / "models" / "musetalkV15" / "musetalk.json"
                        ),
                        "whisper_dir": str(musetalk_dir / "models" / "whisper"),
                        "vae_type": str(musetalk_dir / "models" / "sd-vae"),
                        "use_float16": self.use_float16,
                    }
                )
                + "\n"
            )
            proc.stdin.write(init_msg.encode())
            await proc.stdin.drain()

            # Wait for READY. Generous budgets: weights often live on a NETWORK
            # volume (RunPod), where reading ~5 GB can alone take minutes — a
            # tight timeout kills a worker that was loading fine and the next
            # turn starts the churn all over again.
            model_load_timeout = 420 if self.device == "cuda" else 900
            logger.info(
                f"Waiting for worker to finish loading models (timeout={model_load_timeout}s)…"
            )
            try:
                await self._await_worker_line(proc, model_load_timeout, ready=True)
            except asyncio.TimeoutError:
                raise RuntimeError("MuseTalk worker timed out while loading models")
            except RuntimeError:
                raise  # worker died — already carries the stderr tail
        except BaseException:
            # Covers errors AND cancellation (barge-in lands here when a fresh
            # user input cancels the turn mid-spawn). Without this, a
            # cancelled spawn ORPHANS a model-loading process — the next turn
            # then starts a second worker and VRAM fills up with zombies.
            proc.kill()
            raise

        logger.info("MuseTalk worker ready — models loaded")
        self._worker_proc = proc
        return proc

    async def _worker_infer(
        self, image_path: str, audio_path: str, output_path: str, coord_cache: Optional[str]
    ) -> str:
        """Send one job to the persistent worker and await its result."""
        import uuid as _uuid

        async with self._worker_lock:
            proc = await self._ensure_worker()

            # Correlation id: if a previous caller was cancelled mid-job
            # (barge-in, session teardown), the worker's reply for THAT job
            # is still in the pipe — without ids, the next job would consume
            # the stale reply as its own result (observed live: a dead
            # session's error surfaced as the next session's failure).
            job_id = _uuid.uuid4().hex[:12]
            job = (
                json.dumps(
                    {
                        "job_id": job_id,
                        "image": str(Path(image_path).resolve()),
                        "audio": str(Path(audio_path).resolve()),
                        "output": str(Path(output_path).resolve()),
                        "coord_cache": coord_cache,
                    }
                )
                + "\n"
            )

            # If the worker died (OOM/segfault) its stdin is closed; writing
            # raises BrokenPipeError. Reset the handle so the NEXT job respawns
            # a fresh worker instead of repeatedly failing against a dead pipe.
            try:
                proc.stdin.write(job.encode())
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError) as e:
                proc.kill()
                self._worker_proc = None
                raise RuntimeError(f"MuseTalk worker pipe is dead: {e}") from e

            # GPU: expect ~5-15s per sentence; CPU: up to 5 min. The FIRST job
            # per template additionally pays one-time prep — for a video
            # template that's frame extraction + per-frame landmarks + VAE,
            # which can take minutes — so it gets a much longer allowance.
            template_key = str(Path(image_path).resolve())
            first_job = template_key not in self._prepared_templates
            if self.device == "cuda":
                infer_timeout = 300 if first_job else 60
            else:
                infer_timeout = 900 if first_job else 300
            try:
                # Discard stale replies from cancelled predecessors until OUR
                # job id comes back (bounded by the same overall deadline).
                loop = asyncio.get_running_loop()
                deadline = loop.time() + infer_timeout
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    result = await self._await_worker_line(proc, remaining, ready=False)
                    if result.get("job_id") in (job_id, None):
                        break
                    logger.warning(
                        f"Discarding stale worker reply for job {result.get('job_id')}"
                    )
            except asyncio.TimeoutError:
                proc.kill()
                self._worker_proc = None
                raise RuntimeError(f"MuseTalk inference timed out after {infer_timeout}s")
            except RuntimeError:
                # Worker died mid-job (EOF). Reset so the next call respawns
                # instead of erroring on a half-dead process.
                proc.kill()
                self._worker_proc = None
                raise

            if result["status"] != "ok":
                raise RuntimeError(result.get("msg", "Unknown worker error"))

            self._prepared_templates.add(template_key)
            return output_path

    # ── public API ────────────────────────────────────────────────────────────

    async def animate(
        self,
        avatar_image_path: str,
        audio_path: str,
        output_path: str,
        cache_key: Optional[str] = None,
    ) -> str:
        """
        Animate avatar with audio. Returns path to the generated video.
        Falls back to simple (static image + audio) on any engine failure.
        """
        if not self._initialised:
            await self.initialize()

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"Animating [{self.engine}] image={avatar_image_path} audio={audio_path}")

        try:
            if self.engine == "musetalk":
                return await self._animate_musetalk(avatar_image_path, audio_path, output_path)
            else:
                return await self._animate_simple(avatar_image_path, audio_path, output_path)
        except Exception as e:
            logger.error(f"Animation failed ({self.engine}): {e}. Falling back to simple.")
            return await self._animate_simple(avatar_image_path, audio_path, output_path)

    # ── MuseTalk ──────────────────────────────────────────────────────────────

    async def _animate_musetalk(
        self,
        avatar_path: str,
        audio_path: str,
        output_path: str,
    ) -> str:
        """Run MuseTalk via persistent worker (models stay loaded between calls)."""
        musetalk_dir: Path = self._musetalk_dir  # type: ignore[assignment]

        # Per-avatar face-coordinate cache (saves face-detection on repeat calls)
        avatar_id = hashlib.md5(str(Path(avatar_path).resolve()).encode()).hexdigest()
        coord_cache = str(musetalk_dir / "results" / "coords" / f"{avatar_id}.pkl")
        os.makedirs(os.path.dirname(coord_cache), exist_ok=True)

        await self._worker_infer(avatar_path, audio_path, output_path, coord_cache)

        logger.info(f"MuseTalk animation done: {output_path}")
        return output_path

    # ── Simple ffmpeg fallback ────────────────────────────────────────────────

    async def _animate_simple(
        self,
        avatar_path: str,
        audio_path: str,
        output_path: str,
    ) -> str:
        """Combine template (image or looped video) + audio with FFmpeg. No lip-sync."""
        logger.info("Using simple animation (template + audio, no lip-sync)")

        is_video = Path(avatar_path).suffix.lower() in {".mp4", ".mov", ".webm", ".avi", ".mkv"}
        # Image templates loop via the image demuxer; video templates loop the
        # whole clip (-stream_loop -1) and -shortest trims to the audio.
        input_args = (
            ["-stream_loop", "-1", "-i", str(avatar_path)]
            if is_video
            else ["-loop", "1", "-i", str(avatar_path)]
        )

        cmd = [
            "ffmpeg",
            "-y",
            *input_args,
            "-i",
            str(audio_path),
            "-c:v",
            "libx264",
            *([] if is_video else ["-tune", "stillimage"]),
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-pix_fmt",
            "yuv420p",
            "-shortest",
            "-vf",
            (
                f"fps={self.fps},"
                f"scale={self.resolution}:{self.resolution}:"
                f"force_original_aspect_ratio=decrease,"
                f"pad={self.resolution}:{self.resolution}:(ow-iw)/2:(oh-ih)/2"
            ),
            output_path,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            err = stderr.decode(errors="replace")
            logger.error(f"FFmpeg error:\n{err}")
            raise RuntimeError("Simple animation (ffmpeg) failed")

        logger.info(f"Simple animation done: {output_path}")
        return output_path

    # ── helpers ───────────────────────────────────────────────────────────────

    def generate_cache_key(self, text: str, avatar_id: str) -> str:
        return hashlib.md5(f"{avatar_id}:{text}".encode()).hexdigest()


# Global instance
avatar_animator = AvatarAnimator()
