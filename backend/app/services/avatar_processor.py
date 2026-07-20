import logging
from pathlib import Path
from typing import Optional, Tuple

import cv2
import numpy as np
from PIL import Image

from app.config import settings

logger = logging.getLogger(__name__)


class AvatarProcessor:
    """Process and prepare avatar images for animation"""

    def __init__(self):
        self.resolution = settings.AVATAR_RESOLUTION

    async def process_image(self, image_path: str, output_path: str) -> Tuple[str, dict]:
        """
        Process uploaded avatar image

        Args:
            image_path: Path to input image
            output_path: Path to save processed image

        Returns:
            Tuple of (output_path, metadata)
        """
        try:
            logger.info(f"Processing avatar image: {image_path}")

            # Load image
            image = Image.open(image_path)

            # Convert to RGB
            if image.mode != "RGB":
                image = image.convert("RGB")

            # Get original dimensions
            orig_width, orig_height = image.size

            # Detect face and crop
            face_box = await self._detect_face(np.array(image))

            if face_box:
                x, y, w, h = face_box
                # Add padding
                padding = int(min(w, h) * 0.3)
                x = max(0, x - padding)
                y = max(0, y - padding)
                w = min(orig_width - x, w + 2 * padding)
                h = min(orig_height - y, h + 2 * padding)

                # Crop to face
                image = image.crop((x, y, x + w, y + h))
                logger.info(f"Face detected and cropped: {face_box}")
            else:
                logger.warning("No face detected, using center crop")
                # Center crop
                size = min(orig_width, orig_height)
                left = (orig_width - size) // 2
                top = (orig_height - size) // 2
                image = image.crop((left, top, left + size, top + size))

            # Resize to target resolution
            image = image.resize((self.resolution, self.resolution), Image.Resampling.LANCZOS)

            # Enhance image
            image = await self._enhance_image(image)

            # Save processed image
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            image.save(output_path, quality=95)

            # Create thumbnail
            thumbnail_path = output_path.replace(".", "_thumb.")
            thumbnail = image.copy()
            thumbnail.thumbnail((256, 256), Image.Resampling.LANCZOS)
            thumbnail.save(thumbnail_path, quality=85)

            metadata = {
                "original_size": (orig_width, orig_height),
                "processed_size": (self.resolution, self.resolution),
                "face_detected": face_box is not None,
                "thumbnail_path": thumbnail_path,
            }

            logger.info(f"Avatar processed successfully: {output_path}")
            return output_path, metadata

        except Exception as e:
            logger.error(f"Failed to process avatar image: {e}")
            raise

    async def process_video(self, video_path: str, thumbnail_path: str) -> Tuple[str, dict]:
        """
        Validate an uploaded video template and derive a thumbnail from its
        first frame. Unlike images, the video itself is stored UNMODIFIED —
        the MuseTalk worker extracts/scales frames at its own fps, so any
        re-encode here would just cost quality.

        Returns (thumbnail_path, metadata). Raises ValueError on anything
        that isn't a readable video.
        """
        logger.info(f"Processing avatar video template: {video_path}")

        cap = cv2.VideoCapture(video_path)
        try:
            if not cap.isOpened():
                raise ValueError("File is not a readable video")

            fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
            frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            duration = (frame_count / fps) if fps > 0 else 0.0

            ok, first_frame = cap.read()
            if not ok or first_frame is None:
                raise ValueError("Could not decode the first video frame")
        finally:
            cap.release()

        # Face check on the first frame — warn-only (same policy as images):
        # a template the face tracker can't use fails loudly at animation
        # time anyway, and lighting can defeat the cheap Haar check.
        rgb = cv2.cvtColor(first_frame, cv2.COLOR_BGR2RGB)
        face_box = await self._detect_face(rgb)
        if face_box is None:
            logger.warning("No face detected in video template's first frame")

        thumb = Image.fromarray(rgb)
        thumb.thumbnail((256, 256), Image.Resampling.LANCZOS)
        Path(thumbnail_path).parent.mkdir(parents=True, exist_ok=True)
        thumb.save(thumbnail_path, quality=85)

        metadata = {
            "is_video_template": True,
            "duration_seconds": round(duration, 2),
            "fps": round(fps, 2),
            "original_size": (width, height),
            "face_detected": face_box is not None,
            "thumbnail_path": thumbnail_path,
        }
        logger.info(
            f"Video template ok: {duration:.1f}s @ {fps:.0f}fps {width}x{height}, "
            f"face={'yes' if face_box else 'no'}"
        )
        return thumbnail_path, metadata

    async def _detect_face(self, image: np.ndarray) -> Optional[Tuple[int, int, int, int]]:
        """Detect face in image using OpenCV"""
        try:
            # Load face cascade
            face_cascade = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            )

            # Convert to grayscale
            gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)

            # Detect faces
            faces = face_cascade.detectMultiScale(
                gray, scaleFactor=1.1, minNeighbors=5, minSize=(100, 100)
            )

            if len(faces) > 0:
                # Return largest face
                return tuple(max(faces, key=lambda f: f[2] * f[3]))

            return None

        except Exception as e:
            logger.warning(f"Face detection error: {e}")
            return None

    async def _enhance_image(self, image: Image.Image) -> Image.Image:
        """Enhance image quality"""
        try:
            from PIL import ImageEnhance

            # Slightly enhance sharpness
            enhancer = ImageEnhance.Sharpness(image)
            image = enhancer.enhance(1.1)

            # Slightly enhance contrast
            enhancer = ImageEnhance.Contrast(image)
            image = enhancer.enhance(1.05)

            return image

        except Exception as e:
            logger.warning(f"Image enhancement error: {e}")
            return image

    async def create_thumbnail(
        self, image_path: str, thumbnail_path: str, size: Tuple[int, int] = (256, 256)
    ) -> str:
        """Create thumbnail from image"""
        try:
            image = Image.open(image_path)
            image.thumbnail(size, Image.Resampling.LANCZOS)
            image.save(thumbnail_path, quality=85)
            return thumbnail_path

        except Exception as e:
            logger.error(f"Failed to create thumbnail: {e}")
            raise


# Global instance
avatar_processor = AvatarProcessor()
