# -*- coding: utf-8 -*-
r"""recorder.py 自测：设备枚举 / 默认设备 / 背景电平 / 回环窗口生成器 / 异常路径。

运行：
    cmd /d /c "D:\PY\python.exe D:\pycharm\UA\行商助手\tests\selftest_recorder.py > D:\pycharm\UA\行商助手\tests\out_selftest_recorder.txt 2>&1"
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
DEPS = os.path.join(ROOT, "deps")
if os.path.isdir(DEPS) and DEPS not in sys.path:
    sys.path.insert(0, DEPS)

try:  # 让中文设备名在任何代码页下都能写进重定向文件
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import recorder  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"    [{'PASS' if ok else 'FAIL'}] {name}{(' -- ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)
    return ok


def main() -> int:
    print("=" * 74)
    print("recorder selftest  |  python", sys.version.split()[0])
    print("=" * 74)

    print("\n[0] 模块常量与别名")
    print(f"    SAMPLE_RATE={recorder.SAMPLE_RATE} BLOCK={recorder.BLOCK} "
          f"WINDOW_SECONDS={recorder.WINDOW_SECONDS} UPDATE_SECONDS={recorder.UPDATE_SECONDS} "
          f"MIN_PEAK_RMS={recorder.MIN_PEAK_RMS}")
    check("常量 1:1", (recorder.SAMPLE_RATE, recorder.BLOCK, recorder.WINDOW_SECONDS,
                      recorder.UPDATE_SECONDS, recorder.MIN_PEAK_RMS)
          == (44100, 1024, 1.5, 0.18, 0.0004))
    check("listen_segments is listen_windows", recorder.listen_segments is recorder.listen_windows)
    print(f"    window_frames = int(SAMPLE_RATE*WINDOW_SECONDS) = "
          f"{int(recorder.SAMPLE_RATE * recorder.WINDOW_SECONDS)}")
    print(f"    blocks_per_update = max(1, round(UPDATE_SECONDS*SAMPLE_RATE/BLOCK)) = "
          f"{max(1, round(recorder.UPDATE_SECONDS * recorder.SAMPLE_RATE / recorder.BLOCK))}")

    print("\n[1] list_devices()")
    t0 = time.perf_counter()
    devices = recorder.list_devices()
    dt = time.perf_counter() - t0
    print(f"    用时 {dt:.3f}s，共 {len(devices)} 个扬声器/耳机设备：")
    for i, d in enumerate(devices):
        print(f"      [{i}] {d!r}")
    target = "扬声器 (2- Realtek(R) Audio)"
    check("设备列表非空", len(devices) > 0, f"len={len(devices)}")
    print(f"    含 {target!r}: {target in devices}")
    check("包含默认 Realtek 扬声器", target in devices)

    print("\n[2] default_device_name()")
    t0 = time.perf_counter()
    dflt = recorder.default_device_name()
    print(f"    用时 {time.perf_counter() - t0:.3f}s -> {dflt!r}")
    check("返回非空 str", isinstance(dflt, str) and dflt != "")

    print("\n[3] _background_level()")
    zeros = np.zeros(recorder.SAMPLE_RATE, dtype=np.float32)
    lvl0 = recorder._background_level(zeros)
    print(f"    全零 1s 数组            -> {lvl0!r}")
    check("全零 -> 0.0", lvl0 == 0.0, f"got {lvl0!r}")

    t = np.arange(recorder.SAMPLE_RATE, dtype=np.float64) / recorder.SAMPLE_RATE
    sine = (0.3 * np.sin(2 * np.pi * 500.0 * t)).astype(np.float32)
    lvl1 = recorder._background_level(sine)
    print(f"    500Hz 正弦 幅度0.3 1s   -> {lvl1!r}  (RMS≈{float(np.sqrt(np.mean(sine.astype(np.float64) ** 2))):.6f})")
    check("正弦 -> >0", lvl1 > 0.0, f"got {lvl1!r}")

    print("\n[4] listen_windows() 取 3 个窗口")
    stop_event = threading.Event()
    gen = recorder.listen_windows(stop_event)
    want_len = int(recorder.SAMPLE_RATE * recorder.WINDOW_SECONDS)
    got = 0
    device_start = time.perf_counter()
    try:
        for window, background_rms in gen:
            got += 1
            arr = np.asarray(window)
            rms = float(np.sqrt(np.mean(arr.astype(np.float64) ** 2)))
            print(f"    window#{got}: len={arr.shape[0]} dtype={arr.dtype} "
                  f"RMS={rms:.8f} background_rms={background_rms!r} "
                  f"(t={time.perf_counter() - device_start:.3f}s)")
            check(f"window#{got} 长度=={want_len}", arr.shape[0] == want_len, f"got {arr.shape[0]}")
            check(f"window#{got} dtype==float32", arr.dtype == np.float32, f"got {arr.dtype}")
            check(f"window#{got} background_rms>=0",
                  isinstance(background_rms, float) and background_rms >= 0.0,
                  f"got {background_rms!r}")
            if got >= 3:
                stop_event.set()
                break
    finally:
        stop_event.set()
        gen.close()
    device_elapsed = time.perf_counter() - device_start
    print(f"    取到 {got} 个窗口，占用设备总时长 {device_elapsed:.3f}s")
    check(">=3 个窗口", got >= 3, f"got {got}")
    check("占用设备 <=3s", device_elapsed <= 3.0, f"{device_elapsed:.3f}s")

    print("\n[5] 异常路径：device_name='不存在的设备名'")
    gen2 = recorder.listen_windows(threading.Event(), device_name="不存在的设备名")
    try:
        next(gen2)
        check("抛 RuntimeError", False, "没有抛异常")
    except RuntimeError as exc:
        print(f"    RuntimeError: {exc!r}")
        check("抛 RuntimeError", True)
        check("异常消息一致", str(exc) == "没有找到所选扬声器/耳机设备", f"got {str(exc)!r}")
    except Exception as exc:  # pragma: no cover
        check("抛 RuntimeError", False, f"抛的是 {type(exc).__name__}: {exc}")
    finally:
        gen2.close()

    print("\n" + "=" * 74)
    if FAILURES:
        print(f"结论: FAIL -- {len(FAILURES)} 项未通过: {FAILURES}")
        return 1
    print("结论: PASS -- 全部检查通过")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)