import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.models.schema import MaterialInfo
from app.services import mvp
from app.utils import utils
import oneclick


class TestMvpHelpers(unittest.TestCase):
    def test_template_script_uses_topic_and_stays_shortform(self):
        english = mvp.build_template_script("How AI is changing everyday life")
        chinese = mvp.build_template_script("人工智能如何改变日常生活")

        self.assertIn("How AI is changing everyday life", english)
        self.assertIn("人工智能如何改变日常生活", chinese)
        self.assertGreaterEqual(len(english.split()), 40)
        self.assertLessEqual(len(english.split()), 90)
        self.assertGreaterEqual(len(chinese), 40)
        self.assertLessEqual(len(chinese), 160)

    def test_title_card_text_keeps_spaces_on_wrapped_lines(self):
        self.assertEqual(
            mvp._short_title("How AI is changing everyday life"),
            "How AI is changing\neveryday life",
        )

    def test_empty_topic_is_rejected(self):
        with self.assertRaises(ValueError):
            mvp.build_template_script("   ")

    def test_default_voice_follows_script_language(self):
        self.assertEqual(
            mvp.default_edge_voice("咖啡"),
            mvp.DEFAULT_EDGE_VOICE_ZH,
        )
        self.assertEqual(
            mvp.default_edge_voice("Coffee habits"),
            mvp.DEFAULT_EDGE_VOICE_EN,
        )

    def test_llm_is_skipped_without_env_var(self):
        with (
            patch.object(mvp, "optional_llm_env_present", return_value=False),
            patch("app.services.llm.generate_script") as generate,
        ):
            script, source = mvp.resolve_script("Local only topic")

        generate.assert_not_called()
        self.assertEqual(source, "template")
        self.assertIn("Local only topic", script)

    def test_provided_script_wins_over_llm_env(self):
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False),
            patch("app.services.llm.generate_script") as generate,
        ):
            script, source = mvp.resolve_script("topic", explicit_script="Ready script")

        generate.assert_not_called()
        self.assertEqual((script, source), ("Ready script", "provided"))

    def test_optional_llm_is_used_when_env_var_is_present(self):
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False),
            patch(
                "app.services.llm.generate_script",
                return_value="LLM narration about coffee",
            ) as generate,
        ):
            script, source = mvp.resolve_script("coffee")

        generate.assert_called_once()
        self.assertEqual((script, source), ("LLM narration about coffee", "llm"))

    def test_optional_llm_falls_back_to_template_on_failure(self):
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False),
            patch(
                "app.services.llm.generate_script",
                side_effect=RuntimeError("quota"),
            ),
        ):
            script, source = mvp.resolve_script("coffee")

        self.assertEqual(source, "template")
        self.assertIn("coffee", script)

    def test_list_local_clips_ignores_unsupported_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            (Path(temp_dir) / "keep.mp4").write_bytes(b"clip")
            (Path(temp_dir) / "also.png").write_bytes(b"image")
            (Path(temp_dir) / "skip.txt").write_text("nope", encoding="utf-8")
            self.assertEqual(
                [Path(path).name for path in mvp.list_local_clips(temp_dir)],
                ["also.png", "keep.mp4"],
            )

    def test_resolve_bgm_type_is_silent_without_songs(self):
        with patch("app.services.bgm.list_bgm_files", return_value=[]):
            self.assertEqual(mvp.resolve_bgm_type(), "")

        with patch("app.services.bgm.list_bgm_files", return_value=["/songs/a.mp3"]):
            self.assertEqual(mvp.resolve_bgm_type(), "random")

    def test_render_title_card_image_is_1080x1920(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "card.png"
            mvp.render_title_card_image(
                "How AI is changing everyday life",
                str(image_path),
                "1a1a2e",
            )
            with Image.open(image_path) as image:
                self.assertEqual(image.size, (1080, 1920))

    def test_generate_title_cards_writes_1080x1920_mp4(self):
        if not utils.check_ffmpeg_ready():
            self.skipTest("ffmpeg is required to render title cards")

        with tempfile.TemporaryDirectory() as temp_dir:
            paths = mvp.generate_title_cards("How AI is changing everyday life", temp_dir)
            self.assertGreaterEqual(len(paths), 1)
            probe = _probe_video(paths[0])
            self.assertEqual(probe["width"], 1080)
            self.assertEqual(probe["height"], 1920)
            self.assertGreaterEqual(probe["duration"], 7.0)

    def test_prepare_visuals_prefers_resource_clips(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            clips_dir = Path(temp_dir) / "clips"
            storage_dir = Path(temp_dir) / "local_videos"
            clips_dir.mkdir()
            storage_dir.mkdir()
            source = clips_dir / "scene.mp4"
            source.write_bytes(b"local-clip")

            with (
                patch.object(mvp, "list_local_clips", return_value=[str(source)]),
                patch.object(utils, "storage_dir", return_value=str(storage_dir)),
            ):
                materials, source_name = mvp.prepare_visuals("topic")

                self.assertEqual(source_name, "clips")
                self.assertEqual(len(materials), 1)
                self.assertTrue(materials[0].url.startswith(str(storage_dir)))
                self.assertEqual(Path(materials[0].url).read_bytes(), b"local-clip")

    def test_build_video_params_uses_local_source_and_edge_voice(self):
        materials = [MaterialInfo(provider="local", url="/tmp/card.mp4", duration=0)]
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(mvp, "prepare_visuals", return_value=(materials, "title_cards")),
            patch.object(mvp, "resolve_bgm_type", return_value=""),
        ):
            params, meta = mvp.build_video_params("How AI is changing everyday life")

        self.assertEqual(params.video_source, "local")
        self.assertEqual(params.video_aspect.value, "9:16")
        self.assertEqual(params.voice_name, mvp.DEFAULT_EDGE_VOICE_EN)
        self.assertTrue(params.subtitle_enabled)
        self.assertEqual(params.video_script.count("How AI is changing everyday life"), 1)
        self.assertEqual(meta["script_source"], "template")
        self.assertEqual(meta["visual_source"], "title_cards")
        self.assertEqual(meta["bgm_type"], "none")


class TestOneclickCli(unittest.TestCase):
    def test_topic_is_required(self):
        with self.assertRaises(SystemExit) as raised:
            oneclick.parse_args([])
        self.assertEqual(raised.exception.code, 2)

    def test_run_oneclick_dispatches_existing_task_pipeline(self):
        materials = [MaterialInfo(provider="local", url="/tmp/card.mp4", duration=0)]
        with (
            patch.object(
                mvp,
                "prepare_visuals",
                return_value=(materials, "title_cards"),
            ),
            patch.object(mvp, "resolve_bgm_type", return_value=""),
            patch("app.services.task.start", return_value={"videos": ["/tmp/out.mp4"]}) as start,
            patch("app.utils.utils.get_uuid", return_value="task-mvp"),
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            code = oneclick.run_oneclick(["How AI is changing everyday life"])

        self.assertEqual(code, 0)
        kwargs = start.call_args.kwargs
        self.assertEqual(kwargs["task_id"], "task-mvp")
        self.assertEqual(kwargs["stop_at"], "video")
        self.assertIs(kwargs["allow_server_file_input"], True)
        self.assertEqual(kwargs["params"].video_source, "local")
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["video"], "/tmp/out.mp4")
        self.assertEqual(payload["script_source"], "template")

    def test_run_oneclick_returns_error_on_task_failure(self):
        materials = [MaterialInfo(provider="local", url="/tmp/card.mp4", duration=0)]
        with (
            patch.object(
                mvp,
                "prepare_visuals",
                return_value=(materials, "title_cards"),
            ),
            patch.object(mvp, "resolve_bgm_type", return_value=""),
            patch(
                "app.services.task.start",
                return_value={
                    "state": -1,
                    "failed_stage": "audio",
                    "error": "tts failed",
                },
            ),
            patch("app.utils.utils.get_uuid", return_value="task-fail"),
        ):
            code = oneclick.run_oneclick(["topic"])

        self.assertEqual(code, 1)


def _probe_video(path: str) -> dict[str, float]:
    from moviepy import VideoFileClip

    clip = VideoFileClip(path)
    try:
        width, height = clip.size
        return {
            "width": int(width),
            "height": int(height),
            "duration": float(clip.duration or 0),
        }
    finally:
        clip.close()


if __name__ == "__main__":
    unittest.main()
