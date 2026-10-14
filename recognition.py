"""Python port of the audio matching/filter pipeline identified in MerchantAssistant.dll.

It follows the reference build's 16 kHz mono, 512 point / 64 sample hop,
64-band mel log-energy frames, peak/RMS gating, coarse-to-fine temporal scan,
reference trimming, and optional low-frequency background rejection.

本文件是样本 `recognition.pyc` 的 1:1 复刻（逐指令重建，见 docs/recognition_dis.txt
与 docs/recognition_resolved.txt）。与 `项目状态.md` 第三章伪码的差异全都在反汇编里
逐条核对过，并在对应处注明（起点 offset）。
唯一**有意**改动：match() 结尾 `return output[:limit]`（样本为 `return output[:1]`，
只给榜首；本工具要把完整候选列表交给 UI）。

import 期无副作用：只建 mel 滤波器组与汉宁窗常量，不建模板、不读盘、不开音频设备。
"""

from __future__ import annotations

import json
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


def extract(
    samples: np.ndarray,
    reduce_background: bool = False,
    noise_strength: float | None = None,
) -> Features:
    """按 64 样本跳步切帧（居中，start=frame*HOP-FFT_SIZE//2），出 64 维 mel 对数能量谱。

    反汇编 ('REF', 196)：rms 是切除"静音底噪"后的逐帧 RMS；reduce_background 时
    用 0.2 分位静音帧的 RMS/谱做减除，减除强度默认 NOISE_OVER_SUBTRACTION，
    且减除结果有 band_power*0.025 的下限；谱向量做 log1p(20*E/mean) → 去均值 → L2 归一化。
    """
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    count = max(1, (len(x) + HOP - 1) // HOP + 1)
    raw_rms = np.zeros(count, dtype=np.float32)
    spectra = np.zeros((count, BANDS), dtype=np.float32)
    band_power = np.zeros((count, BANDS), dtype=np.float32)
    for frame in range(count):
        start = frame * HOP - FFT_SIZE // 2
        lo = max(0, start)
        hi = min(len(x), start + FFT_SIZE)
        block = np.zeros(FFT_SIZE, dtype=np.float32)
        if hi > lo:
            block[lo - start : hi - start] = x[lo:hi]
        # 逐帧 RMS（原样：block.astype(float64)**2 再 mean 再 sqrt）
        raw_rms[frame] = np.sqrt(np.mean(block.astype(np.float64) ** 2))
        z = np.fft.rfft(block * _WINDOW)
        power = z.real * z.real + z.imag * z.imag
        bands = _FILTER_BANK @ power.astype(np.float32)
        band_power[frame] = bands
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
        band_power = np.maximum(band_power - strength * noise_profile[None, :], band_power * 0.025)
    for frame in range(count):
        bands = band_power[frame]
        mean_energy = float(np.mean(bands))
        if mean_energy <= 1e-24:
            continue
        v = np.log1p(20.0 * bands / mean_energy).astype(np.float32)
        v -= np.mean(v)
        norm = float(np.linalg.norm(v))
        if norm > 1e-18:
            spectra[frame] = v / norm
    return Features(rms, spectra, float(rms.max(initial=0.0)))


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


def _scan(ref: Features, inp: Features, background_rms: float) -> tuple[float, int]:
    """粗到细时间扫描，返回 (最高相似度, 对应帧偏移)（反汇编 ('REF', 273) + evaluate）。

    evaluate 闭包的顺序与阈值照抄：
      峰值门控 → 有效样本掩码 ir>0 → 能量回退门控 renergy<ref_energy*0.97
      → 背景门控 ir.max()<background_rms*1.65
      → spectral=dot(rr, max(0, row-dot))/ref_weight
      → score=clip(spectral*(0.65+0.35*rms_corr),0,1)
    另外反汇编 offset 298–324 还有一个早退：
      ref_energy<1e-18 or ref_weight<1e-12 → (0.0, 0)（项目状态.md 伪码没写这一条）。
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
    offsets = list(range(-margin, maxoff + 1, step))
    best, best_offset = (0.0, 0)
    top = []

    def evaluate(off: int) -> float:
        nonlocal best, best_offset
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
        score = float(np.clip(spectral * (0.65 + 0.35 * rms_corr), 0.0, 1.0))
        if score > best:
            best, best_offset = score, off
        return score

    for off in offsets:
        score = evaluate(off)
        top.append((score, off))
    for _, center in sorted(top, reverse=True)[:3]:
        for off in range(max(-margin, center - step + 1), min(maxoff, center + step - 1) + 1):
            evaluate(off)
    return (best, best_offset)


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
        self._load_templates()

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
                        # 反汇编 offset 400/462：astype(np.float32) 且 -= float(np.mean(waveform))
                        # （项目状态.md 伪码写的是 float64 / np.min，反汇编是 float32 / np.mean）
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

    def _rank(self, inp: Features, background_rms: float, filtered: bool = False) -> list[dict[str, Any]]:
        """按 (_scan 相似度) 给模板排序（同 kind+key 只留最高分）。"""
        best = {}
        for t in self.templates:
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
                # 反汇编 offset 854：irfft 的 n 也是 MIXED_WAVEFORM_FFT_SIZE
                # （项目状态.md 伪码写成 FFT，且该函数 co_names 里根本没有 FFT_SIZE）
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
        ('REF', 361) offset 4–3022）。
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
        inp = extract(x, reduce_background=True)
        if inp.peak_rms < MIN_PEAK_RMS:
            self.last_reason = "未检测到足够清晰的声音。"
            return []
        if len(x) >= 8000:
            ordered = np.sort(inp.rms)
            q = float(ordered[min(len(ordered) - 1, len(ordered) // 5)])
            if q > 0 and inp.peak_rms < q * 1.8:
                self.last_reason = "声音持续且缺少清晰的短音效，暂不判断。"
                return []
        ranked = self._rank(inp, background_rms)[: max(2, limit)]
        raw_score = ranked[0]["score"] if ranked else 0.0
        selected_filtered = False
        selected_threshold = MIN_SIMILARITY
        should_filter = background_rms >= 0.0004 or raw_score < MIN_SIMILARITY
        if should_filter and 640 <= len(x) <= 48000:
            highpassed = self._highpass(x)
            filtered_inp = extract(highpassed, reduce_background=True)
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
                # 反汇编 offset 1490–1504 是 `or not ranked or filtered_score > raw_score`
                # （`ranked` 为假时直接跳到采纳分支；项目状态.md 伪码写的是 `(ranked and ...)`）
                if (normal_pass or adaptive_pass) and (
                    background_rms >= 0.0004 or not ranked or filtered_score > raw_score
                ):
                    ranked = filtered
                    selected_filtered = True
                    selected_threshold = (
                        FILTERED_MIN_SIMILARITY if normal_pass else ADAPTIVE_FILTERED_MIN_SIMILARITY
                    )
            if not selected_filtered and background_rms >= ADAPTIVE_NOISE_FLOOR:
                gentle_inp = extract(
                    highpassed,
                    reduce_background=True,
                    noise_strength=GENTLE_NOISE_SUBTRACTION,
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
            waveform = self._waveform_rank(x)[: max(2, limit)]
            if waveform:
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
            t = row["template"]  # 样本里赋值后未使用（保留以逐字复刻）
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
        return output[:limit]  # 唯一有意改动：样本为 output[:1]