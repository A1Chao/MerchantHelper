r"""播放一个模板音效（用于本地验证音频触发/识别链路，不接触游戏）。

用法：D:\PY\python.exe tests\play_sound.py 撒币声音 2
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "deps")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import soundcard as sc  # noqa: E402
import soundfile as sf  # noqa: E402

key = sys.argv[1] if len(sys.argv) > 1 else "撒币声音"
times = int(sys.argv[2]) if len(sys.argv) > 2 else 1
peak = float(sys.argv[3]) if len(sys.argv) > 3 else 0.5
gap = float(sys.argv[4]) if len(sys.argv) > 4 else 1.2

path = ROOT / "assets" / "sounds" / f"{key}.wav"
raw, sr = sf.read(str(path), dtype="float32", always_2d=True)
clip = raw.mean(axis=1).astype("float32")
clip = clip / (float(abs(clip).max()) or 1.0) * peak
pad = np.zeros(int(0.3 * sr), dtype="float32")
clip = np.concatenate([pad, clip, pad])

spk = sc.default_speaker()
print(f"[play] {key} → {spk.name} sr={sr} 次数={times} peak={peak}", flush=True)
for i in range(times):
    spk.play(clip, samplerate=sr)
    print(f"[play] 第 {i + 1}/{times} 次完成", flush=True)
    if i + 1 < times:
        time.sleep(gap)