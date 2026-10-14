"""Continuous Windows speaker-loopback capture using the reference window cadence.

相对样本的**健壮性改动**（算法与接口不变，见各常量注释）：
  1) `blocksize`：样本用 soundcard 默认值（=设备最小周期，通常约 10ms），WASAPI 缓冲余量极小；
     游戏内 CPU/GPU 争抢时缓冲溢出，soundcard 报 "data discontinuity"（丢帧）。
     这里显式加大到 BLOCKSIZE_SECONDS。
  2) 环形缓冲改为 numpy 定长环（`_WindowRing`），去掉逐样本 `deque.extend(block.tolist())`
     的 Python 级开销，缩短持 GIL 时间。
  3) 采集线程提升到高优先级（`SetThreadPriority`），并统计丢帧次数供 UI 提示。
"""

from __future__ import annotations

import os
import sys
import threading
import time
import warnings
from collections import deque

import numpy as np

# --- 环境适配（样本之外，唯一的非算法改动）---------------------------------
# 样本运行时 soundcard / comtypes 已装进解释器；本机没有，两个包被放在
# <项目根>\deps 下，所以在导入 soundcard 之前把该目录注入 sys.path。
# 只是路径注入，不开设备、不建窗口，import 期无副作用。
_DEPS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deps")
if os.path.isdir(_DEPS_DIR) and _DEPS_DIR not in sys.path:
    sys.path.insert(0, _DEPS_DIR)

SAMPLE_RATE = 44100
BLOCK = 1024
WINDOW_SECONDS = 1.5
UPDATE_SECONDS = 0.18
MIN_PEAK_RMS = 0.0004

# WASAPI 采集缓冲时长。默认（None）=设备最小周期（Realtek 约 10ms）→ 游戏内极易溢出丢帧。
# 80ms 在"给足余量"与"不显著增加延迟"之间取平衡（我们的窗口是 1.5s，触发后还会等 0.14s）。
BLOCKSIZE_SECONDS = 0.08

# 丢帧计数（WASAPI AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY）
_DISCONTINUITIES = 0
_hook_installed = False
_previous_showwarning = None


def discontinuities() -> int:
    """本次运行中采集丢帧（数据不连续）的累计次数。0 表示采集健康。"""
    return _DISCONTINUITIES


def _install_warning_hook() -> None:
    """只装一次：把 soundcard 的丢帧警告变成计数 + 限流提示（避免刷屏）。"""
    global _hook_installed, _previous_showwarning
    if _hook_installed:
        return
    _previous_showwarning = warnings.showwarning
    try:
        from soundcard.mediafoundation import SoundcardRuntimeWarning

        # 默认过滤器对同一位置只报一次，会漏计；这里对该类警告始终上报
        warnings.filterwarnings("always", message=".*discontinuity.*",
                                category=SoundcardRuntimeWarning)
    except Exception:
        pass

    def _showwarning(message, category, filename, lineno, file=None, line=None):
        global _DISCONTINUITIES
        if "discontinuity" in str(message).lower():
            _DISCONTINUITIES += 1
            n = _DISCONTINUITIES
            if n <= 2 or n in (10, 100) or n % 500 == 0:
                print(f"[audio] 采集丢帧 第{n}次（CPU 争抢或设备被独占）；"
                      f"建议关闭游戏内录屏/降低画质，或换 --device", flush=True)
            return
        return _previous_showwarning(message, category, filename, lineno, file, line)

    warnings.showwarning = _showwarning
    _hook_installed = True


def _raise_capture_thread_priority() -> bool:
    """把当前（采集）线程提到**最高**优先级（TIME_CRITICAL），降低被游戏抢占的概率。

    只读式系统调用：不做任何输入模拟。
    """
    try:
        import ctypes

        THREAD_PRIORITY_TIME_CRITICAL = 15
        k32 = ctypes.windll.kernel32
        # 显式声明原型：GetCurrentThread 返回伪句柄 (HANDLE)-2，不声明容易被当成 32 位整数
        k32.GetCurrentThread.restype = ctypes.c_void_p
        k32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
        k32.SetThreadPriority.restype = ctypes.c_int
        h = k32.GetCurrentThread()
        if k32.SetThreadPriority(h, THREAD_PRIORITY_TIME_CRITICAL):
            return True
        err1 = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
        if k32.SetThreadPriority(h, 2):              # 权限不足时退回 HIGHEST
            return True
        try:
            import sys as _sys

            _sys.stderr.write(f"[audio] 线程优先级提升失败（GetLastError={err1}）\n")
        except Exception:
            pass
        return False
    except Exception:
        return False


def _enter_mmcss() -> tuple | None:
    """把当前线程注册进 Windows MMCSS（"Pro Audio"），这是 Windows 为音频线程
    提供的**防丢帧**机制：让音频线程以受保护的高优先级被调度。

    音频采集线程若被游戏/反作弊抢走 CPU 超过缓冲长度，WASAPI 就会报
    AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY —— 用户听到的"掉帧感"（click）就是它，
    而且断点会把音效波形切断，识别必然失败。失败时返回 None（功能照旧）。
    """
    try:
        import ctypes

        avrt = ctypes.windll.avrt
        task_index = ctypes.c_ulong(0)
        for name in ("Pro Audio", "Audio"):
            handle = avrt.AvSetMmThreadCharacteristicsW(name, ctypes.byref(task_index))
            if handle:
                # AVRT_PRIORITY_CRITICAL = 2（若不允许则退回 HIGHEST=1）
                if not avrt.AvSetMmThreadPriority(handle, 2):
                    avrt.AvSetMmThreadPriority(handle, 1)
                return (handle, task_index.value, name)
        return None
    except Exception:
        return None


def _leave_mmcss(state) -> None:
    try:
        if state:
            import ctypes

            ctypes.windll.avrt.AvRevertMmThreadCharacteristics(state[0])
    except Exception:
        pass


class _WindowRing:
    """定长环形缓冲：始终保存最近 `window_frames` 个样本，按时间顺序取出。

    替代 `deque(maxlen=N)` + `np.fromiter(...)`：每块只做一次 numpy 小拷贝，
    不再把 1024 个样本逐个转成 Python float，显著缩短持 GIL 时间。
    """

    __slots__ = ("_buf", "_pos", "_filled", "_n")

    def __init__(self, window_frames: int):
        self._buf = np.zeros(window_frames, dtype=np.float32)
        self._pos = 0
        self._filled = 0
        self._n = window_frames

    @property
    def filled(self) -> int:
        return self._filled

    def reset(self) -> None:
        """清空环形缓冲：用于「开始监测」时丢弃开始之前的音频，不残留旧内容。"""
        self._buf[:] = 0.0
        self._pos = 0
        self._filled = 0

    def write(self, chunk: np.ndarray) -> None:
        n = int(chunk.shape[0])
        if n <= 0:
            return
        if n >= self._n:
            self._buf[:] = chunk[-self._n:]
            self._pos = 0
            self._filled = self._n
            return
        end = self._pos + n
        if end <= self._n:
            self._buf[self._pos:end] = chunk
        else:
            split = self._n - self._pos
            self._buf[self._pos:] = chunk[:split]
            self._buf[: n - split] = chunk[split:]
        self._pos = end % self._n
        if self._filled < self._n:
            self._filled = min(self._n, self._filled + n)

    def window(self) -> np.ndarray:
        """按时间顺序返回最近 window_frames 个样本（副本）。"""
        if self._filled < self._n:
            if self._pos == 0:
                return self._buf[: self._filled].copy()
            return np.concatenate([self._buf[self._pos: self._filled], self._buf[: self._pos]])
        if self._pos == 0:
            return self._buf.copy()
        return np.concatenate([self._buf[self._pos:], self._buf[: self._pos]])

# COM 生命周期：comtypes 优先，缺失时回退 ctypes.windll.ole32。
# 三个函数里的结构均与样本一致（含 finally 里的 `'comtypes' in locals()` 判定）。


def list_devices() -> list[str]:
    initialized = False
    try:
        import comtypes

        comtypes.CoInitialize()
        initialized = True
    except ImportError:
        import ctypes

        ctypes.windll.ole32.CoInitialize(None)
        initialized = True
    import soundcard as sc

    try:
        return [str(m.name) for m in sc.all_speakers()]
    finally:
        if initialized:
            try:
                if "comtypes" in locals():
                    comtypes.CoUninitialize()
                else:
                    import ctypes

                    ctypes.windll.ole32.CoUninitialize()
            except Exception:
                pass


def default_device_name() -> str:
    """Return the playback device Windows currently marks as default."""
    initialized = False
    try:
        import comtypes

        comtypes.CoInitialize()
        initialized = True
    except ImportError:
        import ctypes

        ctypes.windll.ole32.CoInitialize(None)
        initialized = True
    import soundcard as sc

    try:
        try:
            speaker = sc.default_speaker()
            # 样本为 `str(speaker.name) if speaker is not None else ""`；
            # 契约 §3.2 要求“失败返回 ''”，故额外把取设备异常也归一成空串。
            return str(speaker.name) if speaker is not None else ""
        except Exception:
            return ""
    finally:
        if initialized:
            try:
                if "comtypes" in locals():
                    comtypes.CoUninitialize()
                else:
                    import ctypes

                    ctypes.windll.ole32.CoUninitialize()
            except Exception:
                pass


def _background_level(samples: np.ndarray) -> float:
    """Estimate the lower, mostly-ambient level from short RMS frames."""
    frame = max(1, SAMPLE_RATE // 50)
    usable = samples[: len(samples) // frame * frame]
    if len(usable) == 0:
        return 0.0
    chunks = usable.reshape(-1, frame)
    levels = np.sqrt(np.mean(chunks.astype(np.float64) ** 2, axis=1))
    return float(np.quantile(levels, 0.2))


def loudest_segment(samples: np.ndarray, seconds: float = 0.4) -> np.ndarray:
    """取窗内最响的连续一段（默认 0.4s）。

    为什么需要：1.5s 窗口里音效通常只占 0.3~0.4s，其余是持续背景；
    直接对整窗做频谱统计会被低频背景主导，得出"音效没有高频"的错误结论。
    """
    try:
        x = np.asarray(samples, dtype=np.float64).reshape(-1)
        n = max(1, int(SAMPLE_RATE * float(seconds)))
        if len(x) <= n:
            return np.asarray(samples, dtype=np.float32).reshape(-1)
        c = np.concatenate([[0.0], np.cumsum(x * x)])
        energy = c[n:] - c[:-n]                      # 每个长度 n 的窗能量
        start = int(np.argmax(energy))
        return np.asarray(samples, dtype=np.float32).reshape(-1)[start:start + n]
    except Exception:
        return np.asarray(samples, dtype=np.float32).reshape(-1)


def _zero_runs(x: np.ndarray) -> list:
    """返回所有"全零连续段"的 [start, end) 列表（正确的连续段分解）。"""
    zero = x == 0.0
    if not bool(zero.any()):
        return []
    d = np.diff(zero.astype(np.int8))
    edges = np.flatnonzero(d) + 1
    starts = np.concatenate(([0], edges))
    ends = np.concatenate((edges, [len(x)]))
    return [(int(s), int(e)) for s, e in zip(starts, ends) if bool(zero[s])]


def longest_nonzero_segment(samples: np.ndarray, min_seconds: float = 0.25) -> np.ndarray | None:
    """取窗内最长的一段**连续真实音频**（跳过凭空补的零）。

    为什么需要：设备空闲时 soundcard 会伪造静音把窗口填满（实测 1.5s 窗里补了 1016ms），
    音效被稀释/切断后匹配会时好时坏。音效通常完整落在一段连续真实音频里，
    单独拿这段去匹配能绕开伪造空洞。返回 None 表示本来就没洞、或没有足够长的一段。
    """
    try:
        x = np.asarray(samples, dtype=np.float32).reshape(-1)
        if len(x) == 0:
            return None
        runs = _zero_runs(x)
        if not runs:
            return None                        # 没有洞：不必切
        best_len, best_s, best_e = 0, 0, 0
        cursor = 0
        for s, e in runs + [(len(x), len(x))]:
            if s > cursor and (s - cursor) > best_len:
                best_len, best_s, best_e = s - cursor, cursor, s
            cursor = max(cursor, e)
        if best_len < int(SAMPLE_RATE * float(min_seconds)):
            return None
        return np.asarray(x[best_s:best_e], dtype=np.float32)
    except Exception:
        return None


def zero_fraction(samples: np.ndarray) -> float:
    """窗口里全零样本占比（0~1）。用于判断这个窗口有多少是"补出来的静音"。"""
    try:
        x = np.asarray(samples)
        if x.size == 0:
            return 0.0
        return float((x == 0.0).mean())
    except Exception:
        return 0.0


def zero_gap_stats(samples: np.ndarray, min_gap_ms: float = 6.0) -> tuple:
    """检测窗内"凭空出现的全零段"（伪造静音）。

    为什么需要：soundcard 的 `record(numframes=…)` 在设备暂时没有可用帧时会
    **自己合成一段全零**填进去（deps/soundcard/mediafoundation.py:736-756），
    而这段伪造静音**不会**设置 AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY —— 于是
    `discontinuities()` 显示 0，但音频里已经有空洞：听感是"掉帧感"（click），
    波形被切断 → 相关系数崩掉 → 识别失败。这是实测中最阴险的一种采集故障。

    只统计**内部**零段（两侧都有真实音频），贴窗口两端的静音属正常。
    返回 (缝隙数, 总毫秒, 最长毫秒)。
    """
    try:
        x = np.asarray(samples, dtype=np.float32).reshape(-1)
        if len(x) == 0:
            return 0, 0.0, 0.0
        if bool((x == 0.0).all()):
            return 0, 0.0, 0.0                 # 纯静音：没有内容，不算"缝隙"
        min_frames = max(1, int(SAMPLE_RATE * float(min_gap_ms) / 1000.0))
        count, total, longest = 0, 0.0, 0.0
        for s, e in _zero_runs(x):
            if s <= 0 or e >= len(x):
                continue                       # 贴边的静音不算"洞"
            if e - s >= min_frames:
                count += 1
                ms = (e - s) * 1000.0 / SAMPLE_RATE
                total += ms
                longest = max(longest, ms)
        return count, float(total), float(longest)
    except Exception:
        return 0, 0.0, 0.0


def spectral_rolloff(samples: np.ndarray, fraction: float = 0.995) -> float:
    """频谱滚降：累计能量达到 `fraction` 处的频率（Hz）。

    用途：判断**音频通路带宽**。蓝牙耳机跑在免手持(通话/HFP, 16kHz)通路、
    或系统开了"低音增强/响度均衡"等增强时，音效 8kHz 以上的细节会被整段砍掉
    （实测游戏内录音 8-16kHz 能量占比 0.0%，而同设备桌面录音是 51.7%），
    识别率会因此大幅下降。返回 0.0 表示无法判断。
    """
    try:
        x = np.asarray(samples, dtype=np.float64).reshape(-1)
        n = min(len(x), 1 << 15)
        if n < 2048:
            return 0.0
        spec = np.abs(np.fft.rfft(x[:n] * np.hanning(n))) ** 2
        total = float(spec.sum())
        if total <= 0:
            return 0.0
        cum = np.cumsum(spec) / total
        idx = int(np.searchsorted(cum, float(fraction)))
        return float(np.fft.rfftfreq(n, 1.0 / SAMPLE_RATE)[min(idx, len(spec) - 1)])
    except Exception:
        return 0.0


def listen_windows(stop_event: threading.Event, device_name: str | None = None,
                   blocksize_seconds: float | None = None,
                   gate: "threading.Event | None" = None):
    """Yield overlapping 1.5 s loopback windows about every 180 ms.

    The live recognizer can now match a short item sound even when the game
    ambience continues before and after it. Background RMS is estimated per
    window and supplied to the matcher for its peak/background rejection.

    `blocksize_seconds` 可覆盖 WASAPI 内部缓冲长度：缓冲越大，越能容忍采集线程
    被游戏抢走 CPU（代价是回调更少、延迟略增）。
    """
    com_initialized = False
    mmcss = _enter_mmcss()          # 先注册 MMCSS 再提优先级：防丢帧的关键
    prio_ok = _raise_capture_thread_priority()
    try:
        print(f"[audio] 采集线程保护：MMCSS={mmcss[2] if mmcss else '未注册'} "
              f"线程优先级={'TIME_CRITICAL' if prio_ok else '默认'} "
              f"缓冲={blocksize_seconds or BLOCKSIZE_SECONDS}s", flush=True)
    except Exception:
        pass
    # ⚠️ 导入顺序很重要，不要随意调换：
    #   * comtypes 在**导入时**就执行 CoInitializeEx(STA)（comtypes/__init__.py:176，未捕获异常）
    #   * soundcard.mediafoundation 在**导入时**执行 CoInitializeEx(MTA)（mediafoundation.py:49，
    #     它自己会吞掉 RPC_E_CHANGED_MODE）
    # 若先导入 soundcard，线程先变 MTA，随后 comtypes 的模块级 STA 初始化会抛
    # OSError 0x80010106（RPC_E_CHANGED_MODE）→ 采集直接拿不到设备。先 comtypes 后 soundcard 才稳。
    try:
        import comtypes

        try:
            comtypes.CoInitialize()
        except OSError as exc:
            # 本线程已被其它套间模型初始化（例如宿主先导入了 soundcard）；继续用即可
            if getattr(exc, "winerror", None) not in (-2147417850, 0x80010106):
                raise
        com_initialized = True
    except ImportError:
        import ctypes

        ctypes.windll.ole32.CoInitialize(None)
        com_initialized = True

    try:
        import soundcard as sc

        # COM 已就绪后才装警告钩子（它会 import soundcard.mediafoundation）
        _install_warning_hook()

        if device_name:
            speaker = next((s for s in sc.all_speakers() if s.name == device_name), None)
        else:
            speaker = sc.default_speaker()
        if speaker is None:
            raise RuntimeError("没有找到所选扬声器/耳机设备")
        mic = sc.get_microphone(str(speaker.name), include_loopback=True)

        window_frames = int(SAMPLE_RATE * WINDOW_SECONDS)
        ring = _WindowRing(window_frames)
        blocks_per_update = max(1, round(UPDATE_SECONDS * SAMPLE_RATE / BLOCK))
        blocksize = max(BLOCK, int(SAMPLE_RATE * (
            BLOCKSIZE_SECONDS if not blocksize_seconds else float(blocksize_seconds))))
        block_count = 0

        with mic.recorder(samplerate=SAMPLE_RATE, blocksize=blocksize) as rec:
            paused = False
            while not stop_event.is_set():
                # 「未开始监测」时**完全不从设备取数据**（不录音），并清掉开始前的残留：
                # 这样就不会在你不玩的时候录到别的应用，开始后也只使用新音频。
                if gate is not None and not gate.is_set():
                    if not paused:
                        ring.reset()
                        block_count = 0
                        paused = True
                    time.sleep(0.05)
                    continue
                if paused:
                    ring.reset()          # 恢复瞬间再清一次，确保窗口全是新音频
                    block_count = 0
                    paused = False
                block = np.asarray(rec.record(numframes=BLOCK), dtype=np.float32)
                if block.ndim == 2:
                    block = block.mean(axis=1)
                ring.write(block)
                block_count += 1
                if ring.filled < window_frames or block_count < blocks_per_update:
                    continue

                block_count = 0
                window = ring.window()

                frame = max(1, SAMPLE_RATE // 50)
                usable = window[: len(window) // frame * frame].reshape(-1, frame)
                # 样本里 peak_rms 是死变量（算完未使用）：保留以维持 1:1，仅作诊断。
                peak_rms = float(
                    np.sqrt(np.mean(usable.astype(np.float64) ** 2, axis=1)).max(initial=0.0)
                )

                yield window, _background_level(window)
    finally:
        _leave_mmcss(mmcss)
        if com_initialized:
            try:
                if "comtypes" in locals():
                    comtypes.CoUninitialize()
                else:
                    import ctypes

                    ctypes.windll.ole32.CoUninitialize()
            except Exception:
                pass


listen_segments = listen_windows