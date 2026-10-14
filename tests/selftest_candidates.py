# -*- coding: utf-8 -*-
"""行商助手 · 候选引擎 / 悬浮窗自测（ui-candidates 负责）

运行（PowerShell 直接重定向 Python stdout 会失败，必须用 cmd）：
    cmd /d /c "set QT_QPA_PLATFORM=offscreen && D:\\PY\\python.exe ^
        D:\\pycharm\\UA\\行商助手\\tests\\selftest_candidates.py > ^
        D:\\pycharm\\UA\\行商助手\\tests\\out_selftest_candidates.txt 2>&1"

覆盖：真实 DB 索引 / 假 match_rows 展开与排序 / slots 优先 / filter / 跨声音组去重 /
      QT_QPA_PLATFORM=offscreen 下的 Panel 构造与幂等刷新 / 边界（无输入模拟、无联网）扫描。
"""

import contextlib
import io
import json
import os
import re
import sys
import threading

# stdout 里的中文按 UTF-8 写出，方便事后 read 核对（cmd 重定向默认可能是 GBK）
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = r"D:\pycharm\UA\行商助手"
if BASE not in sys.path:
    sys.path.insert(0, BASE)

RAW_PLATFORM = os.environ.get("QT_QPA_PLATFORM")
# 注意：cmd 的 `set QT_QPA_PLATFORM=offscreen && ...` 会把「&&」前的空格并入变量值，
# 得到 "offscreen "，Qt 会因找不到名为 "offscreen " 的插件而 abort。这里先去除首尾空白。
if RAW_PLATFORM is not None and RAW_PLATFORM != RAW_PLATFORM.strip():
    os.environ["QT_QPA_PLATFORM"] = RAW_PLATFORM.strip()
    RAW_PLATFORM_NOTE = "已去除 cmd set 带入的首尾空白: %r -> %r" % (
        RAW_PLATFORM, RAW_PLATFORM.strip())
else:
    RAW_PLATFORM_NOTE = ""
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# offscreen 平台下 Qt 找不到自带字体目录会刷警告；指向系统字体目录可静音（找不到就跳过）。
_WIN_FONTS = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
if os.path.isdir(_WIN_FONTS):
    os.environ.setdefault("QT_QPA_FONTDIR", _WIN_FONTS)

import candidates as cand_mod  # noqa: E402
import ui as ui_mod  # noqa: E402

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok)))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" | " + detail) if detail else ""))
    return bool(ok)


def line(title=""):
    print("-" * 78)
    if title:
        print(title)


def _capture_panel_stdout(func):
    """抓取 func() 期间写进 stdout 的文本（[filter]/[sticky] 这类日志）。"""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        func()
    return buffer.getvalue()


def main():
    print("Python:", sys.version.replace("\n", " "))
    print("QT_QPA_PLATFORM (进程启动时 env):", RAW_PLATFORM)
    if RAW_PLATFORM_NOTE:
        print("QT_QPA_PLATFORM 规范化:", RAW_PLATFORM_NOTE)
    print("QT_QPA_PLATFORM (本次实际使用):", os.environ.get("QT_QPA_PLATFORM"))
    print("candidates.py:", os.path.join(BASE, "candidates.py"))
    print("ui.py:", os.path.join(BASE, "ui.py"))

    # =============================================================== 1. DB 索引
    line("1. CandidateEngine 读取真实 DB / name 索引")
    db_path = os.path.join(BASE, "assets", "database.json")
    engine = cand_mod.CandidateEngine(db_path)
    print("database_path      :", engine.database_path)
    print("items 数量          :", len(engine.items))
    print("special_items 数量  :", len(engine.special_items))
    print("索引 name 数量      :", len(engine.name_index))
    print("索引内行数合计      :", sum(len(v) for v in engine.name_index.values()))
    print("row_count / rows    :", engine.row_count, len(engine.rows))
    print("重名物品            :", engine.duplicate_names)
    print("DB 解析 errors      :", engine.errors)
    print("qualities           :", engine.qualities)
    print("categories(%d)      :" % len(engine.categories), engine.categories)
    print("orientations        :", engine.orientations)
    print("slots               :", engine.slots)
    print("images_dir 存在     :", os.path.isdir(engine.images_dir))
    check(
        "items=157 / special_items=10 / 索引 name=167",
        len(engine.items) == 157 and len(engine.special_items) == 10 and len(engine.name_index) == 167,
        "实测 %d/%d/%d" % (len(engine.items), len(engine.special_items), len(engine.name_index)),
    )
    probe = engine.lookup("阵列镜片")
    print("lookup('阵列镜片')  :", json.dumps(probe, ensure_ascii=False))
    print("lookup special 行   :", json.dumps(engine.lookup("古董茶壶"), ensure_ascii=False))
    check("items 与 special_items 都能按名查到", probe is not None and engine.lookup("古董茶壶") is not None)
    ref, raw = cand_mod.parse_reference_value(engine.lookup("天线")["reference_value"])
    print("天线 reference_value 解析:", ref, "| 原文:", raw)
    check("非纯数字 reference_value 不炸", ref == 90000, "解析=%r" % (ref,))

    # ============================================== 2. 假 match_rows -> resolve
    line("2. 假 match_rows（2 个声音组，items 为真实 DB 行）-> resolve")
    metal_names = ["阵列镜片", "卫星模型", "金块", "炮弹", "液压扳手", "里拉琴", "钢筋剪", "缓降器"]
    coin_names = ["《黎明》", "鎏金羽宫", "单兵通讯装置", "金质钢笔", "手持分析仪", "腕表"]
    metal_rows = [engine.lookup(n) for n in metal_names]
    coin_rows = [engine.lookup(n) for n in coin_names]
    fake_rows = [
        {
            "key": "金属声音",
            "kind": "normal",
            "score": 97,
            "offset": 12,
            "offset_seconds": 0.19,
            "items": metal_rows,
            "reason": "已匹配；相似度不代表出货概率。",
        },
        {
            "key": "硬币声音",
            "kind": "normal",
            "score": 92,
            "offset": 30,
            "offset_seconds": 0.48,
            "items": coin_rows,
            "reason": "已匹配；相似度不代表出货概率。",
        },
    ]
    print("match_rows 组数     :", len(fake_rows), "| 组1 items=%d 组2 items=%d" % (len(metal_rows), len(coin_rows)))
    resolved = engine.resolve(fake_rows)
    print("resolve 候选数      :", len(resolved))
    print("last_stats          :", json.dumps(engine.last_stats, ensure_ascii=False))
    print("排序后前 5 条：")
    for i, c in enumerate(resolved[:5], 1):
        print(
            "  #%d name=%-8s score=%-3d reference_value=%-8s image_path存在=%s slot=%d/%s sound_key=%s"
            % (
                i,
                c["name"],
                c["score"],
                c["reference_value"],
                os.path.exists(c["image_path"]),
                c["slot"],
                c["orientation"],
                c["sound_key"],
            )
        )
    top5 = resolved[:5]
    check("resolve 候选数 = 14（8+6 无重名）", len(resolved) == 14, "实测 %d" % len(resolved))
    check(
        "前 5 条按 score 降序",
        [c["score"] for c in top5] == sorted([c["score"] for c in top5], reverse=True),
        "scores=%s" % [c["score"] for c in top5],
    )
    contract_keys = {
        "name", "quality", "category", "slot", "orientation",
        "reference_value", "image_path", "score", "sound_key", "reason",
    }
    missing = [k for k in contract_keys if k not in resolved[0]]
    check("Candidate 契约字段齐全", not missing, "缺失=%s" % missing)
    check(
        "image_path 均指向 assets/images 下真实文件",
        all(os.path.exists(c["image_path"]) for c in resolved),
        "首条=%s" % resolved[0]["image_path"],
    )

    # ==================================================== 3. slots 过滤优先
    line("3. slots 优先：resolve(rows, slots=2) vs resolve(rows, slots=0)")
    all_zero = engine.resolve(fake_rows, slots=0)
    prio_two = engine.resolve(fake_rows, slots=2)
    print("slots=0 候选数=%d  slots=2 候选数=%d（不丢弃，数量必须一致）" % (len(all_zero), len(prio_two)))
    print("slots=0 前 8：", [(c["name"], c["slot"], c["score"]) for c in all_zero[:8]])
    print("slots=2 前 8：", [(c["name"], c["slot"], c["score"]) for c in prio_two[:8]])
    print("slots=2 last_stats:", json.dumps(engine.last_stats, ensure_ascii=False))
    n2 = sum(1 for c in all_zero if c["slot"] == 2)
    print("DB 中 slot==2 的候选数:", n2)
    check("数量不变（不丢弃）", len(all_zero) == len(prio_two), "%d vs %d" % (len(all_zero), len(prio_two)))
    check(
        "slots=2 时前 n2 条 slot 全为 2",
        all(c["slot"] == 2 for c in prio_two[:n2]) and n2 > 0,
        "前 %d 条 slots=%s" % (n2, [c["slot"] for c in prio_two[:n2]]),
    )
    check(
        "顺序确有差异（slots=0 首条未必 slot==2）",
        [c["name"] for c in all_zero[:8]] != [c["name"] for c in prio_two[:8]],
        "slots=0 首条 %s(slot=%d) -> slots=2 首条 %s(slot=%d)"
        % (all_zero[0]["name"], all_zero[0]["slot"], prio_two[0]["name"], prio_two[0]["slot"]),
    )
    check(
        "不匹配的候选带 note 且排后",
        all("note" in c and c["note"] for c in prio_two if c["slot"] != 2)
        and all(c["note"] == "" for c in prio_two if c["slot"] == 2),
        "示例 note=%r" % (prio_two[-1]["note"],),
    )

    # ========================================================== 4. filter
    line("4. filter：quality / category / orientation / min_score")
    f_all = engine.filter(resolved)
    f_gold = engine.filter(resolved, quality="金色")
    f_cat = engine.filter(resolved, category="收藏品")
    f_vert = engine.filter(resolved, orientation="竖")
    f_code = engine.filter(resolved, orientation="V")   # vision 代码应等价于 DB 中文「竖」
    f_score = engine.filter(resolved, min_score=95)
    print("不筛            :", len(f_all))
    print("quality=金色     :", len(f_gold), [c["name"] for c in f_gold])
    print("category=收藏品  :", len(f_cat), [c["name"] for c in f_cat])
    print("orientation=竖   :", len(f_vert), [c["name"] for c in f_vert])
    print("orientation=V    :", len(f_code), "(与「竖」等价)")
    print("min_score=95     :", len(f_score), [(c["name"], c["score"]) for c in f_score])
    print("quality=金色+category=收藏品:", len(engine.filter(resolved, quality="金色", category="收藏品")))
    check("quality 筛选生效", 0 < len(f_gold) < len(f_all), "%d -> %d" % (len(f_all), len(f_gold)))
    check("category 筛选生效", 0 < len(f_cat) < len(f_all), "%d -> %d" % (len(f_all), len(f_cat)))
    check("orientation 筛选生效", 0 < len(f_vert) < len(f_all), "%d -> %d" % (len(f_all), len(f_vert)))
    check("orientation 接受 H/V 代码", len(f_code) == len(f_vert), "%d vs %d" % (len(f_code), len(f_vert)))
    check("min_score 筛选生效", all(c["score"] >= 95 for c in f_score) and len(f_score) < len(f_all))

    # ================================================= 5. 跨声音组去重
    line("5. 去重：同一物品出现在两个声音组 -> 只留最高分")
    dup_rows = [
        {"key": "金属声音", "kind": "normal", "score": 80,
         "items": [engine.lookup("阵列镜片"), engine.lookup("金块")],
         "reason": "已匹配；相似度不代表出货概率。"},
        {"key": "阵列镜片", "kind": "item", "score": 99,
         "items": [engine.lookup("阵列镜片")],
         "reason": "已匹配；相似度不代表出货概率。"},
    ]
    dup_resolved = engine.resolve(dup_rows)
    names = [c["name"] for c in dup_resolved]
    print("原始展开组合数      :", engine.last_stats["raw_candidates"])
    print("去重掉的行数        :", engine.last_stats["deduped"])
    print("去重后候选          :", [(c["name"], c["score"], c["sound_key"]) for c in dup_resolved])
    jing = [c for c in dup_resolved if c["name"] == "阵列镜片"]
    check("同物品只出现一次", names.count("阵列镜片") == 1, "出现 %d 次" % names.count("阵列镜片"))
    check("保留的是最高分 99", len(jing) == 1 and jing[0]["score"] == 99 and jing[0]["sound_key"] == "阵列镜片")
    check("其他物品不受影响", "金块" in names and len(dup_resolved) == 2, "候选=%s" % names)

    # ============================================== 6. 历史（单次会话内存）
    line("6. 会话历史 history_add / history")
    engine.history_clear()
    engine.history_add(dup_resolved)
    engine.history_add(resolved)
    print("history 条数        :", len(engine.history))
    print("history[0] 键       :", sorted(engine.history[0].keys()))
    print("history_text 首行   :", engine.history_text().splitlines()[0])
    check("history 内存记录可用", len(engine.history) == 2 and engine.history[0]["count"] == len(dup_resolved))
    engine.history_clear()
    check("history_clear 生效", engine.history == [])

    # ==================================================== 7. UI（offscreen）
    line("7. QT_QPA_PLATFORM=offscreen 下构造 Panel")
    from PyQt5.QtCore import QCoreApplication, QTimer
    from PyQt5.QtWidgets import QApplication

    # 关键：这里**故意不预先创建 QApplication**，验证 Panel 在「app 不存在」时也能自建。
    print("构造 Panel 前 QApplication.instance():", QApplication.instance())
    threads_before = threading.active_count()
    panel = ui_mod.Panel(engine, start_thread=False)
    app = QApplication.instance()
    print("构造 Panel 后 QApplication.instance():", app)
    print("Panel 自建了 QApplication:", panel._owns_app)
    print("Panel 类型          :", type(panel).__name__)
    print("窗口 flags          :", int(panel.windowFlags()))
    print("WA_ShowWithoutActivating:", panel.testAttribute(ui_mod.Qt.WA_ShowWithoutActivating))
    print("WindowStaysOnTopHint:", bool(panel.windowFlags() & ui_mod.Qt.WindowStaysOnTopHint))
    print("FramelessWindowHint :", bool(panel.windowFlags() & ui_mod.Qt.FramelessWindowHint))
    print("Qt.Tool             :", bool(panel.windowFlags() & ui_mod.Qt.Tool))
    print("WindowDoesNotAcceptFocus:", bool(panel.windowFlags() & ui_mod.Qt.WindowDoesNotAcceptFocus))
    print("windowOpacity       :", round(panel.windowOpacity(), 3))
    print("size                :", panel.width(), "x", panel.height())
    geo = QApplication.primaryScreen().availableGeometry()
    print("screen availableGeometry:", geo.x(), geo.y(), geo.width(), geo.height())
    print("默认停靠位置 pos    :", panel.pos().x(), panel.pos().y(),
          "(期望 x = right - width - 24 = %d)" % (geo.right() - panel.width() - 24))
    check("无边框/置顶/Tool/不接受焦点 全部生效",
          bool(panel.windowFlags() & ui_mod.Qt.WindowStaysOnTopHint)
          and bool(panel.windowFlags() & ui_mod.Qt.FramelessWindowHint)
          and bool(panel.windowFlags() & ui_mod.Qt.Tool)
          and bool(panel.windowFlags() & ui_mod.Qt.WindowDoesNotAcceptFocus)
          and panel.testAttribute(ui_mod.Qt.WA_ShowWithoutActivating))
    check("半透明 ~0.92", abs(panel.windowOpacity() - 0.92) < 0.02, "实测 %.3f" % panel.windowOpacity())
    check("尺寸 = ui.PANEL_WIDTH x ui.PANEL_HEIGHT（中号面板）",
          (panel.width(), panel.height()) == (ui_mod.PANEL_WIDTH, ui_mod.PANEL_HEIGHT),
          "实测 %dx%d · 常量 %dx%d" % (panel.width(), panel.height(),
                                       ui_mod.PANEL_WIDTH, ui_mod.PANEL_HEIGHT))
    check("面板尺寸常量 = 用户选定的 440x820",
          (ui_mod.PANEL_WIDTH, ui_mod.PANEL_HEIGHT) == (440, 820),
          "实测 (%d, %d)" % (ui_mod.PANEL_WIDTH, ui_mod.PANEL_HEIGHT))
    check("默认停靠屏幕右侧 x=right-width-24", panel.pos().x() == geo.right() - panel.width() - 24,
          "pos.x=%d 期望=%d" % (panel.pos().x(), geo.right() - panel.width() - 24))
    check("start_thread=False 不新建线程", threading.active_count() == threads_before,
          "线程数 %d -> %d" % (threads_before, threading.active_count()))

    check("start_thread=False 不自建 QTimer", len(panel.findChildren(QTimer)) == 0,
          "QTimer 子对象=%d" % len(panel.findChildren(QTimer)))
    check("Panel 在无 QApplication 时可自建并可用", app is not None and panel._owns_app is True)

    ui_result = panel.show_candidates(
        fake_rows, slots=2, orientation="H", last_reason="已匹配；相似度不代表出货概率。"
    )
    print("show_candidates 返回候选数:", len(ui_result))
    print("窗口内卡片数        :", len(panel._card_widgets), "(上限 %d)" % ui_mod.MAX_CARDS)
    print("summary 文本        :", panel.summary_label.text())
    print("reason  文本        :", panel.reason_label.text().replace("\n", " / "))
    panel.set_status("测试")
    print("status  文本        :", panel.status_label.text())
    check("show_candidates 不抛异常且返回候选", len(ui_result) > 0)
    check("卡片数 = min(候选数, ui.MAX_CARDS)",
          len(panel._card_widgets) == min(len(ui_result), ui_mod.MAX_CARDS),
          "%d 张（候选 %d，上限 %d）" % (len(panel._card_widgets), len(ui_result),
                                        ui_mod.MAX_CARDS))

    # 幂等：同一主线程反复调用不泄漏 widget、不重复 connect
    card_ids = [id(w) for w in panel._card_widgets]
    for _ in range(20):
        panel.show_candidates(fake_rows, slots=2, orientation="H", last_reason="已匹配；相似度不代表出货概率。")
    ids_after = [id(w) for w in panel._card_widgets]
    print("重复 20 次 show_candidates 后卡片数:", len(ids_after), "| 与首次同一批对象:", ids_after == card_ids)
    check("重复调用幂等（不重建、不泄漏）", ids_after == card_ids and len(ids_after) == len(card_ids))
    check("重复调用不新增信号连接",
          panel.quality_combo.receivers(panel.quality_combo.currentIndexChanged) == 1,
          "receivers=%d" % panel.quality_combo.receivers(panel.quality_combo.currentIndexChanged))

    # 变化时确实重建，且旧卡片被回收
    panel.show_candidates(dup_rows, slots=2, orientation="H", last_reason="已匹配；相似度不代表出货概率。")
    print("候选集合变化后卡片数:", len(panel._card_widgets), "| 与旧批次不同对象:",
          [id(w) for w in panel._card_widgets] != card_ids)
    check("集合变化时重建卡片", [id(w) for w in panel._card_widgets] != card_ids)
    from PyQt5.QtCore import QCoreApplication

    QCoreApplication.processEvents()   # 触发 deleteLater 回收
    QCoreApplication.sendPostedEvents(None, 52)  # QEvent.DeferredDelete
    print("deleteLater 处理后卡片引用数:", len(panel._card_widgets))

    # 筛选下拉联动
    panel.show_candidates(fake_rows, slots=0, orientation="", last_reason="已匹配；相似度不代表出货概率。")
    print("品质下拉项          :", [panel.quality_combo.itemText(i) for i in range(panel.quality_combo.count())])
    print("类别下拉项          :", [panel.category_combo.itemText(i) for i in range(panel.category_combo.count())])
    print("朝向下拉项          :", [panel.orientation_combo.itemText(i) for i in range(panel.orientation_combo.count())])
    before = len(panel._shown_candidates)
    panel.quality_combo.setCurrentText("金色")
    after = len(panel._shown_candidates)
    print("下拉选「金色」后显示候选数: %d -> %d" % (before, after))
    check("UI 下拉筛选联动", 0 < after < before, "%d -> %d" % (before, after))

    # 空候选（面板上已有候选）→ 契约已改：粘滞保留，不再清空
    cards_before_empty = len(panel._card_widgets)
    panel.show_candidates([], slots=0, orientation="", last_reason="未检测到足够清晰的声音。")
    print("空候选后 waiting_label 可见:", not panel.waiting_label.isHidden(),
          "| status:", panel.status_label.text())
    print("空候选后卡片数:", len(panel._card_widgets), "| 粘滞:", panel._sticky,
          "| 详情行:", panel.reason_label.text(), "| 匹配行:", repr(panel.match_label.text()))
    check("已有候选时 rows=[] 保留卡片（粘滞），且面板不出现「未匹配」字样",
          cards_before_empty > 0 and len(panel._card_widgets) == cards_before_empty
          and panel._sticky is True and ("未匹配" not in panel.reason_label.text())
          and panel.waiting_label.isHidden(),
          "空回调前=%d 后=%d 粘滞=%s waiting隐藏=%s 详情行=%r"
          % (cards_before_empty, len(panel._card_widgets), panel._sticky,
             panel.waiting_label.isHidden(), panel.reason_label.text()))
    # 从未有过候选的面板 → 底部状态栏必须是用户原话逐字（不再追加别的提示）
    fresh_panel = ui_mod.Panel(None, start_thread=False)
    fresh_panel.show()
    QCoreApplication.processEvents()
    fresh_panel.show_candidates([], slots=0, orientation="", last_reason="未检测到足够清晰的声音。")
    print("全新面板（从未有候选）waiting 可见:", not fresh_panel.waiting_label.isHidden(),
          "| status:", fresh_panel.status_label.text(), "| cards:", len(fresh_panel._card_widgets))
    check("空闲态底部状态栏逐字等于用户原话（且列表区等待文案仍在）",
          fresh_panel.status_label.text() == ui_mod.IDLE_STATUS_TEXT
          and ui_mod.IDLE_STATUS_TEXT == "未开始监测：不读取游戏信息，点上方按钮或按 F8 开始。"
          and (not fresh_panel.waiting_label.isHidden())
          and fresh_panel.waiting_label.text() == ui_mod.WAITING_TEXT
          and len(fresh_panel._card_widgets) == 0 and fresh_panel._sticky is False,
          "status=%r waiting=%r cards=%d 粘滞=%s"
          % (fresh_panel.status_label.text(), fresh_panel.waiting_label.text(),
             len(fresh_panel._card_widgets), fresh_panel._sticky))
    check("署名：标题行与窗口标题都含 烟火",
          ui_mod.AUTHOR_TEXT in fresh_panel.title_label.text()
          and "烟火" in fresh_panel.windowTitle()
          and ui_mod.AUTHOR_TEXT in ui_mod.WINDOW_TITLE,
          "title=%r window=%r" % (fresh_panel.title_label.text(), fresh_panel.windowTitle()))
    check("监测中底栏文案 = 监测中：等待拖动物品…",
          fresh_panel.set_active_state(True) is None
          and fresh_panel.status_label.text() == ui_mod.MONITORING_STATUS_TEXT == "监测中：等待拖动物品…",
          "status=%r" % fresh_panel.status_label.text())
    fresh_panel.close()

    # set_status 可反复调用且稳定（底栏不再追加任何诊断）
    panel.set_status("测试")
    first = panel.status_label.text()
    panel.set_status("测试")
    second = panel.status_label.text()
    print("空候选下 set_status('测试') 两次:", repr(first), repr(second))
    panel.show_candidates(fake_rows, slots=0, orientation="", last_reason="已匹配；相似度不代表出货概率。")
    panel.set_status("测试")
    with_cards = panel.status_label.text()
    print("有候选时 set_status('测试') ->", repr(with_cards))
    check("set_status 可重复调用且文本稳定",
          first == second and first == "测试" and with_cards == "测试",
          "first=%r second=%r with_cards=%r" % (first, second, with_cards))
    panel.set_status("")

    # ---- lead 集成模式：外部 80ms QTimer 轮询 + panel.start() 进入事件循环 ----
    line("7b. main.py 集成模式：外部 80ms 轮询驱动 show_candidates/set_status")
    pump_state = {"n": 0, "errors": [], "cards": []}
    pump_sequence = (fake_rows, dup_rows, [], fake_rows)

    def pump():
        index = pump_state["n"]
        pump_state["n"] = index + 1
        try:
            rows = pump_sequence[index % len(pump_sequence)]
            panel.show_candidates(
                rows,
                slots=2 if index % 2 else 0,
                orientation="H" if index % 3 else "",
                last_reason="已匹配；相似度不代表出货概率。",
            )
            panel.set_status("轮询 #%d" % index)
            pump_state["cards"].append(len(panel._card_widgets))
        except Exception as exc:      # 绝不允许 80ms 轮询把主进程带崩
            pump_state["errors"].append(repr(exc))

    pump_timer = QTimer()
    pump_timer.timeout.connect(pump)
    pump_timer.start(80)
    rc = panel.start(max_ms=700)         # 事件循环里跑 ~700ms ≈ 8 次轮询
    pump_timer.stop()
    print("轮询驱动次数        :", pump_state["n"])
    print("轮询中的异常        :", pump_state["errors"] if pump_state["errors"] else "无")
    print("每次轮询后的卡片数  :", pump_state["cards"])
    print("轮询结束 status     :", repr(panel.status_label.text()))
    check("80ms 轮询下 show_candidates/set_status 零异常",
          pump_state["n"] >= 5 and not pump_state["errors"],
          "n=%d errors=%s" % (pump_state["n"], pump_state["errors"]))
    check("轮询全程卡片数 <= ui.MAX_CARDS",
          pump_state["cards"] and max(pump_state["cards"]) <= ui_mod.MAX_CARDS,
          "max=%s 上限=%d" % (max(pump_state["cards"]) if pump_state["cards"] else None,
                             ui_mod.MAX_CARDS))
    # 用户报的缺陷（80ms 轮询路径）：一次拖动里没有音效的那几轮 rows=[] 曾把列表清空；
    # 现在空轮必须保留上一轮的条数（这就是"不该自己消失"）。
    check("轮询中的空轮不再清空列表（保留上一轮条数）",
          bool(pump_state["cards"]) and all(c > 0 for c in pump_state["cards"]),
          "每轮卡片数=%s" % (pump_state["cards"],))
    check("集成模式下 Panel 仍未自建 QTimer", len(panel.findChildren(QTimer)) == 0,
          "Panel 的 QTimer 子对象=%d" % len(panel.findChildren(QTimer)))
    print("start(max_ms=700) 返回:", rc, "| isVisible:", panel.isVisible())
    check("start() 在事件循环后正常返回", isinstance(rc, int) and panel.isVisible())

    # ---- 7c. 中号面板 / 品质徽章 / 拖动路径（全部从 ui 常量推导） ----
    line("7c. 中号面板常量 / 品质徽章与左边条 / 拖动路径（系统拖动优先，手动回退）")
    from PyQt5.QtCore import QPoint, QPointF, Qt
    from PyQt5.QtGui import QMouseEvent
    from PyQt5.QtWidgets import QFrame as _QFrame
    from PyQt5.QtWidgets import QLabel as _QLabel
    from PyQt5.QtWidgets import QProgressBar as _QProgressBar
    from PyQt5.QtWidgets import QWidget as _QWidget
    from PyQt5.QtCore import QTimer as _QTimer

    spec_pins = (
        ("PANEL_WIDTH", ui_mod.PANEL_WIDTH, 440),
        ("PANEL_HEIGHT", ui_mod.PANEL_HEIGHT, 820),
        ("THUMB_SIZE", ui_mod.THUMB_SIZE, 72),
        ("MAX_CARDS", ui_mod.MAX_CARDS, 20),
    )
    print("常量（用户选定：中面板 440x820 / 缩略图 72 / 上限 20）:")
    for name, got, want in spec_pins:
        print("  %-13s = %-4s 期望 %-4s" % (name, got, want))
    check("尺寸/数量常量 = 用户选定的中号面板",
          all(got == want for _n, got, want in spec_pins),
          "; ".join("%s=%s" % (n, got) for n, got, _w in spec_pins))
    check("缩略图与百分比字号都来自 ui 常量（进度条已删除）",
          ui_mod.THUMB_SIZE >= 72 and ui_mod.SCORE_FONT_PX >= 22
          and not hasattr(ui_mod, "SCORE_BAR_WIDTH"),
          "THUMB_SIZE=%d SCORE_FONT_PX=%d 已无 SCORE_BAR_*" % (ui_mod.THUMB_SIZE,
                                                              ui_mod.SCORE_FONT_PX))
    check("卡片字号写入 STYLESHEET（名称/参考价值/大号百分比）",
          all(("%dpx" % getattr(ui_mod, n)) in ui_mod.STYLESHEET
              for n in ("CARD_NAME_FONT_PX", "CARD_VALUE_FONT_PX", "SCORE_FONT_PX")),
          "name=%dpx meta=%dpx value=%dpx note=%dpx score=%dpx"
          % (ui_mod.CARD_NAME_FONT_PX, ui_mod.CARD_META_FONT_PX, ui_mod.CARD_VALUE_FONT_PX,
             ui_mod.CARD_NOTE_FONT_PX, ui_mod.SCORE_FONT_PX))

    def _probe_card(quality_name, score=88):
        return panel._make_card({
            "name": "测试物品", "quality": quality_name, "category": "收藏品",
            "slot": 1, "orientation": "竖", "reference_value": 12345,
            "reference_value_text": "12345", "score": score, "note": "",
        })

    def _rel_luma(hex_color):
        text = str(hex_color).lstrip("#")
        r, g, b = int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
        return 0.299 * r + 0.587 * g + 0.114 * b

    badge_ok = True
    badge_detail = []
    for quality_name, expect_color in ui_mod.QUALITY_COLORS.items():
        card = _probe_card(quality_name)
        badge = card.findChild(_QLabel, "qualitybadge")
        stripe = card.findChild(_QFrame, "qualitystripe")
        thumb = card.findChild(_QLabel, "cardthumb")
        row_ok = (
            badge is not None and stripe is not None and thumb is not None
            and badge.text() == quality_name
            and expect_color in badge.styleSheet()
            and expect_color in stripe.styleSheet()
            and ui_mod.badge_text_color(expect_color) in badge.styleSheet()
            and (thumb.width(), thumb.height()) == (ui_mod.THUMB_SIZE, ui_mod.THUMB_SIZE)
        )
        badge_ok = badge_ok and row_ok
        badge_detail.append("%s=%s%s" % (quality_name, expect_color, "" if row_ok else "(FAIL)"))
    print("品质徽章/左边条      :", " ".join(badge_detail))
    check("徽章(填充色圆角)+左边条按 QUALITY_COLORS 着色（金/红/紫）", badge_ok,
          " ".join(badge_detail))
    # 大号彩色百分比：**没有进度条**，只有 22px 粗体数字，颜色按概率阈值
    score_cases = ((90, ui_mod.SCORE_COLOR_HIGH), (85, ui_mod.SCORE_COLOR_HIGH),
                   (84, ui_mod.SCORE_COLOR_MID), (70, ui_mod.SCORE_COLOR_MID),
                   (69, ui_mod.SCORE_COLOR_LOW), (50, ui_mod.SCORE_COLOR_LOW),
                   (49, ui_mod.SCORE_COLOR_WEAK), (0, ui_mod.SCORE_COLOR_WEAK))
    score_detail = []
    score_ok = True
    for value, expect in score_cases:
        card = _probe_card("金色", score=value)
        label = card.findChild(_QLabel, "cardscore")
        got_color = ui_mod.score_color(value)
        ok = (label is not None and label.text() == "%d%%" % value
              and got_color == expect and expect in label.styleSheet()
              and ("font-size: %dpx" % ui_mod.SCORE_FONT_PX) in label.styleSheet()
              and "bold" in label.styleSheet()
              and card.findChild(_QProgressBar, "cardbar") is None)
        score_ok = score_ok and ok
        score_detail.append("%d%%->%s%s" % (value, got_color, "" if ok else "(FAIL)"))
    print("百分比数字/颜色      :", " | ".join(score_detail))
    check("大号彩色百分比：22px 粗体 + 概率色阈值（且已无进度条）", score_ok,
          " | ".join(score_detail))
    check("百分比色系与品质徽章色系区分（含未知品质回落色都不撞色）",
          not (set((ui_mod.SCORE_COLOR_HIGH, ui_mod.SCORE_COLOR_MID, ui_mod.SCORE_COLOR_LOW,
                    ui_mod.SCORE_COLOR_WEAK))
               & (set(ui_mod.QUALITY_COLORS.values()) | {ui_mod.DEFAULT_QUALITY_COLOR}))
          and len({ui_mod.SCORE_COLOR_HIGH, ui_mod.SCORE_COLOR_MID, ui_mod.SCORE_COLOR_LOW,
                   ui_mod.SCORE_COLOR_WEAK}) == 4,
          "score=%s quality=%s default=%s" % ([ui_mod.SCORE_COLOR_HIGH, ui_mod.SCORE_COLOR_MID,
                                               ui_mod.SCORE_COLOR_LOW, ui_mod.SCORE_COLOR_WEAK],
                                              list(ui_mod.QUALITY_COLORS.values()),
                                              ui_mod.DEFAULT_QUALITY_COLOR))
    unknown_card = _probe_card("未知品质")
    unknown_badge = unknown_card.findChild(_QLabel, "qualitybadge")
    unknown_stripe = unknown_card.findChild(_QFrame, "qualitystripe")
    check("未知品质回落 DEFAULT_QUALITY_COLOR",
          unknown_badge is not None and unknown_stripe is not None
          and ui_mod.DEFAULT_QUALITY_COLOR in unknown_badge.styleSheet()
          and ui_mod.DEFAULT_QUALITY_COLOR in unknown_stripe.styleSheet(),
          "badge=%r stripe=%r" % (unknown_badge.styleSheet() if unknown_badge else None,
                                  unknown_stripe.styleSheet() if unknown_stripe else None))
    contrast_colors = list(ui_mod.QUALITY_COLORS.values()) + [ui_mod.DEFAULT_QUALITY_COLOR]
    check("徽章文字色按相对亮度取对比色（亮底深字 / 暗底白字）",
          all(ui_mod.badge_text_color(c) == ("#131720" if _rel_luma(c) >= 150 else "#ffffff")
              for c in contrast_colors),
          ", ".join("%s->%s" % (c, ui_mod.badge_text_color(c)) for c in contrast_colors))
    # 顺序检查：UI 只能展示引擎给顺序，不得重排（slots=2 的槽位优先语义由引擎决定）
    for _combo in (panel.quality_combo, panel.category_combo, panel.orientation_combo):
        _combo.setCurrentIndex(0)
    ordered_names = panel.show_candidates(
        fake_rows, slots=2, orientation="H", last_reason="已匹配；相似度不代表出货概率。")
    # 玩家面板默认 strict_shape=True（task-16）→ 对照也用严格口径，验证"UI 不重排"
    expected_names = engine.resolve(fake_rows, 2, "H", strict_shape=True)
    shown_names = list(panel._shown_candidates)
    check("候选顺序未被 UI 改动（徽章/左边条只是展示；严格过滤后仍不重排）",
          [c["name"] for c in ordered_names] == [c["name"] for c in expected_names]
          and [c["name"] for c in shown_names] == [c["name"] for c in expected_names]
          and len(expected_names) < len(fake_rows and engine.resolve(fake_rows, 2, "H")),
          "UI 显示前 3 条=%s（严格=%d 条 / 旧口径=%d 条）"
          % ([c["name"] for c in shown_names[:3]], len(expected_names),
             len(engine.resolve(fake_rows, 2, "H"))))

    # 布局回归：缩略图加大 + MAX_CARDS 变多后，卡片不得被压缩、内容不得溢出、必须能滚动
    layout_cands = [{
        "name": "布局%d" % i, "quality": ("金色", "红色", "紫色")[i % 3], "category": "收藏品",
        "slot": i % 9, "orientation": "竖", "reference_value": 1000 * i,
        "score": 90 - i % 10, "note": "",
    } for i in range(ui_mod.MAX_CARDS)]
    panel._rebuild_cards(layout_cands)
    QCoreApplication.processEvents()
    layout_cards = list(panel._card_widgets)
    overflow = []
    for card in layout_cards:
        if card.layout() is not None:
            card.layout().activate()
        rect = card.rect()
        for child in card.findChildren(_QWidget):
            geo = child.geometry()
            if (geo.left() < 0 or geo.top() < 0
                    or geo.right() > rect.right() or geo.bottom() > rect.bottom()):
                overflow.append((child.objectName() or type(child).__name__,
                                 (geo.x(), geo.y(), geo.width(), geo.height())))
    print("卡片数/高度         : %d 张，高度%s … 下限 CARD_MIN_HEIGHT=%d"
          % (len(layout_cards), [c.height() for c in layout_cards[:4]], ui_mod.CARD_MIN_HEIGHT))
    print("卡片内容溢出        :", overflow if overflow else "无")
    check("卡片不被压缩：高度 >= CARD_MIN_HEIGHT（72px 缩略图装得下）",
          bool(layout_cards) and all(c.height() >= ui_mod.CARD_MIN_HEIGHT for c in layout_cards),
          "heights=%s 下限=%d" % ([c.height() for c in layout_cards], ui_mod.CARD_MIN_HEIGHT))
    check("缩略图/徽章/左边条/大号百分比都装得进卡片（无溢出）", not overflow, str(overflow))
    scrollbar = panel.scroll_area.verticalScrollBar()
    viewport_h = panel.scroll_area.viewport().height()
    need = len(layout_cards) * ui_mod.CARD_MIN_HEIGHT + max(0, len(layout_cards) - 1) * panel.cards_layout.spacing()
    print("滚动范围            : need=%d viewport=%d host=%d scrollbar.max=%d"
          % (need, viewport_h, panel.cards_host.height(), scrollbar.maximum()))
    check("候选多于一屏时可滚动（滚动条范围 > 0 且内容不越界）",
          (need <= viewport_h or scrollbar.maximum() > 0) and panel.cards_host.height() >= need,
          "need=%d viewport=%d max=%d host=%d hostHint=%s hostMinHint=%s visible=%s vpol=%s"
          % (need, viewport_h, scrollbar.maximum(), panel.cards_host.height(),
             panel.cards_host.sizeHint(), panel.cards_host.minimumSizeHint(),
             panel.isVisible(), panel.scroll_area.verticalScrollBarPolicy()))
    panel._rebuild_cards(layout_cands[:2])
    QCoreApplication.processEvents()
    check("候选变少时滚动范围缩回（不残留空白）",
          panel.scroll_area.verticalScrollBar().maximum() == 0
          and panel.cards_host.height() <= panel.scroll_area.viewport().height() + 1,
          "max=%d host=%d viewport=%d" % (panel.scroll_area.verticalScrollBar().maximum(),
                                          panel.cards_host.height(),
                                          panel.scroll_area.viewport().height()))
    panel._rebuild_cards([])
    QCoreApplication.processEvents()

    # 拖动：系统拖动优先 → 拿不到就手动回退；三条失败分支都必须不抛异常
    try:
        panel.show()
        panel.winId()                      # 强制创建 native 窗口，windowHandle() 才可用
        QCoreApplication.processEvents()
    except Exception:
        pass
    handle = panel.windowHandle()
    api_ok = handle is not None and hasattr(handle, "startSystemMove")
    print("platformName         :", QApplication.platformName())
    print("windowHandle()/API   :", handle, "| startSystemMove 存在:", api_ok)
    check("QWindow.startSystemMove API 可用（不可用时代码必须走手动回退）", api_ok,
          "handle=%r" % (handle,))
    real_result = None
    if api_ok and QApplication.platformName() == "offscreen":
        # 只在 offscreen 调真实实现：真实桌面上"无按键"调用会进入系统移窗循环，不能离线做
        try:
            real_result = bool(handle.startSystemMove())
        except Exception as exc:                       # pragma: no cover
            real_result = "EXC:%s" % type(exc).__name__
        print("真实 startSystemMove():", real_result, "（offscreen 平台未实现 → 预期 False）")
        check("offscreen 下真实 startSystemMove() 返回 False 且不抛异常", real_result is False,
              "实测=%r" % (real_result,))
    else:
        print("[SKIP] 非 offscreen 平台：不调用真实 startSystemMove()（会进入系统移窗循环）")

    move_ev = QMouseEvent(QMouseEvent.MouseMove, QPointF(40, 40), QPointF(340, 340),
                          Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
    press_ev = QMouseEvent(QMouseEvent.MouseButtonPress, QPointF(10, 10), QPointF(310, 310),
                           Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    release_ev = QMouseEvent(QMouseEvent.MouseButtonRelease, QPointF(40, 40), QPointF(340, 340),
                             Qt.LeftButton, Qt.NoButton, Qt.NoModifier)

    # (1) 系统路径：spy 真实的 QWindow.startSystemMove()，返回 True 时必须不再手动 move
    spy = {"n": 0}
    real_method = None
    panel.show()
    panel.winId()
    QCoreApplication.processEvents()
    handle = panel.windowHandle()          # 已 show/native，句柄可用
    api_ok = handle is not None and hasattr(handle, "startSystemMove")
    if api_ok:
        real_method = handle.startSystemMove
        handle.startSystemMove = lambda: (spy.__setitem__("n", spy["n"] + 1), True)[1]
    panel.move(300, 300)
    pos_before = (panel.x(), panel.y())
    panel.mousePressEvent(press_ev)
    exc_system = None
    try:
        panel.mouseMoveEvent(move_ev)
    except Exception as exc:
        exc_system = "%s: %s" % (type(exc).__name__, exc)
    pos_after_system = (panel.x(), panel.y())
    system_done = panel._drag_system_done
    if api_ok and handle is not None and real_method is not None:
        handle.startSystemMove = real_method
    check("系统拖动路径：调用了 QWindow.startSystemMove()、无异常、不叠加手动 move",
          api_ok and spy["n"] == 1 and exc_system is None
          and pos_after_system == pos_before and system_done is True,
          "startSystemMove 调用=%d exc=%s pos %s -> %s _drag_system_done=%s"
          % (spy["n"], exc_system, pos_before, pos_after_system, system_done))

    # (2) 回退路径：startSystemMove 不可用（False）时手动 move，语义与旧实现一致 + 强制重绘
    repaints = {"n": 0}
    original_force_repaint = panel._force_repaint

    def _counting_repaint():
        repaints["n"] += 1
        return original_force_repaint()

    original_try_system = panel._try_start_system_move
    panel._force_repaint = _counting_repaint
    panel._try_start_system_move = lambda: False
    panel.move(300, 300)
    panel.mousePressEvent(press_ev)
    offset = panel._drag_offset
    exc_manual = None
    try:
        panel.mouseMoveEvent(move_ev)
    except Exception as exc:
        exc_manual = "%s: %s" % (type(exc).__name__, exc)
    expected = QPoint(340, 340) - offset
    pos_after_manual = (panel.x(), panel.y())
    panel.mouseReleaseEvent(release_ev)
    panel._force_repaint = original_force_repaint
    panel._try_start_system_move = original_try_system
    check("回退路径：手动 move 与旧语义一致（按住点偏移跟随光标）且强制重绘",
          exc_manual is None and pos_after_manual == (expected.x(), expected.y())
          and repaints["n"] >= 1,
          "offset=%s pos=%s 期望=%s 重绘次数=%d exc=%s"
          % (offset, pos_after_manual, (expected.x(), expected.y()), repaints["n"], exc_manual))
    check("拖动结束后状态复位（_drag_offset/_drag_system_done）",
          panel._drag_offset is None and panel._drag_system_done is False,
          "offset=%r system_done=%r" % (panel._drag_offset, panel._drag_system_done))

    # (3) 三条回退分支：windowHandle() 为 None / 无 startSystemMove / 调用抛异常
    original_window_handle = panel.windowHandle

    class _NoApiHandle:
        pass

    class _RaisingHandle:
        def startSystemMove(self):
            raise RuntimeError("boom")

    branch_results = []
    for label, fake in (("windowHandle()=None", lambda: None),
                        ("无 startSystemMove", lambda: _NoApiHandle()),
                        ("startSystemMove 抛异常", lambda: _RaisingHandle())):
        panel.windowHandle = fake
        try:
            got = panel._try_start_system_move()
            branch_results.append((label, got, None))
        except Exception as exc:
            branch_results.append((label, None, "%s: %s" % (type(exc).__name__, exc)))
    panel.windowHandle = original_window_handle
    print("回退分支             :", branch_results)
    check("windowHandle() 为 None / API 缺失 / 调用抛异常 → 一律 False 且不抛异常",
          all(got is False and err is None for _l, got, err in branch_results),
          str(branch_results))

    panel.close()
    check("Panel.close() 可调用", True)
    check("7c 之后 Panel 仍无 QTimer 子对象（延后滚动刷新用的函数式 singleShot）",
          len(panel.findChildren(_QTimer)) == 0,
          "QTimer 子对象=%d" % len(panel.findChildren(_QTimer)))
    print("UI 构造成功")

    # ============================================ 7d. 粘滞显示（候选不许自己消失）
    line("7d. 粘滞显示：rows=[] 时保留上次候选，直到下次真正匹配到声音才更新")
    # 用 engine=None 的 Panel：这里测的是 UI 侧粘滞逻辑本身，候选展开/排序不在本段范围，
    # 直接给 dict 候选可以让 S1~S5 完全确定性（不依赖 DB / fake_rows 的候选数）。
    sticky = ui_mod.Panel(None, start_thread=False)
    sticky.show()
    QCoreApplication.processEvents()
    batch_a = [
        {"name": "粘滞甲", "quality": "金色", "category": "收藏品", "slot": 2, "orientation": "H",
         "reference_value": 100, "score": 90, "note": ""},
        {"name": "粘滞乙", "quality": "红色", "category": "收藏品", "slot": 2, "orientation": "H",
         "reference_value": 80, "score": 80, "note": ""},
        {"name": "粘滞丙", "quality": "紫色", "category": "收藏品", "slot": 1, "orientation": "V",
         "reference_value": 60, "score": 70, "note": ""},
    ]
    batch_b = [
        {"name": "粘滞新", "quality": "金色", "category": "收藏品", "slot": 1, "orientation": "H",
         "reference_value": 999, "score": 99, "note": ""},
    ]

    def _sticky_state(tag):
        detail = sticky.reason_label.text()
        match_text = sticky.match_label.text()
        ages = re.findall(r"\d+\.\ds", match_text)
        print("%-20s 卡片=%d all=%d 粘滞=%s waiting可见=%s 匹配行=%r 详情行=%r"
              % (tag, len(sticky._card_widgets), len(sticky._all_candidates), sticky._sticky,
                 sticky.waiting_label.isVisible(), match_text, detail))
        return match_text, ages

    def _capture_stdout(func):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            func()
        return buffer.getvalue()

    # ---- S1: 3 条 → 空回调 → 仍是 3 条 ----
    sticky.show_candidates(batch_a, slots=2, orientation="H",
                           last_reason="与参考样本不够相似，可能是未知音效或背景",
                           source="红块", trigger_src="鼠标上升沿")
    cards_before = len(sticky._card_widgets)
    output_s1 = _capture_stdout(lambda: sticky.show_candidates(
        [], slots=2, orientation="H", last_reason="与参考样本不够相似，可能是未知音效或背景",
        source="红块", trigger_src="按住续触"))
    text_s1, ages_s1 = _sticky_state("S1 空回调")
    sticky_lines_s1 = [ln for ln in output_s1.splitlines() if ln.startswith("[sticky]")]
    check("S1 rows=[] 后卡片不消失（仍是上一批 3 张，_all_candidates 不变）",
          cards_before == 3 and len(sticky._card_widgets) == 3
          and len(sticky._all_candidates) == 3 and sticky._sticky is True,
          "首轮=%d 空回调后=%d all=%d 粘滞=%s"
          % (cards_before, len(sticky._card_widgets), len(sticky._all_candidates), sticky._sticky))
    check("S1 面板不出现「未匹配」/「原因」字样；匹配时间行给出 dd.ds（粘滞期体现新旧）",
          ("未匹配" not in sticky.reason_label.text())
          and ("未匹配" not in text_s1) and ("原因" not in sticky.reason_label.text())
          and bool(ages_s1) and re.match(r"^匹配于 \d+\.\ds 前$", text_s1 or ""),
          "匹配行=%r 详情行=%r" % (text_s1, sticky.reason_label.text()))
    check("S1 [sticky] 只打一行到 stdout（诊断仍在日志里）",
          len(sticky_lines_s1) == 1, "%r" % (sticky_lines_s1,))

    # ---- S4: 连续两次空回调 → 不翻倍、控件树无累积、不再打日志 ----
    widgets_before = len(sticky.findChildren(_QWidget))
    output_s4 = _capture_stdout(lambda: (sticky.show_candidates([]), sticky.show_candidates([])))
    widgets_after = len(sticky.findChildren(_QWidget))
    check("S4 连续两次 rows=[]：卡片不翻倍、控件树无累积、[sticky] 不重复",
          len(sticky._card_widgets) == 3 and widgets_after == widgets_before
          and not [ln for ln in output_s4.splitlines() if ln.startswith("[sticky]")],
          "卡片=%d 控件 %d->%d 日志=%r"
          % (len(sticky._card_widgets), widgets_before, widgets_after, output_s4))

    # ---- S3: 换一批新的非空候选 → 正常替换、粘滞提示消失 ----
    sticky.show_candidates(batch_b, slots=1, orientation="H", last_reason="已匹配")
    text_s3, ages_s3 = _sticky_state("S3 新批次")
    names_s3 = [c.get("name") for c in sticky._shown_candidates]
    check("S3 新候选替换为新的一批、匹配时间行刷新（无粘滞字样）",
          len(sticky._card_widgets) == 1 and sticky._sticky is False
          and names_s3 == ["粘滞新"]
          and bool(ages_s3) and float(ages_s3[0][:-1]) < 1.0
          and ("未匹配" not in sticky.reason_label.text()),
          "卡片=%d 粘滞=%s shown=%s 匹配行=%r" % (len(sticky._card_widgets), sticky._sticky,
                                               names_s3, text_s3))

    # ---- S5: 空 → 非空 → 空，_last_match_at 被刷新、年龄每次重算 ----
    sticky._last_match_at -= 5.0            # 人为把"上次匹配"拨旧 5 秒
    sticky.show_candidates([], last_reason="与参考样本不够相似")
    text_s5a, ages_s5a = _sticky_state("S5 拨旧 5s")
    aged_ok = bool(ages_s5a) and abs(float(ages_s5a[0][:-1]) - 5.0) < 0.5
    match_at_before = sticky._last_match_at
    sticky.show_candidates(batch_b, slots=1, orientation="H", last_reason="已匹配")
    refreshed_ok = sticky._last_match_at is not None and (sticky._last_match_at - match_at_before) > 4.0
    sticky.show_candidates([], last_reason="与参考样本不够相似")
    text_s5b, ages_s5b = _sticky_state("S5 空→非空→空")
    final_age = float(ages_s5b[0][:-1]) if ages_s5b else -1.0
    check("S5 年龄每次刷新按当前时间重算（拨旧 5s 立刻反映为 5.x s）", aged_ok,
          "拨旧后年龄=%s" % ages_s5a)
    check("S5 非空批次刷新 _last_match_at，之后的空回调年龄重新变小",
          refreshed_ok and 0.0 <= final_age < 1.0,
          "last_match_at 前移=%.2fs 最终年龄=%s" % (sticky._last_match_at - match_at_before, ages_s5b))

    # ---- S2: 清空后空回调不许复活（本方案最易漏的一条）----
    sticky._on_clear_clicked()
    cards_after_clear = len(sticky._card_widgets)
    sticky.show_candidates([], last_reason="与参考样本不够相似")
    QCoreApplication.processEvents()
    text_s2, ages_s2 = _sticky_state("S2 清空后再空回调")
    check("S2 清空后再 rows=[]：卡片仍为空（不复活）、waiting 文案可见、状态已清",
          cards_after_clear == 0 and len(sticky._card_widgets) == 0
          and not sticky._all_candidates and sticky.waiting_label.isVisible()
          and sticky._sticky is False and sticky._last_match_at is None,
          "清空后卡片=%d 再空回调后=%d all=%d 粘滞=%s match_at=%r waiting可见=%s"
          % (cards_after_clear, len(sticky._card_widgets), len(sticky._all_candidates),
             sticky._sticky, sticky._last_match_at, sticky.waiting_label.isVisible()))
    check("S2 清空后匹配时间行消失（不再有年龄文本）、面板无诊断字样",
          ("未匹配" not in text_s2) and not ages_s2
          and sticky.match_label.text() == "" and not sticky.match_label.isVisible(),
          "匹配行=%r 年龄=%s" % (text_s2, ages_s2))
    sticky.close()
    check("7d 面板关闭无异常", sticky._closed is True)

    # ======================================== 7e. 面向玩家的展示契约（只留四类信息）
    line("7e. 玩家面板契约：只留四类信息；诊断只写日志、不进面板")
    player = ui_mod.Panel(None, start_thread=False)
    player.show()
    QCoreApplication.processEvents()
    forbidden_on_panel = ("触发", "原因", "格宽", "锁定", "已锁", "trace", "Trace", "TRACE",
                          "管理员", "看到", "受理", "pitch", "置信度", "来源=")
    player_rows = [{"name": "金块", "quality": "金色", "category": "收藏品", "slot": 2,
                    "orientation": "V", "reference_value": 270000, "score": 92, "note": ""}]
    player.set_status(ui_mod.MONITORING_STATUS_TEXT)
    player.show_candidates(player_rows, slots=2, orientation="V",
                           last_reason="物品边框｜触发#7 视觉✓ 音频✓ 来源=边框 锁定=已锁 2格V",
                           source="边框", trigger_src="按住续触")
    visible_texts = []
    for _lab in player.findChildren(_QLabel):
        if _lab.isVisible():
            visible_texts.append((_lab.objectName(), _lab.text()))
    leaks = [(tok, obj, txt) for tok, obj, txt in
             ((tok, obj, txt) for obj, txt in visible_texts for tok in forbidden_on_panel)
             if tok in txt]
    print("面板可见文本        :", visible_texts)
    print("诊断字样泄漏        :", leaks if leaks else "0 命中")
    check("① 面板可见文本里找不到 触发/原因/格宽/锁定/trace/看到受理/pitch 字样",
          not leaks, str(leaks))
    check("① 诊断值原样**送达**面板（_last_reason/_last_trigger_src），只是不显示",
          player._last_reason == "物品边框｜触发#7 视觉✓ 音频✓ 来源=边框 锁定=已锁 2格V"
          and player._last_trigger_src == "按住续触",
          "last_reason=%r last_trigger_src=%r" % (player._last_reason,
                                                  player._last_trigger_src))
    check("① 详情行只有 格数/朝向/来源 三个字段",
          player.reason_label.text() == "格数 2格 · 朝向 V · 来源 边框",
          "详情行=%r" % player.reason_label.text())
    check("① 传入的 reason / trigger_src 被接收但一字不进详情行",
          "按住续触" not in player.reason_label.text()
          and "视觉" not in player.reason_label.text()
          and player._last_trigger_src == "按住续触",
          "详情行=%r trigger_src=%r" % (player.reason_label.text(), player._last_trigger_src))

    # ② 音频信息行：三段齐全；缺段优雅省略；一段都没有则整行隐藏
    player.set_audio_source(samplerate=48000, device="Speakers (Realtek(R) Audio)", peak=0.018)
    audio_full = player.audio_label.text()
    player.set_audio_source(samplerate=44100)
    audio_one = player.audio_label.text()
    audio_one_visible = player.audio_label.isVisible()
    player.set_audio_source()
    audio_none = player.audio_label.text()
    audio_none_visible = player.audio_label.isVisible()
    print("音频行（三段/一段/无）:", repr(audio_full), repr(audio_one), repr(audio_none))
    check("② 音频信息行三段格式 = 音频 <Hz>Hz · <设备名> · 峰值 <n>",
          audio_full == "音频 48000Hz · Speakers (Realtek(R) Audio) · 峰值 0.018",
          "audio=%r" % audio_full)
    check("② 缺段优雅省略（只给采样率时只显示第一段；全缺则整行隐藏，绝不编造）",
          audio_one == "音频 44100Hz" and audio_one_visible
          and audio_none == "" and not audio_none_visible,
          "one=%r(%s) none=%r(%s)" % (audio_one, audio_one_visible, audio_none,
                                      audio_none_visible))

    # ③ 匹配时间行：格式 + 每次刷新按当前时间重算
    age0 = re.findall(r"\d+\.\ds", player.match_label.text())
    player._last_match_at -= 7.5
    player.set_status(ui_mod.MONITORING_STATUS_TEXT)          # 触发一次刷新（不是重建）
    aged_match = player.match_label.text()
    age1 = re.findall(r"\d+\.\ds", aged_match)
    print("匹配时间行          :", repr(aged_match), "（拨旧 7.5s 前 =", age0, "）")
    check("③ 匹配时间行格式 = 匹配于 <n.n>s 前，且每次刷新重算（拨旧 7.5s 立刻反映）",
          bool(age0) and bool(age1) and re.match(r"^匹配于 \d+\.\ds 前$", aged_match)
          and abs(float(age1[0][:-1]) - 7.5) < 0.5,
          "刷新前=%s 拨旧后=%r" % (age0, aged_match))

    # ④ 大号百分比数字的顺序与分数一致（卡片数量/顺序/筛选语义不变）
    player.set_audio_source(samplerate=44100, device="Dev", peak=0.001)
    many = [{"name": "A", "quality": "金色", "category": "收藏品", "slot": 2, "orientation": "H",
             "reference_value": 10, "score": 92, "note": ""},
            {"name": "B", "quality": "红色", "category": "收藏品", "slot": 2, "orientation": "H",
             "reference_value": 20, "score": 61, "note": ""},
            {"name": "C", "quality": "紫色", "category": "收藏品", "slot": 1, "orientation": "V",
             "reference_value": 30, "score": 30, "note": ""}]
    player.show_candidates(many, slots=2, orientation="H", last_reason="r", source="边框")
    score_texts = [c.findChild(_QLabel, "cardscore").text() for c in player._card_widgets]
    colors = [c.findChild(_QLabel, "cardscore").styleSheet() for c in player._card_widgets]
    print("卡片百分比/颜色     :", list(zip(score_texts,
                                          [ui_mod.score_color(s["score"]) for s in many])))
    check("④ 卡片数量/顺序/百分比与引擎给的一致（大号数字着色正确）",
          score_texts == ["92%", "61%", "30%"]
          and ui_mod.score_color(92) in colors[0]
          and ui_mod.score_color(61) in colors[1]
          and ui_mod.score_color(30) in colors[2],
          "texts=%s colors=%s" % (score_texts, colors))
    # ⑤ 空闲态文案逐字（再次在“有候选过”的面板上验证切回空闲态）
    player.set_active_state(False)
    check("⑤ 切回空闲后底栏逐字等于用户原话（不追加任何诊断）",
          player.status_label.text() == ui_mod.IDLE_STATUS_TEXT,
          "status=%r" % player.status_label.text())
    # ⑥ 署名
    check("⑥ 标题行/窗口标题含 作者 烟火",
          player.title_label.text().endswith(ui_mod.AUTHOR_TEXT)
          and player.windowTitle().endswith(ui_mod.AUTHOR_TEXT),
          "title=%r window=%r" % (player.title_label.text(), player.windowTitle()))
    player.close()
    check("7e 面板关闭无异常", player._closed is True)

    # ============================ 7f. 收起/展开 与 关闭并退出（task-15）
    line("7f. 标题行「收起/展开」与「关闭并退出」（折叠而非 showMinimized/hide）")
    from PyQt5.QtCore import QRect as _QRect

    btns = ui_mod.Panel(None, start_thread=False)
    btns.show()
    QCoreApplication.processEvents()
    exp_w, exp_h = int(ui_mod.PANEL_WIDTH), int(ui_mod.PANEL_HEIGHT)
    bg = _QRect(0, 0, 0, 0)
    try:
        _screen = QApplication.primaryScreen()
        if _screen is not None:
            bg = _screen.geometry()
    except Exception:
        pass

    def _rect_near(rect):
        """沿用 mprobe3_screenrect 的口径：矩形落在屏幕坐标系附近。"""
        if not rect:
            return False
        x, y, w, h = rect
        return ((bg.left() - w < x < bg.left() + bg.width())
                and (bg.top() - h < y < bg.top() + bg.height()))

    # B1 默认展开
    print("B1 展开态            : size=(%d,%d) body可见=%s 折叠=%s 按钮=%r/%r"
          % (btns.width(), btns.height(), btns.body.isVisible(), btns.is_collapsed(),
             btns.collapse_button.text(), btns.close_button.text()))
    check("B1 默认展开：size == (PANEL_WIDTH, PANEL_HEIGHT)、body 可见、按钮文案为 —/✕",
          (btns.width(), btns.height()) == (exp_w, exp_h) and btns.body.isVisible()
          and btns.is_collapsed() is False and btns.collapse_button.text() == "—"
          and btns.close_button.text() == "✕",
          "size=%s body=%s 折叠=%s" % ((btns.width(), btns.height()), btns.body.isVisible(),
                                      btns.is_collapsed()))
    check("B1b 两按钮不抢焦点 + tooltip 写明语义",
          btns.collapse_button.focusPolicy() == Qt.NoFocus
          and btns.close_button.focusPolicy() == Qt.NoFocus
          and "收起" in btns.collapse_button.toolTip()
          and "关闭" in btns.close_button.toolTip()
          and "停止监测" in btns.close_button.toolTip(),
          "tooltip=%r / %r" % (btns.collapse_button.toolTip(), btns.close_button.toolTip()))

    # B2 点 — → 折叠；标题与两按钮仍可见、仍在窗口内、命中测试可点；screen_rect 跟随
    rect_expanded = btns.screen_rect()
    btns.collapse_button.click()
    QCoreApplication.processEvents()
    coll_w, coll_h = btns.width(), btns.height()
    rect_collapsed = btns.screen_rect()
    t_geo = btns.title_label.geometry()
    c_geo = btns.collapse_button.geometry()
    x_geo = btns.close_button.geometry()
    inside = all(g.bottom() < coll_h and g.right() <= coll_w for g in (t_geo, c_geo, x_geo))
    hit_collapse = btns.childAt(c_geo.center()) is btns.collapse_button
    hit_close = btns.childAt(x_geo.center()) is btns.close_button
    print("B2 折叠态            : size=(%d,%d) body可见=%s 标题/收起/关闭可见=%s/%s/%s 窗内=%s 命中=%s/%s "
          "按钮文案=%r" % (coll_w, coll_h, btns.body.isVisible(), btns.title_label.isVisible(),
                         btns.collapse_button.isVisible(), btns.close_button.isVisible(), inside,
                         hit_collapse, hit_close, btns.collapse_button.text()))
    print("B2 screen_rect       : 展开=%s → 折叠=%s（折叠后应为 (x, y, %d, %d)）"
          % (rect_expanded, rect_collapsed, coll_w, coll_h))
    check("B2 点 —：高度显著变小、body 隐藏、标题+两按钮可见可点且都在窗口内",
          coll_h < exp_h - 100 and btns.body.isVisible() is False and btns.is_collapsed() is True
          and btns.title_label.isVisible() and btns.collapse_button.isVisible()
          and btns.close_button.isVisible() and inside and hit_collapse and hit_close
          and btns.collapse_button.text() == "+",
          "折叠=(%d,%d) 展开=(%d,%d) 窗内=%s 命中=%s/%s" % (coll_w, coll_h, exp_w, exp_h, inside,
                                                          hit_collapse, hit_close))
    check("B2b screen_rect() 跟随折叠后的实际尺寸（宽高 == size）",
          rect_collapsed is not None and rect_expanded is not None
          and rect_collapsed[2] == coll_w and rect_collapsed[3] == coll_h
          and rect_expanded[3] == exp_h and rect_collapsed[3] < rect_expanded[3],
          "展开=%s 折叠=%s 折叠 size=(%d,%d)" % (rect_expanded, rect_collapsed, coll_w, coll_h))

    # 折叠后仍能拖动（手动回退路径；系统路径已由 7c 覆盖）
    btns.set_collapsed(True)                      # 状态显式化：确保此刻确实折叠
    QCoreApplication.processEvents()
    btns.move(200, 120)
    original_handle = btns.windowHandle
    btns.windowHandle = lambda: None
    drag_exc = None
    drag_before = (btns.x(), btns.y())
    collapsed_before_drag = btns.is_collapsed()
    try:
        btns.mousePressEvent(press_ev)
        btns.mouseMoveEvent(move_ev)
        btns.mouseReleaseEvent(release_ev)
    except Exception as exc:                              # pragma: no cover
        drag_exc = "%s: %s" % (type(exc).__name__, exc)
    drag_after = (btns.x(), btns.y())
    btns.windowHandle = original_handle
    print("折叠态拖动           : 位置 %s → %s（异常=%s，拖动前折叠=%s，拖动后仍折叠=%s）"
          % (drag_before, drag_after, drag_exc, collapsed_before_drag, btns.is_collapsed()))
    check("折叠后仍能用鼠标拖动窗口（move() 回退路径生效、拖动状态复位）",
          drag_exc is None and collapsed_before_drag and btns.is_collapsed()
          and drag_after != drag_before and btns._drag_offset is None
          and btns._drag_system_done is False,
          "%s → %s exc=%s 折叠=%s→%s" % (drag_before, drag_after, drag_exc,
                                        collapsed_before_drag, btns.is_collapsed()))

    # B3 往返 3 次幂等（先显式回到展开态，再靠点击切换）
    btns.set_collapsed(False)
    QCoreApplication.processEvents()
    cycle = []
    for _ in range(3):
        btns.collapse_button.click()
        QCoreApplication.processEvents()
        cycle.append(("折叠", btns.width(), btns.height(), btns.body.isVisible(),
                      btns.collapse_button.text(), btns.screen_rect()))
        btns.collapse_button.click()
        QCoreApplication.processEvents()
        cycle.append(("展开", btns.width(), btns.height(), btns.body.isVisible(),
                      btns.collapse_button.text(), btns.screen_rect()))
    print("B3 往返 3 次         :", cycle)
    check("B3 收起/展开往返 3 次幂等（尺寸/可见性/文案一致，screen_rect 同步）",
          all(st == "折叠" and (w, h) == (exp_w, coll_h) and vis is False and txt == "+"
              and rect is not None and rect[3] == coll_h
              for st, w, h, vis, txt, rect in cycle[0::2])
          and all(st == "展开" and (w, h) == (exp_w, exp_h) and vis is True and txt == "—"
                  and rect is not None and rect[3] == exp_h
                  for st, w, h, vis, txt, rect in cycle[1::2]),
          str(cycle))

    check("7f 收起/关闭按钮未引入 QTimer 子对象",
          len(btns.findChildren(_QTimer)) == 0,
          "QTimer 子对象=%d" % len(btns.findChildren(_QTimer)))

    # B5 折叠态下 show_candidates：不报错、数据仍写入、展开后仍是 3 张
    btns.set_collapsed(True)                          # 显式折叠
    QCoreApplication.processEvents()
    b5_collapsed_before = btns.is_collapsed()
    rows3 = [{"name": "折叠甲", "quality": "金色", "category": "收藏品", "slot": 2,
              "orientation": "H", "reference_value": 100, "score": 92, "note": ""},
             {"name": "折叠乙", "quality": "红色", "category": "收藏品", "slot": 2,
              "orientation": "H", "reference_value": 80, "score": 61, "note": ""},
             {"name": "折叠丙", "quality": "紫色", "category": "收藏品", "slot": 1,
              "orientation": "V", "reference_value": 60, "score": 30, "note": ""}]
    b5_exc = None
    try:
        btns.show_candidates(rows3, slots=2, orientation="H", last_reason="折叠态喂入",
                             source="边框", trigger_src="按住续触")
    except Exception as exc:                              # pragma: no cover
        b5_exc = "%s: %s" % (type(exc).__name__, exc)
    cards_collapsed = len(btns._card_widgets)
    all_collapsed = len(btns._all_candidates)
    btns.collapse_button.click()                      # 展开
    QCoreApplication.processEvents()
    print("B5 折叠态喂候选      : 折叠中 cards=%d all=%d 异常=%s → 展开后 cards=%d shown=%d"
          % (cards_collapsed, all_collapsed, b5_exc, len(btns._card_widgets),
             len(btns._shown_candidates)))
    check("B5 折叠态 show_candidates(3 条) 不报错、数据写入，展开后仍是 3 张卡",
          b5_exc is None and b5_collapsed_before and cards_collapsed == 3 and all_collapsed == 3
          and len(btns._card_widgets) == 3 and len(btns._shown_candidates) == 3
          and btns.is_collapsed() is False,
          "折叠中 cards=%d all=%d exc=%s 展开后 cards=%d shown=%d 折叠=%s"
          % (cards_collapsed, all_collapsed, b5_exc, len(btns._card_widgets),
             len(btns._shown_candidates), btns.is_collapsed()))

    # B6 screen_rect 折叠前后都在屏幕附近
    btns.set_collapsed(True)
    QCoreApplication.processEvents()
    rect_collapsed2 = btns.screen_rect()
    print("B6 screen_rect 落屏  : 展开=%s near=%s · 折叠=%s near=%s"
          % (rect_expanded, _rect_near(rect_expanded), rect_collapsed2,
             _rect_near(rect_collapsed2)))
    check("B6 screen_rect() 折叠前后都落在屏幕附近（沿用 mprobe3_screenrect 口径）",
          _rect_near(rect_expanded) and _rect_near(rect_collapsed2)
          and rect_collapsed2[3] == btns.height() and rect_collapsed2[2] == btns.width(),
          "展开=%s 折叠=%s" % (rect_expanded, rect_collapsed2))

    # B4 ✕ → 关闭路径（桩记录 stop()，不真的退出测试进程）
    stop_calls = []
    original_stop = btns.stop

    def _stub_stop():
        stop_calls.append("stop")
        btns._stopping = True          # 只记录：不调用 QApplication.quit()，否则会关掉自测事件循环

    btns.stop = _stub_stop
    try:
        btns.close_button.click()
        QCoreApplication.processEvents()
    finally:
        btns.stop = original_stop
    print("B4 关闭按钮          : _closed=%s stop() 调用=%d 次（桩）" % (btns._closed, len(stop_calls)))
    check("B4 点 ✕：走关闭路径（_closed=True 且 stop() 被调用；桩记录不退出测试进程）",
          btns._closed is True and bool(stop_calls) and all(c == "stop" for c in stop_calls),
          "_closed=%s stop 调用=%d" % (btns._closed, len(stop_calls)))
    check("B4b 关闭后 show_candidates 被 _closed 门挡住（返回空、不再改动面板）",
          btns.show_candidates(rows3, slots=2, orientation="H") == [] and btns._closed is True,
          "返回=%r _closed=%s" % (btns.show_candidates(rows3, slots=2, orientation="H"),
                                 btns._closed))

    # ================================================ 9. 严格形状过滤（task-16）
    line("9. 严格形状过滤 F1~F6：不匹配格数/朝向的候选不再展示（只写日志）")
    # 真实 DB 数据（slots=2/H 的「相符」样本；slots=1/H 与 slots=2/V 是两类不符样本）
    fit_2h = ["卫星模型", "金块", "液压扳手"]          # slot=2 orientation=横
    bad_slot = ["缓降器", "金质钢笔"]                  # slot=1 orientation=正（格数不符）
    bad_orient = ["阵列镜片", "单兵通讯装置"]          # slot=2 orientation=竖（朝向不符）
    shape_rows = [{
        "key": "金属声音", "kind": "normal", "score": 90,
        "items": [engine.lookup(n) for n in (fit_2h + bad_slot + bad_orient)],
        "reason": "已匹配；相似度不代表出货概率。",
    }]
    print("严格样本 fit(2格H)      :", fit_2h)
    print("格数不符样本(1格正)     :", bad_slot)
    print("朝向不符样本(2格V)      :", bad_orient)

    # --- F1 严格模式丢弃 slot 不符 ------------------------------------------
    strict_all = engine.resolve(shape_rows, 2, "", strict_shape=True)
    strict_stats = dict(engine.last_stats)          # 必须在下一次 resolve 前快照
    loose_all = engine.resolve(shape_rows, 2, "", strict_shape=False)
    s_names = [c["name"] for c in strict_all]
    print("F1 strict slots=2     :", s_names, "| dropped=", strict_stats["dropped_names"])
    print("F1 loose  slots=2     :", [c["name"] for c in loose_all])
    check("F1 严格模式丢弃 slot 不符（1格 全丢，2格 全留）",
          set(s_names) == set(fit_2h + bad_orient) and not (set(s_names) & set(bad_slot))
          and len(loose_all) == 7,
          "strict=%d 条 loose=%d 条 dropped_shape=%d"
          % (len(strict_all), len(loose_all), strict_stats["dropped_shape"]))
    check("F1b meta：dropped_shape/dropped_names 齐全且 slot_matched 统计原始解析（不受丢弃影响）",
          strict_stats["strict_shape"] is True
          and strict_stats["dropped_shape"] == 2
          and len(strict_stats["dropped_names"]) == 2
          and all(n.startswith(tuple(bad_slot)) for n in strict_stats["dropped_names"])
          and strict_stats["slot_matched"] == 5
          and strict_stats["slot_mismatched"] == 2
          and strict_stats["empty_after_shape"] is False,
          "stats=%s" % {k: strict_stats[k] for k in
                        ("strict_shape", "dropped_shape", "slot_matched", "slot_mismatched",
                         "empty_after_shape")})

    # --- F2 丢弃 orientation 不符 -------------------------------------------
    strict_2h = engine.resolve(shape_rows, 2, "H", strict_shape=True)
    print("F2 strict slots=2/H   :", [c["name"] for c in strict_2h],
          "| dropped=", engine.last_stats["dropped_names"])
    check("F2 朝向不符（2格V）也被丢弃",
          set(c["name"] for c in strict_2h) == set(fit_2h)
          and engine.last_stats["dropped_shape"] == 4
          and any("阵列镜片(2格V,90)" == n for n in engine.last_stats["dropped_names"]),
          "strict=%d 条 dropped=%s" % (len(strict_2h), engine.last_stats["dropped_names"]))

    # --- F3 默认（strict_shape=False）逐字不变 -------------------------------
    loose_2h = engine.resolve(shape_rows, 2, "H")
    golden = [(c["name"], c["score"], c["slot_match"], c["orientation_match"], c["note"])
              for c in loose_2h]
    print("F3 loose slots=2/H 逐条:", golden)
    expect_notes = {
        "卫星模型": "", "金块": "", "液压扳手": "",
        "缓降器": "槽位不匹配（期望 2 格，实际 1 格）；朝向不匹配（期望 横，实际 正）",
        "金质钢笔": "槽位不匹配（期望 2 格，实际 1 格）；朝向不匹配（期望 横，实际 正）",
        "阵列镜片": "朝向不匹配（期望 横，实际 竖）",
        "单兵通讯装置": "朝向不匹配（期望 横，实际 竖）",
    }
    check("F3 strict_shape=False 旧行为逐字不变（不匹配仍保留、排后、带 note）",
          len(loose_2h) == 7
          and all(c["slot_match"] for c in loose_2h[:3])
          and [c["name"] for c in loose_2h[:3]] == fit_2h
          and all(c["note"] == expect_notes[c["name"]] for c in loose_2h)
          and engine.last_stats["dropped_shape"] == 0
          and engine.last_stats["strict_shape"] is False,
          "顺序=%s notes=%s" % ([c["name"] for c in loose_2h],
                                {c["name"]: c["note"] for c in loose_2h}))
    check("F3b 默认参数就是不严格（resolve(rows,2,'H') 与显式 strict_shape=False 完全一致）",
          [(c["name"], c["score"], c["note"]) for c in engine.resolve(shape_rows, 2, "H")]
          == [(c["name"], c["score"], c["note"]) for c in loose_2h]
          and engine.last_stats["strict_shape"] is False,
          "默认=%d 条" % len(engine.resolve(shape_rows, 2, "H")))

    # --- F4 过滤后为空：面板文案正确且没有任何不匹配卡片 ---------------------
    empty_rows = [{
        "key": "硬币声音", "kind": "normal", "score": 92,
        "items": [engine.lookup(n) for n in bad_slot],
        "reason": "已匹配；相似度不代表出货概率。",
    }]
    panel_strict = ui_mod.Panel(engine, start_thread=False)
    panel_strict.set_status("监测中")
    ret = panel_strict.show_candidates(empty_rows, slots=2, orientation="V",
                                       last_reason="已匹配；相似度不代表出货概率。")
    empty_stats = dict(engine.last_stats)
    QCoreApplication.processEvents()
    panel_visible = " | ".join([
        panel_strict.reason_label.text().replace("\n", " "),
        panel_strict.status_label.text(),
        panel_strict.summary_label.text(),
        panel_strict.waiting_label.text(),
    ] + [w.findChild(_QLabel, "cardname").text() if w.findChild(_QLabel, "cardname") else ""
         for w in panel_strict._card_widgets])
    print("F4 严格空结果 → 返回候选数:", len(ret), "| cards:", len(panel_strict._card_widgets))
    print("F4 empty_after_shape :", empty_stats.get("empty_after_shape"),
          "| dropped:", empty_stats.get("dropped_names"))
    print("F4 面板逐行文本      :")
    for _label, _text in (("详情行", panel_strict.reason_label.text()),
                          ("状态栏", panel_strict.status_label.text()),
                          ("汇总  ", panel_strict.summary_label.text()),
                          ("等待区", panel_strict.waiting_label.text()),
                          ("匹配行", panel_strict.match_label.text() or "（隐藏）"),
                          ("音频行", panel_strict.audio_label.text() or "（隐藏）")):
        print("      %s: %r" % (_label, _text))
    check("F4 过滤后为空 → waiting_label 显示『无与 2格V 相符的候选』且 0 张卡片",
          len(ret) == 0 and empty_stats["empty_after_shape"] is True
          and len(panel_strict._card_widgets) == 0
          and not panel_strict.waiting_label.isHidden()      # offscreen 下用 isHidden（isVisible 需窗口已 show）
          and panel_strict.waiting_label.text() == ui_mod.SHAPE_EMPTY_TEXT.format(want="2格V"),
          "waiting=%r cards=%d" % (panel_strict.waiting_label.text(),
                                   len(panel_strict._card_widgets)))
    check("F4b 面板可见文本里没有任何不匹配候选名（也不回退显示）",
          not any(n in panel_visible for n in bad_slot + bad_orient + fit_2h)
          and len(panel_strict._shown_candidates) == 0,
          "可见文本=%r" % panel_visible)

    # --- F5 --show-mismatch（严格关）时不丢、照旧显示 -------------------------
    panel_loose = ui_mod.Panel(engine, start_thread=False, strict_shape=False)
    panel_loose.set_status("监测中")
    ret_loose = panel_loose.show_candidates(empty_rows, slots=2, orientation="V",
                                            last_reason="已匹配；相似度不代表出货概率。")
    QCoreApplication.processEvents()
    shown_loose = [w.findChild(_QLabel, "cardname").text() if w.findChild(_QLabel, "cardname") else ""
                   for w in panel_loose._card_widgets]
    print("F5 --show-mismatch → 返回候选数:", len(ret_loose), "| 卡片名:", shown_loose)
    check("F5 --show-mismatch（strict_shape=False）时不丢：不匹配项照旧显示",
          len(ret_loose) == 2 and len(panel_loose._card_widgets) == 2
          and set(shown_loose) == set(bad_slot)
          and not panel_loose._shape_empty
          and panel_loose.waiting_label.text() == ui_mod.WAITING_TEXT,
          "卡片=%s waiting=%r" % (shown_loose, panel_loose.waiting_label.text()))

    # --- F6 被丢项只出现在日志、不在面板可见文本 ----------------------------
    # 用同一份真实数据跑一次严格路径，抓 stdout
    log_panel = ui_mod.Panel(engine, start_thread=False)
    log_panel.set_status("监测中")
    captured = io.StringIO()
    _old_stdout = sys.stdout
    try:
        sys.stdout = captured
        log_panel.show_candidates(shape_rows, slots=2, orientation="H",
                                  last_reason="已匹配；相似度不代表出货概率。")
    finally:
        sys.stdout = _old_stdout
    filter_lines = [ln for ln in captured.getvalue().splitlines() if ln.startswith("[filter]")]
    panel_text = "\n".join([log_panel.reason_label.text(), log_panel.status_label.text(),
                            log_panel.summary_label.text(), log_panel.waiting_label.text()]
                           + [w.findChild(_QLabel, "cardname").text()
                              for w in log_panel._card_widgets
                              if w.findChild(_QLabel, "cardname")])
    print("F6 [filter] 日志行    :")
    for _ln in filter_lines:
        print("      %s" % _ln)
    print("F6 面板可见文本       :", repr(panel_text.replace("\n", " / ")))
    check("F6 被丢 4 条只出现在日志、面板只剩相符的 3 条",
          len(filter_lines) == 1
          and filter_lines[0].startswith("[filter] 丢弃 4 条与 2格H 不符的候选：")
          and all(n in filter_lines[0] for n in bad_slot + bad_orient)
          and not any(n in panel_text for n in bad_slot + bad_orient)
          and set(c["name"] for c in log_panel._shown_candidates) == set(fit_2h),
          "日志=%s｜面板=%s" % (filter_lines, [c["name"] for c in log_panel._shown_candidates]))
    check("F6b [filter] 同样内容不重复打印（80ms 轮询不刷屏）",
          len([ln for ln in _capture_panel_stdout(
              lambda: log_panel.show_candidates(shape_rows, slots=2, orientation="H"))
              .splitlines() if ln.startswith("[filter]")]) == 0,
          "同样丢弃重复调用时的 [filter] 行数=0（去重生效）")
    check("F6c 严格过滤丢空时另外记一行『没有相符候选』",
          any(ln.startswith("[filter] 严格过滤后没有与 2格V 相符的候选")
              for ln in _capture_panel_stdout(
                  lambda: ui_mod.Panel(engine, start_thread=False).show_candidates(
                      empty_rows, slots=2, orientation="V")).splitlines()),
          "见 stdout")

    panel_strict.close()
    panel_loose.close()
    log_panel.close()

    # --- F7 真实 DB 审计：同音组按形状筛，谁被丢、还剩谁（确认不误伤真物品）-----
    sound_groups = {}
    for _row in engine.rows:
        _key = str(_row.get("sound_type") or "(无 sound_type)")
        sound_groups.setdefault(_key, []).append(_row)
    audit_bad = []
    for _slots, _orient in ((2, "V"), (2, "H")):
        _want_o = cand_mod.normalize_orientation(_orient)
        print("F7 真实同音组 × 形状 %d格%s（按 audio 组的 sound_type 归组，共 %d 组）："
              % (_slots, _orient, len(sound_groups)))
        for _key in sorted(sound_groups):
            _rows = sound_groups[_key]
            _keep_expect = [r["name"] for r in _rows
                            if int(r.get("slot") or 0) == _slots
                            and cand_mod.normalize_orientation(r.get("orientation")) == _want_o]
            _drop_expect = [r["name"] for r in _rows if r["name"] not in _keep_expect]
            _res = engine.resolve([{"key": _key, "kind": "normal", "score": 90,
                                    "items": _rows, "reason": ""}],
                                  _slots, _orient, strict_shape=True)
            _got = [c["name"] for c in _res]
            _stats = dict(engine.last_stats)
            if set(_got) != set(_keep_expect) or _stats["dropped_shape"] != len(_drop_expect):
                audit_bad.append("%s %d格%s kept=%s/%s dropped=%s"
                                 % (_key, _slots, _orient, _got, _keep_expect,
                                    _stats["dropped_names"]))
            if _got:      # 只打印"用户真会看到候选"的组，避免刷屏
                print("      %-10s 相符→%-22s 丢弃→%s"
                      % (_key, "、".join(_got) or "（无）",
                         "、".join(_stats["dropped_names"]) or "（无）"))
    print("F7 形状相符但被误丢 / 漏丢的组:", audit_bad if audit_bad else "0 处")
    check("F7 真实 DB：严格过滤结果 == 组内形状相符项（不误伤真物品、也不漏丢）",
          not audit_bad, "%d 组 × 2 形状全部核对" % len(sound_groups))

    # --- F8 --show-mismatch 接线 + engine is None 路径 ----------------------
    import main as main_mod

    args_default = main_mod.parse_args([])
    args_debug = main_mod.parse_args(["--show-mismatch"])
    main_src = io.open(os.path.join(BASE, "main.py"), encoding="utf-8").read()
    print("F8 --show-mismatch 默认:", args_default.show_mismatch,
          "| 打开时:", args_debug.show_mismatch)
    print("F8 main.py 建面板       :",
          [ln.strip() for ln in main_src.splitlines()
           if "ui.Panel(engine.candidate_engine" in ln
           or "strict_shape=not getattr" in ln])
    check("F8 --show-mismatch 默认关；main.py 用 strict_shape=not show_mismatch 建面板",
          args_default.show_mismatch is False and args_debug.show_mismatch is True
          and "strict_shape=not getattr(args, \"show_mismatch\", False)" in main_src
          and "ui.Panel(engine.candidate_engine" in main_src,
          "default=%s debug=%s" % (args_default.show_mismatch, args_debug.show_mismatch))
    check("F8b Panel 默认 strict_shape=True（玩家面向默认）",
          ui_mod.Panel.__init__.__defaults__[-1] is True
          and ui_mod.Panel(None, start_thread=False)._strict_shape is True,
          "签名默认值=%s" % (ui_mod.Panel.__init__.__defaults__,))
    none_panel = ui_mod.Panel(None, start_thread=False)   # 纯 UI 路径：无引擎 → 不做任何过滤
    none_rows = [{"name": "缓降器", "slot": 1, "orientation": "正", "score": 90},
                 {"name": "金质钢笔", "slot": 1, "orientation": "正", "score": 88}]
    none_ret = none_panel.show_candidates(none_rows, slots=2, orientation="V")
    QCoreApplication.processEvents()
    print("F8c engine is None → 返回 %d 条 / 卡片 %d 张（原行为：dict 直通、2/2）"
          % (len(none_ret), len(none_panel._card_widgets)))
    check("F8c engine is None 时行为不变（严格开关不影响纯 UI 路径：dict 直通、不过滤）",
          len(none_ret) == 2 and len(none_panel._card_widgets) == 2
          and [c["name"] for c in none_ret] == ["缓降器", "金质钢笔"]
          and not none_panel._shape_empty
          and none_panel.waiting_label.text() == ui_mod.WAITING_TEXT,
          "返回=%d 卡片=%d waiting=%r" % (len(none_ret), len(none_panel._card_widgets),
                                          none_panel.waiting_label.text()))
    none_panel.close()

    # ================================================ 8. 边界静态扫描
    line("8. 边界扫描：输入模拟 / 联网 / 内存读取（只扫 candidates.py 与 ui.py）")
    # 注意：这些禁词本身用拼接方式构造，避免自测脚本自己成为「命中源」。
    forbidden = [
        "Send" + "Input", "mouse" + "_event", "keybd" + "_event", "py" + "autogui",
        "win32" + "api", "SetCursor" + "Pos",
        "import " + "socket", "import " + "requests", "urllib" + ".request",
        "http" + ".client", "Open" + "Process",
        "ReadProcess" + "Memory", "WriteProcess" + "Memory",
    ]
    hits = []
    for path in (os.path.join(BASE, "candidates.py"), os.path.join(BASE, "ui.py")):
        with io.open(path, "r", encoding="utf-8") as fp:
            text = fp.read()
        for token in forbidden:
            if token in text:
                hits.append("%s: %s" % (os.path.basename(path), token))
    print("扫描文件            : candidates.py, ui.py")
    print("禁止项命中          :", hits if hits else "0 命中")
    check("无键鼠模拟/内存读取/联网代码", not hits, str(hits))
    print("candidates.py 行数  :", len(io.open(os.path.join(BASE, "candidates.py"), encoding="utf-8").readlines()))
    print("ui.py 行数          :", len(io.open(os.path.join(BASE, "ui.py"), encoding="utf-8").readlines()))
    cand_text = io.open(os.path.join(BASE, "candidates.py"), encoding="utf-8").read()
    gui_import = re.search(r"(?m)^\s*(?:import|from)\s+" + "PyQt" + r"5\b", cand_text)
    ui_import = re.search(r"(?m)^\s*(?:import|from)\s+" + "PyQt" + r"5\b",
                          io.open(os.path.join(BASE, "ui.py"), encoding="utf-8").read())
    print("candidates.py GUI import:", gui_import.group(0).strip() if gui_import else "无")
    print("ui.py GUI import        :", ui_import.group(0).strip() if ui_import else "无")
    check("candidates.py 是纯数据逻辑（无 GUI import）", gui_import is None)
    check("ui.py 确实使用 GUI 库", ui_import is not None)

    # ============================================================= 汇总
    line("汇总")
    failed = [n for n, ok in CHECKS if not ok]
    print("检查项: %d 项，通过 %d 项，失败 %d 项" % (len(CHECKS), len(CHECKS) - len(failed), len(failed)))
    if failed:
        for name in failed:
            print("  FAIL:", name)
    print("RESULT:", "ALL PASS" if not failed else "HAS FAILURES")


if __name__ == "__main__":
    main()