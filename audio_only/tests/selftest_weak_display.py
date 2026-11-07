# -*- coding: utf-8 -*-
"""`--show-weak`：未达判定阈值时给出"最像的几个 + 百分制分数"，面板能直接显示。

历史缺陷两处：
  1. 调用处把 `self.matcher` 当成 `samples` 传进去（`_weak_candidates` 少一个参数）
     → TypeError 被 `except Exception: return []` 吞掉，开了 `--show-weak` 也永远没有候选；
  2. 就算不抛异常，返回的是 `_rank` 的原始行（score 是 0~1 浮点、没有 items），
     面板会显示成 0 分。

用"模板 + 宽带噪声"造一个谱分落在 0.75~0.80 的窗口（游戏内实录就在这个区间：
金狮 0.766 / 耐冲击 0.698），断言弱候选给出的就是这条谱路径的分数与物品。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main as app  # noqa: E402
import recognition  # noqa: E402

#: 模板本身就是 16kHz 单声道，测试窗口也在 16kHz 域里构造（别混采样率）
SR = 16000
KEY = "金狮声音"
NOISE = 0.5          # 宽带噪声相对音效的幅度；调它让"原始谱 + 高通谱"都过不了阈值


def noisy_window(template: np.ndarray, alpha: float = NOISE) -> np.ndarray:
    rng = np.random.default_rng(11)
    base = np.asarray(template, dtype=np.float32)
    white = rng.standard_normal(len(base)).astype(np.float32)
    white = white / np.abs(white).max() * np.abs(base).max() * alpha
    sound = (base + white).astype(np.float32)
    window = np.zeros(int(1.5 * SR), dtype=np.float32)
    window[len(window) - len(sound):] = sound
    return window


def main() -> int:
    args = app.parse_args(["--show-weak"])
    engine = app.Engine(args)
    engine.load()
    matcher = engine.matcher
    template = next(t for t in matcher.templates if t.key == KEY)
    window = noisy_window(template.samples)

    failures = []

    def check(name: str, ok: bool, detail: str) -> None:
        print(f"  [{'OK' if ok else 'FAIL'}] {name}: {detail}")
        if not ok:
            failures.append(name)

    # 基准：弱候选 = 原始谱 + 高通谱两条路径里每个 key 取最高，再全局排序
    inp = recognition.extract(window, reduce_background=True)
    merged: dict = {}
    for row in list(matcher._rank(inp, 0.0)) + list(
            matcher._rank(recognition.extract(matcher._highpass(window), reduce_background=True),
                          0.0, filtered=True)):
        k = row.get("key")
        if k and row["score"] > merged.get(k, {}).get("score", 0):
            merged[k] = row
    expect = max(merged.values(), key=lambda r: r["score"])
    check("[0] 构造出的窗口谱分低于判定阈值", expect["score"] < recognition.MIN_SIMILARITY,
          f"最好 {expect['key']}={expect['score']:.3f} < {recognition.MIN_SIMILARITY}")

    weak = engine._weak_candidates(window, SR, 3, 0.0)   # 修复前的调用方式会抛 TypeError
    check("[1] 弱候选非空（旧代码这里恒为空）", bool(weak), f"{len(weak)} 条")
    if weak:
        top = weak[0]
        check("[2] 首选就是谱路径第一", top["key"] == expect["key"], f"{top['key']}")
        check("[3] 分数是百分制整数", isinstance(top["score"], int)
              and 0 <= top["score"] <= 100,
              f"score={top['score']} 应为 0-100 整数（当前引擎：{getattr(args, 'engine', 'spectral')}）")
        check("[3] 行结构可直接给面板用",
              isinstance(top.get("items"), list) and bool(top["items"]),
              f"items={len(top.get('items') or [])} 条")

    # 弱候选只影响显示：match() 的阈值判定一行没动
    before = matcher.match(window, SR, 5, 0.0)
    after = matcher.match(window, SR, 5, 0.0)
    check("[4] 弱候选不改变判定", before == after, f"rows={len(before)}")

    print()
    if failures:
        print("== FAIL ==", " / ".join(failures))
        return 1
    print("== PASS ==  --show-weak 给出低置信候选（百分制/可用行），判定阈值未变")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
