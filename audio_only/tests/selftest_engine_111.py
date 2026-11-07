# -*- coding: utf-8 -*-
"""自测：声控版 1.1.1 识别移植版（判定 = 1.1.1，实现 = 10-19 提速版）。

守三件事：
  1. recognition.py 用 1.1.1 的判定（四个阈值 + 波形兜底 0.12），10-19 提速版的
     实验开关默认全关（波形覆盖=True / 拆段=False / 低切=0 / 学习模板=False）；
  2. 引擎能建库（33 模板、0 错误）且 33/33 自识别 ≥90 分、静音被拒绝；
  3. CLI/Engine 里不再挂 prefix/hybrid/waveform 引擎，也没有 --engine 开关。

    D:\\PY\\python.exe tests\\selftest_engine_111.py
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, os.path.join(ROOT, "deps")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402

import recognition  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print("  [OK] " + msg, flush=True)
    else:
        print("  [FAIL] " + msg, flush=True)
        FAILS.append(msg)


def main() -> int:
    print("== selftest_engine_111 ==", flush=True)

    # ---- 1. 引擎身份：1.1.1 的判定 + 10-19 提速实现 -------------------- #
    print("[1] recognition.py = 1.1.1 判定 + 提速实现（实验开关默认全关）", flush=True)
    # 提速版（perf/audio-fast）保留了实验接口，但默认值是 1.1.1 原行为；
    # 逐用例对拍见 _tmp/bench_engine_parity.py（25 窗 0 差异 + 33 模板 0 差异）。
    check(getattr(recognition, "WAVEFORM_OVERRIDES_PASSED_SPECTRAL", None) is True,
          "波形兜底默认仍可覆盖已通过的谱匹配（与反汇编一致）")
    check(getattr(recognition, "SPLIT_TEMPLATE_SEGMENTS", None) is False, "默认不拆段")
    check(float(getattr(recognition, "LOWCUT_HZ", 0.0) or 0.0) == 0.0, "默认无低切")
    check(getattr(recognition, "LEARNED_TEMPLATES", None) is False, "默认不加载学习模板")
    check(recognition.MIN_SIMILARITY == 0.88, "谱阈值 0.88")
    check(recognition.FILTERED_MIN_SIMILARITY == 0.85, "高通阈值 0.85")
    check(recognition.ADAPTIVE_FILTERED_MIN_SIMILARITY == 0.82, "自适应阈值 0.82")
    check(recognition.GENTLE_FILTERED_MIN_SIMILARITY == 0.78, "温和阈值 0.78")
    check(recognition.MIXED_WAVEFORM_MIN_CORRELATION == 0.12, "波形兜底 0.12")

    # ---- 2. 建库 -------------------------------------------------------- #
    print("[2] 建模板库", flush=True)
    t0 = time.time()
    matcher = recognition.SoundMatcher(
        os.path.join(ROOT, "..", "assets"),
        os.path.join(ROOT, "..", "assets", "database.json"),
    )
    load_s = time.time() - t0
    check(len(matcher.templates) == 33, "模板数 = 33（实际 %d）" % len(matcher.templates))
    check(not matcher.errors, "模板错误为空（实际 %r）" % (matcher.errors[:3],))
    print("  加载 %.2fs" % load_s, flush=True)

    # ---- 3. 33/33 自识别 ------------------------------------------------ #
    print("[3] 33 条素材自识别", flush=True)
    ok = 0
    worst = 100
    wrong: list[str] = []
    t1 = time.time()
    for t in matcher.templates:
        rows = matcher.match(t.samples, recognition.SAMPLE_RATE, 3, 0.0)
        score = rows[0]["score"] if rows else 0
        key = rows[0]["key"] if rows else "-"
        if rows and key == t.key and score >= 90:
            ok += 1
            worst = min(worst, score)
        else:
            wrong.append("%s→%s/%d（期望 ≥90）" % (t.key, key, score))
    match_s = time.time() - t1
    check(ok == 33, "自识别 %d/33（最低 %d 分；单次约 %.0f ms）"
          % (ok, worst, match_s / max(1, len(matcher.templates)) * 1000))
    for line in wrong[:5]:
        print("  错的：" + line, flush=True)

    # ---- 4. 静音拒绝 ---------------------------------------------------- #
    print("[4] 静音窗口必须被拒绝", flush=True)
    rows = matcher.match(np.zeros(16000, dtype=np.float32), recognition.SAMPLE_RATE, 3, 0.0)
    check(rows == [], "静音 → 0 候选")
    check("未检测到" in matcher.last_reason, "原因串含『未检测到』（实际 %r）"
          % matcher.last_reason)

    # ---- 5. CLI：没有 --engine / --learned-templates -------------------- #
    print("[5] CLI 面", flush=True)
    import main as main_mod  # noqa: E402
    args = main_mod.parse_args([])
    check(not hasattr(args, "engine"), "没有 --engine 参数")
    check(not hasattr(args, "learned_templates"), "没有 --learned-templates 参数")
    check(getattr(args, "min_score", None) == 70.0, "--min-score 默认 70")
    check(getattr(args, "listen_max", None) == 0.8, "--listen-max 默认 0.8")
    rejected = False
    try:
        main_mod.parse_args(["--engine", "prefix"])
    except SystemExit:
        rejected = True
    check(rejected, "--engine prefix 被 argparse 拒绝")

    # ---- 6. Engine 加载 ------------------------------------------------ #
    print("[6] Engine.load() 只建谱引擎", flush=True)
    engine = main_mod.Engine(args)
    engine.load()
    check(len(engine.matcher.templates) == 33, "Engine.matcher 模板 = 33")
    check(engine.candidate_engine is not None, "候选引擎已加载")
    check(not hasattr(engine, "prefix") or engine.prefix is None,
          "没有 prefix 引擎实例")

    print("== %s ==  %d 失败" % ("PASS" if not FAILS else "FAIL", len(FAILS)), flush=True)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
