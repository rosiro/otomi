# otomi（音見）

**English** | [日本語](README.ja.md)

> A local tool that auto-categorizes your sound effect library with AI and lets you find sounds by description or by similarity.

Once you've collected thousands of sound effects, finding "that one sound" gets hard.
otomi analyzes the audio files in your folders with [CLAP](https://github.com/LAION-AI/CLAP) (Contrastive Language-Audio Pretraining, a model that maps audio and text into the same vector space).
It organizes and searches by what the sounds actually sound like, without relying on file names or your existing folder layout.

![otomi screenshot](docs/images/screenshot.png)

> **Status: alpha.** It works, but features and the database format may still change.

> **Note:** The user interface and the built-in category names are in Japanese. Text search itself works in English (see [Search tips](#search-tips)).

## Features

- 📁 **Just register a folder**: subfolders are scanned recursively. Later scans only analyze files that were added or changed.
- 🏷 **Automatic categorization**: sorts sounds into about 120 categories in 16 groups (UI sounds, impacts, weapons, magic, human voice, animals, nature, ambience, vehicles, household, horror, instruments, and more).
- 🔎 **Text search**: describe a sound, such as `door slam` or `rain on a window`, to find sounds with a similar meaning.
- 🔁 **Similar-sound search**: lists the sounds closest to the one you pick.
- 🎧 **Fast auditioning**: a list with waveforms that plays each sound as you move through it with the ↑↓ keys.
- ✏ **Manual fixes and favorites**: correct the category when the AI gets it wrong, and star the sounds you like.
- 🖱 **Drag and drop**: drag a row into your DAW, video editor, or file explorer (Chrome / Edge).
- 🔒 **Fully local**: audio is never sent anywhere, and original files are never modified.
- 📤 **Export**: write the results to CSV, or into per-category folders (copies or hard links).

Supported formats: wav / mp3 / flac / ogg / opus / aiff / m4a / aac / wma / caf

## Requirements

- Windows / macOS / Linux
- [uv](https://docs.astral.sh/uv/) (sets up Python 3.12 and all dependencies for you)
- An NVIDIA GPU is recommended. It also runs on CPU only, but analysis is slower.
- Disk space: a few GB for PyTorch and the model
- An internet connection on first launch, which downloads the CLAP model (about 800 MB) from Hugging Face

For reference, analysis runs at about 5 files per second on a GTX 1650 (4 GB VRAM).

## Installation

```bash
git clone https://github.com/rosiro/otomi.git
cd otomi
uv sync
```

By default, `pyproject.toml` installs the **CUDA 12.6 build of PyTorch**.

- **No NVIDIA GPU, or macOS**: delete the `[tool.uv.sources]` and `[[tool.uv.index]]` blocks from `pyproject.toml`, then run `uv sync`. This installs the regular PyPI build, which runs on the CPU.
- **A different CUDA version**: change the `cu126` part of `url = "https://download.pytorch.org/whl/cu126"` to match your environment.

## Usage

```bash
uv run otomi serve
```

On Windows you can also double-click `otomi.bat`.

1. Your browser opens `http://127.0.0.1:8765/`. On first launch, downloading and loading the model takes a few minutes.
2. Click ⚙ in the top-right corner, enter the path to your sound effects folder, and click 「追加して解析」 (Add and analyze).
3. As analysis proceeds, the category list on the left fills in with counts.
4. Click a category or type a search query to find sounds.

The server only listens on `127.0.0.1`, so other computers can't reach it.

### Controls

| Action | What it does |
| --- | --- |
| Click a row / ↑↓ | Select and play (when 「自動再生」, auto-play, is on) |
| Space | Play / stop |
| Click the waveform | Play from that position |
| S | Find sounds similar to the selected one |
| F | Toggle favorite |
| E | Show the file in Explorer (Finder) |
| / | Jump to the search box |
| Esc | Close menus and dialogs |
| Click a category label | Change the category by hand (also shows the AI's top 3 guesses with probabilities) |
| Drag a row | Drop the file into a DAW or folder (Chrome / Edge) |

### Search tips

CLAP's text encoder was trained on English, so **type text searches in English**.

- The more specific the description, the better the results: `footsteps on gravel`, `sword clash`, `cute pop`, `sci-fi door open`.
- A Japanese query is first matched against the category names (for example `雨` rain, `爆発` explosion, `足音` footsteps). If it matches, otomi searches with that category's descriptions. If not, it searches file names instead.
- The second box next to the search box only filters by file name (it works with Japanese file names too).
- If you search while a category is selected, results within that category are sorted by how closely they match.

## Command line

```bash
uv run otomi add "D:\SE"              # register a folder and analyze it
uv run otomi index                     # rescan registered folders (new and changed files only)
uv run otomi index --retry-errors      # also retry files that failed to load
uv run otomi roots                     # list registered folders
uv run otomi remove "D:\SE"            # unregister a folder (files are not deleted)
uv run otomi search "glass breaking"   # search from the terminal
uv run otomi categories                # sound counts per category
uv run otomi export-csv result.csv     # export results to CSV
uv run otomi export-folders D:\SE_sorted --mode hardlink   # export into per-category folders
uv run otomi serve --port 9000 --no-browser --no-scan
```

`export-folders` writes files as `group/category/filename`.
With `--mode hardlink`, files on the same drive are exported without using extra disk space.

## Customizing categories

```bash
uv run otomi init-categories   # copy the default definitions to data/categories.toml
```

After editing `data/categories.toml`, click 「カテゴリを再読み込み」 (Reload categories) in the settings dialog. Changes take effect immediately.
The audio vectors are already saved and only the matching against categories is redone, so **no re-analysis of your audio is needed**.

```toml
[horror]
name = "ホラー・不気味"
[horror.items]
creak = { name = "きしみ", prompts = ["creaking wood", "creaky floorboard"] }
```

- `name` is the label shown in the UI (any language works).
- `prompts` are English descriptions. Listing several phrasings makes classification more stable.

## Configuration (environment variables)

| Variable | Default | Description |
| --- | --- | --- |
| `OTOMI_HOME` | `./data` | Where the database and user settings are stored |
| `OTOMI_MODEL` | `laion/larger_clap_general` | The CLAP model to use (for example `laion/clap-htsat-unfused`). Changing it re-analyzes all files. |
| `HF_HUB_OFFLINE` | – | Set to `1` to use only the local cache, without checking for model updates |

The results database (`data/otomi.db`) contains the paths and file names of your registered folders. Don't commit it to a repository (it's meant to be excluded by `.gitignore`).

## How it works

1. Each file is converted to 48 kHz mono and cut into clips of up to 10 seconds. For long sounds, up to 4 clips are taken from evenly spaced positions.
2. CLAP's audio encoder turns each clip into a 512-dimensional vector, which is saved to SQLite. For long sounds, the clip vectors are averaged.
3. The English descriptions of each category are turned into vectors by the text encoder, and each sound goes to the category with the highest cosine similarity (zero-shot classification).
4. Search queries are vectorized the same way and results are sorted by similarity. "Similar sounds" compares audio vectors directly.

## Known limitations

- Text search works in English only (a CLAP limitation).
- Classification is zero-shot (no training), so closely related categories (for example "hit" vs. "punch") can get mixed up. Fix them by hand or adjust the `prompts`.
- Each file gets exactly one category (no multi-tagging yet).
- For files longer than 300 seconds, only the first 300 seconds are analyzed.
- Dragging files out of the list only works in Chromium-based browsers (Chrome / Edge).

## Development

```bash
uv sync            # also installs dev dependencies (pytest, etc.)
uv run pytest      # fast tests that don't need the CLAP model
```

| File | Role |
| --- | --- |
| `otomi/audio.py` | Audio loading, resampling, waveform thumbnails |
| `otomi/model.py` | Wrapper around the CLAP model (Hugging Face transformers) |
| `otomi/indexer.py` | Folder scanning and embedding computation |
| `otomi/categories.py` / `categories.toml` | Category definitions and zero-shot classification |
| `otomi/library.py` | In-memory classification and search |
| `otomi/db.py` | SQLite storage |
| `otomi/server.py` / `otomi/static/` | Web UI (FastAPI + plain HTML/JS) |
| `otomi/cli.py` | Command line |

## Acknowledgements

- [LAION-AI/CLAP](https://github.com/LAION-AI/CLAP): the default model, [`laion/larger_clap_general`](https://huggingface.co/laion/larger_clap_general), is licensed under Apache-2.0. The model isn't included in this repository; it's downloaded from Hugging Face at runtime.
- [Hugging Face Transformers](https://github.com/huggingface/transformers), [PyTorch](https://pytorch.org/), [FastAPI](https://fastapi.tiangolo.com/), [python-soundfile](https://github.com/bastibe/python-soundfile), [PyAV](https://github.com/PyAV-Org/PyAV), [python-soxr](https://github.com/dofuuz/python-soxr)
