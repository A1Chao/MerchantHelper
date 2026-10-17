# MerchantHelper

> An **external-perception** helper for the "field merchant" gameplay in *Arena Breakout: Infinite*.
> Author: **烟火**

While you **hold the left mouse button and drag an item**, it both *listens* and *looks*, then shows
candidate items and their reference value in a small always-on-top window.

---

## 1. What it is

| | What it does |
| --- | --- |
| **Listen** | Captures the system loopback audio (WASAPI loopback) and matches it against built-in sound templates to list candidate items |
| **Look** (official build) | Recognizes the **grid footprint and orientation** of the item under the cursor, used to rank and filter candidates |
| **Show** | A separate always-on-top window: cells / orientation / source + candidate list (quality, category, reference value) |

**The principle is the same as OBS-like recording/display software**: it performs *external capture*
of screen and system audio, then recognizes it. This tool is **strictly read-only**:

- ❌ Does not read game memory ❌ Does not inject into any process ❌ Does not modify game files
- ❌ Does not unpack game resources
- ❌ Does not synthesize input (no `SendInput` / `mouse_event` / `keybd_event`), no auto-click, no auto-buy
- ❌ No network access, no self-learning, no hot updates
- ✅ Reads keys and cursor only through read-only APIs (`GetAsyncKeyState` / `GetCursorPos`)
- ✅ The UI is a separate window: it never overlays the game and never steals focus

In theory there is no risk, but please judge for yourself before using it.

## 2. ⚠️ This repository is the "open edition"

This repository is the **semi-open-source** edition. It contains audio recognition (`recognition.py`),
candidate filtering (`candidates.py`), the UI (`ui.py`), recording/denoising, and the integration
layer (`main.py`).

**It does not contain** the grid-footprint recognition (the vision core, `vision.py`) — the
`vision.py` here is only a placeholder stub.

| Capability | Open edition (this repo) | Official build (exe in Releases) |
| --- | :---: | :---: |
| Audio recognition | ✅ | ✅ |
| Candidate filtering | ✅ | ✅ |
| Panel UI | ✅ | ✅ |
| **Grid footprint / orientation** | ❌ (shows `?格`) | ✅ |

→ **For the full feature set, use the official exe in Releases.**

**Latest download: [Releases](https://github.com/A1Chao/MerchantHelper/releases/latest) (currently v1.1.0,
a single portable exe).** If Windows shows "Windows protected your PC" (SmartScreen) before the UAC
prompt, see [`docs/SmartScreen与代码签名.md`](docs/SmartScreen与代码签名.md) (Chinese) — **do it once
and only the UAC prompt remains.** Files received via QQ / WeChat / USB stick are not marked at all,
so you can skip that page.

## 3. Running from source

### Requirements

- Windows 10 / 11
- Python **3.12** (developed on 3.12.5; other versions untested)
- `pip install -r requirements.txt`
- Audio device access uses the bundled `deps/` (comtypes, soundcard) — nothing extra to install

### Quick start

```bat
python main.py --list-devices          :: list playback devices
python main.py                          :: pick the default playback device automatically
python main.py --device "Speakers (2- Realtek(R) Audio)"
python main.py --no-vision              :: audio-only mode (what this repo can do)
python main.py --pitch 85               :: force the grid pitch (needs the vision core)
```

### Usage

1. Press **F8** to start monitoring (the panel shows "monitoring")
2. In game, **hold the left mouse button and drag** an item — **a click makes no sound; you must drag**
3. Read the panel: cells / orientation / source + the candidate list
4. Press **F8** again to stop

### ⚠️ Must run as administrator

The target game was measured to run at a higher privilege level. **Without elevation this tool
cannot read any key press** — the symptom is "nothing happens at all".
Please **right-click → Run as administrator**.

## 4. Supported environment (unverified)

- Verified only on **one resolution / fullscreen** setup so far (measured grid pitch ≈ 85 px)
- **Other resolutions, windowed mode, and multi-monitor setups are unverified** — please open an
  Issue with your environment and symptoms
- The exact in-game screens/paths depend on your game version

## 5. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Dragging an item does nothing | Not running as administrator (see above) |
| "Windows protected your PC" on launch | Download mark from a browser (or a low-integrity copy); see [the SmartScreen notes](docs/SmartScreen与代码签名.md) — one-time fix |
| No candidates in the panel | A click makes no sound — you must **drag**; or the wrong device (`--list-devices`) |
| Footprint shows `?格` | The open edition has no vision core; use the official exe |
| Panel blocks the view | The panel is a separate draggable window — move it |

## 6. Feedback

**GitHub Issues.** Please include: resolution and window mode, whether you ran as administrator,
a panel screenshot, and the log.

## 7. Disclaimer

- This project is for **study and exchange only**; **any commercial use is prohibited**
- It is **not affiliated with, authorized by, or endorsed by** the game's publisher
- The item icons, sound effects, and item data under `assets/` originate from the game and
  **remain the property of their rights holders**; they are included for study only
- **You bear all consequences** of using this tool (including any risk to your game account)

## 8. Third party

See [`THIRD-PARTY-LICENSES.md`](THIRD-PARTY-LICENSES.md): comtypes (MIT), soundcard (BSD-3-Clause),
and a **PyQt5 GPL v3 notice**.

## 9. Layout

```text
main.py           integration: triggers, per-gesture lock, hold-time sampling, results
recognition.py    audio recognition (mel filterbank + similarity)
candidates.py     candidate filtering and ranking
ui.py             PyQt5 floating window
recorder.py       loopback recording   denoise.py denoising   diag.py diagnostics
vision.py         placeholder stub (the vision core is not in this repository)
assets/           item database, icons, sound effects
deps/             third party: comtypes, soundcard
tests/            offline tests (audio / candidates / triggers / session lock / hold sampling)
docs/详细说明.md   original detailed notes (Chinese)
docs/SmartScreen与代码签名.md   Windows SmartScreen prompt: why it appears and how to stop it (Chinese)
```