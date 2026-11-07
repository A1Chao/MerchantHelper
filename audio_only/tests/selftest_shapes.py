# -*- coding: utf-8 -*-
"""自测：格数形状语义（``shapes.py``）—— 纯函数，不需要 GUI/音频。

断言的是**产品语义**而不是实现细节：面板上那排图标按钮点了以后，到底按几格筛、
朝向怎么算、「不限」是不是真的不过滤。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import shapes  # noqa: E402

FAILED = []


def check(condition, message):
    if not condition:
        FAILED.append(message)
        print("  [FAIL] %s" % message)
    return bool(condition)


def main() -> int:
    print("== shapes：格数形状语义 ==")

    # 1) 按钮顺序就是用户要求的顺序：不限 / 1*1 / 1*2 / 2*1 / 2*2 / 2*3 / 3*2
    print("[1] 图标按钮的 7 个形状与顺序")
    check(shapes.SHAPE_CHOICES == ((0, 0), (1, 1), (1, 2), (2, 1), (2, 2), (2, 3), (3, 2)),
          "SHAPE_CHOICES 顺序错: %r" % (shapes.SHAPE_CHOICES,))
    check(len(shapes.SHAPE_CHOICES) == 7, "应当正好 7 个选项")

    # 2) 格数 = 宽 × 高；「不限」= 0（= candidates.resolve 的"不过滤"）
    print("[2] 格数映射")
    expected_slots = {(0, 0): 0, (1, 1): 1, (1, 2): 2, (2, 1): 2,
                      (2, 2): 4, (2, 3): 6, (3, 2): 6}
    for shape, want in expected_slots.items():
        got = shapes.slots_of(shape)
        check(got == want, "slots_of(%r) = %r，期望 %r" % (shape, got, want))

    # 3) 朝向：宽>高=横，高>宽=竖，相等=正；「不限」为空串
    print("[3] 朝向映射")
    expected_orient = {(0, 0): "", (1, 1): "正", (1, 2): "竖", (2, 1): "横",
                       (2, 2): "正", (2, 3): "竖", (3, 2): "横"}
    for shape, want in expected_orient.items():
        got = shapes.orientation_of(shape)
        check(got == want, "orientation_of(%r) = %r，期望 %r" % (shape, got, want))

    # 4) 归一化：UI 元组 / 命令行字符串 / 坏输入都要能安全落地
    print("[4] 入参归一化")
    cases = [
        (None, (0, 0)),
        ("", (0, 0)),
        ("不限", (0, 0)),
        ("2*3", (2, 3)),
        ("3x2", (3, 2)),
        ("2×3", (2, 3)),
        ("garbage", (0, 0)),
        ((2, 3), (2, 3)),
        ([1, 2], (1, 2)),
        ((0, 0), (0, 0)),
        ((-1, 2), (0, 0)),
        (4, (4, 4)),
        ("6", (6, 6)),
    ]
    for value, want in cases:
        got = shapes.normalize_shape(value)
        check(got == want, "normalize_shape(%r) = %r，期望 %r" % (value, got, want))
    check(shapes.normalize_shape((2, 3)) == shapes.normalize_shape("2*3"),
          "元组与字符串两种写法必须归一化成同一个形状")

    # 5) is_any 只对「不限」为真
    print("[5] is_any")
    check(shapes.is_any((0, 0)) and shapes.is_any(None) and shapes.is_any("不限"),
          "不限格数的三种写法都应为 True")
    check(not shapes.is_any((1, 1)) and not shapes.is_any((2, 3)),
          "具体形状不应被判成不限")

    # 6) 文案（面板详情行/日志用；图标按钮里**不显示**这些字）
    print("[6] 文案")
    check(shapes.describe_shape((0, 0)) == "不限格数", "不限格数文案")
    check(shapes.describe_shape((2, 3)) == "2×3（6格 · 竖）",
          "describe_shape((2,3)) = %r" % shapes.describe_shape((2, 3)))
    check(shapes.describe_shape((3, 2)) == "3×2（6格 · 横）",
          "describe_shape((3,2)) = %r" % shapes.describe_shape((3, 2)))
    check(shapes.describe_shape((1, 1)) == "1×1（1格）",
          "正方形不标朝向: %r" % shapes.describe_shape((1, 1)))
    check(shapes.grid_text((2, 3)) == "2×3" and shapes.grid_text((0, 0)) == "不限",
          "grid_text")

    print()
    if FAILED:
        print("== FAIL ==  %d 项不通过" % len(FAILED))
        return 1
    print("== PASS ==  形状语义全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
