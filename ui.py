# -*- coding: utf-8 -*-
"""行商助手 · PyQt5 独立悬浮窗（只读展示）。

接口契约 §3.4：
    ``Panel(engine, start_thread=True)`` / ``show_candidates(rows, slots, orientation, last_reason, source="")``
    / ``set_status(text)`` / ``start()`` / ``close()`` / ``stop()`` / ``set_audio_source(...)``

窗口性质：
    * **独立悬浮窗**，不是游戏内覆盖层、不做任何注入；
    * 置顶 ``Qt.WindowStaysOnTopHint``、无边框 ``Qt.FramelessWindowHint``、``Qt.Tool``、
      ``Qt.WindowDoesNotAcceptFocus`` + ``WA_ShowWithoutActivating``：**永不抢焦点**
      （本文件刻意不主动激活窗口、不提升窗口层级、不设置键盘焦点）；
    * 半透明 ``setWindowOpacity(0.92)``，鼠标左键拖动移动窗口：优先 ``QWindow.startSystemMove()``
      交给系统移窗（Qt>=5.15；旧区域由系统失效，是 layered window 拖动残影的正解）；
      ``windowHandle()`` 为 None 或平台不支持时**回退**到手动 ``move()``，两条路径都不抛异常，
      并在移动/拖动开始结束时强制重绘（外观不变：仍 92% 半透明 + 8px 圆角）；
    * 默认停靠主屏右侧（``primaryScreen().availableGeometry()``）。

面向玩家的展示契约（task-14 定稿，**只显示四类信息**）：
    ① 详情行 ``格数 2格 · 朝向 V · 来源 边框``（只这三个字段）；
    ② 音频信息行 ``音频 48000Hz · <设备名> · 峰值 0.018``（三段缺一即省略，绝不编造）；
    ③ 匹配时间行 ``匹配于 3.2s 前``（年龄每次刷新重算；从未匹配则不显示）；
    ④ 候选卡片：缩略图 + 品质徽章 + 名称 + 品质/类别/格数 + 参考价值 + 大号彩色百分比数字
       （**没有进度条**；百分比颜色按概率高低：≥85 绿 / ≥70 黄 / ≥50 橙 / <50 灰）。
    底部状态栏只放**检测状态**（空闲=用户原话、监测中=等待拖动物品…、音频错误/无信号提示）。
    诊断信息（本次原因 ``last_reason``、触发来源 ``trigger_src``、格宽、锁定状态、[trace] 类
    文本、看到/受理计数、管理员提示）**一律只接收不显示**，继续由集成层写日志 ——
    ``show_candidates()`` 的签名保持不变，旧调用与探针不受影响。

边界（严格禁止）：不模拟键鼠、不自动点击、不自动购买、不读游戏内存、不联网。
本文件只做「画窗口 + 收鼠标左键拖动 + 展示引擎数据」，不含任何键盘/鼠标注入类调用。
署名：作者 烟火（标题行 + 窗口标题）。
"""

from __future__ import annotations

import os
import sys
import time

from PyQt5.QtCore import QRect, Qt
from PyQt5.QtGui import QColor, QPainter, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

__all__ = ["Panel", "run_ui", "MAX_CARDS"]

#: 一屏最多显示多少条候选（窗口 440x820；列表在 QScrollArea 里滚动，超过 20 条才截断）
MAX_CARDS = 20
THUMB_SIZE = 72
PANEL_WIDTH = 440
PANEL_HEIGHT = 820
SCORE_FONT_PX = 22             # 大号百分比数字（已删进度条：只留数字）
#: 百分比颜色按"概率高低"着色，**刻意与品质徽章色系区分**（别让人当成品质色）。
#: 注意：任务书建议的 #d9b64a / #9aa4b2 恰好等于「金色」徽章色与「未知品质」回落色，
#: 用它们会让大号数字和品质徽章撞色 → 这里改成同色系但明确不同的取值，并保持
#: 绿→黄→橙→灰的概率语义。
SCORE_COLOR_HIGH = "#3fd07a"   # ≥85 绿
SCORE_COLOR_MID = "#ffd24a"    # ≥70 黄（亮黄；与金色徽章 #d9b64a 的暗金明显不同）
SCORE_COLOR_LOW = "#ff9a3d"    # ≥50 橙
SCORE_COLOR_WEAK = "#7f8b9e"   # <50 灰（比未知品质回落色 #9aa4b2 更深）
QUALITY_STRIPE_WIDTH = 5
CARD_PADDING_V = 3          # 卡片内上下留白（左边条也用它避开 6px 圆角）
CARD_FRAME_PX = 1           # QFrame#card 的边框宽度
# 卡片最低高度：缩略图 + 上下留白 + 边框。
# 必须显式 setMinimumHeight，否则 QScrollArea(widgetResizable) 会在候选多时把卡片压缩到
# 比缩略图还矮（实测 72px 缩略图被压到 69px 卡片，子控件溢出卡片矩形）。
CARD_MIN_HEIGHT = THUMB_SIZE + 2 * CARD_PADDING_V + 2 * CARD_FRAME_PX
# 卡片内字号（px，任务 B：名称/参考价值/分数加大）；单点定义，自测从这些常量推导
CARD_NAME_FONT_PX = 15
CARD_META_FONT_PX = 12
CARD_VALUE_FONT_PX = 13
CARD_NOTE_FONT_PX = 12
MARGIN_RIGHT = 24
OPACITY = 0.92
WAITING_TEXT = "等待拖动物品…"
#: 严格形状过滤（task-16）把这一轮的声音候选全丢掉时的**状态文案**（不是诊断信息）。
#: ``{want}`` = "2格V" / "2格"（视觉没给朝向时只写格数）。
SHAPE_EMPTY_TEXT = "无与 {want} 相符的候选"
#: 空闲态检测状态（用户原话逐字，不要改写）
IDLE_STATUS_TEXT = "未开始监测：不读取游戏信息，点上方按钮或按 F8 开始。"
#: 监测中的检测状态
MONITORING_STATUS_TEXT = "监测中：等待拖动物品…"
#: 署名（玩家"烟火"）
AUTHOR_TEXT = "作者 烟火"
WINDOW_TITLE = "行商助手 · 候选（只读） · %s" % AUTHOR_TEXT
#: 标题行小按钮尺寸（标题行右侧的 — 与 ✕；不抢视线、不抢焦点）
TITLE_BUTTON_W = 24
TITLE_BUTTON_H = 20
#: 折叠（收起）后的最低高度：标题行 + 上下边距，保证标题与两个按钮完整可见、可点
COLLAPSED_MIN_HEIGHT = 34

QUALITY_COLORS = {"金色": "#d9b64a", "红色": "#e0635c", "紫色": "#a274d8"}
DEFAULT_QUALITY_COLOR = "#9aa4b2"


def score_color(score) -> str:
    """匹配概率 → 大号百分比数字的颜色（≥85 绿 / ≥70 黄 / ≥50 橙 / <50 灰）。

    刻意不用品质徽章那套颜色：这里表达的是"概率高低"，与物品品质无关。
    """
    try:
        value = int(score)
    except Exception:
        value = 0
    if value >= 85:
        return SCORE_COLOR_HIGH
    if value >= 70:
        return SCORE_COLOR_MID
    if value >= 50:
        return SCORE_COLOR_LOW
    return SCORE_COLOR_WEAK


def quality_color(quality) -> str:
    """品质名 -> 颜色；未知品质用 DEFAULT_QUALITY_COLOR。

    徽章填充色、左边条、cardmeta 文字色共用这一个取值，避免三处颜色漂移。
    """
    return QUALITY_COLORS.get(str(quality or "").strip(), DEFAULT_QUALITY_COLOR)


def badge_text_color(fill: str) -> str:
    """在填充色上挑对比文字色（亮底深字、暗底白字）。

    品质色里金色 (#d9b64a) 偏亮、红/紫偏暗，面板又是 92% 半透明压在深色底上，
    统一白字会让金色徽章发糊；按 Rec.601 相对亮度取阈值 150 挑。
    """
    try:
        text = str(fill).lstrip("#")
        r, g, b = int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except Exception:
        return "#ffffff"
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    return "#131720" if luminance >= 150 else "#ffffff"


def badge_style(fill: str) -> str:
    """品质徽章 QSS：填充色圆角胶囊 + 对比文字色。"""
    return (
        "QLabel#qualitybadge { background-color: %s; color: %s; border: none;"
        " border-radius: 9px; padding: 2px 10px; font-size: 12px; font-weight: bold; }"
        % (fill, badge_text_color(fill))
    )


def stripe_style(fill: str) -> str:
    """品质左边条 QSS：卡片左内边的一条品质色。"""
    return (
        "QFrame#qualitystripe { background-color: %s; border: none; border-radius: 2px; }" % fill
    )


_STYLESHEET_BASE = """
QWidget#panel { background-color: #16191f; border: 1px solid #333a45; border-radius: 8px; }
QLabel { color: #d8dee9; background: transparent; }
QLabel#title { color: #8fb8ff; font-weight: bold; font-size: 15px; }
QLabel#reason { color: #cfd6e4; }
QLabel#status { color: #8fb8ff; }
QLabel#summary { color: #7d8697; }
QLabel#hint { color: #616b7d; }
QLabel#waiting { color: #6d7789; font-size: 13px; }
QLabel#audio { color: #7d8697; font-size: 12px; }
QLabel#match { color: #8fb8ff; font-size: 12px; }
QPushButton#collapsebutton, QPushButton#closebutton {
    background-color: #262d38; color: #c8d0dc; border: 1px solid #3a4250;
    border-radius: 3px; font-size: 12px; padding: 0px; }
QPushButton#collapsebutton:hover { background-color: #333c4a; color: #ffffff; }
QPushButton#closebutton { color: #e8a0a0; }
QPushButton#closebutton:hover { background-color: #7a2d2d; color: #ffffff; border-color: #a34141; }
QFrame#card { background-color: #1e232b; border: 1px solid #2c333d; border-radius: 6px; }
QFrame#card:hover { border: 1px solid #3f4a5a; }
QComboBox { background-color: #232a34; color: #d8dee9; border: 1px solid #333a45;
            border-radius: 4px; padding: 2px 4px; }
QComboBox QAbstractItemView { background-color: #232a34; color: #d8dee9;
                              selection-background-color: #33405a; }
QPushButton { background-color: #232a34; color: #d8dee9; border: 1px solid #333a45;
              border-radius: 4px; padding: 3px 8px; }
QPushButton:hover { background-color: #2d3642; border: 1px solid #47536a; }
QPushButton:pressed { background-color: #1b2129; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: #1b2027; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #3a4453; border-radius: 5px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QTextEdit { background-color: #1b2027; color: #d8dee9; border: 1px solid #333a45; }
"""

# 卡片内字号单独生成（用 % 拼装：CSS 里全是花括号，不能用 str.format）
_CARD_STYLE = """
QLabel#cardname { color: #eef2f8; font-weight: bold; font-size: %(name)dpx; }
QLabel#cardmeta { color: #98a2b3; font-size: %(meta)dpx; }
QLabel#cardvalue { color: #7fd1a8; font-size: %(value)dpx; }
QLabel#cardnote { color: #e0a45c; font-size: %(note)dpx; }
QLabel#cardscore { color: #cfd6e4; font-weight: bold; font-size: %(score)dpx; }
QLabel#qualitybadge { background-color: %(badge)s; color: %(badgetext)s; border: none;
                      border-radius: 9px; padding: 2px 10px; font-size: 12px; font-weight: bold; }
QFrame#qualitystripe { background-color: %(badge)s; border: none; border-radius: 2px; }
""" % {
    "name": CARD_NAME_FONT_PX,
    "meta": CARD_META_FONT_PX,
    "value": CARD_VALUE_FONT_PX,
    "note": CARD_NOTE_FONT_PX,
    "score": SCORE_FONT_PX,
    "badge": DEFAULT_QUALITY_COLOR,
    "badgetext": badge_text_color(DEFAULT_QUALITY_COLOR),
}

STYLESHEET = _STYLESHEET_BASE + _CARD_STYLE


def _ensure_application():
    """返回 (QApplication, owns_app)：已存在则复用，不存在则在当前线程创建。"""
    app = QApplication.instance()
    if app is not None:
        return app, False
    app = QApplication(sys.argv[:1] if sys.argv else ["hangshang-assistant"])
    return app, True


class Panel(QWidget):
    """只读悬浮窗。所有数据来自 ``CandidateEngine``，窗口自身不做任何自动化动作。"""

    def __init__(self, engine, start_thread=True, strict_shape=True):
        """构造窗口。

        ``start_thread``：仅记录为 ``self.start_thread``。
        - ``True``（默认）：``start()`` 会在进入事件循环后做一次轻量首帧预热；
        - ``False``（main.py 集成模式）：**完全不创建线程、不创建定时器、不主动取数据**，
          由集成层自己的 80ms 轮询重复调用 ``show_candidates`` / ``set_status``。

        ``strict_shape``（**玩家面向默认 True**）：只显示与视觉判出的格数/朝向相符的候选，
        同音组里形状不符的一律不显示（只写日志）。调试可用 ``--show-mismatch`` 关掉，
        恢复"保留但排后"的旧行为（见 ``candidates.resolve``）。
        """
        # QWidget 必须在有 QApplication 的线程构造；无 GUI 环境（offscreen）下同样如此。
        self.app, self._owns_app = _ensure_application()
        super().__init__(None)

        self.engine = engine
        self.start_thread = bool(start_thread)
        self._strict_shape = bool(strict_shape)   # 只显示与格数/朝向相符的候选（玩家面向默认）
        self._shape_empty = False                 # 本轮严格过滤把声音候选全丢了
        self._shape_dropped = []                  # 本轮被丢候选（名字(格数朝向,分数)，只写日志）
        self._filter_log_sig = None               # [filter] 日志去重（同样的丢弃只打一行）

        self._all_candidates = []      # 当前一次 resolve 的完整候选
        self._shown_candidates = []    # 当前窗口里实际显示的候选
        self._status_text = ""
        self._last_reason = ""
        self._last_slots = 0
        self._last_orientation = ""
        self._last_source = ""
        self._last_trigger_src = ""    # 触发来源（鼠标上升沿/按住续触/松开/音频触发）
        self._last_signature = None
        self._history_signature = None
        # 粘滞显示：一次拖动会产生多个触发（按下/续触/松开），只有音效响的那一轮有候选，
        # 其余几轮 rows=[] —— 用户要的是"留着，直到下一次真正匹配到声音才换"。
        self._last_match_at = None     # 上次真正匹配到声音的 monotonic 时刻
        self._sticky = False           # 当前是否处于"保留上次结果"状态
        self._sticky_announced = False # 本次粘滞是否已打过 [sticky] 日志（只打一行）
        self._stopping = False
        self._closed = False
        self._card_widgets = []
        self._drag_offset = None
        self._drag_system_done = False   # 本次拖动是否已交给 startSystemMove()
        self._audio_samplerate = None
        self._audio_window_getter = None
        self._audio_device = None      # 真正在用的回环设备名（拿不到就 None，绝不编造）
        self._audio_peak = None        # 当前峰值（engine.audio.peak_rms 实时喂入）
        self._monitoring = False       # 是否处于"监测中"（决定底部状态栏默认文案）

        self._build_ui()
        self._connect_signals_once()
        self.set_status("")            # 初始：等待拖动物品…

    # ------------------------------------------------------------------ 构建
    def _build_ui(self):
        self.setObjectName("panel")
        self.setWindowTitle(WINDOW_TITLE)
        self.setWindowFlags(
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setWindowOpacity(OPACITY)
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedSize(PANEL_WIDTH, PANEL_HEIGHT)
        self.setStyleSheet(STYLESHEET)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        # --- 标题行：标题 + 「收起/展开」+「关闭并退出」 ---
        # 折叠后**只剩这一行**（无边框 Tool 窗口没有任务栏条目，真最小化会找不回来）。
        self.title_row = QHBoxLayout()
        self.title_row.setSpacing(4)
        self.title_label = QLabel("行商助手 · 候选（只读） · %s" % AUTHOR_TEXT)
        self.title_label.setObjectName("title")
        self.title_row.addWidget(self.title_label, 1)
        self.collapse_button = QPushButton("—")
        self.collapse_button.setObjectName("collapsebutton")
        self.collapse_button.setFixedSize(TITLE_BUTTON_W, TITLE_BUTTON_H)
        self.collapse_button.setFocusPolicy(Qt.NoFocus)
        self.collapse_button.setToolTip("收起 / 展开面板")
        self.title_row.addWidget(self.collapse_button)
        self.close_button = QPushButton("✕")
        self.close_button.setObjectName("closebutton")
        self.close_button.setFixedSize(TITLE_BUTTON_W, TITLE_BUTTON_H)
        self.close_button.setFocusPolicy(Qt.NoFocus)
        self.close_button.setToolTip("关闭并退出（会停止监测）")
        self.title_row.addWidget(self.close_button)
        root.addLayout(self.title_row)

        # --- 标题行以下的内容**全部**装进 self.body ---
        # 折叠 = body.setVisible(False) + 固定成标题行高度：只隐藏一个容器，
        # 以后往 body 里加控件也不会漏（不要逐个 hide 分散控件）。
        self.body = QWidget()
        self.body.setObjectName("body")
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(6)
        root.addWidget(self.body, 1)

        # --- 开始/停止监测（默认停止：不在你没玩的时候截屏/录音/识别其它应用）---
        self.start_button = QPushButton("▶ 开始监测（F8）")
        self.start_button.setObjectName("startbutton")
        self.start_button.setToolTip(
            "未开始监测时不读取游戏信息。点这里或按 F8 开始/停止。")
        body.addWidget(self.start_button)
        self._active_getter = None
        self._active_setter = None

        # 详情行：**只有 格数 / 朝向 / 来源** 三个字段
        # （原因/触发/格宽/锁定/追踪等诊断一律不进面板，只写日志）
        self.reason_label = QLabel("")
        self.reason_label.setObjectName("reason")
        self.reason_label.setWordWrap(True)
        body.addWidget(self.reason_label)

        # 音频信息行：当前声音状态（采样率 / 设备名 / 当前峰值），缺段自动省略
        self.audio_label = QLabel("")
        self.audio_label.setObjectName("audio")
        self.audio_label.setWordWrap(True)
        body.addWidget(self.audio_label)

        # 匹配时间行：多久前匹配到新声音（粘滞时靠这行体现新旧）
        self.match_label = QLabel("")
        self.match_label.setObjectName("match")
        body.addWidget(self.match_label)

        self.status_label = QLabel(IDLE_STATUS_TEXT)
        self.status_label.setObjectName("status")
        self.status_label.setWordWrap(True)
        body.addWidget(self.status_label)

        # --- 筛选控件（只建一次、只 connect 一次） ---
        filter_row = QHBoxLayout()
        filter_row.setSpacing(4)
        self.quality_combo = QComboBox()
        self.category_combo = QComboBox()
        self.orientation_combo = QComboBox()
        for combo in (self.quality_combo, self.category_combo, self.orientation_combo):
            combo.setMinimumWidth(66)
            combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        filter_row.addWidget(self.quality_combo)
        filter_row.addWidget(self.category_combo)
        filter_row.addWidget(self.orientation_combo)
        body.addLayout(filter_row)

        button_row = QHBoxLayout()
        button_row.setSpacing(4)
        self.summary_label = QLabel("候选 0 条")
        self.summary_label.setObjectName("summary")
        button_row.addWidget(self.summary_label, 1)
        self.clear_button = QPushButton("清空")
        self.history_button = QPushButton("历史")
        button_row.addWidget(self.clear_button)
        button_row.addWidget(self.history_button)
        body.addLayout(button_row)

        # --- 候选列表 ---
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.cards_host = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_host)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(6)
        self.cards_layout.setAlignment(Qt.AlignTop)
        self.waiting_label = QLabel(WAITING_TEXT)
        self.waiting_label.setObjectName("waiting")
        self.waiting_label.setAlignment(Qt.AlignCenter)
        self.cards_layout.addWidget(self.waiting_label)
        self.scroll_area.setWidget(self.cards_host)
        body.addWidget(self.scroll_area, 1)

        self.hint_label = QLabel("左键拖动移动窗口 · 仅提示，不自动操作")
        self.hint_label.setObjectName("hint")
        body.addWidget(self.hint_label)

        self.history_dialog = None
        self._collapsed = False
        self._place_right()
        self._rebuild_combos([], [], [])

    def _connect_signals_once(self):
        """所有信号只在这里连接一次，避免重复调用 show_candidates 时重复 connect。"""
        self.quality_combo.currentIndexChanged.connect(self._on_filter_changed)
        self.category_combo.currentIndexChanged.connect(self._on_filter_changed)
        self.orientation_combo.currentIndexChanged.connect(self._on_filter_changed)
        self.clear_button.clicked.connect(self._on_clear_clicked)
        self.history_button.clicked.connect(self._on_history_clicked)
        self.start_button.clicked.connect(self._on_start_clicked)
        self.collapse_button.clicked.connect(self.toggle_collapsed)
        self.close_button.clicked.connect(self.close)
        self.set_active_state(False)

    # ------------------------------------------------------ 收起 / 关闭按钮
    def _collapsed_height(self) -> int:
        """折叠后的高度：标题行 + 上下边距，保证标题与两个按钮完整可见、可点。"""
        try:
            self.title_row.activate()
        except Exception:
            pass
        row_h = 0
        try:
            row_h = int(self.title_row.sizeHint().height())
        except Exception:
            row_h = 0
        pad = 16
        try:
            margins = self.layout().contentsMargins()
            pad = int(margins.top()) + int(margins.bottom())
        except Exception:
            pass
        return max(COLLAPSED_MIN_HEIGHT, row_h + pad)

    def is_collapsed(self) -> bool:
        """当前是否处于收起（折叠）态。"""
        return bool(getattr(self, "_collapsed", False))

    def set_collapsed(self, collapsed: bool):
        """收起 / 展开面板（幂等、可反复）。

        **刻意不用** ``showMinimized()`` / ``hide()``：本窗口是无边框 ``Qt.Tool``，
        没有任务栏条目，真最小化在游戏里就找不回来了。收起 = 只隐藏 ``self.body``
        一个容器 + 固定成标题行高度，标题与两个按钮仍在原位、可点，再点一次即展开。

        收起/展开都会改变窗口尺寸，``screen_rect()`` 读的是 ``frameGeometry()``，
        因此集成层（每轮 80ms 刷新 panel_rect）自动跟随，不会出现"面板已收起但仍按
        原尺寸屏蔽"的问题。
        """
        collapsed = bool(collapsed)
        self._collapsed = collapsed
        try:
            if collapsed:
                self.body.setVisible(False)
                self.setFixedSize(PANEL_WIDTH, self._collapsed_height())
                self.collapse_button.setText("+")
                self.collapse_button.setToolTip("展开面板")
            else:
                self.body.setVisible(True)
                self.setFixedSize(PANEL_WIDTH, PANEL_HEIGHT)
                self.collapse_button.setText("—")
                self.collapse_button.setToolTip("收起 / 展开面板")
                # 折叠期间可能刚加过候选卡片：展开后内容重新可见，滚动范围要重算
                self._refresh_scroll_extent()
            self._clamp_frame_inside()
        except Exception:
            pass
        return self.is_collapsed()

    def toggle_collapsed(self):
        """标题行 ``—`` / ``+``：收起 ↔ 展开。"""
        return self.set_collapsed(not self.is_collapsed())

    def _clamp_frame_inside(self):
        """尺寸变化后若窗口超出可用屏幕，把它拉回屏内（只处理越界的边，不主动挪位）。"""
        try:
            geo = self._available_geometry()
            frame = self.frameGeometry()
            x, y = int(frame.x()), int(frame.y())
            max_x = max(geo.left(), geo.right() - int(frame.width()) + 1)
            max_y = max(geo.top(), geo.bottom() - int(frame.height()) + 1)
            new_x = max(geo.left(), min(x, max_x))
            new_y = max(geo.top(), min(y, max_y))
            if new_x != x or new_y != y:
                self.move(new_x, new_y)
        except Exception:
            pass

    # ------------------------------------------------------------ 窗口位置
    def _available_geometry(self):
        screen = QApplication.primaryScreen()
        if screen is None:
            return QRect(0, 0, 1920, 1080)
        geometry = screen.availableGeometry()
        if geometry.isEmpty():
            return QRect(0, 0, 1920, 1080)
        return geometry

    def _place_right(self):
        """默认停靠主屏右侧：x = right - width - 24。"""
        geometry = self._available_geometry()
        x = geometry.right() - self.width() - MARGIN_RIGHT
        x = max(geometry.left(), min(x, max(geometry.left(), geometry.right() - self.width())))
        y = geometry.top() + 60
        y = max(geometry.top(), min(y, max(geometry.top(), geometry.bottom() - self.height())))
        self.move(x, y)
        return self.pos()

    def screen_rect(self):
        """面板顶层窗口在**屏幕物理像素**坐标系中的矩形 ``(x, y, w, h)``；拿不到返回 ``None``。

        坐标系：与 ``GetCursorPos()`` / ``GetSystemMetrics(SM_XVIRTUALSCREEN=76,
        SM_YVIRTUALSCREEN=77)`` 完全一致（主屏左上角 = (0,0)，主屏左侧的副屏为负），
        也就是 ``ImageGrab.grab(all_screens=True)`` 的像素坐标。集成层据此把面板矩形
        换算成「某张截图内的像素坐标」后交给视觉做屏蔽（``ignore_rects``），并用来
        跳过「落在面板内」的鼠标触发。

        实现：``frameGeometry()``（含边框的顶层窗口矩形）+ 该屏 ``devicePixelRatio()``。
        已知局限：Qt 全局坐标是**逻辑像素**，这里以「主屏左上角 = (0,0)」为锚点按本屏
        DPR 缩放到物理像素；所有屏幕同一缩放（含 100%）时与系统坐标完全一致，混合 DPI
        的多屏布局下可能有几十像素误差 —— 只影响屏蔽范围的大小，不影响物品判定本身。

        本方法**只读几何**：不抓屏、不移动窗口、不创建定时器、不抢焦点。
        """
        try:
            frame = self.frameGeometry()
            w, h = int(frame.width()), int(frame.height())
            if w <= 0 or h <= 0:
                return None
            x, y = int(frame.x()), int(frame.y())
            dpr = 1.0
            try:
                screen = self.screen() or QApplication.primaryScreen()
                if screen is not None:
                    dpr = float(screen.devicePixelRatio() or 1.0)
            except Exception:
                dpr = 1.0
            if abs(dpr - 1.0) > 1e-6:
                x, y = int(round(x * dpr)), int(round(y * dpr))
                w, h = int(round(w * dpr)), int(round(h * dpr))
            return (x, y, w, h)
        except Exception:
            return None

    # ------------------------------------------------------- 状态行（幂等）
    def bind_active(self, getter, setter) -> None:
        """把「开始监测」按钮接到集成层的开关上。

        getter() -> bool：当前是否在监测；setter(bool) -> bool：切换监测。
        面板自身仍然只读：它只切换"要不要采样"，不做任何游戏内操作。
        """
        self._active_getter = getter
        self._active_setter = setter
        self.set_active_state(bool(getter()))

    def set_active_state(self, active: bool) -> None:
        """刷新按钮外观与检测状态文案（集成层切换状态后调用，或由轮询调用）。

        底部状态栏**只放检测状态**：空闲用用户原话（``IDLE_STATUS_TEXT``），监测中用
        ``MONITORING_STATUS_TEXT``。锁定/触发/看到受理计数等诊断一律不进这一栏。
        """
        try:
            self._monitoring = bool(active)
            if active:
                self.start_button.setText("■ 停止监测（F8）")
                self.start_button.setStyleSheet(
                    "QPushButton#startbutton { background-color:#2f5d3a; color:#eaf7ee;"
                    " border:1px solid #4c8a5f; font-weight:bold; padding:5px; }")
                self._status_text = MONITORING_STATUS_TEXT
            else:
                self.start_button.setText("▶ 开始监测（F8）")
                self.start_button.setStyleSheet(
                    "QPushButton#startbutton { background-color:#5d3a2f; color:#f7eee9;"
                    " border:1px solid #8a5f4c; font-weight:bold; padding:5px; }")
                self._status_text = IDLE_STATUS_TEXT
            self._update_labels()
        except Exception:
            pass

    def _on_start_clicked(self) -> None:
        try:
            if self._active_setter is None or self._active_getter is None:
                return
            new_state = bool(self._active_setter(not bool(self._active_getter())))
            self.set_active_state(new_state)
        except Exception:
            pass

    def set_status(self, text):
        """更新底部检测状态栏（集成层每轮 80ms 重设）。可反复调用。

        传空文本时按当前是否监测回落默认文案（空闲=用户原话 / 监测中=等待拖动物品…）。
        **只喂检测状态**：锁定/触发/看到受理计数等诊断不许塞进来（它们只写日志）。
        """
        self._status_text = "" if text is None else str(text)
        self._update_labels()

    def set_audio_source(self, samplerate=None, window_getter=None, device=None, peak=None):
        """记录"当前声音状态"，供面板音频信息行显示：``音频 48000Hz · <设备名> · 峰值 0.018``。

        * 三段都是**可选**的：拿不到的段请传 ``None``，面板自动省略，**绝不编造**
          （例如设备名解析失败时不要传 ``"(系统默认设备)"`` 这类占位）；
        * ``device`` 请给集成层**真正在用**的那个设备名（自动选择时的实际设备）；
        * ``peak`` 是当前峰值（``engine.audio.peak_rms``），每次刷新实时喂入；
        * **不创建定时器、不主动取数据**（集成层自己驱动刷新），offscreen / 无音频设备下安全；
        * 旧调用 ``set_audio_source(samplerate, window_getter)`` 仍然可用（后两参可选）。
        """
        self._audio_samplerate = samplerate
        self._audio_window_getter = window_getter
        self._audio_device = device
        self._audio_peak = peak
        self._update_labels()

    def _audio_line(self) -> str:
        """音频信息行文本；缺段直接省略（返回空串时该行整体隐藏）。"""
        parts = []
        if self._audio_samplerate:
            try:
                parts.append("音频 %dHz" % int(self._audio_samplerate))
            except Exception:
                parts.append("音频 %sHz" % self._audio_samplerate)
        if self._audio_device:
            parts.append(str(self._audio_device))
        if self._audio_peak is not None:
            try:
                parts.append("峰值 %.3f" % float(self._audio_peak))
            except Exception:
                pass
        return " · ".join(parts)

    def _match_line(self) -> str:
        """匹配时间行：``匹配于 3.2s 前``（年龄**每次刷新按当前时间重算**）。

        从未匹配过 → 返回空串（该行整体隐藏）。粘滞期间列表保留，就靠这一行体现新旧。
        """
        if self._last_match_at is None:
            return ""
        return "匹配于 %.1fs 前" % self._sticky_age()

    def _update_labels(self):
        """刷新面板文本。

        **面板只显示四类信息**：详情行（格数/朝向/来源）、音频信息行、匹配时间行、候选卡片。
        本次原因（``last_reason``）、触发来源（``trigger_src``）、格宽、锁定状态、[trace]
        类文本 —— 一律只接收不显示（诊断继续由集成层写日志）。
        """
        # ① 底部检测状态栏
        text = self._status_text.strip()
        if not text:
            text = MONITORING_STATUS_TEXT if self._monitoring else IDLE_STATUS_TEXT
        self.status_label.setText(text)

        # ② 详情行：只有 格数 / 朝向 / 来源（视觉没判出格数时写 ?格，仍照常显示）
        slots_text = "?格" if not self._last_slots else "%d格" % self._last_slots
        self.reason_label.setText(
            "格数 %s · 朝向 %s · 来源 %s"
            % (slots_text, self._last_orientation or "-", self._last_source or "-"))

        # ③ 音频信息行（缺段省略）
        audio = self._audio_line()
        self.audio_label.setText(audio)
        self.audio_label.setVisible(bool(audio))

        # ④ 匹配时间行（每次刷新重算年龄；从未匹配过则隐藏）
        matched = self._match_line()
        self.match_label.setText(matched)
        self.match_label.setVisible(bool(matched))

        self.waiting_label.setVisible(not self._shown_candidates)

    # ------------------------------------------------ 粘滞显示（保留上次结果）
    def _sticky_age(self) -> float:
        """距上次真正匹配到声音的秒数（每次调用都按当前时间重算）。"""
        if self._last_match_at is None:
            return 0.0
        try:
            return max(0.0, time.monotonic() - float(self._last_match_at))
        except Exception:
            return 0.0

    def _reset_sticky(self):
        """清掉粘滞状态与匹配时刻。

        「清空」按钮必须走这里：否则清空后下一个 ``rows=[]`` 会被当成"还有候选要保留"，
        把旧卡片复活；残留的 ``_last_match_at`` 还会让年龄从很旧的时刻算起。
        """
        self._sticky = False
        self._sticky_announced = False
        self._last_match_at = None

    # -------------------------------------------------- 候选展示（可重复调用）
    def show_candidates(self, match_rows, slots=0, orientation="", last_reason="", source="",
                        trigger_src=""):
        """展开并显示候选。可被同一 Qt 线程以高频（如 80ms）反复调用。

        ``source``：视觉格数的来源（"红块"/"边框"/""），只用于展示。
        ``trigger_src``：触发来源（"鼠标上升沿"/"按住续触"/"松开"/"音频触发"），只用于展示；
        不传则这一行与历史输出逐字一致（幂等性不受影响）。
        视觉失败（slots=0）时**照样显示音频候选**，格数显示为 ``?格``。

        粘滞显示（用户语义："拖动完不切换就自己消失"是缺陷）：
        一次拖动会产生多个触发（按下沿 / 按住续触每 0.9s / 松开），只有音效响的那一轮有候选，
        其余几轮 ``rows=[]``。因此：
        * ``match_rows`` 非空 → 照旧重建卡片，记下 ``_last_match_at``，结束粘滞；
        * ``match_rows`` 为空但面板上还有候选 → **不重建、不清空**，只更新格数/朝向/来源/触发
          来源，原因行改成"本次未匹配到声音 · 以下为 N.Ns 前的结果"（年龄每次刷新重算）；
        * ``match_rows`` 为空且从未有过候选 → 维持现状（等待文案）。
        「清空」按钮会同时清掉粘滞状态（见 ``_reset_sticky``），清空后不会复活旧卡片。

        幂等保证：
        * 候选集合（名称+分数）与 slots/orientation 未变化时**不重建任何 widget**，只刷新文本；
        * 发生变化时先彻底清空上一次的卡片（``setParent(None)`` + ``deleteLater``），
          最多显示 ``MAX_CARDS`` 条；
        * 粘滞分支直接返回，重复 ``rows=[]`` 不会重复插卡、不会累积控件；
        * 历史只在结果真正变化时追加一次，避免 80ms 轮询把历史刷爆。
        """
        if self._closed:
            return []

        self._last_slots = int(slots or 0)
        self._last_orientation = str(orientation or "")
        self._last_reason = "" if last_reason is None else str(last_reason)
        self._last_source = "" if source is None else str(source)
        self._last_trigger_src = "" if trigger_src is None else str(trigger_src)

        if self.engine is None:
            candidates = [c for c in (match_rows or []) if isinstance(c, dict)]
            stats = {}
        else:
            candidates = self.engine.resolve(match_rows or [], self._last_slots,
                                             self._last_orientation,
                                             strict_shape=self._strict_shape)
            stats = dict(getattr(self.engine, "last_stats", None) or {})
        # 严格形状过滤（task-16）：被丢候选**只写日志**，永不进面板
        self._shape_dropped = list(stats.get("dropped_names") or [])
        self._shape_empty = bool(stats.get("empty_after_shape"))
        self._log_shape_filter(stats)

        if not candidates and self._all_candidates and not self._shape_empty:
            # ---- 粘滞：本次没匹配到声音，面板上还留着上次的结果 → 原样保留 ----
            self._sticky = True
            if not self._sticky_announced:
                # 只打一行（80ms 轮询会把这里反复调到，不能刷屏）；年龄每次重算
                self._sticky_announced = True
                try:
                    sys.stdout.write("[sticky] 本次未匹配到声音：保留 %d 张上次候选（%.1fs 前）\n"
                                     % (len(self._card_widgets) or len(self._all_candidates),
                                        self._sticky_age()))
                    sys.stdout.flush()
                except Exception:
                    pass
            self._update_labels()
            return list(self._all_candidates)

        if self._shape_empty:
            # 这一轮**听到了声音**，但它的候选没有一条符合视觉判出的格数/朝向：
            # 绝不显示不匹配项（这正是用户看到的"格数 2格V 里混着 1格(H)"）。
            keep = [c for c in self._all_candidates if self._shape_ok(c)]
            if keep:
                # 面板上原有候选本身就符合当前格数/朝向 → 保留它们（用户正在看的结果不该消失）；
                # 过滤细节只写日志，面板不加诊断文字。
                self._all_candidates = keep
                self._last_signature = (
                    self._last_slots, self._last_orientation,
                    tuple((c.get("name"), c.get("score")) for c in keep),
                )
                try:
                    sys.stdout.write("[filter] 本轮候选与 %s 不符（丢弃 %d 条）→ 保留上次 %d 张相符候选\n"
                                     % (self._shape_want_text(), len(self._shape_dropped), len(keep)))
                    sys.stdout.flush()
                except Exception:
                    pass
                self._apply_filter()
                return []
            # 面板上也没有相符的候选 → 清空卡片 + 明确状态文案（等待区那一行）
            self._all_candidates = []
            self._last_signature = (self._last_slots, self._last_orientation, ())
            self._rebuild_combos([], [], [])
            self._apply_filter()
            return []

        if candidates:
            # 真正匹配到声音：正常替换，粘滞结束（下一轮若又是空行会重新打日志）
            self._last_match_at = time.monotonic()
            self._sticky = False
            self._sticky_announced = False

        signature = (
            self._last_slots,
            self._last_orientation,
            tuple((c.get("name"), c.get("score")) for c in candidates),
        )

        if signature != self._last_signature:
            self._last_signature = signature
            self._all_candidates = candidates
            if candidates:
                if self.engine is not None and signature != self._history_signature:
                    self._history_signature = signature
                    try:
                        self.engine.history_add(candidates, {"slots": self._last_slots})
                    except Exception:
                        pass
            self._rebuild_combos(
                self._values(candidates, "quality"),
                self._values(candidates, "category"),
                self._values(candidates, "orientation"),
            )
            self._apply_filter()
        else:
            self._all_candidates = candidates

        self._update_labels()
        return candidates

    def _values(self, candidates, field):
        if self.engine is not None:
            try:
                return self.engine.distinct(candidates, field)
            except Exception:
                pass
        values = []
        for candidate in candidates:
            value = str(candidate.get(field) or "").strip()
            if value and value not in values:
                values.append(value)
        return values

    # ------------------------------------------------ 严格形状过滤（task-16）
    def _shape_want_text(self) -> str:
        """当前期望形状的短文案：``2格V`` / ``2格``（无朝向时不补）。"""
        return "%d格%s" % (int(self._last_slots or 0), str(self._last_orientation or ""))

    def _shape_ok(self, candidate) -> bool:
        """候选是否符合**当前**格数/朝向（严格过滤的同一判据，供粘滞沿用）。

        只用 ``resolve`` 给出的 ``slot`` / ``orientation_code`` 比对，**不重新解释**数据。
        """
        if not isinstance(candidate, dict):
            return False
        want_slots = int(self._last_slots or 0)
        if want_slots and int(candidate.get("slot") or 0) != want_slots:
            return False
        want_orient = str(self._last_orientation or "").strip()
        if want_orient and str(candidate.get("orientation_code") or "") != want_orient:
            return False
        return True

    def _log_shape_filter(self, stats) -> None:
        """把被丢弃的候选写进日志（面板不显示）。同样的丢弃只打一行，避免 80ms 刷屏。"""
        dropped = list(stats.get("dropped_names") or [])
        if not dropped:
            return
        signature = (int(self._last_slots or 0), str(self._last_orientation or ""),
                     tuple(dropped))
        if signature == self._filter_log_sig:
            return
        self._filter_log_sig = signature
        try:
            sys.stdout.write(
                "[filter] 丢弃 %d 条与 %s 不符的候选：%s\n"
                % (len(dropped), self._shape_want_text(), " ".join(dropped)))
            if stats.get("empty_after_shape"):
                sys.stdout.write(
                    "[filter] 严格过滤后没有与 %s 相符的候选（不显示不匹配项）\n"
                    % self._shape_want_text())
            sys.stdout.flush()
        except Exception:
            pass

    def _waiting_text(self) -> str:
        """等待区文案：严格过滤丢空时给出明确状态，其余情况仍是原「等待拖动物品…」。"""
        if self._shape_empty and not self._shown_candidates:
            return SHAPE_EMPTY_TEXT.format(want=self._shape_want_text())
        return WAITING_TEXT

    def _rebuild_combos(self, qualities, categories, orientations):
        """在 blockSignals 保护下刷新下拉项，保留原选择（幂等、不重复 connect）。"""
        pairs = (
            (self.quality_combo, qualities),
            (self.category_combo, categories),
            (self.orientation_combo, orientations),
        )
        for combo, values in pairs:
            current = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("全部")
            for value in values:
                combo.addItem(value)
            index = combo.findText(current)
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.blockSignals(False)

    def _apply_filter(self):
        """按当前下拉选择筛选并重建卡片。"""
        if self.engine is not None:
            try:
                filtered = self.engine.filter(
                    self._all_candidates,
                    quality=self._selected(self.quality_combo),
                    category=self._selected(self.category_combo),
                    orientation=self._selected(self.orientation_combo),
                )
            except Exception:
                filtered = list(self._all_candidates)
        else:
            filtered = list(self._all_candidates)

        self._shown_candidates = filtered
        self._rebuild_cards(filtered[:MAX_CARDS])
        self.summary_label.setText(
            "候选 %d 条 / 显示 %d 条" % (len(self._all_candidates), len(filtered[:MAX_CARDS]))
        )
        self.waiting_label.setText(self._waiting_text())
        self.waiting_label.setVisible(not filtered)
        self._update_labels()

    @staticmethod
    def _selected(combo):
        text = combo.currentText()
        return "" if text in ("", "全部") else text

    def _on_filter_changed(self, *_args):
        self._apply_filter()

    def _on_clear_clicked(self, *_args):
        """清空当前展示（历史保留）。

        **必须同时清掉粘滞状态与 `_last_match_at`**：否则清空之后紧接着到来的 `rows=[]`
        会被粘滞分支当成"面板上还有候选"而把旧卡片复活（`drop_sticky=True`）。
        """
        self._all_candidates = []
        self._shown_candidates = []
        self._last_signature = None
        self._shape_empty = False          # 用户主动清空 → 等待文案回到「等待拖动物品…」
        self._shape_dropped = []
        self._filter_log_sig = None
        self._rebuild_combos([], [], [])
        self._rebuild_cards([], drop_sticky=True)
        self.summary_label.setText("候选 0 条")
        self.waiting_label.setText(WAITING_TEXT)
        self._status_text = ""
        self._update_labels()

    # --------------------------------------------------------------- 卡片
    def _clear_cards(self, drop_sticky=False):
        """清掉卡片 widget（每次重建都会调用）。

        ``drop_sticky=True`` 时才连带清掉粘滞状态：普通重建若也清，`_last_match_at` 会被
        每次重建抹掉，粘滞年龄就永远算不对。只有"真的把面板清空且不马上重建"的路径
        （清空按钮）才该这么调。
        """
        for widget in self._card_widgets:
            try:
                widget.setParent(None)
                widget.deleteLater()
            except RuntimeError:
                pass
        self._card_widgets = []
        # waiting_label 常驻在布局里，清理时不动它
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            widget = item.widget()
            if widget is not None and widget is not self.waiting_label:
                widget.setParent(None)
                widget.deleteLater()
        if drop_sticky:
            self._reset_sticky()

    def _rebuild_cards(self, candidates, drop_sticky=False):
        self._clear_cards(drop_sticky=drop_sticky)
        for candidate in candidates:
            card = self._make_card(candidate)
            self._card_widgets.append(card)
            self.cards_layout.addWidget(card)
        if not candidates:
            self.cards_layout.addWidget(self.waiting_label)
        self.waiting_label.setVisible(not candidates)
        self._refresh_scroll_extent()
        # 布局/可见性要等事件循环转一圈才稳定（新建卡片在布局里暂时算"空"），
        # 再补一次延后刷新，确保滚动范围最终正确。QTimer.singleShot 用函数式重载，
        # **不**以本窗口为 receiver，所以不会给 Panel 添 QTimer 子对象（main.py 独占轮询定时器）。
        try:
            from PyQt5.QtCore import QTimer

            QTimer.singleShot(0, self._refresh_scroll_extent)
        except Exception:
            pass

    def _content_height(self) -> int:
        """卡片区内容高度（像素）——不依赖布局此刻是否已"看见"新卡片。

        新建后立刻取 ``layout.sizeHint()`` 可能得到 0（布局里暂时算空），所以这里用
        每张卡片的 sizeHint 与显式下限 CARD_MIN_HEIGHT 取大，再加间距/边距兜底。
        """
        cards = self._card_widgets
        if not cards:
            return max(0, self.waiting_label.sizeHint().height())
        margins = self.cards_layout.contentsMargins()
        spacing = self.cards_layout.spacing()
        total = margins.top() + margins.bottom()
        total += sum(max(card.sizeHint().height(), card.minimumHeight()) for card in cards)
        total += spacing * max(0, len(cards) - 1)
        return int(total)

    def _refresh_scroll_extent(self):
        """候选数量变化后让 QScrollArea 正确重算滚动范围。

        踩过的坑（实测）：``widgetResizable`` 下这个滚动 widget 的尺寸由 QScrollArea 决定，
        而它只在"widget 收到 Resize"时重算滚动条。当**内容从一屏内涨到超出一屏**、而滚动条
        此刻正好是隐藏的，QScrollArea 会用"没有滚动条时的最大视口"把 widget 压回视口高度，
        范围算成 0 → 滚动条永远不出现 → 底部卡片既看不到也滚不到
        （实测 20 张卡片：host=580 / sizeHint=1714 / scrollBar.maximum()=0）。

        这里先判内容是否超出一屏：超出就**临时强制显示滚动条**走 QScrollArea 的另一条分支
        （此时 maximumViewportSize() != viewport()->size()），让 host 按内容撑开、范围被算出来，
        再还原 AsNeeded —— 此时范围已 > 0，滚动条会保持可见。内容变少时 adjustSize() 收回去。
        异常一律吞掉（UI 只读容错）。
        """
        try:
            host = self.cards_host
            area = self.scroll_area
            layout = host.layout()
            if layout is not None:
                layout.activate()
            viewport_h = max(1, area.viewport().height())
            content_h = max(host.sizeHint().height(), self._content_height())
            if content_h > viewport_h + 1:
                area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
                host.adjustSize()
                area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            else:
                host.adjustSize()
            area.updateGeometry()
        except Exception:
            pass

    def _make_card(self, candidate):
        card = QFrame()
        card.setObjectName("card")
        card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        card.setMinimumHeight(CARD_MIN_HEIGHT)

        row = QHBoxLayout(card)
        # 品质左边条贴住卡片左内边（上下留 CARD_PADDING_V 避开 6px 圆角），右侧留 8px
        row.setContentsMargins(0, CARD_PADDING_V, 8, CARD_PADDING_V)
        row.setSpacing(8)

        quality = str(candidate.get("quality") or "-")
        color = quality_color(quality)

        stripe = QFrame()
        stripe.setObjectName("qualitystripe")
        stripe.setFixedWidth(QUALITY_STRIPE_WIDTH)
        stripe.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        stripe.setStyleSheet(stripe_style(color))
        stripe.setToolTip("品质色：%s" % quality)
        row.addWidget(stripe)

        thumb = QLabel()
        thumb.setObjectName("cardthumb")
        thumb.setFixedSize(THUMB_SIZE, THUMB_SIZE)
        thumb.setPixmap(self._thumb_pixmap(candidate))
        row.addWidget(thumb)

        left = QVBoxLayout()
        left.setSpacing(3)
        name_label = QLabel(str(candidate.get("name") or ""))
        name_label.setObjectName("cardname")

        # 填充色圆角品质徽章（颜色取 QUALITY_COLORS，未知品质用默认色）
        badge = QLabel(quality)
        badge.setObjectName("qualitybadge")
        badge.setAlignment(Qt.AlignCenter)
        badge.setStyleSheet(badge_style(color))
        badge.setToolTip("品质：%s" % quality)

        name_row = QHBoxLayout()
        name_row.setSpacing(6)
        name_row.addWidget(name_label, 0)
        name_row.addWidget(badge, 0)
        name_row.addStretch(1)
        left.addLayout(name_row)

        category = str(candidate.get("category") or "-")
        meta_label = QLabel(
            "%s · %s · %s格%s"
            % (
                quality,
                category,
                candidate.get("slot", 0),
                ("（%s）" % candidate.get("orientation")) if candidate.get("orientation") else "",
            )
        )
        meta_label.setObjectName("cardmeta")
        meta_label.setStyleSheet("color: %s;" % color)
        meta_label.setWordWrap(True)
        left.addWidget(meta_label)

        value_text = candidate.get("reference_value_text")
        if not value_text and candidate.get("reference_value") is not None:
            value_text = str(candidate["reference_value"])
        value_label = QLabel("参考价值 %s" % (value_text or "未知"))
        value_label.setObjectName("cardvalue")
        left.addWidget(value_label)

        note = str(candidate.get("note") or "").strip()
        if note:
            note_label = QLabel(note)
            note_label.setObjectName("cardnote")
            note_label.setWordWrap(True)
            left.addWidget(note_label)

        row.addLayout(left, 1)

        right = QVBoxLayout()
        right.setSpacing(2)
        right.setAlignment(Qt.AlignTop)
        score = int(candidate.get("score") or 0)
        # 大号彩色百分比：**没有进度条**，只有数字；颜色表达"概率高低"（与品质色系区分）
        score_label = QLabel("%d%%" % score)
        score_label.setObjectName("cardscore")
        score_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        score_label.setStyleSheet(
            "QLabel#cardscore { color: %s; font-size: %dpx; font-weight: bold; }"
            % (score_color(score), SCORE_FONT_PX))
        score_label.setToolTip("匹配概率（与品质无关）")
        right.addWidget(score_label)
        row.addLayout(right)
        return card

    def _thumb_pixmap(self, candidate):
        path = str(candidate.get("image_path") or "")
        pixmap = QPixmap(path) if path and os.path.exists(path) else QPixmap()
        if pixmap.isNull():
            pixmap = QPixmap(THUMB_SIZE, THUMB_SIZE)
            pixmap.fill(QColor("#2a313b"))
            painter = QPainter(pixmap)
            painter.setPen(QColor("#6d7789"))
            painter.drawText(pixmap.rect(), Qt.AlignCenter, "?")
            painter.end()
            return pixmap
        return pixmap.scaled(
            THUMB_SIZE, THUMB_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )

    # --------------------------------------------------------------- 历史
    def _on_history_clicked(self, *_args):
        text = "（暂无历史）"
        if self.engine is not None:
            try:
                text = self.engine.history_text()
            except Exception as exc:
                text = "历史读取失败: %s" % exc
        if self.history_dialog is None:
            self.history_dialog = QDialog(self)
            self.history_dialog.setWindowTitle("候选历史（本次会话）")
            self.history_dialog.resize(360, 260)
            dialog_layout = QVBoxLayout(self.history_dialog)
            self.history_view = QTextEdit(self.history_dialog)
            self.history_view.setReadOnly(True)
            dialog_layout.addWidget(self.history_view)
        self.history_view.setPlainText(text)
        self.history_dialog.show()  # 非模态，不阻塞、不抢事件循环

    # ------------------------------------------------------------ 拖动 / 事件
    def _force_repaint(self):
        """拖动期间强制重绘。

        ``setWindowOpacity()`` 在 Windows 上走 layered window，窗口被移动后**旧位置不会
        自动失效** → 实测症状是拖动后面板留残影（代码只创建一个 Panel，不是两扇窗）。
        先 ``update()`` 排一次重绘，再 ``repaint()`` 立刻兑现一次；任何异常都不许冒泡
        （offscreen / 隐藏窗口 / 正在析构时都必须安全）。
        """
        try:
            self.update()
        except Exception:
            pass
        try:
            self.repaint()
        except Exception:
            pass

    def _try_start_system_move(self) -> bool:
        """把本次拖动交给系统：``QWindow.startSystemMove()``（Qt>=5.15）。

        系统移窗时旧区域由 Windows 自己失效，是 layered window 残影的正解。
        返回 ``True`` = 已交给系统；以下情况一律返回 ``False``，由调用方回退手动 ``move()``：
          * ``windowHandle()`` 为 ``None``（offscreen / 窗口尚未 native）；
          * 该平台没有 ``startSystemMove``（旧 Qt 或未实现，offscreen 平台返回 False）；
          * 调用抛异常。

        刻意**不缓存失败**：某些平台会先返回 False（窗口还没 native）随后成功，
        缓存 False 会让系统拖动永久失效。每次失败只多一次函数调用，代价可忽略。
        """
        try:
            handle = self.windowHandle()
        except Exception:
            return False
        if handle is None or not hasattr(handle, "startSystemMove"):
            return False
        try:
            return bool(handle.startSystemMove())
        except Exception:
            return False

    def mousePressEvent(self, event):
        """仅记录左键按下位置（手动回退用），并在拖动开始时完整重绘一次。

        不改变焦点、不模拟任何输入；外观不变。
        """
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPos() - self.frameGeometry().topLeft()
            self._drag_system_done = False
            self._force_repaint()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        """左键拖动：优先系统拖动，拿不到才回退手动 ``move()``。

        * 系统路径：交给 Windows 移窗（旧区域自动失效），本次拖动不再手动 move，
          避免两种移动互相打架；
        * 回退路径：保留原手动 ``move()`` 语义（按住点偏移跟随光标），并强制重绘。
        """
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            handled = False
            if not self._drag_system_done:
                handled = self._try_start_system_move()
                if handled:
                    self._drag_system_done = True
            if not handled:
                self.move(event.globalPos() - self._drag_offset)
                self.update()   # 排一次重绘（moveEvent 里还会强制 repaint 一次）
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        """结束拖动：复位拖动状态并完整重绘一次（清掉最后一段位移）。"""
        if event.button() == Qt.LeftButton:
            self._drag_offset = None
            self._drag_system_done = False
            self._force_repaint()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def moveEvent(self, event):
        """窗口被**任何方式**移动后都强制重绘一次。

        这是"系统拖动路径"的重绘兜底：系统移窗不经过 ``mouseMoveEvent``，但会发 moveEvent。
        """
        super().moveEvent(event)
        self._force_repaint()

    # ------------------------------------------------------------ 生命周期
    def start(self, max_ms=None):
        """进入 Qt 事件循环（阻塞）。

        - ``max_ms`` 为 None 时一直运行到 ``stop()`` / 窗口关闭；
        - 传毫秒数时用于自测/受控运行：到点自动退出事件循环（offscreen 下也能安全返回）；
        - 任何异常都不允许冒泡成进程崩溃：捕获后写进状态行。
        """
        try:
            if not self.isVisible():
                self.show()
            if self.start_thread:
                # 轻量首帧预热（非线程；集成模式下 start_thread=False 完全不执行）
                self._place_right()
            application = QApplication.instance()
            if application is None:
                self.set_status("UI 事件循环不可用：QApplication 不存在")
                return None
            if max_ms:
                from PyQt5.QtCore import QTimer

                QTimer.singleShot(int(max_ms), application.quit)
            return application.exec_()
        except Exception as exc:  # 绝不能因为 UI 异常让主进程崩溃
            try:
                self.set_status("UI 事件循环异常：%s" % exc)
            except Exception:
                pass
            return None

    def stop(self):
        """退出事件循环（不销毁窗口）。"""
        self._stopping = True
        try:
            application = QApplication.instance()
            if application is not None:
                application.quit()
        except Exception:
            pass

    def close(self):
        """关闭窗口并退出事件循环（可重复调用）。"""
        self._closed = True
        self.stop()
        try:
            super().close()
        except Exception:
            pass

    def closeEvent(self, event):
        self._closed = True
        self.stop()
        self._drag_offset = None
        self._drag_system_done = False
        event.accept()


def run_ui(engine=None):
    """便捷入口：构造 Panel 并进入事件循环（main.py 不必使用）。"""
    if engine is None:
        from candidates import CandidateEngine

        engine = CandidateEngine()
    panel = Panel(engine, start_thread=False)
    return panel.start()