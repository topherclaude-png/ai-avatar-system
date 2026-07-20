"""
Persistent MuseTalk worker.

Lifecycle:
  1. Start: reads one JSON line {
       "unet_model_path": ...,
       "unet_config": ...,
       "whisper_dir": ...,
       "vae_type": ...,
       "use_float16": true/false   <- optional, default false (enable on GPU for ~2x speedup)
     }
  2. Loads all models once, prints "READY\\n" to stdout.
  3. Loop: reads one JSON job line {
           "image": ..., "audio": ...,
           "output": ..., "coord_cache": ...}
           runs inference, prints {"status":"ok","output":...}\\n
           or {"status":"error","msg":...}\\n

The "image" field is the avatar TEMPLATE and may be a still image OR a
short video clip (detected by extension). A video template supplies real
idle motion — blinks, micro-movement — that a still photo cannot: frames
are extracted at FPS, face-tracked, VAE-encoded once, and cycled
(forward+reverse ping-pong) under the generated mouths. A rolling frame
offset carries the cycle position across jobs so consecutive sentence
chunks continue the idle motion instead of restarting it.

All paths may be absolute or relative to cwd (the MuseTalk repo root).
"""
import sys, os, glob, json, copy, pickle, shutil, traceback
from typing import Any, Dict, List
import cv2, numpy as np, torch
from omegaconf import OmegaConf
from transformers import WhisperModel

from musetalk.utils.blending import get_image
from musetalk.utils.face_parsing import FaceParsing
from musetalk.utils.audio_processor import AudioProcessor
from musetalk.utils.utils import get_file_type, get_video_fps, datagen, load_all_model
from musetalk.utils.preprocessing import get_landmark_and_bbox, read_imgs, coord_placeholder

FPS = 25
EXTRA_MARGIN = 10
BATCH_SIZE = 8
AUDIO_PAD_L = 2
AUDIO_PAD_R = 2

# Video-template bounds: 10 s at 25 fps. Caps one-time prep cost (landmarks +
# VAE per frame) and steady-state RAM (frames are held decoded for blending).
MAX_TEMPLATE_FRAMES = 250
# Templates are downscaled to at most this height on extraction — output
# video matches template resolution, and >720p bloats RAM/latency for no
# visible gain at kiosk viewing distance.
MAX_TEMPLATE_HEIGHT = 720

_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".avi", ".mkv"}

# Prepared-template cache. Single entry by design: the kiosk drives exactly
# one avatar, and each prepared video template holds decoded frames +
# latents (potentially ~0.5 GB) — caching several would bloat RAM.
_template_cache: Dict[Any, Dict[str, Any]] = {}


def _reply(obj: dict):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _is_video(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in _VIDEO_EXTS


def _extract_template_frames(video_path: str, frames_dir: str) -> List[str]:
    """Extract up to MAX_TEMPLATE_FRAMES at FPS, capped to MAX_TEMPLATE_HEIGHT."""
    os.makedirs(frames_dir, exist_ok=True)
    rc = os.system(
        f'ffmpeg -y -v warning -i "{video_path}" '
        f'-vf "fps={FPS},scale=-2:\'min({MAX_TEMPLATE_HEIGHT},ih)\'" '
        f'-frames:v {MAX_TEMPLATE_FRAMES} "{frames_dir}/%08d.png"'
    )
    frames = sorted(glob.glob(os.path.join(frames_dir, "*.png")))
    if rc != 0 or not frames:
        raise RuntimeError(f"Could not extract frames from template video: {video_path}")
    return frames


def _prepare_template(template_path: str, coord_cache: str, vae) -> Dict[str, Any]:
    """
    One-time (per template, per worker lifetime) preparation: frame list,
    face coordinates, VAE latents, and the ping-pong cycles. Jobs after the
    first reuse everything and only run audio + UNet + blending.
    """
    try:
        mtime = os.path.getmtime(template_path)
    except OSError:
        mtime = 0
    key = (os.path.abspath(template_path), mtime)
    cached = _template_cache.get(key)
    if cached is not None:
        return cached

    if _is_video(template_path):
        # Persistent extraction dir next to the coord cache — survives worker
        # restarts so only landmark/VAE prep repeats, not ffmpeg extraction.
        base = coord_cache or (template_path + ".cache")
        frames_dir = base + "_tplframes"
        frames = sorted(glob.glob(os.path.join(frames_dir, "*.png")))
        if not frames:
            frames = _extract_template_frames(template_path, frames_dir)
        input_img_list = frames
    else:
        input_img_list = [template_path]

    # ── face coordinates (pickle-cached; must match the frame count) ─────────
    coord_list: List[Any] = []
    frame_list: List[Any] = []
    if coord_cache and os.path.exists(coord_cache):
        with open(coord_cache, "rb") as f:
            cached_coords = list(pickle.load(f))
        if len(cached_coords) == len(input_img_list):
            coord_list = cached_coords
            frame_list = list(read_imgs(input_img_list))
    if not coord_list:
        _coords, _frames = get_landmark_and_bbox(input_img_list, 0)
        coord_list, frame_list = list(_coords), list(_frames)
        if coord_cache:
            os.makedirs(os.path.dirname(coord_cache), exist_ok=True)
            with open(coord_cache, "wb") as f:
                pickle.dump(coord_list, f)

    if not frame_list or all(c == coord_placeholder for c in coord_list):
        raise RuntimeError("No face detected in avatar template")

    # Drop frames where tracking failed so coords/frames/latents stay aligned.
    tracked = [
        (c, f) for c, f in zip(coord_list, frame_list) if c != coord_placeholder
    ]
    coord_list = [c for c, _ in tracked]
    frame_list = [f for _, f in tracked]

    # ── VAE-encode every template frame once ─────────────────────────────────
    input_latent_list = []
    for bbox, frame in zip(coord_list, frame_list):
        x1, y1, x2, y2 = bbox
        y2 = min(y2 + EXTRA_MARGIN, frame.shape[0])
        crop = cv2.resize(frame[y1:y2, x1:x2], (256, 256), interpolation=cv2.INTER_LANCZOS4)
        input_latent_list.append(vae.get_latents_for_unet(crop))  # type: ignore[arg-type]

    if not input_latent_list:
        raise RuntimeError("No valid face crops produced")

    prep = {
        # Ping-pong cycles: forward + reversed = seamless loop with no cut
        "frame_cycle": frame_list + list(reversed(frame_list)),
        "coord_cycle": coord_list + list(reversed(coord_list)),
        "latent_cycle": input_latent_list + list(reversed(input_latent_list)),
        # Rolling start position — carries idle motion across jobs
        "offset": 0,
    }
    _template_cache.clear()
    _template_cache[key] = prep
    sys.stderr.write(
        f"INFO: template prepared ({len(frame_list)} frame(s), "
        f"{'video' if len(frame_list) > 1 else 'still image'})\n"
    )
    sys.stderr.flush()
    return prep


def _run_job(job, vae, unet, pe, audio_processor, whisper, fp, timesteps, device):
    template_path = job["image"]  # still image OR video template
    audio_path    = job["audio"]
    output_path   = job["output"]
    coord_cache   = job.get("coord_cache")

    prep = _prepare_template(template_path, coord_cache, vae)
    cycle_len = len(prep["latent_cycle"])
    offset = prep["offset"] % cycle_len

    # Rotate the cycles so this job continues where the previous one stopped —
    # consecutive sentence chunks flow through the idle motion instead of
    # every chunk snapping back to frame 0.
    def _rot(lst):
        return lst[offset:] + lst[:offset]

    frame_list_cycle  = _rot(prep["frame_cycle"])
    coord_list_cycle  = _rot(prep["coord_cycle"])
    latent_list_cycle = _rot(prep["latent_cycle"])

    # ── audio features ───────────────────────────────────────────────────────
    weight_dtype = unet.model.dtype
    whisper_input_features, librosa_length = audio_processor.get_audio_feature(audio_path)
    whisper_chunks = audio_processor.get_whisper_chunk(
        whisper_input_features, device, weight_dtype, whisper, librosa_length,
        fps=FPS,
        audio_padding_length_left=AUDIO_PAD_L,
        audio_padding_length_right=AUDIO_PAD_R,
    )

    # ── UNet inference ───────────────────────────────────────────────────────
    gen = datagen(
        whisper_chunks=whisper_chunks,
        vae_encode_latents=latent_list_cycle,
        batch_size=BATCH_SIZE,
        delay_frame=0,
        device=device,
    )
    res_frame_list = []
    for whisper_batch, latent_batch in gen:
        audio_feat = pe(whisper_batch)
        latent_batch = latent_batch.to(dtype=weight_dtype)
        pred = unet.model(latent_batch, timesteps, encoder_hidden_states=audio_feat).sample
        for f in vae.decode_latents(pred):
            res_frame_list.append(f)

    # ── blend back ───────────────────────────────────────────────────────────
    frames_dir = output_path + "_frames"
    os.makedirs(frames_dir, exist_ok=True)
    for i, res_frame in enumerate(res_frame_list):
        bbox = coord_list_cycle[i % len(coord_list_cycle)]
        ori  = copy.deepcopy(frame_list_cycle[i % len(frame_list_cycle)])
        x1, y1, x2, y2 = bbox
        y2 = min(y2 + EXTRA_MARGIN, ori.shape[0])
        try:
            res_frame = cv2.resize(res_frame.astype(np.uint8), (x2-x1, y2-y1))
        except Exception:
            continue
        combined = get_image(ori, res_frame, [x1, y1, x2, y2], mode="jaw", fp=fp)
        cv2.imwrite(f"{frames_dir}/{str(i).zfill(8)}.png", combined)

    # Advance the rolling offset for the next job
    prep["offset"] = (offset + len(res_frame_list)) % cycle_len

    # ── assemble video ───────────────────────────────────────────────────────
    tmp_vid = output_path + ".tmp.mp4"
    os.system(f"ffmpeg -y -v warning -r {FPS} -f image2 "
              f"-i {frames_dir}/%08d.png "
              f"-vcodec libx264 -vf format=yuv420p -crf 18 {tmp_vid}")
    os.system(f"ffmpeg -y -v warning -i {audio_path} -i {tmp_vid} {output_path}")
    shutil.rmtree(frames_dir)
    os.remove(tmp_vid)


def main():
    # ── read init config ──────────────────────────────────────────────────────
    init = json.loads(sys.stdin.readline())

    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    use_float16 = init.get("use_float16", False)

    vae, unet, pe = load_all_model(
        unet_model_path=init["unet_model_path"],
        vae_type=init["vae_type"],
        unet_config=init["unet_config"],
        device=device,
    )

    # float16 on GPU = ~2× faster via Tensor Cores (A10G / L4 / V100 all support it)
    if use_float16 and device.type == "cuda":
        pe         = pe.half()
        vae.vae    = vae.vae.half()
        unet.model = unet.model.half()
        sys.stderr.write("INFO: float16 enabled — ~2× faster on GPU\n")
        sys.stderr.flush()

    pe         = pe.to(device)
    vae.vae    = vae.vae.to(device)
    unet.model = unet.model.to(device)

    weight_dtype = unet.model.dtype
    audio_processor = AudioProcessor(feature_extractor_path=init["whisper_dir"])
    whisper = WhisperModel.from_pretrained(init["whisper_dir"])
    whisper = whisper.to(device=device, dtype=weight_dtype).eval()
    whisper.requires_grad_(False)

    fp = FaceParsing()
    timesteps = torch.tensor([0], device=device)

    sys.stdout.write("READY\n")
    sys.stdout.flush()

    # ── job loop ──────────────────────────────────────────────────────────────
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            job = json.loads(raw)
        except json.JSONDecodeError:
            continue
        job_id = job.get("job_id")
        try:
            _run_job(job, vae, unet, pe, audio_processor, whisper, fp, timesteps, device)
            _reply({"status": "ok", "output": job["output"], "job_id": job_id})
        except Exception as e:
            _reply(
                {"status": "error", "msg": str(e), "tb": traceback.format_exc(), "job_id": job_id}
            )


if __name__ == "__main__":
    main()
