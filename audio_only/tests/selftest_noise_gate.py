# -*- coding: utf-8 -*-
"""环境音门控：窗口里"根本没响过"时不许报物品。

用户日志（2026-10-20，15 次触发）里的规律：

    音效段 = 无       → 8 次，全部报了 72~93 分的错误物品（脚步/门/风）
    音效段 0.013~0.064 → 7 次，是真拖动，识别正常

判据沿用产品 `[trace]` 里已经在打印的那句话：
`音效段 RMS >= max(0.0012, 背景 × 3.0)`。

本测试用合成窗口锁住这个判据的边界：
  [1] 纯环境音（宽带噪声，RMS 与背景同级）→ 判为"没有清晰音效段"
  [2] 真音效（模板放进窗口，比背景大一个量级）→ 判为"有音效段"
  [3] 门槛随背景抬高：背景越大，同样的音效越容易被判为环境音（所以音量很重要）
  [5] `--min-score` 默认 70：低分结果不上屏（只影响显示，不改判定）
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

SR = 44100
WINDOW = int(1.5 * SR)


def ambient(rms: float, seed: int = 5) -> np.ndarray:
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(WINDOW)
    x = x / np.sqrt(np.mean(x ** 2)) * rms
    return x.astype(np.float32)


def with_sound(template: np.ndarray, bg_rms: float, sound_rms: float, seed: int = 6):
    """背景 + 一小段真实音效（放在窗口末尾，像"拿起后放下"）。"""
    x = ambient(bg_rms, seed)
    t = np.asarray(template, np.float64)
    t = t / max(1e-9, np.sqrt(np.mean(t ** 2))) * sound_rms
    x[len(x) - len(t):] += t.astype(np.float32)
    return x


def main() -> int:
    args = app.parse_args([])
    engine = app.Engine(args)
    engine.load()
    template = next(t for t in engine.matcher.templates if t.key == "打铁声音")
    failures = []

    def check(name: str, ok: bool, detail: str) -> None:
        print(f"  [{'OK' if ok else 'FAIL'}] {name}: {detail}")
        if not ok:
            failures.append(name)

    def gate(win, bg):
        engine._weak_snap = (win, bg)
        seg, bg2 = engine._recognized_segment()
        floor = max(app.MIN_TRIGGER_SEGMENT_RMS, bg2 * app.TRIGGER_SEGMENT_BG_RATIO)
        return seg, floor, seg >= floor

    # [1] 纯环境音
    seg, floor, has = gate(ambient(0.0050), 0.0036)
    check("[1] 环境音被判为没有音效段", not has, f"音效段={seg:.5f} < 门槛={floor:.5f}")

    # [2] 真音效（实测用户真拖动的音效段是 0.013~0.064）
    seg, floor, has = gate(with_sound(template.samples, 0.0005, 0.0150), 0.0005)
    check("[2] 真音效被判为有音效段", has, f"音效段={seg:.5f} >= 门槛={floor:.5f}")

    # [3] 背景变响（比如游戏里放音乐）→ 同样的音效就不够格了
    seg_quiet, floor_quiet, has_quiet = gate(with_sound(template.samples, 0.0005, 0.0120), 0.0005)
    seg_loud, floor_loud, has_loud = gate(with_sound(template.samples, 0.0060, 0.0120), 0.0060)
    check("[3] 背景越大门槛越高", floor_loud > floor_quiet,
          f"{floor_quiet:.5f} → {floor_loud:.5f}")
    check("[3] 同一音效在吵闹环境下会被判为环境音", has_quiet and not has_loud,
          f"安静={has_quiet}({seg_quiet:.5f}/{floor_quiet:.5f}) "
          f"吵闹={has_loud}({seg_loud:.5f}/{floor_loud:.5f})")

    # [4] 默认**不**拦：实测同一局 10 个窗口里 5 个被拦掉，而它们谱分 0.85~0.91（真物品）
    check("[4] 默认不启用（不再误伤真物品）", not args.require_sound_segment,
          f"require_sound_segment={args.require_sound_segment}")
    check("[4] 可选开启", getattr(app.parse_args(["--require-sound-segment"]),
                                 "require_sound_segment", False),
          "--require-sound-segment 打开后才拦")

    # [5] 显示下限默认 70：分数 50~67 的结果基本不可信，不上屏
    check("[5] 显示下限默认 70", abs(args.min_score - 70.0) < 1e-6, f"min_score={args.min_score}")
    check("[5] 可关掉", getattr(app.parse_args(["--min-score", "0"]), "min_score", None) == 0.0,
          "--min-score 0 显示全部")

    engine._weak_snap = None
    print()
    if failures:
        print("== FAIL ==", " / ".join(failures))
        return 1
    print("== PASS ==  环境音门控可选生效、默认不误伤；显示下限默认 70")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
