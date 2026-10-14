r"""recognition.py 自测（原始数据证据）。

运行（PowerShell 直接重定向 Python stdout 会失败，必须走 cmd）：
  cmd /d /c "D:\PY\python.exe D:\pycharm\UA\行商助手\tests\selftest_recognition.py > D:\pycharm\UA\行商助手\tests\out_selftest_recognition.txt 2>&1"

覆盖：模板加载数/errors、逐模板自识别命中率与 score 中位数、静音输入 last_reason、
真实环回录音（tests\\loopback_test.npy，44100 Hz，含音效）匹配返回行、加载与单次 match 耗时、
以及 import 期无副作用的实测（用 open 间谍检查 import 期间没有读 sounds/ 或 database.json）。
"""

from __future__ import annotations

import builtins
import statistics
import sys
import time
from pathlib import Path

import numpy as np

try:  # 保证重定向到文件时是 UTF-8，中文原因串可读
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
DATABASE = ASSETS / "database.json"
LOOPBACK = Path(__file__).resolve().parent / "loopback_test.npy"

sys.path.insert(0, str(ROOT))

# ---- import 期副作用实测：把 builtins.open 换成间谍后再 import recognition ----
_opened: list[str] = []
_real_open = builtins.open


def _spy_open(file, *args, **kwargs):
    try:
        _opened.append(str(file))
    except Exception:
        pass
    return _real_open(file, *args, **kwargs)


builtins.open = _spy_open
import recognition  # noqa: E402

builtins.open = _real_open
_import_side_effect_hits = [p for p in _opened if "sounds" in p or "database.json" in p]

print("=" * 78)
print("recognition 自测 / 原始数据")
print("=" * 78)
print(f"python        : {sys.version.split()[0]}")
print(f"root          : {ROOT}")
print(f"assets        : {ASSETS}")
print(f"database      : {DATABASE}")
print(f"loopback      : {LOOPBACK}  exists={LOOPBACK.is_file()}")
print("-" * 78)
print("[常量与模块级表]")
print(f"SAMPLE_RATE={recognition.SAMPLE_RATE} FFT_SIZE={recognition.FFT_SIZE} "
      f"HOP={recognition.HOP} BANDS={recognition.BANDS}")
print(f"MIN_SIMILARITY={recognition.MIN_SIMILARITY} AMBIGUITY_MARGIN={recognition.AMBIGUITY_MARGIN} "
      f"MIN_PEAK_RMS={recognition.MIN_PEAK_RMS} MAX_CANDIDATES={recognition.MAX_CANDIDATES}")
print(f"NOISE_OVER_SUBTRACTION={recognition.NOISE_OVER_SUBTRACTION} "
      f"FILTERED_MIN_SIMILARITY={recognition.FILTERED_MIN_SIMILARITY} "
      f"ADAPTIVE_NOISE_FLOOR={recognition.ADAPTIVE_NOISE_FLOOR} "
      f"ADAPTIVE_FILTERED_MIN_SIMILARITY={recognition.ADAPTIVE_FILTERED_MIN_SIMILARITY} "
      f"ADAPTIVE_MIN_MARGIN={recognition.ADAPTIVE_MIN_MARGIN}")
print(f"GENTLE_NOISE_SUBTRACTION={recognition.GENTLE_NOISE_SUBTRACTION} "
      f"GENTLE_FILTERED_MIN_SIMILARITY={recognition.GENTLE_FILTERED_MIN_SIMILARITY} "
      f"GENTLE_MIN_MARGIN={recognition.GENTLE_MIN_MARGIN}")
print(f"MIXED_WAVEFORM_MIN_CORRELATION={recognition.MIXED_WAVEFORM_MIN_CORRELATION} "
      f"MIXED_WAVEFORM_MIN_MARGIN={recognition.MIXED_WAVEFORM_MIN_MARGIN} "
      f"MIXED_WAVEFORM_FFT_SIZE={recognition.MIXED_WAVEFORM_FFT_SIZE}")
print(f"_FILTER_BANK shape={recognition._FILTER_BANK.shape} dtype={recognition._FILTER_BANK.dtype} "
      f"rowsum[min,max]=({recognition._FILTER_BANK.sum(axis=1).min():.6f},"
      f"{recognition._FILTER_BANK.sum(axis=1).max():.6f})")
print(f"_WINDOW       len={len(recognition._WINDOW)} dtype={recognition._WINDOW.dtype}")
print(f"[import 副作用] 导入期间被打开的 sounds/ 或 database.json 文件数 = "
      f"{len(_import_side_effect_hits)}  {_import_side_effect_hits}")

# ---------------------------------------------------------------- 1) 模板加载
print("-" * 78)
print("[1] 模板加载")
t0 = time.perf_counter()
matcher = recognition.SoundMatcher(ASSETS, DATABASE)
load_seconds = time.perf_counter() - t0
print(f"len(matcher.templates) = {len(matcher.templates)}")
print(f"len(matcher.errors)    = {len(matcher.errors)}")
print(f"matcher.errors         = {matcher.errors!r}")
print(f"len(matcher.all_items) = {len(matcher.all_items)}")
print(f"模板加载耗时           = {load_seconds * 1000:.1f} ms")
kinds: dict[str, int] = {}
for t in matcher.templates:
    kinds[t.kind] = kinds.get(t.kind, 0) + 1
print(f"按 kind 统计           = {kinds}")
print("逐模板: idx key kind name len(samples) len(feat.rms) peak_rms len(filtered.rms) "
      "broadband waveform_norm")
for i, t in enumerate(matcher.templates):
    print(
        f"  {i:2d} {t.key} | {t.kind} | {t.name} | {len(t.samples)} | "
        f"{len(t.features.rms)} | {t.original_features.peak_rms:.6f} | "
        f"{len(t.filtered_features.rms)} | {t.broadband} | {t.waveform_norm:.3f}"
    )

# ---------------------------------------------------------------- 2) 自识别
print("-" * 78)
print("[2] 自识别：每个模板用自身 wav 走 read_audio + match(x, 16000, 5, 0.0)")
self_scores: list[int] = []
self_sims: list[float] = []
hits = 0
rows_counts: list[int] = []
print("逐模板: idx key -> top1_key top1_kind score rows hit")
for i, t in enumerate(matcher.templates):
    x, sr = recognition.read_audio(t.path)
    rows = matcher.match(x, 16000, 5, 0.0)
    if rows:
        top = rows[0]
        hit = top["key"] == t.key
        hits += 1 if hit else 0
        self_scores.append(top["score"])
        self_sims.append(top["score"] / 100.0)
        rows_counts.append(len(rows))
        print(
            f"  {i:2d} {t.key} ({len(x)} samples) -> {top['key']} [{top['kind']}] "
            f"score={top['score']} rows={len(rows)} hit={'Y' if hit else 'N'} "
            f"reason={top['reason']}"
        )
    else:
        rows_counts.append(0)
        print(
            f"  {i:2d} {t.key} ({len(x)} samples) -> [] last_reason={matcher.last_reason}"
        )
total = len(matcher.templates)
hit_rate = (hits / total) if total else 0.0
print(f"自识别 top-1 命中自身 key: {hits}/{total} = {hit_rate * 100:.1f}%")
if self_scores:
    print(f"自识别 top-1 score: 中位数={statistics.median(self_scores)} "
          f"最低={min(self_scores)} 最高={max(self_scores)}")
    print(f"自识别 top-1 相似度: 中位数={statistics.median(self_sims):.4f} "
          f"最低={min(self_sims):.4f}")
else:
    print("自识别 top-1 score: 无（全部无结果）")
print(f"每次 match 返回行数: {rows_counts}")
print(f"通过阈值判定: 命中率 {hit_rate * 100:.1f}% >= 90% -> {hit_rate >= 0.90}")

# ---------------------------------------------------------------- 3) 静音
print("-" * 78)
print("[3] 静音输入 match(np.zeros(16000, np.float32), 16000, 5, 0.0)")
silence = np.zeros(16000, np.float32)
srows = matcher.match(silence, 16000, 5, 0.0)
silence_reason = matcher.last_reason
print(f"返回 = {srows!r}")
print(f"last_reason = {silence_reason!r}")
print(f"期望 [] 且 last_reason == '未检测到足够清晰的声音。' -> "
      f"{srows == [] and silence_reason == '未检测到足够清晰的声音。'}")

# 顺带跑其它原因串分支（边界证据）
edge_cases = [
    ("len<128", np.zeros(64, np.float32), "音频窗口太短。"),
    ("len>192000", np.zeros(192001, np.float32), "请将音频分为不超过12秒的窗口。"),
]
for label, arr, expect in edge_cases:
    rows = matcher.match(arr, 16000, 5, 0.0)
    print(f"  边界 {label}: rows={rows!r} last_reason={matcher.last_reason!r} "
          f"expect={expect!r} ok={rows == [] and matcher.last_reason == expect}")

# ---------------------------------------------------------------- 4) 真实录音
print("-" * 78)
print("[4] 真实环回录音 tests\\loopback_test.npy")
if LOOPBACK.is_file():
    y = np.load(LOOPBACK)
    print(f"shape={y.shape} dtype={y.dtype} 时长={len(y) / 44100:.3f}s @44100 "
          f"峰值={float(np.max(np.abs(y))):.5f} RMS={float(np.sqrt(np.mean(y.astype(np.float64) ** 2))):.6f}")
    t0 = time.perf_counter()
    rows = matcher.match(y, 44100, 5, 0.0)
    loop_ms = (time.perf_counter() - t0) * 1000
    print(f"match(y, 44100, 5, 0.0) 返回 {len(rows)} 行  耗时={loop_ms:.1f} ms")
    print(f"last_reason = {matcher.last_reason!r}")
    for j, row in enumerate(rows):
        names = [it.get("name") for it in row["items"]]
        print(
            f"  row{j}: key={row['key']!r} kind={row['kind']!r} score={row['score']} "
            f"offset={row['offset']} offset_seconds={row['offset_seconds']:.4f} "
            f"items={len(row['items'])} reason={row['reason']!r}"
        )
        print(f"        items.name={names}")

    # ---- 4b) 为什么给出 0 行？把三路候选的原始分数打出来（阈值逐字未改） ----
    print("-" * 78)
    print("[4b] 实录诊断（未改任何阈值，属于原始数据）")
    gg = recognition.math.gcd(44100, recognition.SAMPLE_RATE)
    xr = recognition.signal.resample_poly(
        y, recognition.SAMPLE_RATE // gg, 44100 // gg
    ).astype(np.float32)
    inp_r = recognition.extract(xr, reduce_background=True)
    hp_r = matcher._highpass(xr)
    finp_r = recognition.extract(hp_r, reduce_background=True)
    raw_r = matcher._rank(inp_r, 0.0)
    filt_r = matcher._rank(finp_r, 0.0, filtered=True)
    wave_r = matcher._waveform_rank(xr)
    print(f"重采样后长度={len(xr)}  inp.peak_rms={inp_r.peak_rms:.6f} "
          f"raw_rms_max={float(inp_r.rms.max()):.6f}")
    print("raw  (_rank 未滤波)  top5: "
          + ", ".join(f"{r['key']}={r['score']:.4f}" for r in raw_r[:5]))
    print("filt (_rank 滤波/宽带) top5: "
          + ", ".join(f"{r['key']}={r['score']:.4f}" for r in filt_r[:5]))
    print("wave (_waveform_rank) top5: "
          + ", ".join(f"{r['key']}={r['score']:.4f}" for r in wave_r[:5]))
    if len(wave_r) > 1:
        print(f"wave margin(1,2)={wave_r[0]['score'] - wave_r[1]['score']:.4f} "
              f"(需要 >= MIXED_WAVEFORM_MIN_MARGIN={recognition.MIXED_WAVEFORM_MIN_MARGIN})")
    if len(filt_r) > 1:
        print(f"filt margin(1,2)={filt_r[0]['score'] - filt_r[1]['score']:.4f}")
    coin = next((t for t in matcher.templates if t.key == "撒币声音"), None)
    if coin is not None:
        cs, coff = recognition._scan(coin.filtered_features, finp_r, 0.0)
        print(f"撒币声音 模板 filtered _scan 直接打分={cs:.4f} offset={coff} "
              f"({coff * recognition.HOP / recognition.SAMPLE_RATE:.3f}s)")
        print(f"阈值: FILTERED_MIN_SIMILARITY={recognition.FILTERED_MIN_SIMILARITY} "
              f"MIN_SIMILARITY={recognition.MIN_SIMILARITY} "
              f"MIXED_WAVEFORM_MIN_CORRELATION={recognition.MIXED_WAVEFORM_MIN_CORRELATION} "
              f"MIXED_WAVEFORM_MIN_MARGIN={recognition.MIXED_WAVEFORM_MIN_MARGIN}")
        print(f"reference wav RMS={float(np.sqrt(np.mean(coin.samples.astype(np.float64) ** 2))):.6f} "
              f"peak={float(np.max(np.abs(coin.samples))):.6f}")
        bg_chunk = xr[int(1.55 * 16000): int(2.0 * 16000)]
        print(f"实录背景段(1.55-2.0s) RMS="
              f"{float(np.sqrt(np.mean(bg_chunk.astype(np.float64) ** 2))):.6f} "
              f"（比参考 wav 整段 RMS 高 "
              f"{float(np.sqrt(np.mean(bg_chunk.astype(np.float64) ** 2))) / max(1e-12, float(np.sqrt(np.mean(coin.samples.astype(np.float64) ** 2)))):.1f} 倍）")
        # SNR 回归：干净模板 + 实录背景 ×alpha（16 kHz），看分数随信噪比的变化
        clean = coin.samples.astype(np.float32)
        snr_line = []
        for alpha in (0.0, 0.2, 0.4, 0.6, 1.0, 2.0):
            bgv = np.zeros(len(clean), np.float32)
            bgv[:] = bg_chunk[: len(clean)]
            rows_a = matcher.match((clean + alpha * bgv).astype(np.float32), 16000, 5, 0.0)
            top_a = rows_a[0] if rows_a else None
            snr_line.append(
                f"alpha={alpha}:"
                + (f"{top_a['key']}/{top_a['score']}" if top_a else "[]")
            )
        print("SNR 回归（干净模板+实录背景×alpha）: " + "  ".join(snr_line))
else:
    print(f"缺失：{LOOPBACK}")

# ---------------------------------------------------------------- 5) 性能
print("-" * 78)
print("[5] 性能")
print(f"模板加载（{len(matcher.templates)} 个模板）耗时 = {load_seconds * 1000:.1f} ms")
probe = matcher.templates[0]
px, _ = recognition.read_audio(probe.path)
times = []
for _ in range(5):
    t0 = time.perf_counter()
    matcher.match(px, 16000, 5, 0.0)
    times.append((time.perf_counter() - t0) * 1000)
print(f"单次 match（{len(px)} samples 干净模板音，含 {len(matcher.templates)} 模板扫描）："
      f"min={min(times):.1f} ms  median={statistics.median(times):.1f} ms  "
      f"all={[round(t, 1) for t in times]}")
if LOOPBACK.is_file():
    times2 = []
    for _ in range(3):
        t0 = time.perf_counter()
        matcher.match(y, 44100, 5, 0.0)
        times2.append((time.perf_counter() - t0) * 1000)
    print(f"单次 match（实录 {len(y)} samples @44100，含重采样/高通/滤波多路扫描）："
          f"min={min(times2):.1f} ms  median={statistics.median(times2):.1f} ms  "
          f"all={[round(t, 1) for t in times2]}")

print("=" * 78)
print("[6] read_audio 两条读盘路径 + 错误路径（soundfile 已装，ImportError 回退平时跑不到）")
_fallback_probe = matcher.templates[0].path
_y_sf, _sr_sf = recognition.read_audio(_fallback_probe)
import builtins as _bt  # noqa: E402

_real_import = _bt.__import__


def _block_soundfile(name, *args, **kwargs):
    if name == "soundfile":
        raise ImportError("blocked by selftest")
    return _real_import(name, *args, **kwargs)


_bt.__import__ = _block_soundfile
try:
    _y_wf, _sr_wf = recognition.read_audio(_fallback_probe)
finally:
    _bt.__import__ = _real_import
_same = len(_y_sf) == len(_y_wf) and _sr_sf == _sr_wf
_diff = (
    float(np.max(np.abs(_y_sf.astype(np.float64) - _y_wf.astype(np.float64)))) if _same else None
)
print(f"探测文件={_fallback_probe.name}")
print(f"soundfile : len={len(_y_sf)} dtype={_y_sf.dtype} sr={_sr_sf} "
      f"rms={float(np.sqrt(np.mean(_y_sf.astype(np.float64) ** 2))):.8f}")
print(f"wavfile   : len={len(_y_wf)} dtype={_y_wf.dtype} sr={_sr_wf} "
      f"rms={float(np.sqrt(np.mean(_y_wf.astype(np.float64) ** 2))):.8f}")
print(f"两条路径一致: len/sr 相同={_same} max|diff|={_diff}")
try:
    recognition.read_audio(_fallback_probe.with_name("__missing__.wav"))
    print("缺失文件: 未抛异常（不符合预期）")
except ValueError as exc:
    print(f"缺失文件: ValueError('{exc}')  前缀正确="
          f"{str(exc).startswith('无法读取音频 __missing__.wav: ')}")
except Exception as exc:  # noqa: BLE001
    print(f"缺失文件: 抛了 {type(exc).__name__}（样本应转成 ValueError）")

print("=" * 78)
print("结论：")
print(f"  模板数={len(matcher.templates)} errors={len(matcher.errors)}")
print(f"  自识别命中率={hit_rate * 100:.1f}% score 中位数="
      f"{statistics.median(self_scores) if self_scores else None}")
print(f"  静音 last_reason={silence_reason!r}")
print(f"  import 副作用读盘次数={len(_import_side_effect_hits)}")
print("=" * 78)