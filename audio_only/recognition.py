"""Python port of the audio matching/filter pipeline identified in MerchantAssistant.dll.

It follows the reference build's 16 kHz mono, 512 point / 64 sample hop,
64-band mel log-energy frames, peak/RMS gating, coarse-to-fine temporal scan,
reference trimming, and optional low-frequency background rejection.

本文件是样本 `recognition.pyc` 的 1:1 复刻（逐指令重建，见 docs/recognition_dis.txt
与 docs/recognition_resolved.txt）。与 `项目状态.md` 第三章伪码的差异全都在反汇编里
逐条核对过，并在对应处注明（起点 offset）。
唯一**有意**改动：match() 结尾 `return output[:limit]`（样本为 `return output[:1]`，
只给榜首；本工具要把完整候选列表交给 UI）。

提速改造（2026-10-19，分支 `perf/audio-fast`）：只改**实现**，判据/阈值/分支顺序一字未改——
帧批量 FFT + 批量归一化（`_frame_power`/`_features_from_power`）、门控批量算 + 幸存偏移
点积（`_scan`）、filtered/gentle 复用同一份帧功率、强背景下把原始谱 rank 推迟到需要时。
单次 match 113→25ms（安静）/ 214→96ms（强背景）/ 175→44ms（实录）。与改造前逐用例对拍：
`tests/selftest_audio_equiv.py`（golden `tests/audio_golden.json`，89 例，分数差 ≤3.4e-8、
行序/偏移/原因串全同、显示分无变化）。

import 期无副作用：只建 mel 滤波器组与汉宁窗常量，不建模板、不读盘、不开音频设备。
"""

from __future__ import annotations

import json
import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from scipy import signal
from scipy.io import wavfile

SAMPLE_RATE = 16000
FFT_SIZE = 512
HOP = 64
BANDS = 64
MIN_SIMILARITY = 0.88
AMBIGUITY_MARGIN = 0.045
MIN_PEAK_RMS = 0.0004
MAX_CANDIDATES = 5
NOISE_OVER_SUBTRACTION = 1.35
FILTERED_MIN_SIMILARITY = 0.85
ADAPTIVE_NOISE_FLOOR = 0.002
ADAPTIVE_FILTERED_MIN_SIMILARITY = 0.82
ADAPTIVE_MIN_MARGIN = 0.1
GENTLE_NOISE_SUBTRACTION = 0.5
GENTLE_FILTERED_MIN_SIMILARITY = 0.78
GENTLE_MIN_MARGIN = 0.075
MIXED_WAVEFORM_MIN_CORRELATION = 0.12
MIXED_WAVEFORM_MIN_MARGIN = 0.05
MIXED_WAVEFORM_FFT_SIZE = 65536













WAVEFORM_OVERRIDES_PASSED_SPECTRAL = True


def set_waveform_override(enabled: bool) -> None:
    """切换「波形兜底能否覆盖已通过的谱匹配」（进程内全局；默认保持原样 True）。"""
    global WAVEFORM_OVERRIDES_PASSED_SPECTRAL
    WAVEFORM_OVERRIDES_PASSED_SPECTRAL = bool(enabled)









SPLIT_TEMPLATE_SEGMENTS = False
SEGMENT_FRAME_SECONDS = 0.02
SEGMENT_MIN_GAP_SECONDS = 0.04
SEGMENT_MIN_PART_SECONDS = 0.06
SEGMENT_ACTIVE_RATIO = 0.12


def set_template_splitting(enabled: bool) -> None:
    """切换「素材拆段建模板」（须在 `SoundMatcher` 构造前调用；进程内全局）。"""
    global SPLIT_TEMPLATE_SEGMENTS
    SPLIT_TEMPLATE_SEGMENTS = bool(enabled)


def split_template_segments(samples) -> list:
    """按 20ms 包络的内部静音缝（>=40ms）把素材拆段，丢掉短于 60ms 的碎片。

    返回 `np.ndarray` 列表；单段素材返回 `[]`（不需要额外模板）。
    """
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    frame = max(1, int(SEGMENT_FRAME_SECONDS * SAMPLE_RATE))
    n = len(x) // frame
    if n < 2:
        return []
    env = np.sqrt(np.mean(x[: n * frame].astype(np.float64).reshape(n, frame) ** 2, axis=1))
    if not np.isfinite(env).all() or float(env.max()) <= 0:
        return []
    active = env > float(env.max()) * SEGMENT_ACTIVE_RATIO
    idx = np.flatnonzero(active)
    if not idx.size:
        return []
    min_gap = max(1, int(SEGMENT_MIN_GAP_SECONDS * SAMPLE_RATE))
    min_part = max(1, int(SEGMENT_MIN_PART_SECONDS * SAMPLE_RATE))
    spans = []
    start = prev = int(idx[0])
    for i in idx[1:]:
        i = int(i)
        if (i - prev) * frame >= min_gap:
            spans.append((start, prev))
            start = i
        prev = i
    spans.append((start, prev))
    if len(spans) < 2:
        return []
    parts = [x[a * frame: (b + 1) * frame].copy() for a, b in spans]
    return [p for p in parts if len(p) >= min_part]


def read_audio(path: Path) -> tuple[np.ndarray, int]:
    """读音频为 16 kHz 单声道。优先 soundfile，ImportError 回退 scipy.io.wavfile。

    反汇编 ('REF', 85)：
      - soundfile 是函数内 import，因此 ImportError 既覆盖"没装 soundfile"也覆盖导入失败；
      - wavfile 回退里整型 PCM 用 max(abs(info.min), info.max) 归一化；
      - 结尾 nan_to_num(asarray(y, dtype=np.float32))、resample_poly(...).astype(np.float32)
        （项目状态.md 伪码写的是 float64，反汇编 offset 888/1184 用的是 np.float32）。
    """
    try:
        import soundfile as sf

        y, sr = sf.read(str(path), dtype="float32", always_2d=True)
        y = y.mean(axis=1)
    except ImportError:
        sr, raw = wavfile.read(path)
        integer_data = bool(np.issubdtype(raw.dtype, np.integer))
        info = np.iinfo(raw.dtype) if integer_data else None
        if raw.ndim == 2:
            raw = raw.astype(np.float64).mean(axis=1)
        if integer_data:
            y = raw.astype(np.float32) / max(abs(info.min), info.max)
        else:
            y = raw.astype(np.float32)
    except Exception as exc:
        raise ValueError(f"无法读取音频 {path.name}: {exc}") from exc
    if y.ndim == 2:
        y = y.mean(axis=1)
    y = np.nan_to_num(np.asarray(y, dtype=np.float32))
    if sr != SAMPLE_RATE and len(y):
        g = math.gcd(int(sr), SAMPLE_RATE)
        y = signal.resample_poly(y, SAMPLE_RATE // g, int(sr) // g).astype(np.float32)
    return y, SAMPLE_RATE


def _hz_to_mel(hz: float) -> np.ndarray:
    return 2595.0 * np.log10(1.0 + np.asarray(hz) / 700.0)


def _mel_to_hz(mel: np.ndarray) -> np.ndarray:
    return 700.0 * (10.0 ** (np.asarray(mel) / 2595.0) - 1.0)


def _build_bank() -> np.ndarray:
    """80–7800 Hz、BANDS 个三角 mel 滤波，每一行按行和归一化（反汇编 offset 412–530）。"""
    mel_pts = _mel_to_hz(np.linspace(_hz_to_mel(80.0), _hz_to_mel(7800.0), BANDS + 2))
    bins_hz = np.arange(FFT_SIZE // 2 + 1) * SAMPLE_RATE / FFT_SIZE
    bank = np.zeros((BANDS, FFT_SIZE // 2 + 1), dtype=np.float32)
    for b in range(BANDS):
        left, center, right = mel_pts[b : b + 3]
        bank[b] = np.maximum(
            0.0,
            np.minimum(
                (bins_hz - left) / (center - left),
                (right - bins_hz) / (right - center),
            ),
        )
        total = bank[b].sum()
        if total > 0:
            bank[b] /= total
    return bank


_FILTER_BANK = _build_bank()
_WINDOW = np.hanning(FFT_SIZE).astype(np.float32)


@dataclass
class Features:
    rms: np.ndarray
    spectrum: np.ndarray
    peak_rms: float
    start_frame: int = 0

def _frame_power(samples: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """把整段音频一次切成 count 个 512 点帧，批量算逐帧 RMS 与 64 带功率。

    旧实现是 376 次 Python 往返（每次十几个小 numpy 调用），这是 extract() 的主要开销。
    这里帧、FFT 全部批量做，**数学与逐帧版等价**（差异只有 BLAS 累加顺序，实测特征
    差 ~4e-7；A/B 对拍见 tests/selftest_audio_equiv.py）。
    """
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    count = max(1, (len(x) + HOP - 1) // HOP + 1)
    pad_l = FFT_SIZE // 2
    pad_r = max(0, (count - 1) * HOP + FFT_SIZE - (pad_l + len(x)))
    padded = np.zeros(pad_l + len(x) + pad_r, dtype=np.float32)
    padded[pad_l : pad_l + len(x)] = x
    frames = np.lib.stride_tricks.sliding_window_view(padded, FFT_SIZE)[::HOP][:count]
    raw_rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1)).astype(np.float32)
    z = np.fft.rfft(frames * _WINDOW, axis=1)
    power = z.real * z.real + z.imag * z.imag
    band_power = (power.astype(np.float32) @ _FILTER_BANK.T).astype(np.float32)
    return raw_rms, band_power


def _features_from_power(
    raw_rms: np.ndarray,
    band_power: np.ndarray,
    reduce_background: bool = False,
    noise_strength: float | None = None,
) -> Features:
    """extract() 的后半段：背景减除 → 逐帧 log1p → 去均值 → L2 归一化。

    与 noise_strength 无关的帧功率可以复用（match() 里 filtered/gentle 两条路
    因此只算一次 FFT），所以拆成单独函数。
    """
    count = band_power.shape[0]
    rms = raw_rms
    if reduce_background and len(raw_rms):
        quiet_cut = float(np.quantile(raw_rms, 0.2))
        quiet = raw_rms <= max(quiet_cut, 1e-09)
        noise_rms = float(np.median(raw_rms[quiet])) if np.any(quiet) else 0.0
        rms = np.sqrt(np.maximum(raw_rms * raw_rms - noise_rms * noise_rms, 0.0)).astype(
            np.float32
        )
        noise_profile = (
            np.median(band_power[quiet], axis=0)
            if np.any(quiet)
            else np.zeros(BANDS, dtype=np.float32)
        )
        strength = (
            NOISE_OVER_SUBTRACTION if noise_strength is None else max(0.0, float(noise_strength))
        )
        band_power = np.maximum(
            band_power - strength * noise_profile[None, :], band_power * 0.025
        )
    spectra = np.zeros((count, BANDS), dtype=np.float32)
    mean_energy = band_power.mean(axis=1)
    ok = mean_energy > 1e-24
    if np.any(ok):
        v = np.log1p(20.0 * band_power[ok] / mean_energy[ok][:, None]).astype(np.float32)
        v -= v.mean(axis=1, keepdims=True)
        norm = np.linalg.norm(v, axis=1)
        good = norm > 1e-18
        if np.any(good):
            spectra[np.flatnonzero(ok)[good]] = v[good] / norm[good][:, None]
    return Features(rms, spectra, float(rms.max(initial=0.0)))







LOWCUT_HZ = 0.0


def set_lowcut(hz: float) -> None:
    """改前端低切频率（0 关闭）。模板与查询同源，所以不影响干净素材自比。"""
    global LOWCUT_HZ
    LOWCUT_HZ = float(hz)



LEARNED_TEMPLATES = False

LEARNED_ROOT: Path | None = None

EXTRA_ROOTS: list[Path] = []

LEARNED_MAX_PER_KEY = 12

EXTRA_MAX_PER_KEY = 40

PREFILTER_MIN_TEMPLATES = 60

PREFILTER_TOP_K = 24


def set_learned_templates(enabled: bool, root: Path | str | None = None,
                          extra_roots: "list[Path | str] | None" = None) -> None:
    """开启/关闭学习样本模板，并指定根目录（可附带多个"外部真实录音"根）。"""
    global LEARNED_TEMPLATES, LEARNED_ROOT, EXTRA_ROOTS
    LEARNED_TEMPLATES = bool(enabled)
    LEARNED_ROOT = Path(root) if root else None
    EXTRA_ROOTS = [Path(r) for r in (extra_roots or [])]


def _lowcut(samples: np.ndarray) -> np.ndarray:
    """一阶高通（与竞品 Prepare 的写法一致）：y[n] = a*(y[n-1] + x[n] - x[n-1])。"""
    hz = float(LOWCUT_HZ)
    x = np.asarray(samples, dtype=np.float64).reshape(-1)
    if hz <= 0.0 or len(x) == 0:
        return np.asarray(samples, dtype=np.float32)
    a = float(np.exp(-2.0 * np.pi * hz / SAMPLE_RATE))
    y = np.empty_like(x)
    prev_in = 0.0
    prev_out = 0.0
    for i in range(len(x)):
        v = x[i]
        prev_out = a * (prev_out + v - prev_in)
        prev_in = v
        y[i] = prev_out
    return y.astype(np.float32)


def extract(
    samples: np.ndarray,
    reduce_background: bool = False,
    noise_strength: float | None = None,
) -> Features:
    """按 64 样本跳步切帧（居中，start=frame*HOP-FFT_SIZE//2），出 64 维 mel 对数能量谱。

    与旧逐帧实现数学等价（仅 BLAS 累加顺序噪声，A/B 见 tests/selftest_audio_equiv.py）。
    reduce_background 语义未变：用 0.2 分位静音帧的 RMS/谱做减除，强度默认
    NOISE_OVER_SUBTRACTION，结果有 band_power*0.025 下限；谱做
    log1p(20*E/mean) → 去均值 → L2 归一化。
    """
    signal_lowcut = _lowcut(samples) if LOWCUT_HZ > 0.0 else samples
    raw_rms, band_power = _frame_power(signal_lowcut)
    return _features_from_power(raw_rms, band_power, reduce_background, noise_strength)



def trim_reference(f: Features) -> Features:
    """把参考模板裁到有效发声段（反汇编 ('REF', 216)）。

    空输入直接返回空 Features（反汇编 offset 2–206 与 286–480 两处早退）；
    然后就是样本里那个**看似冗余的比较**（项目状态.md 也标注了）：gate=f.rms*0.055 是
    逐帧自比，f.rms[i] >= f.rms[i]*0.055 恒真（RMS 非负），所以 active 恒为全部帧、
    首末帧外扩 1 帧后 left=0/right=len-1；真正会动裁剪的只有后面"超过 300 帧就取
    300 帧最大能量窗"那一步（现有 33 个模板帧数 76–180，都 <300，实际都不裁剪）。
    照原样复刻，不做"修正"。
    """
    if len(f.rms) == 0:
        return Features(
            np.zeros(0, dtype=np.float32),
            np.zeros((0, BANDS), dtype=np.float32),
            0.0,
            0,
        )
    gate = f.rms * 0.055
    active = np.flatnonzero(f.rms >= gate)
    if len(active) == 0:
        return Features(
            np.zeros(0, dtype=np.float32),
            np.zeros((0, BANDS), dtype=np.float32),
            0.0,
            0,
        )
    left = max(0, int(active[0]) - 1)
    right = min(len(f.rms) - 1, int(active[-1]) + 1)
    if right - left + 1 > 300:
        e = f.rms[left : right + 1].astype(np.float64) ** 2
        csum = np.concatenate(([0.0], np.cumsum(e)))
        sums = csum[300:] - csum[:-300]
        left += int(np.argmax(sums))
        right = left + 299
    rms = f.rms[left : right + 1].copy()
    spec = f.spectrum[left : right + 1].copy()
    return Features(rms, spec, float(rms.max(initial=0.0)), left)

def _scan_evaluate(
    ref: Features,
    inp: Features,
    off: int,
    peak_idx: int,
    peak_gate: float,
    ref_energy: float,
    ref_weight: float,
    background_rms: float,
) -> float:
    """单个偏移的相似度（原 _scan 闭包 evaluate 的逐字搬运，服务边角偏移与细化）。"""
    n = len(inp.rms)
    rn = len(ref.rms)
    pi = off + peak_idx
    if pi < 0 or pi >= n or inp.rms[pi] < peak_gate:
        return 0.0
    lo = max(0, -off)
    hi = min(rn, n - off)
    rr = ref.rms[lo:hi].astype(np.float64)
    ir = inp.rms[lo + off : hi + off].astype(np.float64)
    live = ir > 0
    rr, ir = rr[live], ir[live]
    if not len(rr):
        return 0.0
    renergy = float(np.dot(rr, rr))
    ienergy = float(np.dot(ir, ir))
    if renergy < ref_energy * 0.97 or ienergy < 1e-18:
        return 0.0
    if background_rms > 0 and float(ir.max()) < background_rms * 1.65:
        return 0.0
    rs = ref.spectrum[lo:hi][live]
    ins = inp.spectrum[lo + off : hi + off][live]
    dots = np.maximum(0.0, np.einsum("ij,ij->i", rs, ins, dtype=np.float64))
    spectral = float(np.dot(rr, dots)) / ref_weight
    rms_corr = float(np.dot(rr, ir) / math.sqrt(ref_energy * ienergy))
    return float(np.clip(spectral * (0.65 + 0.35 * rms_corr), 0.0, 1.0))


def _scan(ref: Features, inp: Features, background_rms: float) -> tuple[float, int]:
    """粗到细时间扫描，返回 (最高相似度, 对应帧偏移)。

    语义与旧实现逐项对齐（峰值门控 → 有效样本掩码 → 能量回退门控 → 背景门控 →
    谱相关 → score=spectral*(0.65+0.35*rms_corr)），"粗扫 step=4 / 取前三名 / 严格大于
    才更新 best"的顺序也一致，保证同分、同偏移。

    提速点在**批量化门控与打分**：旧实现对 33 模板 × 约 60 偏移逐个调用 Python 闭包，
    哪怕一次门控失败也要付十几微秒的调用开销；这里先用只依赖 RMS 的门控把偏移
    `np.searchsorted` 缩到幸存集合，谱点积只为幸存偏移计算。点积保持逐偏移的 64 维
    f32 内积（累加噪声 ~1e-8，A/B 对拍见 tests/selftest_audio_equiv.py）。
    """
    n = len(inp.rms)
    rn = len(ref.rms)
    if rn < 2 or n < rn:
        return (0.0, 0)
    ref_energy = float(np.dot(ref.rms, ref.rms))
    ref_weight = float(ref.rms.sum())
    if ref_energy < 1e-18 or ref_weight < 1e-12:
        return (0.0, 0)
    peak_idx = int(np.argmax(ref.rms))
    peak_gate = max(1e-09, background_rms * 1.65, inp.peak_rms * 0.025)
    margin = min(2, rn // 10)
    maxoff = n - rn + margin
    step = 2 if rn < 10 else 4
    offsets = np.arange(-margin, maxoff + 1, step)
    ref_rms64 = ref.rms.astype(np.float64)
    inp_rms = inp.rms
    inp_spec = inp.spectrum


    peak_at = offsets + peak_idx
    inside = (peak_at >= 0) & (peak_at < n)
    keep = np.zeros(len(offsets), dtype=bool)
    keep[inside] = inp_rms[peak_at[inside]] >= peak_gate
    if not keep.any():
        return (0.0, 0)

    scores = np.zeros(len(offsets), dtype=np.float64)
    survivors = offsets[keep]
    interior = survivors[(survivors >= 0) & (survivors <= n - rn)]
    edge = survivors[(survivors < 0) | (survivors > n - rn)]

    if len(interior):

        rms_windows = np.lib.stride_tricks.sliding_window_view(inp_rms, rn)[interior]
        live = rms_windows > 0
        rr = np.where(live, ref_rms64[None, :], 0.0)
        ir = np.where(live, rms_windows.astype(np.float64), 0.0)
        renergy = np.einsum("or,or->o", rr, rr)
        ienergy = np.einsum("or,or->o", ir, ir)
        ok = (renergy >= ref_energy * 0.97) & (ienergy >= 1e-18)
        if background_rms > 0:
            ok &= ir.max(axis=1) >= background_rms * 1.65
        interior, rr, ir, ienergy = interior[ok], rr[ok], ir[ok], ienergy[ok]

        if len(interior):

            dots = np.empty((len(interior), rn), dtype=np.float32)
            ref_spec = ref.spectrum
            for k, off in enumerate(interior):
                dots[k] = np.einsum("ij,ij->i", ref_spec, inp_spec[off : off + rn])
            np.maximum(0.0, dots, out=dots)
            dots64 = dots.astype(np.float64)
            spectral = np.einsum("or,or->o", rr, dots64) / ref_weight
            with np.errstate(divide="ignore", invalid="ignore"):
                rms_corr = np.einsum("or,or->o", rr, ir) / np.sqrt(ref_energy * ienergy)
            score = np.clip(spectral * (0.65 + 0.35 * rms_corr), 0.0, 1.0)
            scores[np.searchsorted(offsets, interior)] = score


    for off in edge:
        scores[np.searchsorted(offsets, off)] = _scan_evaluate(
            ref, inp, int(off), peak_idx, peak_gate, ref_energy, ref_weight, background_rms
        )


    best, best_offset = (0.0, 0)
    for k, off in enumerate(offsets):
        if scores[k] > best:
            best, best_offset = float(scores[k]), int(off)

    pairs = sorted(((float(scores[k]), int(off)) for k, off in enumerate(offsets)), reverse=True)

    for _, center in pairs[:3]:
        refine = list(
            range(
                max(-margin, center - step + 1),
                min(maxoff, center + step - 1) + 1,
            )
        )
        inner_refine = [off for off in refine if 0 <= off <= n - rn]
        outer_refine = [off for off in refine if not (0 <= off <= n - rn)]
        refined: dict[int, float] = {}
        if inner_refine:
            oi = np.asarray(inner_refine)
            rms_windows = np.lib.stride_tricks.sliding_window_view(inp_rms, rn)[oi]
            live = rms_windows > 0
            rr = np.where(live, ref_rms64[None, :], 0.0)
            ir = np.where(live, rms_windows.astype(np.float64), 0.0)
            renergy = np.einsum("or,or->o", rr, rr)
            ienergy = np.einsum("or,or->o", ir, ir)
            ok = (
                (renergy >= ref_energy * 0.97)
                & (ienergy >= 1e-18)
                & (inp_rms[oi + peak_idx] >= peak_gate)
            )
            if background_rms > 0:
                ok &= ir.max(axis=1) >= background_rms * 1.65
            for k, off in enumerate(oi):
                if not ok[k]:
                    refined[int(off)] = 0.0
                    continue
                dots = np.maximum(
                    0.0, np.einsum("ij,ij->i", ref.spectrum, inp_spec[off : off + rn])
                ).astype(np.float64)
                spectral = float(np.dot(rr[k], dots)) / ref_weight
                rms_corr = float(np.dot(rr[k], ir[k]) / math.sqrt(ref_energy * ienergy[k]))
                refined[int(off)] = float(np.clip(spectral * (0.65 + 0.35 * rms_corr), 0.0, 1.0))
        for off in outer_refine:
            refined[int(off)] = _scan_evaluate(
                ref, inp, int(off), peak_idx, peak_gate, ref_energy, ref_weight, background_rms
            )
        for off in refine:
            value = refined[int(off)]
            if value > best:
                best, best_offset = value, off
    return (float(best), int(best_offset))



@dataclass
class Template:
    key: str
    kind: str
    name: str
    path: Path
    samples: np.ndarray
    features: Features
    original_features: Features
    filtered_features: Features
    broadband: bool
    waveform: np.ndarray
    waveform_norm: float
    waveform_spectrum: np.ndarray

    is_extra: bool = False


class SoundMatcher:
    def __init__(self, root: Path, database_path: Path):
        self.root = Path(root)
        self.database_path = Path(database_path)
        self.database = json.loads(self.database_path.read_text(encoding="utf-8"))
        self.all_items = list(self.database.get("items", [])) + list(
            self.database.get("special_items", [])
        )
        self.templates = []
        self.errors = []
        self.last_reason = ""

        self.segment_templates = 0

        self.learned_templates = 0
        self._load_templates()
        if SPLIT_TEMPLATE_SEGMENTS:
            self._apply_segment_templates()
        if LEARNED_TEMPLATES and LEARNED_ROOT is not None:
            self._load_learned_templates()

    def _apply_segment_templates(self) -> None:
        """把"多段拼接"素材的**特征**换成它"主导段"的特征（模板条数不变）。

        为什么不"每段加一条模板"：游戏里只有那一段最响、最稳定；按段加模板会让
        **多段素材比单段素材多出一次匹配机会**，音色相近的物品之间首位就会翻转
        （实测真机把"液压扩张器/打铁声音"认成了"空桶砸地声"）。改成"只换主导段"
        后条数仍是 33，A/B 准确率与"每段都加"完全相同（93.9% vs 93.9%，见
        `_tmp/bench_template_modes.py`），既不偏袒也不变慢。

        `samples` / `waveform*` 仍保留**完整素材**：波形兜底路径照旧按整条比，
        不受影响；只有谱路径改用主导段（间隔变化时才对得上）。
        """
        replaced = 0
        for t in self.templates:
            try:
                parts = split_template_segments(t.samples)
                if not parts:
                    continue
                dominant = max(
                    parts, key=lambda p: float(np.sqrt(np.mean(np.asarray(p, np.float64) ** 2)))
                )
                original = extract(dominant)
                feat = trim_reference(original)
                if len(feat.rms) < 2 or original.peak_rms < MIN_PEAK_RMS:
                    continue
                filtered = trim_reference(extract(self._highpass(dominant)))
                t.features = feat
                t.filtered_features = filtered
                replaced += 1
            except Exception as exc:
                self.errors.append(str(exc))
        self.segment_templates = replaced

    def _load_learned_templates(self) -> None:
        """把 `learned/positive/<key>/*.wav` 追加成模板（同 key 继承原 kind）。

        每类最多 `LEARNED_MAX_PER_KEY` 条、按内容 sha1 去重；未知 key 跳过。
        任何异常只记进 self.errors，不影响正常模板。
        """
        assert LEARNED_ROOT is not None
        by_key: dict[str, list[Template]] = {}
        for t in self.templates:
            by_key.setdefault(t.key, []).append(t)
        roots = [Path(LEARNED_ROOT)] + list(EXTRA_ROOTS)
        added = 0
        seen: set[str] = set()
        key_dirs: list[Path] = []
        for root in roots:
            pos = Path(root) / "positive"
            if pos.is_dir():
                key_dirs.extend(sorted(pos.iterdir()))
        if not key_dirs:
            return
        root_of: dict[Path, Path] = {}
        for root in roots:
            pos = Path(root) / "positive"
            if pos.is_dir():
                for kd in pos.iterdir():
                    root_of.setdefault(kd, pos)
        for key_dir in key_dirs:
            if not key_dir.is_dir():
                continue
            key = key_dir.name
            same = by_key.get(key)
            if not same:
                continue
            kind = same[0].kind
            is_extra = root_of.get(key_dir) not in (None, Path(LEARNED_ROOT) / "positive")
            cap = EXTRA_MAX_PER_KEY if is_extra else LEARNED_MAX_PER_KEY
            for wav in sorted(key_dir.glob("*.wav"))[-cap:]:
                try:
                    samples, _sr = read_audio(wav)
                except Exception as exc:
                    self.errors.append(f"{wav.name}: {exc}")
                    continue
                if len(samples) < 1600:
                    continue
                digest = hashlib.sha1(
                    np.asarray(samples, dtype=np.float32).tobytes()
                ).hexdigest()
                if digest in seen:
                    continue
                seen.add(digest)
                try:
                    original = extract(samples)
                    feat = trim_reference(original)
                    if len(feat.rms) < 2 or original.peak_rms < MIN_PEAK_RMS:
                        continue
                    filtered = trim_reference(extract(self._highpass(samples)))
                    waveform = self._highpass(samples).astype(np.float32)
                    waveform -= float(np.mean(waveform))
                    if len(waveform) > int(SAMPLE_RATE * 1.5):
                        waveform = waveform[: int(SAMPLE_RATE * 1.5)]
                    self.templates.append(
                        Template(
                            key,
                            kind,
                            same[0].name,
                            wav,
                            samples,
                            feat,
                            original,
                            filtered,
                            self._has_broadband(samples),
                            waveform,
                            float(np.linalg.norm(waveform)),
                            np.fft.rfft(waveform, n=MIXED_WAVEFORM_FFT_SIZE).astype(
                                np.complex64
                            ),
                            is_extra=True,
                        )
                    )
                    added += 1
                except Exception as exc:
                    self.errors.append(f"{wav.name}: {exc}")
        self.learned_templates = added

    def _load_templates(self) -> None:
        """三段加载：sounds 列表 → special_items 音效 → sounds/*.wav 兜底（反汇编 offset 2–1306）。"""
        sd = self.root / "sounds"
        seen = set()
        item_names = {i.get("name") for i in self.database.get("items", [])}
        special_names = {i.get("name") for i in self.database.get("special_items", [])}
        for sound in self.database.get("sounds", []):
            key = sound.get("name", "")
            filename = sound.get("file") or f"{key}.wav"
            p = sd / filename
            if p.is_file() and p.resolve() not in seen:
                self._add_template(p, "normal", key, key)
                seen.add(p.resolve())
        for item in self.database.get("special_items", []):
            filename = item.get("sound_file") or item.get("sound")
            if filename:
                p = sd / filename
                if p.is_file() and p.resolve() not in seen:
                    self._add_template(p, "special", item.get("name", ""), item.get("name", ""))
                    seen.add(p.resolve())
        for p in sd.glob("*.wav"):
            if p.resolve() not in seen:
                kind = (
                    "special"
                    if p.stem in special_names
                    else "item"
                    if p.stem in item_names
                    else "normal"
                )
                self._add_template(p, kind, p.stem, p.stem)
                seen.add(p.resolve())

    def _add_template(self, p: Path, kind: str, key: str, name: str) -> None:
        """建一个模板；任何异常进 self.errors（反汇编 offset 2–1136）。"""
        try:
            samples, sr = read_audio(p)
            original = extract(samples)
            feat = trim_reference(original)
            if len(samples) >= 64:
                if len(feat.rms) >= 2:
                    if original.peak_rms >= MIN_PEAK_RMS:
                        filtered = trim_reference(extract(self._highpass(samples)))
                        broadband = self._has_broadband(samples)


                        waveform = self._highpass(samples).astype(np.float32)
                        waveform -= float(np.mean(waveform))
                        if len(waveform) > int(SAMPLE_RATE * 1.5):
                            waveform = waveform[: int(SAMPLE_RATE * 1.5)]
                        waveform_norm = float(np.linalg.norm(waveform))
                        waveform_spectrum = np.fft.rfft(
                            waveform, n=MIXED_WAVEFORM_FFT_SIZE
                        ).astype(np.complex64)
                        self.templates.append(
                            Template(
                                key,
                                kind,
                                name,
                                p,
                                samples,
                                feat,
                                original,
                                filtered,
                                broadband,
                                waveform,
                                waveform_norm,
                                waveform_spectrum,
                            )
                        )
                        return
            return
        except Exception as exc:
            self.errors.append(str(exc))

    @staticmethod
    def _has_broadband(samples: np.ndarray) -> bool:
        """频谱能量铺得够散就算宽带（最高 1/64 频带占比 <0.8）。"""
        n = 1
        while n < len(samples):
            n <<= 1
        z = np.fft.rfft(np.pad(samples, (0, n - len(samples))))
        energy = np.abs(z) ** 2
        groups = np.zeros(64, dtype=np.float64)
        for i in range(1, len(z)):
            groups[min(63, int(i * 128 / n))] += energy[i]
        total = groups.sum()
        return bool(total > 0 and groups.max() / total < 0.8)

    @staticmethod
    def _has_temporal_coverage(
        reference: np.ndarray, observed: np.ndarray, offset: int
    ) -> bool:
        """样本里的死代码（没有任何调用点），照抄保留：分段 160 帧相关性覆盖度。"""
        ref = np.asarray(reference, dtype=np.float64)
        obs = np.asarray(observed, dtype=np.float64)
        offset = max(0, offset)
        length = min(len(ref), max(0, len(obs) - offset))
        if length <= 0:
            return False
        ref = ref[:length]
        obs = obs[offset : offset + length]
        ref_energy = float(np.dot(ref, ref))
        if ref_energy <= 1e-20:
            return False
        global_similarity = abs(float(np.dot(ref, obs))) / ref_energy
        covered = 0.0
        for start in range(0, length, 160):
            a = ref[start : start + 160]
            b = obs[start : start + 160]
            if not len(a):
                continue
            ea = float(np.dot(a, a))
            eb = float(np.dot(b, b))
            dot = float(np.dot(a, b))
            if eb <= 1e-20 or ea <= 1e-20:
                continue
            if eb < ea * global_similarity * global_similarity * 0.01:
                continue
            correlation = abs(dot) / math.sqrt(ea * eb)
            if correlation >= 0.45:
                covered += ea
        return covered >= ref_energy * 0.97

    @staticmethod
    def _highpass(samples: np.ndarray) -> np.ndarray:
        """2 阶 1000 Hz Butterworth 高通（fs=16000）。

        反汇编 ('REF', 309) offset 62–260：样本里是**两次**级联 sosfilt
        （y=sosfilt(sos,samples).astype(float32) → return sosfilt(sos,y).astype(float32)，
        等效 4 阶）；项目状态.md 伪码只写了单次且带 np.asarray。
        """
        sos = signal.butter(2, 1000, fs=SAMPLE_RATE, btype="highpass", output="sos")
        y = signal.sosfilt(sos, samples).astype(np.float32)
        return signal.sosfilt(sos, y).astype(np.float32)

    def _build_prefilter(self) -> None:
        """预计算粗筛用的矩阵（模板平均谱 + 平均 RMS），供 _prefilter 用。"""
        raw_means, raw_rms, filt_means, filt_rms = [], [], [], []
        for t in self.templates:
            for feats, means, rmss in (
                (t.features, raw_means, raw_rms),
                (t.filtered_features, filt_means, filt_rms),
            ):
                if len(feats.rms) < 2:
                    means.append(None)
                    rmss.append(0.0)
                    continue
                vec = feats.spectrum.mean(axis=0)
                n = float(np.linalg.norm(vec))
                means.append(vec / n if n > 0 else vec)
                rmss.append(float(np.mean(feats.rms)))
        self._pf_raw = self._stack(means, raw_rms)
        self._pf_filt = self._stack(filt_means, filt_rms)

    @staticmethod
    def _stack(means, rmss):
        """把 (可能含 None 的) 平均谱拼成矩阵；用 0 行占位，靠 mask 排除。"""
        rows = []
        mask = []
        for vec in means:
            if vec is None:
                rows.append(np.zeros(BANDS, dtype=np.float32))
                mask.append(False)
            else:
                rows.append(np.asarray(vec, dtype=np.float32))
                mask.append(True)
        return (np.stack(rows) if rows else np.zeros((0, BANDS), np.float32),
                np.asarray(mask, dtype=bool),
                np.asarray(rmss, dtype=np.float32))

    def _prefilter(self, inp: Features, filtered: bool) -> list:
        """粗筛：返回可能值得精算的模板子集（数量 PREFILTER_TOP_K）。"""
        internal = [t for t in self.templates if not getattr(t, "is_extra", False)]
        extra = [t for t in self.templates if getattr(t, "is_extra", False)]
        if not extra or len(extra) <= PREFILTER_TOP_K:
            return self.templates
        if not hasattr(self, "_pf_raw"):
            self._build_prefilter()
        mat, mask, rmss = self._pf_filt if filtered else self._pf_raw
        if mat.shape[0] == 0 or len(inp.spectrum) == 0:
            return self.templates
        vec = inp.spectrum.mean(axis=0)
        n = float(np.linalg.norm(vec))
        if n <= 0:
            return self.templates
        coarse = mat @ (vec / n)

        in_rms = max(float(np.mean(inp.rms)), 1e-9)
        ratio = np.maximum(rmss, 1e-9) / in_rms
        penalty = 0.15 * np.abs(np.log(np.maximum(ratio, 1e-9)))
        scores = np.where(mask, coarse - penalty, -9.0)

        scores = np.array([-9.0 if not getattr(t, "is_extra", False) else sc
                           for sc, t in zip(scores, self.templates)], dtype=np.float32)

        if filtered:
            scores = np.array([sc if t.broadband else -9.0
                               for sc, t in zip(scores, self.templates)], dtype=np.float32)
        k = min(PREFILTER_TOP_K, len(extra))
        order = np.argsort(-scores)[:k]
        chosen = [self.templates[i] for i in order if getattr(self.templates[i], "is_extra", False)]
        self.prefilter_hits = len(chosen)
        return internal + chosen

    def _rank(self, inp: Features, background_rms: float, filtered: bool = False) -> list[dict[str, Any]]:
        """按 (_scan 相似度) 给模板排序（同 kind+key 只留最高分）。"""
        best = {}
        for t in self._prefilter(inp, filtered):
            if filtered and not t.broadband:
                continue
            ref = t.filtered_features if filtered else t.features
            if len(ref.rms) < 2:
                continue
            score, off = _scan(ref, inp, background_rms)
            old = best.get((t.kind, t.key))
            if old is None or score > old["score"]:
                best[(t.kind, t.key)] = {
                    "key": t.key,
                    "kind": t.kind,
                    "score": score,
                    "offset": off,
                    "offset_seconds": off * HOP / SAMPLE_RATE,
                    "template": t,
                    "feature_start": ref.start_frame,
                }
        return sorted(best.values(), key=lambda x: x["score"], reverse=True)

    def _waveform_rank(self, samples: np.ndarray) -> list[dict[str, Any]]:
        """Rank templates by normalized waveform correlation in a noisy window."""
        x = self._highpass(np.asarray(samples, dtype=np.float32).reshape(-1))
        x -= float(np.mean(x))
        if not len(x):
            return []
        energy = np.concatenate(([0.0], np.cumsum(x.astype(np.float64) ** 2)))
        use_cached_fft = (
            len(x) <= 40000
            and max((len(t.waveform) for t in self.templates), default=0) + len(x) - 1
            <= MIXED_WAVEFORM_FFT_SIZE
        )
        x_spectrum = np.fft.rfft(x, n=MIXED_WAVEFORM_FFT_SIZE) if use_cached_fft else None
        best = {}
        for t in self.templates:
            ref = t.waveform
            rn = len(ref)
            if rn < 64 or rn > len(x) or t.waveform_norm <= 1e-12:
                continue
            if use_cached_fft:


                corr = np.fft.irfft(
                    x_spectrum * np.conj(t.waveform_spectrum), n=MIXED_WAVEFORM_FFT_SIZE
                )[: len(x) - rn + 1]
            else:
                corr = signal.correlate(x, ref, mode="valid", method="fft")
            pos = int(np.argmax(np.abs(corr)))
            local_energy = max(1e-20, float(energy[pos + rn] - energy[pos]))
            score = float(abs(corr[pos]) / (math.sqrt(local_energy) * t.waveform_norm))
            old = best.get((t.kind, t.key))
            if old is None or score > old["score"]:
                best[(t.kind, t.key)] = {
                    "key": t.key,
                    "kind": t.kind,
                    "score": score,
                    "offset": pos,
                    "offset_seconds": pos / SAMPLE_RATE,
                    "template": t,
                    "feature_start": 0,
                }
        return sorted(best.values(), key=lambda row: row["score"], reverse=True)

    def match(
        self,
        samples: np.ndarray,
        sr: int,
        limit: int = MAX_CANDIDATES,
        background_rms: float = 0.0,
    ):
        """识别一个音频窗口，返回按相似度降序的候选行（无结果返回 []，原因在 last_reason）。

        8 条中文原因串与全部分支/阈值逐字照抄反汇编（见 docs/recognition_dis.txt
        ('REF', 361) offset 4–3022）。提速改造只做了两件**不动判定**的事：

          1. 高通后的帧功率只算一次，filtered/gentle 两条路复用（它们只差噪声减除强度）；
          2. `background_rms >= 0.0004` 时原始谱路径的分数根本不参与判定
             （`... or background_rms >= 0.0004 or ...` 短路为真），于是把它**推迟**到
             滤波/温和两条路都没被采纳时才算——省一次 33 模板 rank。
        """
        if sr != SAMPLE_RATE and len(samples):
            g = math.gcd(int(sr), SAMPLE_RATE)
            samples = signal.resample_poly(samples, SAMPLE_RATE // g, int(sr) // g).astype(
                np.float32
            )
        x = np.asarray(samples, dtype=np.float32).reshape(-1)
        if len(x) < 128:
            self.last_reason = "音频窗口太短。"
            return []
        if len(x) > 192000:
            self.last_reason = "请将音频分为不超过12秒的窗口。"
            return []
        input_power = _frame_power(x)
        inp = _features_from_power(input_power[0], input_power[1], True)
        if inp.peak_rms < MIN_PEAK_RMS:
            self.last_reason = "未检测到足够清晰的声音。"
            return []
        if len(x) >= 8000:
            ordered = np.sort(inp.rms)
            q = float(ordered[min(len(ordered) - 1, len(ordered) // 5)])
            if q > 0 and inp.peak_rms < q * 1.8:
                self.last_reason = "声音持续且缺少清晰的短音效，暂不判断。"
                return []
        ranked = None
        raw_score = 0.0
        should_filter = background_rms >= 0.0004
        if not should_filter:

            ranked = self._rank(inp, background_rms)[: max(2, limit)]
            raw_score = ranked[0]["score"] if ranked else 0.0
            should_filter = raw_score < MIN_SIMILARITY
        selected_filtered = False
        selected_threshold = MIN_SIMILARITY
        if should_filter and 640 <= len(x) <= 48000:
            highpassed = self._highpass(x)
            highpass_power = _frame_power(highpassed)
            filtered_inp = _features_from_power(highpass_power[0], highpass_power[1], True)
            filtered = self._rank(filtered_inp, 0.0, filtered=True)[: max(2, limit)]
            if filtered:
                filtered_score = filtered[0]["score"]
                filtered_margin = (
                    filtered_score - filtered[1]["score"] if len(filtered) > 1 else 1.0
                )
                normal_pass = filtered_score >= FILTERED_MIN_SIMILARITY
                adaptive_pass = (
                    background_rms >= ADAPTIVE_NOISE_FLOOR
                    and filtered_score >= ADAPTIVE_FILTERED_MIN_SIMILARITY
                    and filtered_margin >= ADAPTIVE_MIN_MARGIN
                )

                if (normal_pass or adaptive_pass) and (
                    background_rms >= 0.0004 or not ranked or filtered_score > raw_score
                ):
                    ranked = filtered
                    selected_filtered = True
                    selected_threshold = (
                        FILTERED_MIN_SIMILARITY if normal_pass else ADAPTIVE_FILTERED_MIN_SIMILARITY
                    )
            if not selected_filtered and background_rms >= ADAPTIVE_NOISE_FLOOR:

                gentle_inp = _features_from_power(
                    highpass_power[0], highpass_power[1], True, GENTLE_NOISE_SUBTRACTION
                )
                gentle = self._rank(gentle_inp, 0.0, filtered=True)[: max(2, limit)]
                if gentle:
                    gentle_score = gentle[0]["score"]
                    gentle_margin = gentle_score - gentle[1]["score"] if len(gentle) > 1 else 1.0
                    if (
                        gentle_score >= GENTLE_FILTERED_MIN_SIMILARITY
                        and gentle_margin >= GENTLE_MIN_MARGIN
                    ):
                        ranked = gentle
                        selected_filtered = True
                        selected_threshold = GENTLE_FILTERED_MIN_SIMILARITY
        if not selected_filtered:
            if ranked is None:

                ranked = self._rank(inp, background_rms)[: max(2, limit)]
                raw_score = ranked[0]["score"] if ranked else 0.0
            waveform = self._waveform_rank(x)[: max(2, limit)]

            spectral_passed = bool(ranked) and raw_score >= MIN_SIMILARITY
            if waveform and (WAVEFORM_OVERRIDES_PASSED_SPECTRAL or not spectral_passed):
                wave_margin = waveform[0]["score"] - waveform[1]["score"] if len(waveform) > 1 else 1.0
                if (
                    waveform[0]["score"] >= MIXED_WAVEFORM_MIN_CORRELATION
                    and wave_margin >= MIXED_WAVEFORM_MIN_MARGIN
                ):
                    ranked = waveform
                    selected_filtered = True
                    selected_threshold = MIXED_WAVEFORM_MIN_CORRELATION
        threshold = selected_threshold if selected_filtered else MIN_SIMILARITY
        if not ranked or ranked[0]["score"] < threshold:
            self.last_reason = "与参考样本不够相似，可能是未知音效或背景声。"
            return []
        ambiguous = len(ranked) > 1 and ranked[0]["score"] - ranked[1]["score"] < AMBIGUITY_MARGIN
        if ambiguous:
            self.last_reason = "多个声音组相似度接近，以下候选需要结合占格或画面判断。"
        else:
            self.last_reason = "匹配到声音组；组内物品可能同音，相似度不代表出货概率。"
        output = []
        for row in ranked[:limit]:
            t = row["template"]
            if row["kind"] == "special":
                items = [
                    i
                    for i in self.database.get("special_items", [])
                    if i.get("name") == row["key"]
                ]
            elif row["kind"] == "item":
                items = [
                    i for i in self.database.get("items", []) if i.get("name") == row["key"]
                ]
            else:
                items = [
                    i
                    for i in self.database.get("items", [])
                    if i.get("sound_type") == row["key"]
                ]
                if not items:
                    items = [{"name": row["key"], "sound_type": row["key"]}]
            output.append(
                {
                    "key": row["key"],
                    "kind": row["kind"],
                    "score": int(row["score"] * 100),
                    "offset": int(row.get("offset", 0)),
                    "offset_seconds": float(row.get("offset_seconds", 0.0)),
                    "items": items,
                    "reason": "已匹配；相似度不代表出货概率。",
                }
            )
        return output[:limit]
