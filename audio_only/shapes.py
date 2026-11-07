# -*- coding: utf-8 -*-
"""行商助手 · 声控版 · 格数形状（UI 与集成层共用的唯一权威定义）

声控版**不做视觉**，所以"这件物品占几格"不再由截图推断，而是由玩家在面板上
**手动选择**。本模块把那一排图标按钮背后的形状语义集中在这里，避免 UI、集成层、
自测三处各写一套而漂移。

形状用 ``(宽, 高)`` 表示，单位是**格**：

    ``(0, 0)``  不限格数（= 不按格数过滤）
    ``(1, 1)``  1 格      正
    ``(1, 2)``  2 格      竖（1 列 × 2 行）
    ``(2, 1)``  2 格      横（2 列 × 1 行）
    ``(2, 2)``  4 格      正
    ``(2, 3)``  6 格      竖（2 列 × 3 行）
    ``(3, 2)``  6 格      横（3 列 × 2 行）

为什么是这几个：``assets/database.json`` 里物品的 ``slot``（占格数）只有
1/2/4/6 四档，而 2 格与 6 格各自有两种摆法（横/竖）。给图标而不是文字，是因为
玩家在游戏里要**一眼扫过**就选对，字反而更慢（用户明确要求"不用文字展示"）。

朝向只做**软优先**（排前 + 灰字标注），**绝不硬丢**：2×3 与 3×2 面积相同、互为
镜像，一次判断偏了就会把正确物品整条丢掉 —— 这是原版实测踩过的坑
（见 ``candidates.CandidateEngine.resolve`` 的 ``strict_orientation``）。
"""

from __future__ import annotations

__all__ = [
    "SHAPE_ANY",
    "SHAPE_CHOICES",
    "slots_of",
    "orientation_of",
    "is_any",
    "normalize_shape",
    "describe_shape",
    "grid_text",
]


SHAPE_ANY = (0, 0)


SHAPE_CHOICES = (
    SHAPE_ANY,
    (1, 1),
    (1, 2),
    (2, 1),
    (2, 2),
    (2, 3),
    (3, 2),
)


_ORIENT_NORMAL = "正"
_ORIENT_FLAT = "横"
_ORIENT_TALL = "竖"


def is_any(shape) -> bool:
    """是否「不限格数」。"""
    return normalize_shape(shape) == SHAPE_ANY


def normalize_shape(shape):
    """把任意入参归一化成 ``(宽, 高)`` 的整数二元组；认不出的一律当「不限」。

    UI 传进来的是自己的元组、自测传的是 ``SHAPE_CHOICES`` 的元素、命令行可能传
    ``"2*3"`` 这种字符串 —— 全部在这里统一，调用方不必各自 try/except。
    """
    if shape is None:
        return SHAPE_ANY
    if isinstance(shape, str):
        text = shape.strip().lower().replace("x", "*").replace("×", "*")
        if not text or text in ("any", "all", "none", "不限", "*"):
            return SHAPE_ANY
        parts = text.split("*")
        if len(parts) == 1:

            try:
                value = int(parts[0])
            except (TypeError, ValueError):
                return SHAPE_ANY
            return (value, value) if value > 0 else SHAPE_ANY
        if len(parts) != 2:
            return SHAPE_ANY
        try:
            return (int(parts[0]), int(parts[1]))
        except (TypeError, ValueError):
            return SHAPE_ANY
    if isinstance(shape, (tuple, list)):
        try:
            if len(shape) == 0:
                return SHAPE_ANY
            if len(shape) == 1:
                value = int(shape[0])
                return (value, value) if value > 0 else SHAPE_ANY
            width, height = int(shape[0]), int(shape[1])
        except (TypeError, ValueError):
            return SHAPE_ANY
        if width <= 0 or height <= 0:
            return SHAPE_ANY
        return (width, height)
    try:
        return (int(shape), int(shape))
    except (TypeError, ValueError):
        return SHAPE_ANY


def slots_of(shape) -> int:
    """该形状占几格；「不限」返回 ``0``（= ``candidates.resolve`` 的"不过滤"）。

    注意返回值直接喂给 ``CandidateEngine.resolve(slots=...)``，那边 ``0`` 的语义
    就是**不按格数过滤**，与本模块一致。
    """
    width, height = normalize_shape(shape)
    if width <= 0 or height <= 0:
        return 0
    return int(width * height)


def orientation_of(shape) -> str:
    """该形状期望的朝向（数据库用语：横 / 竖 / 正）；「不限」返回空串。

    只用于**软优先**与卡片上的灰字标注，不参与硬丢弃。
    """
    width, height = normalize_shape(shape)
    if width <= 0 or height <= 0:
        return ""
    if width > height:
        return _ORIENT_FLAT
    if height > width:
        return _ORIENT_TALL
    return _ORIENT_NORMAL


def grid_text(shape) -> str:
    """形状的短文本 ``2×3``（日志/自测用；**面板上的图标按钮不显示文字**）。"""
    width, height = normalize_shape(shape)
    if width <= 0 or height <= 0:
        return "不限"
    return "%d×%d" % (width, height)


def describe_shape(shape) -> str:
    """形状的一句话说明（日志与提示用，例如 ``2×3（6格 · 竖）``）。"""
    width, height = normalize_shape(shape)
    if width <= 0 or height <= 0:
        return "不限格数"
    orient = orientation_of(shape)
    if orient == _ORIENT_NORMAL:
        return "%d×%d（%d格）" % (width, height, width * height)
    return "%d×%d（%d格 · %s）" % (width, height, width * height, orient)
