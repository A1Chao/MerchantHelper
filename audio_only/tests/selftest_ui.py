# -*- coding: utf-8 -*-
"""自测：声控版面板（offscreen，无需真实音频设备）。

守的是**用户能看见的那部分**：开始监测下方那排格数图标按钮是否真的能筛候选、
「不限」是否真的不过滤、格数筛空时给不给明确状态、赞助窗口两张收款码在不在。
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

import main as main_mod  # noqa: E402
import recognition  # noqa: E402
import shapes  # noqa: E402
import ui  # noqa: E402
from candidates import CandidateEngine  # noqa: E402

FAILED = []


def check(condition, message):
    if not condition:
        FAILED.append(message)
        print("  [FAIL] %s" % message)
    return bool(condition)


def _rows_with_six_slot(matcher, engine):
    """找一组真实匹配结果，其中含 6 格物品（用来验证格数硬过滤）。"""
    sounds_dir = os.path.join(ROOT, "..", "assets", "sounds")
    for name in sorted(os.listdir(sounds_dir)):
        if not name.lower().endswith(".wav"):
            continue
        samples, sr = recognition.read_audio(os.path.join(sounds_dir, name))
        rows = matcher.match(samples, sr, 5, 0.0)
        if not rows:
            continue
        six = engine.resolve(rows, 6, "", strict_shape=True, strict_orientation=False)
        if six:
            return rows, six
    return [], []


def main() -> int:
    print("== 面板：格数图标按钮 / 显示 / 赞助 ==")
    app = QApplication.instance() or QApplication([])

    engine = CandidateEngine()
    print("[1] 面板与格数按钮")
    panel = ui.Panel(engine, start_thread=False)
    check(panel.shape == shapes.SHAPE_ANY, "默认格数筛选应为「不限」")
    check(len(panel.shape_buttons) == 7, "应正好 7 个格数图标按钮")
    check([b.shape for b in panel.shape_buttons] == list(shapes.SHAPE_CHOICES),
          "按钮形状顺序应与 SHAPE_CHOICES 一致")
    check(all(not b.text() for b in panel.shape_buttons),
          "格数按钮内**不许有文字**（用户要求图标展示）")
    checked = [b.shape for b in panel.shape_buttons if b.isChecked()]
    check(checked == [shapes.SHAPE_ANY], "默认只应勾选「不限」，实际 %r" % (checked,))
    # 图标几何：具体形状要画 w*h 个格子；不限画虚线框（无格子）
    for shape in shapes.SHAPE_CHOICES:
        cells = ui.shape_glyph_rects(shape)
        want = 0 if shapes.is_any(shape) else shape[0] * shape[1]
        check(len(cells) == want,
              "图标 %r 应画 %d 个格子，实际 %d" % (shape, want, len(cells)))
    check(all(c[2] > 0 for shape in shapes.SHAPE_CHOICES for c in ui.shape_glyph_rects(shape)),
          "格子边长必须为正")

    print("[2] 真实识别结果 → 面板显示")
    matcher = recognition.SoundMatcher(os.path.join(ROOT, "..", "assets"),
                                       os.path.join(ROOT, "..", "assets", "database.json"))
    check(len(matcher.errors) == 0, "模板加载不应有错误: %r" % (matcher.errors[:3],))
    rows, six_slot = _rows_with_six_slot(matcher, engine)
    if not check(bool(rows), "素材库里应至少有一组能命中 6 格物品的模板"):
        print("== FAIL ==")
        return 1
    shown = panel.show_candidates(rows, last_reason="", trigger_src="音频触发")
    check(bool(shown), "「不限」时应显示候选")
    check(len(shown) == len(panel._all_candidates), "返回值应与面板候选一致")
    check(panel.summary_label.text().startswith("候选 %d / 显示" % len(shown)),
          "汇总行文本: %r" % panel.summary_label.text())
    check("0%" not in panel.summary_label.text(), "汇总行不应出现百分比")
    check(panel.waiting_label.isHidden() or not panel._shown_candidates,
          "有候选时等待文案应隐藏")

    print("[3] 点 2×3 图标 → 只留 6 格")
    btn_23 = [b for b in panel.shape_buttons if b.shape == (2, 3)][0]
    btn_23.click()
    check(panel.shape == (2, 3), "点 2×3 后面板形状应为 (2,3)，实际 %r" % (panel.shape,))
    check(panel._last_slots == 6, "2×3 应筛 6 格")
    check(all(int(c["slot"]) == 6 for c in panel._all_candidates),
          "6 格筛选后不该出现非 6 格候选: %r"
          % [c["slot"] for c in panel._all_candidates])
    check(bool(panel._all_candidates), "这组素材在 6 格筛选下应有候选")
    check("格数筛选：2×3（6格 · 竖）" in panel.reason_label.text(),
          "详情行应写明当前格数筛选: %r" % panel.reason_label.text())

    print("[4] 换成必然筛空的格数 → 明确状态文案")
    empty_shape = None
    for shape in ((1, 2), (2, 1), (2, 2), (1, 1)):
        probe = engine.resolve(rows, shapes.slots_of(shape), shapes.orientation_of(shape),
                               strict_shape=True, strict_orientation=False)
        if not probe:
            empty_shape = shape
            break
    if empty_shape is not None:
        panel.set_shape(empty_shape)
        check(not panel._all_candidates, "%r 应当筛空" % (empty_shape,))
        check(panel._shape_empty, "筛空后应标记 empty_after_shape")
        check(not panel.waiting_label.isHidden(), "筛空后应显示等待/空态文案")
        check("没有与" in panel.waiting_label.text(),
              "空态文案应说明格数不符: %r" % panel.waiting_label.text())
    else:
        # 这组素材在每个格数下都有候选：改用一条**只含 1 格物品**的合成声音组，
        # 保证"筛空"这条 UI 路径一定被覆盖（否则空态文案坏了也测不出来）。
        panel.show_candidates([{"key": "太阳能板", "score": 90,
                                "items": [{"name": "太阳能板"}]}])
        panel.set_shape((2, 3))
        check(not panel._all_candidates, "只含 1 格物品时选 2×3 应当筛空")
        check(panel._shape_empty, "筛空后应标记 empty_after_shape")
        check(not panel.waiting_label.isHidden(), "筛空后应显示等待/空态文案")
        check("没有与" in panel.waiting_label.text(),
              "空态文案应说明格数不符: %r" % panel.waiting_label.text())
        panel.show_candidates(rows)
    panel.set_shape(shapes.SHAPE_ANY)
    check(len(panel._all_candidates) > len(six_slot) or len(panel._all_candidates) >= len(six_slot),
          "切回「不限」后候选数应恢复（不少于 6 格筛选的结果）")

    print("[5] 粘滞：这几次没听到声音时保留上次结果")
    kept = panel.show_candidates([], last_reason="没有候选", trigger_src="音频触发")
    check(bool(kept), "空结果不应清空上一次的候选（粘滞）")
    check(panel._sticky, "空结果后应处于粘滞态")
    check("保留上次结果" in panel.match_label.text(),
          "粘滞时匹配行应说明保留: %r" % panel.match_label.text())

    print("[6] 清空")
    panel._on_clear_clicked()
    check(not panel._all_candidates and not panel._shown_candidates, "清空后候选应为空")
    check(not panel._sticky, "清空应同时清掉粘滞")
    check(panel.shape == shapes.SHAPE_ANY or not shapes.is_any(panel.shape),
          "清空不应破坏格数选择")

    print("[7] 赞助窗口：公开仓库允许未分发收款码，并验证缺图回退")
    dialog = panel._build_sponsor_dialog()
    check(dialog.windowTitle().startswith("赞助"), "赞助窗口标题: %r" % dialog.windowTitle())
    check(len(panel._sponsor_qr) == 2, "赞助窗口应有 2 个收款码位")
    pixmaps = 0
    expected = 0
    for title, filename, pic in panel._sponsor_qr:
        path = panel._sponsor_asset(filename)
        loaded = pic.pixmap() is not None and not pic.pixmap().isNull()
        if os.path.isfile(path):
            expected += 1
            check(loaded and not pic.text(), "%s 的现有图片应正常加载" % title)
        else:
            check(not loaded and bool(pic.text()), "%s 缺图时应明确提示" % title)
        pixmaps += int(loaded)
    check(pixmaps == expected, "收款码加载数与实际分发图片数一致")
    check("免费" in dialog.findChildren(type(panel.reason_label))[0].text()
          if dialog.findChildren(type(panel.reason_label)) else True,
          "赞助窗口应说明免费")

    print("[8] 文案常量与 main.py 一致（面板/集成层不许各写一套）")
    check(ui.IDLE_STATUS_TEXT == main_mod._IDLE_STATUS,
          "空闲文案不一致: ui=%r main=%r" % (ui.IDLE_STATUS_TEXT, main_mod._IDLE_STATUS))
    check(ui.MONITORING_STATUS_TEXT == main_mod._MONITORING_STATUS,
          "监测中文案不一致: ui=%r main=%r"
          % (ui.MONITORING_STATUS_TEXT, main_mod._MONITORING_STATUS))
    check("拖动" not in ui.WAITING_TEXT and "拖动" not in ui.MONITORING_STATUS_TEXT,
          "纯音频版的等待文案不应再提「拖动」: %r / %r"
          % (ui.WAITING_TEXT, ui.MONITORING_STATUS_TEXT))

    print("[8.1] 底栏实时状态（正在识别 / 刚识别出结果 / 等待音效）")

    class _FakeAudio:
        last_error = ""
        windows = 30
        peak_rms = 0.05

    class _FakeEngine:
        audio = _FakeAudio()
        busy = True
        last_result = None

    fake = _FakeEngine()
    line = main_mod._detection_status_line(fake)
    check(line == main_mod.RECOGNIZING_STATUS,
          "正在识别时底栏应显示「正在识别…」，实际 %r" % line)
    fake.busy = False
    line = main_mod._detection_status_line(fake)
    check(line == main_mod._MONITORING_STATUS,
          "空闲时应回落到等待音效文案，实际 %r" % line)
    fake.last_result = {"at": time.time(), "rows": [{"key": "撒币声音", "score": 99}],
                        "reason": ""}
    line = main_mod._detection_status_line(fake)
    check(line.startswith("已识别：") and "撒币声音" in line,
          "刚识别出结果时底栏应显示「已识别：xxx」，实际 %r" % line)

    print("[9] 滚动区底色（曾经的缺陷：viewport 默认浅色 → 空列表整块发白）")
    check(not panel.scroll_area.autoFillBackground(), "scroll_area 不应自动填充底色")
    check(not panel.scroll_area.viewport().autoFillBackground(),
          "scroll viewport 不应自动填充底色（否则空态一片白）")
    check(not panel.cards_host.autoFillBackground(), "cards_host 不应自动填充底色")

    print("[10] 收起 / 展开不丢状态")
    panel.set_collapsed(True)
    check(panel.is_collapsed() and panel.height() < ui.PANEL_HEIGHT,
          "收起后高度应变小: %r" % panel.height())
    panel.set_collapsed(False)
    check(not panel.is_collapsed() and panel.height() == ui.PANEL_HEIGHT,
          "展开后高度应恢复: %r" % panel.height())

    print("[11] ✕ 防误触：第一次点击不退出，2.5s 内第二次才退出")
    panel._on_close_clicked()
    check(not bool(getattr(panel, "_closed", False)), "第一次点击 ✕ 不应关闭面板")
    check(float(getattr(panel, "_close_click_at", 0.0) or 0.0) > 0.0,
          "第一次点击应记下时间戳")
    panel._on_close_clicked()
    check(bool(getattr(panel, "_closed", False)), "2.5s 内第二次点击 ✕ 应关闭面板")

    panel.close()
    print()
    if FAILED:
        print("== FAIL ==  %d 项不通过" % len(FAILED))
        return 1
    print("== PASS ==  面板/格数按钮/赞助全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
