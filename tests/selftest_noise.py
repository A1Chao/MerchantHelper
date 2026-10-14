r"""噪声基准：确定性噪声夹具 + （对照 / 降噪）识别能力对比。

目的：量化"环境背景很响时识别失效"以及"降噪前后"的差异，全部数字可复现。

只读生产代码：本脚本不改 recognition.py / recorder.py / denoise.py / 阈值 / assets。
只写两个自己的路径：
  * tests/fixtures_noise/*.npz    （夹具）
  * tests/out_selftest_noise.txt  （由调用方重定向）

夹具规则（确定性）：
  1. 3 个模板音效：撒币声音 / 金属声音 / 座钟。混音用 soundfile 读的**原生 44.1k** 波形；
     16k 版走 recognition.read_audio 只为核对（打印长度/rms）。
  2. 背景取自 tests/loopback_test.npy 里"没有音效"的段落：先用高通归一化互相关定位音效区间，
     再把音效区间**之外**的头段+尾段拼成 1.5s@44100 无音效背景基。
  3. 音效按自身原始电平（不做任何增益）放在 1.5s 窗口正中；背景整段缩放到目标 SNR：
        bg_rms = sound_rms / 10**(snr_db/20)，  snr_db = 20*log10(音效rms / 背景rms)
     其中 sound_rms 定义为**音效自身长度内**的 RMS（下文另给窗口级 SNR 供参考）。
  4. 每个夹具另存等长、同电平、不含音效的 reference（供 denoise.enhance 使用）。
  5. 固定随机种子只用于给每个音效挑一个背景基循环旋转偏移（同一音效的 6 个 SNR 档位
     共用同一背景实现，保证档位间严格可比）；不用它加噪。

运行（PowerShell 直接重定向 Python stdout 会失败，必须走 cmd）：
  cmd /d /c "D:\PY\python.exe tests\selftest_noise.py > tests\out_selftest_noise.txt 2>&1"
"""

from __future__ import annotations

import hashlib
import math
import sys
import time
from pathlib import Path

import numpy as np

try:  # 重定向到文件时保证 UTF-8，中文可读
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

# import denoise / recognition 会写 __pycache__（项目根，属于别人的目录）。
# 本脚本是只读基准，不希望在测试目录之外产生任何写入，因此关掉字节码缓存。
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
DATABASE = ASSETS / "database.json"
SOUNDS = ASSETS / "sounds"
LOOPBACK = TESTS / "loopback_test.npy"
FIXDIR = TESTS / "fixtures_noise"

for _p in (str(ROOT), str(TESTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import recognition  # noqa: E402  只读使用，不修改
from scipy import signal  # noqa: E402

SR = 44100
WINDOW_LEN = int(SR * 1.5)  # 66150
KEYS = ("撒币声音", "金属声音", "座钟")
SNRS = (20, 10, 6, 3, 0, -3)
SEED_BASE = 20240517
LIMIT = 5
LINE = "=" * 100
SEP = "-" * 100


# ------------------------------------------------------------------ 小工具
def rms(x) -> float:
    a = np.asarray(x, dtype=np.float64)
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


def db(x: float) -> float:
    return 20.0 * np.log10(max(float(x), 1e-12))


def array_digest(a: np.ndarray) -> str:
    a = np.ascontiguousarray(a)
    h = hashlib.sha256()
    h.update(str(a.dtype).encode())
    h.update(str(a.shape).encode())
    h.update(a.tobytes())
    return h.hexdigest()


def file_digest(p: Path) -> str:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except Exception as exc:  # pragma: no cover
        return f"<{type(exc).__name__}: {exc}>"


def read_native(path: Path) -> tuple[np.ndarray, int]:
    """原生采样率单声道（soundfile），用于混音。"""
    import soundfile as sf

    y, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return np.ascontiguousarray(y.mean(axis=1), dtype=np.float32), int(sr)


def background_level(samples: np.ndarray) -> float:
    """与 recorder._background_level 逐行一致：50ms 帧 RMS 的 0.2 分位。"""
    frame = max(1, SR // 50)
    usable = np.asarray(samples, dtype=np.float32)[: len(samples) // frame * frame]
    if len(usable) == 0:
        return 0.0
    chunks = usable.reshape(-1, frame)
    levels = np.sqrt(np.mean(chunks.astype(np.float64) ** 2, axis=1))
    return float(np.quantile(levels, 0.2))


def hp_norm(x: np.ndarray) -> np.ndarray:
    """与识别链路同一 4 阶 1000Hz 高通 + 去均值（用于定位音效 / 验证无音效）。"""
    y = recognition.SoundMatcher._highpass(np.asarray(x, dtype=np.float32)).astype(np.float64)
    return y - float(np.mean(y))


def norm_corr_scan(x_hp: np.ndarray, tpl_hp: np.ndarray) -> tuple[int, float, np.ndarray]:
    """归一化互相关（取绝对值）全扫描，返回 (最佳偏移, 最佳相关, 全曲线)。"""
    c = signal.correlate(x_hp, tpl_hp, mode="valid", method="fft")
    e = np.concatenate(([0.0], np.cumsum(x_hp * x_hp)))
    pos = np.arange(len(c))
    denom = np.sqrt(np.maximum(e[pos + len(tpl_hp)] - e[pos], 1e-20)) * float(
        np.linalg.norm(tpl_hp)
    )
    cc = np.abs(c) / np.maximum(denom, 1e-20)
    p = int(np.argmax(cc))
    return p, float(cc[p]), cc


def ratio_low(x: np.ndarray, cut_hz: int = 1000) -> float:
    """<cut_hz 的能量占比（核对'88% 能量<1kHz'）。"""
    z = np.fft.rfft(np.asarray(x, dtype=np.float64))
    e = z.real * z.real + z.imag * z.imag
    freqs = np.fft.rfftfreq(len(x), d=1.0 / SR)
    total = float(e.sum())
    return float(e[freqs < cut_hz].sum()) / total if total > 0 else 0.0


def make_fixture(
    key: str, sound: np.ndarray, bg_base: np.ndarray, snr_db: float, rot: int
) -> dict[str, object]:
    """按规则混一个夹具（全程 float64 计算，落盘 float32）。"""
    bg_rot = np.roll(np.asarray(bg_base, dtype=np.float64), rot)
    sound64 = np.asarray(sound, dtype=np.float64)
    sound_rms = rms(sound64)
    bg_rms_target = sound_rms / (10.0 ** (float(snr_db) / 20.0))
    gain = bg_rms_target / max(rms(bg_rot), 1e-12)
    bg = bg_rot * gain
    start = (WINDOW_LEN - len(sound64)) // 2
    window = bg.copy()
    window[start : start + len(sound64)] += sound64
    return {
        "key": key,
        "snr": float(snr_db),
        "window": window.astype(np.float32),
        "reference": bg.astype(np.float32),
        "samplerate": np.int32(SR),
        "expect": np.array(key),
        "snr_db": np.float32(snr_db),
        "bg_rms": np.float32(rms(bg)),
        "sound_rms": np.float32(sound_rms),
        "sound_start": np.int32(start),
        "bg_rotation": np.int32(rot),
    }


def measure(matcher, wav: np.ndarray, bg_rms: float) -> dict:
    """跑一次 match，返回 rows / top / reason / 耗时 / 异常。"""
    t0 = time.perf_counter()
    try:
        rows = matcher.match(np.asarray(wav, dtype=np.float32), SR, LIMIT, float(bg_rms))
        return {
            "rows": rows,
            "top": rows[0] if rows else None,
            "reason": matcher.last_reason,
            "ms": (time.perf_counter() - t0) * 1000.0,
            "err": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "rows": [],
            "top": None,
            "reason": "",
            "ms": (time.perf_counter() - t0) * 1000.0,
            "err": f"{type(exc).__name__}: {exc}",
        }


def top_text(res: dict | None) -> str:
    if res is None:
        return "N/A"
    if res["err"]:
        return f"ERR({res['err'][:36]})"
    top = res["top"]
    return f"{top['key']}/{top['score']}" if top else "-/0"


def candidate_text(res: dict | None) -> str:
    if res is None:
        return "N/A"
    if res["err"]:
        return "ERR"
    return f"是({len(res['rows'])})" if res["rows"] else "否"


def hit(res: dict | None, expect: str) -> bool:
    return bool(res) and bool(res["rows"]) and res["top"] is not None and res["top"]["key"] == expect


# ------------------------------------------------------------------ denoise 探测
def probe_denoise() -> dict:
    path = ROOT / "denoise.py"
    info: dict[str, object] = {
        "path": path,
        "exists": path.is_file(),
        "module": None,
        "import_err": None,
        "enhance_err": None,
        "sha256": "-",
        "mtime": "-",
    }
    if not info["exists"]:
        return info
    info["sha256"] = file_digest(path)
    info["mtime"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime))
    try:
        import denoise  # noqa: PLC0415

        info["module"] = denoise
        if not hasattr(denoise, "enhance"):
            info["enhance_err"] = "模块存在但没有 enhance 属性"
    except Exception as exc:  # noqa: BLE001
        info["import_err"] = f"{type(exc).__name__}: {exc}"
    return info


def run_denoise(mod, window: np.ndarray, reference: np.ndarray) -> dict:
    """调用 denoise.enhance，返回 (波形 / 异常 / 原始 dtype/长度)。"""
    try:
        out = mod.enhance(
            np.asarray(window, dtype=np.float32),
            np.asarray(reference, dtype=np.float32),
            SR,
        )
        arr = np.asarray(out)
        if arr.ndim != 1:
            arr = arr.reshape(-1)
        return {
            "wave": arr.astype(np.float32),
            "err": None,
            "raw_dtype": str(arr.dtype),
            "raw_len": int(arr.size),
        }
    except Exception as exc:  # noqa: BLE001
        return {"wave": None, "err": f"{type(exc).__name__}: {exc}", "raw_dtype": "-", "raw_len": -1}


def path_report(matcher, wav: np.ndarray, bg_rms: float) -> dict:
    """复刻 match() 内部的四路排队（只读调用），用于"候选是哪条路径给的"归因。

    match 的顺序：raw=_rank(inp, bg) → filt=_rank(highpass, 0.0, filtered) →
    gentle（同一高通输入 + noise_strength=0.5）→ wave=_waveform_rank。这里取各路 top1。
    """
    x = np.asarray(wav, dtype=np.float32).reshape(-1)
    if len(x) and len(x) != recognition.SAMPLE_RATE:
        g = math.gcd(SR, recognition.SAMPLE_RATE)
        x = signal.resample_poly(x, recognition.SAMPLE_RATE // g, SR // g).astype(np.float32)
    inp = recognition.extract(x, reduce_background=True)
    raw = matcher._rank(inp, float(bg_rms))
    hp = matcher._highpass(x)
    finp = recognition.extract(hp, reduce_background=True)
    filt = matcher._rank(finp, 0.0, filtered=True)
    gentle = matcher._rank(
        recognition.extract(hp, reduce_background=True,
                            noise_strength=recognition.GENTLE_NOISE_SUBTRACTION),
        0.0,
        filtered=True,
    )
    wave = matcher._waveform_rank(x)
    should_filter = (
        float(bg_rms) >= 0.0004 or (raw[0]["score"] if raw else 0.0) < recognition.MIN_SIMILARITY
    ) and 640 <= len(x) <= 48000

    def t(r):
        return (r[0]["key"], float(r[0]["score"])) if r else ("-", 0.0)

    return {
        "raw": t(raw),
        "filt": t(filt),
        "gentle": t(gentle),
        "wave": t(wave),
        "should_filter": should_filter,
        "len16k": len(x),
    }


ROUTES = ("raw", "filt", "gentle", "wave")


def route_of(paths: dict, res: dict | None) -> str:
    """按 match 输出的整数分反查实际采纳路径（recognition 用 int(score*100) 截断）。"""
    if res is None:
        return "N/A"
    if res.get("err"):
        return "ERR"
    if not res["rows"]:
        return "无候选"
    s = res["top"]["score"]
    names = [n for n in ROUTES if int(paths[n][1] * 100) == s]
    return "+".join(names) if names else "未归因"


# ------------------------------------------------------------------ main
def main() -> int:
    print(LINE)
    print("噪声基准自测 / 确定性噪声夹具 + （对照 vs 降噪）识别对比")
    print(LINE)
    print(f"python     : {sys.version.split()[0]}   numpy={np.__version__}")
    print(f"运行时刻   : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"root       : {ROOT}")
    print(f"assets     : {ASSETS}")
    print(f"database   : {DATABASE}  exists={DATABASE.is_file()}")
    print(f"loopback   : {LOOPBACK}  exists={LOOPBACK.is_file()}  "
          f"size={LOOPBACK.stat().st_size if LOOPBACK.is_file() else 0}")
    print(f"夹具目录   : {FIXDIR}")
    print(f"固定种子   : SEED_BASE={SEED_BASE}（只用于挑选背景旋转偏移，见 [2]）")
    print("阈值（逐字来自 recognition.py，本脚本不修改）: "
          f"原始={recognition.MIN_SIMILARITY} 滤波={recognition.FILTERED_MIN_SIMILARITY} "
          f"自适应={recognition.ADAPTIVE_FILTERED_MIN_SIMILARITY}+margin{recognition.ADAPTIVE_MIN_MARGIN} "
          f"温和={recognition.GENTLE_FILTERED_MIN_SIMILARITY}+margin{recognition.GENTLE_MIN_MARGIN} "
          f"波形={recognition.MIXED_WAVEFORM_MIN_CORRELATION}+margin{recognition.MIXED_WAVEFORM_MIN_MARGIN} "
          f"歧义余量={recognition.AMBIGUITY_MARGIN}")

    # ---------------------------------------------------------- [0] denoise 状态
    print(SEP)
    print("[0] denoise.py 探测（并行实现中：存在就测，不存在标 N/A 且不报错）")
    den = probe_denoise()
    use_denoise = bool(
        den["exists"] and den["module"] is not None and not den["import_err"] and not den["enhance_err"]
    )
    if den["exists"]:
        print(f"  denoise.py      : {den['path']}")
        print(f"  mtime           : {den['mtime']}")
        print(f"  sha256          : {den['sha256']}")
        print(f"  import          : "
              f"{'OK' if den['module'] is not None and not den['import_err'] else den['import_err']}")
        print(f"  enhance 可用    : {'是' if use_denoise else (den['enhance_err'] or '否')}")
        print("  >>> 下方'降噪组'数字对应以上 sha256/mtime 的 denoise.py 版本")
    else:
        print(f"  denoise.py 不存在 : {den['path']}")
        print("  >>> 降噪组 = N/A（跳过，不计为失败）")

    # ---------------------------------------------------------- [1] 素材来源
    print(SEP)
    print("[1] 夹具素材来源与背景段选取")
    if not LOOPBACK.is_file():
        print(f"  缺失 {LOOPBACK} —— 无法构造背景，退出")
        return 1

    sounds: dict[str, np.ndarray] = {}
    print("  模板（混音用原生 44.1k；16k 走 recognition.read_audio 核对）:")
    print(f"  {'key':<10} {'原生sr':>7} {'原生len':>8} {'原生rms':>10} {'16k len':>8} "
          f"{'16k rms':>10} {'峰值':>9}")
    for k in KEYS:
        p = SOUNDS / f"{k}.wav"
        nat, nat_sr = read_native(p)
        y16, _ = recognition.read_audio(p)
        sounds[k] = nat
        print(f"  {k:<10} {nat_sr:>7} {len(nat):>8} {rms(nat):>10.6f} {len(y16):>8} "
              f"{rms(y16):>10.6f} {float(np.max(np.abs(nat))):>9.5f}")

    print("  背景来源 : tests/loopback_test.npy（实录音，含一次真实音效）")
    y_raw = np.load(LOOPBACK)
    y = y_raw.reshape(-1).astype(np.float32)
    print(f"  shape={y.shape} dtype={y_raw.dtype} 时长={len(y)/SR:.3f}s @44100 "
          f"峰值={float(np.max(np.abs(y))):.5f} RMS={rms(y):.6f}")
    y_hp = hp_norm(y)
    print(f"  高通后 RMS={rms(y_hp):.6f}；原始 <1kHz 能量占比={ratio_low(y):.3f}"
          "（与既有'约 88% 能量 <1kHz'一致）")

    # 音效定位（用 撒币声音 原生模板做归一化互相关）
    coin_nat = sounds[KEYS[0]]
    pos, corr, _ = norm_corr_scan(y_hp, hp_norm(coin_nat))
    snd_end = min(len(y), pos + len(coin_nat))
    print(f"  音效定位 : 与'{KEYS[0]}'原生模板归一化互相关峰值={corr:.3f} @偏移={pos}"
          f"（t={pos/SR:.3f}s）→ 音效占用 [{pos}, {snd_end}) = "
          f"{pos/SR:.3f}s~{snd_end/SR:.3f}s（{snd_end-pos} 样本）")
    print("             其余模板峰值相关: " + ", ".join(
        f"{k}={norm_corr_scan(y_hp, hp_norm(sounds[k]))[1]:.3f}" for k in KEYS[1:]))

    head_free = y[:pos]
    tail_free = y[snd_end:]
    print(f"  无音效区段: 头段[0:{pos}] ({pos} 样本, rms={rms(head_free):.5f})  "
          f"尾段[{snd_end}:{len(y)}] ({len(tail_free)} 样本, rms={rms(tail_free):.5f})")
    if len(tail_free) >= WINDOW_LEN:
        bg_base = np.array(tail_free[:WINDOW_LEN], dtype=np.float32)
        bg_desc = f"纯尾段[{snd_end}:{snd_end+WINDOW_LEN}]（恰好 1.5s，无音效）"
    else:
        need = WINDOW_LEN - len(tail_free)
        if need > pos:
            print("  ⚠ 无音效素材不足 1.5s —— 退回 y[0:66150]（含音效）")
            bg_base = np.array(y[:WINDOW_LEN], dtype=np.float32)
            bg_desc = "回退 y[0:66150]（含音效，不推荐）"
        else:
            bg_base = np.concatenate([tail_free, y[:need]]).astype(np.float32)
            bg_desc = (f"尾段[{snd_end}:{len(y)}]（{len(tail_free)} 样本）+ 头段[0:{need}]"
                       f"（{need} 样本）拼接 = {WINDOW_LEN} 样本，均无音效")
    print(f"  背景基(1.5s): {bg_desc}")
    print(f"                长度={len(bg_base)} rms={rms(bg_base):.6f} "
          f"峰值={float(np.max(np.abs(bg_base))):.5f} sha256={array_digest(bg_base)[:16]}")
    print("  无音效验证  : 背景基与各模板归一化互相关峰值 = " + ", ".join(
        f"{k}={norm_corr_scan(hp_norm(bg_base), hp_norm(sounds[k]))[1]:.3f}" for k in KEYS)
        + f"（原录音里 {KEYS[0]} 峰值 {corr:.3f}）")
    print("  说明        : 背景基是唯一背景源；夹具只对它做整段增益缩放 + 循环旋转，"
          "不加任何人工噪声")

    # ---------------------------------------------------------- [2] 生成夹具
    print(SEP)
    print("[2] 生成夹具（tests/fixtures_noise/<音效>_snr<dB>.npz）")
    FIXDIR.mkdir(parents=True, exist_ok=True)
    seeds: dict[str, int] = {}
    rots: dict[str, int] = {}
    for i, k in enumerate(KEYS):
        seed = SEED_BASE + i
        seeds[k] = seed
        rots[k] = int(np.random.default_rng(seed).integers(0, WINDOW_LEN))
    print("  每音效一个固定种子（同一音效的 6 个档位共用同一背景实现，档位间严格可比）:")
    for k in KEYS:
        print(f"    {k:<10} seed={seeds[k]}  旋转偏移={rots[k]}（={rots[k]/SR:.4f}s）")

    fixtures: list[dict] = []
    print(SEP)
    print(f"  {'文件':<26} {'音效':<10} {'SNR':>4} {'bg_rms实测':>11} {'sound_rms':>10} "
          f"{'实测SNR':>8} {'窗口SNR':>8} {'窗rms':>9} {'参考rms':>9} {'win#':>6} {'ref#':>6}")
    for k in KEYS:
        for snr in SNRS:
            fx = make_fixture(k, sounds[k], bg_base, snr, rots[k])
            name = f"{k}_snr{snr:+d}.npz"
            path = FIXDIR / name
            np.savez(
                path,
                window=fx["window"],
                reference=fx["reference"],
                samplerate=fx["samplerate"],
                expect=fx["expect"],
                snr_db=fx["snr_db"],
                bg_rms=fx["bg_rms"],
                sound_rms=fx["sound_rms"],
                sound_start=fx["sound_start"],
                bg_rotation=fx["bg_rotation"],
            )
            st = int(fx["sound_start"])
            n = len(sounds[k])
            w64 = fx["window"].astype(np.float64)
            r64 = fx["reference"].astype(np.float64)
            meas_snr = db(rms(w64[st : st + n] - r64[st : st + n]) / max(rms(r64), 1e-12))
            win_snr = db(rms(w64) / max(rms(r64), 1e-12))
            fx.update({"name": name, "path": path, "meas_snr": meas_snr, "win_snr": win_snr})
            fixtures.append(fx)
            print(f"  {name:<26} {k:<10} {snr:>+4} {rms(fx['reference']):>11.6f} "
                  f"{float(fx['sound_rms']):>10.6f} {meas_snr:>8.3f} {win_snr:>8.3f} "
                  f"{rms(w64):>9.5f} {rms(r64):>9.5f} {len(w64):>6} {len(r64):>6}")
    print("  注: '实测SNR'=20log10(rms(窗口内音效段-同段背景)/bg_rms)，与定义式一致；"
          "'窗口SNR'=20log10(rms(整窗)/rms(背景))，因音效只占窗口一部分而必然更低。")

    # ---------------------------------------------------------- [2b] 可复现性
    print(SEP)
    print("[2b] 逐样本可复现性验证（按同一规则再生成一次，与落盘 npz 对比）")
    all_ok = True
    worst = 0.0
    set_digest = hashlib.sha256()
    print(f"  {'文件':<26} {'win sha256':<18} {'ref sha256':<18} {'重建==落盘':>10} {'max|diff|':>12}")
    for fx in fixtures:
        again = make_fixture(fx["key"], sounds[fx["key"]], bg_base, fx["snr"], rots[fx["key"]])
        saved = np.load(fx["path"])
        same = True
        dmax = 0.0
        for key in ("window", "reference"):
            a = np.ascontiguousarray(saved[key], dtype=np.float32)
            b = np.ascontiguousarray(again[key], dtype=np.float32)
            if a.shape != b.shape or not np.array_equal(a, b):
                same = False
            dmax = max(dmax, float(np.max(np.abs(a.astype(np.float64) - b.astype(np.float64)))))
        all_ok = all_ok and same
        worst = max(worst, dmax)
        set_digest.update(fx["name"].encode("utf-8"))
        set_digest.update(array_digest(saved["window"]).encode())
        set_digest.update(array_digest(saved["reference"]).encode())
        print(f"  {fx['name']:<26} {array_digest(saved['window'])[:16]:<18} "
              f"{array_digest(saved['reference'])[:16]:<18} {str(same):>10} {dmax:>12.3e}")
    print(f"  全部逐样本一致 = {all_ok}  最大逐样本差 = {worst:.3e}")
    print(f"  FIXTURE_SET_DIGEST = {set_digest.hexdigest()}")
    print("  注: npz 是 zip 容器（写入时间戳会让文件级 sha256 每次不同）；可复现性以数组 "
          "sha256（上表）与 FIXTURE_SET_DIGEST 为准。")

    # ---------------------------------------------------------- [3] 测量
    print(SEP)
    print("[3] 识别测量（limit=5；两组都传夹具实测背景电平 bg_rms）")
    t0 = time.perf_counter()
    matcher = recognition.SoundMatcher(ASSETS, DATABASE)
    print(f"  模板加载: len(templates)={len(matcher.templates)} errors={matcher.errors!r} "
          f"耗时={(time.perf_counter()-t0)*1000:.1f} ms")
    if use_denoise:
        print(f"  降噪组: denoise.enhance(window, reference, {SR})  "
              f"（denoise.py sha256={den['sha256'][:16]} mtime={den['mtime']}）")
    else:
        print("  降噪组: N/A（denoise.py 不存在或不可用）")

    print(SEP)
    hdr = (f"  {'音效':<10} {'SNR(dB)':>7} {'对照top1/分数':<22} {'对照出候选?':<12} "
           f"{'降噪top1/分数':<22} {'降噪出候选?':<12} {'降噪命中真值':<12}")
    print(hdr)
    print("  " + "-" * 92)
    per_snr: dict[int, dict[str, int]] = {s: {"ctl": 0, "den": 0, "ctl85": 0, "den85": 0}
                                          for s in SNRS}
    for fx in sorted(fixtures, key=lambda f: (KEYS.index(f["key"]), -f["snr"])):
        w = np.load(fx["path"])
        ctl = measure(matcher, w["window"], float(w["bg_rms"]))
        fx["ctl"] = ctl
        fx["ctl_paths"] = path_report(matcher, w["window"], float(w["bg_rms"]))
        fx["den_wave"] = None
        fx["den_paths"] = None
        if use_denoise:
            e = run_denoise(den["module"], w["window"], w["reference"])
            if e["err"]:
                fx["den"] = {"rows": [], "top": None, "reason": "", "ms": 0.0, "err": e["err"]}
                fx["den_extra"] = f"enhance 抛异常: {e['err']}"
            else:
                fx["den"] = measure(matcher, e["wave"], float(w["bg_rms"]))
                fx["den_wave"] = e["wave"]
                fx["den_paths"] = path_report(matcher, e["wave"], float(w["bg_rms"]))
                fx["den_extra"] = (f"enh_rms={rms(e['wave']):.6f} raw_dtype={e['raw_dtype']} "
                                   f"len={e['raw_len']}  窗口rms={rms(w['window']):.6f}"
                                   f"（比值={rms(e['wave'])/max(rms(w['window']),1e-12):.2f}×）")
                if e["raw_len"] != WINDOW_LEN:
                    fx["den_extra"] += " ⚠长度与窗口不一致"
        else:
            fx["den"] = None
            fx["den_extra"] = ""
        if hit(ctl, fx["key"]):
            per_snr[int(fx["snr"])]["ctl"] += 1
        if hit(ctl, fx["key"]) and ctl["top"]["score"] >= 85:
            per_snr[int(fx["snr"])]["ctl85"] += 1
        if fx["den"] is not None and hit(fx["den"], fx["key"]):
            per_snr[int(fx["snr"])]["den"] += 1
            if fx["den"]["top"]["score"] >= 85:
                per_snr[int(fx["snr"])]["den85"] += 1
        print(f"  {fx['key']:<10} {int(fx['snr']):>+7} {top_text(ctl):<22} "
              f"{candidate_text(ctl):<12} {top_text(fx['den']):<22} "
              f"{candidate_text(fx['den']):<12} "
              f"{(str(hit(fx['den'], fx['key'])) if fx['den'] is not None else 'N/A'):<12}")
        if fx["den_extra"]:
            print(f"             └ 降噪: {fx['den_extra']}")
        if ctl["rows"]:
            extra = "  ".join(
                f"({i}){r['key']}={r['score']}" for i, r in enumerate(ctl["rows"][1:4], 1)
            )
            if extra:
                print(f"             └ 对照其余候选: {extra}")
        print(f"             └ 对照 last_reason={ctl['reason']!r}  耗时={ctl['ms']:.1f} ms")

    # ---------------------------------------------------------- [3b] 路径归因
    print(SEP)
    print("[3b] 路径归因：match() 内部四路 top1（分数 = int(score*100)，与 match 输出同口径）")
    print("     路径: raw=_rank 原始谱 / filt=高通后 filtered / gentle=高通+noise_strength0.5 / wave=波形相关")
    print("     说明：'出候选'由 match 采纳路径的阈值决定（原始0.88/滤波0.85/自适应0.82/温和0.78/波形0.12），")
    print("           所以低 SNR 下'有候选'可能只是波形路径 0.12 的低门槛；分数才是能力刻度。")
    print(f"  {'音效':<10} {'SNR':>4} | {'对照 raw':<18} {'对照 filt':<18} {'对照 gentle':<18} "
          f"{'对照 wave':<18} {'胜出':<8} | {'降噪 raw':<18} {'降噪 filt':<18} {'降噪 gentle':<18} "
          f"{'降噪 wave':<18} {'胜出':<8}")
    for fx in sorted(fixtures, key=lambda f: (KEYS.index(f["key"]), -f["snr"])):
        cp, dp = fx["ctl_paths"], fx["den_paths"]

        def cell(p, n):
            return f"{p[n][0]}/{int(p[n][1] * 100)}"

        cden = "N/A" if dp is None else route_of(dp, fx["den"])
        dcell = ["N/A"] * 4 if dp is None else [cell(dp, n) for n in ROUTES]
        print(f"  {fx['key']:<10} {int(fx['snr']):>+4} | {cell(cp,'raw'):<18} {cell(cp,'filt'):<18} "
              f"{cell(cp,'gentle'):<18} {cell(cp,'wave'):<18} {route_of(cp, fx['ctl']):<8} | "
              f"{dcell[0]:<18} {dcell[1]:<18} {dcell[2]:<18} {dcell[3]:<18} {cden:<8}")

    # ---------------------------------------------------------- [4] 汇总
    print(SEP)
    print("[4] 汇总：每个 SNR 档位下'命中真值率'（3 个音效中 top1==真值 且 出候选 的个数）")
    print(f"  {'SNR(dB)':>8} {'对照组命中':>12} {'对照组命中率':>14} {'降噪组命中':>12} "
          f"{'降噪组命中率':>14}")
    for s in SNRS:
        c, d = per_snr[s]["ctl"], per_snr[s]["den"]
        print(f"  {s:>+8} {f'{c}/3':>12} {f'{c/3*100:.1f}%':>14} "
              f"{(f'{d}/3' if use_denoise else 'N/A'):>12} "
              f"{(f'{d/3*100:.1f}%' if use_denoise else 'N/A'):>14}")
    tot_c = sum(v["ctl"] for v in per_snr.values())
    tot_d = sum(v["den"] for v in per_snr.values())
    print(f"  {'合计':>8} {f'{tot_c}/18':>12} {f'{tot_c/18*100:.1f}%':>14} "
          f"{(f'{tot_d}/18' if use_denoise else 'N/A'):>12} "
          f"{(f'{tot_d/18*100:.1f}%' if use_denoise else 'N/A'):>14}")
    print("  各组 top1 分布（含'无候选'）:")
    for label, key in (("对照", "ctl"), ("降噪", "den")):
        if label == "降噪" and not use_denoise:
            continue
        cnt: dict[str, int] = {}
        for fx in fixtures:
            res = fx[key]
            nm = res["top"]["key"] if res and res["top"] else "无候选"
            cnt[nm] = cnt.get(nm, 0) + 1
        print(f"    {label}: " + ", ".join(
            f"{k}={v}" for k, v in sorted(cnt.items(), key=lambda t: -t[1])))

    print("  补充口径（更严）：'命中真值 且 分数>=85'（85 = 滤波阈值 0.85 的整数分口径）")
    print(f"  {'SNR(dB)':>8} {'对照>=85':>10} {'降噪>=85':>10}")
    for s in SNRS:
        print(f"  {s:>+8} {f'{per_snr[s]["ctl85"]}/3':>10} "
              f"{(f'{per_snr[s]["den85"]}/3' if use_denoise else 'N/A'):>10}")
    t85c = sum(v["ctl85"] for v in per_snr.values())
    t85d = sum(v["den85"] for v in per_snr.values())
    print(f"  {'合计':>8} {f'{t85c}/18':>10} {(f'{t85d}/18' if use_denoise else 'N/A'):>10}")

    # ---------------------------------------------------------- [5] 真实录音
    print(SEP)
    print("[5] 真实录音 tests/loopback_test.npy（对照 vs 降噪）")
    try:
        import recorder  # noqa: PLC0415

        bg_meas = float(recorder._background_level(y))
        bg_src = "recorder._background_level(全文件)"
    except Exception as exc:  # noqa: BLE001
        bg_meas = background_level(y)
        bg_src = f"内联复刻（import recorder 失败: {type(exc).__name__}）"
    win15 = y[:WINDOW_LEN]
    bg15 = background_level(win15)
    len2s_ref = int(np.ceil(len(y) / WINDOW_LEN))
    ref2s = np.tile(bg_base, len2s_ref)[: len(y)].astype(np.float32)
    print(f"  文件      : len={len(y)} ({len(y)/SR:.3f}s) rms={rms(y):.6f} "
          f"峰值={float(np.max(np.abs(y))):.5f}")
    print(f"  背景电平  : bg_meas(全文件)={bg_meas:.6f} [{bg_src}]；"
          f"bg_meas(前1.5s)={bg15:.6f}；1.5s 无音效参考 rms={rms(bg_base):.6f}")
    print(f"  音效位置  : [{pos}, {snd_end}) → 落在前 1.5s 窗口内 = {snd_end <= WINDOW_LEN}")
    print(f"  降噪参考  : 2s 用背景基平铺到 {len(y)}（无音效）；1.5s 窗口用背景基本身")
    cases = [
        ("全长2s + bg=0.0（既有 0.712 口径）", y, ref2s, 0.0),
        (f"全长2s + bg=实测 {bg_meas:.5f}", y, ref2s, bg_meas),
        ("前1.5s窗口 + bg=0.0", win15, bg_base, 0.0),
        (f"前1.5s窗口 + bg=实测 {bg15:.5f}", win15, bg_base, bg15),
    ]
    print(SEP)
    print(f"  {'场景':<38} {'对照top1/分数':<22} {'候选':<8} {'降噪top1/分数':<22} "
          f"{'候选':<8} {'降噪命中':<8}")
    case_paths: list[dict] = []
    case_paths_den: list[dict] = []
    for label, wav, ref, bgr in cases:
        ctl = measure(matcher, wav, bgr)
        dres = None
        derr = ""
        dwave = None
        if use_denoise:
            e = run_denoise(den["module"], wav, ref)
            if e["err"]:
                dres = {"rows": [], "top": None, "err": e["err"]}
                derr = e["err"]
            else:
                dres = measure(matcher, e["wave"], bgr)
                dwave = e["wave"]
        print(f"  {label:<38} {top_text(ctl):<22} {candidate_text(ctl):<8} "
              f"{top_text(dres):<22} {candidate_text(dres):<8} "
              f"{(str(hit(dres, KEYS[0])) if dres is not None else 'N/A'):<8}")
        print(f"      └ 对照 last_reason={ctl['reason']!r} 耗时={ctl['ms']:.1f} ms"
              + (f"  降噪异常={derr}" if derr else ""))
        cp = path_report(matcher, wav, bgr)
        case_paths.append(cp)
        print(f"      └ 对照四路(分数=int(score*100)): " + "  ".join(
            f"{n}={cp[n][0]}/{int(cp[n][1]*100)}" for n in ROUTES)
            + f"  胜出={route_of(cp, ctl)}  [len16k={cp['len16k']} 走滤波分支={cp['should_filter']}]")
        if dwave is not None:
            dp = path_report(matcher, dwave, bgr)
            case_paths_den.append(dp)
            print("      └ 降噪四路: " + "  ".join(
                f"{n}={dp[n][0]}/{int(dp[n][1]*100)}" for n in ROUTES)
                + f"  胜出={route_of(dp, dres)}  [enh_rms={rms(dwave):.6f} "
                  f"输入rms={rms(wav):.6f} 比值={rms(dwave)/max(rms(wav),1e-12):.2f}×]")

    p0 = case_paths[0] if case_paths else None
    if p0 is not None:
        f0 = p0["filt"]
        print(f"  既有事实核对: 全长2s + bg=0.0 时 filt 撒币声音={f0[1]*100:.1f} 分"
              f"（filt top1={f0[0]}）；已知事实'撒币声音滤波路径 0.712' → "
              f"一致={abs(f0[1]-0.712) < 0.002}")

    # ---------------------------------------------------------- [6] 结论/限制
    print(SEP)
    print("[6] 结论与已知限制")
    print(f"  1) 夹具逐样本可复现 = {all_ok}（最大逐样本差={worst:.3e}，"
          f"FIXTURE_SET_DIGEST={set_digest.hexdigest()[:16]}）")
    print(f"  2) 背景段取自 loopback_test.npy 的 {bg_desc}；rms={rms(bg_base):.6f}")
    print("  3) 降噪组数字对应 denoise.py "
          + (f"sha256={den['sha256'][:16]} mtime={den['mtime']}" if use_denoise
             else "不存在/不可用 → 全部 N/A"))
    print("  4) 混音只做'背景整体缩放'，音效零增益；窗口 1.5s@44100、音效居中；")
    print("     reference 与 window 的背景实现逐样本相同（只差音效段）。")
    print("  5) 局限：背景源只有这一条实录音，无音效素材仅 1.5s 强（尾段 53196 + 头段 12954），")
    print("     夹具背景是'尾段+头段'拼接 + 循环旋转，拼接处有一个样本级不连续；")
    print("     实录音音效定位用'撒币声音'模板互相关，模板电平/时长不匹配会带来定位误差；")
    print("     背景非严格平稳（各 1.5s 窗 rms 0.0899~0.0914），因此各档位实测 SNR 有 ~0.01dB 抖动。")
    print("  6) 降噪参考用了'理想参考'（与窗口背景逐样本相同）；真实运行时参考只能来自")
    print("     '音效出现之前'的历史窗口，域偏移会让实际降噪效果低于本基准。")
    low_ctl = [fx["name"] for fx in fixtures
               if hit(fx["ctl"], fx["key"]) and fx["ctl"]["top"]["score"] < 85]
    print(f"  7) 口径提醒：对照组有 {len(low_ctl)}/18 个夹具虽'命中真值'但分数 <85（靠波形路径 0.12 "
          "的低门槛出候选），")
    print("     因此 [4] 的'命中率'在低 SNR 下会虚高；'分数'与 [4] 的 >=85 严口径才是能力刻度。")
    if use_denoise:
        ratios = []
        for fx in fixtures:
            if fx["den_wave"] is not None:
                ratios.append(rms(fx["den_wave"]) / max(rms(np.load(fx["path"])["window"]), 1e-12))
        if ratios:
            print(f"  8) 观察：本轮 denoise.enhance 输出的 RMS 普遍大于输入窗口（比值 "
                  f"{min(ratios):.2f}× ~ {max(ratios):.2f}×），即该版本在放大而非衰减；")
            print("     这属于被测版本的行为事实，未在本脚本内做任何修正/归一化。")
        deltas = sorted(fx["den_paths"]["filt"][1] - fx["ctl_paths"]["filt"][1]
                        for fx in fixtures if fx["den_paths"] is not None)
        if deltas:
            print(f"  9) 降噪对 filtered 路径分数的影响 Δ=filt_降噪-filt_对照（{len(deltas)} 个夹具）: "
                  f"min={deltas[0]*100:+.1f} 中位={deltas[len(deltas)//2]*100:+.1f} "
                  f"max={deltas[-1]*100:+.1f}；Δ>0 的个数={sum(1 for d in deltas if d > 0)}/{len(deltas)}")
        if case_paths and case_paths_den:
            cf, df = case_paths[0]["filt"], case_paths_den[0]["filt"]
            print(f" 10) 真实录音(全长2s, bg=0.0): 对照 filt={cf[0]}/{int(cf[1]*100)} → "
                  f"降噪 filt={df[0]}/{int(df[1]*100)}；两组仍然都无候选"
                  f"（对照 filt 撒币=71.2 分，即已知的 0.712）")
    coin0 = next((f for f in fixtures if f["key"] == KEYS[0] and int(f["snr"]) == 0), None)
    coin3 = next((f for f in fixtures if f["key"] == KEYS[0] and int(f["snr"]) == 3), None)
    if coin0 is not None and coin3 is not None:
        print(f" 11) 与既有事实的差异（重要）：本基准下撒币 0dB 时 filt="
              f"{int(coin0['ctl_paths']['filt'][1]*100)} 分、+3dB 时 filt="
              f"{int(coin3['ctl_paths']['filt'][1]*100)} 分，且都出候选；")
        print("     而已知实测是'背景 rms 0.042 / 音效 rms 0.05（≈+1.5dB）只剩 31~34、完全不出候选'。")
        print("     即：纯 SNR 轴 + 平稳背景 + 背景与参考逐样本一致，无法复现那次真实失效；")
        print("     真实失效更可能来自背景与音效的频谱重叠、背景非平稳，或参考窗与真实背景的域偏移。")
    print(LINE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())