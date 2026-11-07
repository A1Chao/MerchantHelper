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
from contextlib import contextmanager
from collections import deque

import numpy as np





_DEPS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "deps")
if os.path.isdir(_DEPS_DIR) and _DEPS_DIR not in sys.path:
    sys.path.insert(0, _DEPS_DIR)

SAMPLE_RATE = 44100
BLOCK = 1024
WINDOW_SECONDS = 1.5
UPDATE_SECONDS = 0.18
MIN_PEAK_RMS = 0.0004



BLOCKSIZE_SECONDS = 0.08


RECOVERY_MAX_FAILURES = 3
RECOVERY_RESET_SECONDS = 5.0


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

        k32.GetCurrentThread.restype = ctypes.c_void_p
        k32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
        k32.SetThreadPriority.restype = ctypes.c_int
        h = k32.GetCurrentThread()
        if k32.SetThreadPriority(h, THREAD_PRIORITY_TIME_CRITICAL):
            return True
        err1 = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
        if k32.SetThreadPriority(h, 2):
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

        from ctypes import wintypes
        avrt = ctypes.WinDLL("avrt", use_last_error=True)
        avrt.AvSetMmThreadCharacteristicsW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        avrt.AvSetMmThreadCharacteristicsW.restype = wintypes.HANDLE
        avrt.AvSetMmThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
        avrt.AvSetMmThreadPriority.restype = wintypes.BOOL
        task_index = wintypes.DWORD(0)
        for name in ("Pro Audio", "Audio"):
            handle = avrt.AvSetMmThreadCharacteristicsW(name, ctypes.byref(task_index))
            if handle:

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

            from ctypes import wintypes
            avrt = ctypes.WinDLL("avrt", use_last_error=True)
            avrt.AvRevertMmThreadCharacteristics.argtypes = [wintypes.HANDLE]
            avrt.AvRevertMmThreadCharacteristics.restype = wintypes.BOOL
            avrt.AvRevertMmThreadCharacteristics(state[0])
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
        energy = c[n:] - c[:-n]
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
            return None
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
            return 0, 0.0, 0.0
        min_frames = max(1, int(SAMPLE_RATE * float(min_gap_ms) / 1000.0))
        count, total, longest = 0, 0.0, 0.0
        for s, e in _zero_runs(x):
            if s <= 0 or e >= len(x):
                continue
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


def _resample_to_44k(x: np.ndarray, src_rate: int) -> np.ndarray:
    """把设备原生采样率的块重采样到 SAMPLE_RATE（44.1k），保证后续算法/模板一致。

    只有设备拒绝 44.1kHz（极少数）时才会走到这里；用 polyphase，音质/相位都够。
    """
    try:
        x = np.asarray(x, dtype=np.float32).reshape(-1)
        if int(src_rate) == SAMPLE_RATE or x.size == 0:
            return x
        import math

        from scipy import signal

        g = math.gcd(int(src_rate), SAMPLE_RATE)
        up, down = SAMPLE_RATE // g, int(src_rate) // g
        return signal.resample_poly(x, up, down).astype(np.float32)
    except Exception:
        return np.asarray(x, dtype=np.float32).reshape(-1)


def _all_speakers():
    import soundcard as sc

    return list(sc.all_speakers())


def _open_loopback(device_name: str | None, samplerate: int,
                   blocksize_seconds: float | None = None,
                   allow_fallback: bool = True):
    """打开回环录音器（已跑完 WASAPI Initialize，可直接 `with rec:` 读取）。

    相对旧实现的三处加固（都是为了「某些型号耳机完全听不到」这类问题）：
      1) **按设备 ID 取回环**，不再用名字字符串匹配 —— 两台同名端点（2.4G 接收器
         插两个、蓝牙同时暴露 A2DP/HFP、虚拟声卡同名）时旧写法会抓到另一台；
      2) 默认设备打不开时，自动在**其它活动端点**上重试（换耳机/切默认设备后
         端点 GUID 变化，旧设备可能已经失效）；
      3) 设备拒绝 44.1kHz 时依次退 48k/44.1k/16k、再退 2 声道，并在采集端重采样
         回 44.1k（算法只认 44.1k，不能直接用别人的采样率喂模板）。

    返回 ``(rec, speaker, attempts)``；``attempts`` 是每次尝试的失败原因，供日志复盘。
    全部失败时抛 RuntimeError，消息里带上前几次失败原因。
    """
    import soundcard as sc

    attempts: list[str] = []
    speakers = _all_speakers()

    def _open_device(dev, label: str):
        rates = [int(samplerate)] + [r for r in (48000, 44100, 16000) if r != int(samplerate)]
        for rate in rates:
            for channels in (None, 2):
                frames = max(BLOCK, int(rate * (BLOCKSIZE_SECONDS if not blocksize_seconds
                                                else float(blocksize_seconds))))
                try:
                    return dev.recorder(samplerate=rate, channels=channels, blocksize=frames)
                except Exception as exc:
                    msg = f"{label} @{rate}Hz/{channels or '默认'}ch: {type(exc).__name__}: {exc}"
                    if msg not in attempts:
                        attempts.append(msg)
        return None

    def _try_speaker(spk, why: str):
        try:
            mic = sc.get_microphone(spk.id, include_loopback=True)
        except Exception as exc:
            attempts.append(f"{why}{spk.name}: 取回环失败 {type(exc).__name__}: {exc}")
            return None
        return _open_device(mic, f"{why}{spk.name}")

    if device_name:
        match = next((s for s in speakers if s.name == device_name), None)
        if match is None:
            match = next((s for s in speakers if s.id == device_name), None)
        if match is not None:
            rec = _try_speaker(match, "")
            if rec is not None:
                return rec, match, attempts
        else:
            attempts.append(f"指定的设备 {device_name!r} 不在当前活动播放端点里")


        try:
            src = sc.get_microphone(device_name, include_loopback=False)
            rec = _open_device(src, f"录音设备 {getattr(src, 'name', device_name)}")
            if rec is not None:
                print(f"[audio] 按普通录音设备打开：{getattr(src, 'name', device_name)}"
                      "（注意：这不是回环，录的是该输入设备的信号）", flush=True)
                return rec, src, attempts
        except Exception as exc:
            attempts.append(f"按录音设备打开 {device_name!r} 失败：{type(exc).__name__}: {exc}")

        raise RuntimeError("回环设备打不开：" + "；".join(attempts[-6:]))


    default = None
    try:
        default = sc.default_speaker()
    except Exception as exc:
        attempts.append(f"取系统默认播放设备失败：{type(exc).__name__}: {exc}")
    order = ([default] if default is not None else [])
    order += [s for s in speakers if default is None or s.id != default.id]
    for spk in order:
        why = "默认设备 " if (default is not None and spk.id == default.id) else "备选设备 "
        rec = _try_speaker(spk, why)
        if rec is not None:
            if attempts:
                print(f"[audio] 回环降级：默认/首选设备打不开，改用 {spk.name}", flush=True)
            return rec, spk, attempts
    raise RuntimeError("回环设备全部打不开（系统没有可用输出端点）：" + "；".join(attempts[-8:]))


def doctor(seconds: float = 2.0, tone: bool = False, device_name: str | None = None,
           writer=print) -> list[dict]:
    """逐端点自检：设备格式 / 能否打开 / 实际采到的电平。

    这是给「某些型号耳机听不到声音」定位用的：把每个播放端点都单独开回环采一遍，
    输出设备混音格式、客户端格式、峰值、静音率、5–8kHz 占比。若加 ``tone=True``，
    还会向该端点播一个 1kHz 测试音再采 —— 能区分：
      * 连自己放的测试音都采不到 → 这台设备/驱动不允许 WASAPI 回环（抓不到游戏声是必然）；
      * 测试音采得到、游戏声采不到 → 游戏声音根本没从这台设备输出（选错设备）；
      * 格式/打开报错 → 日志里直接有 HRESULT 中文原因。
    """
    import soundcard as sc

    report: list[dict] = []
    try:
        default = sc.default_speaker()
        default_id = default.id
    except Exception:
        default_id = None
    try:
        mics = [str(m.name) for m in sc.all_microphones(include_loopback=False)]
    except Exception:
        mics = []

    write = writer
    write(f"[doctor] 系统默认播放设备 = "
          f"{getattr(default, 'name', '(取不到)') if default_id else '(取不到)'}")
    write(f"[doctor] 录音（麦克风）端点 = {' | '.join(mics) if mics else '(无)'}")
    write(f"[doctor] 逐个端点采集 {seconds:.1f}s"
          f"{'，并播 1kHz 测试音自检' if tone else ''}…")
    write("")

    speakers = _all_speakers()
    for spk in speakers:
        info = {"name": spk.name, "id": spk.id,
                "default": bool(default_id and spk.id == default_id),
                "ok": False, "peak": 0.0, "rms": 0.0, "zero": 1.0, "band": 0.0,
                "device_mix": "", "client_format": "", "tone_peak": None, "error": ""}
        try:
            info["device_mix"] = _device_format_text(spk)
        except Exception as exc:
            info["device_mix"] = f"(读取失败 {type(exc).__name__})"
        try:
            mic = sc.get_microphone(spk.id, include_loopback=True)
            rec = mic.recorder(samplerate=SAMPLE_RATE,
                               blocksize=max(BLOCK, int(SAMPLE_RATE * BLOCKSIZE_SECONDS)))
            info["client_format"] = str(getattr(rec, "client_format", ""))
            blocks: list[np.ndarray] = []
            tone_thread = None
            if tone:
                tone_thread = _play_tone(spk, seconds=0.45)
            with rec:
                need = int(SAMPLE_RATE * float(seconds))
                got = 0
                while got < need:
                    chunk = np.asarray(rec.record(numframes=BLOCK), dtype=np.float32)
                    if chunk.ndim == 2:
                        chunk = chunk.mean(axis=1)
                    blocks.append(chunk)
                    got += int(chunk.shape[0])
            if tone_thread is not None:
                tone_thread.join(timeout=1.5)
            x = np.concatenate(blocks)[: int(SAMPLE_RATE * float(seconds))] if blocks else np.zeros(1, np.float32)
            info["ok"] = True
            info["peak"] = float(np.abs(x).max()) if x.size else 0.0
            info["rms"] = float(np.sqrt(np.mean(np.asarray(x, np.float64) ** 2))) if x.size else 0.0
            info["zero"] = float((np.abs(x) < 1e-7).mean()) if x.size else 1.0
            info["band"] = _band_ratio(x, 5000.0, 8000.0)
            if tone:
                info["tone_peak"] = _segment_peak(x, 0.0, 0.8)
        except Exception as exc:
            info["error"] = f"{type(exc).__name__}: {exc}"
        report.append(info)

        flag = "★默认" if info["default"] else "    "
        head = (f"[doctor] {flag} {info['name']}")
        write(head)
        write(f"[doctor]        端点格式 = {info['device_mix']}")
        if info["error"]:
            write(f"[doctor]        ✗ 打不开：{info['error']}")
            continue
        write(f"[doctor]        采集格式 = {info['client_format']}")
        write(f"[doctor]        峰值 {info['peak']:.4f}  RMS {info['rms']:.5f}  "
              f"静音率 {info['zero']:.0%}  5–8kHz占比 {info['band']:.1%}"
              + (f"  测试音峰值 {info['tone_peak']:.4f}" if info["tone_peak"] is not None else ""))
        if info["peak"] < 0.0004:
            write("[doctor]        → 这段没有信号（游戏没声音，或游戏不走这个端点）")
        else:
            write("[doctor]        → 有信号，这台设备可以监听")
    write("")
    return report


def _device_format_text(spk) -> str:
    """读端点「设备默认格式」原始字节（诊断用）。"""
    import soundcard.mediafoundation as mf

    ptr = None
    try:
        with mf._DeviceEnumerator() as enum:
            ptr = enum.device_ptr(spk.id)
            ppStore = mf._ffi.new('IPropertyStore **')
            mf._com.check_error(ptr[0][0].lpVtbl.OpenPropertyStore(ptr[0], 0, ppStore))
            pv = mf._PropVariant()
            key = mf._ffi.new("PROPERTYKEY *",
                              [[0xf19f064d, 0x82c, 0x4e27,
                                [0xbc, 0x73, 0x68, 0x82, 0xa1, 0xbb, 0x8e, 0x4c]], 0])
            mf._com.check_error(ppStore[0][0].lpVtbl.GetValue(ppStore[0], key, pv.ptr))
            mf._com.release(ppStore)
            if pv.ptr[0].vt != 65:
                return f"(属性不是 BLOB, vt={pv.ptr[0].vt})"
            blob = mf._ffi.cast("BLOB_PROPVARIANT *", pv.ptr)
            cb = int(blob[0].blob.cbSize)
            wf = mf._ffi.cast("WAVEFORMATEX *", blob[0].blob.pBlobData)
            return (f"tag=0x{int(wf[0].wFormatTag):04x} ch={int(wf[0].nChannels)} "
                    f"rate={int(wf[0].nSamplesPerSec)} bits={int(wf[0].wBitsPerSample)} "
                    f"blob={cb}B")
    finally:
        if ptr is not None:
            try:
                mf._com.release(ptr)
            except Exception:
                pass


def _band_ratio(x: np.ndarray, lo: float, hi: float) -> float:
    try:
        x = np.asarray(x, np.float64).reshape(-1)
        n = min(len(x), 1 << 14)
        if n < 2048:
            return 0.0
        spec = np.abs(np.fft.rfft(x[:n] * np.hanning(n))) ** 2
        freqs = np.fft.rfftfreq(n, 1.0 / SAMPLE_RATE)
        band = float(spec[(freqs >= lo) & (freqs <= hi)].sum())
        total = float(spec.sum()) or 1.0
        return band / total
    except Exception:
        return 0.0


def _segment_peak(x: np.ndarray, start_s: float, end_s: float) -> float:
    try:
        a = int(start_s * SAMPLE_RATE)
        b = int(end_s * SAMPLE_RATE)
        seg = np.asarray(x, np.float32)[a:b]
        return float(np.abs(seg).max()) if seg.size else 0.0
    except Exception:
        return 0.0


def _play_tone(spk, seconds: float = 0.45, freq: float = 1000.0, amplitude: float = 0.12):
    """向指定端点播一个短测试音（独立线程 + 自己初始化 COM）。返回线程对象。"""
    import threading

    def run():
        try:
            import comtypes

            try:
                comtypes.CoInitialize()
            except OSError:
                pass
            rate = SAMPLE_RATE
            n = int(rate * float(seconds))
            tone = (amplitude * np.sin(2 * np.pi * freq * np.arange(n) / rate)).astype(np.float32)
            with spk.player(samplerate=rate) as player:
                player.play(tone)
        except Exception as exc:
            print(f"[doctor] 测试音播放失败（{spk.name}）：{type(exc).__name__}: {exc}", flush=True)

    t = threading.Thread(target=run, name="doctor-tone", daemon=True)
    t.start()
    return t


class _DeviceLost(Exception):
    """运行中采集设备失效（拔出 / 切换到别的端点 / 被独占 / 蓝牙休眠）。

    单独一个异常类型，是为了只把「设备没了」这种情况交给上层重开设备；
    调用方处理音频时抛出的其它异常必须原样上抛，不能被当成设备故障吞掉。
    """


@contextmanager
def _recording_context(rec):
    """仅把设备进入/退出错误转为可恢复错误，不吞消费者或算法异常。"""
    try:
        rec.__enter__()
    except (RuntimeError, OSError) as exc:
        raise _DeviceLost(f"打开采集流失败：{type(exc).__name__}: {exc}") from exc
    try:
        yield rec
    finally:
        original = sys.exc_info()
        try:
            rec.__exit__(*original)
        except (RuntimeError, OSError) as exc:
            if original[0] is None:
                raise _DeviceLost(f"关闭采集流失败：{type(exc).__name__}: {exc}") from exc

            try:
                print(f"[audio] 采集流清理失败，保留原始异常：{exc}", file=sys.stderr, flush=True)
            except Exception:
                pass


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
    mmcss = _enter_mmcss()
    prio_ok = _raise_capture_thread_priority()
    try:
        print(f"[audio] 采集线程保护：MMCSS={mmcss[2] if mmcss else '未注册'} "
              f"线程优先级={'TIME_CRITICAL' if prio_ok else '默认'} "
              f"缓冲={blocksize_seconds or BLOCKSIZE_SECONDS}s", flush=True)
    except Exception:
        pass
    try:

        try:
            import comtypes
            try:
                comtypes.CoInitialize()
                com_initialized = True
            except OSError as exc:
                if getattr(exc, "winerror", None) not in (-2147417850, 0x80010106):
                    raise

        except ImportError:
            import ctypes
            hr = ctypes.windll.ole32.CoInitialize(None)
            if hr in (0, 1):
                com_initialized = True
            elif hr not in (-2147417850, 0x80010106):
                raise OSError(f"CoInitialize failed: {hr:#x}")

        import soundcard as sc


        _install_warning_hook()

        window_frames = int(SAMPLE_RATE * WINDOW_SECONDS)
        ring = _WindowRing(window_frames)
        blocks_per_update = max(1, round(UPDATE_SECONDS * SAMPLE_RATE / BLOCK))

        rec = None
        speaker = None
        failures = 0
        block_count = 0
        silent = {"since": None, "warned": False}

        def _pump():
            """在当前设备上跑一轮采集；设备失效时抛 ``_DeviceLost``，其它异常照常上抛。"""
            nonlocal block_count, failures
            actual = int(getattr(rec, "samplerate", SAMPLE_RATE) or SAMPLE_RATE)
            healthy_frames = 0
            with _recording_context(rec):
                paused = False
                while not stop_event.is_set():


                    if gate is not None and not gate.is_set():
                        if not paused:
                            ring.reset()
                            block_count = 0
                            silent["since"] = None
                            paused = True
                        time.sleep(0.05)
                        continue
                    if paused:
                        ring.reset()
                        block_count = 0
                        paused = False


                    try:
                        block = np.asarray(rec.record(numframes=BLOCK), dtype=np.float32)
                    except (RuntimeError, OSError) as exc:
                        raise _DeviceLost(f"{type(exc).__name__}: {exc}") from exc
                    if block.ndim == 2:
                        block = block.mean(axis=1)
                    if actual != SAMPLE_RATE:

                        block = _resample_to_44k(block, actual)
                    ring.write(block)
                    healthy_frames += len(block)
                    if healthy_frames >= SAMPLE_RATE * RECOVERY_RESET_SECONDS:
                        failures = 0
                    block_count += 1
                    if ring.filled < window_frames or block_count < blocks_per_update:
                        continue

                    block_count = 0
                    window = ring.window()

                    frame = max(1, SAMPLE_RATE // 50)
                    usable = window[: len(window) // frame * frame].reshape(-1, frame)

                    peak_rms = float(
                        np.sqrt(np.mean(usable.astype(np.float64) ** 2, axis=1)).max(initial=0.0)
                    )



                    level = float(np.max(np.abs(window))) if window.size else 0.0
                    if level > 1e-5:
                        silent["since"] = None
                    elif silent["since"] is None:
                        silent["since"] = time.time()
                    elif not silent["warned"] and time.time() - silent["since"] > 10.0:
                        silent["warned"] = True
                        try:
                            print("[device] 已 10 秒没有任何信号（峰值≈0）：当前抓的是 "
                                  "%r，多半选错了。候选设备：" % (speaker.name,), flush=True)
                            for spk in _all_speakers():
                                print("[device]   --device \"%s\"" % (spk.name,), flush=True)
                        except Exception:
                            pass

                    yield window, _background_level(window)

        while not stop_event.is_set():
            try:
                if rec is None:

                    try:
                        rec, speaker, attempts = _open_loopback(
                            device_name, SAMPLE_RATE, blocksize_seconds)
                    except (RuntimeError, OSError) as exc:
                        raise _DeviceLost(f"重开设备失败：{type(exc).__name__}: {exc}") from exc
                    print(f"[audio] 回环设备 = {speaker.name}", flush=True)
                    print(f"[audio] 端点格式 = {getattr(rec, 'device_mix_format', '?')}", flush=True)
                    print(f"[audio] 采集格式 = {getattr(rec, 'client_format', '?')}", flush=True)
                    if attempts:
                        print(f"[audio] 打开回退记录 = {'；'.join(attempts[:4])}", flush=True)
                    actual_sr = int(getattr(rec, "samplerate", SAMPLE_RATE) or SAMPLE_RATE)
                    if actual_sr != SAMPLE_RATE:
                        print(f"[audio] {actual_sr}Hz → 重采样到 {SAMPLE_RATE}Hz", flush=True)
                    ring.reset()
                    block_count = 0
                    silent.update(since=None, warned=False)
                yield from _pump()
                break
            except _DeviceLost as exc:
                rec = None
                if stop_event.is_set():
                    break
                failures += 1
                if failures > RECOVERY_MAX_FAILURES:
                    raise RuntimeError(
                        f"音频设备连续失效，恢复失败：{exc}；请停止后重新开始监测重试") from exc
                delay = min(0.3 * (2 ** (failures - 1)), 1.0)
                print(f"[audio] 采集中断：{exc}；{delay:.1f}s 后重开 "
                      f"（连续失败 {failures}/{RECOVERY_MAX_FAILURES}）", flush=True)
                if stop_event.wait(delay):
                    break
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
