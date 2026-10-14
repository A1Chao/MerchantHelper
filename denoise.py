r"""跨窗谱减降噪：用"音效出现之前"的窗口作参考，把持续背景从当前窗口里减掉。

为什么需要它
------------
样本 `recognition.extract(reduce_background=True)` 的噪声估计是**窗内** 0.2 分位静音帧：
在持续背景（游戏环境音/音乐）下，窗内最静的帧同样含背景，谱减后低能量频段被钳到
`band_power * 0.025`（仍由背景主导），频谱形状被扭曲 → 实测「撒币声音」在响亮背景下
只得 31~34 分（安静时 92~99），达不到任何阈值（0.88/0.85/0.82/0.78）。

做法（不改 recognition.py 的任何阈值与算法）
--------------------------------------------
1. 对「当前窗口」与「参考窗口（按下之前的纯背景）」分别做 STFT（幅度平方 = 功率谱）；
2. 参考窗的噪声功率谱取跨时间的**低分位**（对参考里可能残留的瞬时声更稳健）；
3. 维纳式软增益 `G = max(floor, 1 - alpha * N / P)`，沿频率做轻度平滑以抑制"音乐噪声"；
4. 用当前窗口自己的相位重建波形（ISTFT + 重叠相加），再交给 matcher。

接口
----
`enhance(window, reference, samplerate=44100, ...) -> np.ndarray`（等长 float32）
`noise_psd(reference, ...) -> np.ndarray`      参考窗的噪声功率谱
`snr_db(window, reference, ...) -> float`      粗略信噪比估计（诊断用）
"""

from __future__ import annotations

from typing import Sequence

import numpy as np

N_FFT = 1024
HOP = 256
STRENGTH = 1.0        # alpha：噪声功率的过减系数
FLOOR = 0.06          # 增益下限（保留一点残响，避免过度切削造成失真）
FREQ_SMOOTH = 5       # 增益沿频率的平滑窗长（奇数）
NOISE_PERCENTILE = 30.0


def _stft(x: np.ndarray, n_fft: int = N_FFT, hop: int = HOP):
    """返回 (帧矩阵 (帧数, n_fft//2+1) 的复数谱, 窗, 帧数)。Hann 周期窗，无填充。"""
    x = np.asarray(x, dtype=np.float64).reshape(-1)
    win = np.hanning(n_fft + 1)[:-1]                # 周期 Hann
    if len(x) < n_fft:
        return np.zeros((0, n_fft // 2 + 1), dtype=np.complex128), win, 0
    n_frames = 1 + (len(x) - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n_frames)[:, None]
    frames = x[idx] * win[None, :]
    return np.fft.rfft(frames, axis=1), win, n_frames


def _istft(spec: np.ndarray, win: np.ndarray, hop: int = HOP, length: int | None = None) -> np.ndarray:
    """重叠相加重建（Hann 的 COLA 归一化），返回 float32。"""
    n_frames, n_bins = spec.shape
    n_fft = (n_bins - 1) * 2
    frames = np.fft.irfft(spec, n=n_fft, axis=1) * win[None, :]
    out_len = n_fft + hop * (n_frames - 1) if length is None else max(length, n_fft + hop * (n_frames - 1))
    out = np.zeros(out_len, dtype=np.float64)
    norm = np.zeros(out_len, dtype=np.float64)
    w2 = win * win
    for i in range(n_frames):
        s = i * hop
        out[s:s + n_fft] += frames[i]
        norm[s:s + n_fft] += w2
    nz = norm > 1e-12
    out[nz] /= norm[nz]
    if length is not None:
        out = out[:length]
    return out.astype(np.float32)


def noise_psd(reference: np.ndarray, n_fft: int = N_FFT, hop: int = HOP,
              percentile: float = NOISE_PERCENTILE) -> np.ndarray:
    """参考窗的噪声功率谱（跨时间低分位，对参考中的瞬时残响更稳健）。"""
    spec, _, n_frames = _stft(reference, n_fft, hop)
    if n_frames == 0:
        return np.zeros(n_fft // 2 + 1, dtype=np.float64)
    power = (spec.real ** 2 + spec.imag ** 2)
    return np.percentile(power, percentile, axis=0)


def noise_psd_profile(windows: Sequence[np.ndarray], n_fft: int = N_FFT, hop: int = HOP,
                      percentile: float = 25.0) -> np.ndarray | None:
    """长期背景谱：对多个历史窗口**逐频点**取低分位，比单窗参考稳得多。

    游戏环境音不能改，也不保证按下前那一小段"干净"；持续监听时总有若干段只含背景的
    窗口，逐频点低分位能把瞬时音效（如别的物品声音）剔除掉，留下真正的背景底色。
    窗口少于 2 个或全空返回 None。
    """
    spectra = []
    for w in windows or []:
        spec, _, n_frames = _stft(w, n_fft, hop)
        if n_frames == 0:
            continue
        spectra.append((spec.real ** 2 + spec.imag ** 2).reshape(-1, n_fft // 2 + 1))
    if len(spectra) < 2:
        return None
    # 每个窗口先沿时间取 30 分位（窗内噪声底），再跨窗口逐频点取低分位
    per_window = np.vstack([np.percentile(s, 30.0, axis=0) for s in spectra])
    profile = np.percentile(per_window, percentile, axis=0)
    if not np.any(profile > 0):
        return None
    return profile


def _smooth_freq(gain: np.ndarray, width: int = FREQ_SMOOTH) -> np.ndarray:
    if width <= 1:
        return gain
    width = width if width % 2 == 1 else width + 1
    kernel = np.ones(width, dtype=np.float64) / width
    pad = width // 2
    padded = np.pad(gain, ((0, 0), (pad, pad)), mode="edge")
    return np.apply_along_axis(lambda row: np.convolve(row, kernel, mode="valid"), 1, padded)


def enhance(
    window: np.ndarray,
    reference: np.ndarray | None = None,
    samplerate: int = 44100,
    strength: float = STRENGTH,
    floor: float = FLOOR,
    n_fft: int = N_FFT,
    hop: int = HOP,
    freq_smooth: int = FREQ_SMOOTH,
    *,
    profile: np.ndarray | None = None,
) -> np.ndarray:
    """跨窗谱减：把参考背景从 `window` 里减掉。

    参数
    ----
    window     : 含音效的窗口（float32/float64，任意长度）
    reference  : 音效出现**之前**的背景窗口；None/空/过短时看 `profile`
    profile    : 预先算好的噪声功率谱（`noise_psd_profile()` 的长期背景谱）。
                 给出时优先使用它（比单窗参考稳，且能剔除参考里混入的瞬时音效）。
    samplerate : 仅用于接口一致性（算法在频域，与采样率无关）
    strength   : 噪声功率过减系数 alpha（1.0 = 严格相减，>1 更激进）
    floor      : 增益下限，避免把弱频段完全抹成 0（产生"音乐噪声"）
    返回：等长 float32 波形。异常一律返回原窗口（绝不抛错）。
    """
    try:
        w = np.asarray(window, dtype=np.float32).reshape(-1)
        if len(w) < n_fft:
            return w

        noise = None
        if profile is not None:
            p = np.asarray(profile, dtype=np.float64).reshape(-1)
            if p.size == n_fft // 2 + 1 and np.any(p > 0):
                noise = p
        if noise is None:
            if reference is None:
                return w
            r = np.asarray(reference, dtype=np.float32).reshape(-1)
            if len(r) < n_fft:
                return w
            noise = noise_psd(r, n_fft, hop)
        if not np.any(noise > 0):
            return w

        spec_w, win, n_frames = _stft(w, n_fft, hop)
        if n_frames == 0:
            return w

        power = spec_w.real ** 2 + spec_w.imag ** 2
        gain = 1.0 - float(strength) * (noise[None, :] / np.maximum(power, 1e-20))
        gain = np.maximum(gain, float(floor)).clip(0.0, 1.0)
        if freq_smooth and freq_smooth > 1:
            gain = _smooth_freq(gain, int(freq_smooth))
        out = _istft(spec_w * gain, win, hop, length=len(w))
        if not np.all(np.isfinite(out)):
            return w
        return out.astype(np.float32)
    except Exception:
        return np.asarray(window, dtype=np.float32).reshape(-1)


def snr_db(window: np.ndarray, reference: np.ndarray | None) -> float:
    """粗略信噪比（dB）：用两段波形 RMS 估算，仅作诊断。"""
    try:
        w = np.asarray(window, dtype=np.float64).reshape(-1)
        if reference is None:
            return float("nan")
        r = np.asarray(reference, dtype=np.float64).reshape(-1)
        rms_r = float(np.sqrt(np.mean(r ** 2))) if r.size else 0.0
        rms_w = float(np.sqrt(np.mean(w ** 2))) if w.size else 0.0
        if rms_r <= 1e-12 or rms_w <= 1e-12:
            return float("nan")
        return 20.0 * float(np.log10(rms_w / rms_r))
    except Exception:
        return float("nan")


def enhance_pair(window: np.ndarray, reference: np.ndarray | None, samplerate: int = 44100):
    """诊断用：返回 (增强波形, 信噪比估计dB)。"""
    return enhance(window, reference, samplerate), snr_db(window, reference)