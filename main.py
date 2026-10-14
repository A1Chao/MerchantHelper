"""行商助手 · 集成层 main.py

外部感知流程（严格只读）：
    鼠标左键按下 → 光标附近截图（占格/朝向） + 系统环回音频最新窗口
    → recognition.SoundMatcher.match  →  candidates.CandidateEngine.resolve
    → PyQt5 独立悬浮窗候选列表

硬边界（本项目红线）：
    * 不读游戏内存、不注入进程、不改游戏文件、不解包游戏资源
    * 不模拟键鼠（无 SendInput / mouse_event / keybd_event）、不自动点击、不自动购买
    * 不联网、不自学习、不热更新
    * 鼠标仅通过只读 API 读取：GetAsyncKeyState / GetCursorPos
    * UI 是独立置顶悬浮窗，绝不覆盖在游戏画面之上

用法：
    D:\\PY\\python.exe main.py --list-devices
    D:\\PY\\python.exe main.py                 # 自动选择系统默认播放设备
    D:\\PY\\python.exe main.py --device "扬声器 (2- Realtek(R) Audio)"
    D:\\PY\\python.exe main.py --no-vision     # 只做音频识别
    D:\\PY\\python.exe main.py --pitch 85      # 手动指定格宽（权威值，原样传给视觉）

格宽（pitch）策略：
    * 给了 `--pitch`：它**就是**权威值，原样传给视觉（`detect_press_item(..., pitch=85)`）；
    * 没给：传 `pitch=None`，由**视觉在光标周围自测**（`DragInfo.pitch`）。
    旧设计「整屏测一次格宽、缓存成整场权威」**已废弃**：整屏实测会被我们自己的置顶
    面板污染（真机实测 127.8，真值 85），错值会让物品边框 87x171px 吸附不成整数格。
    集成层现在只在 `[pitch]` 行**报告**格宽来源（caller / 视觉局部 / 未测到），绝不回传。

我方面板矩形：
    * `ui.Panel.screen_rect()` 给出面板在屏幕物理像素下的 (x, y, w, h)（与 GetCursorPos /
      ImageGrab 同坐标系）；集成层每个触发把它换算到当前截图的像素坐标后作为
      `ignore_rects` 传给视觉，让面板不参与网格线/边框判定；
    * 光标落在面板矩形内的**鼠标触发**直接跳过（`[skip] 触发落在面板内`），不抓图不判格。
"""
from __future__ import annotations
    
import argparse
import ctypes
import inspect
import queue
import re
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for _p in (str(ROOT), str(ROOT / "deps")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ASSETS = ROOT / "assets"
DATABASE = ASSETS / "database.json"

import numpy as np

VK_LBUTTON = 0x01
_user32 = ctypes.windll.user32

MIN_VISION_CONFIDENCE = 0.55

GOOD_ENOUGH_SCORE = 85

_PITCH_IN_REASON = re.compile(r"格宽[=＝]?\s*([0-9]+(?:\.[0-9]+)?)")

_PITCH_SUPPORT = re.compile(r"support=(\d+)")


def _pitch_src_kind(pitch_source) -> str:
    """把 `vision.DragInfo.pitch_source` 归类（纯函数，只读字符串）。

    取值：
      ``caller``       = 调用方权威（`--pitch`，原样透传）；
      ``local_scored`` = 视觉在光标周围自洽打分（`局部自洽打分(support=N)`）；
      ``local_icons``  = 视觉按图标间距自估（`icons`，红块路径用）；
      ``fallback``     = 视觉估不出来 → 回退常量并已标「不确定」；
      ``unknown``      = 认不出的旧串（`网格线(...)` 等）→ 保持旧标注「视觉局部」。
    """
    src = str(pitch_source or "").strip()
    if not src or src == "none":
        return "unknown"
    low = src.lower()
    if "常量回退" in src or "不确定" in src or low == "grid_constant":
        return "fallback"
    if "caller" in low:
        return "caller"
    if "局部自洽" in src:
        return "local_scored"
    if low == "icons":
        return "local_icons"
    return "unknown"


def _pitch_src_label(pitch_source) -> str:
    """`[pitch]` 行的 `来源=` 取值：caller / caller权威 / 视觉局部 / 常量回退(不确定)。"""
    kind = _pitch_src_kind(pitch_source)
    if kind == "fallback":
        return "常量回退(不确定)"
    if kind == "caller":
        return "caller权威"
    return "视觉局部"


def _set_process_priority() -> bool:
    """整进程提到 ABOVE_NORMAL，降低游戏把采集线程挤掉导致丢帧的概率。"""
    try:
        ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
        k32 = ctypes.windll.kernel32
        return bool(k32.SetPriorityClass(k32.GetCurrentProcess(), ABOVE_NORMAL_PRIORITY_CLASS))
    except Exception:
        return False


def _lower_current_thread_priority() -> bool:
    """把当前（识别工作）线程降到 BELOW_NORMAL：让采集线程优先拿到 CPU。"""
    try:
        THREAD_PRIORITY_BELOW_NORMAL = -1
        k32 = ctypes.windll.kernel32
        return bool(k32.SetThreadPriority(k32.GetCurrentThread(), THREAD_PRIORITY_BELOW_NORMAL))
    except Exception:
        return False


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def left_button_down() -> bool:
    """只读左键状态，绝不写入。"""
    return bool(_user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)


VK_F8 = 0x77


def f8_down() -> bool:
    """只读 F8 状态（不注册系统热键、不模拟按键，只读全局键态）。

    用途：游戏内不方便点面板时的"开始/停止监测"快捷键。因为只读键态，
    即使游戏窗口在前台、甚至游戏自己占用了 F8，这里依然读得到物理按键。
    """
    return bool(_user32.GetAsyncKeyState(VK_F8) & 0x8000)


def cursor_pos() -> tuple[int, int]:
    """只读光标位置，绝不移动光标。"""
    pt = _POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def _field(obj, key, default=None):
    """兼容 dataclass / dict 两种 DragInfo 实现。"""
    if obj is None:
        return default
    try:
        return obj[key]
    except Exception:
        return getattr(obj, key, default)


TRIG_PRESS = "鼠标上升沿"
TRIG_HOLD = "按住续触"
TRIG_RELEASE = "松开"
TRIG_AUDIO = "音频触发"

SESSION_NONE = 0

STALL_WARN_AFTER = 15.0

_IDLE_STATUS = "未开始监测：不读取游戏信息，点上方按钮或按 F8 开始。"
_MONITORING_STATUS = "监测中：等待拖动物品…"


class PressSession:
    """一次「按下」会话（用户方案：**会话锁**）。

    * ``anchor`` = 按下沿那一刻的光标（会话锚点）；无锚点会话为 ``None``；
    * ``locked`` = 会话内**首次成功**的视觉结果 ``{slots, orientation, source, rect,
      pitch, pitch_tag, confidence, cursor}`` —— 一旦写入，本次会话内**不再改写**：
      鼠标掠过别的物品只报告、不覆盖，必须重新按下才能换目标。

    没有它的时候，每个受理的触发都拿"处理时刻的光标"重判并覆盖面板 → 拖 A 时
    鼠标掠过 B，续触/音频触发就把面板跳到 B（用户实测：「我选了 A，鼠标不小心指向
    B，最后展示会变」）。
    """

    __slots__ = ("id", "anchor", "locked", "hold_vis")

    def __init__(self, sid: int, anchor=None):
        self.id = int(sid)
        self.anchor = None if anchor is None else (int(anchor[0]), int(anchor[1]))
        self.locked = None
        self.hold_vis = None


def _split_trigger(payload):
    """触发队列元素归一化（**唯一归一化点**）→ ``(pressed_at, source, cursor_xy, session_id)``。

    当前元素 = ``(时间戳, 来源, 发生时刻的光标, 会话 id)``；**继续兼容**：
      * ``None``（停止哨兵）→ ``(None, "", None, 0)``；
      * 裸时间戳（旧代码/旧探针）→ 光标 ``None``（回退实时光标）、会话 ``0``（不加锁）；
      * 旧 2 元组 ``(ts, source)`` → 同样缺字段回退。
    缺失光标 = "用实时光标"（今天的行为）；缺失会话 id = "无会话、不加锁"。
    """
    if payload is None:
        return None, "", None, SESSION_NONE
    if isinstance(payload, (tuple, list)):
        try:
            pressed_at = float(payload[0])
        except (TypeError, ValueError, IndexError):
            return None, "", None, SESSION_NONE
        source = str(payload[1]) if len(payload) >= 2 else ""
        cursor = None
        if len(payload) >= 3 and payload[2] is not None:
            try:
                cursor = (int(payload[2][0]), int(payload[2][1]))
            except (TypeError, ValueError, IndexError):
                cursor = None
        session_id = SESSION_NONE
        if len(payload) >= 4:
            try:
                session_id = int(payload[3] or 0)
            except (TypeError, ValueError):
                session_id = SESSION_NONE
        return pressed_at, source, cursor, session_id
    try:
        return float(payload), "", None, SESSION_NONE
    except (TypeError, ValueError):
        return None, "", None, SESSION_NONE


def _is_admin():
    """本进程是否以管理员身份运行 → ``True`` / ``False`` / ``None``（查不到）。

    用 ``shell32.IsUserAnAdmin()``：只读查询自己的令牌，**不打开任何进程、
    不读别人内存、不提权**（AST 审计的红线 API 一个都不碰）。
    """
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return None


def _print_env_banner() -> bool:
    """启动横幅：打印管理员权限；不是管理员时明确提示游戏内左键可能读不到。

    返回是否以管理员运行（``None`` 视为"未知/未确认"）。这是本次实测的核心结论之一：
    游戏以更高权限运行时（UIPI），本进程读不到游戏窗口的按键状态，
    表现为心跳里的 ``左键 看到0`` 或 "看到N次但受理0"。
    """
    admin = _is_admin()
    if admin is True:
        print("[env] 管理员权限 = 是（游戏若也以管理员运行，游戏内左键可正常读取）", flush=True)
    elif admin is False:
        print("[env] 管理员权限 = 否", flush=True)
        print("[env] ⚠️ 未以管理员身份运行：游戏若以更高权限运行，游戏内左键可能**完全读不到**"
              "（UIPI 拦截）。处置：右键『以管理员身份运行』重开本工具，"
              "或改用 --trigger audio / both（音频触发不依赖按键）。", flush=True)
    else:
        print("[env] 管理员权限 = 未知（IsUserAnAdmin 不可用）", flush=True)
    return admin


def _weak_candidates(matcher, samples, sr: int, limit: int, background_rms: float) -> list:
    """未达阈值时给出"最像的几名 + 分数"（低置信参考，不改变任何阈值判定）。

    直接用 matcher 的排序结果（`_rank`）：原始路径 + 高通路径，同一物品取较高分。
    """
    try:
        import math

        import recognition
        import scipy.signal as signal

        x = np.asarray(samples, dtype=np.float32).reshape(-1)
        if sr != recognition.SAMPLE_RATE and len(x):
            g = math.gcd(int(sr), recognition.SAMPLE_RATE)
            x = signal.resample_poly(x, recognition.SAMPLE_RATE // g, int(sr) // g).astype(np.float32)
        if len(x) < 128:
            return []
        rows = list(matcher._rank(recognition.extract(x, reduce_background=True), background_rms))
        if 640 <= len(x) <= 48000:
            hp = matcher._highpass(x)
            rows += list(matcher._rank(recognition.extract(hp, reduce_background=True), 0.0,
                                       filtered=True))
        best: dict = {}
        for r in rows:
            k = r.get("key")
            if k is None:
                continue
            if k not in best or r.get("score", 0) > best[k].get("score", 0):
                best[k] = r
        return sorted(best.values(), key=lambda r: -float(r.get("score", 0)))[: max(1, limit)]
    except Exception:
        return []


class AudioFeed:
    def __init__(self, device_name: str | None = None, history: int = 12,
                 trigger_queue=None, trigger_mode: str = "mouse",
                 min_interval: float = 0.9, blocksize_seconds: float | None = None,
                 active_gate=None):
        self.blocksize_seconds = blocksize_seconds
        self._active_gate = active_gate
        self.device_name = device_name
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._latest = None
        self._windows = 0
        self._peak_rms = 0.0
        self.last_error: str | None = None
        self._thread: threading.Thread | None = None
        self._history: list = []
        self._history_max = max(2, int(history))
        self._profile = None
        self._profile_job = None
        self._profile_lock = threading.Lock()
        self._trigger_queue = trigger_queue
        self._trigger_mode = trigger_mode
        self._min_interval = float(min_interval)
        self._last_audio_trigger = 0.0
        self.audio_triggers = 0
        self.session_provider = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="audio-feed", daemon=True)
        self._thread.start()

    def _refresh_profile(self) -> None:
        """在后台线程里重算长期背景谱（约 10ms，不能让采集线程承担）。"""
        try:
            import denoise

            with self._lock:
                windows = [w for (w, _b, _t) in self._history]
            profile = denoise.noise_psd_profile(windows)
            with self._profile_lock:
                if profile is not None:
                    self._profile = profile
        except Exception:
            pass

    def _run(self) -> None:
        try:
            import recorder

            source = recorder.listen_windows(
                self.stop_event, self.device_name,
                blocksize_seconds=getattr(self, "blocksize_seconds", None),
                gate=getattr(self, "_active_gate", None))
            for item in source:
                if isinstance(item, tuple) and len(item) == 3:
                    window, _sr, background_rms = item
                else:
                    window, background_rms = item
                window = np.asarray(window, dtype=np.float32)
                rms = float(np.sqrt(np.mean(window.astype(np.float64) ** 2)))
                with self._lock:
                    self._latest = (window.copy(), float(background_rms), time.time())
                    self._windows += 1
                    if rms > self._peak_rms:
                        self._peak_rms = rms
                    self._history.append((window.copy(), float(background_rms), time.time()))
                    if len(self._history) > self._history_max:
                        del self._history[0:len(self._history) - self._history_max]
                    need_profile = self._windows >= 4
                if need_profile and (self._profile is None or self._windows % 4 == 0):
                    if self._profile_job is None or not self._profile_job.is_alive():
                        self._profile_job = threading.Thread(
                            target=self._refresh_profile, name="noise-profile", daemon=True)
                        self._profile_job.start()
                if self._trigger_mode in ("audio", "both"):
                    if self._active_gate is None or self._active_gate.is_set():
                        self._maybe_audio_trigger(window, float(background_rms))
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"

    def stop(self) -> None:
        self.stop_event.set()

    def snapshot(self):
        """返回 (window, background_rms, age_seconds)；无数据返回 None。"""
        with self._lock:
            if self._latest is None:
                return None
            window, background_rms, ts = self._latest
        return window, background_rms, max(0.0, time.time() - ts)

    @property
    def profile(self):
        """长期背景谱（denoise 用）；尚未算出时返回 None。"""
        with self._profile_lock:
            return self._profile

    def reference_for(self, pressed_at: float, min_gap: float = 0.15):
        """挑一个"按下之前"的最安静窗口做背景参考；没有合适的返回 None。

        为什么用"最安静"而不是"最近"：按下前后游戏可能已经在播别的音效，
        最安静的窗口最可能只含持续背景，谱减时不会把音效本身减掉。
        """
        with self._lock:
            items = list(self._history)
        older = [it for it in items if it[2] <= pressed_at - min_gap]
        if not older:
            return None
        return min(older, key=lambda it: it[1])[0]

    def _maybe_audio_trigger(self, window, background_rms: float) -> None:
        """音频触发：听到"突起的短音效"就当成一次拖动（鼠标被拦时的替代方案）。

        判据只用**相对**量：窗口内 20ms 帧峰值 RMS 相对该窗背景电平的倍数。
        宁可多触发（多触发只是多一次"没有候选"的日志），也不漏掉音效。
        """
        if self._trigger_queue is None:
            return
        now = time.time()
        if now - self._last_audio_trigger < self._min_interval:
            return
        try:
            w = np.asarray(window, dtype=np.float32).reshape(-1)
            frame = 882
            usable = w[: len(w) // frame * frame].reshape(-1, frame)
            if usable.size == 0:
                return
            peak = float(np.sqrt(np.mean(usable.astype(np.float64) ** 2, axis=1)).max())
        except Exception:
            return
        threshold = max(0.0012, float(background_rms) * 3.5)
        if peak < threshold:
            self._prev_peak = peak
            return
        if float(getattr(self, "_prev_peak", 0.0)) >= threshold * 0.6:
            self._prev_peak = peak
            return
        self._prev_peak = peak
        self._last_audio_trigger = now
        self.audio_triggers += 1
        cursor, session_id = None, SESSION_NONE
        provider = getattr(self, "session_provider", None)
        if provider is not None:
            try:
                cursor, session_id = provider()
            except Exception:
                cursor, session_id = None, SESSION_NONE
        try:
            self._trigger_queue.put_nowait((now, TRIG_AUDIO, cursor, session_id))
        except Exception:
            pass

    @property
    def windows(self) -> int:
        with self._lock:
            return self._windows

    @property
    def peak_rms(self) -> float:
        with self._lock:
            return self._peak_rms

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def discontinuities(self) -> int:
        """采集丢帧（WASAPI data discontinuity）累计次数；0 = 采集健康。"""
        try:
            import recorder

            return int(recorder.discontinuities())
        except Exception:
            return 0


class Engine:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.matcher = None
        self.candidate_engine = None
        self.active = threading.Event()
        if getattr(args, "autostart", False):
            self.active.set()
        self.triggers: queue.Queue = queue.Queue(maxsize=2)
        self.results: queue.Queue = queue.Queue()
        self.audio = AudioFeed(args.device, trigger_queue=self.triggers,
                               trigger_mode=getattr(args, "trigger", "mouse"),
                               min_interval=args.min_interval,
                               blocksize_seconds=getattr(args, "audio_blocksize", None),
                               active_gate=self.active)
        self.audio.session_provider = self._audio_session
        self.stop_event = threading.Event()
        self.stats = {"triggers": 0, "matches": 0, "empty": 0, "errors": 0, "vision_fail": 0,
                      "presses_seen": 0, "presses_accepted": 0, "watch_errors": 0,
                      "hold_retriggers": 0, "release_triggers": 0, "vision_highlight": 0,
                      "sound_found": 0, "panel_skips": 0,
                      "hold_samples": 0, "hold_hits": 0}
        self._watcher: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self._heartbeat: threading.Thread | None = None
        self._last_trigger = 0.0
        self._vdebug_ready = False
        self._press_item_missing = False
        self._monitor_since = None
        self._stall_warned_session = -1
        self._poll_gap_max_ms = 0.0
        self._last_poll_ts = None
        self.last_admin = None
        self._session_lock = threading.RLock()
        self._session_seq = 0
        self._open_session = SESSION_NONE
        self._sessions: dict = {}
        self._hold_stop = threading.Event()
        self._hold_thread: threading.Thread | None = None
        self._hold_gate = threading.Lock()
        self.panel = None
        self._panel_lock = threading.Lock()
        self._panel_rect: tuple | None = None
        self._panel_rect_reported = False

    def load(self) -> float:
        import candidates as candidates_mod
        import recognition

        t0 = time.time()
        self.matcher = recognition.SoundMatcher(str(ASSETS), str(DATABASE))
        self.candidate_engine = candidates_mod.CandidateEngine(str(DATABASE))
        elapsed = time.time() - t0
        print(
            f"[engine] 模板={len(self.matcher.templates)} 错误={len(self.matcher.errors)} "
            f"加载耗时={elapsed:.2f}s",
            flush=True,
        )
        for err in self.matcher.errors[:5]:
            print(f"[engine] 模板错误: {err}", flush=True)
        self._report_vision_api()
        return elapsed

    def _report_vision_api(self) -> None:
        """启动时报告「按下判定」用的是哪套视觉接口（缺失时明确退化路径）。"""
        try:
            import vision

            if getattr(vision, "detect_press_item", None) is None:
                self._press_item_missing = True
                print("[vision] 边框检测未就绪（vision.detect_press_item 缺失）："
                      "按下判定退化为「红块/无来源」，**不会**回退到几何猜测", flush=True)
            else:
                print("[vision] 按下判定 = detect_press_item（红块优先 → 物品边框兜底）",
                      flush=True)
        except Exception as exc:
            print(f"[vision] 接口探测失败: {type(exc).__name__}: {exc}", flush=True)

    def bind_panel(self, panel) -> None:
        """绑定 UI 面板：用于拿「面板屏幕矩形」，屏蔽给视觉并跳过落在面板内的触发。

        只保存引用，**不**在这里读几何（几何读取要么由 UI 线程 `set_panel_rect` 上报，
        要么在自测里由主线程直接调用 `panel.screen_rect()`）。
        """
        self.panel = panel

    def set_panel_rect(self, rect) -> None:
        """由 **UI 线程**（main() 的 80ms 轮询）上报面板屏幕矩形；工作线程只读该缓存。

        为什么不让工作线程直接读 Qt：`frameGeometry()` 属 GUI 对象，跨线程访问不安全。
        传 ``None`` = 面板当前拿不到几何（隐藏/未就绪）；此后不再回落到现场读取。
        """
        value = None
        if rect is not None:
            try:
                x, y, w, h = (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
                if w > 0 and h > 0:
                    value = (x, y, w, h)
            except Exception:
                value = None
        with self._panel_lock:
            self._panel_rect = value
            self._panel_rect_reported = True

    def _panel_screen_rect(self):
        """面板在屏幕坐标下的矩形 ``(x, y, w, h)``；没有面板/拿不到返回 ``None``。只读。"""
        with self._panel_lock:
            if self._panel_rect_reported:
                return self._panel_rect
        panel = self.panel
        if panel is None:
            return None
        fn = getattr(panel, "screen_rect", None)
        if fn is None:
            return None
        try:
            rect = fn()
        except Exception as exc:
            print(f"[panel] 面板矩形读取失败: {type(exc).__name__}: {exc}", flush=True)
            return None
        if not rect:
            return None
        try:
            x, y, w, h = (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
        except Exception:
            return None
        return (x, y, w, h) if w > 0 and h > 0 else None

    @staticmethod
    def _point_in_rect(px: float, py: float, rect) -> bool:
        """点是否落在矩形内（左上闭、右下开）。rect = (x, y, w, h)。"""
        x, y, w, h = rect
        return x <= px < x + w and y <= py < y + h

    def _panel_skip_enabled(self) -> bool:
        """面板内触发跳过是否生效。

        只在**鼠标参与触发**的模式下生效（mouse/both）：audio 模式的光标位置与触发
        毫无关系，跳过会误伤真实拖动音效。
        """
        return str(getattr(self.args, "trigger", "mouse")) in ("mouse", "both")

    def is_active(self) -> bool:
        """是否处于「开始监测」状态。"""
        return bool(self.active.is_set())

    def set_active(self, on: bool) -> bool:
        """开始/停止监测。停止时：不截屏、不识别、不保存任何文件。

        Returns 新状态。
        """
        on = bool(on)
        if on == self.active.is_set():
            return on
        if on:
            self.active.set()
            self.stats["started"] = self.stats.get("started", 0) + 1
            self._monitor_since = time.time()
            self._poll_gap_max_ms = 0.0
            self._last_poll_ts = None
            print("[gate] ▶ 已开始监测：从现在起才截图/听音/识别（之前的音频已丢弃）",
                  flush=True)
        else:
            self.active.clear()
            self._monitor_since = None
            self._press_session_end()
            try:
                while True:
                    self.triggers.get_nowait()
            except queue.Empty:
                pass
            print("[gate] ■ 已停止监测：不再截屏、不再识别、不再保存文件", flush=True)
        return on

    def start_threads(self) -> None:
        self.audio.start()
        self._worker = threading.Thread(target=self._work_loop, name="match-worker", daemon=True)
        self._worker.start()
        if getattr(self.args, "trigger", "mouse") in ("mouse", "both"):
            self._watcher = threading.Thread(target=self._watch_loop, name="mouse-watcher",
                                             daemon=True)
            self._watcher.start()
        self._heartbeat = threading.Thread(target=self._heartbeat_loop, name="heartbeat",
                                           daemon=True)
        self._heartbeat.start()

    def _watch_loop(self) -> None:
        interval = self.args.poll
        hold_interval = float(getattr(self.args, "hold_interval", 0.25) or 0.25)
        _min_iv = float(getattr(self.args, "min_interval", 0.9) or 0.0)
        hold_interval = 0.0 if _min_iv <= 0 else min(hold_interval, _min_iv)
        was_down = False
        f8_was = False
        edges = str(getattr(self.args, "trigger_edges", "both"))
        while not self.stop_event.is_set():
            try:
                iter_now = time.time()
                if self._last_poll_ts is not None:
                    gap_ms = (iter_now - self._last_poll_ts) * 1000.0
                    if gap_ms > self._poll_gap_max_ms:
                        self._poll_gap_max_ms = gap_ms
                self._last_poll_ts = iter_now

                f8 = f8_down()
                if f8 and not f8_was:
                    self.set_active(not self.active.is_set())
                f8_was = f8

                down = left_button_down()
                now = time.time()
                is_press = down and not was_down
                is_release = (not down) and was_down
                if is_press:
                    self.stats["presses_seen"] += 1

                if not self.active.is_set():
                    was_down = down
                    time.sleep(interval)
                    continue

                try:
                    here_cursor = cursor_pos()
                except Exception:
                    here_cursor = None
                if is_press:
                    sid = self._press_session_new(here_cursor)
                else:
                    sid = self._open_session_id()

                if is_press:
                    self._hold_sampler_start()
                    self._last_trigger = now
                    self.stats["presses_accepted"] += 1
                    self._enqueue_trigger(now, source=TRIG_PRESS,
                                          cursor_xy=here_cursor, session_id=sid)
                elif down and not is_press and edges in ("both", "press") \
                        and now - self._last_trigger >= hold_interval:
                    self._last_trigger = now
                    self.stats["hold_retriggers"] += 1
                    self._enqueue_trigger(now, low_priority=True, source=TRIG_HOLD,
                                          cursor_xy=here_cursor, session_id=sid)
                elif is_release and edges != "press":
                    self._last_trigger = now
                    self.stats["release_triggers"] += 1
                    self._enqueue_trigger(now, low_priority=True, source=TRIG_RELEASE,
                                          cursor_xy=here_cursor, session_id=sid)
                if is_release:
                    self._hold_sampler_stop()
                    self._press_session_end()
                was_down = down
            except Exception as exc:
                self.stats["watch_errors"] += 1
                if self.stats["watch_errors"] <= 3:
                    print(f"[watch] 按键轮询失败（第 {self.stats['watch_errors']} 次）："
                          f"{type(exc).__name__}: {exc}", flush=True)
            time.sleep(interval)

    def _enqueue_trigger(self, ts: float, low_priority: bool = False, source: str = "",
                         cursor_xy=None, session_id: int = SESSION_NONE) -> None:
        """入队一次触发；低优先级（按住续触/松开）在已有待处理项时直接跳过。

        元素 = ``(时间戳, 触发来源, 发生时刻的光标, 会话 id)``：
          * 来源/光标**随触发一起入队** —— 队列会丢低优先级项、worker 还有 --settle
            延迟，事后用"当前状态/当前光标"反推必然错位；
          * 会话 id 让"同一次按下"的续触/松开/音频触发都能找到同一个会话（会话锁）。
        旧调用（只给 ts/source）仍合法：光标=None（回退实时光标）、会话=0（不加锁）。

        为什么要这样：一次识别最多要听 --listen-max 秒 + 视觉轮询，而按住拖动时
        每 --min-interval 秒就会产生一次触发；不控制就会无限积压、面板延迟越来越大。
        """
        if low_priority and not self.triggers.empty():
            sess = self._press_session_get(session_id)
            if sess is None or getattr(sess, "locked", None) is not None:
                return
        item = (float(ts), str(source or ""), cursor_xy, int(session_id or SESSION_NONE))
        try:
            self.triggers.put_nowait(item)
        except queue.Full:
            try:
                self.triggers.get_nowait()
            except queue.Empty:
                pass
            try:
                self.triggers.put_nowait(item)
            except queue.Full:
                pass

    def _press_session_new(self, anchor=None) -> int:
        """按下沿开一个新会话（锚点 = 按下那一刻的光标），返回会话 id。"""
        with self._session_lock:
            self._session_seq += 1
            sid = self._session_seq
            self._sessions[sid] = PressSession(sid, anchor)
            self._open_session = sid
            if len(self._sessions) > 8:
                for old in sorted(self._sessions)[:-8]:
                    if old != self._open_session:
                        self._sessions.pop(old, None)
            return sid

    def _press_session_end(self) -> None:
        """松开：当前会话结束（后续音频触发不再继承它 → 回到"不加锁"行为）。"""
        with self._session_lock:
            self._open_session = SESSION_NONE

    def _open_session_id(self) -> int:
        with self._session_lock:
            return int(self._open_session)

    def _press_session_get(self, session_id):
        """按 id 取会话；``0``（无会话）或已淘汰 → ``None``（= 不加锁）。"""
        try:
            sid = int(session_id or 0)
        except (TypeError, ValueError):
            return None
        if sid <= 0:
            return None
        with self._session_lock:
            return self._sessions.get(sid)

    def _hold_sampler_start(self) -> None:
        """按下沿：启动专用采样线程（已在跑则不重复启动）。"""
        if not bool(getattr(self.args, "hold_sample", True)):
            return
        th = self._hold_thread
        if th is not None and th.is_alive():
            return
        self._hold_stop.clear()
        th = threading.Thread(target=self._hold_sample_loop, name="hold-sampler", daemon=True)
        self._hold_thread = th
        th.start()

    def _hold_sampler_stop(self) -> None:
        """松开：请求采样线程退出并等它收尾。

        采到的结果早已写进 `PressSession.hold_vis`，所以**松开那次触发**（队列里排在后面）
        照样能用上它 —— 不需要把结果留在线程里。
        """
        self._hold_stop.set()
        th = self._hold_thread
        if th is not None and th.is_alive():
            th.join(timeout=0.8)
        self._hold_thread = None

    def _hold_sample_loop(self) -> None:
        """按住期间 ~10Hz：整屏抓图 → 找红块 → **采到就写进当前会话**。

        触发是"在某一刻取样"，而红块只在拖动过程中存在；快拖时那一刻可能已经放下。
        这里把取样窗口从"触发那一刻"扩成"整个按住期间"：**只要红块出现过一次就抓得到**。
        结果交给会话锁在"本次触发自己没判出"时兜底启用（见 `_apply_session_lock`）。
        """
        import vision

        rate = float(getattr(self.args, "hold_sample_rate", 0.1) or 0.1)
        rate = max(0.03, min(1.0, rate))
        while not self._hold_stop.is_set() and not self.stop_event.is_set():
            try:
                if self.active.is_set() and not self.args.no_vision:
                    self._hold_sample_once(vision)
            except Exception as exc:
                print(f"[hold] 采样失败: {type(exc).__name__}: {exc}", flush=True)
            self._hold_stop.wait(rate)

    def _hold_sample_once(self, vision) -> None:
        """一次采样：整屏 → 红块；命中则按置信度择优写进当前会话。"""
        sid = self._open_session_id()
        if sid <= 0:
            return
        sess = self._press_session_get(sid)
        if sess is None:
            return
        try:
            cur = cursor_pos()
        except Exception:
            return
        image_f, origin_f = vision.capture_full_screen()
        self.stats["hold_samples"] += 1
        cur_f = (int(cur[0]) - int(origin_f[0]), int(cur[1]) - int(origin_f[1]))
        full_pad = int(getattr(self.args, "pad_max", 0) or self.args.pad)
        pitch_hint = None if self.args.no_vision else self._pitch_arg()
        try:
            ignore_full = self._ignore_rects_in_image(self._panel_screen_rect(), origin_f)
        except Exception:
            ignore_full = []
        drag_f, src = self._press_from_fullscreen(vision, image_f, cur_f, full_pad,
                                                  pitch_hint, ignore_full)
        if drag_f is None or str(src) != "红块":
            return
        slots = int(_field(drag_f, "slots", 0) or 0)
        if slots <= 0:
            return
        conf = float(_field(drag_f, "confidence", 0.0) or 0.0)
        cand = {"slots": slots,
                "orientation": str(_field(drag_f, "orientation", "") or ""),
                "source": "红块",
                "rect": _field(drag_f, "rect", None),
                "pitch": float(_field(drag_f, "pitch", 0.0) or 0.0),
                "pitch_tag": "",
                "confidence": conf,
                "cursor": (int(cur[0]), int(cur[1]))}
        with self._hold_gate:
            old = sess.hold_vis
            if old is None or conf > float(old.get("confidence") or 0.0):
                sess.hold_vis = cand
                self.stats["hold_hits"] += 1
                print(f"[hold] 按住采样命中: {slots}格{cand['orientation'] or '-'} "
                      f"conf={conf:.2f}（第{self.stats['hold_samples']}次采样，"
                      f"来源=红块 光标=({cur[0]},{cur[1]})）", flush=True)

    def _audio_session(self):
        """音频触发入队时被 `AudioFeed` 调用 → ``(发生时刻光标, 会话 id)``。

        按键按下期间继承当前按下会话（可加锁）；按钮未按下 → 会话 0（不加锁，
        跟随当前光标 = 与今天完全一致的行为）。
        """
        try:
            cursor = cursor_pos()
        except Exception:
            cursor = None
        return cursor, self._open_session_id()

    def _lock_tag(self, session, verb: str = "已锁") -> str:
        """`已锁 2格V@(1500,700) 格宽=85.0(视觉局部)`（供 [trace]/状态行显示）。"""
        locked = getattr(session, "locked", None)
        if not locked:
            return ""
        tag = f"{verb} {int(locked.get('slots', 0))}格{locked.get('orientation') or '-'}"
        pos = locked.get("cursor")
        if pos:
            tag += f"@({pos[0]},{pos[1]})"
        pitch_tag = str(locked.get("pitch_tag") or "").strip()
        if pitch_tag:
            tag += " " + pitch_tag
        return tag

    def _apply_session_lock(self, session, this: dict, trigger_src: str) -> dict:
        """会话锁核心：**首次成功才锁定**，锁定后一律用锁定值，绝不覆盖。

        ``this`` = 本次触发（在它携带的光标位置上）判出来的东西：
        ``{slots, orientation, source, confidence, cursor, rect, pitch, pitch_tag}``。

        返回 ``{use_locked, slots, orientation, source, confidence, tag, note}``：
          * 无会话（``session is None``）→ 原样直通（纯音频触发不加锁，保持今天行为）；
          * 会话未锁且本次成功 → 写入 ``session.locked``（本次结果就是锁定值）；
          * 会话已锁 → 用锁定值；本次判出不同的东西只打一行 ``[lock]``（只报告、不覆盖）；
            本次什么都没判出 → 同样保持锁定值。

        为什么要"锁"：用户实测「我选了 A，鼠标不小心指向 B，最后展示会变」——
        按住拖动时鼠标必然经过别的格子，每次续触都按当前位置重判就会把 A 顶掉。
        """
        out = {"use_locked": False, "slots": this["slots"], "orientation": this["orientation"],
               "source": this["source"], "confidence": this["confidence"],
               "tag": "", "note": ""}
        if session is None:
            return out
        ls, lo, lsrc = int(this["slots"]), str(this["orientation"] or ""), str(this["source"] or "")
        tpos = this.get("cursor") or (0, 0)
        success = ls > 0 and lsrc != ""
        if session.locked is None:
            if not success:
                hv = getattr(session, "hold_vis", None)
                if not hv:
                    return out
                session.locked = dict(hv)
                out.update(use_locked=True,
                           slots=int(hv["slots"]),
                           orientation=str(hv.get("orientation") or ""),
                           source=str(hv.get("source") or "红块"),
                           confidence=float(hv.get("confidence") or 0.0),
                           tag=self._lock_tag(session, "已锁(按住采样)"),
                           note="【按住采样兜底】")
                return out
            session.locked = {"slots": ls, "orientation": lo, "source": lsrc,
                              "rect": this.get("rect"), "pitch": this.get("pitch"),
                              "pitch_tag": this.get("pitch_tag", ""),
                              "confidence": float(this["confidence"]), "cursor": tpos}
            out["tag"] = self._lock_tag(session, "已锁")
            return out
        locked = session.locked
        lpos = locked.get("cursor") or (0, 0)
        pitch_txt = f" {locked['pitch_tag'].strip()}" if locked.get("pitch_tag") else ""
        differs = (ls != int(locked["slots"]) or lo != str(locked["orientation"])
                   or lsrc != str(locked["source"]))
        if differs:
            head = (f"[lock] 本次按下已锁定 {int(locked['slots'])}格"
                    f"{locked['orientation'] or '-'}@({lpos[0]},{lpos[1]}){pitch_txt}")
            tail = f"（触发源={trigger_src or '未知'}；只报告，不覆盖）"
            if success:
                print(f"{head}，忽略当前位置的 {ls}格{lo or '-'}@({tpos[0]},{tpos[1]}){tail}",
                      flush=True)
            else:
                print(f"{head}，当前位置({tpos[0]},{tpos[1]})未判出物品 → 保持锁定值{tail}",
                      flush=True)
            out["note"] = f"【忽略本次{ls}格{lo or '-'}@({tpos[0]},{tpos[1]})】"
        out.update(use_locked=True, slots=int(locked["slots"]),
                   orientation=str(locked["orientation"]), source=str(locked["source"]),
                   confidence=float(locked.get("confidence", 0.0)),
                   tag=self._lock_tag(session, "已锁"))
        return out

    def _heartbeat_loop(self) -> None:
        """每 5 秒打一行心跳：游戏内"没反应"时，用它一眼看出是哪一环断了。

        「触发被拦」的警告交给 `_stall_warning`（按监视会话只喊一次）——
        旧条件 `看到==0` 会被一次点面板按钮凑成 1 而永久沉默。
        """
        bw_warned = False
        gate_notified = False
        while not self.stop_event.is_set():
            time.sleep(5.0)
            if self.stop_event.is_set():
                break
            if not self.active.is_set():
                if not gate_notified:
                    print("[hb] 未开始监测：此刻不截屏、不录音、不识别。"
                          "点面板『▶ 开始监测』或按 F8 开始。", flush=True)
                    gate_notified = True
                bw_warned = False
                continue
            if gate_notified:
                print("[hb] ▶ 监测进行中。", flush=True)
                gate_notified = False
            seen = self.stats["presses_seen"]
            try:
                import recorder as _rec

                snap = self.audio.snapshot()
                seg = _rec.loudest_segment(snap[0]) if snap is not None else None
                bw = _rec.spectral_rolloff(seg) if seg is not None else 0.0
                seg_rms = (float(np.sqrt(np.mean(np.asarray(seg, dtype=np.float64) ** 2)))
                           if seg is not None else 0.0)
                bg_now = float(snap[1]) if snap is not None else 0.0
                sound_present = seg_rms >= max(0.0012, bg_now * 3.0)
            except Exception:
                bw, sound_present = 0.0, False
            print(self._heartbeat_text(bw), flush=True)
            if not bw_warned and sound_present and bw and bw < 9000:
                bw_warned = True
                print(f"[hb] ⚠️ 音频通路带宽只有 ≈{bw / 1000:.1f}kHz（正常应 >16kHz）："
                      "音效高频被整段砍掉了，识别必然不稳定。常见原因："
                      "① 蓝牙耳机跑在『免手持/HFP 通话』通路（改用立体声 A2DP，或在声音设置里"
                      "禁用该设备的免手持项）；② 输出设备开了音频增强/低音增强/响度均衡；"
                      "③ 设备高级选项里采样率是 16kHz。处置：换有线/Realtek 输出，"
                      "并把游戏输出与 --device 都指到同一设备后重试。", flush=True)
            stall = self._stall_warning(seen)
            if stall:
                print(stall, flush=True)

    def _heartbeat_text(self, bw: float) -> str:
        """心跳主体行（纯格式化，便于离线断言）。

        本轮新增三样诊断量，用于区分「按键读不到」与「被游戏抢 CPU 漏拍」：
          · ``轮询最大间隔``：`_watch_loop` 相邻两次迭代的最大间隔 ms；
          · ``光标``：当前光标坐标（证明本进程在游戏前台时**仍然能读到光标**）；
          · ``面板跳过``：`panel_skips`（有多少次触发落在我们自己的面板上被丢弃）。
        """
        try:
            cx, cy = cursor_pos()
        except Exception:
            cx, cy = -1, -1
        return (
            f"[hb] 左键 看到{self.stats['presses_seen']}/受理{self.stats['presses_accepted']}"
            f"（按住续触{self.stats['hold_retriggers']} 松开{self.stats['release_triggers']}）"
            f"｜轮询最大间隔{self._poll_gap_max_ms:.0f}ms 光标({cx},{cy})"
            f" 面板跳过{self.stats.get('panel_skips', 0)}"
            f"｜音频 {self.audio.windows}窗 峰值{self.audio.peak_rms:.5f}"
            f" 丢帧{self.audio.discontinuities}"
            f"｜带宽≈{bw / 1000:.1f}kHz"
            f"｜触发{self.stats['triggers']} 候选{self.stats['matches']}"
            f" 空{self.stats['empty']} 高亮{self.stats['vision_highlight']}"
            f" 错误{self.stats['errors']}"
            + (f"｜音频触发{self.audio.audio_triggers}"
               if getattr(self.args, "trigger", "mouse") in ("audio", "both") else "")
        )

    def _stall_warning(self, seen: int):
        """「游戏内触发被拦」的警告文本；不需要喊时返回 ``None``。

        旧条件是 ``看到==0``——本次实测它被**一次点面板按钮**凑成 1 而永久沉默
        （日志：``左键 看到2/受理0`` 两次心跳都一样，却没有一句警告）。
        新条件：**受理==0 且本次监测已满 ``STALL_WARN_AFTER`` 秒**，并按原因分开写：
          · ``看到==0`` → 按键完全读不到（权限/UIPI）；
          · ``看到>0`` 且都在面板内 → 你在点我们自己的面板，不是游戏里的物品。
        每次监测会话只喊一次（`set_active` 会增加 `stats["started"]`）。
        """
        if getattr(self.args, "trigger", "mouse") not in ("mouse", "both"):
            return None
        if self._monitor_since is None:
            return None
        session = int(self.stats.get("started", 0))
        if self._stall_warned_session == session:
            return None
        elapsed = time.time() - self._monitor_since
        if elapsed < STALL_WARN_AFTER:
            return None
        if int(self.stats["presses_accepted"]) > 0:
            return None
        self._stall_warned_session = session
        secs = f"{elapsed:.0f}s"
        if int(seen) == 0:
            return (f"[hb] ⚠️ 监测已 {secs}：本进程一次左键都没看到（轮询正常）→ "
                    f"按键读取被系统拦住了（游戏以更高权限运行 / UIPI）。"
                    f"处置：右键『以管理员身份运行』重开本工具，或改用 --trigger audio / both。")
        skips = int(self.stats.get("panel_skips", 0))
        if skips > 0:
            return (f"[hb] ⚠️ 监测已 {secs}：看到 {int(seen)} 次左键但受理 0 次，"
                    f"其中 {skips} 次落在本工具面板内被跳过 → 你按的是我们自己面板上的按钮，"
                    f"不是游戏里的物品。请在游戏内**按住物品拖动**（面板内点击不会触发识别）。")
        return (f"[hb] ⚠️ 监测已 {secs}：看到 {int(seen)} 次左键但受理 0 次，"
                f"且没有一次落在面板内 → 这些按键发生在『开始监测』之前，"
                f"或未达 --min-interval 节流间隔。请在**监测中**于游戏内按住物品拖动。")

    def _work_loop(self) -> None:
        _lower_current_thread_priority()
        while not self.stop_event.is_set():
            try:
                payload = self.triggers.get(timeout=0.2)
            except queue.Empty:
                continue
            if payload is None:
                break
            pressed_at, trigger_src, trig_cursor, session_id = _split_trigger(payload)
            if pressed_at is None:
                continue
            try:
                self.handle_trigger(pressed_at, trigger_src, trig_cursor, session_id)
            except Exception as exc:
                self.stats["errors"] += 1
                print(f"[error] 触发处理失败: {type(exc).__name__}: {exc}", flush=True)
                self._emit([], 0, "", f"内部错误：{type(exc).__name__}: {exc}",
                           trigger_src=trigger_src)

    def handle_trigger(self, pressed_at: float, trigger_src: str = "", cursor_xy=None,
                       session_id: int = SESSION_NONE) -> None:
        """处理一次触发。

        ``trigger_src`` = 触发来源（4 类之一）；``cursor_xy`` = **该触发发生时刻**的光标
        （旧格式没带 → 回退实时光标）；``session_id`` = 按下会话（0 = 无会话、不加锁）。
        视觉一律在这个携带的光标位置上判定，**不再读实时光标** —— 否则
        --settle + 队列延迟期间的鼠标移动会把判定的位置带偏。
        """
        import recorder

        if not self.active.is_set():
            return

        carried_cursor = False
        if cursor_xy is not None:
            try:
                trig_cursor = (int(cursor_xy[0]), int(cursor_xy[1]))
                carried_cursor = True
            except (TypeError, ValueError, IndexError):
                trig_cursor = None
        else:
            trig_cursor = None
        if trig_cursor is None:
            try:
                trig_cursor = cursor_pos()
            except Exception:
                trig_cursor = (0, 0)
        session = self._press_session_get(session_id)

        panel_rect = self._panel_screen_rect()
        if panel_rect is not None and self._panel_skip_enabled():
            cur_x, cur_y = trig_cursor
            if self._point_in_rect(cur_x, cur_y, panel_rect):
                self.stats["panel_skips"] = self.stats.get("panel_skips", 0) + 1
                print(f"[skip] 触发落在面板内 光标=({cur_x},{cur_y}) 面板={panel_rect}"
                      f" 触发源={trigger_src or '未知'}"
                      f" → 不抓图/不判格/不出结果（本次触发不处理）", flush=True)
                return

        self.stats["triggers"] += 1
        t_start = time.perf_counter()
        trace_no = self.stats["triggers"]
        try:
            import recorder as _rec0

            disc0 = int(_rec0.discontinuities())
        except Exception:
            disc0 = 0

        slots, orientation, confidence = 0, "", 0.0
        vis_source = ""
        vis_ok, vis_state = None, "跳过(--no-vision)"
        best_info = None
        caller_pitch = None if self.args.no_vision else self._pitch_arg()
        if not self.args.no_vision:
            import vision

            pitch_hint = caller_pitch
            center_ok = self._capture_center_supported(vision)
            if carried_cursor and center_ok:
                center_note = " 截图中心=触发锚点"
            elif carried_cursor:
                center_note = " 截图中心=实时光标(vision 不支持 center)"
            else:
                center_note = " 截图中心=实时光标(未携带光标)"

            best_vis = None
            vis_full_note = ""
            vis_attempts = 0
            wait = float(getattr(self.args, "vision_wait", 0.6) or 0.0)
            pad0 = int(self.args.pad)
            pad_max = int(getattr(self.args, "pad_max", 0) or 0)
            deadline = time.perf_counter() + wait
            last_stat = ""
            last_pad = 0
            while True:
                pad = pad_max if pad_max > pad0 else pad0
                pad = max(pad, last_pad)
                try:
                    if carried_cursor and center_ok:
                        image, origin = vision.capture_cursor_region(pad=pad, center=trig_cursor)
                    else:
                        image, origin = vision.capture_cursor_region(pad=pad)
                    last_pad = pad
                    arr = np.asarray(image)
                    gray_ = arr[..., :3].mean(axis=2) if arr.ndim == 3 else arr.astype(float)
                    last_stat = (f"截{arr.shape[1]}x{arr.shape[0]} 亮度{gray_.mean():.0f} "
                                 f"标准差{gray_.std():.0f}{center_note}")
                    cursor_crop = (trig_cursor[0] - origin[0], trig_cursor[1] - origin[1])
                    if carried_cursor and not (0 <= cursor_crop[0] < arr.shape[1]
                                               and 0 <= cursor_crop[1] < arr.shape[0]):
                        why = ("已按锚点为中心截图 → 锚点仍不在图内（真越界/多屏异常）"
                               if (center_ok and carried_cursor)
                               else "截图中心=实时光标且 vision 不支持 center（未传）")
                        vis_attempts += 1
                        last_stat = (f"截{arr.shape[1]}x{arr.shape[0]} 触发锚点"
                                     f"({trig_cursor[0]},{trig_cursor[1]})不在截图内"
                                     f"(裁剪内={cursor_crop[0]},{cursor_crop[1]})；{why}")
                        break
                    ignore_crop = self._ignore_rects_in_image(panel_rect, origin)
                    drag, src = self._detect_press(image, cursor_xy=cursor_crop,
                                                   pitch=pitch_hint,
                                                   ignore_rects=ignore_crop)
                    vis_attempts += 1
                    s_ = int(_field(drag, "slots", 0) or 0)
                    o_ = str(_field(drag, "orientation", "") or "")
                    c_ = float(_field(drag, "confidence", 0.0) or 0.0)
                    hl_ = _field(drag, "highlight", None)
                    hl_clip_ = bool(_field(drag, "highlight_clipped", False))
                    r_ = str(_field(drag, "reason", "") or "")
                    if src == "红块":
                        prio = 2.0 + c_
                    elif src == "边框":
                        prio = 1.0 + c_
                    else:
                        prio, s_, o_, c_ = 0.0, 0, "", 0.0
                    if prio > 0 and (best_vis is None or prio > best_vis[0]):
                        best_vis = (prio, s_, o_, c_, hl_, r_, hl_clip_, src, drag)
                        best_info = drag
                    if self._press_item_missing:
                        self._save_vision_debug(trace_no, vis_attempts, image, src or "无")
                    if self.args.save_shots:
                        self._save_shot(trace_no, vis_attempts, image, origin, cursor_crop,
                                        {"pad": pad, "slots": s_, "orientation": o_,
                                         "confidence": c_, "highlight": hl_,
                                         "来源": src or "无", "reason": r_})
                    if src == "红块" and c_ >= MIN_VISION_CONFIDENCE and not hl_clip_:
                        break
                except Exception as exc:
                    vis_ok, vis_state = False, f"异常 {type(exc).__name__}: {exc}"
                    print(f"[vision] 失败: {type(exc).__name__}: {exc}", flush=True)
                    break
                if time.perf_counter() >= deadline:
                    break
                time.sleep(0.09)

            mode = str(getattr(self.args, "capture_mode", "auto"))
            red_ok = bool(best_vis and best_vis[7] == "红块" and not best_vis[6])
            if mode == "full" or (mode == "auto" and not red_ok):
                try:
                    t_full = time.perf_counter()
                    image_f, origin_f = vision.capture_full_screen()
                    grab_ms = (time.perf_counter() - t_full) * 1000.0
                    cur_f = (trig_cursor[0] - origin_f[0], trig_cursor[1] - origin_f[1])
                    full_pad = int(getattr(self.args, "pad_max", 0) or self.args.pad)
                    ignore_full = self._ignore_rects_in_image(panel_rect, origin_f)
                    t_det = time.perf_counter()
                    drag_f, f_src = self._press_from_fullscreen(vision, image_f, cur_f,
                                                                full_pad, pitch_hint,
                                                                ignore_full)
                    det_ms = (time.perf_counter() - t_det) * 1000.0
                    if self._press_item_missing:
                        self._save_vision_debug(trace_no, 90, image_f, f_src or "无")
                    if self.args.save_shots:
                        self._save_shot(trace_no, 90, image_f, origin_f, cur_f,
                                        {"整屏": f"{image_f.shape[1]}x{image_f.shape[0]}",
                                         "抓图ms": round(grab_ms, 1),
                                         "识别ms": round(det_ms, 1),
                                         "来源": f_src or "无",
                                         "结果": None if drag_f is None else
                                         {"slots": _field(drag_f, "slots", 0),
                                          "reason": _field(drag_f, "reason", "")}})
                    if drag_f is not None:
                        fs_ = int(_field(drag_f, "slots", 0) or 0)
                        fo_ = str(_field(drag_f, "orientation", "") or "")
                        fc_ = float(_field(drag_f, "confidence", 0.0) or 0.0)
                        fhl_ = _field(drag_f, "highlight", None)
                        fclip_ = bool(_field(drag_f, "highlight_clipped", False))
                        fprio = 2.0 + fc_
                        full_note = (f" 整屏{image_f.shape[1]}x{image_f.shape[0]}"
                                     f"→红块{fc_:.2f} ({grab_ms:.0f}+{det_ms:.0f}ms)")
                        print(f"[vision] 整屏兜底: slots={fs_} conf={fc_:.2f} "
                              f"highlight={fhl_} 来源=红块 抓图{grab_ms:.0f}ms "
                              f"识别{det_ms:.0f}ms {_field(drag_f, 'reason', '')}", flush=True)
                        if best_vis is None or fprio > best_vis[0]:
                            best_vis = (fprio, fs_, fo_, fc_, fhl_,
                                        str(_field(drag_f, "reason", "") or ""), fclip_, "红块",
                                        drag_f)
                            best_info = drag_f
                            vis_full_note = full_note
                    else:
                        vis_full_note = (f" 整屏{image_f.shape[1]}x{image_f.shape[0]}"
                                         f"未找到红块({grab_ms:.0f}+{det_ms:.0f}ms)")
                        print(f"[vision] 整屏未找到红块（抓图{grab_ms:.0f}ms "
                              f"识别{det_ms:.0f}ms）", flush=True)
                except Exception as exc:
                    vis_full_note = f" 整屏失败 {type(exc).__name__}"
                    print(f"[vision] 整屏兜底失败: {type(exc).__name__}: {exc}", flush=True)
            if best_vis is not None:
                _prio, slots, orientation, confidence, hl, vis_reason, hl_clip, vis_source = \
                    best_vis[:8]
                vis_ok = True
                if hl and not hl_clip:
                    self.stats["vision_highlight"] += 1
                vis_state = (f"{slots}格{orientation or '正'} conf={confidence:.2f}"
                             + (f" 高亮{hl[2]}x{hl[3]}"
                                + ("(被裁切!范围不够)" if hl_clip else "") if hl else " 无高亮")
                             + f" 轮询{vis_attempts}次 {last_stat}{vis_full_note}")
                if confidence < MIN_VISION_CONFIDENCE:
                    if slots or orientation:
                        print(f"[vision] 置信度过低 conf={confidence:.2f} slots={slots} "
                              f"来源={vis_source} → 按未识别处理", flush=True)
                    vis_state += f" → 低于 {MIN_VISION_CONFIDENCE}，按未识别处理"
                    slots, orientation = 0, ""
            else:
                vis_ok = True
                vis_source = ""
                vis_state = (f"没找到物品边框（按下的位置可能是空格子/提示框）"
                             f"轮询{vis_attempts}次 {last_stat}{vis_full_note}")

        this_pitch_tag = self._pitch_tag(best_info, caller_pitch)
        vis_state += this_pitch_tag
        self._print_pitch_line(trace_no, best_info, caller_pitch, bool(self.args.no_vision))

        this_vis = {"slots": slots, "orientation": orientation, "source": vis_source,
                    "confidence": confidence, "cursor": trig_cursor,
                    "rect": (_field(best_info, "highlight", None)
                             if best_info is not None else None),
                    "pitch": (self._pitch_from_info(best_info)[0]
                              if best_info is not None else None),
                    "pitch_tag": this_pitch_tag}
        applied = self._apply_session_lock(session, this_vis, trigger_src)
        lock_state = applied.get("tag") or ""
        if lock_state:
            vis_state = (f"【{lock_state}】{applied.get('note') or ''}"
                         f"（当前位置判定：{slots}格{orientation or '-'} 来源{vis_source or '无'}）"
                         + vis_state)
        if applied.get("use_locked"):
            slots = applied["slots"]
            orientation = applied["orientation"]
            vis_source = applied["source"]
            confidence = applied["confidence"]

        if self.args.settle > 0:
            time.sleep(self.args.settle)

        src_label = vis_source or "无"
        snap = self.audio.snapshot()
        if snap is None:
            trace = (f"触发#{trace_no} 视觉{'✓' if vis_ok else ('✗' if vis_ok is False else '–')}"
                     f"{vis_state} 音频✗未就绪 识别–未执行 来源={src_label}")
            print(f"[trace] {trace}", flush=True)
            self._emit([], slots, orientation,
                       f"音频尚未就绪（正在等待第一个回环窗口）。｜{trace}", confidence,
                       source=vis_source, trigger_src=trigger_src, lock_state=lock_state)
            return
        _w, _bg, _age = snap
        try:
            _rms = float(np.sqrt(np.mean(np.asarray(_w, dtype=np.float64) ** 2)))
        except Exception:
            _rms = 0.0
        try:
            _seg = recorder.loudest_segment(_w)
            _bw = recorder.spectral_rolloff(_seg)
            _seg_rms = float(np.sqrt(np.mean(np.asarray(_seg, dtype=np.float64) ** 2)))
        except Exception:
            _bw, _seg_rms = 0.0, 0.0
        _has_sound = _seg_rms >= max(0.0012, float(_bg) * 3.0)
        try:
            _gaps, _gap_ms, _gap_max = recorder.zero_gap_stats(_w)
        except Exception:
            _gaps, _gap_ms, _gap_max = 0, 0.0, 0.0
        aud_state = (f"age={_age:.2f}s bg={_bg:.5f} rms={_rms:.5f}"
                     + (f" 音效段{_seg_rms:.5f} 带宽{_bw / 1000:.1f}kHz" if _has_sound else " 无音效段")
                     + (f" 静音缝{_gaps}处/{_gap_ms:.0f}ms" if _gaps else ""))

        def _attempt(pressed_ts):
            """对一个窗口同时试「原始」与「降噪后」，取分数更高的一条路径。

            降噪（跨窗谱减）在低信噪比下可能过减而变差，所以**从不单独使用**：
            两条路径都算，只保留更强的结果 —— 保证"只会更好，不会更差"。
            """
            snap2 = self.audio.snapshot()
            if snap2 is None:
                return [], "", 0.0, "none", None, (0, 0, 0.0)
            win, bg, age2 = snap2
            cands = [("raw", win, bg)]
            try:
                gaps0 = int(recorder.zero_gap_stats(win)[0])
                if gaps0 > 0:
                    seg = recorder.longest_nonzero_segment(win)
                    if seg is not None and len(seg) >= 128:
                        cands.append(("segment", seg, recorder._background_level(seg)))
            except Exception:
                gaps0 = 0
            if getattr(self.args, "denoise", False):
                try:
                    import denoise

                    profile = self.audio.profile
                    ref = self.audio.reference_for(pressed_ts)
                    if profile is not None or ref is not None:
                        enh = denoise.enhance(win, ref, recorder.SAMPLE_RATE, profile=profile)
                        if enh is not None and len(enh) == len(win):
                            cands.append(("denoise", enh, recorder._background_level(enh)))
                except Exception as exc:
                    if self.args.verbose:
                        print(f"[denoise] 跳过: {type(exc).__name__}: {exc}", flush=True)
            best_local = None
            for pathname, x, bg2 in cands:
                rr = self.matcher.match(x, recorder.SAMPLE_RATE, self.args.limit, bg2)
                ss = rr[0]["score"] if rr else 0
                if self.args.verbose and len(cands) > 1:
                    print(f"[{pathname}] rows={len(rr)} score={ss} bg={bg2:.5f}", flush=True)
                try:
                    gaps = int(recorder.zero_gap_stats(win)[0])
                except Exception:
                    gaps = 0
                key = (1 if rr else 0, -gaps, ss)
                if best_local is None or key > best_local[5]:
                    best_local = (rr, self.matcher.last_reason, ss, pathname, age2, key)
            if best_local is None:
                return [], "", 0.0, "none", age2, (0, 0, 0.0)
            return best_local

        rows, reason, best_score, best_path, age, _best_key = _attempt(pressed_at)
        if age is not None and age > self.args.max_age:
            print(f"[audio] 窗口偏旧 age={age:.2f}s", flush=True)
        if best_score > 0:
            self.stats["sound_found"] += 1

        listen_max = float(getattr(self.args, "listen_max", 2.5) or 0.0)
        deadline = time.perf_counter() + max(0.0, listen_max)
        attempts = 0
        while True:
            if best_score >= GOOD_ENOUGH_SCORE:
                break
            if attempts >= max(0, int(self.args.retry)):
                if listen_max <= 0 or time.perf_counter() >= deadline:
                    break
                if best_score > 0 and attempts >= max(1, int(self.args.retry)) * 2:
                    break
            attempts += 1
            time.sleep(self.args.retry_delay)
            r2, rs2, s2, p2, _age2, key2 = _attempt(pressed_at)
            if s2 > 0:
                self.stats["sound_found"] += 1
            if self.args.verbose:
                print(f"[retry{attempts}] rows={len(r2)} score={s2} ({p2}) 缝隙{-key2[1]}"
                      f" （当前最好 {best_score}）", flush=True)
            if key2 > _best_key:
                rows, reason, best_score, best_path, _best_key = r2, rs2, s2, p2, key2

        if rows:
            self.stats["matches"] += 1
            floor = float(getattr(self.args, "min_score", 0.0) or 0.0)
            if floor > 0:
                kept = [r for r in rows if float(r.get("score", 0)) >= floor]
                if kept:
                    rows = kept
                else:
                    best_r = rows[0]
                    reason = (f"最像的是 {best_r['key']}（{best_r['score']} 分），"
                              f"低于你设置的显示下限 {floor:.0f} 分；实际判定阈值见 --show-weak")
                    rows = []
                    self.stats["empty"] += 1
        else:
            self.stats["empty"] += 1
            if self.args.show_weak:
                snapw = self.audio.snapshot()
                if snapw is not None:
                    weak = _weak_candidates(self.matcher, snapw[0], recorder.SAMPLE_RATE,
                                            self.args.limit, snapw[1])
                    if weak:
                        rows = weak
                        reason = (f"未达阈值（阈值 0.88/0.85/0.82/0.78，歧义余量 0.045）。"
                                  f"以下为最像的候选及其分数，仅供参考：{reason}")
        top = rows[0] if rows else None
        rec_ok = bool(rows)
        rec_state = (f"rows={len(rows)} top={top['key']}/{top['score']}({best_path})"
                     if top else f"rows=0 {str(reason)[:20]}")
        try:
            disc_delta = int(recorder.discontinuities()) - disc0
        except Exception:
            disc_delta = 0
        if disc_delta > 0:
            self.stats["glitchy_triggers"] = self.stats.get("glitchy_triggers", 0) + 1
        trace = (f"触发#{trace_no}"
                 f" 按键(看到{self.stats['presses_seen']}/受理{self.stats['presses_accepted']}"
                 f"/松开{self.stats['release_triggers']})"
                 f" 视觉{'✓' if vis_ok else ('✗' if vis_ok is False else '–')}{vis_state}"
                 f" 音频✓{aud_state}"
                 f" 采集断点+{disc_delta}"
                 f" 识别{'✓' if rec_ok else '✗'}{rec_state}"
                 f" 用时{time.perf_counter() - t_start:.2f}s"
                 f" 触发源={trigger_src or '未知'}"
                 f" 来源={src_label}")
        print(f"[trace] {trace}", flush=True)
        if disc_delta > 0:
            print(f"[glitch] 本次识别期间采集丢帧 {disc_delta} 次 → 音频被切断，"
                  f"听到的就是『掉帧感』，识别结果不可靠。可试 --audio-blocksize 0.3",
                  flush=True)
        if _gaps > 0:
            print(f"[glitch] 本次窗口检测到 {_gaps} 处凭空静音（共 {_gap_ms:.0f}ms，"
                  f"最长 {_gap_max:.0f}ms）→ soundcard 在设备无帧时**自动补零**，"
                  f"不报 DATA_DISCONTINUITY。这就是『掉帧感』的来源，波形被切断，"
                  f"本次识别结果不可靠（已优先采用无缝隙窗口）。", flush=True)

        if self.args.save_audio:
            self._save_audio_snapshot(trace_no, pressed_at, slots, orientation,
                                      confidence, rows)

        if self.args.verbose:
            print(
                f"[match] slots={slots}{orientation} conf={confidence:.2f} rows={len(rows)} "
                f"top={top['key'] if top else '-'} score={top['score'] if top else '-'} "
                f"reason={reason}",
                flush=True,
            )
        self._emit(rows, slots, orientation,
                   (reason or "") + ("｜" if reason else "") + trace, confidence,
                   source=vis_source, trigger_src=trigger_src, lock_state=lock_state)

    def _pitch_arg(self):
        """`--pitch` 给出的权威格宽；未给（或非正数/非数字）返回 ``None``。

        ``None`` 的语义 = **交给视觉在光标周围自测**（`detect_press_item(..., pitch=None)`）。
        """
        raw = self.args.pitch
        if raw is None:
            return None
        try:
            value = float(raw)
        except Exception:
            return None
        return value if value > 0 else None

    def _pitch_value(self):
        """兼容旧接口：只返回 `--pitch` 的值（会话级「整屏实测」缓存已废弃）。"""
        return self._pitch_arg()

    @staticmethod
    def _pitch_from_info(info):
        """从**视觉结果**里读出它自己用的格宽：``(pitch|None, 来源串)``。只读，不猜。

        优先 `DragInfo.pitch`（+ `pitch_source`）；拿不到才从 `DragInfo.reason` 里的
        `格宽85.0(...)` 文本兜底。两者都没有 → ``(None, "")``（调用方必须写 `格宽=?`）。
        """
        if info is None:
            return None, ""
        src = str(_field(info, "pitch_source", "") or "")
        try:
            value = float(_field(info, "pitch", 0.0) or 0.0)
        except Exception:
            value = 0.0
        if value > 0:
            return value, src
        match = _PITCH_IN_REASON.search(str(_field(info, "reason", "") or ""))
        if match:
            try:
                return float(match.group(1)), src
            except Exception:
                return None, src
        return None, src

    def _pitch_tag(self, info=None, caller=None) -> str:
        """`[trace]` 视觉段的格宽标记。**必须区分来源**：

        `格宽=85.0(caller)` / `格宽=85.1(视觉局部·自洽support=29)` /
        `格宽=84.6(视觉局部)`（未认出的旧串，保持原样）/
        `格宽=79.0(常量回退·不确定)` / `格宽=?`（视觉没给出值）。

        为什么不能一律写「视觉局部」：`常量回退(不确定)` 是**没量到**时的常量兜底，
        标成「视觉局部」会让用户以为这是量出来的格宽（本次离线复核实测到）。
        只读视觉结果；**不**写任何缓存、**不**把它传回视觉（旧设计已废弃）。
        """
        if caller is None:
            caller = self._pitch_arg()
        if caller is not None:
            return f" 格宽={float(caller):.1f}(caller)"
        value, src = self._pitch_from_info(info)
        if value is None:
            return " 格宽=?"
        kind = _pitch_src_kind(src)
        if kind == "fallback":
            return f" 格宽={float(value):.1f}(常量回退·不确定)"
        if kind == "caller":
            return f" 格宽={float(value):.1f}(caller权威)"
        if kind == "local_scored":
            match = _PITCH_SUPPORT.search(str(src))
            detail = f"·自洽{match.group(0)}" if match else ""
            return f" 格宽={float(value):.1f}(视觉局部{detail})"
        if kind == "local_icons":
            return f" 格宽={float(value):.1f}(视觉局部·图标间距)"
        return f" 格宽={float(value):.1f}(视觉局部)"

    def _print_pitch_line(self, no: int, info, caller, no_vision: bool = False) -> None:
        """每次触发打印一行 `[pitch]`：
        来源 = caller / caller权威 / 视觉局部 / 常量回退(不确定) / 未测到。只报告，不回传。"""
        if no_vision:
            print(f"[pitch] 触发#{no} 来源=未测到 pitch=? （--no-vision：本次不做视觉）",
                  flush=True)
            return
        if caller is not None:
            print(f"[pitch] 触发#{no} 来源=caller pitch={float(caller):.1f}"
                  f"（--pitch 权威值，原样透传视觉；视觉自估值仅作参考、不改写它）",
                  flush=True)
            return
        value, src = self._pitch_from_info(info)
        if value is None:
            print(f"[pitch] 触发#{no} 来源=未测到 pitch=? "
                  f"（视觉本次未给出格宽；集成层不回传任何权威值）", flush=True)
            return
        label = _pitch_src_label(src)
        note = "" if label == "视觉局部" else "（不是量出来的格宽，只是常量兜底/来源声明）"
        print(f"[pitch] 触发#{no} 来源={label} pitch={float(value):.1f}"
              f"({src or '视觉自测'}){note}（仅报告，不回传）", flush=True)

    @staticmethod
    def _ignore_rects_in_image(rect, origin):
        """把屏幕坐标矩形换算成**以 origin 为左上角的那张图**的像素坐标。

        `ignore_rects` 的坐标系就是传入 image 的像素坐标（vision 冻结契约），所以必须
        减去该截图的原点：裁剪图原点来自 `capture_cursor_region`，整屏图原点来自
        `capture_full_screen`（= SM_XVIRTUALSCREEN/SM_YVIRTUALSCREEN）。

        返回 `list[(x, y, w, h)]`；没有面板/矩形无效 → `[]`（= 不屏蔽，而非猜测）。
        """
        if rect is None or origin is None:
            return []
        try:
            x, y, w, h = (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
            ox, oy = int(origin[0]), int(origin[1])
        except Exception:
            return []
        if w <= 0 or h <= 0:
            return []
        return [(x - ox, y - oy, w, h)]

    @staticmethod
    def _shift_rects(rects, dx: int, dy: int):
        """把一批矩形按 (dx, dy) 平移（整屏图 → 以红块为中心的精裁子图）。"""
        out = []
        for rect in rects or ():
            try:
                out.append((int(rect[0]) + int(dx), int(rect[1]) + int(dy),
                            int(rect[2]), int(rect[3])))
            except Exception:
                continue
        return out

    @staticmethod
    def _accepts_kwarg(fn, name: str) -> bool:
        """视觉函数是否接受该关键字参数。

        为什么先探签名：同事的 `ignore_rects` 可能还没落地，直接传会 TypeError；
        而且失败后"再试一次不带该参数"会让视觉函数被调用两次（重复抓图/重复计算）。
        """
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            return True
        if name in params:
            return True
        return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())

    def _capture_center_supported(self, vision) -> bool:
        """`vision.capture_cursor_region` 是否已支持 `center=`（task-12 两 lane 并行）。

        按**函数对象身份**缓存探测结果：vision.py 与本 lane 谁先落地都不会互相破坏；
        测试里把函数换成不同签名的桩时，缓存会自动失效（身份变了）。
        """
        fn = getattr(vision, "capture_cursor_region", None)
        if fn is None:
            return False
        cache = getattr(self, "_center_kw_cache", None)
        if cache is not None and cache[0] is fn:
            return bool(cache[1])
        ok = bool(self._accepts_kwarg(fn, "center"))
        self._center_kw_cache = (fn, ok)
        return ok

    def _vision_debug_dir(self):
        """`--vision-debug` 目录；不存在则创建。未设置返回 None。

        懒创建（只在真的处理触发时建），保证「未开始监测」绝不落盘。
        """
        raw = self.args.vision_debug
        if not raw:
            return None
        if self._vdebug_ready:
            return str(raw)
        try:
            Path(str(raw)).mkdir(parents=True, exist_ok=True)
            self._vdebug_ready = True
            print(f"[vision] 调试图目录 = {raw}", flush=True)
            return str(raw)
        except Exception as exc:
            print(f"[vision] 调试图目录创建失败: {type(exc).__name__}: {exc}", flush=True)
            return None

    def _save_vision_debug(self, no: int, idx: int, image, tag: str) -> None:
        """把「喂给视觉的那一帧」存进 `--vision-debug` 目录（仅 detect_press_item 缺失时）。

        为什么集成层也要存：vision 那边的中间图要等它自己实现完才会落盘；在那之前
        这个目录空着，用户复测时无法确认「按下瞬间到底截到了什么」。中文路径用
        cv2.imencode+tofile（cv2.imwrite 在中文路径下会静默失败）。
        """
        out = self._vision_debug_dir()
        if not out:
            return
        try:
            import cv2

            arr = np.asarray(image)
            ok, buf = cv2.imencode(".png", arr)
            if ok:
                buf.tofile(str(Path(out) / f"press_{no}_{idx}_{tag}.png"))
        except Exception as exc:
            print(f"[vision] 调试图保存失败: {type(exc).__name__}: {exc}", flush=True)

    def _detect_press(self, image, cursor_xy, pitch=None, ignore_rects=None):
        """按下瞬间的物品判定；返回 `(info, source)`，source ∈ {"红块","边框",""}。

        优先 `vision.detect_press_item`（混合：红块优先 → 物品边框兜底）。
        它还没实现完时**退化为「只认红块」**：绝不回退 detect_drag_slots 的几何猜测
        （那条路会把鼠标提示框当物品）。这一点是用户明确要求的。

        `pitch`：`--pitch` 给定时由调用方权威传入，否则 ``None`` → **视觉自己在光标
        周围估**（旧设计"整屏测一次当整场权威"已废弃）。

        `ignore_rects`：我方置顶面板在**这张 image 的像素坐标**下的矩形列表，视觉据此
        屏蔽面板区域（不参与边框/网格线判定）。面板没绑定时传 ``[]``。
        """
        import vision

        debug_dir = self._vision_debug_dir()
        rects = [] if ignore_rects is None else list(ignore_rects)
        fn = getattr(vision, "detect_press_item", None)
        if fn is None:
            if not self._press_item_missing:
                self._press_item_missing = True
                print("[vision] 边框检测未就绪（vision.detect_press_item 缺失）→ "
                      "本次退化为红块/无来源", flush=True)
            info = vision.detect_drag_slots(image, cursor_xy)
            src = "红块" if self._is_red_sourced(info) else ""
            return info, src
        kwargs = {"cursor_xy": cursor_xy, "pitch": pitch}
        if self._accepts_kwarg(fn, "debug_dir"):
            kwargs["debug_dir"] = debug_dir
        if self._accepts_kwarg(fn, "ignore_rects"):
            kwargs["ignore_rects"] = rects
        try:
            info = fn(image, **kwargs)
        except TypeError:
            info = fn(image, cursor_xy=cursor_xy, pitch=pitch)
        src = str(_field(info, "source", "") or "")
        if src not in ("红块", "边框"):
            if int(_field(info, "slots", 0) or 0) > 0:
                print(f"[vision] detect_press_item 返回 slots="
                      f"{_field(info, 'slots', 0)} 但 source={src!r}（非 红块/边框）"
                      f"→ 按无来源处理", flush=True)
            return info, ""
        return info, src

    @staticmethod
    def _is_red_sourced(info) -> bool:
        """info 的格数是否**就是红块本身**给出的（vision 自己的语义，不猜）。"""
        if info is None:
            return False
        hl = _field(info, "highlight", None)
        rect = _field(info, "rect", None)
        if not hl or rect is None:
            return False
        if int(_field(info, "slots", 0) or 0) <= 0:
            return False
        if bool(_field(info, "highlight_clipped", False)):
            return False
        try:
            return tuple(rect) == tuple(hl)
        except Exception:
            return False

    def _press_from_fullscreen(self, vision, image_f, cur_f, pad: int, pitch=None,
                               ignore_rects=None):
        """整屏兜底 = **红块搜索**：整屏定位红块 → 以红块为中心精裁 → 判定格数。

        `pitch` = `--pitch` 权威值，或 ``None``（= 由视觉在光标周围自测）。
        ``None`` 时**只**用一个局部变量 ``search_pitch``（`vision.GRID_PITCH_PX`）
        去喂"找红块的形状筛选"——它**绝不**当作格宽权威传给内层视觉：
        `pitch 非 None = 调用方权威` 是冻结契约，冒充权威会让 reason/日志把
        常量 79.0 写成"caller权威"（本次离线复核实测到过）。
        `ignore_rects` = 面板在**整屏图**像素坐标下的矩形（换算到精裁子图后再传给视觉）。
        返回 `(info, source)`；没找到红块 / 拿不到「红块」来源时返回 `(None, "")`。
        """
        h, w = image_f.shape[:2]
        search_pitch = float(pitch) if pitch is not None \
            else float(getattr(vision, "GRID_PITCH_PX", 79.0))
        finder = getattr(vision, "find_drag_highlight_fullscreen", None)
        if finder is None:
            fn_fs = getattr(vision, "detect_drag_slots_fullscreen", None)
            if fn_fs is None:
                return None, ""
            info = fn_fs(image_f, cur_f, pad=pad)
            if info is None or not self._is_red_sourced(info):
                return None, ""
            return info, "红块"
        hl = finder(image_f, cur_f, search_pitch)
        if not hl:
            return None, ""
        hx, hy, hw, hh = int(hl[0]), int(hl[1]), int(hl[2]), int(hl[3])
        half = int(max(max(hw, hh) / 2.0 + int(pad) * 0.5, float(pad)))
        left = max(0, min(w - 1, hx + hw // 2 - half))
        top = max(0, min(h - 1, hy + hh // 2 - half))
        right = min(w, left + 2 * half)
        bottom = min(h, top + 2 * half)
        crop = np.ascontiguousarray(image_f[top:bottom, left:right])
        if crop.size == 0:
            return None, ""
        anchor = (min(max(hx + hw // 2 - left, 0), crop.shape[1] - 1),
                  min(max(hy + hh // 2 - top, 0), crop.shape[0] - 1))
        ignore_crop = self._shift_rects(ignore_rects, -left, -top)
        info, src = self._detect_press(crop, cursor_xy=anchor, pitch=pitch,
                                       ignore_rects=ignore_crop)
        if src != "红块":
            return None, ""
        try:
            hl2 = _field(info, "highlight", None)
            if hl2 is not None:
                info.highlight = (hl2[0] + left, hl2[1] + top, hl2[2], hl2[3])
            r2 = _field(info, "rect", None)
            if r2 is not None:
                info.rect = (r2[0] + left, r2[1] + top, r2[2], r2[3])
        except Exception:
            pass
        return info, "红块"

    def _save_shot(self, no: int, idx: int, image, origin, cursor_crop, info) -> None:
        """把截图存成 PNG（含全屏一张），用于确认"截屏范围/是否截到游戏"。"""
        try:
            from pathlib import Path

            import cv2

            out = Path(str(self.args.save_shots))
            out.mkdir(parents=True, exist_ok=True)
            arr = np.asarray(image)
            ok, buf = cv2.imencode(".png", arr)
            if ok:
                buf.tofile(str(out / f"crop_{no}_{idx}.png"))
            if idx == 1:
                from PIL import ImageGrab

                full = np.asarray(ImageGrab.grab())
                ok2, buf2 = cv2.imencode(".png", full[:, :, ::-1])
                if ok2:
                    buf2.tofile(str(out / f"full_{no}.png"))
            print(f"[shot] crop_{no}_{idx}.png 尺寸={arr.shape} origin={origin} "
                  f"光标在图内={cursor_crop} {info}", flush=True)
        except Exception as exc:
            print(f"[shot] 保存失败: {type(exc).__name__}: {exc}", flush=True)

    def _save_audio_snapshot(self, no: int, pressed_at: float, slots: int, orientation: str,
                             confidence: float, rows) -> None:
        """把这次触发用到的音频窗口 + 背景参考存盘，便于离线复现音频问题。"""
        try:
            import json
            from pathlib import Path

            out = Path(str(self.args.save_audio))
            out.mkdir(parents=True, exist_ok=True)
            snap = self.audio.snapshot()
            if snap is None:
                return
            win, bg, age = snap
            tag = f"{int(pressed_at * 1000)}"
            np.save(out / f"win_{tag}.npy", np.asarray(win, dtype=np.float32))
            ref = self.audio.reference_for(pressed_at)
            if ref is not None:
                np.save(out / f"ref_{tag}.npy", np.asarray(ref, dtype=np.float32))
            top = rows[0] if rows else None
            meta = {
                "tag": tag, "pressed_at": pressed_at, "device": self.args.device,
                "trigger_mode": getattr(self.args, "trigger", "mouse"),
                "slots": slots, "orientation": orientation, "vision_confidence": confidence,
                "bg": bg, "age": age,
                "rms": float(np.sqrt(np.mean(np.asarray(win, dtype=np.float64) ** 2))),
                "ref_rms": (float(np.sqrt(np.mean(np.asarray(ref, dtype=np.float64) ** 2)))
                            if ref is not None else None),
                "rows": len(rows), "top": (top.get("key") if top else None),
                "top_score": (top.get("score") if top else None),
                "samplerate": 44100,
            }
            with open(out / f"meta_{tag}.json", "w", encoding="utf-8") as fh:
                json.dump(meta, fh, ensure_ascii=False, indent=2)
            print(f"[save] 已保存 {out.name}/win_{tag}.npy 与元数据（bg={bg:.5f} "
                  f"rms={meta['rms']:.5f}）", flush=True)
        except Exception as exc:
            print(f"[save] 失败: {type(exc).__name__}: {exc}", flush=True)

    def _emit(self, rows, slots, orientation, reason, confidence=0.0, source="",
              trigger_src="", lock_state="") -> None:
        try:
            self.results.put_nowait(
                {
                    "rows": rows,
                    "slots": slots,
                    "orientation": orientation,
                    "reason": reason,
                    "confidence": confidence,
                    "source": source,
                    "trigger_src": trigger_src,
                    "lock_state": lock_state,
                    "t": time.time(),
                }
            )
        except queue.Full:
            pass

    def shutdown(self) -> None:
        self.stop_event.set()
        self.audio.stop()
        try:
            self.triggers.put_nowait(None)
        except queue.Full:
            pass
        for th in (self._worker, self._watcher, self._heartbeat, self.audio._thread):
            if th is not None:
                th.join(timeout=2.0)
        print(f"[stats] {self.stats}", flush=True)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="行商助手（外部感知，只读）")
    p.add_argument("--device", default=None, help="回环设备名；默认自动取系统当前播放设备")
    p.add_argument("--list-devices", action="store_true", help="列出可选播放/回环设备后退出")
    p.add_argument("--diag", action="store_true",
                   help="一键诊断『拖动没反应』卡在哪一步（只读；会请你配合按几次左键）")
    p.add_argument("--trigger", choices=["mouse", "audio", "both"], default="mouse",
                   help="触发方式：mouse=读左键（默认）；audio=只靠音效起始（游戏/反作弊"
                        "拦截输入读取时用这个）；both=两者都要")
    p.add_argument("--trigger-edges", choices=["press", "both", "release"], default="both",
                   help="鼠标触发的边沿：press=只在按下；release=只在松开；"
                        "both=按下+按住续触+松开（默认，音效常在放下那瞬才响）")
    p.add_argument("--audio-blocksize", type=float, default=0.2,
                   help="WASAPI 内部缓冲秒数（默认 0.2）。调大能容忍采集线程被游戏抢走"
                        "更久，缓解'掉帧感'与断点，代价是回调更少、延迟略增")
    p.add_argument("--listen-max", type=float, default=2.0,
                   help="一次触发最多持续听多久（秒），默认 2.5：实测多数按下时还没出声，"
                        "需要在之后一段时间里持续取样才能听到音效；0=只用 --retry 次数")
    p.add_argument("--pad", type=int, default=200, help="光标附近截图半边长（像素），默认 200")
    p.add_argument("--limit", type=int, default=5, help="最多返回的候选声音组数，默认 5")
    p.add_argument("--poll", type=float, default=0.012, help="鼠标轮询间隔秒，默认 0.012")
    p.add_argument("--settle", type=float, default=0.14, help="按下后等待再取音频/截图，默认 0.14")
    p.add_argument("--min-interval", type=float, default=0.9, help="音频触发最小间隔秒，默认 0.9")
    p.add_argument("--hold-interval", type=float, default=0.25,
                   help="按住拖动时续触/松开的最小间隔秒，默认 0.25。"
                        "与音频节流 --min-interval 解耦：后者 0.9s 曾把快速连续拖动整段吞掉。")
    p.add_argument("--hold-sample", dest="hold_sample", action="store_true", default=True,
                   help="按住期间专用采样（默认开）：~10Hz 抓整屏找红块，"
                        "整个拖动过程只要红块出现过一次就抓得到（快拖兜底）")
    p.add_argument("--no-hold-sample", dest="hold_sample", action="store_false",
                   help="关闭按住期间专用采样（回到「只在触发那一刻取样」的旧行为）")
    p.add_argument("--hold-sample-rate", type=float, default=0.1,
                   help="按住期间采样间隔秒，默认 0.1（约 10Hz；实测一轮抓图+识别约 70ms）")
    p.add_argument("--retry", type=int, default=3,
                   help="按下后继续听取音效的额外次数（取最高分），默认 3；0=只听一次")
    p.add_argument("--retry-delay", type=float, default=0.28, help="继续听取的间隔秒，默认 0.28")
    p.add_argument("--max-age", type=float, default=0.6, help="音频窗口最大可接受年龄秒")
    p.add_argument("--no-vision", action="store_true", help="跳过占格识别，只做音频识别")
    p.add_argument("--denoise", action="store_true",
                   help="启用跨窗谱减降噪（实验性，默认关闭）：与原始路径同时算、取更高分。"
                        "注意：当前版本在噪声基准上会把部分音效的滤波路径打崩"
                        "（座钟 70→0），仅在你自己实测更好时才打开")
    p.add_argument("--show-weak", action="store_true",
                   help="未达阈值时也显示最像的几个候选及其分数（低置信，仅参考）")
    p.add_argument("--show-mismatch", action="store_true",
                   help="调试用（默认关）：连与视觉格数/朝向**不符**的候选也显示。默认只显示相符的"
                        "——一个音效键常对应多个物品（同音组），格数/朝向是唯一能把它们分开的信息；"
                        "被过滤掉的候选仍会写进日志（[filter] 行）")
    p.add_argument("--min-score", type=float, default=0.0,
                   help="显示下限：分数低于该值的候选不显示（0=不设限）。注意识别本身的阈值"
                        "（0.88/0.85/0.82/0.78、波形路径 0.12）不改，这里只过滤显示")
    p.add_argument("--vision-wait", type=float, default=0.6,
                   help="按下后为等『拖动高亮』画出来而轮询截图的最长秒数，默认 0.6")
    p.add_argument("--pad-max", type=int, default=400,
                   help="高亮超出 pad 范围时自动放大的上限（默认 400 → 800x800 截屏）；0=不放大。"
                        "首次取图直接用 pad-max（当 pad-max>pad），因为局部窗口半径约 3 格"
                        "（pitch=85 → 255px），280x280 装不下")
    p.add_argument("--pitch", type=float, default=None,
                   help="手动指定格宽（像素）：给定时一律作为**权威值**原样传给视觉"
                        "（detect_press_item(..., pitch=85)），替代任何自动估计；不给（默认）"
                        "则传 pitch=None，由视觉在光标周围自测。真机真值 ≈ 85（旧设计的"
                        "『整屏实测』会被我们自己的置顶面板污染成 127.8，故已废弃）")
    p.add_argument("--pitch-refresh", type=float, default=0.0,
                   help="【已废弃】旧设计里『整屏实测格宽的重新测量周期秒』。会话级格宽"
                        "权威缓存已废弃（整屏实测会被我方置顶面板污染），本参数只为兼容"
                        "旧命令行而保留：解析不报错，但不再有任何效果。要指定格宽请用 --pitch")
    p.add_argument("--autostart", action="store_true",
                   help="启动即开始监测（默认关闭：必须先点面板『开始监测』或按 F8，"
                        "避免在你没玩的时候截屏/录音/识别到其它应用）")
    p.add_argument("--capture-mode", choices=["auto", "crop", "full"], default="auto",
                   help="视觉取图方式：crop=只截光标周围（快）；full=只整屏找红块；"
                        "auto=先裁剪，没拿到完整高亮就整屏兜底（默认）")
    p.add_argument("--save-shots", default=None,
                   help="把每次截屏存成 PNG 到该目录（含全屏一张），用于确认截屏范围")
    p.add_argument("--vision-debug", default=None,
                   help="把视觉中间图（红块/物品边框/裁剪过程）写到该目录，用于排查"
                        "「为什么这个位置判成 0 格」；目录不存在会自动创建")
    p.add_argument("--save-audio", default=None,
                   help="把每次触发的窗口与背景参考存成 .npy 到该目录（排查/复现音频问题用）")
    p.add_argument("--log", default=None,
                   help="把控制台输出同时写入该文件（UTF-8，便于事后排查/发回分析）")
    p.add_argument("--verbose", action="store_true", help="打印每次识别详情")
    return p.parse_args(argv)


def _result_status_line(item: dict, src: str, trg_src: str) -> str:
    """面板状态行文本（纯格式化，便于离线断言）。

    会话锁提示放在**最前**：「已锁 2格V｜…」——让用户一眼看出现在显示的是
    "我按下的那个物品"，而不是鼠标当前掠过的东西（ui.py 本轮由别的 lane 在改，
    所以锁提示只走 set_status 这一条通道）。
    """
    lock_state = str(item.get("lock_state") or "")
    return (
        f"{lock_state + '｜' if lock_state else ''}{item['reason']}"
        f"（格数 {item['slots'] or '?'}{item['orientation']}"
        f" 来源 {src} 触发 {trg_src}"
        + (f" 置信度 {item['confidence']:.2f}" if item.get("confidence") else "")
        + "）"
    )


class _Tee:
    """把 stdout/stderr 同时写到控制台与文件（--log 用）。"""

    def __init__(self, *streams):
        self._streams = [s for s in streams if s is not None]

    def write(self, data):
        for s in self._streams:
            try:
                s.write(data)
            except Exception:
                pass
        return len(data) if isinstance(data, str) else 0

    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        return False


def _install_log_tee(path: str) -> bool:
    try:
        fh = open(path, "w", encoding="utf-8", errors="replace", buffering=1)
        sys.stdout = _Tee(sys.__stdout__, fh)
        sys.stderr = _Tee(sys.__stderr__, fh)
        print(f"[log] 控制台输出同时写入 {path}（UTF-8）", flush=True)
        return True
    except Exception as exc:
        print(f"[log] 无法写入 {path}: {type(exc).__name__}: {exc}", flush=True)
        return False


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.log:
        _install_log_tee(args.log)

    if args.diag:
        import diag

        return diag.run_diag(args)

    if args.list_devices:
        import recorder

        print("[devices]", flush=True)
        for name in recorder.list_devices():
            print(f"  {name}", flush=True)
        print(f"[default] {recorder.default_device_name()}", flush=True)
        return 0

    engine = Engine(args)
    engine.last_admin = _print_env_banner()
    _set_process_priority()
    engine.load()

    audio_samplerate = None
    audio_device = None
    try:
        import recorder as _rec

        _chosen = args.device or _rec.default_device_name() or "(系统默认设备)"
        print(f"[audio] 回环设备 = {_chosen}", flush=True)
        print(f"[audio] 可选设备: {' | '.join(_rec.list_devices())}", flush=True)
        print("[audio] 若长时间无声音，请用 --list-devices 查看并用 --device \"设备名\" 指定", flush=True)
        audio_samplerate = getattr(_rec, "SAMPLE_RATE", None)
        if args.device:
            audio_device = args.device
        else:
            try:
                audio_device = _rec.default_device_name() or None
            except Exception:
                audio_device = None
    except Exception as exc:
        print(f"[audio] 设备枚举失败: {type(exc).__name__}: {exc}", flush=True)

    from PyQt5.QtCore import QTimer
    from PyQt5.QtWidgets import QApplication

    import ui

    app = QApplication.instance() or QApplication(sys.argv[:1])
    panel = ui.Panel(engine.candidate_engine, start_thread=False,
                     strict_shape=not getattr(args, "show_mismatch", False))
    panel.bind_active(engine.is_active, engine.set_active)
    panel.set_status(_IDLE_STATUS)
    engine.bind_panel(panel)
    engine.set_panel_rect(panel.screen_rect())

    engine.start_threads()

    gate_state = {"last": None}

    def detection_status_line() -> str:
        """面板底栏文本：**只讲检测状态**，不带原因/触发/锁定/计数（那些只写日志）。

        可操作提示（音频错误 / 回环无信号）保留 —— 它们直接告诉玩家怎么办。
        """
        if engine.audio.last_error:
            return f"音频错误：{engine.audio.last_error}"
        if engine.audio.windows == 0:
            return "等待拖动物品…（正在初始化回环音频）"
        if engine.audio.windows >= 25 and engine.audio.peak_rms < 0.0004:
            return ("回环音频无信号：请确认游戏声音从当前设备播放，"
                    "或用 --list-devices / --device 指定正确设备")
        return _MONITORING_STATUS

    def pump() -> None:
        act = engine.is_active()
        if gate_state["last"] != act:
            gate_state["last"] = act
            panel.set_active_state(act)
        panel.set_status(detection_status_line() if act else _IDLE_STATUS)
        try:
            panel.set_audio_source(samplerate=audio_samplerate, device=audio_device,
                                   peak=engine.audio.peak_rms)
        except Exception:
            pass
        if not act:
            return
        try:
            engine.set_panel_rect(panel.screen_rect())
        except Exception:
            pass
        for _ in range(4):
            try:
                item = engine.results.get_nowait()
            except queue.Empty:
                break
            src = item.get("source") or "无"
            trg_src = item.get("trigger_src") or "未知"
            panel.show_candidates(item["rows"], item["slots"], item["orientation"],
                                  item["reason"], source=src, trigger_src=trg_src)

    timer = QTimer()
    timer.timeout.connect(pump)
    timer.start(80)

    try:
        panel.start()
    except KeyboardInterrupt:
        pass
    finally:
        timer.stop()
        engine.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())