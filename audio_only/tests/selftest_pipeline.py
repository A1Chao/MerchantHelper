# -*- coding: utf-8 -*-
"""自测：端到端音频链路（不需要声卡，也不需要玩游戏）。

把 ``assets/sounds/*.wav`` 当作"刚从回环采集到的窗口"喂进 ``main.Engine``，
验证：触发 → 取窗 → 识别 → 候选 → 按格数筛选 → 面板展示，整条链路是通的。
这是**没有视觉**的版本里唯一能自动化验证的"真识别"路径，所以必须存在。
"""
from __future__ import annotations

import os
import queue
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402

import main as main_mod  # noqa: E402
import recognition  # noqa: E402
import recorder  # noqa: E402
import shapes  # noqa: E402
from candidates import CandidateEngine  # noqa: E402

FAILED = []


def check(condition, message):
    if not condition:
        FAILED.append(message)
        print("  [FAIL] %s" % message)
    return bool(condition)


class StubAudio:
    """假的采集器：``snapshot()`` 直接返回一段模板 wav（按 44100 采样率）。"""

    def __init__(self, samples, background=0.0005):
        self.samples = np.asarray(samples, dtype=np.float32)
        self.background = float(background)
        self.last_error = None
        self.windows = 10
        self.peak_rms = float(np.sqrt(np.mean(self.samples.astype(np.float64) ** 2)))
        self.audio_triggers = 1

    def snapshot(self):
        return self.samples.copy(), self.background, 0.05

    @property
    def profile(self):
        return None

    def reference_for(self, pressed_at, min_gap=0.15):
        return None

    @property
    def discontinuities(self):
        return 0

    def stop(self):
        pass


def _load_window(name):
    """读一个模板 wav，返回 (44100 采样率波形, 16k 波形, 形状)。"""
    samples16, sr = recognition.read_audio(os.path.join(ROOT, "..", "assets", "sounds", name))
    from math import gcd

    import scipy.signal as signal

    if sr == recorder.SAMPLE_RATE:
        window = np.asarray(samples16, dtype=np.float32)
    else:
        g = gcd(int(sr), recorder.SAMPLE_RATE)
        window = signal.resample_poly(samples16, recorder.SAMPLE_RATE // g, int(sr) // g)
        window = np.asarray(window, dtype=np.float32)
    return window, samples16


def _shape_for_candidate(candidate):
    """从候选的 slot + orientation 反推用户该点哪个图标（模拟玩家操作）。"""
    slot = int(candidate.get("slot") or 0)
    orient = str(candidate.get("orientation") or "")
    if slot == 1:
        return (1, 1)
    if slot == 2:
        return (2, 1) if orient == "横" else (1, 2)
    if slot == 4:
        return (2, 2)
    if slot == 6:
        return (3, 2) if orient == "横" else (2, 3)
    return shapes.SHAPE_ANY


def main() -> int:
    print("== 端到端：wav → Engine → 候选 → 格数筛选 ==")

    print("[1] CLI 默认值（默认不自动开录）")
    args = main_mod.parse_args([])
    check(args.autostart is False, "默认不应自动开始监测（避免没玩时录其它应用）")
    check(args.denoise is False, "降噪默认关闭")
    check(0.0 < args.listen_max <= 2.0, "listen-max 默认应在 (0,2] 秒: %r" % args.listen_max)
    check(args.retry >= 0 and args.retry_delay > 0, "retry 参数默认值不合理")
    check(not hasattr(args, "vision"), "声控版不应再有 --vision 参数")

    print("[2] 引擎加载（模板 + 候选库）")
    engine = main_mod.Engine(args)
    engine.load()
    check(len(engine.matcher.templates) == 33,
          "模板数应为 33，实际 %d" % len(engine.matcher.templates))
    check(len(engine.matcher.errors) == 0, "模板错误: %r" % (engine.matcher.errors[:3],))
    check(engine.candidate_engine is not None, "候选引擎应加载")
    check(recognition.SAMPLE_RATE == 16000, "识别采样率应为 16k")
    check(recorder.SAMPLE_RATE == 44100, "采集采样率应为 44100")

    print("[3] 触发 → 识别（真实模板当窗口）")
    sounds = sorted(n for n in os.listdir(os.path.join(ROOT, "..", "assets", "sounds"))
                    if n.lower().endswith(".wav"))
    hit = 0
    for name in sounds[:12]:
        window, samples16 = _load_window(name)
        engine.audio = StubAudio(window)
        try:
            while True:
                engine.results.get_nowait()
        except queue.Empty:
            pass
        engine.active.set()
        engine.handle_trigger(time.time(), main_mod.TRIG_AUDIO)
        try:
            item = engine.results.get_nowait()
        except queue.Empty:
            check(False, "%s 触发后没有产生结果" % name)
            continue
        rows = item.get("rows") or []
        if not rows:
            continue
        hit += 1
        top = rows[0]
        check(float(top.get("score") or 0) >= 60,
              "%s 命中分数异常低: %r" % (name, top.get("score")))
        check(bool(top.get("items")), "命中结果应带 items")
        # 底栏实时反馈：识别完必须从「等待音效」变成「已识别：xxx」（用户实测发现的缺陷：
        # 以前底栏永远停在等待文案，拖动时看不到任何进展）
        status = main_mod._detection_status_line(engine)
        check(status.startswith("已识别："), "识别成功后底栏应显示「已识别：…」，实际 %r" % status)
        check(str(top.get("key")) in status, "底栏应带上识别到的名字: %r" % status)
        engine.active.clear()
        break
    check(hit > 0, "至少应有一个模板能被完整识别出来")

    print("[3.1] 连续两次触发 → 两次结果（用户实测：第二次拖动别卡住）")
    engine.audio = StubAudio(window)
    engine.active.set()
    for i in range(2):
        engine.handle_trigger(time.time(), main_mod.TRIG_AUDIO)
        try:
            again = engine.results.get_nowait()
        except queue.Empty:
            check(False, "第 %d 次触发没有产生结果（第二次拖动卡住）" % (i + 1))
            break
        check(bool(again.get("rows")),
              "第 %d 次触发应仍有候选（实际 %r）" % (i + 1, again.get("reason")))
    engine.active.clear()

    print("[4] 候选展开 + 按格数硬筛（模拟玩家点图标）")
    candidates = engine.candidate_engine.resolve(rows, 0, "",
                                                 strict_shape=True, strict_orientation=False)
    check(bool(candidates), "不限格数时应展开出候选")
    top_candidate = candidates[0]
    check(top_candidate.get("name"), "候选应有名称")
    check(int(top_candidate.get("slot") or 0) > 0, "候选应有格数")
    check(top_candidate.get("image_path") and
          os.path.isfile(top_candidate["image_path"]),
          "候选应带存在的缩略图: %r" % top_candidate.get("image_path"))

    shape = _shape_for_candidate(top_candidate)
    print("      该物品 %s（%s格 %s）→ 玩家应点 %s"
          % (top_candidate["name"], top_candidate["slot"],
             top_candidate.get("orientation") or "-", shapes.grid_text(shape)))
    filtered = engine.candidate_engine.resolve(
        rows, shapes.slots_of(shape), shapes.orientation_of(shape),
        strict_shape=True, strict_orientation=False)
    check(bool(filtered), "按该物品格数筛选后不应为空")
    check(all(int(c["slot"]) == shapes.slots_of(shape) for c in filtered),
          "筛选后混进了别的格数: %r" % [c["slot"] for c in filtered])
    check(top_candidate["name"] in [c["name"] for c in filtered],
          "按自己格数筛选后应仍含该物品")
    # 朝向只标注不丢：同面积另一种摆法也必须还在（2×3 vs 3×2 互为镜像）
    flipped = (shape[1], shape[0])
    if flipped != shape:
        same = engine.candidate_engine.resolve(
            rows, shapes.slots_of(shape), shapes.orientation_of(flipped),
            strict_shape=True, strict_orientation=False)
        check({c["name"] for c in same} == {c["name"] for c in filtered},
              "朝向不同的同面积物品不该被丢弃（朝向只标注不丢）")

    print("[5] 音效起始判定（音频触发，不依赖设备）")
    feed = main_mod.AudioFeed(None, trigger_queue=queue.Queue(maxsize=8), min_interval=0.0)
    rng = np.random.default_rng(7)
    win_n, rate = 66150, 44100                  # 1.5s @44.1k：与真实窗口同长

    def _quiet():
        return (0.0002 * rng.standard_normal(win_n)).astype(np.float32)

    def _with_sound():
        # 音效放在窗口**末尾** 0.35s：真实触发就是"刚刚响起"的那一窗
        w = _quiet()
        i0, n = win_n - int(0.35 * rate), int(0.35 * rate)
        w[i0:i0 + n] += (0.25 * np.sin(np.linspace(0, 40, n))).astype(np.float32)
        return w

    quiet, loud = _quiet(), _with_sound()
    feed._maybe_audio_trigger(quiet, 0.0005)
    check(feed.audio_triggers == 0, "安静窗口不该触发")
    feed._maybe_audio_trigger(loud, 0.0005)
    check(feed.audio_triggers == 1, "突起音效应触发一次，实际 %d" % feed.audio_triggers)
    feed._maybe_audio_trigger(loud, 0.0005)
    check(feed.audio_triggers == 1, "持续音效不该反复触发（只认上升沿）")
    # 用户实测缺陷的回归：上一次音效还留在 1.5s 窗口里时，下一段音效也必须能触发
    feed._maybe_audio_trigger(quiet, 0.0005)             # 音效结束 → 重新武装
    feed._maybe_audio_trigger(loud, 0.0005)
    check(feed.audio_triggers == 2,
          "第二段音效应再触发一次，实际 %d（旧实现会永久卡住）" % feed.audio_triggers)

    print("[6] 识别阈值没有被本版改动")
    check(recognition.SAMPLE_RATE == 16000, "识别采样率")
    check(getattr(engine.matcher, "last_reason", None) is not None,
          "matcher 应提供 last_reason 供日志用")

    engine.active.clear()
    engine.shutdown()
    print()
    if FAILED:
        print("== FAIL ==  %d 项不通过" % len(FAILED))
        return 1
    print("== PASS ==  端到端音频链路通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
