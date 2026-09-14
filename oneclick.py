from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from loguru import logger


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate one 9:16 (1080x1920) short video from a topic. "
            "Uses a local script template, Edge TTS, local clips or title cards, "
            "and optional bundled BGM. No paid API is required."
        ),
        epilog=(
            "Example:\n"
            '  uv run python oneclick.py "How AI is changing everyday life"\n'
            "  python oneclick.py 人工智能如何改变日常生活\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "topic",
        help="single topic string used to write the narration and title cards",
    )
    parser.add_argument(
        "--script",
        default="",
        help="optional complete narration; skips the local template and LLM",
    )
    parser.add_argument(
        "--no-bgm",
        action="store_true",
        help="force silent background even if resource/songs has files",
    )
    return parser.parse_args(argv)


def run_oneclick(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    from app.services import mvp
    from app.services import task as tm
    from app.utils import utils

    try:
        params, meta = mvp.build_video_params(
            args.topic,
            script=args.script,
            bgm_type="" if args.no_bgm else None,
        )
    except (ValueError, RuntimeError, OSError) as exc:
        logger.error(f"invalid oneclick input: {exc}")
        return 2

    task_id = utils.get_uuid()
    logger.info(
        "start oneclick task: "
        f"task_id={task_id}, script_source={meta['script_source']}, "
        f"visual_source={meta['visual_source']}, bgm={meta['bgm_type']}"
    )
    try:
        result = tm.start(
            task_id=task_id,
            params=params,
            stop_at="video",
            allow_server_file_input=True,
        )
    except Exception as exc:
        logger.exception(f"oneclick task failed: task_id={task_id}, error={exc}")
        return 1

    if not result or result.get("state") == tm.const.TASK_STATE_FAILED:
        failed_stage = result.get("failed_stage", "unknown") if result else "unknown"
        error = result.get("error", "unknown task error") if result else "empty result"
        logger.error(
            f"oneclick task failed: task_id={task_id}, "
            f"stage={failed_stage}, error={error}"
        )
        return 1

    videos = result.get("videos") or []
    video_path = videos[0] if videos else ""
    print(
        json.dumps(
            {
                "task_id": task_id,
                "video": video_path,
                "script_source": meta["script_source"],
                "visual_source": meta["visual_source"],
                "bgm_type": meta["bgm_type"],
                "voice_name": meta["voice_name"],
                "result": result,
            },
            ensure_ascii=False,
        )
    )
    return 0


def _force_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            continue


if __name__ == "__main__":
    _force_utf8_console()
    raise SystemExit(run_oneclick())
