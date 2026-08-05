"""Optional video understanding for the chatbot's @mention replies."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from google import genai

logger = logging.getLogger("reminderbot.video")

VideoMode = Literal["inline", "files_api", "frames", "youtube"]

# --- Configuration -----------------------------------------------------------

def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


CHATBOT_VIDEO_ENABLED = _env_bool("CHATBOT_VIDEO_ENABLED", False)
CHATBOT_VIDEO_INLINE_ENABLED = _env_bool("CHATBOT_VIDEO_INLINE_ENABLED", True)
CHATBOT_VIDEO_FRAMES_ENABLED = _env_bool("CHATBOT_VIDEO_FRAMES_ENABLED", True)
CHATBOT_VIDEO_FILES_API_ENABLED = _env_bool("CHATBOT_VIDEO_FILES_API_ENABLED", False)
CHATBOT_VIDEO_YOUTUBE_ENABLED = _env_bool("CHATBOT_VIDEO_YOUTUBE_ENABLED", False)

CHATBOT_VIDEO_MAX_CALLS_PER_DAY = int(os.getenv("CHATBOT_VIDEO_MAX_CALLS_PER_DAY", "10"))
CHATBOT_VIDEO_MAX_PER_MESSAGE = int(os.getenv("CHATBOT_VIDEO_MAX_PER_MESSAGE", "1"))
CHATBOT_VIDEO_MAX_SIZE_MB = int(os.getenv("CHATBOT_VIDEO_MAX_SIZE_MB", "10"))
CHATBOT_VIDEO_INLINE_MAX_MB = int(os.getenv("CHATBOT_VIDEO_INLINE_MAX_MB", "8"))
CHATBOT_VIDEO_MAX_DURATION_SEC = int(os.getenv("CHATBOT_VIDEO_MAX_DURATION_SEC", "30"))
CHATBOT_VIDEO_FRAME_COUNT = int(os.getenv("CHATBOT_VIDEO_FRAME_COUNT", "5"))
CHATBOT_VIDEO_FILES_API_POLL_SEC = int(os.getenv("CHATBOT_VIDEO_FILES_API_POLL_SEC", "3"))

_MAX_SIZE_BYTES = CHATBOT_VIDEO_MAX_SIZE_MB * 1024 * 1024
_INLINE_MAX_BYTES = CHATBOT_VIDEO_INLINE_MAX_MB * 1024 * 1024

_YOUTUBE_PATTERN = re.compile(
    r"(https?://(?:www\.)?(?:youtube\.com/watch\?[^\s]+|youtu\.be/[^\s]+))",
    re.IGNORECASE,
)

_VIDEO_EXTENSIONS: dict[str, str] = {
    ".mp4": "video/mp4",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpg",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".webm": "video/webm",
    ".wmv": "video/x-ms-wmv",
    ".flv": "video/x-flv",
    ".3gp": "video/3gpp",
    ".3gpp": "video/3gpp",
}

_ffmpeg_available_cache: bool | None = None


@dataclass
class VideoInput:
    """Prepared video media for one chat reply."""

    mode: VideoMode | None = None
    inline: tuple[bytes, str] | None = None
    file_uri: str | None = None
    frames: list[tuple[bytes, str]] | None = None
    error_message: str | None = None
    uses_video_budget: bool = False


def chatbot_video_is_on() -> bool:
    """Whether video understanding is on (env default + live /settings override)."""
    import db

    saved = db.get_setting("chatbot_video_enabled", "")
    return saved == "on" if saved else CHATBOT_VIDEO_ENABLED


def video_enabled() -> bool:
    return chatbot_video_is_on()


def inline_enabled() -> bool:
    return CHATBOT_VIDEO_INLINE_ENABLED


def frames_enabled() -> bool:
    return CHATBOT_VIDEO_FRAMES_ENABLED


def files_api_enabled() -> bool:
    return CHATBOT_VIDEO_FILES_API_ENABLED


def youtube_enabled() -> bool:
    return CHATBOT_VIDEO_YOUTUBE_ENABLED


def any_attachment_method_enabled() -> bool:
    return inline_enabled() or frames_enabled() or files_api_enabled()


def extract_youtube_url(text: str) -> str | None:
    match = _YOUTUBE_PATTERN.search(text.strip())
    return match.group(1) if match else None


def is_video_attachment(content_type: str | None, filename: str) -> bool:
    if content_type and content_type.startswith("video/"):
        return True
    ext = os.path.splitext(filename.lower())[1]
    return ext in _VIDEO_EXTENSIONS


def guess_video_mime(content_type: str | None, filename: str) -> str:
    if content_type and content_type.startswith("video/"):
        return content_type
    ext = os.path.splitext(filename.lower())[1]
    return _VIDEO_EXTENSIONS.get(ext, "video/mp4")


def ffmpeg_available() -> bool:
    global _ffmpeg_available_cache
    if _ffmpeg_available_cache is None:
        _ffmpeg_available_cache = (
            shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
        )
        if not _ffmpeg_available_cache and frames_enabled():
            logger.warning(
                "CHATBOT_VIDEO_FRAMES_ENABLED is on but ffmpeg/ffprobe was not found on PATH"
            )
    return _ffmpeg_available_cache


def _run_subprocess(args: list[str], input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        input=input_bytes,
        capture_output=True,
        check=False,
    )


def get_video_duration_sec(video_bytes: bytes, mime: str) -> float | None:
    if not ffmpeg_available():
        return None
    suffix = ".mp4" if "mp4" in mime else ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(video_bytes)
        tmp_path = tmp.name
    try:
        result = _run_subprocess(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                tmp_path,
            ]
        )
        if result.returncode != 0:
            logger.debug("ffprobe failed: %s", result.stderr.decode(errors="replace"))
            return None
        return float(result.stdout.decode().strip())
    except (ValueError, OSError) as exc:
        logger.debug("Could not read video duration: %s", exc)
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def extract_key_frames(
    video_bytes: bytes,
    mime: str,
    count: int | None = None,
) -> list[tuple[bytes, str]]:
    if not ffmpeg_available():
        return []
    frame_count = count if count is not None else CHATBOT_VIDEO_FRAME_COUNT
    suffix = ".mp4" if "mp4" in mime else ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(video_bytes)
        tmp_path = tmp.name

    frames: list[tuple[bytes, str]] = []
    try:
        duration = get_video_duration_sec(video_bytes, mime) or 1.0
        for index in range(frame_count):
            timestamp = duration * (index + 1) / (frame_count + 1)
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as frame_tmp:
                frame_path = frame_tmp.name
            try:
                result = _run_subprocess(
                    [
                        "ffmpeg",
                        "-y",
                        "-ss",
                        f"{timestamp:.3f}",
                        "-i",
                        tmp_path,
                        "-frames:v",
                        "1",
                        "-q:v",
                        "2",
                        frame_path,
                    ]
                )
                if result.returncode != 0:
                    logger.debug(
                        "ffmpeg frame extract failed at %.2fs: %s",
                        timestamp,
                        result.stderr.decode(errors="replace"),
                    )
                    continue
                with open(frame_path, "rb") as frame_file:
                    frames.append((frame_file.read(), "image/jpeg"))
            finally:
                try:
                    os.unlink(frame_path)
                except OSError:
                    pass
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return frames


def prepare_video_for_gemini(
    video_bytes: bytes,
    mime: str,
    filename: str,
) -> VideoInput:
    """Pick a processing method for one Discord video attachment."""
    if not any_attachment_method_enabled():
        return VideoInput(
            error_message="Video understanding is not configured on this bot.",
        )

    size = len(video_bytes)
    if size > _MAX_SIZE_BYTES:
        return VideoInput(
            error_message=(
                f"That video is too big for me (max {CHATBOT_VIDEO_MAX_SIZE_MB} MB)."
            ),
        )

    duration = get_video_duration_sec(video_bytes, mime)
    if duration is not None and duration > CHATBOT_VIDEO_MAX_DURATION_SEC:
        return VideoInput(
            error_message=(
                f"That video is too long for me (max {CHATBOT_VIDEO_MAX_DURATION_SEC}s)."
            ),
        )

    if inline_enabled() and size <= _INLINE_MAX_BYTES:
        return VideoInput(
            mode="inline",
            inline=(video_bytes, mime),
            uses_video_budget=True,
        )

    if files_api_enabled():
        return VideoInput(
            mode="files_api",
            inline=(video_bytes, mime),
            uses_video_budget=True,
        )

    if frames_enabled() and ffmpeg_available():
        frames = extract_key_frames(video_bytes, mime)
        if frames:
            return VideoInput(
                mode="frames",
                frames=frames,
                uses_video_budget=True,
            )
        return VideoInput(
            error_message="I couldn't pull frames from that video.",
        )

    if size > _INLINE_MAX_BYTES:
        return VideoInput(
            error_message=(
                f"That video is too big for inline mode (max {CHATBOT_VIDEO_INLINE_MAX_MB} MB)."
            ),
        )

    return VideoInput(
        error_message="Video understanding is not configured for this clip.",
    )


async def upload_to_files_api(
    client: genai.Client,
    video_bytes: bytes,
    mime: str,
    filename: str,
) -> tuple[str, str]:
    """Upload video bytes, wait until ACTIVE, return (uri, file_name)."""
    from google.genai import types

    suffix = os.path.splitext(filename)[1] or ".mp4"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(video_bytes)
        tmp_path = tmp.name

    try:
        uploaded = await asyncio.to_thread(
            client.files.upload,
            file=tmp_path,
            config=types.UploadFileConfig(mime_type=mime),
        )
        while uploaded.state.name == "PROCESSING":
            await asyncio.sleep(CHATBOT_VIDEO_FILES_API_POLL_SEC)
            uploaded = await asyncio.to_thread(client.files.get, name=uploaded.name)
        if uploaded.state.name != "ACTIVE":
            raise RuntimeError(f"File processing failed: {uploaded.state.name}")
        return uploaded.uri, uploaded.name
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def delete_uploaded_file(client: genai.Client, file_name: str) -> None:
    try:
        await asyncio.to_thread(client.files.delete, name=file_name)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not delete uploaded video file %s: %s", file_name, exc)


def prepare_youtube_input(url: str) -> VideoInput:
    return VideoInput(
        mode="youtube",
        file_uri=url,
        uses_video_budget=True,
    )
