# -*- coding: utf-8 -*-
"""行商助手 · 声控版 · PyQt5 独立悬浮窗（纯音频 · 只读展示）

本版**没有视觉**：不截屏、不读像素、不碰光标，面板也不做任何几何上报。因此与完整版
ui.py 相比，这里删掉了「格数识别 / 占格清单 / 物品边框 / 黄绿记录 / 图片预览」整条链路，
只在「开始监测」按钮**下方**加了一排**格数图标按钮**（用户要求：不用文字，用格子图标）：

    [不限] [1×1] [1×2] [2×1] [2×2] [2×3] [3×2]

为什么要有它：不看屏幕就没法自动知道"这件占几格"，而一个音效键常对应**多个物品**
（同音组：例如"空油桶声音"一组 4 件，2 格与 6 格混在一起）。格数是唯一能把它们分开的
硬信息，于是改为玩家手动选 —— 比自动推断更可靠，而且点一下只看图标、不读字，最快。

窗口性质（与完整版一致）：
    * **独立悬浮窗**，不是游戏内覆盖层、不做任何注入；
    * 置顶 ``WindowStaysOnTopHint``、无边框、``Qt.Tool``、``WindowDoesNotAcceptFocus``
      + ``WA_ShowWithoutActivating``：**永不抢焦点**；
    * 半透明 ``setWindowOpacity(0.92)``，左键拖动移动窗口；
    * 默认停靠主屏右侧。

面板只显示四类信息（与原版 task-14 定稿一致）：
    ① 详情行 ``格数筛选：2×3（6格 · 竖） · 来源 手动``；
    ② 音频信息行 ``音频 44100Hz · <设备名> · 峰值 0.018``（缺段省略，绝不编造）；
    ③ 匹配时间行 ``匹配于 3.2s 前``（年龄每次刷新重算；从未匹配则不显示）；
    ④ 候选卡片：缩略图 + 品质徽章 + 名称 + 品质/类别/格数 + 参考价值 + 大号彩色百分比。
底部状态栏只放**检测状态**；诊断（原因 / 触发来源 / 耗时 / 丢帧）一律只写日志。

边界（严格禁止）：不模拟键鼠、不自动点击、不自动购买、不读游戏内存、不联网、不截屏。
本文件只做「画窗口 + 收鼠标左键拖动 + 展示引擎数据」。
署名：作者 烟火（标题行 + 窗口标题）。
"""

from __future__ import annotations

import os
import sys
import time

from PyQt5.QtCore import QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (
    QAbstractButton,
    QApplication,
    QButtonGroup,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from shapes import SHAPE_ANY, SHAPE_CHOICES, describe_shape, grid_text
from shapes import is_any, normalize_shape, orientation_of, slots_of

__all__ = ["Panel", "ShapeButton", "run_ui", "MAX_CARDS", "SHAPE_BTN_W", "SHAPE_BTN_H"]


MAX_CARDS = 20
THUMB_SIZE = 72
PANEL_WIDTH = 440
PANEL_HEIGHT = 820
SCORE_FONT_PX = 22
MARGIN_RIGHT = 24
OPACITY = 0.92


SCORE_COLOR_HIGH = "#3fd07a"
SCORE_COLOR_MID = "#ffd24a"
SCORE_COLOR_LOW = "#ff9a3d"
SCORE_COLOR_WEAK = "#7f8b9e"


SECONDARY_GAP = 8

SECONDARY_GAP_CHOICES = (SECONDARY_GAP, 4, 15, 0)

QUALITY_STRIPE_WIDTH = 5
CARD_PADDING_V = 3
CARD_FRAME_PX = 1
CARD_MIN_HEIGHT = THUMB_SIZE + 2 * CARD_PADDING_V + 2 * CARD_FRAME_PX
CARD_NAME_FONT_PX = 15
CARD_META_FONT_PX = 12
CARD_VALUE_FONT_PX = 13
CARD_NOTE_FONT_PX = 12

WAITING_TEXT = "等待音效…"

SHAPE_EMPTY_TEXT = "本轮声音里没有与 {want} 相符的物品（不显示不匹配项）"

IDLE_STATUS_TEXT = "未开始监测：不读取游戏信息，点上方按钮或按 F8 开始。"

MONITORING_STATUS_TEXT = "监测中：等待音效…"

AUTHOR_TEXT = "作者 烟火"
WINDOW_TITLE = "行商助手 · 声控版（纯音频） · %s" % AUTHOR_TEXT


TITLE_BUTTON_W = 24
TITLE_BUTTON_H = 20

COLLAPSED_MIN_HEIGHT = 34


SHAPE_BTN_W = 44
SHAPE_BTN_H = 34

SHAPE_BTN_GAP = 5


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


ASSETS_DIR = os.path.join(os.path.dirname(BASE_DIR), "assets")

PAY_SUBDIR = "pay"
PAY_IMAGES = (("微信支付", "wechat.png"), ("支付宝", "alipay.png"))

PAY_QR_SIZE = 300

QUALITY_COLORS = {"金色": "#d9b64a", "红色": "#e0635c", "紫色": "#a274d8"}
DEFAULT_QUALITY_COLOR = "#9aa4b2"





def score_color(score) -> str:
    """匹配概率 → 大号百分比数字的颜色（≥85 绿 / ≥70 黄 / ≥50 橙 / <50 灰）。"""
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
    """品质名 -> 颜色；未知品质用 DEFAULT_QUALITY_COLOR。"""
    return QUALITY_COLORS.get(str(quality or "").strip(), DEFAULT_QUALITY_COLOR)


def badge_text_color(fill: str) -> str:
    """在填充色上挑对比文字色（亮底深字、暗底白字，按 Rec.601 相对亮度取阈值 150）。"""
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


def secondary_gap(score, top_score) -> int:
    """该候选比最高分低多少分（负数/0 = 不低于，返回 0）。"""
    try:
        return max(0, int(top_score or 0) - int(score or 0))
    except (TypeError, ValueError):
        return 0


def is_secondary(score, top_score, gap=None) -> bool:
    """是否属于「次要候选」（比最高分低 ≥ ``gap`` 分，``gap`` 省略时用 ``SECONDARY_GAP``）。"""
    limit = SECONDARY_GAP if gap is None else int(gap)
    if limit <= 0:
        return False
    return secondary_gap(score, top_score) >= limit


def secondary_text(score, top_score, gap=None) -> str:
    """次要候选的灰标文字；主候选（或关了标记）返回空串。"""
    limit = SECONDARY_GAP if gap is None else int(gap)
    if limit <= 0:
        return ""
    d = secondary_gap(score, top_score)
    return "次要 · 低%d分" % d if d >= limit else ""



SHAPE_GLYPH_SIDE = 22


def shape_glyph_rects(shape, rect=None):
    """把形状换算成图标里每个格子的矩形列表（纯几何，自测直接断言）。

    ``rect`` 是图标可用区域（默认 22x22 居中正方形）。返回 ``[(x, y, side), ...]``，
    顺序 = 先按列、再按行。「不限」返回空列表（它画的是虚线框，没有格子）。
    """
    width, height = normalize_shape(shape)
    if width <= 0 or height <= 0:
        return []
    if rect is None:
        rect = (0.0, 0.0, float(SHAPE_GLYPH_SIDE), float(SHAPE_GLYPH_SIDE))
    left, top, box_w, box_h = (float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]))
    gap = 2.0
    side = min((box_w - (width - 1) * gap) / width,
               (box_h - (height - 1) * gap) / height)
    side = max(3.0, min(side, 8.0))
    total_w = width * side + (width - 1) * gap
    total_h = height * side + (height - 1) * gap
    x0 = left + (box_w - total_w) / 2.0
    y0 = top + (box_h - total_h) / 2.0
    out = []
    for col in range(width):
        for row in range(height):
            out.append((x0 + col * (side + gap), y0 + row * (side + gap), side))
    return out


def _paint_shape_glyph(painter, rect: QRectF, shape, color: QColor) -> None:
    """画一个"格数图标"：具体形状 → 一格格小方块；不限 → 虚线方框。"""
    width, height = normalize_shape(shape)
    if width <= 0 or height <= 0:
        painter.setPen(QPen(color, 1.3, Qt.DashLine))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(rect.adjusted(2.0, 2.0, -2.0, -2.0), 3.0, 3.0)
        return
    painter.setPen(Qt.NoPen)
    painter.setBrush(color)
    for x, y, side in shape_glyph_rects(
            shape, (rect.left(), rect.top(), rect.width(), rect.height())):
        painter.drawRoundedRect(QRectF(x, y, side, side), 1.5, 1.5)





_STYLESHEET_BASE = """
QWidget#panel { background-color: #16191f; border: 1px solid #333a45; border-radius: 8px; }
QLabel { color: #d8dee9; background: transparent; }
QLabel#title { color: #8fb8ff; font-weight: bold; font-size: 15px; }
QLabel#reason { color: #cfd6e4; }
QLabel#status { color: #8fb8ff; }
QLabel#summary { color: #7d8697; font-size: 12px; }
QLabel#hint { color: #616b7d; }
QLabel#waiting { color: #6d7789; font-size: 13px; }
QLabel#audio { color: #7d8697; font-size: 12px; }
QLabel#match { color: #8fb8ff; font-size: 12px; }
QPushButton#viewbutton, QPushButton#smallbutton {
    background-color: #262d38; color: #b9c3d2; border: 1px solid #3a4250;
    border-radius: 3px; font-size: 12px; padding: 2px 0px; }
QPushButton#viewbutton:hover, QPushButton#smallbutton:hover {
    background-color: #313a48; color: #e6ecf5; }
QPushButton#collapsebutton, QPushButton#closebutton {
    background-color: #262d38; color: #c8d0dc; border: 1px solid #3a4250;
    border-radius: 3px; font-size: 12px; padding: 0px; }
QPushButton#collapsebutton:hover { background-color: #333c4a; color: #ffffff; }
QPushButton#closebutton { color: #e8a0a0; }
QPushButton#closebutton:hover { background-color: #7a2d2d; color: #ffffff; border-color: #a34141; }
QDialog#sponsordlg { background-color: #16191f; }
QLabel#sponsorhead { color: #8fb8ff; font-weight: bold; font-size: 14px; }
QLabel#sponsornote { color: #b9c3d2; font-size: 12px; }
QLabel#sponsorname { color: #eef2f8; font-weight: bold; font-size: 14px; }
QLabel#sponsorpic { background-color: #2a313b; border: 1px solid #37414f;
                    border-radius: 6px; color: #8b94a6; font-size: 12px; }
QLabel#sponsorhint { color: #7d8697; font-size: 11px; }
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
QScrollArea#cardscroll, QWidget#cardviewport, QWidget#cardshost { background: transparent; }
QScrollBar:vertical { background: #1b2027; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #3a4453; border-radius: 5px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QTextEdit { background-color: #1b2027; color: #d8dee9; border: 1px solid #333a45; }
"""


_CARD_STYLE = """
QLabel#cardname { color: #eef2f8; font-weight: bold; font-size: %(name)dpx; }
QLabel#cardmeta { color: #98a2b3; font-size: %(meta)dpx; }
QLabel#cardvalue { color: #7fd1a8; font-size: %(value)dpx; }
QLabel#cardnote { color: #e0a45c; font-size: %(note)dpx; }
QLabel#cardscore { color: #cfd6e4; font-weight: bold; font-size: %(score)dpx; }
QLabel#cardsecondary { color: #8b94a6; font-size: 10px; }
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
    app = QApplication(sys.argv[:1] if sys.argv else ["hangshang-audio"])
    return app, True


class ClickableLabel(QLabel):
    """可点击的标签（保留给自测/扩展用：面板左键任意处都能拖动窗口，靠子控件忽略事件冒泡）。"""

    clicked = pyqtSignal()

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.clickable = False

    def mousePressEvent(self, event):
        try:
            if self.clickable and event.button() == Qt.LeftButton:
                self.clicked.emit()
                event.accept()
                return
        except Exception:
            pass
        super().mousePressEvent(event)


class ShapeButton(QAbstractButton):
    """格数图标按钮（**没有文字**，只有格子图标）。

    用户要求："这几个选项内不用文字展示，为图标类似的格数展示，方便查看"。
    所以整按钮自绘：圆角底板 + 中央格子图标；选中态用绿色底 + 绿边 + 亮绿格子，
    比文字按钮更好扫视，也不占更多宽度。
    """

    def __init__(self, shape, parent=None):
        super().__init__(parent)
        self.shape = normalize_shape(shape)
        self.setCheckable(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WA_Hover, True)
        self.setFixedSize(SHAPE_BTN_W, SHAPE_BTN_H)
        self.setToolTip(self._tooltip())


    def _tooltip(self) -> str:
        shape = self.shape
        if is_any(shape):
            return "不限格数：所有格数的物品都显示（听到音效时按声音识别）"
        orient = orientation_of(shape)
        orient_text = "横（宽>高）" if orient == "横" else (
            "竖（高>宽）" if orient == "竖" else "正方形")
        return ("只看 %s 格的物品：%s\n"
                "（点一下按这个格数筛候选；朝向只做标注、不会丢掉朝向不符的候选）"
                % (describe_shape(shape), orient_text))

    def sizeHint(self) -> QSize:
        return QSize(SHAPE_BTN_W, SHAPE_BTN_H)

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            rect = QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
            checked = bool(self.isChecked())
            hovered = bool(self.underMouse())
            if checked:
                background, border = QColor("#2f4a3c"), QColor("#4fbf72")
            elif hovered:
                background, border = QColor("#313a48"), QColor("#56688a")
            else:
                background, border = QColor("#262d38"), QColor("#3a4250")
            painter.setPen(QPen(border, 1.0))
            painter.setBrush(background)
            painter.drawRoundedRect(rect, 4.0, 4.0)
            glyph = QRectF(rect.center().x() - SHAPE_GLYPH_SIDE / 2.0,
                           rect.center().y() - SHAPE_GLYPH_SIDE / 2.0,
                           float(SHAPE_GLYPH_SIDE), float(SHAPE_GLYPH_SIDE))
            _paint_shape_glyph(painter, glyph, self.shape,
                               QColor("#9fe0b4") if checked else QColor("#b9c3d2"))
        finally:
            painter.end()


class Panel(QWidget):
    """只读悬浮窗。所有数据来自 ``CandidateEngine`` + 音频识别结果，窗口自身不做任何操作。"""

    def __init__(self, engine, start_thread=True, strict_shape=True):
        """构造窗口。

        ``start_thread``：``False``（集成模式）= **完全不创建线程/定时器**，由集成层
        自己的 80ms 轮询调用 ``show_candidates`` / ``set_status``。
        ``strict_shape``：只显示与**玩家手选格数**相符的候选（默认 True，玩家面向）；
        调试/自测可关掉，恢复"保留但排后"的行为（见 ``candidates.resolve``）。
        """

        self.app, self._owns_app = _ensure_application()
        super().__init__(None)

        self.engine = engine
        self.start_thread = bool(start_thread)
        self._strict_shape = bool(strict_shape)
        self._shape = SHAPE_ANY
        self._shape_empty = False
        self._filter_log_sig = None

        self._all_candidates = []
        self._shown_candidates = []
        self._status_text = ""
        self._last_reason = ""
        self._last_slots = 0
        self._last_orientation = ""
        self._last_trigger_src = ""
        self._last_signature = None
        self._history_signature = None


        self._last_match_at = None
        self._sticky = False
        self._sticky_announced = False
        self._last_match_rows = []
        self._stopping = False
        self._closed = False
        self._allow_close = False
        self._system_shutdown = False
        self._close_click_at = None
        self._start_error = None
        self._card_widgets = []
        self._drag_offset = None
        self._drag_system_done = False
        self._audio_samplerate = None
        self._audio_window_getter = None
        self._audio_device = None
        self._audio_peak = None
        self._monitoring = False
        self._secondary_gap = SECONDARY_GAP
        self._active_getter = None
        self._active_setter = None
        self._shape_getter = None
        self._shape_setter = None
        self._collapsed = False

        self._build_ui()
        self._connect_signals_once()
        self.set_status("")


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


        self.title_row = QHBoxLayout()
        self.title_row.setSpacing(4)
        self.title_label = QLabel("行商助手 · 声控版 · %s" % AUTHOR_TEXT)
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


        self.body = QWidget()
        self.body.setObjectName("body")
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(6)
        root.addWidget(self.body, 1)


        self.start_button = QPushButton("▶ 开始监测（F8）")
        self.start_button.setObjectName("startbutton")
        self.start_button.setToolTip(
            "未开始监测时不读取任何信息。点这里或按 F8 开始/停止。\n"
            "本版只监听系统回环音频（不截屏、不读画面）。")
        body.addWidget(self.start_button)




        self.shape_group = QButtonGroup(self)
        self.shape_group.setExclusive(True)
        self.shape_buttons = []
        shape_row = QHBoxLayout()
        shape_row.setSpacing(SHAPE_BTN_GAP)
        shape_row.setContentsMargins(0, 0, 0, 0)
        for shape in SHAPE_CHOICES:
            button = ShapeButton(shape)
            self.shape_group.addButton(button)
            self.shape_buttons.append(button)
            shape_row.addWidget(button)
            if shape == SHAPE_ANY:
                button.setChecked(True)
        shape_row.addStretch(1)
        body.addLayout(shape_row)


        self.reason_label = QLabel("")
        self.reason_label.setObjectName("reason")
        self.reason_label.setWordWrap(True)
        body.addWidget(self.reason_label)


        self.audio_label = QLabel("")
        self.audio_label.setObjectName("audio")
        self.audio_label.setWordWrap(True)
        body.addWidget(self.audio_label)


        self.match_label = QLabel("")
        self.match_label.setObjectName("match")
        body.addWidget(self.match_label)

        self.status_label = QLabel(IDLE_STATUS_TEXT)
        self.status_label.setObjectName("status")
        self.status_label.setWordWrap(True)
        body.addWidget(self.status_label)


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
        self.summary_label = QLabel("候选 0 / 显示 0")
        self.summary_label.setObjectName("summary")
        button_row.addWidget(self.summary_label, 1)
        self.secondary_button = QPushButton("次要 8分")
        self.secondary_button.setObjectName("smallbutton")
        self.secondary_button.setToolTip(
            "「次要候选」的界线：比**本列表最高分**低多少分就把该候选标成灰色「次要」。\n"
            "点击循环切换：8分 → 4分 → 15分 → 关（关 = 完全不标）。\n"
            "**不砍信息**：任何档位下低分候选都照样显示，只是标得强弱不同。")
        button_row.addWidget(self.secondary_button)
        self.yes_button = QPushButton("✓ 对 F9")
        self.yes_button.setObjectName("smallbutton")
        self.yes_button.setToolTip(
            "把**刚才这次识别**用的音频存成正确样本（learned/positive/<声音组>/）。\n"
            "本版是 1.1.1 识别引擎，不加载学习模板；样本只作归档/以后版本用。快捷键 F9。")
        self.no_button = QPushButton("✗ 错 F10")
        self.no_button.setObjectName("smallbutton")
        self.no_button.setToolTip(
            "把刚才这次识别标成错误样本，只留档（learned/negative/），不进模板库。快捷键 F10。")
        button_row.addWidget(self.yes_button)
        button_row.addWidget(self.no_button)
        self.clear_button = QPushButton("清空")
        self.history_button = QPushButton("历史")
        button_row.addWidget(self.clear_button)
        button_row.addWidget(self.history_button)
        body.addLayout(button_row)


        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("cardscroll")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)



        self.scroll_area.setAutoFillBackground(False)
        self.scroll_area.viewport().setObjectName("cardviewport")
        self.scroll_area.viewport().setAutoFillBackground(False)
        self.cards_host = QWidget()
        self.cards_host.setObjectName("cardshost")
        self.cards_host.setAutoFillBackground(False)
        self.cards_layout = QVBoxLayout(self.cards_host)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(6)
        self.cards_layout.setAlignment(Qt.AlignTop)
        self.waiting_label = QLabel(WAITING_TEXT)
        self.waiting_label.setObjectName("waiting")
        self.waiting_label.setAlignment(Qt.AlignCenter)



        self.scroll_area.setWidget(self.cards_host)
        body.addWidget(self.scroll_area, 1)

        self.hint_label = QLabel("左键拖动移动窗口 · 仅提示，不自动操作")
        self.hint_label.setObjectName("hint")

        self.sponsor_button = QPushButton("赞助")
        self.sponsor_button.setObjectName("smallbutton")
        self.sponsor_button.setFocusPolicy(Qt.NoFocus)
        self.sponsor_button.setFixedWidth(48)
        self.sponsor_button.setToolTip(
            "支持作者：弹出一个静态窗口，里面是**微信支付 / 支付宝**两张收款码。\n"
            "纯自愿 —— 不打赏不影响任何功能：本程序不联网、不校验授权、不采集任何信息。\n"
            "窗口不抢焦点、非模态：开着它照样监测与识别，点右上角 ✕ 关掉即可。")
        hint_row = QHBoxLayout()
        hint_row.setSpacing(4)
        hint_row.addWidget(self.hint_label, 1)
        hint_row.addWidget(self.sponsor_button)
        body.addLayout(hint_row)

        self.history_dialog = None
        self.sponsor_dialog = None
        self._place_right()
        self._rebuild_combos([], [], [])

        self._rebuild_cards([])

    def _connect_signals_once(self):
        """所有信号只在这里连接一次，避免重复调用 show_candidates 时重复 connect。"""
        self.quality_combo.currentIndexChanged.connect(self._on_filter_changed)
        self.category_combo.currentIndexChanged.connect(self._on_filter_changed)
        self.orientation_combo.currentIndexChanged.connect(self._on_filter_changed)
        self.clear_button.clicked.connect(self._on_clear_clicked)
        self.yes_button.clicked.connect(lambda: self._on_mark_clicked(True))
        self.no_button.clicked.connect(lambda: self._on_mark_clicked(False))
        self.history_button.clicked.connect(self._on_history_clicked)
        self.start_button.clicked.connect(self._on_start_clicked)
        self.collapse_button.clicked.connect(self.toggle_collapsed)
        self.close_button.clicked.connect(self._on_close_clicked)
        self.secondary_button.clicked.connect(self._cycle_secondary_gap)
        self.sponsor_button.clicked.connect(self._on_sponsor_clicked)
        for button in self.shape_buttons:
            button.clicked.connect(
                lambda _checked=False, b=button: self._on_shape_clicked(b.shape))
        self.set_active_state(False)


    @property
    def shape(self):
        """当前格数筛选（``shapes`` 里的二元组；``(0, 0)`` = 不限）。"""
        return self._shape

    def set_shape(self, shape, notify=True) -> None:
        """设定格数筛选（幂等）。``notify=True`` 时同步给集成层（若有）。"""
        shape = normalize_shape(shape)
        changed = shape != self._shape
        self._shape = shape
        for button in self.shape_buttons:
            button.setChecked(button.shape == shape)
            button.update()
        if changed:
            print("[shape] 格数筛选 = %s" % describe_shape(shape), flush=True)
        if notify and self._shape_setter is not None:
            try:
                self._shape_setter(shape)
            except Exception:
                pass

        if changed and self._last_match_rows:
            self._apply_shape()
            self._update_labels()

    def bind_shape(self, getter, setter=None) -> None:
        """把格数筛选接到集成层（可选；面板自己也能独立工作）。

        ``getter() -> shape``：初始值；``setter(shape)``：变化通知。
        """
        self._shape_getter = getter
        self._shape_setter = setter
        if getter is not None:
            try:
                self.set_shape(getter(), notify=False)
            except Exception:
                pass

    def _on_shape_clicked(self, shape, *_args):
        """图标按钮点击：切到该格数（互斥组已负责选中态，这里只更新筛选与日志）。"""
        self.set_shape(shape)


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
        没有任务栏条目，真最小化在游戏里就找不回来了。收起 = 只隐藏 ``self.body`` 一个容器。
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


    def bind_active(self, getter, setter) -> None:
        """把「开始监测」按钮接到集成层的开关上。

        getter() -> bool：当前是否在监测；setter(bool) -> bool：切换监测。
        面板自身仍然只读：它只切换"要不要采样"，不做任何游戏内操作。
        """
        self._active_getter = getter
        self._active_setter = setter
        self.set_active_state(bool(getter()))

    def set_active_state(self, active: bool) -> None:
        """刷新按钮外观与检测状态文案（集成层切换状态后调用，或由轮询调用）。"""
        try:
            self._monitoring = bool(active)
            if active:
                self.start_button.setText("■ 停止监测（F8）")

                self.start_button.setStyleSheet(
                    "QPushButton#startbutton { background-color:#5d3a2f; color:#f7eee9;"
                    " border:1px solid #8a5f4c; font-weight:bold; padding:5px; }")
                self._status_text = MONITORING_STATUS_TEXT
            else:
                self.start_button.setText("▶ 开始监测（F8）")

                self.start_button.setStyleSheet(
                    "QPushButton#startbutton { background-color:#2f5d3a; color:#eaf7ee;"
                    " border:1px solid #4c8a5f; font-weight:bold; padding:5px; }")
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

        传空文本时按当前是否监测回落默认文案。**只喂检测状态**：诊断不许塞进来。
        """
        self._status_text = "" if text is None else str(text)
        self._update_labels()

    def set_audio_source(self, samplerate=None, window_getter=None, device=None, peak=None):
        """记录"当前声音状态"，供面板音频信息行显示：``音频 44100Hz · <设备名> · 峰值 0.018``。

        三段都是**可选**的：拿不到的段请传 ``None``，面板自动省略，**绝不编造**。
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
        """匹配时间行：``匹配于 3.2s 前``（年龄**每次刷新按当前时间重算**）。"""
        if self._last_match_at is None:
            return ""
        base = "匹配于 %.1fs 前" % self._sticky_age()
        if self._sticky:
            return "%s · 这几下没听到新的声音，保留上次结果" % base
        return base

    def _shape_need_text(self) -> str:
        """当前格数筛选的一句话（详情行 / 空态文案共用）。"""
        return describe_shape(self._shape)

    def _update_labels(self):
        """刷新面板文本。面板只显示四类信息（见模块文档）。"""

        text = self._status_text.strip()
        if not text:
            text = MONITORING_STATUS_TEXT if self._monitoring else IDLE_STATUS_TEXT
        self.status_label.setText(text)


        self.reason_label.setText(
            "格数筛选：%s · 来源 手动" % self._shape_need_text())


        audio = self._audio_line()
        self.audio_label.setText(audio)
        self.audio_label.setVisible(bool(audio))


        matched = self._match_line()
        self.match_label.setText(matched)
        self.match_label.setVisible(bool(matched))

        self.waiting_label.setVisible(not self._shown_candidates)
        self.waiting_label.setText(self._waiting_text())

    def _waiting_text(self) -> str:
        """等待区文案：严格过滤丢空时给出明确状态，其余情况是原「等待音效…」。"""
        if self._shape_empty:
            return SHAPE_EMPTY_TEXT.format(want=self._shape_need_text())
        return WAITING_TEXT


    def _sticky_age(self) -> float:
        """距上次真正匹配到声音的秒数（每次调用都按当前时间重算）。"""
        if self._last_match_at is None:
            return 0.0
        try:
            return max(0.0, time.monotonic() - float(self._last_match_at))
        except Exception:
            return 0.0

    def _reset_sticky(self):
        """清掉粘滞状态与匹配时刻（「清空」按钮必须走这里，否则旧卡片会被复活）。"""
        self._sticky = False
        self._sticky_announced = False
        self._last_match_at = None
        self._last_match_rows = []


    def show_candidates(self, match_rows, last_reason="", trigger_src=""):
        """展开并显示候选。可被同一 Qt 线程以高频（如 80ms）反复调用。

        一次拖动会产生多次触发，只有音效响的那一轮 ``rows`` 非空；其余几轮为空。
        因此：
        * ``match_rows`` 非空 → 按当前格数重筛并重建卡片，记下匹配时刻，结束粘滞；
        * ``match_rows`` 为空但手上还有上次的声音组 → **不重建、不清空**（粘滞），
          只刷新格数/年龄；
        * ``match_rows`` 为空且从未有过声音 → 维持等待文案。

        幂等保证：候选集合（名称+分数）与格数筛选都没变化时**不重建任何 widget**，
        只刷新文本；历史只在结果真正变化时追加一次（80ms 轮询不会刷爆历史）。
        """
        if self._closed:
            return []
        self._last_reason = "" if last_reason is None else str(last_reason)
        self._last_trigger_src = "" if trigger_src is None else str(trigger_src)

        rows = [r for r in (match_rows or []) if isinstance(r, dict)]
        if rows:
            self._last_match_rows = rows
            self._last_match_at = time.monotonic()
            self._sticky = False
            self._sticky_announced = False
        elif self._last_match_rows:

            self._sticky = True
            if not self._sticky_announced:
                self._sticky_announced = True
                try:
                    sys.stdout.write("[sticky] 本次未匹配到声音：保留 %.1fs 前的结果\n"
                                     % self._sticky_age())
                    sys.stdout.flush()
                except Exception:
                    pass
        else:
            self._shape_empty = False
            self._all_candidates = []
            self._shown_candidates = []
            self._rebuild_combos([], [], [])
            self._rebuild_cards([])
            self.summary_label.setText("候选 0 条")
            self._update_labels()
            return []

        self._apply_shape()
        self._update_labels()
        return list(self._all_candidates)

    def _apply_shape(self):
        """按**当前格数筛选**把 ``_last_match_rows`` 重新解析成候选并重建卡片。

        格数硬筛（``strict_shape``），朝向**只标注不丢**（``strict_orientation=False``）：
        2×3 与 3×2 面积相同、互为镜像，朝向判偏就丢会把正确物品整条丢掉 —— 这是原版
        实测踩过的坑，所以朝向永远只做"排前 + 灰字提示"。
        """
        slots = slots_of(self._shape)
        orientation = orientation_of(self._shape)
        self._last_slots = int(slots)
        self._last_orientation = str(orientation or "")
        if self.engine is None:
            candidates = [c for c in self._last_match_rows if isinstance(c, dict)]
            stats = {}
        else:
            try:
                candidates = self.engine.resolve(
                    self._last_match_rows, slots, orientation,
                    strict_shape=self._strict_shape,
                    strict_orientation=False)
            except Exception as exc:
                print("[resolve] 候选解析失败: %s: %s" % (type(exc).__name__, exc),
                      flush=True)
                candidates = []
            stats = dict(getattr(self.engine, "last_stats", None) or {})
        self._shape_empty = bool(stats.get("empty_after_shape"))
        self._log_shape_filter(stats)

        signature = (int(slots), str(orientation),
                     tuple((c.get("name"), c.get("score")) for c in candidates))
        if signature != self._last_signature:
            self._last_signature = signature
            self._all_candidates = candidates
            if candidates and self.engine is not None\
                    and signature != self._history_signature:
                self._history_signature = signature
                try:
                    self.engine.history_add(candidates, {"slots": slots,
                                                         "shape": grid_text(self._shape)})
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

    def _log_shape_filter(self, stats) -> None:
        """把被丢弃的候选写进日志（面板不显示）。同样的丢弃只打一行，避免刷屏。"""
        dropped = list(stats.get("dropped_names") or [])
        if not dropped:
            return
        signature = (int(self._last_slots or 0), str(self._last_orientation or ""),
                     tuple(dropped))
        if signature == self._filter_log_sig:
            return
        self._filter_log_sig = signature
        try:
            sys.stdout.write("[filter] 丢弃 %d 条与「%s」不符的候选：%s\n"
                             % (len(dropped), self._shape_need_text(), " ".join(dropped)))
            if stats.get("empty_after_shape"):
                sys.stdout.write("[filter] 严格过滤后没有与「%s」相符的候选（不显示不匹配项）\n"
                                 % self._shape_need_text())
            sys.stdout.flush()
        except Exception:
            pass

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

    def _rebuild_combos(self, qualities, categories, orientations):
        """在 blockSignals 保护下刷新下拉项，保留原选择（幂等、不重复 connect）。"""
        pairs = ((self.quality_combo, qualities),
                 (self.category_combo, categories),
                 (self.orientation_combo, orientations))
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


        summary = "候选 %d / 显示 %d" % (len(self._all_candidates),
                                         len(filtered[:MAX_CARDS]))
        if not is_any(self._shape):
            summary += " ｜ %s" % grid_text(self._shape)
        self.summary_label.setText(summary)
        self.summary_label.setToolTip(
            "候选 %d 条，当前显示 %d 条（格数筛选：%s）"
            % (len(self._all_candidates), len(filtered), self._shape_need_text()))
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
        """清空当前展示（历史保留；格数筛选不重置 —— 那是玩家的长期选择）。"""
        self._all_candidates = []
        self._shown_candidates = []
        self._last_signature = None
        self._shape_empty = False
        self._filter_log_sig = None
        self._reset_sticky()
        self._rebuild_combos([], [], [])
        self._rebuild_cards([], drop_sticky=True)
        self.summary_label.setText("候选 0 条")
        self.waiting_label.setText(WAITING_TEXT)
        self._update_labels()

    def _cycle_secondary_gap(self, *_args) -> None:
        """切换「次要线」：8 → 4 → 15 → 关 → 8。"""
        choices = list(SECONDARY_GAP_CHOICES)
        try:
            idx = choices.index(int(self._secondary_gap))
        except ValueError:
            idx = 0
        self._secondary_gap = choices[(idx + 1) % len(choices)]
        self._update_secondary_button()
        if self._all_candidates:
            self._apply_filter()
        else:
            self._refresh_scroll_extent()

    def _update_secondary_button(self) -> None:
        gap = int(self._secondary_gap)
        try:
            self.secondary_button.setText("次要 关" if gap <= 0 else "次要 %d分" % gap)
        except Exception:
            pass


    def _clear_cards(self, drop_sticky=False):
        """清掉卡片 widget（每次重建都会调用）。``drop_sticky`` 只在「清空」时用。"""
        for widget in self._card_widgets:
            try:
                widget.setParent(None)
                widget.deleteLater()
            except RuntimeError:
                pass
        self._card_widgets = []
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            widget = item.widget()
            if widget is None:
                continue
            if widget is self.waiting_label:
                continue
            widget.setParent(None)
            widget.deleteLater()
        if drop_sticky:
            self._reset_sticky()

    def _rebuild_cards(self, candidates, drop_sticky=False):
        self._clear_cards(drop_sticky=drop_sticky)
        self.cards_layout.removeWidget(self.waiting_label)
        top_score = int((candidates[0] or {}).get("score") or 0) if candidates else 0
        for candidate in candidates:
            card = self._make_card(candidate, top_score=top_score)
            self._card_widgets.append(card)
            self.cards_layout.addWidget(card)
        if not candidates:

            self.cards_layout.addStretch(1)
            self.cards_layout.addWidget(self.waiting_label, 0, Qt.AlignHCenter)
            self.cards_layout.addStretch(1)
        self.waiting_label.setVisible(not candidates)
        self._refresh_scroll_extent()
        try:
            from PyQt5.QtCore import QTimer

            QTimer.singleShot(0, self._refresh_scroll_extent)
        except Exception:
            pass

    def _content_height(self) -> int:
        """卡片区内容高度（像素）——不依赖布局此刻是否已"看见"新卡片。"""
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
        """候选数量变化后让 QScrollArea 正确重算滚动范围（详见完整版 ui.py 同名方法）。"""
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

    def _make_card(self, candidate, top_score=None):
        from PyQt5.QtWidgets import QFrame

        card = QFrame()
        card.setObjectName("card")
        card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        card.setMinimumHeight(CARD_MIN_HEIGHT)

        row = QHBoxLayout(card)
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
            % (quality, category, candidate.get("slot", 0),
               ("（%s）" % candidate.get("orientation"))
               if candidate.get("orientation") else ""))
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
        score_label = QLabel("%d%%" % score)
        score_label.setObjectName("cardscore")
        score_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        score_label.setStyleSheet(
            "QLabel#cardscore { color: %s; font-size: %dpx; font-weight: bold; }"
            % (score_color(score), SCORE_FONT_PX))
        score_label.setToolTip("匹配概率（与品质无关）")
        right.addWidget(score_label)
        mark = secondary_text(score, top_score, self._secondary_gap)
        if mark:
            mark_label = QLabel(mark.replace(" · ", "\n"))
            mark_label.setObjectName("cardsecondary")
            mark_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            mark_label.setToolTip(
                "比列表最高分（%d%%）低 %d 分：不是不可能，但明显更弱，别当成同等候选。"
                % (int(top_score or 0), secondary_gap(score, top_score)))
            right.addWidget(mark_label)
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
        return pixmap.scaled(THUMB_SIZE, THUMB_SIZE, Qt.KeepAspectRatio,
                             Qt.SmoothTransformation)


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
        self.history_dialog.show()


    def _sponsor_asset(self, filename) -> str:
        """赞助图 ``assets/pay/<filename>`` 的绝对路径。

        **每次调用都读模块全局** ``ASSETS_DIR``：编译版由 ``_rt.launcher`` 把它改指向
        挂载出来的虚拟目录（磁盘上没有 ``assets/``），导入期算好会指错地方。
        """
        return os.path.join(ASSETS_DIR, PAY_SUBDIR, str(filename))

    def _sponsor_pixmap(self, filename):
        """读一张收款码；读不到返回 ``None``（绝不画空白框假装有图）。

        ``QPixmap(path)`` 在编译版会被 ``_rt.vfs`` 换成"从资产包里取字节"的工厂 ——
        所以这里**必须走模块全局这个名字**，不能用 ``QtGui.QPixmap`` 绕过它。
        """
        path = self._sponsor_asset(filename)
        pixmap = QPixmap(path) if os.path.exists(path) else QPixmap()
        if pixmap.isNull():
            return None
        return pixmap.scaled(PAY_QR_SIZE, PAY_QR_SIZE, Qt.KeepAspectRatio,
                             Qt.SmoothTransformation)

    def _build_sponsor_dialog(self):
        """建「赞助」窗口（只建一次，之后反复 ``show()``）。

        窗口性质与面板一致：``Qt.Tool`` + 置顶 + ``WindowDoesNotAcceptFocus`` +
        ``WA_ShowWithoutActivating`` —— **不抢游戏焦点**。内容全是静态文字与图片：
        不联网、不生成订单、不读任何用户信息、不模拟任何输入。
        """
        dialog = QDialog(self)
        dialog.setObjectName("sponsordlg")
        dialog.setWindowTitle("赞助 · 请作者喝杯咖啡")
        dialog.setWindowFlags(
            Qt.Tool | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus)
        dialog.setAttribute(Qt.WA_ShowWithoutActivating, True)
        dialog.setStyleSheet(STYLESHEET)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        head = QLabel("行商助手（声控版）是免费的。")
        head.setObjectName("sponsorhead")
        layout.addWidget(head)
        note = QLabel(
            "觉得有用的话，可以请作者喝杯咖啡 —— 纯自愿：不打赏也不影响任何功能"
            "（本程序不联网、不校验授权、不采集任何信息）。")
        note.setObjectName("sponsornote")
        note.setWordWrap(True)
        layout.addWidget(note)

        row = QHBoxLayout()
        row.setSpacing(18)
        self._sponsor_qr = []
        for title, filename in PAY_IMAGES:
            column = QVBoxLayout()
            column.setSpacing(4)
            name = QLabel(title)
            name.setObjectName("sponsorname")
            name.setAlignment(Qt.AlignCenter)
            column.addWidget(name)

            pic = QLabel("")
            pic.setObjectName("sponsorpic")
            pic.setFixedSize(PAY_QR_SIZE, PAY_QR_SIZE)
            pic.setAlignment(Qt.AlignCenter)
            pixmap = self._sponsor_pixmap(filename)
            if pixmap is None:
                pic.setText("收款码图片缺失\n%s" % filename)
                pic.setToolTip("资产包里没有这张图：%s\n源码运行时它应在 %s"
                               % (filename, self._sponsor_asset(filename)))
            else:
                pic.setPixmap(pixmap)
                pic.setToolTip("%s 收款码（%d×%d 显示）" % (title, PAY_QR_SIZE, PAY_QR_SIZE))
            column.addWidget(pic, 0, Qt.AlignHCenter)

            tip = QLabel("手机 微信 / 支付宝「扫一扫」")
            tip.setObjectName("sponsorhint")
            tip.setAlignment(Qt.AlignCenter)
            column.addWidget(tip)
            row.addLayout(column)
            self._sponsor_qr.append((title, filename, pic))
        row.addStretch(1)
        layout.addLayout(row)

        foot = QLabel("这两张只是收款码，扫不扫、给多少都由你自己决定。")
        foot.setObjectName("sponsorhint")
        foot.setWordWrap(True)
        layout.addWidget(foot)
        return dialog

    def _on_sponsor_clicked(self, *_args):
        """打开（或前置）「赞助」窗口；**不**影响监测与识别（非模态 ``show()``）。"""
        if self.sponsor_dialog is None:
            self.sponsor_dialog = self._build_sponsor_dialog()
        self.sponsor_dialog.show()
        try:
            self.sponsor_dialog.raise_()
        except Exception:
            pass

    def _close_dialogs(self):
        """跟着面板一起关掉非模态子窗口（「历史」「赞助」）。"""
        for attr in ("history_dialog", "sponsor_dialog"):
            dialog = getattr(self, attr, None)
            if dialog is None:
                continue
            try:
                dialog.close()
            except Exception:
                pass


    def _force_repaint(self):
        """拖动期间强制重绘（``setWindowOpacity`` 在 Windows 上走 layered window，
        窗口被移动后旧位置不会自动失效 → 不重绘会留残影）。任何异常都不许冒泡。"""
        try:
            self.update()
        except Exception:
            pass
        try:
            self.repaint()
        except Exception:
            pass

    def _try_start_system_move(self) -> bool:
        """把本次拖动交给系统：``QWindow.startSystemMove()``（Qt>=5.15）。"""
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
        """仅记录左键按下位置（手动回退用），并在拖动开始时完整重绘一次。"""
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPos() - self.frameGeometry().topLeft()
            self._drag_system_done = False
            self._force_repaint()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        """左键拖动：优先系统拖动，拿不到才回退手动 ``move()``。"""
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            handled = False
            if not self._drag_system_done:
                handled = self._try_start_system_move()
                if handled:
                    self._drag_system_done = True
            if not handled:
                self.move(event.globalPos() - self._drag_offset)
                self.update()
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
        """窗口被**任何方式**移动后都强制重绘一次（系统拖动路径的重绘兜底）。"""
        super().moveEvent(event)
        self._force_repaint()


    def start(self, max_ms=None):
        """进入 Qt 事件循环（阻塞）。

        ``max_ms`` 为 None 时一直运行到 ``stop()`` / 窗口关闭；传毫秒数时用于自测/受控运行。
        任何异常都不允许冒泡成进程崩溃：捕获后写进状态行。
        """
        try:
            if not self.isVisible():
                self.show()
            if self.start_thread:
                self._place_right()
            application = QApplication.instance()
            if application is None:
                self.set_status("UI 事件循环不可用：QApplication 不存在")
                return None
            if max_ms:
                from PyQt5.QtCore import QTimer

                QTimer.singleShot(int(max_ms), application.quit)
            return application.exec_()
        except Exception as exc:

            import traceback

            self._start_error = "%s: %s" % (type(exc).__name__, exc)
            print("[fatal] UI 事件循环异常：%s: %s" % (type(exc).__name__, exc), flush=True)
            traceback.print_exc()
            try:
                self.set_status("UI 事件循环异常：%s" % exc)
            except Exception:
                pass
            return None

    def _confirm_close_request(self, source):
        """非模态二次确认，系统关闭也受保护，不抢游戏焦点。"""
        now = time.monotonic()
        last = self._close_click_at
        if last is not None and 0 <= now - last <= 2.5:
            print(f"[exit] {source}：2.5s 内再次请求 → 确认退出", flush=True)
            return True
        self._close_click_at = now
        print(f"[exit] {source}：首次请求已取消，2.5s 内再次请求才退出", flush=True)
        self.close_button.setText("？")
        self.close_button.setToolTip("2.5 秒内再关闭一次才退出（防误触）")
        from PyQt5.QtCore import QTimer
        QTimer.singleShot(2500, lambda: self._reset_close_confirmation(now))
        return False

    def _reset_close_confirmation(self, token):
        if self._closed or self._stopping or self._close_click_at != token:
            return
        self._close_click_at = None
        self.close_button.setText("✕")
        self.close_button.setToolTip("关闭并退出（需要二次确认）")

    def _on_close_clicked(self, *_args):
        if self._confirm_close_request("关闭按钮"):
            self.close()

    def stop(self):
        """退出事件循环（不销毁窗口）。"""
        print("[exit] stop() 被调用", flush=True)
        self._stopping = True
        try:
            application = QApplication.instance()
            if application is not None:
                application.quit()
        except Exception:
            pass

    def close(self):
        """代码主动关闭（测试、已确认按钮）不重复询问。"""
        self._allow_close = True
        self._closed = True
        self._close_dialogs()
        self.stop()
        try:
            super().close()
        except Exception:
            pass

    def nativeEvent(self, event_type, message):

        if os.name == "nt" and event_type in (b"windows_generic_MSG", "windows_generic_MSG"):
            try:
                import ctypes
                from ctypes import wintypes
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == 0x0011:
                    self._system_shutdown = True
                    print("[exit] Windows 正在查询关机/注销，允许结束会话", flush=True)
                    return True, 1
                if msg.message == 0x0016:
                    self._system_shutdown = bool(msg.wParam)
                    if self._system_shutdown:
                        print("[exit] Windows 会话结束", flush=True)
                        self.stop()
            except Exception:
                pass
        return False, 0

    def closeEvent(self, event):
        spontaneous = bool(event.spontaneous())
        print(f"[exit] closeEvent spontaneous={spontaneous} system_shutdown={self._system_shutdown}", flush=True)
        if spontaneous and not self._allow_close and not self._system_shutdown:
            if not self._confirm_close_request("系统窗口关闭请求"):
                event.ignore()
                return
        self._closed = True
        self._close_dialogs()
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
