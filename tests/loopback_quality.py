r"""回环通路质量测试：播放已知宽带信号，同时回环录回，测出端到端频率响应。

用途：区分"音效被采集通路降质"和"游戏自身混音如此"。
  · 桌面跑一遍 = 基线（正常应接近全带宽 20kHz、各频段增益接近 0dB）
  · 开着游戏/语音再跑一遍 = 若高频增益骤降或带宽掉到几 kHz，即确认通路在游戏期间降质

用法：
  cmd /d /c "D:\PY\python.exe tests\loopback_quality.py --rounds 3 > tests\out_loopback_quality.txt 2>&1"
  可选 --device "耳机 (vivo TWS 5)" 指定回环设备；--level 0.3 播放电平；--seconds 1.2 信号时长

只读+自测：不读游戏内存、不模拟输入、不联网。播放的是本地生成的测试噪声。
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
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import soundcard as sc  # noqa: E402

SR = 44100
BANDS = [(0, 200), (200, 500), (500, 1000), (1000, 2000), (2000, 4000),
         (4000, 8000), (8000, 16000), (16000, 21000)]


def band_power(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64).reshape(-1)
    n = 1 << 15
    n = min(n, len(x))
    if n < 2048:
        return np.zeros(len(BANDS))
    spec = np.abs(np.fft.rfft(x[:n] * np.hanning(n))) ** 2
    f = np.fft.rfftfreq(n, 1.0 / SR)
    return np.array([float(spec[(f >= lo) & (f < hi)].sum()) for lo, hi in BANDS])


def make_signal(kind: str, seconds: float, level: float, seed: int = 20261012) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(SR * seconds)
    if kind == "sweep":
        t = np.arange(n) / SR
        f0, f1 = 100.0, 18000.0
        k = (f1 / f0) ** (1.0 / seconds)
        phase = 2 * np.pi * f0 * (k ** t - 1.0) / np.log(k)
        x = np.sin(phase)
    else:                                     # 白噪声：全频段等能量，最能暴露带宽损失
        x = rng.standard_normal(n)
    pad = np.zeros(int(SR * 0.3), dtype=np.float64)
    x = np.concatenate([pad, x, pad])
    x = x / (np.abs(x).max() or 1.0) * level
    return x.astype(np.float32)


def main() -> int:
    ap = argparse.ArgumentParser(description="回环通路质量测试（只读自测）")
    ap.add_argument("--device", default=None, help="回环设备名；默认系统当前播放设备")
    ap.add_argument("--level", type=float, default=0.3, help="播放电平（峰值），默认 0.3")
    ap.add_argument("--seconds", type=float, default=1.2, help="测试信号时长秒，默认 1.2")
    ap.add_argument("--rounds", type=int, default=3, help="重复轮数，默认 3")
    ap.add_argument("--signal", choices=["noise", "sweep"], default="noise")
    args = ap.parse_args()

    spk = sc.default_speaker()
    if args.device:
        try:
            mic = sc.get_microphone(args.device, include_loopback=True)
        except Exception as exc:
            print(f"找不到回环设备 {args.device!r}: {exc}")
            print("可选：", [m.name for m in sc.all_microphones(include_loopback=True)])
            return 2
    else:
        mic = sc.get_microphone(spk.name, include_loopback=True)

    print(f"播放设备: {spk.name}")
    print(f"回环设备: {mic.name}")
    print(f"信号: {args.signal} 时长={args.seconds}s 电平={args.level} 轮数={args.rounds}\n")

    sig = make_signal(args.signal, args.seconds, args.level)
    src_band = band_power(sig)
    src_tot = float(src_band.sum()) or 1.0
    print("源信号频段占比:", " ".join(
        f"{lo}-{hi}:{100 * p / src_tot:.1f}%" for (lo, hi), p in zip(BANDS, src_band)))

    verdicts = []
    for r in range(1, args.rounds + 1):
        captured = []

        def rec_thread():
            try:
                with mic.recorder(samplerate=SR, blocksize=int(SR * 0.08)) as rec:
                    t_end = time.time() + args.seconds + 1.0
                    while time.time() < t_end:
                        captured.append(rec.record(numframes=int(SR * 0.08)))
            except Exception as exc:                       # 设备被占用等
                print(f"  录制失败: {type(exc).__name__}: {exc}")

        th = threading.Thread(target=rec_thread, daemon=True)
        th.start()
        time.sleep(0.25)
        spk.play(sig, samplerate=SR)
        th.join(timeout=args.seconds + 3.0)

        if not captured:
            print(f"[第{r}轮] 没有录到数据")
            continue
        buf = np.concatenate([np.asarray(c, dtype=np.float32).reshape(-1) for c in captured])
        # 取最响的 0.6 秒（就是我们播放的那段）
        seg = _loudest(buf, int(SR * 0.6))
        cap_band = band_power(seg)
        cap_tot = float(cap_band.sum()) or 1.0
        print(f"\n[第{r}轮] 采集 RMS={float(np.sqrt(np.mean(seg.astype(np.float64)**2))):.5f} "
              f"峰值={float(np.abs(seg).max()):.5f}")
        gains = []
        for (lo, hi), sb, cb in zip(BANDS, src_band, cap_band):
            if sb <= 0:
                gains.append(np.nan)
                continue
            # 每段增益(dB)：把源信号的电平差先归一化掉，只看"形状"变化
            gains.append(10 * np.log10((cb / cap_tot) / (sb / src_tot) + 1e-12))
        print("  频段增益(相对源):", " ".join(
            f"{lo}-{hi}:{g:+.1f}dB" for (lo, hi), g in zip(BANDS, gains)))
        ro = _rolloff(seg)
        print(f"  采集带宽(滚降99.5%)≈{ro / 1000:.1f}kHz")
        hi_gain = float(np.nanmean([g for (lo, hi), g in zip(BANDS, gains) if lo >= 4000]))
        print(f"  高频(>4kHz)平均增益 {hi_gain:+.1f}dB")
        verdicts.append((ro, hi_gain))

    print("\n" + "=" * 68)
    if verdicts:
        ros = [v[0] for v in verdicts]
        his = [v[1] for v in verdicts]
        print(f"汇总：带宽 {min(ros)/1000:.1f}–{max(ros)/1000:.1f}kHz，"
              f"高频增益 {min(his):+.1f}–{max(his):+.1f}dB")
        if max(ros) < 12000 or max(his) < -6:
            print("▶ 判定：**通路在损失高频**（高频增益明显为负 / 带宽不足）。")
            print("  这就是音效被降质的来源，与识别阈值无关。处置见下方清单。")
            print("  1) 蓝牙耳机改走立体声 A2DP，禁用其『免手持/Hands-Free』端点")
            print("  2) 声音设置→该设备→增强：关闭低音增强/响度均衡/空间音效(Windows Sonic)")
            print("  3) 高级→默认格式选 48000 Hz（16000 Hz 即为元凶）")
            print("  4) 控制面板→声音→通信→选『不执行任何操作』")
            print("  5) 关闭游戏内语音/麦克风占用；或直接改用有线/Realtek 输出并让工具指向同一设备")
        else:
            print("▶ 判定：通路带宽正常（>12kHz、高频增益不低）。")
            print("  那么游戏内音质差来自游戏自身混音/音量设置（如低音增强、夜间模式、")
            print("  或音效音量偏低），可通过提高游戏音效音量改善信噪比。")
    print("=" * 68)
    return 0


def _loudest(x: np.ndarray, n: int) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64).reshape(-1)
    if len(x) <= n:
        return np.asarray(x, dtype=np.float32)
    c = np.concatenate([[0.0], np.cumsum(x * x)])
    e = c[n:] - c[:-n]
    s = int(np.argmax(e))
    return np.asarray(x[s:s + n], dtype=np.float32)


def _rolloff(x: np.ndarray, fraction: float = 0.995) -> float:
    x = np.asarray(x, dtype=np.float64).reshape(-1)
    n = min(len(x), 1 << 15)
    if n < 2048:
        return 0.0
    spec = np.abs(np.fft.rfft(x[:n] * np.hanning(n))) ** 2
    cum = np.cumsum(spec) / (spec.sum() or 1.0)
    idx = int(np.searchsorted(cum, fraction))
    return float(np.fft.rfftfreq(n, 1.0 / SR)[min(idx, len(spec) - 1)])


if __name__ == "__main__":
    raise SystemExit(main())