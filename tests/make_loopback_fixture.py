r"""生成确定性实录夹具：把已知模板从扬声器播出去 → 生产路径回环录回 → 存最佳窗口。

用途：给 tests/selftest_integration.py 提供**可重复的正样本**（真实回环链路，
含设备混音/重采样/系统音量等真实域偏移），并保留一段已知劣质录音作为负样本。

命令：
  cmd /d /c "D:\PY\python.exe tests\make_loopback_fixture.py > tests\out_make_fixture.txt 2>&1"
可选：--device "扬声器 (2- Realtek(R) Audio)"
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "deps")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import recognition  # noqa: E402
import recorder  # noqa: E402

ASSETS = ROOT / "assets"
EXPECT = "撒币声音"
OUT_NPZ = ROOT / "tests" / "loopback_ref.npz"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default=None, help="播放+回环设备，默认系统默认设备")
    ap.add_argument("--peak", type=float, default=0.5, help="播放归一化峰值，默认 0.5 (-6dBFS)")
    args = ap.parse_args()

    matcher = recognition.SoundMatcher(str(ASSETS), str(ASSETS / "database.json"))
    tpl = {t.key: t for t in matcher.templates}[EXPECT]

    # 播放线程（WASAPI 需要在线程内初始化 COM）
    def play():
        try:
            import comtypes

            comtypes.CoInitialize()
        except Exception:
            try:
                import ctypes

                ctypes.windll.ole32.CoInitialize(None)
            except Exception:
                pass
        import soundcard as sc
        import soundfile as sf

        spk = sc.get_speaker(args.device) if args.device else sc.default_speaker()
        raw, native_sr = sf.read(str(tpl.path), dtype="float32", always_2d=True)
        clip = raw.mean(axis=1).astype(np.float32)
        clip = clip / (float(np.abs(clip).max()) or 1.0) * args.peak
        pad = np.zeros(int(0.6 * native_sr), dtype=np.float32)
        clip = np.concatenate([pad, clip, pad])
        print(f"[play] device={spk.name} sr={native_sr} samples={len(clip)}", flush=True)
        spk.play(clip, samplerate=native_sr)

    stop = threading.Event()
    windows: list[tuple[np.ndarray, float]] = []
    err: list[str] = []

    def collect():
        try:
            for win, bg in recorder.listen_windows(stop, args.device):
                windows.append((win, float(bg)))
                if len(windows) >= 14:
                    break
        except Exception as exc:
            err.append(f"{type(exc).__name__}: {exc}")

    th = threading.Thread(target=collect, daemon=True)
    th.start()
    time.sleep(0.3)
    threading.Thread(target=play, daemon=True).start()
    t_end = time.time() + 4.2
    while time.time() < t_end and not err:
        time.sleep(0.1)
    stop.set()
    th.join(timeout=3)

    if err:
        print(f"[fail] 采集失败: {err[0]}")
        return 1
    if not windows:
        print("[fail] 未采集到窗口")
        return 1

    scored = []
    for win, bg in windows:
        rows = matcher.match(win, recorder.SAMPLE_RATE, 5, bg)
        top = rows[0] if rows else None
        scored.append((top["score"] if top else 0, top["key"] if top else "-", win, bg, rows))
        print(f"[win] rms={float(np.sqrt(np.mean(win.astype('float64')**2))):.5f} bg={bg:.5f} "
              f"top={top['key'] if top else '-':8s} score={top['score'] if top else 0}", flush=True)

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_key, best_win, best_bg, best_rows = scored[0]
    print(f"[best] key={best_key} score={best_score} bg={best_bg}")

    np.savez(
        OUT_NPZ,
        window=best_win.astype(np.float32),
        background_rms=np.float32(best_bg),
        samplerate=np.int32(recorder.SAMPLE_RATE),
        expect=np.array(best_key),
        expected_key=np.array(EXPECT),
        score=np.int32(best_score),
        device=np.array(args.device or recorder.default_device_name()),
        captured_at=np.array(time.strftime("%Y-%m-%d %H:%M:%S")),
    )
    print(f"[save] {OUT_NPZ} ({OUT_NPZ.stat().st_size} bytes)")
    ok = best_key == EXPECT and best_score >= 80
    print(f"[result] {'OK' if ok else 'WEAK'}: top={best_key} score={best_score}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())