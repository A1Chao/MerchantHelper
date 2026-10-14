r"""把保存的录音导出成可播放的 WAV，供人耳确认"音频通路是否被砍掉高频"。

用法：cmd /d /c "D:\PY\python.exe tests\export_wav.py 录音 > tests\out_export_wav.txt 2>&1"

导出内容（全部统一峰值归一化，便于直接对比听感）：
  1) 游戏内每段 win_*.npy          → 录音/wav/游戏内_序号.wav
  2) 游戏内那次"最响 0.4 秒"音效段  → 录音/wav/游戏内_序号_音效段.wav
  3) 桌面干净录音（能拿 99 分）      → 录音/wav/对照_桌面干净录音.wav
  4) 该录音的低通版本 1kHz / 4kHz    → 录音/wav/对照_桌面录音_低通1kHz.wav / _低通4kHz.wav
  5) 两个模板                        → 录音/wav/模板_撒币声音.wav / 模板_重物拖动声.wav

听法：如果"游戏内_*.wav"听起来像"对照_桌面录音_低通1kHz.wav"那样**发闷、像电话**，
而"对照_桌面干净录音.wav"清亮 → 就确认了高频是在**采集通路上**被砍掉的（不是游戏混音问题）。
"""
from __future__ import annotations

import json
import sys
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

import soundfile as sf  # noqa: E402

import recorder  # noqa: E402

SR = 44100


def norm(x: np.ndarray, peak: float = 0.95) -> tuple[np.ndarray, float]:
    """峰值归一化，返回 (音频, 增益倍数)。"""
    x = np.asarray(x, dtype=np.float32).reshape(-1)
    p = float(np.abs(x).max())
    if p <= 1e-9:
        return x, 1.0
    g = float(peak) / p
    return (x * g).astype(np.float32), g


def lowpass(x: np.ndarray, cutoff: float) -> np.ndarray:
    import scipy.signal as signal

    sos = signal.butter(6, cutoff, btype="low", fs=SR, output="sos")
    return signal.sosfilt(sos, np.asarray(x, dtype=np.float64)).astype(np.float32)


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "录音"
    out = src / "wav"
    out.mkdir(parents=True, exist_ok=True)
    rows = []

    def emit(name: str, x: np.ndarray, note: str = "") -> None:
        y, g = norm(x)
        fp = out / name
        sf.write(str(fp), y, SR, subtype="PCM_16")
        seg = recorder.loudest_segment(np.asarray(x, dtype=np.float32), 0.4)
        rows.append((name, float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2))),
                     float(np.abs(np.asarray(x)).max()), g,
                     recorder.spectral_rolloff(seg), note))

    wins = sorted(src.glob("win_*.npy"))
    for i, wf in enumerate(wins, 1):
        tag = wf.stem.split("_", 1)[1]
        meta_p = src / f"meta_{tag}.json"
        meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.is_file() else {}
        x = np.load(wf).astype(np.float32).reshape(-1)
        top = f"{meta.get('top')}/{meta.get('top_score')}" if meta.get("top") else "无候选"
        emit(f"游戏内_{i:02d}_{top.replace('/', '_')}.wav", x, f"win_{tag}")
        seg = recorder.loudest_segment(x, 0.4)
        emit(f"游戏内_{i:02d}_音效段_{top.replace('/', '_')}.wav", seg, "最响0.4秒")

    ref_npz = ROOT / "tests" / "loopback_ref.npz"
    if ref_npz.is_file():
        clean = np.asarray(np.load(ref_npz)["window"], dtype=np.float32).reshape(-1)
        emit("对照_桌面干净录音.wav", clean, "同设备桌面录音，撒币声音 99 分")
        emit("对照_桌面录音_低通4kHz.wav", lowpass(clean, 4000.0), "人工低通 4kHz")
        emit("对照_桌面录音_低通1kHz.wav", lowpass(clean, 1000.0), "人工低通 1kHz（类似电话）")

    for name in ("撒币声音", "重物拖动声"):
        p = ROOT / "assets" / "sounds" / f"{name}.wav"
        if p.is_file():
            raw, sr = sf.read(str(p), dtype="float32", always_2d=True)
            x = raw.mean(axis=1).astype(np.float32)
            if sr != SR:
                import math

                import scipy.signal as signal

                g = math.gcd(sr, SR)
                x = signal.resample_poly(x, SR // g, sr // g).astype(np.float32)
            emit(f"模板_{name}.wav", x, "识别用的模板")

    print(f"已导出 {len(rows)} 个 WAV 到：{out}\n")
    print(f"{'文件':>44} {'rms':>9} {'峰值':>8} {'增益':>7} {'滚降kHz':>8}  说明")
    for name, rms, peak, g, ro, note in rows:
        print(f"{name:>44} {rms:>9.5f} {peak:>8.5f} {g:>6.1f}x {ro / 1000:>8.1f}  {note}")
    print("\n听法：把『游戏内_*.wav』和『对照_桌面干净录音.wav』对比。")
    print("      若游戏内的听起来**发闷像电话**、和『对照_桌面录音_低通1kHz.wav』相似，")
    print("      而干净的清亮 → 确认高频是在采集通路（蓝牙免手持/音频增强/16kHz）被砍掉的。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())