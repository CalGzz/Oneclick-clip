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


def _escape_drawtext(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
    )


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


def generate_title_cards(
    topic: str,
    output_dir: str,
    *,
    count: int = TITLE_CARD_COUNT,
    duration: float = TITLE_CARD_DURATION_SECONDS,
) -> list[str]:
    """Render 1080x1920 color/title cards with ffmpeg. Color-only if drawtext fails."""
    os.makedirs(output_dir, exist_ok=True)
    ffmpeg = utils.get_ffmpeg_binary()
    title = _short_title(topic)
    font_path = _title_font_path(topic)
    paths: list[str] = []

    for index in range(count):
        color = TITLE_CARD_COLORS[index % len(TITLE_CARD_COLORS)]
        output_path = os.path.join(output_dir, f"mvp-title-{uuid4().hex}.mp4")
        color_input = f"color=c=0x{color}:s=1080x1920:d={duration}:r=30"
        command = [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            color_input,
        ]
        text_file = ""
        if font_path and title:
            text_file = os.path.join(output_dir, f"mvp-title-{uuid4().hex}.txt")
            with open(text_file, "w", encoding="utf-8") as handle:
                handle.write(title)
            drawtext = (
                f"drawtext=fontfile={_escape_drawtext(font_path)}:"
                f"textfile={_escape_drawtext(text_file)}:"
                "fontsize=64:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2:"
                "expansion=none:borderw=4:bordercolor=black"
            )
            command.extend(["-vf", drawtext])
        command.extend(
            [
                "-pix_fmt",
                "yuv420p",
                "-r",
                "30",
                "-movflags",
                "+faststart",
                output_path,
            ]
        )

        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning(f"title card ffmpeg failed: {exc}")
            if os.path.exists(output_path):
                os.remove(output_path)
            if text_file and os.path.exists(text_file):
                os.remove(text_file)
            continue

        if text_file and os.path.exists(text_file):
            os.remove(text_file)

        if completed.returncode != 0 or not os.path.isfile(output_path):
            logger.warning(
                "title card with text failed, retrying color-only: "
                f"{completed.stderr[-400:] if completed.stderr else completed.returncode}"
            )
            if os.path.exists(output_path):
                os.remove(output_path)
            color_only = [
                ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                color_input,
                "-pix_fmt",
                "yuv420p",
                "-r",
                "30",
                "-movflags",
                "+faststart",
                output_path,
            ]
            retry = subprocess.run(
                color_only,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if retry.returncode != 0 or not os.path.isfile(output_path):
                logger.error(
                    "color-only title card failed: "
                    f"{retry.stderr[-400:] if retry.stderr else retry.returncode}"
                )
                continue

        paths.append(output_path)

    if not paths:
        raise RuntimeError(
            "failed to generate title cards; install ffmpeg and retry"
        )
    return paths


def prepare_visuals(topic: str) -> tuple[list[MaterialInfo], str]:
    """Use resource/clips when present; otherwise write local title cards."""
    local_dir = utils.storage_dir("local_videos", create=True)
    clips = list_local_clips()
    if clips:
        materials: list[MaterialInfo] = []
        for source_path in clips:
            extension = Path(source_path).suffix.lower()
            target_path = os.path.join(
                local_dir, f"mvp-clip-{uuid4().hex}{extension}"
            )
            shutil.copy2(source_path, target_path)
            materials.append(
                MaterialInfo(provider="local", url=target_path, duration=0)
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
