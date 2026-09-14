# Local MVP: topic → 9:16 MP4

One local command takes a topic string and writes a playable portrait MP4
(1080×1920, about 15–30 seconds). No paid API is required.

## One-time setup

1. Install **Python 3.11+** and **ffmpeg** (must be on `PATH`).
2. From the repo root, install dependencies:

```bash
# preferred
uv sync

# or
pip install -r requirements.txt
```

`config.toml` is created automatically from `config.example.toml` on first run.
You do **not** need to fill LLM, Pexels, or other vendor keys for this MVP.

Optional local assets:

- Drop clips or stills in `resource/clips/` (videos: `.mp4`, `.mov`, `.mkv`, `.webm`;
  stills: `.jpg`, `.jpeg`, `.png`, `.bmp`).
  When that folder has supported files, `python oneclick.py` uses them instead of title cards.
  Video files are copied as-is. Stills (including small PNGs such as 170×170) are
  scaled and encoded to **1080×1920** MP4 with the same still-to-video helper as
  title cards **before** the task pipeline, so preprocess cannot reject them.
  An empty folder (or only `.gitkeep`) still falls back to generated title cards.
- Bundled BGM already lives in `resource/songs/`. Delete or empty that folder for a silent video, or pass `--no-bgm`.

## Exact command

```bash
uv run python oneclick.py "How AI is changing everyday life"
```

Without uv:

```bash
python oneclick.py "How AI is changing everyday life"
```

The command prints one JSON object to stdout. The MP4 path is `video`
(also under `storage/tasks/<task-id>/`).

Useful options:

```bash
python oneclick.py "人工智能如何改变日常生活"
python oneclick.py "Your topic" --script "Write the full narration yourself."
python oneclick.py "Your topic" --no-bgm
```

PNG-only (nothing but a still in `resource/clips/`):

```bash
python oneclick.py "How AI is changing everyday life"
```

Stdout `visual_source` is `clips` (not `title_cards`). The `video` field is a playable 9:16 MP4.

## What the happy path uses

| Stage     | MVP behavior |
|-----------|----------------|
| Script    | Local template from the topic. Optional LLM **only** if an env var such as `OPENAI_API_KEY` is already set; failure falls back to the template. |
| Voice     | Edge TTS (free, no API key). Chinese topics use `zh-CN-XiaoxiaoNeural`; otherwise `en-US-JennyNeural`. |
| Visuals   | Files in `resource/clips/` when present (stills scaled to 1080×1920 MP4 first; videos used as-is). Empty folder falls back to generated 1080×1920 color/title cards. |
| Subtitles | Burned from Edge TTS timestamps. Whisper / GPU is not used. |
| BGM       | Random file from `resource/songs/` or `storage/bgm/` if any exist; silent otherwise. |

Vendor WebUI, HTTP API, batch jobs, text-to-video, and publish flows are out of scope for this command.
