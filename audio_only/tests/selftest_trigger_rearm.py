# -*- coding: utf-8 -*-
"""自测：音效触发必须能**连续**触发（1.5s 窗口里残留上一声时也要触发）。

回归的缺陷（2026-10-21 用户实测「必须点一下清空才能识别下一个」）：
5–8kHz 频带判据原来按**整窗**取峰值，上一声在 1.5s 窗口里残留期间每一窗都命中 →
tail 被强行抬高 → `_armed` 永远不重新武装 → 两件物品间隔 <1.5s 时第二件不触发。
修法：频带判据只看**窗口末尾**（与 tail 同段）。

用**真实模板音效**（打铁 / 金狮）合成窗口，不用纯低频音——低频音根本触发不了
频带判据，旧测试因此没抓到这个问题。

    D:\\PY\\python.exe tests\\selftest_trigger_rearm.py
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, os.path.join(ROOT, "deps")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402
from scipy import signal  # noqa: E402

import main as app  # noqa: E402
import recognition  # noqa: E402
import recorder  # noqa: E402

SR = recorder.SAMPLE_RATE
WIN = int(1.5 * SR)
QUIET = 0.0005
FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  [OK] " if cond else "  [FAIL] ") + msg, flush=True)
    if not cond:
        FAILS.append(msg)


def load_sound(name: str) -> np.ndarray:
    samples, sr = recognition.read_audio(Path(ROOT).parent / "assets" / "sounds" / name)
    if sr != SR:
        g = math.gcd(int(sr), SR)
        samples = signal.resample_poly(samples, SR // g, int(sr) // g).astype(np.float32)
    return samples


def window_at(t: float, sounds) -> np.ndarray:
    """窗口覆盖 [t-1.5, t]；把 (绝对秒, 波形) 叠加到安静底噪上。"""
    rng = np.random.default_rng(7)
    w = rng.normal(0.0, QUIET, WIN).astype(np.float32)
    for start, snd in sounds:
        rel = start - (t - 1.5)
        if rel >= 1.5 or rel + len(snd) / SR <= 0:
            continue
        i = int(rel * SR)
        src0 = max(0, -i)
        dst0 = max(0, i)
        n = min(len(snd) - src0, WIN - dst0)
        if n > 0:
            w[dst0:dst0 + n] += snd[src0:src0 + n]
    return w


def run_scene(sounds, seconds: float = 4.0):
    feed = app.AudioFeed(trigger_queue=None, min_interval=0.4)
    q = app.queue.Queue(maxsize=64)
    feed._trigger_queue = q
    fired = []
    t = 0.0
    while t < seconds:
        feed._maybe_audio_trigger(window_at(t, sounds), QUIET, now=1000.0 + t)
        try:
            q.get_nowait()
            fired.append(round(t, 2))
        except app.queue.Empty:
            pass
        t = round(t + 0.18, 3)
    return feed, fired


def main() -> int:
    print("== selftest_trigger_rearm ==", flush=True)

    a = load_sound("打铁声音.wav")
    b = load_sound("金狮声音.wav")

    print("[1] 两件物品间隔 < 1.5s：第二件必须触发（旧实现吞掉）", flush=True)
    for gap in (0.54, 0.72, 0.90, 1.20, 1.80):
        feed, fired = run_scene([(0.0, a), (gap, b)], seconds=gap + 2.4)
        hit = [t for t in fired if gap - 0.05 <= t <= gap + 0.40]
        check(bool(hit), f"间隔 {gap:.2f}s → 第二声触发（窗口 {fired}）")
        check(feed.audio_triggers == len(fired), "触发计数与队列一致")

    print("[2] 连续 4 件（每 0.9s 一件）必须每次触发", flush=True)
    sounds = [(i * 0.9, a if i % 2 == 0 else b) for i in range(4)]
    _, fired = run_scene(sounds, seconds=4.8)
    check(len(fired) >= 4, f"4 次拖动 → {len(fired)} 次触发（{fired}）")

    print("[3] 持续 3s 的连续噪声只触发一次（不能一直响个不停）", flush=True)
    rng = np.random.default_rng(3)
    sustained = rng.normal(0.0, 0.02, int(3.0 * SR)).astype(np.float32)
    _, fired = run_scene([(0.0, sustained)], seconds=4.5)
    check(len(fired) == 1, f"3s 连续声 → {len(fired)} 次触发（{fired}）")

    print("[4] 安静房间（0.0005 / 0.004 底噪）不得误触发", flush=True)
    rng = np.random.default_rng(11)
    for quiet in (0.0005, 0.004):
        feed = app.AudioFeed(trigger_queue=None, min_interval=0.4)
        q = app.queue.Queue(maxsize=64)
        feed._trigger_queue = q
        n = 0
        for i in range(200):
            feed._maybe_audio_trigger(
                rng.normal(0, quiet, WIN).astype(np.float32), quiet, now=1000.0 + i * 0.18)
            try:
                q.get_nowait()
                n += 1
            except app.queue.Empty:
                pass
        check(n == 0, f"底噪 {quiet} → 误触发 {n}/200")

    print("[5] 频带判据只使用末尾窗口（证据行带 频带=..）", flush=True)
    feed, _ = run_scene([(0.0, a), (0.9, b)], seconds=2.7)
    check(bool(getattr(feed, "_last_band", "")), "触发日志里有频带证据：%r"
          % getattr(feed, "_last_band", ""))

    print("== %s ==  %d 失败" % ("PASS" if not FAILS else "FAIL", len(FAILS)), flush=True)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
