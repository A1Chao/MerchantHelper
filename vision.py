# -*- coding: utf-8 -*-
r"""视觉识别核心的**占位文件** —— 开源仓不含真实实现。

本仓库是「行商助手」的开源部分：**音频识别 + 候选筛选 + 面板界面 + 集成层**。
占格识别（`vision.py` 的真实实现）由作者保留，**不在本仓**。

为什么保留
  · 它是本工具唯一的技术壁垒，且包含大量在真实游戏画面下反复实测得到的几何与阈值决策；
  · 保留它不影响你阅读 / 修改 / 贡献本仓的其他任何部分。

本文件的作用：让 `python main.py` 在纯本仓环境下**原样跑起来**
（音频识别、候选筛选、面板全部可用），行为等价于命令行开关

    python main.py --no-vision          # 跳过占格识别，只做音频识别

官方发行版（Releases 中的 exe）内置真实视觉实现，占格识别可用。

接口契约（若你想自行实现视觉，真实实现应提供这些名字与语义）
    GRID_PITCH_PX                                  # 网格间距默认值，可为 None
    capture_cursor_region(pad=140, center=None)     -> (HxWx3 uint8 BGR, (origin_x, origin_y))
    capture_full_screen()                           -> (HxWx3 uint8 BGR, (origin_x, origin_y))
    detect_press_item(image, cursor_xy, ...)        -> DragInfo | None
    detect_drag_slots(image, cursor_xy)             -> DragInfo | None

`DragInfo` 需提供 main.py 读取的字段：
    slots(int)  orientation(str)  source(str)  confidence(float)
    reason(str) rect(tuple|None)  highlight(tuple|None)  pitch(float)  pitch_source(str)

`source` 的取值约定：命中拖动高亮 → "红块"；命中物品自身边框 → "边框"；未判出 → ""。
"""
from __future__ import annotations

import ctypes
from typing import Any

import numpy as np

#: 网格间距的默认值（公开常识值，日志里本来就会报告"格宽=85.0"这类信息）。
#: 真实实现会在运行时**实测**该值并按日志报告；这里给一个数值是为了让 main.py 的
#: 几何兜底路径不会拿到 None。
GRID_PITCH_PX: float | None = 85.0


def _black(h: int, w: int) -> np.ndarray:
    return np.zeros((int(h), int(w), 3), dtype=np.uint8)


def _virtual_screen() -> tuple[int, int, int, int]:
    """(left, top, width, height)；取不到就退回主屏尺寸。"""
    try:
        u = ctypes.windll.user32
        return (int(u.GetSystemMetrics(76)), int(u.GetSystemMetrics(77)),
                int(u.GetSystemMetrics(78)), int(u.GetSystemMetrics(79)))
    except Exception:
        return 0, 0, 1920, 1080


def _cursor() -> tuple[int, int]:
    try:
        class _Pt(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        p = _Pt()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
        return int(p.x), int(p.y)
    except Exception:
        return 0, 0


def capture_cursor_region(pad: int = 140, center: tuple[int, int] | None = None):
    """**占位**：返回全黑图 + 正确的 origin（真实实现返回真实屏幕内容）。"""
    pad = int(pad)
    cx, cy = center if center is not None else _cursor()
    return _black(2 * pad, 2 * pad), (int(cx) - pad, int(cy) - pad)


def capture_full_screen():
    """**占位**：返回全黑图 + 虚拟屏原点。"""
    left, top, w, h = _virtual_screen()
    return _black(h, w), (left, top)


def detect_press_item(*_a: Any, **_k: Any):
    """**占位**：永远判不出物品（等价 --no-vision）。"""
    return None


def detect_drag_slots(*_a: Any, **_k: Any):
    """**占位**：同上。"""
    return None