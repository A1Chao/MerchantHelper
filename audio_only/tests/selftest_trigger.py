# -*- coding: utf-8 -*-
"""自测：音频触发（音效起始判定）—— 每次拖动都要触发一次。

不发声卡：合成"环境噪声 + 几段短音效"的 1.5s 滑窗时间线（每 0.2s 吐一窗，与真实
``recorder.listen_windows`` 同节奏），逐窗喂给 ``AudioFeed._maybe_audio_trigger``。

守的缺陷（用户实测）：旧实现用**整窗最响帧**判断"现在响不响"，而 1.5s 窗口里必然
残留上一次音效，于是第二次拖动起永远判成"还响着"→ 再也不触发，一直卡在「等待音效」。
因此这里必须验证：
  1) 连续四次拖动（间隔 1.0s，**小于窗口长度**）→ 四次都要触发；
  2) 同一段音效持续几秒也只触发一次（只认上升沿）；
  3) 安静时间线一次都不触发。

    D:\\PY\\python.exe tests\\selftest_trigger.py
"""
from __future__ import annotations

import os
import queue
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import main as main_mod  # noqa: E402
import recorder  # noqa: E402

SR = 44100
WIN = int(1.5 * SR)
STEP = 0.2                                  # 真实吐窗节奏 ≈0.18~0.2s
FRAME_SECONDS = 0.02

FAILED = []


def check(condition, message):
    if condition:
        print("  [OK] %s" % message)
    else:
        FAILED.append(message)
        print("  [FAIL] %s" % message)


def timeline(bg_level, bursts, dur=9.0, seed=0):
    """合成回环音频：``bg_level`` 的环境声 + ``bursts`` 里的短音效。"""
    rng = np.random.default_rng(seed)
    t = np.arange(int(SR * dur)) / SR
    w = (bg_level * np.sin(2 * np.pi * 180 * t)
         + bg_level * 0.5 * rng.normal(0.0, 1.0, len(t))).astype(np.float32)
    for at, amp, length in bursts:
        i0, n = int(at * SR), int(length * SR)
        tt = t[i0:i0 + n] - at
        w[i0:i0 + n] += (amp * np.sin(2 * np.pi * 900 * tt) * np.hanning(n)).astype(np.float32)
    return w


def new_feed(min_interval=None):
    feed = main_mod.AudioFeed(None, trigger_queue=queue.Queue(maxsize=64),
                              min_interval=(main_mod.parse_args([]).min_interval
                                            if min_interval is None else min_interval))
    return feed


def slide(feed, w):
    """按真实节奏滑窗喂触发判定，返回触发时刻（窗口末尾的秒数）。"""
    fired = []
    end = 1.5
    while end <= len(w) / SR - STEP / 2:
        window = w[int(end * SR) - WIN: int(end * SR)]
        feed._maybe_audio_trigger(window, recorder._background_level(window), now=1000.0 + end)
        while not feed._trigger_queue.empty():
            fired.append(round(float(end), 2))
            feed._trigger_queue.get_nowait()
        end += STEP
    return fired


def close_to(fired, burst_at, tol=0.45):
    return any(abs(f - burst_at) <= tol for f in fired)


def main() -> int:
    print("[1] 默认间隔 min_interval 足够小，允许「每次拖动」都触发")
    args = main_mod.parse_args([])
    check(args.min_interval <= 0.5,
          "默认 --min-interval 应 ≤0.5s（实际 %.2f）" % args.min_interval)

    print("[2] 四次拖动、间隔 1.0s（小于 1.5s 窗口）→ 每次都触发")
    drags = [2.0, 3.0, 4.0, 5.0]
    w = timeline(0.002, [(at, 0.05, 0.35) for at in drags])
    feed = new_feed()
    fired = slide(feed, w)
    print("     触发时刻 = %s" % fired)
    for at in drags:
        check(close_to(fired, at), "第 %.1fs 的拖动应触发（实际 %s）" % (at, fired))
    check(len(fired) == len(drags),
          "触发次数应恰好 %d 次（多触发=%r）" % (len(drags), fired))

    print("[3] 一段持续 3s 的长音效 → 只触发一次（只认上升沿）")
    w = timeline(0.002, [(2.0, 0.05, 3.0)])
    fired = slide(new_feed(), w)
    print("     触发时刻 = %s" % fired)
    check(len(fired) == 1, "长音效应只触发 1 次（实际 %s）" % fired)
    check(close_to(fired, 2.1, tol=0.6), "应在音效开始后立刻触发（实际 %s）" % fired)

    print("[4] 安静时间线 → 一次都不触发")
    fired = slide(new_feed(), timeline(0.002, []))
    check(fired == [], "安静环境不该触发（实际 %s）" % fired)

    print("[5] 背景更响（0.02）、间隔 1.0s → 仍然每次都触发")
    drags = [2.0, 3.0, 4.0]
    w = timeline(0.02, [(at, 0.14, 0.4) for at in drags], seed=1)
    fired = slide(new_feed(), w)
    print("     触发时刻 = %s" % fired)
    for at in drags:
        check(close_to(fired, at), "背景响时第 %.1fs 的拖动应触发（实际 %s）" % (at, fired))

    print("[6] 窗口里留着上一声时，识别前要把它裁掉（否则更响的上一声会抢榜首）")
    mixed = timeline(0.002, [(0.50, 0.12, 0.35), (1.15, 0.35, 0.35)], dur=1.5, seed=3)
    trimmed, cut = main_mod._trim_previous_sound(mixed)
    check(trimmed is not None, "两声窗口应被裁剪（现在裁剪没触发）")
    if trimmed is not None:
        check(0.75 <= cut / SR <= 1.0,
              "切割点应落在上一声结束处（0.85s 附近），实际 %.2fs" % (cut / SR))
        check(len(trimmed) / SR <= 0.85,
              "裁完只留 [安静段 + 这一声]（≤0.85s），实际 %.2fs" % (len(trimmed) / SR))
        check(float(np.abs(trimmed).max()) >= 0.2,
              "裁完窗口里必须还留着这一声（max=%.2f）" % float(np.abs(trimmed).max()))
    clean = timeline(0.002, [(1.15, 0.35, 0.35)], dur=1.5, seed=4)
    check(main_mod._trim_previous_sound(clean)[0] is None,
          "只有一声的干净窗口不应裁剪（行为必须完全不变）")

    print()
    if FAILED:
        print("== FAIL ==  %d 项失败" % len(FAILED))
        for m in FAILED:
            print("   - %s" % m)
        return 1
    print("== PASS ==  音效触发（每次拖动都触发）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
