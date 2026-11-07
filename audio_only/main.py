# -*- coding: utf-8 -*-
"""行商助手 · 声控版 · 集成层 main.py（**纯音频，无视觉**）

与完整版的唯一区别就是"感知通道"：本版**不看屏幕**。

    环回音频（WASAPI loopback）听到"突起的短音效"
      → recognition.SoundMatcher.match 得到声音组
      → ui.Panel 按玩家手选的**格数**筛出候选卡片

硬边界（与原版同一套红线，本版更窄）：
    * 不截屏、不读像素、不碰屏幕/光标/GDI（源码里没有任何截图调用）
    * 不读游戏内存、不注入进程、不改游戏文件、不解包游戏资源
    * 不模拟键鼠（无 SendInput / mouse_event / keybd_event）、不自动点击、不自动购买
    * 不联网、不自学习、不热更新
    * 唯一的键读取是 ``GetAsyncKeyState(F8)``：只读全局键态，用来开始/停止监测
      （不注册系统热键、不挂钩子、不模拟按键）

因为不看屏幕，"这件占几格"由玩家在面板上点那排**图标按钮**手动选
（不限 / 1×1 / 1×2 / 2×1 / 2×2 / 2×3 / 3×2，见 ``shapes.py``）。
选「不限」= 不按格数过滤；选了具体形状就按格数硬筛（朝向只标注、不丢）。

用法：
    D:\\PY\\python.exe main.py              # 自动选择系统默认播放设备
    D:\\PY\\python.exe main.py --list-devices
    D:\\PY\\python.exe main.py --device "扬声器 (2- Realtek(R) Audio)"
    D:\\PY\\python.exe main.py --diag       # 只测音频链路（无面板、不识别物品）
    D:\\PY\\python.exe main.py --autostart  # 启动即开始监测（默认要点按钮/按 F8）
"""
from __future__ import annotations

import argparse
import ctypes
import os
import queue
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def app_dir() -> Path:
    """程序（exe）所在目录 —— **frozen 下绝不能用 `__file__`**。

    Nuitka onefile 会把程序解包到 `%TEMP%\\onefile_xxx\\`，主模块的 `__file__` 指向
    那个临时目录；照着 `__file__` 写日志/诊断报告，进程一退就随临时目录一起消失。
    `sys.argv[0]` 才是 exe 路径（`_rt/launcher.py` 的 `_log_path()` 用的也是它）。
    不可写时再退 `%LOCALAPPDATA%\\行商助手声控版\\`。
    """
    try:
        cand = Path(sys.argv[0]).resolve().parent
        if cand.is_dir():
            return cand
    except Exception:
        pass
    return ROOT
for _p in (str(ROOT), str(ROOT.parent / "deps")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ASSETS = ROOT.parent / "assets"
DATABASE = ASSETS / "database.json"



RESULT_DELAY_S = 0.0
try:
    RESULT_DELAY_S = max(0.0, float(os.environ.get("MR_RESULT_DELAY_S", "0") or 0.0))
except ValueError:
    RESULT_DELAY_S = 0.0

import numpy as np






MIN_TRIGGER_SEGMENT_RMS = 0.0012
TRIGGER_SEGMENT_BG_RATIO = 3.0
GOOD_ENOUGH_SCORE = 85



_IDLE_STATUS = "未开始监测：不读取游戏信息，点上方按钮或按 F8 开始。"



_MONITORING_STATUS = "监测中：等待音效…"

RECOGNIZING_STATUS = "监测中：正在识别…"

RECENT_RESULT_SECONDS = 2.5


TRIG_AUDIO = "音频触发"


STALL_WARN_AFTER = 20.0




ONSET_TAIL_SECONDS = 0.25

ONSET_GAP_SECONDS = 0.20

ONSET_BASE_SECONDS = 0.60

ONSET_RATIO = 3.5

BAND_RATIO = 20.0

BAND_FLOOR = 1e-7

BAND_HZ = (5000.0, 8000.0)

ONSET_FRAME = 882


def _band_envelope(window: np.ndarray, rate: int) -> np.ndarray:
    """按 20ms 块算 5–8kHz 频带能量（竞品 PickupEventDetector 的判据频段）。"""
    blk = max(64, int(rate * 0.02))
    n = len(window) // blk
    if n <= 0:
        return np.zeros(0, np.float64)
    nfft = 1024
    win = np.hanning(nfft)
    freqs = np.fft.rfftfreq(nfft, 1.0 / rate)
    sel = (freqs >= BAND_HZ[0]) & (freqs <= BAND_HZ[1])
    out = np.empty(n, np.float64)
    for i in range(n):
        seg = window[i * blk:(i + 1) * blk]
        if len(seg) < nfft:
            seg = np.pad(seg, (0, nfft - len(seg)))
        sp = np.abs(np.fft.rfft(seg[:nfft] * win)) ** 2
        out[i] = float(np.sum(sp[sel]))
    return out


def _trim_previous_sound(window, frame: int = ONSET_FRAME):
    """窗口里还留着"上一声"时把它裁掉，只保留 ``[安静段 + 当前这一声]``。

    为什么需要：识别是对**整窗**做匹配（噪声谱取整窗最安静的那批帧、峰值门控用整窗峰值），
    相邻两次拖动间隔 < 1.5s 时窗口里必然还带着上一声；上一声更响时它会直接抢占榜首
    （合成实测：上一声 2 倍音量 → 上一声 97 分、真正这一声 93 分），面板就显示成别的物品。

    做法：逐 20ms 帧算 RMS → 把响帧聚成"声段"（容忍 60ms 内部空隙，金属音效有瞬态凹陷）
    → 最后一段就是当前这一声，它前一段就是上一声 → 从上一声结束处开始保留。

    返回 ``(裁剪后的窗口, 裁掉的样本数)``；窗口本来就干净（只有一段响 / 安静段直达窗口
    开头 / 裁完太短不可靠）时返回 ``(None, 0)``，调用方保持原样（干净窗口行为完全不变）。
    """
    import recorder

    x = np.asarray(window, dtype=np.float32).reshape(-1)
    n = len(x) // frame
    if n < 10:
        return None, 0
    levels = np.sqrt(np.mean(x[: n * frame].reshape(n, frame).astype(np.float64) ** 2, axis=1))

    gate = max(0.0015, float(np.quantile(levels, 0.10)) * 2.0)
    idx = np.flatnonzero(levels > gate)
    if len(idx) < 2:
        return None, 0

    spans = []
    s0 = prev = int(idx[0])
    for i in idx[1:]:
        i = int(i)
        if i - prev <= 3:
            prev = i
        else:
            spans.append((s0, prev))
            s0 = prev = i
    spans.append((s0, prev))
    if len(spans) < 2:
        return None, 0
    cur_start = spans[-1][0]
    cut = spans[-2][1] + 1
    if cut >= cur_start:
        cut = max(0, cur_start - 2)
    if cut <= 0:
        return None, 0
    trimmed = x[cut * frame:]
    if len(trimmed) < int(0.55 * recorder.SAMPLE_RATE):
        return None, 0
    return trimmed, cut * frame


VK_F8 = 0x77
_user32 = ctypes.windll.user32





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


def f8_down() -> bool:
    """只读 F8 状态（不注册系统热键、不挂钩子、不模拟按键，只读全局键态）。

    用途：游戏内不方便点面板时的"开始/停止监测"快捷键。因为只读键态，
    即使游戏窗口在前台、甚至游戏自己占用了 F8，这里依然读得到物理按键。
    """
    try:
        return bool(_user32.GetAsyncKeyState(VK_F8) & 0x8000)
    except Exception:
        return False


VK_F9 = 0x78
VK_F10 = 0x79


def _key_down(vk: int) -> bool:
    """只读某个虚拟键的状态（不注册热键、不挂钩子、不模拟按键）。"""
    try:
        return bool(_user32.GetAsyncKeyState(int(vk)) & 0x8000)
    except Exception:
        return False


def f9_down() -> bool:
    return _key_down(VK_F9)


def f10_down() -> bool:
    return _key_down(VK_F10)


def _is_admin():
    """本进程是否以管理员身份运行；查不到返回 ``None``（只读查询自己的令牌）。"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return None


def _print_env_banner() -> None:
    """启动横幅：只说本版**真正需要**的东西（管理员权限对纯音频不是必需）。"""
    print("[env] 行商助手 · 声控版 · 1.1.1 识别移植版（纯音频，不截屏、不读画面）",
          flush=True)
    print("[env] 识别引擎 = 1.1.1 谱匹配（33 模板，阈值 0.88/0.85/0.82/0.78）",
          flush=True)
    admin = _is_admin()
    if admin is True:
        print("[env] 管理员权限 = 是", flush=True)
    elif admin is False:
        print("[env] 管理员权限 = 否（本版不需要提权：识别只依赖回环音频）", flush=True)
    else:
        print("[env] 管理员权限 = 未知", flush=True)





class AudioFeed:
    """后台采集线程 + "最新窗口"缓存 + 音效起始触发。

    触发判据只用**相对**量：窗口**末尾** 0.25s 的 20ms 帧峰值 RMS，相对该窗背景电平、
    以及窗口更早 0.6s 的基线中位数。宁可多触发（多触发只是多一条"没有候选"的日志），
    也不漏掉音效。**不要**改回"整窗最响帧"——那会让 1.5s 窗口里残留的上一声把下一次
    拖动永久压住（用户实测：第二个物品起一直卡在等待）。
    """

    def __init__(self, device_name: str | None = None, history: int = 12,
                 trigger_queue=None, min_interval: float = 0.4,
                 blocksize_seconds: float | None = None, active_gate=None):
        self.blocksize_seconds = blocksize_seconds
        self._active_gate = active_gate
        self.device_name = device_name
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._latest = None
        self._windows = 0
        self._last_rms = 0.0
        self._peak_rms = 0.0
        self.last_error: str | None = None
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()
        self._capture_generation = 0

        self._history: list = []
        self._history_max = max(2, int(history))
        self._profile = None
        self._profile_job = None
        self._profile_lock = threading.Lock()

        self._trigger_queue = trigger_queue
        self._min_interval = float(min_interval)
        self._last_audio_trigger = 0.0
        self._armed = True
        self.audio_triggers = 0


    def start(self) -> bool:
        """不重复启动活线程；失败后由用户重新开始监测来重建。"""
        with self._lifecycle_lock:
            if self.running:
                return False
            self.stop_event.clear()
            self._capture_generation += 1
            self.last_error = None
            with self._profile_lock:
                self._profile = None
            with self._lock:
                self._latest = None
                self._windows = 0
                self._last_rms = self._peak_rms = 0.0
                self._history.clear()
            self._armed = True
            self._last_audio_trigger = 0.0
            self._thread = threading.Thread(target=self._run, name="audio-feed", daemon=True)
            self._thread.start()
            return True

    def stop(self) -> None:
        self.stop_event.set()

    def _refresh_profile(self, generation=None) -> None:
        """在后台线程里重算长期背景谱（约 10ms，不能让采集线程承担）。"""
        try:
            import denoise

            with self._lock:
                windows = [w for (w, _b, _t) in self._history]
            profile = denoise.noise_psd_profile(windows)
            with self._profile_lock:
                if profile is not None and (generation is None or generation == self._capture_generation):
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
                    self._last_rms = rms
                    if rms > self._peak_rms:
                        self._peak_rms = rms

                    self._history.append((window.copy(), float(background_rms), time.time()))
                    if len(self._history) > self._history_max:
                        del self._history[0:len(self._history) - self._history_max]
                    need_profile = self._windows >= 4
                if need_profile and (self._profile is None or self._windows % 4 == 0):
                    if self._profile_job is None or not self._profile_job.is_alive():
                        self._profile_job = threading.Thread(
                            target=self._refresh_profile, name="noise-profile", daemon=True,
                            kwargs={"generation": self._capture_generation})
                        self._profile_job.start()
                if self._trigger_queue is not None:
                    if self._active_gate is None or self._active_gate.is_set():
                        self._maybe_audio_trigger(window, float(background_rms))
            if not self.stop_event.is_set():
                raise RuntimeError("音频采集提前结束；请停止后重新开始监测重试")
        except Exception as exc:
            import traceback
            self.last_error = f"{type(exc).__name__}: {exc}"
            print(f"[error] 音频线程失败 device={self.device_name!r}: {self.last_error}", file=sys.stderr, flush=True)
            traceback.print_exc()


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
        """挑一个"触发之前"的最安静窗口做背景参考；没有合适的返回 None。

        为什么用"最安静"而不是"最近"：触发前后游戏可能已经在播别的音效，
        最安静的窗口最可能只含持续背景，谱减时不会把音效本身减掉。
        """
        with self._lock:
            items = list(self._history)
        older = [it for it in items if it[2] <= pressed_at - min_gap]
        if not older:
            return None
        return min(older, key=lambda it: it[1])[0]

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
    def latest_age(self) -> float | None:
        with self._lock:
            return None if self._latest is None else max(0.0, time.time() - self._latest[2])

    @property
    def discontinuities(self) -> int:
        """采集丢帧（WASAPI data discontinuity）累计次数；0 = 采集健康。"""
        try:
            import recorder

            return int(recorder.discontinuities())
        except Exception:
            return 0


    def _maybe_audio_trigger(self, window, background_rms: float,
                             now: float | None = None) -> None:
        """听到"突起的短音效"就当成一次拖动/拾取；只认**上升沿**。

        ⚠️ 旧实现用**整个 1.5s 窗口的最响帧**判断"现在响不响"，而窗口里必然还留着上一次
        音效（窗口 1.5s、相邻两次拖动常小于 1.5s）→ 永远判成"还响着"，第二次拖动起就再也
        不触发（用户实测：第一个物品正常，之后一直卡在等待）。现在改成：

        * ``tail``：窗口**末尾 0.25s** 的最响 20ms 帧 —— 只反映"刚刚"有没有声音；
        * ``base``：末尾之前（再留 0.20s 尾音）0.6s 的帧 RMS 中位数 —— 只反映环境基线；
        * ``tail >= max(绝对下限, 背景×3.5, 基线×3.5)`` 且此前已武装 → 触发一次；
        * ``tail`` 回落到阈值以下 → 重新武装（音效一结束就武装，与窗口里残留多久无关）。

        ``now`` 只为自测注入假时钟，正常运行传 None（用 ``time.time()``）。
        """
        now = time.time() if now is None else float(now)
        try:
            w = np.asarray(window, dtype=np.float32).reshape(-1)
            usable = w[: len(w) // ONSET_FRAME * ONSET_FRAME].reshape(-1, ONSET_FRAME)
            if usable.size == 0:
                return
            levels = np.sqrt(np.mean(usable.astype(np.float64) ** 2, axis=1))
        except Exception:
            return
        n = len(levels)
        tail_n = max(1, min(n, int(round(ONSET_TAIL_SECONDS / 0.02))))
        gap_n = max(0, int(round(ONSET_GAP_SECONDS / 0.02)))
        base_n = max(1, int(round(ONSET_BASE_SECONDS / 0.02)))
        tail = float(levels[n - tail_n:].max())
        base_end = max(0, n - tail_n - gap_n)
        base_start = max(0, base_end - base_n)
        if base_end > base_start:
            base = float(np.median(levels[base_start:base_end]))
        else:
            base = float(np.median(levels[: max(1, n - tail_n)]))
        onset_at = max(0.0012, float(background_rms) * ONSET_RATIO, base * ONSET_RATIO)







        try:
            import recorder as _rec

            env = _band_envelope(w, int(_rec.SAMPLE_RATE))
            if len(env) >= 8:
                env_tail = env[-min(len(env), tail_n):]
                env_base = env[: max(1, len(env) - len(env_tail))]
                peak_band = float(env_tail.max())
                med_band = float(np.median(env_base))
                if peak_band > BAND_FLOOR and peak_band > max(med_band, 1e-12) * BAND_RATIO:
                    tail = max(tail, onset_at * 1.01)
                    self._band_hits = getattr(self, "_band_hits", 0) + 1
                    self._last_band = (f"{peak_band:.3f}/{med_band:.4f}"
                                       f"={peak_band / max(med_band, 1e-12):.0f}x")
        except Exception:
            pass
        if tail < onset_at:
            self._armed = True

            if tail > 0.004 and now - getattr(self, "_last_near_log", 0.0) > 2.0:
                self._last_near_log = now
                print(f"[near] 响了但未达阈值 tail={tail:.4f} 阈值={onset_at:.4f} "
                      f"base={base:.4f} bg={float(background_rms):.4f}", flush=True)
            return
        if not self._armed:
            return
        if now - self._last_audio_trigger < self._min_interval:

            self._armed = False
            return
        prev = self._last_audio_trigger
        self._armed = False
        self._last_audio_trigger = now
        self.audio_triggers += 1

        try:
            gap = "首次" if prev <= 0.0 else "%.2fs" % (now - prev)
            print(f"[trig] 音效触发#{self.audio_triggers} tail={tail:.4f} base={base:.4f} "
                  f"阈值={onset_at:.4f} 频带={getattr(self, '_last_band', '')} "
                  f"距上次={gap}", flush=True)
        except Exception:
            pass
        try:
            self._trigger_queue.put_nowait((now, TRIG_AUDIO))
        except queue.Full:
            pass





class Engine:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.matcher = None
        self.candidate_engine = None
        self.active = threading.Event()
        if getattr(args, "autostart", False):
            self.active.set()
        self.triggers: queue.Queue = queue.Queue(maxsize=2)
        self.results: queue.Queue = queue.Queue(maxsize=8)
        self._session_lock = threading.RLock()
        self._generation = 0
        self._work_context = threading.local()
        self.audio = AudioFeed(args.device, trigger_queue=self.triggers,
                               min_interval=args.min_interval,
                               blocksize_seconds=getattr(args, "audio_blocksize", None),
                               active_gate=self.active)
        self.stop_event = threading.Event()
        self.stats = {"triggers": 0, "matches": 0, "empty": 0, "errors": 0,
                      "sound_found": 0, "started": 0, "f8_toggles": 0,
                      "glitchy_triggers": 0, "windows_seen_max": 0}
        self._worker: threading.Thread | None = None
        self._heartbeat: threading.Thread | None = None
        self._f8: threading.Thread | None = None
        self._monitor_since = None
        self._stall_warned_session = -1
        self._last_trigger = 0.0
        self.busy = False
        self.last_result = None

        import learned as learned_mod
        self.learned = learned_mod.LearnedStore(
            Path(getattr(args, "learned_dir", "") or (ROOT / "learned")))


    def load(self) -> float:
        import candidates as candidates_mod
        import recognition

        t0 = time.time()


        self.matcher = recognition.SoundMatcher(str(ASSETS), str(DATABASE))
        self.candidate_engine = candidates_mod.CandidateEngine(str(DATABASE))
        elapsed = time.time() - t0
        print(
            f"[engine] 1.1.1 判定 + 提速实现：模板={len(self.matcher.templates)} "
            f"错误={len(self.matcher.errors)} 加载耗时={elapsed:.2f}s",
            flush=True,
        )
        for err in self.matcher.errors[:5]:
            print(f"[engine] 模板错误: {err}", flush=True)
        return elapsed


    def is_active(self) -> bool:
        """是否处于「开始监测」状态。"""
        return bool(self.active.is_set())

    def set_active(self, on: bool) -> bool:
        """切换监测代次；停止后重新开始可重建已结束的音频线程。"""
        on = bool(on)
        with self._session_lock:
            if on and self.stop_event.is_set():
                return False
            if on == self.active.is_set():
                return on
            self._generation += 1
            for pending in (self.triggers, self.results):
                try:
                    while True:
                        pending.get_nowait()
                except queue.Empty:
                    pass
            self.last_result = None
            if on:
                self.active.set()
                self.stats["started"] = self.stats.get("started", 0) + 1
                self._monitor_since = time.time()
                self._stall_warned_session = -1
                if getattr(self.audio, "_thread", None) is not None and not self.audio.running:
                    print("[audio] 用户重新开始监测：重建已结束的采集线程", flush=True)
                    self.audio.start()
                print("[gate] ▶ 已开始监测：从现在起才听音/识别（之前的音频已丢弃）", flush=True)
            else:
                self.active.clear()
                self._monitor_since = None
                print("[gate] ■ 已停止监测：不再录音、不再识别、不再保存文件", flush=True)
        return on

    def _session_current(self, generation) -> bool:
        with self._session_lock:
            return self.active.is_set() and not self.stop_event.is_set() and generation == self._generation

    def result_is_current(self, item) -> bool:
        return self._session_current(item.get("generation"))

    def _wait_for_session(self, seconds, generation) -> bool:
        deadline = time.monotonic() + max(0.0, float(seconds))
        while self._session_current(generation):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            if self.stop_event.wait(min(remaining, 0.05)):
                break
        return False

    def start_threads(self) -> None:
        self.audio.start()
        self._worker = threading.Thread(target=self._work_loop, name="match-worker", daemon=True)
        self._worker.start()
        self._f8 = threading.Thread(target=self._f8_loop, name="f8-watcher", daemon=True)
        self._f8.start()
        self._heartbeat = threading.Thread(target=self._heartbeat_loop, name="heartbeat",
                                           daemon=True)
        self._heartbeat.start()

    def _f8_loop(self) -> None:
        """只读 F8/F9/F10 上升沿：F8 开关监测、F9 标对、F10 标错。

        全部只读全局键态，不注册系统热键、不挂钩子、不模拟按键。
        """
        was_down = False
        self._f9_was = self._f10_was = False
        while not self.stop_event.is_set():
            try:
                down = f8_down()
                if down and not was_down:
                    self.stats["f8_toggles"] += 1
                    self.set_active(not self.active.is_set())
                was_down = down
                d9, d10 = f9_down(), f10_down()
                if d9 and not self._f9_was:
                    self.mark_last(True)
                if d10 and not self._f10_was:
                    self.mark_last(False)
                self._f9_was, self._f10_was = d9, d10
            except Exception:
                pass
            self.stop_event.wait(0.05)

    def _heartbeat_loop(self) -> None:
        """每 5 秒一行心跳：采集是否活着、有没有开始监测、识别计数。"""
        while not self.stop_event.is_set():
            if self.stop_event.wait(5.0):
                break
            try:
                bw = float(getattr(self.audio, "peak_rms", 0.0) or 0.0)
                disc = self.audio.discontinuities
                print(
                    f"[hb] 监测={'开' if self.active.is_set() else '关'} "
                    f"窗口={self.audio.windows} 峰值={bw:.4f} "
                    f"末窗={getattr(self.audio, '_last_rms', 0.0):.4f} 断点={disc} "
                    f"触发={self.stats['triggers']} 命中={self.stats['matches']} "
                    f"空={self.stats['empty']}",
                    flush=True,
                )
                self._quiet_device_warning(bw)
                self._stall_warning(self.stats["triggers"])
            except Exception:
                pass

    def _quiet_device_warning(self, peak: float) -> None:
        """抓到的声音一直很小且从未触发 → 十有八九是回环设备选错了。

        典型场景：系统默认设备是蓝牙耳机，但游戏输出到 Realtek 扬声器，
        于是回环抓到的是"没有游戏的"那条声道（峰值 <0.002、触发=0）。
        这里只提示一次，避免刷屏。
        """
        if getattr(self, "_quiet_warned", False):
            return
        if self.stats["triggers"] > 0 or self.audio.windows < 8 or peak >= 0.002:
            return
        self._quiet_warned = True
        try:
            import recorder

            devs = [d for d in recorder.list_devices() if d != "(default)"]
            cur = getattr(self.args, "device", None) or recorder.default_device_name()
            print(f"[device] 峰值只有 {peak:.4f} 且没有任何触发 —— 很可能是回环设备选错了。",
                  flush=True)
            print(f"[device] 当前抓的是：{cur}", flush=True)
            print(f"[device] 可选设备：{' | '.join(devs)}", flush=True)
            print('[device] 处置：确认游戏"音频输出"实际用的是哪个设备，然后用 '
                  '--device "设备名" 指定它；', flush=True)
            print("[device] 或是跑 tools/probe_devices.py 逐个设备量一次电平（拖着物品跑）。",
                  flush=True)
        except Exception:
            return

    def _stall_warning(self, triggers: int) -> None:
        """开始监测很久却一次都没触发 → 明确告警（设备选错/游戏静音/未播放）。"""
        if not self.active.is_set() or self._monitor_since is None:
            return
        monitor_id = int(self.stats.get("started", 0))
        if self._stall_warned_session == monitor_id:
            return
        elapsed = time.time() - self._monitor_since
        if elapsed < STALL_WARN_AFTER or triggers > 0:
            return
        self._stall_warned_session = monitor_id
        print("[stall] 已监测 %.0f 秒仍未听到任何可触发音效。请检查：" % elapsed, flush=True)
        print("[stall]   ① 游戏声音是否从『当前默认播放设备』输出（换过耳机/声卡要重选）；",
              flush=True)
        print("[stall]   ② 设备是否正确：--list-devices 查看，--device \"名字\" 指定；",
              flush=True)
        print("[stall]   ③ 游戏是否静音/音量过低；④ 是否忘记点「开始监测」（或按 F8）。",
              flush=True)


    def _run_match(self, samples, sr: int, limit: int, background_rms: float) -> list:
        """1.1.1 引擎统一入口：只用 recognition.SoundMatcher.match。"""
        return self.matcher.match(samples, sr, limit, background_rms)

    def _weak_candidates(self, samples, sr: int, limit: int, background_rms: float) -> list:
        """未达阈值时给出"最像的几名 + 分数"（低置信参考，不改变任何阈值判定）。"""
        try:
            import math

            import recognition
            import scipy.signal as signal

            x = np.asarray(samples, dtype=np.float32).reshape(-1)
            if sr != recognition.SAMPLE_RATE and len(x):
                g = math.gcd(int(sr), recognition.SAMPLE_RATE)
                x = signal.resample_poly(x, recognition.SAMPLE_RATE // g,
                                         int(sr) // g).astype(np.float32)
            if len(x) < 128:
                return []
            rows = list(self.matcher._rank(
                recognition.extract(x, reduce_background=True), background_rms))
            if 640 <= len(x) <= 48000:
                hp = self.matcher._highpass(x)
                rows += list(self.matcher._rank(
                    recognition.extract(hp, reduce_background=True), 0.0, filtered=True))
            best: dict = {}
            for r in rows:
                k = r.get("key")
                if k is None:
                    continue
                if k not in best or r.get("score", 0) > best[k].get("score", 0):
                    best[k] = r
            ordered = sorted(best.values(), key=lambda r: -float(r.get("score", 0)))[: max(1, limit)]


            out = []
            database = getattr(self.matcher, "database", {}) or {}
            for row in ordered:
                kind = row.get("kind")
                if kind == "special":
                    items = [i for i in database.get("special_items", [])
                             if i.get("name") == row.get("key")]
                elif kind == "item":
                    items = [i for i in database.get("items", [])
                             if i.get("name") == row.get("key")]
                else:
                    items = [i for i in database.get("items", [])
                             if i.get("sound_type") == row.get("key")]
                    if not items:
                        items = [{"name": row.get("key"), "sound_type": row.get("key")}]
                out.append({
                    "key": row.get("key"),
                    "kind": kind,
                    "score": int(round(float(row.get("score", 0.0)) * 100)),
                    "offset": int(row.get("offset", 0)),
                    "offset_seconds": float(row.get("offset_seconds", 0.0)),
                    "items": items,
                    "reason": "低置信参考：低于判定阈值，仅供参考",
                })
            return out
        except Exception:
            return []

    def _attempt(self, pressed_ts):
        """对一个窗口同时试「原始」与（可选）「降噪后」，取分数更高的一条路径。

        返回 ``(rows, reason, score, path, age, key)``，``key`` 用于跨次择优：
        有候选 > 缝隙少 > 分数高（带"凭空静音"的窗口波形被切断，分数再高也不可信）。
        """
        import recorder

        snap2 = self.audio.snapshot()
        if snap2 is None:
            return [], "", 0.0, "none", None, (0, 0, 0.0)
        win, bg, age2 = snap2


        trimmed, cut = _trim_previous_sound(win)
        if trimmed is not None:
            if self.args.verbose:
                print(f"[trim] 裁掉上一声 {cut / recorder.SAMPLE_RATE:.2f}s："
                      f"窗口 {len(win) / recorder.SAMPLE_RATE:.2f}s → "
                      f"{len(trimmed) / recorder.SAMPLE_RATE:.2f}s", flush=True)
            win = trimmed
            bg = recorder._background_level(win)
        cands = [("raw", win, bg)]


        gaps_win = 0
        try:
            gaps_win = int(recorder.zero_gap_stats(win)[0])
            if gaps_win > 0:
                seg = recorder.longest_nonzero_segment(win)
                if seg is not None and len(seg) >= 128:
                    cands.append(("segment", seg, recorder._background_level(seg)))
        except Exception:
            gaps_win = 0
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
        self._weak_snap = None
        for pathname, x, bg2 in cands:
            rr = self._run_match(x, recorder.SAMPLE_RATE, self.args.limit, bg2)
            ss = rr[0]["score"] if rr else 0
            if self.args.verbose and len(cands) > 1:
                print(f"[{pathname}] rows={len(rr)} score={ss} bg={bg2:.5f}", flush=True)
            key = (1 if rr else 0, -gaps_win, ss)
            if best_local is None or key > best_local[5]:
                best_local = (rr, self.matcher.last_reason, ss, pathname, age2, key)
                self._weak_snap = (x, bg2)
        if best_local is None:
            return [], "", 0.0, "none", age2, (0, 0, 0.0)
        return best_local

    def _work_loop(self) -> None:
        _lower_current_thread_priority()
        while not self.stop_event.is_set():
            try:
                payload = self.triggers.get(timeout=0.2)
            except queue.Empty:
                continue
            if payload is None:
                break
            try:
                pressed_at, source = (float(payload[0]), str(payload[1]) if len(payload) > 1
                                      else TRIG_AUDIO)
            except (TypeError, ValueError, IndexError):
                continue
            with self._session_lock:
                generation = self._generation
                if not self.active.is_set():
                    continue
                if self._monitor_since is not None and pressed_at < self._monitor_since:
                    continue
            try:
                self.handle_trigger(pressed_at, source, generation=generation)
            except Exception as exc:
                self.stats["errors"] += 1
                print(f"[error] 识别失败: {type(exc).__name__}: {exc}", flush=True)

    def handle_trigger(self, pressed_at: float, trigger_src: str = TRIG_AUDIO, generation=None) -> None:
        with self._session_lock:
            if generation is None:
                generation = self._generation
            if not self._session_current(generation):
                return
        self._work_context.generation = generation
        self.busy = True
        try:
            self._handle_trigger(pressed_at, trigger_src)
        finally:
            self.busy = False
            self._work_context.generation = None

    def _handle_trigger(self, pressed_at: float, trigger_src: str = TRIG_AUDIO) -> None:
        """一次触发：等 settle → 取音频窗口 → 识别 → 按时间窗多听几次取最优 → 推给面板。"""
        import recorder

        self.stats["triggers"] += 1
        self._last_trigger = time.time()
        t_start = time.perf_counter()
        trace_no = self.stats["triggers"]
        disc0 = self.audio.discontinuities
        generation = getattr(self._work_context, "generation", self._generation)
        if not self._wait_for_session(self.args.settle, generation):
            return

        snap = self.audio.snapshot()
        if snap is None:
            self._emit([], "音频尚未就绪（正在等待第一个回环窗口）。", 0.0, trigger_src,
                       t_start)
            print(f"[trace] 触发#{trace_no} 音频✗未就绪", flush=True)
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
                     + (f" 音效段{_seg_rms:.5f} 带宽{_bw / 1000:.1f}kHz" if _has_sound
                        else " 无音效段")
                     + (f" 静音缝{_gaps}处/{_gap_ms:.0f}ms" if _gaps else ""))

        rows, reason, best_score, best_path, age, _best_key = self._attempt(pressed_at)
        self._gate_snap = getattr(self, "_weak_snap", None)
        if age is not None and age > self.args.max_age:
            print(f"[audio] 窗口偏旧 age={age:.2f}s", flush=True)
        if best_score > 0:
            self.stats["sound_found"] += 1



        listen_max = float(getattr(self.args, "listen_max", 0.8) or 0.0)
        deadline = time.perf_counter() + max(0.0, listen_max)
        attempts = 0
        while True:
            if not self._session_current(generation):
                return
            if best_score >= GOOD_ENOUGH_SCORE:
                break
            if attempts >= max(0, int(self.args.retry)):
                if listen_max <= 0 or time.perf_counter() >= deadline:
                    break
                if best_score > 0 and attempts >= max(1, int(self.args.retry)) * 2:
                    break
            attempts += 1
            if not self._wait_for_session(self.args.retry_delay, generation):
                return
            r2, rs2, s2, p2, _age2, key2 = self._attempt(pressed_at)
            if s2 > 0:
                self.stats["sound_found"] += 1
            if self.args.verbose:
                print(f"[retry{attempts}] rows={len(r2)} score={s2} ({p2}) "
                      f"缝隙{-key2[1]} （当前最好 {best_score}）", flush=True)
            if key2 > _best_key:
                rows, reason, best_score, best_path, _best_key = r2, rs2, s2, p2, key2
                self._gate_snap = getattr(self, "_weak_snap", None)




        if rows and getattr(self.args, "require_sound_segment", False):
            seg_rms, seg_bg = self._recognized_segment()
            floor = max(MIN_TRIGGER_SEGMENT_RMS, seg_bg * TRIGGER_SEGMENT_BG_RATIO)
            if seg_rms < floor:
                rows = []
                best_score = 0.0
                reason = (f"窗口里没有清晰音效段（最响一段 {seg_rms:.5f} < 门槛 {floor:.5f}"
                          f"＝背景 {seg_bg:.5f} 的 {TRIGGER_SEGMENT_BG_RATIO:.0f} 倍），"
                          f"本次按环境音忽略（--require-sound-segment 开启时才会这样）")
                self.stats["silent_triggers"] = self.stats.get("silent_triggers", 0) + 1
                print(f"[trace] 触发#{trace_no} 判定为环境音（音效段 {seg_rms:.5f} < "
                      f"{floor:.5f}）→ 不出结果", flush=True)


        try:
            win = None
            if self.matcher is not None:
                snap = getattr(self, "_gate_snap", None) or getattr(self, "_weak_snap", None)
                if snap is not None:
                    win = snap[0]
            if win is not None and len(np.asarray(win)) >= 128:
                top = rows[0] if rows else None
                self.learned.stash(
                    win,
                    top["key"] if top else None,
                    int(round(float(top["score"]))) if top else 0,
                    str(top.get("path", "")) if top else "",
                    reason or "",
                )
        except Exception as exc:
            print(f"[learn] 暂存失败：{exc}", flush=True)

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
                snapw = getattr(self, "_weak_snap", None) or self.audio.snapshot()
                if snapw is not None:
                    weak = self._weak_candidates(snapw[0], recorder.SAMPLE_RATE,
                                                 self.args.limit, snapw[1])
                    if weak:
                        rows = weak
                        head = weak[0]
                        reason = (f"未达判定阈值（0.88/0.85/0.82/0.78，歧义余量 0.045）；"
                                  f"最像的是 {head['key']} {head['score']} 分，仅供参考。")
                    else:
                        reason = ("未达判定阈值，且没有可参考的候选（窗口里可能没有清晰音效）。"
                                  + str(reason))

        top = rows[0] if rows else None
        try:
            disc_delta = int(recorder.discontinuities()) - disc0
        except Exception:
            disc_delta = 0
        if disc_delta > 0:
            self.stats["glitchy_triggers"] = self.stats.get("glitchy_triggers", 0) + 1
        trace = (f"触发#{trace_no} 音频✓{aud_state} 采集断点+{disc_delta} "
                 f"识别{'✓' if rows else '✗'}"
                 + (f" rows={len(rows)} top={top['key']}/{top['score']}({best_path})"
                    if top else f" rows=0 {str(reason)[:20]}")
                 + f" 用时{time.perf_counter() - t_start:.2f}s")
        print(f"[trace] {trace}", flush=True)
        if disc_delta > 0:
            print(f"[glitch] 本次识别期间采集丢帧 {disc_delta} 次 → 音频被切断，"
                  f"听到的就是『掉帧感』，识别结果不可靠。可试 --audio-blocksize 0.3",
                  flush=True)
        if _gaps > 0:
            print(f"[glitch] 本次窗口检测到 {_gaps} 处凭空静音（共 {_gap_ms:.0f}ms，"
                  f"最长 {_gap_max:.0f}ms）→ soundcard 在设备无帧时**自动补零**，"
                  f"波形被切断，本次识别结果不可靠（已优先采用无缝隙窗口）。", flush=True)
        if self.args.verbose:
            print(f"[match] rows={len(rows)} top={top['key'] if top else '-'} "
                  f"score={top['score'] if top else '-'} reason={reason}", flush=True)
        self._emit(rows, reason, best_score, trigger_src, t_start)

    def _recognized_segment(self) -> tuple:
        """返回 (最响一段的 RMS, 该窗口的背景电平)；数据取真正送识别的那条窗口。"""
        snap = getattr(self, "_gate_snap", None) or getattr(self, "_weak_snap", None)
        if snap is None:
            snap = self.audio.snapshot()
        if snap is None:
            return 0.0, 0.0
        win, bg = snap[0], float(snap[1] or 0.0)
        try:
            import recorder
            seg = recorder.loudest_segment(win)
            return float(np.sqrt(np.mean(np.asarray(seg, dtype=np.float64) ** 2))), bg
        except Exception:
            return 0.0, bg

    def mark_last(self, correct: bool) -> str:
        """把"最近一次送识别的窗口"标为对/错（面板按钮 / F9 / F10 都走这里）。"""
        msg = self.learned.mark(bool(correct))
        self._mark_at = time.time()
        self._mark_msg = msg
        print(f"[learn] {msg}", flush=True)
        return msg

    def _emit(self, rows, reason, score, trigger_src, t_start) -> None:
        generation = getattr(self._work_context, "generation", None)
        if generation is None:
            generation = self._generation

        if not self._wait_for_session(RESULT_DELAY_S, generation):
            return
        with self._session_lock:
            if not self._session_current(generation):
                return
            payload = {"rows": rows, "reason": reason, "score": score,
                       "trigger_src": trigger_src, "generation": generation,
                       "took": max(0.0, time.perf_counter() - t_start), "t": time.time()}
            self.last_result = {"at": payload["t"], "rows": rows, "reason": reason}
            try:
                self.results.put_nowait(payload)
            except queue.Full:
                try:
                    self.results.get_nowait()
                except queue.Empty:
                    pass
                self.results.put_nowait(payload)
                self.stats["results_dropped"] = self.stats.get("results_dropped", 0) + 1


    def shutdown(self) -> None:
        self.stop_event.set()
        self.set_active(False)
        self.audio.stop()
        try:
            self.triggers.put_nowait(None)
        except queue.Full:
            pass
        for th in (self._worker, self._f8, self._heartbeat,
                   getattr(self.audio, "_thread", None), getattr(self.audio, "_profile_job", None)):
            if th is not None and th is not threading.current_thread():
                try:
                    th.join(timeout=2.0)
                    if th.is_alive():
                        print(f"[exit] 线程停止超时：{th.name}；入口将硬退出，避免解释器收尾竞态", file=sys.stderr, flush=True)
                except Exception as exc:
                    print(f"[exit] 等待线程失败：{exc}", file=sys.stderr, flush=True)
        print(f"[stats] {self.stats}", flush=True)


def _detection_status_line(engine) -> str:
    """面板底栏文本：**只讲检测状态**，不带原因/触发/计数（那些只写日志）。

    顺序：音频错误 → 回环未就绪 → 正在识别 → 刚识别出结果 → 无信号告警 → 等待音效。
    「正在识别 / 已识别 xxx」是本版新增的实时反馈：以前底栏永远停在
    「等待拖动物品…」，玩家拖动时看不到任何进展（用户实测反馈）。
    """
    mark_msg = getattr(engine, "_mark_msg", "")
    if mark_msg:
        try:
            fresh = time.time() - float(getattr(engine, "_mark_at", 0.0)) <= 6.0
        except (TypeError, ValueError):
            fresh = False
        if fresh:
            return "✓ " + mark_msg if not mark_msg.startswith("(") else mark_msg
    audio = engine.audio
    if audio.last_error:
        return f"音频错误：{audio.last_error}（停止后重新开始可重试）"
    if getattr(audio, "_thread", None) is not None and not audio.running:
        return "音频采集已停止：请停止后重新开始监测重试"
    age = getattr(audio, "latest_age", None)
    if age is not None and age > 3.0:
        return "音频窗口未更新：设备可能中断，正在等待恢复"
    if audio.windows == 0:
        return "等待音效…（正在初始化回环音频）"
    if getattr(engine, "busy", False):
        return RECOGNIZING_STATUS
    result = getattr(engine, "last_result", None)
    if result is not None:
        try:
            age = time.time() - float(result.get("at", 0.0))
        except (TypeError, ValueError):
            age = RECENT_RESULT_SECONDS + 1.0
        rows = result.get("rows") or []
        if age <= RECENT_RESULT_SECONDS and rows:
            top = rows[0]
            return "已识别：%s %s 分（点格数图标可筛选）" % (
                top.get("key", ""), top.get("score", 0))
    if audio.windows >= 25 and audio.peak_rms < 0.0004:
        return ("回环音频无信号：请确认游戏声音从当前设备播放，"
                "或用 --list-devices / --device 指定正确设备")
    return _MONITORING_STATUS





def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="行商助手 · 声控版 · 1.1.1 识别移植版（纯音频识别，不截屏、不读画面，只读）")
    p.add_argument("--device", default=None,
                   help="回环设备名；默认自动取系统当前播放设备")
    p.add_argument("--list-devices", action="store_true",
                   help="列出可选播放/回环设备后退出")
    p.add_argument("--diag", action="store_true",
                   help="只测音频链路（列出设备、采几秒、报峰值/断点），不识别物品")
    p.add_argument("--diag-seconds", type=float, default=6.0,
                   help="--diag 采集时长秒，默认 6")
    p.add_argument("--audio-doctor", action="store_true",
                   help="逐端点自检（格式 / 能否打开 / 实际电平），报告写 logs\\audio_doctor.txt；"
                        "排查『某些型号耳机没声音』用")
    p.add_argument("--doctor-seconds", type=float, default=2.0,
                   help="--audio-doctor 每个端点采集秒数，默认 2")
    p.add_argument("--doctor-tone", action="store_true",
                   help="--audio-doctor 时向每个端点播 1kHz 短测试音，验证回环复制本身通不通")
    p.add_argument("--audio-blocksize", type=float, default=0.2,
                   help="WASAPI 内部缓冲秒数（默认 0.2）。调大能容忍采集线程被游戏抢走"
                        "更久，缓解'掉帧感'与断点，代价是回调更少、延迟略增")
    p.add_argument("--listen-max", type=float, default=0.8,
                   help="一次触发最多持续听多久（秒），默认 0.8；漏音效多时可调到 1.5；0=只用 --retry")
    p.add_argument("--limit", type=int, default=5, help="最多返回的候选声音组数，默认 5")
    p.add_argument("--settle", type=float, default=0.03,
                   help="触发后等待再取音频的秒数，默认 0.03")
    p.add_argument("--min-interval", type=float, default=0.4,
                   help="两次音频触发之间的最小间隔秒，默认 0.4（同一次音效只触发一次由"
                        "起音判定保证，这个值只用来防抖；想更保守可调大）")
    p.add_argument("--retry", type=int, default=2,
                   help="触发后继续听取音效的额外次数（取最高分），默认 2；0=只听一次")
    p.add_argument("--retry-delay", type=float, default=0.2,
                   help="继续听取的间隔秒，默认 0.2")
    p.add_argument("--max-age", type=float, default=0.6,
                   help="音频窗口最大可接受年龄秒，超过只写日志（不改判定）")
    p.add_argument("--denoise", action="store_true",
                   help="启用跨窗谱减降噪（实验性，默认关闭）：与原始路径同时算、取更高分")
    p.add_argument("--show-weak", action="store_true",
                   help="未达阈值时也显示最像的几个候选及其分数（低置信，仅参考）")
    p.add_argument("--min-score", type=float, default=70.0,
                   help="显示下限：分数低于该值就不出结果（默认 70）。只影响是否上屏，识别本身的"
                        "阈值不改；设 0 可关掉。实测同一物品在吵闹环境下会掉到 50~67 分，"
                        "这些结果基本不可信，显示出来反而像'识别错了'")
    p.add_argument("--autostart", action="store_true",
                   help="启动即开始监测（默认关闭：必须先点面板『开始监测』或按 F8，"
                        "避免在你没玩的时候录音/识别到其它应用）")
    p.add_argument("--log", default=None,
                   help="把控制台输出同时写入该文件（UTF-8，便于事后排查/发回分析）")
    p.add_argument("--learned-dir", default="",
                   help="样本暂存目录（默认程序目录下 learned\\）。F9 标对 / F10 标错只做归档，"
                        "本版（1.1.1 引擎）不会把它加载成模板")
    p.add_argument("--require-sound-segment", action="store_true",
                   help="只认『窗口里最响一段 >= 背景的 3 倍』的触发（默认关闭）。注意：实测同一局"
                        "10 个窗口里有 5 个被这个判据拦掉，而它们谱分高达 0.85~0.91 —— 会把真物品"
                        "一起丢掉，所以默认不启用")
    p.add_argument("--verbose", action="store_true", help="打印每次识别详情")
    return p.parse_args(argv)


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


def _install_crash_logging() -> None:
    """未捕获异常也要落进日志：UI 线程 / 工作线程崩了都能查到调用栈。

    面板 ``Panel.start()`` 会把事件循环里的异常吞掉（不让进程崩），但那样**只
    剩一个消失的窗口、没有任何记录**——用户报"闪退"时就是这种最难查的情况。
    """
    import threading
    import traceback

    def _dump(prefix: str, exc_type, exc, tb) -> None:
        try:
            print(f"[fatal] {prefix}：{exc_type.__name__}: {exc}", flush=True)
            traceback.print_exception(exc_type, exc, tb)
            for stream in (sys.__stdout__, sys.__stderr__):
                try:
                    stream.flush()
                except Exception:
                    pass
        except Exception:
            pass

    sys.excepthook = lambda t, e, tb: _dump("未捕获异常（主线程）", t, e, tb)

    def _thread_hook(args) -> None:
        _dump(f"线程 {getattr(args.thread, 'name', '?')} 崩了",
              args.exc_type, args.exc_value, args.exc_traceback)

    try:
        threading.excepthook = _thread_hook
    except Exception:
        pass



    fault_fh = None
    try:
        import faulthandler
        from pathlib import Path as _Path

        logs_dir = app_dir() / "logs"
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            local = os.environ.get("LOCALAPPDATA")
            if local:
                logs_dir = Path(local) / "行商助手声控版" / "logs"
                logs_dir.mkdir(parents=True, exist_ok=True)
        fault_fh = open(logs_dir / "native_crash.log", "a",
                        encoding="utf-8", errors="replace", buffering=1)
        fault_fh.write("\n=== 启动 %s ===\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        fault_fh.flush()
        faulthandler.enable(file=fault_fh, all_threads=True)
        globals()["_FAULT_FILE"] = fault_fh
    except Exception:
        fault_fh = None

    try:
        from PyQt5.QtCore import qInstallMessageHandler

        def _qt_message(_mode, _context, message):
            if fault_fh is not None:
                try:
                    fault_fh.write("[qt] %s\n" % message)
                    fault_fh.flush()
                except Exception:
                    pass

        qInstallMessageHandler(_qt_message)
    except Exception:
        pass


def _install_log_tee(path: str) -> bool:
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)

        try:
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                os.replace(path, path + ".1")
        except OSError:
            pass
        fh = open(path, "w", encoding="utf-8", errors="replace", buffering=1)
        sys.stdout = _Tee(sys.__stdout__, fh)
        sys.stderr = _Tee(sys.__stderr__, fh)
        print(f"[log] 控制台输出同时写入 {path}（UTF-8）", flush=True)
        return True
    except Exception as exc:
        print(f"[log] 无法写入 {path}: {type(exc).__name__}: {exc}", flush=True)
        return False


def run_diag(args) -> int:
    """--diag：只验证"有没有声音进来"，不识别物品、不出面板。"""
    import recorder

    print("[diag] == 声控版音频链路自检 ==", flush=True)
    default = recorder.default_device_name()
    print(f"[diag] 系统默认播放设备 = {default}", flush=True)
    print("[diag] 可选设备：", flush=True)
    for name in recorder.list_devices():
        print(f"  {name}", flush=True)
    chosen = args.device or default
    print(f"[diag] 本次使用 = {chosen}", flush=True)

    stop = threading.Event()
    feed = AudioFeed(args.device, trigger_queue=None, min_interval=0.0,
                     blocksize_seconds=args.audio_blocksize)

    feed._active_gate = None
    feed.start()
    seconds = max(1.0, float(args.diag_seconds))
    print(f"[diag] 采样 {seconds:.0f} 秒…（请让游戏播放一点声音）", flush=True)
    t0 = time.time()
    triggers = 0
    last_prev = 0.0
    while time.time() - t0 < seconds:
        time.sleep(0.5)
        snap = feed.snapshot()
        if snap is None:
            print("[diag] 还没有窗口…", flush=True)
            continue
        window, bg, age = snap
        try:
            seg = recorder.loudest_segment(window)
            seg_rms = float(np.sqrt(np.mean(np.asarray(seg, dtype=np.float64) ** 2)))
            bw = recorder.spectral_rolloff(seg)
        except Exception:
            seg_rms, bw = 0.0, 0.0
        try:
            gaps = int(recorder.zero_gap_stats(window)[0])
        except Exception:
            gaps = 0
        print(f"[diag] t={time.time() - t0:4.1f}s age={age:.2f}s 窗口={feed.windows} "
              f"背景={bg:.5f} 音效段={seg_rms:.5f} 带宽={bw / 1000:.1f}kHz "
              f"静音缝={gaps} 断点={feed.discontinuities}", flush=True)
        last_prev = seg_rms
    feed.stop()
    try:
        feed._thread.join(timeout=2.0)
    except Exception:
        pass
    print(f"[diag] 采集窗口数 = {feed.windows}，断点 = {feed.discontinuities}", flush=True)
    if feed.last_error:
        print(f"[diag] ✗ 采集报错：{feed.last_error}", flush=True)
        print("[diag]   处置：确认有正在使用的播放设备；换耳机后要重新指定 --device；"
              "独占模式(WASAPI exclusive)会抢不到回环。", flush=True)
        return 2
    if feed.windows == 0:
        print("[diag] ✗ 一帧都没采到：设备选错或系统没有播放设备。", flush=True)
        return 2
    if feed.peak_rms < 0.0004:
        print("[diag] ⚠ 采到了窗口但几乎全是静音：游戏没声音/静音/不是这个设备在放。", flush=True)
        return 1
    print(f"[diag] ✓ 音频链路正常（峰值 {feed.peak_rms:.4f}）。"
          f"若面板仍不识别，多半是没点「开始监测」。", flush=True)
    return 0


def run_doctor(args) -> int:
    """--audio-doctor：逐个播放端点自检，报告写 logs\\audio_doctor.txt。

    为什么需要：用户报「某些型号耳机完全没声音、管理员也不行」时，`--diag` 只有一行
    报错，不足以定位。这里把每个播放端点单独开回环采一遍，输出：
      * 端点默认格式（tag/位深/采样率/声道/blob 字节数）—— 判断是否踩到
        「非 32-bit float / 非 40 字节」那类格式；
      * 能否打开 + HRESULT 中文原因 —— 判断是否被独占/服务未运行/格式不支持；
      * 实际电平、静音率、5–8kHz 占比 —— 判断声音到底在哪个端点；
      * 可选 --doctor-tone：向该端点播 1kHz 短测试音再采，区分「驱动不支持回环复制」
        和「游戏声音走了别的设备」。
    """
    import recorder

    out_dir = app_dir() / "logs"
    out_path = out_dir / "audio_doctor.txt"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        local = os.environ.get("LOCALAPPDATA")
        if local:
            out_dir = Path(local) / "行商助手声控版" / "logs"
            out_path = out_dir / "audio_doctor.txt"
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
    fh = None
    try:
        fh = open(out_path, "w", encoding="utf-8", buffering=1)
    except Exception:
        fh = None

    def write(msg: str = "") -> None:
        text = str(msg)
        try:
            print(text, flush=True)
        except Exception:
            pass
        if fh is not None:
            try:
                fh.write(text + "\n")
                fh.flush()
            except Exception:
                pass

    write("行商助手声控版 · 音频端点自检（--audio-doctor）")
    write("时间 " + time.strftime("%Y-%m-%d %H:%M:%S"))
    write("程序 " + sys.executable)
    write("")


    try:
        import comtypes

        try:
            comtypes.CoInitialize()
        except OSError:
            pass
    except ImportError:
        try:
            ctypes.windll.ole32.CoInitialize(None)
        except Exception:
            pass

    report = []
    infrastructure_error = None
    try:
        report = recorder.doctor(seconds=max(0.5, float(args.doctor_seconds)),
                                 tone=bool(args.doctor_tone), writer=write)
    except Exception as exc:
        import traceback

        infrastructure_error = exc
        write("[doctor] ✗ 自检本身失败：%s: %s" % (type(exc).__name__, exc))
        try:
            write(traceback.format_exc())
        except Exception:
            pass

    write("")
    write("== 结论 ==")
    opened = [r for r in report if r.get("ok")]
    failed = [r for r in report if not r.get("ok")]
    live = [r for r in opened if float(r.get("peak") or 0.0) >= 0.0004]
    if failed:
        write("· 打不开的端点 %d 个（原因见上面每条 ✗）：" % len(failed))
        for r in failed:
            write("  - %s → %s" % (r["name"], r.get("error") or "未知"))
    if infrastructure_error is not None:
        write("· 音频自检基础设施失败，请发送上面的完整异常栈；不能据此判断为无播放设备。")
    elif not report:
        write("· 没有枚举到任何播放端点：系统没有活动音频输出，或 Windows 音频服务未运行。")
    elif live:
        write("· 有信号的端点 %d 个：%s" % (len(live), " | ".join(r["name"] for r in live)))
        write("  → 这台机器的 WASAPI 回环本身可用；游戏声音必须落在这些端点之一。")
        write("    若游戏播放时报告里全都没信号，说明游戏走了别的输出设备（或被独占）。")
    elif opened:
        write("· 所有端点都能打开，但采到的全是静音。")
        if args.doctor_tone:
            write("  已经播过 1kHz 测试音仍采不到 → 这些设备的驱动不支持 WASAPI 回环复制，")
            write("  需要虚拟声卡（VB-CABLE / VoiceMeeter）或『立体声混音』兜底。")
        else:
            write("  加 --doctor-tone 再跑一次，可区分『设备不支持回环』与『游戏没出声』。")
    write("")
    write("报告已写入：" + str(out_path))
    try:
        if fh is not None:
            fh.close()
    except Exception:
        pass
    return 2 if infrastructure_error is not None or not opened else 0


def main(argv=None) -> int:
    args = parse_args(argv)

    if not args.log and not (args.diag or args.audio_doctor or args.list_devices):

        try:
            default_dir = app_dir() / "logs"
            default_dir.mkdir(parents=True, exist_ok=True)
            args.log = str(default_dir / "run_log.txt")
        except OSError:
            pass
    if args.log:
        _install_log_tee(args.log)
    _install_crash_logging()
    try:
        import shutdown as exitlog
        exitlog.install("main", argv=argv)
    except Exception:
        pass

    if args.diag:
        return run_diag(args)

    if args.audio_doctor:
        return run_doctor(args)

    if args.list_devices:
        import recorder

        print("[devices]", flush=True)
        for name in recorder.list_devices():
            print(f"  {name}", flush=True)
        print(f"[default] {recorder.default_device_name()}", flush=True)
        return 0

    engine = Engine(args)
    _print_env_banner()
    _set_process_priority()
    engine.load()


    audio_samplerate = None
    audio_device = None
    try:
        import recorder as _rec

        _chosen = args.device or _rec.default_device_name() or "(系统默认设备)"
        print(f"[audio] 回环设备 = {_chosen}", flush=True)
        print(f"[audio] 可选设备: {' | '.join(_rec.list_devices())}", flush=True)
        print("[audio] 若长时间无声音，请用 --list-devices 查看并用 --device \"设备名\" 指定",
              flush=True)
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

    engine.start_threads()

    gate_state = {"last": None}

    def detection_status_line() -> str:
        return _detection_status_line(engine)

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
        for _ in range(4):
            try:
                item = engine.results.get_nowait()
            except queue.Empty:
                break

            with engine._session_lock:
                if not engine.result_is_current(item):
                    continue
                panel.show_candidates(item.get("rows") or [],
                                      last_reason=item.get("reason") or "",
                                      trigger_src=item.get("trigger_src") or "")

    timer = QTimer()
    timer.timeout.connect(pump)
    timer.start(80)

    try:
        panel.start()
    except KeyboardInterrupt:
        print("[exit] 控制台 Ctrl+C 中断", flush=True)
    finally:

        err = getattr(panel, "_start_error", None)
        print(f"[exit] UI 循环结束：{('UI 异常 ' + err) if err else '窗口被关闭 / stop()'}", flush=True)
        timer.stop()
        engine.shutdown()
    return 0


def _source_entrypoint():
    """源码入口也硬退出；普通导入与自测不执行此函数。"""
    from shutdown import _hard_exit
    code, reason = 0, "source main done"
    try:
        code = main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        reason = f"source SystemExit {exc.code!r}"
    except KeyboardInterrupt:
        code, reason = 130, "source KeyboardInterrupt"
    except BaseException:
        import traceback
        traceback.print_exc()
        code, reason = 1, "source unhandled exception"
    _hard_exit(code if isinstance(code, int) else 0, reason)


if __name__ == "__main__":
    _source_entrypoint()
