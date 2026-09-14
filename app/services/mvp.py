"""Local MVP helpers for one-topic, no-paid-API video generation.

This module prepares a VideoParams payload for the existing task pipeline:
local script template (optional LLM only when an env var is already set),
Edge TTS voice, local clips or generated title cards, and optional BGM.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

from loguru import logger

from app.models import const
from app.models.schema import MaterialInfo, VideoParams
from app.services import bgm as bgm_service
from app.utils import utils


DEFAULT_EDGE_VOICE_ZH = "zh-CN-XiaoxiaoNeural-Female"
DEFAULT_EDGE_VOICE_EN = "en-US-JennyNeural-Female"
CLIP_DIR_NAME = "clips"
TITLE_CARD_DURATION_SECONDS = 8.0
TITLE_CARD_COUNT = 3
TITLE_CARD_COLORS = ("1a1a2e", "16213e", "0f3460")
PORTRAIT_WIDTH = 1080
PORTRAIT_HEIGHT = 1920
_STILL_SCALE_FILTER = (
    f"scale={PORTRAIT_WIDTH}:{PORTRAIT_HEIGHT}:force_original_aspect_ratio=decrease,"
    f"pad={PORTRAIT_WIDTH}:{PORTRAIT_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
)
# Only honor already-present env vars. Do not read paid keys from config.toml.
OPTIONAL_LLM_ENV_VARS = (
    "OPENAI_API_KEY",
    "LLM_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "DASHSCOPE_API_KEY",
    "MOONSHOT_API_KEY",
    "DEEPSEEK_API_KEY",
)
_CLIP_EXTENSIONS = {
    f".{ext}" for ext in (*const.FILE_TYPE_VIDEOS, *const.FILE_TYPE_IMAGES)
}
_STILL_EXTENSIONS = {f".{ext}" for ext in const.FILE_TYPE_IMAGES}


def looks_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def default_edge_voice(topic: str) -> str:
    return DEFAULT_EDGE_VOICE_ZH if looks_cjk(topic) else DEFAULT_EDGE_VOICE_EN


def build_template_script(topic: str) -> str:
    """Build a ~15-30 second narration from a topic. No network, no LLM."""
    cleaned = " ".join((topic or "").split())
    if not cleaned:
        raise ValueError("topic cannot be empty")

    if looks_cjk(cleaned):
        return (
            f"{cleaned}。接下来二十秒，把这件事讲清楚。"
            f"先抓住核心：它为什么重要，以及现在就该注意什么。"
            f"再用一个具体场景说明，你会立刻知道怎么用。"
            f"最后只记一句：把今天的重点带走，下一步就能行动。"
        )
    return (
        f"{cleaned}. In the next twenty seconds, here is the idea that matters. "
        f"First, the core point in plain language, and why it matters right now. "
        f"Then one concrete example you can picture and reuse today. "
        f"Remember this takeaway: keep the main point, then take one clear next step."
    )


def optional_llm_env_present(environ: dict[str, str] | None = None) -> bool:
    source = os.environ if environ is None else environ
    return any(str(source.get(name, "")).strip() for name in OPTIONAL_LLM_ENV_VARS)


def resolve_script(topic: str, explicit_script: str = "") -> tuple[str, str]:
    """Return (script, source) where source is provided, llm, or template."""
    provided = (explicit_script or "").strip()
    if provided:
        return provided, "provided"

    if optional_llm_env_present():
        try:
            from app.services import llm

            generated = llm.generate_script(
                video_subject=topic,
                paragraph_number=1,
            )
        except Exception as exc:
            logger.warning(f"optional LLM script failed; using local template: {exc}")
        else:
            if generated and "Error: " not in generated:
                return generated.strip(), "llm"
            logger.warning("optional LLM returned no usable script; using local template")

    return build_template_script(topic), "template"


def clips_dir() -> str:
    path = utils.resource_dir(CLIP_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def list_local_clips(directory: str | None = None) -> list[str]:
    clips_root = directory or clips_dir()
    if not os.path.isdir(clips_root):
        return []

    found: list[str] = []
    for name in sorted(os.listdir(clips_root)):
        file_path = os.path.join(clips_root, name)
        if not os.path.isfile(file_path):
            continue
        if Path(name).suffix.lower() not in _CLIP_EXTENSIONS:
            continue
        found.append(os.path.realpath(file_path))
    return found


def resolve_bgm_type() -> str:
    """Use bundled or uploaded songs when present; stay silent otherwise."""
    try:
        files = bgm_service.list_bgm_files()
    except Exception as exc:
        logger.warning(f"failed to list background music; continuing silent: {exc}")
        return ""
    return "random" if files else ""


def _short_title(topic: str, max_chars: int = 22) -> str:
    """Fit the topic on up to two title-card lines without dropping spaces."""
    words = " ".join(topic.split()).split()
    if not words:
        return ""

    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > max_chars:
            lines.append(current)
            current = word
            if len(lines) == 2:
                current = ""
                break
        else:
            current = candidate
    if current and len(lines) < 2:
        lines.append(current)
    if len(lines) == 2 and len(lines[1]) > max_chars:
        lines[1] = lines[1][: max_chars - 3].rstrip() + "..."
    return "\n".join(lines)


def _title_font_path(_topic: str) -> str:
    # BeVietnamPro-Bold ships with a zero-width space glyph, so title cards
    # would render English as "HowAI ischanging". STHeitiMedium keeps spaces
    # and also covers CJK topics.
    preferred = (
        os.path.join(utils.font_dir(), "STHeitiMedium.ttc"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    )
    for font_path in preferred:
        if os.path.isfile(font_path):
            return font_path
    return ""


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    return (int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16))


def render_title_card_image(topic: str, output_path: str, color: str) -> None:
    """Paint a 1080x1920 title card. Pillow avoids ffmpeg drawtext/libfreetype."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (PORTRAIT_WIDTH, PORTRAIT_HEIGHT), _hex_to_rgb(color))
    draw = ImageDraw.Draw(image)
    font_path = _title_font_path(topic)
    try:
        font = (
            ImageFont.truetype(font_path, 64)
            if font_path
            else ImageFont.load_default()
        )
    except OSError:
        font = ImageFont.load_default()

    title = _short_title(topic) or " "
    bbox = draw.multiline_textbbox(
        (0, 0), title, font=font, align="center", spacing=16
    )
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    x = (PORTRAIT_WIDTH - text_width) / 2 - bbox[0]
    y = (PORTRAIT_HEIGHT - text_height) / 2 - bbox[1]
    draw.multiline_text(
        (x, y),
        title,
        font=font,
        fill=(255, 255, 255),
        align="center",
        spacing=16,
        stroke_width=4,
        stroke_fill=(0, 0, 0),
    )
    image.save(output_path)


def _encode_still_to_mp4(
    image_path: str,
    output_path: str,
    *,
    duration: float,
    ffmpeg: str,
) -> bool:
    command = [
        ffmpeg,
        "-y",
        "-loop",
        "1",
        "-i",
        image_path,
        "-t",
        str(duration),
        "-vf",
        _STILL_SCALE_FILTER,
        "-pix_fmt",
        "yuv420p",
        "-r",
        "30",
        "-movflags",
        "+faststart",
        output_path,
    ]
    try:
        # Windows locale is often cp1252. ffmpeg stderr can include CJK paths
        # (byte 0x8f etc.); decode as UTF-8 and replace so the reader thread
        # does not raise UnicodeDecodeError.
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError) as exc:
        logger.warning(f"still ffmpeg encode failed: {exc}")
        return False
    if completed.returncode == 0 and os.path.isfile(output_path):
        return True
    logger.warning(
        "still ffmpeg encode failed: "
        f"code={completed.returncode} {(completed.stderr or '')[-500:]}"
    )
    return False


def generate_title_cards(
    topic: str,
    output_dir: str,
    *,
    count: int = TITLE_CARD_COUNT,
    duration: float = TITLE_CARD_DURATION_SECONDS,
) -> list[str]:
    """Render 1080x1920 title cards as PNG, then encode with any ffmpeg build."""
    os.makedirs(output_dir, exist_ok=True)
    ffmpeg = utils.get_ffmpeg_binary()
    paths: list[str] = []

    for index in range(count):
        color = TITLE_CARD_COLORS[index % len(TITLE_CARD_COLORS)]
        stem = uuid4().hex
        image_path = os.path.join(output_dir, f"mvp-title-{stem}.png")
        output_path = os.path.join(output_dir, f"mvp-title-{stem}.mp4")
        try:
            render_title_card_image(topic, image_path, color)
            if not _encode_still_to_mp4(
                image_path, output_path, duration=duration, ffmpeg=ffmpeg
            ):
                logger.error(f"failed to encode title card: {output_path}")
                continue
        finally:
            if os.path.exists(image_path):
                os.remove(image_path)
        paths.append(output_path)

    if not paths:
        raise RuntimeError(
            "failed to generate title cards; install ffmpeg and retry"
        )
    return paths


def _stage_local_clip(source_path: str, local_dir: str, ffmpeg: str) -> str | None:
    """Copy videos as-is; encode listed stills to 1080x1920 MP4 before compose."""
    extension = Path(source_path).suffix.lower()
    if extension in _STILL_EXTENSIONS:
        stem = uuid4().hex
        # ASCII temp input so Windows ffmpeg does not have to open a CJK path.
        ascii_still = os.path.join(local_dir, f"mvp-still-{stem}{extension}")
        target_path = os.path.join(local_dir, f"mvp-clip-{stem}.mp4")
        try:
            shutil.copy2(source_path, ascii_still)
            if not _encode_still_to_mp4(
                ascii_still,
                target_path,
                duration=TITLE_CARD_DURATION_SECONDS,
                ffmpeg=ffmpeg,
            ):
                logger.error(f"failed to encode still clip: {source_path}")
                return None
            return target_path
        finally:
            if os.path.exists(ascii_still):
                os.remove(ascii_still)

    target_path = os.path.join(local_dir, f"mvp-clip-{uuid4().hex}{extension}")
    shutil.copy2(source_path, target_path)
    return target_path


def prepare_visuals(topic: str) -> tuple[list[MaterialInfo], str]:
    """Use resource/clips when present; otherwise write local title cards."""
    local_dir = utils.storage_dir("local_videos", create=True)
    clips = list_local_clips()
    if clips:
        ffmpeg = utils.get_ffmpeg_binary()
        materials: list[MaterialInfo] = []
        for source_path in clips:
            staged_path = _stage_local_clip(source_path, local_dir, ffmpeg)
            if not staged_path:
                continue
            materials.append(
                MaterialInfo(provider="local", url=staged_path, duration=0)
            )
        if not materials:
            raise RuntimeError(
                "failed to prepare local clips; install ffmpeg and retry"
            )
        return materials, "clips"

    cards = generate_title_cards(topic, local_dir)
    return (
        [MaterialInfo(provider="local", url=path, duration=0) for path in cards],
        "title_cards",
    )


def build_video_params(
    topic: str,
    *,
    script: str | None = None,
    voice_name: str | None = None,
    bgm_type: str | None = None,
) -> tuple[VideoParams, dict[str, str]]:
    cleaned_topic = " ".join((topic or "").split())
    if not cleaned_topic:
        raise ValueError("topic cannot be empty")

    video_script, script_source = resolve_script(
        cleaned_topic, explicit_script=script or ""
    )
    materials, visual_source = prepare_visuals(cleaned_topic)
    resolved_bgm = resolve_bgm_type() if bgm_type is None else bgm_type
    params = VideoParams(
        video_subject=cleaned_topic,
        video_script=video_script,
        video_source="local",
        video_materials=materials,
        video_aspect="9:16",
        video_concat_mode="sequential",
        video_clip_duration=int(TITLE_CARD_DURATION_SECONDS),
        voice_name=voice_name or default_edge_voice(cleaned_topic),
        voice_rate=1.0,
        bgm_type=resolved_bgm,
        subtitle_enabled=True,
        font_name="STHeitiMedium.ttc",
    )
    meta = {
        "script_source": script_source,
        "visual_source": visual_source,
        "bgm_type": resolved_bgm or "none",
        "voice_name": params.voice_name or "",
    }
    return params, meta
