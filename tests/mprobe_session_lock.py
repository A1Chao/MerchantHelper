# -*- coding: utf-8 -*-
r"""task-9 探针（ui-candidates）：**按下会话锁** —— 鼠标掠过别的物品不该改写已选结果。

用户原话：「假如我选择了物品 a，鼠标不小心指向了物品 b，最后物品展示会变。」

离线、只读：截图全部用**世界模型**替换（`capture_cursor_region` 返回固定 origin 的假图）、
音频用离线回环窗 + 假匹配器、键鼠只**读**（`left_button_down`/`cursor_pos` 都被脚本替换，
从不调用 SendInput/鼠标事件），不联网、不改任何识别阈值。

跑法：
    cmd /d /c "set QT_QPA_PLATFORM=offscreen&& D:\PY\python.exe tests\mprobe_session_lock.py > tests\out_trig_lock.txt 2>&1"

世界模型：截图 origin 固定 (600,100)、图 800x800 → `cursor_crop = 屏幕坐标 - origin`。
    A（2 格竖）在 crop (400,400) → 屏幕 (1000,500)
    B（1 格横）在 crop (600,400) → 屏幕 (1200,500)
    空格子（无物品）在 crop (100,100) → 屏幕 (700,200)
所以"判定用的是哪个坐标"可以从假 `detect_press_item` 收到的 `cursor_xy` 直接看出来。

L1 按住 A 后光标移到 B → 仍显示 A + [lock] 行；L2 松开时在别处 → 仍 A；
L3 重新按下 B → 显示 B（锁定重置）；L4 纯音频触发 → 不加锁、跟随光标；
L5 首次判定失败 → 后续成功才锁定；L6 延迟处理用**按下时刻**的位置；
L7 旧格式（裸时间戳 / 2 元组 / 3 元组）仍能工作。另有 L0：`_watch_loop` 真实接线。
"""

from __future__ import annotations

import contextlib
import inspect
import io
import os
import queue
import sys
import threading
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (str(ROOT), str(ROOT / "deps"), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402

import main as main_mod  # noqa: E402
import vision  # noqa: E402

NEAR = 70                           # 命中半径（px）
A_SCREEN, B_SCREEN = (1000, 500), (1200, 500)
EMPTY_SCREEN = (700, 200)
FAR_SCREEN = (3000, 2000)           # 远在 400x400 截图之外（给"锚点不在截图内"守卫用）

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), str(detail)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" :: {detail}" if detail else ""),
          flush=True)
    return bool(cond)


def section(title):
    print("-" * 104, flush=True)
    print(title, flush=True)
    print("-" * 104, flush=True)


def show(text, prefix="      "):
    for line in str(text).splitlines():
        print(prefix + line, flush=True)


class _FakeInfo:
    def __init__(self, **kw):
        self.slots = 0
        self.orientation = ""
        self.confidence = 0.0
        self.pitch = 0.0
        self.pitch_source = ""
        self.reason = ""
        self.source = ""
        self.highlight = None
        self.rect = None
        self.highlight_clipped = False
        for k, v in kw.items():
            setattr(self, k, v)


def _item(slots, orientation, screen):
    return _FakeInfo(slots=slots, orientation=orientation, confidence=0.71, pitch=85.0,
                     pitch_source="局部自洽打分(support=29)",
                     reason=f"来源=边框(局部线段) {slots}格{orientation}@{screen} 格宽85.0"
                            f"(局部自洽打分(support=29))",
                     source="边框",
                     highlight=(screen[0] - 40, screen[1] - 40, 80, 160),
                     rect=(screen[0] - 40, screen[1] - 40, 80, 160))


def _noitem():
    return _FakeInfo(slots=0, orientation="", confidence=0.0, source="",
                     reason="该位置没有物品边框（空格子/提示框/文字）；格宽85.0(局部自洽打分)")


def _world_lookup(screen):
    """世界模型：**屏幕坐标** → 物品（截图的 origin 由桩按中心算，见 Harness._grab）。"""
    if screen is None:
        return _noitem()
    x, y = int(screen[0]), int(screen[1])
    if abs(x - A_SCREEN[0]) <= NEAR and abs(y - A_SCREEN[1]) <= NEAR:
        return _item(2, "V", A_SCREEN)
    if abs(x - B_SCREEN[0]) <= NEAR and abs(y - B_SCREEN[1]) <= NEAR:
        return _item(1, "H", B_SCREEN)
    return _noitem()


class _FakeMatcher:
    """假匹配器：始终给 1 条音频候选 → 用于证明"锁定只锁视觉，音频候选照旧来自本次声音"。"""

    last_reason = "（探针：假匹配器）"

    def __init__(self, key="测试音效"):
        self.key = key

    def match(self, x, sr, limit, bg):  # noqa: ARG002
        return [{"key": self.key, "score": 99, "score_raw": 99, "path": "raw"}]


class Harness:
    """把 vision 截图 / detect_press_item / main.cursor_pos / main.left_button_down 换成脚本。

    ``support_center``：桩的签名是否带 `center=`（模拟 vision 两种版本：已落地 / 未落地）。
    ``honor_center``：桩是否真的按 center 截图（False = 模拟"真的越界/多屏异常"的守卫场景）。
    截图桩记录每次收到的 (pad, center)；`detect` 把 cursor_xy 换算回**屏幕坐标**再查世界模型，
    所以"截图中心 / 判定中心用的是哪个位置"都是可直接打印、可直接断言的证据。
    """

    def __init__(self, support_center=True, honor_center=True):
        self.world = {"pos": A_SCREEN}
        self.support_center = support_center
        self.honor_center = honor_center
        self.detected = []           # detect 收到的 cursor_xy（裁剪图坐标）
        self.det_screen = []         # 同上换算回屏幕坐标（= 实际判定中心）
        self.centers = []            # capture 收到的 center（None = 未传 center）
        self.pads = []
        self.origins = []
        self.last_origin = (0, 0)
        self.down_script = None      # [(down, world_pos), ...]；None=恒 False
        self._i = 0
        self._orig = {}

    def install(self):
        self._orig = {
            "crop": vision.capture_cursor_region, "full": vision.capture_full_screen,
            "det": vision.detect_press_item, "cursor": main_mod.cursor_pos,
            "down": main_mod.left_button_down, "f8": main_mod.f8_down,
        }
        # 两种签名：不带 center 的就是"vision 还没落地"的旧版
        if self.support_center:
            vision.capture_cursor_region = lambda pad=140, center=None: self._grab(pad, center)
        else:
            vision.capture_cursor_region = lambda pad=140: self._grab(pad, None)
        vision.capture_full_screen = self.fake_full
        vision.detect_press_item = self.fake_detect
        main_mod.cursor_pos = lambda: tuple(self.world["pos"])
        main_mod.left_button_down = self.fake_down
        main_mod.f8_down = lambda: False
        return self

    def remove(self):
        vision.capture_cursor_region = self._orig["crop"]
        vision.capture_full_screen = self._orig["full"]
        vision.detect_press_item = self._orig["det"]
        main_mod.cursor_pos = self._orig["cursor"]
        main_mod.left_button_down = self._orig["down"]
        main_mod.f8_down = self._orig["f8"]

    # ---- 脚本 ----
    def _grab(self, pad, center):
        pad = int(pad)
        self.pads.append(pad)
        self.centers.append(None if center is None else tuple(center))
        if self.honor_center and center is not None:
            origin = (int(center[0]) - pad, int(center[1]) - pad)   # 与判定同中心
        else:
            pos = self.world["pos"]                                 # 旧版：实时光标为中心
            origin = (int(pos[0]) - pad, int(pos[1]) - pad)
        self.last_origin = origin
        self.origins.append(origin)
        return np.zeros((2 * pad, 2 * pad, 3), np.uint8), origin

    def fake_full(self):
        return np.zeros((1000, 1400, 3), np.uint8), (0, 0)

    def fake_detect(self, image, cursor_xy=None, pitch=None, debug_dir=None,
                    ignore_rects=None, **kw):
        self.detected.append(None if cursor_xy is None else tuple(cursor_xy))
        if cursor_xy is None:
            self.det_screen.append(None)
            return _noitem()
        screen = (self.last_origin[0] + int(cursor_xy[0]),
                  self.last_origin[1] + int(cursor_xy[1]))
        self.det_screen.append(screen)
        return _world_lookup(screen)

    def fake_down(self):
        if self.down_script is None:
            return False
        if self._i < len(self.down_script):
            down, pos = self.down_script[self._i]
            self._i += 1
            if pos is not None:
                self.world["pos"] = tuple(pos)
            return bool(down)
        return bool(self.down_script[-1][0])


def make_engine(extra=()):
    args = main_mod.parse_args(
        ["--limit", "3", "--settle", "0", "--vision-wait", "0", "--capture-mode", "crop",
         "--pad", "200", "--pad-max", "0", "--listen-max", "0", "--retry", "0",
         "--trigger", "mouse", "--min-interval", "0"] + list(extra))
    eng = main_mod.Engine(args)
    eng.matcher = _FakeMatcher()
    eng.audio.snapshot = lambda: (LOOPBACK[0], LOOPBACK[1], 0.05)
    return eng


def load_loopback():
    with np.load(HERE / "loopback_ref.npz") as z:
        return (np.asarray(z["window"], dtype=np.float32).reshape(-1),
                float(z["background_rms"]))


try:
    LOOPBACK = load_loopback()
except Exception:  # pragma: no cover
    LOOPBACK = (np.zeros(4410, np.float32), 0.0)


def run_trigger(eng, payload):
    """按队列元素形状处理一次触发，返回 (stdout, 结果 dict 或 None)。"""
    ts, src, cur, sid = main_mod._split_trigger(payload)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        eng.handle_trigger(ts, src, cur, sid)
    out = buf.getvalue()
    try:
        item = eng.results.get_nowait()
    except queue.Empty:
        item = None
    return out, item


def lines_of(out, tag):
    return [ln for ln in str(out).splitlines() if tag in ln]


def brief(item):
    if item is None:
        return "(无结果)"
    return (f"slots={item['slots']} 朝向={item['orientation'] or '-'} "
            f"来源={item['source'] or '无'} 候选={len(item['rows'])} "
            f"lock_state={item.get('lock_state') or '-'}")


def emit_evidence(out, item, label="本次"):
    for ln in lines_of(out, "[lock]"):
        show(f"{label} [lock] 行：{ln}")
    for ln in lines_of(out, "[trace]"):
        show(f"{label} [trace] ：{ln}")
    if item is not None:
        show(f"{label} 结果 dict ：{brief(item)}")
        show(f"{label} 状态行   ：{main_mod._result_status_line(item, item['source'] or '无', item.get('trigger_src') or '未知')}")


# --------------------------------------------------------------------------- #
# L0 `_watch_loop` 真实接线：按下沿读锚点 + 新会话；续触/松开继承同一会话
# --------------------------------------------------------------------------- #
def case_L0_watch_loop_wiring():
    h = Harness().install()
    eng = make_engine(["--poll", "0.01"])
    collected = []
    try:
        # 脚本：按下@A → 按住(光标已到 B) → 松开@B → 重新按下@B → 松开@B → 保持松开
        h.down_script = [(True, A_SCREEN), (True, B_SCREEN), (False, B_SCREEN),
                         (True, B_SCREEN), (False, B_SCREEN), (False, B_SCREEN)]
        eng.set_active(True)
        th = threading.Thread(target=eng._watch_loop, daemon=True)
        th.start()
        deadline = time.time() + 0.8
        while time.time() < deadline and len(collected) < 5:
            try:
                collected.append(eng.triggers.get(timeout=0.05))
            except queue.Empty:
                pass
        eng.stop_event.set()
        th.join(timeout=1.0)
    finally:
        h.remove()
        eng.shutdown()
    parsed = [main_mod._split_trigger(p) for p in collected]
    print("      _watch_loop 实跑（脚本：按下@A → 续触@B → 松开@B → 按下@B → 松开@B）：")
    for p in parsed:
        show(f"入队 (ts, 来源={p[1]!r}, 光标={p[2]}, 会话={p[3]})")
    presses = [p for p in parsed if p[1] == main_mod.TRIG_PRESS]
    s1 = presses[0][3] if presses else 0
    same = [p for p in parsed if p[3] == s1]
    holds = [p for p in parsed if p[1] == main_mod.TRIG_HOLD]
    releases = [p for p in parsed if p[1] == main_mod.TRIG_RELEASE]
    s2 = presses[1][3] if len(presses) > 1 else -1
    ok = (len(presses) >= 2 and s1 > 0 and s2 > s1
          and presses[0][2] == A_SCREEN                     # 按下沿锚点 = 按下时刻光标
          and holds and all(p[2] == B_SCREEN for p in holds)  # 续触带"此刻"光标
          and all(p[3] == s1 for p in same if p[1] != main_mod.TRIG_PRESS or p is presses[0])
          and releases and all(p[3] == s1 for p in releases[:1])
          and presses[1][3] == s2 and presses[1][2] == B_SCREEN)
    yield_lines = [f"按下沿 sid={s1} 锚点={presses[0][2] if presses else None}",
                   f"续触 光标={[p[2] for p in holds]} sid={[p[3] for p in holds]}",
                   f"松开 光标={[p[2] for p in releases]} sid={[p[3] for p in releases]}",
                   f"再次按下 sid={s2}（应为新会话 > {s1}）光标="
                   f"{presses[1][2] if len(presses) > 1 else None}"]
    for line in yield_lines:
        show(line)
    return ok, "；".join(yield_lines)


# --------------------------------------------------------------------------- #
# L1 按住 A → 光标移到 B → 仍显示 A + [lock] 行
# --------------------------------------------------------------------------- #
def case_L1_hold_ignores_other_item():
    h = Harness().install()
    eng = make_engine()
    try:
        eng.set_active(True)
        h.world["pos"] = A_SCREEN
        sid = eng._press_session_new(A_SCREEN)                     # 模拟按下沿（锚点 A）
        out1, item1 = run_trigger(eng, (time.time(), main_mod.TRIG_PRESS, A_SCREEN, sid))
        h.world["pos"] = B_SCREEN                                  # 鼠标掠过 B
        out2, item2 = run_trigger(eng, (time.time(), main_mod.TRIG_HOLD, B_SCREEN, sid))
        show("L1 第 1 次触发（按下 @A）：")
        emit_evidence(out1, item1)
        show("L1 第 2 次触发（按住续触，光标已到 B）：")
        emit_evidence(out2, item2)
        lock_lines = lines_of(out2, "[lock]")
        print(f"      假 detect 收到的 cursor_xy（裁剪图坐标）：{h.detected}")
        print(f"      截图收到的 center：{h.centers}（None=未传）｜判定中心（屏幕坐标）：{h.det_screen}")
        ok = (item1 is not None and int(item1["slots"]) == 2 and item1["orientation"] == "V"
              and item2 is not None and int(item2["slots"]) == 2 and item2["orientation"] == "V"
              and (item2["source"] or "") == "边框"
              and len(lock_lines) == 1 and "忽略当前位置的 1格H" in lock_lines[0]
              and "2格V" in lock_lines[0]
              and item2["lock_state"].startswith("已锁 2格V")
              and len(item2["rows"]) == 1)                        # 音频候选照旧来自本次声音
        return ok, (f"{brief(item1)} → {brief(item2)}；[lock]={lock_lines[0] if lock_lines else '(无)'}")
    finally:
        h.remove()
        eng.shutdown()


# --------------------------------------------------------------------------- #
# L2 松开时光标在别处 → 仍 A（松开那次触发仍属于该会话）
# --------------------------------------------------------------------------- #
def case_L2_release_keeps_lock():
    h = Harness().install()
    eng = make_engine()
    try:
        eng.set_active(True)
        h.world["pos"] = A_SCREEN
        sid = eng._press_session_new(A_SCREEN)
        run_trigger(eng, (time.time(), main_mod.TRIG_PRESS, A_SCREEN, sid))
        h.world["pos"] = B_SCREEN
        run_trigger(eng, (time.time(), main_mod.TRIG_HOLD, B_SCREEN, sid))
        out_r, item_r = run_trigger(eng, (time.time(), main_mod.TRIG_RELEASE, B_SCREEN, sid))
        eng._press_session_end()                                   # 松开：会话结束
        show("L2 松开触发（光标在 B，会话已结束但该触发仍带 sid）：")
        emit_evidence(out_r, item_r)
        ok = (item_r is not None and int(item_r["slots"]) == 2 and item_r["orientation"] == "V"
              and len(lines_of(out_r, "[lock]")) == 1)
        return ok, f"松开 → {brief(item_r)}；[lock] 行 {len(lines_of(out_r, '[lock]'))} 条"
    finally:
        h.remove()
        eng.shutdown()


# --------------------------------------------------------------------------- #
# L3 重新按下 B → 显示 B（锁定被重置）
# --------------------------------------------------------------------------- #
def case_L3_repress_resets_lock():
    h = Harness().install()
    eng = make_engine()
    try:
        eng.set_active(True)
        # 第一次按下 A（会话 1）
        h.world["pos"] = A_SCREEN
        sid1 = eng._press_session_new(A_SCREEN)
        run_trigger(eng, (time.time(), main_mod.TRIG_PRESS, A_SCREEN, sid1))
        h.world["pos"] = B_SCREEN
        out_hold, item_hold = run_trigger(eng, (time.time(), main_mod.TRIG_HOLD, B_SCREEN, sid1))
        run_trigger(eng, (time.time(), main_mod.TRIG_RELEASE, B_SCREEN, sid1))
        eng._press_session_end()
        # 第二次按下 B（新会话）
        sid2 = eng._press_session_new(B_SCREEN)
        out_new, item_new = run_trigger(eng, (time.time(), main_mod.TRIG_PRESS, B_SCREEN, sid2))
        show(f"L3 会话重置：sid1={sid1}（锁 A） → sid2={sid2}（按下 B）")
        show("L3 第一次按下期间的续触（被锁成 A）：")
        emit_evidence(out_hold, item_hold)
        show("L3 重新按下 B 之后：")
        emit_evidence(out_new, item_new)
        ok = (item_hold is not None and int(item_hold["slots"]) == 2
              and item_new is not None and int(item_new["slots"]) == 1
              and item_new["orientation"] == "H"
              and item_new["lock_state"].startswith("已锁 1格H")
              and not lines_of(out_new, "[lock]"))                  # 新会话首次成功即可锁定
        return ok, (f"续触 {brief(item_hold)} → 重新按下 {brief(item_new)}；"
                    f"新会话 [lock] 行 {len(lines_of(out_new, '[lock]'))} 条")
    finally:
        h.remove()
        eng.shutdown()


# --------------------------------------------------------------------------- #
# L4 纯音频触发（无按键会话）→ 不加锁、跟随当前光标
# --------------------------------------------------------------------------- #
def case_L4_audio_no_lock():
    h = Harness().install()
    eng = make_engine(["--trigger", "both"])
    try:
        eng.set_active(True)
        # 4a 生产侧：未按下 → 会话 0；按下期间 → 继承当前会话
        h.world["pos"] = B_SCREEN
        cur_a, sid_a = eng._audio_session()
        sid_press = eng._press_session_new(A_SCREEN)
        cur_b, sid_b = eng._audio_session()
        eng._press_session_end()
        cur_c, sid_c = eng._audio_session()
        print(f"      _audio_session（未按下）→ cursor={cur_a} sid={sid_a}")
        print(f"      _audio_session（按下期间）→ cursor={cur_b} sid={sid_b}（=会话 {sid_press}）")
        print(f"      _audio_session（已松开）→ cursor={cur_c} sid={sid_c}")
        # 4b 消费侧：sid=0 的音频触发不加锁，跟随光标（B → 1格H）
        h.world["pos"] = B_SCREEN
        out1, item1 = run_trigger(eng, (time.time(), main_mod.TRIG_AUDIO, B_SCREEN,
                                       main_mod.SESSION_NONE))
        h.world["pos"] = A_SCREEN
        out2, item2 = run_trigger(eng, (time.time(), main_mod.TRIG_AUDIO, A_SCREEN,
                                       main_mod.SESSION_NONE))
        show("L4 音频触发（未按下，无会话）：")
        emit_evidence(out1, item1)
        emit_evidence(out2, item2)
        ok = (sid_a == main_mod.SESSION_NONE and sid_b == sid_press
              and sid_c == main_mod.SESSION_NONE
              and item1 is not None and int(item1["slots"]) == 1 and item1["orientation"] == "H"
              and not item1["lock_state"]
              and item2 is not None and int(item2["slots"]) == 2 and item2["orientation"] == "V"
              and not item2["lock_state"]
              and not lines_of(out1, "[lock]") and not lines_of(out2, "[lock]"))
        return ok, (f"sid: 未按下={sid_a} 按下={sid_b} 松开={sid_c}；"
                    f"音频#1 {brief(item1)}；音频#2 {brief(item2)}（跟随光标、无锁）")
    finally:
        h.remove()
        eng.shutdown()


# --------------------------------------------------------------------------- #
# L5 会话内首次判定失败（无物品）→ 后续成功才锁定
# --------------------------------------------------------------------------- #
def case_L5_first_fail_then_lock():
    h = Harness().install()
    eng = make_engine()
    try:
        eng.set_active(True)
        h.world["pos"] = EMPTY_SCREEN
        sid = eng._press_session_new(EMPTY_SCREEN)
        out_p, item_p = run_trigger(eng, (time.time(), main_mod.TRIG_PRESS, EMPTY_SCREEN, sid))
        out_h1, item_h1 = run_trigger(eng, (time.time(), main_mod.TRIG_HOLD, A_SCREEN, sid))
        out_h2, item_h2 = run_trigger(eng, (time.time(), main_mod.TRIG_HOLD, B_SCREEN, sid))
        show("L5 第 1 次（空格子，判不出物品 → 不锁定）：")
        emit_evidence(out_p, item_p)
        show("L5 第 2 次（光标到 A，首次成功 → 此时才锁定）：")
        emit_evidence(out_h1, item_h1)
        show("L5 第 3 次（光标到 B → 已锁 A，忽略）：")
        emit_evidence(out_h2, item_h2)
        ok = (item_p is not None and int(item_p["slots"]) == 0 and not item_p["lock_state"]
              and item_h1 is not None and int(item_h1["slots"]) == 2
              and item_h1["lock_state"].startswith("已锁 2格V")
              and not lines_of(out_h1, "[lock]")                 # 首次锁定不需要"忽略"提示
              and item_h2 is not None and int(item_h2["slots"]) == 2
              and len(lines_of(out_h2, "[lock]")) == 1)
        return ok, (f"失败 {brief(item_p)} → 成功锁定 {brief(item_h1)} → 掠过 {brief(item_h2)}")
    finally:
        h.remove()
        eng.shutdown()


# --------------------------------------------------------------------------- #
# L6 触发携带的光标位置生效（延迟处理用按下时刻的位置）
# --------------------------------------------------------------------------- #
def case_L6_carried_cursor_wins():
    h = Harness().install()
    eng = make_engine()
    try:
        eng.set_active(True)
        h.world["pos"] = A_SCREEN
        sid = eng._press_session_new(A_SCREEN)
        payload = (time.time(), main_mod.TRIG_PRESS, A_SCREEN, sid)   # 按下时刻在 A
        h.world["pos"] = B_SCREEN                                     # 处理前鼠标已到 B
        out, item = run_trigger(eng, payload)
        print(f"      触发携带光标={A_SCREEN}；处理时实时光标={B_SCREEN}")
        print(f"      截图收到的 center={h.centers}（应=[{A_SCREEN}]，不是实时光标 {B_SCREEN}）")
        print(f"      判定中心（屏幕坐标）={h.det_screen}（应=[{A_SCREEN}]）")
        emit_evidence(out, item)
        ok = (h.det_screen == [A_SCREEN] and item is not None and int(item["slots"]) == 2
              and item["orientation"] == "V")
        return ok, f"判定中心={h.det_screen} → {brief(item)}"
    finally:
        h.remove()
        eng.shutdown()


# --------------------------------------------------------------------------- #
# L7 旧格式（裸时间戳 / 2 元组 / 3 元组）仍能工作
# --------------------------------------------------------------------------- #
def case_L7_legacy_payloads():
    h = Harness().install()
    eng = make_engine()
    try:
        eng.set_active(True)
        h.world["pos"] = B_SCREEN
        out1, item1 = run_trigger(eng, time.time())                       # 裸时间戳
        out2, item2 = run_trigger(eng, (time.time(), main_mod.TRIG_HOLD))  # 旧 2 元组
        out3, item3 = run_trigger(eng, (time.time(), main_mod.TRIG_PRESS, B_SCREEN))  # 3 元组
        print(f"      实时光标={B_SCREEN}｜截图 center={h.centers}（前两条应为 None）")
        show("L7 裸时间戳（回退实时光标、无会话）：")
        emit_evidence(out1, item1)
        show("L7 旧 2 元组（同上）：")
        emit_evidence(out2, item2)
        show("L7 旧 3 元组（带光标、无会话 id → 不加锁）：")
        emit_evidence(out3, item3)
        print(f"      判定中心（屏幕坐标）={h.det_screen}")
        ok = (item1 is not None and int(item1["slots"]) == 1 and not item1["lock_state"]
              and item2 is not None and int(item2["slots"]) == 1 and not item2["lock_state"]
              and item3 is not None and int(item3["slots"]) == 1 and not item3["lock_state"]
              and h.det_screen == [B_SCREEN, B_SCREEN, B_SCREEN]      # 都用了实时光标
              and h.centers[:2] == [None, None]                       # 旧格式不传 center
              and h.centers[2] == B_SCREEN                            # 3 元组带了光标 → 传 center
              and not lines_of(out1, "[lock]"))
        return ok, f"三种旧格式都跑通：{[brief(i) for i in (item1, item2, item3)]}"
    finally:
        h.remove()
        eng.shutdown()


# --------------------------------------------------------------------------- #
# L8 面板/状态行可见性（ui.py 不在本 lane，只走 set_status 通道）
# --------------------------------------------------------------------------- #
def case_L8_status_visibility():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtWidgets import QApplication, QLabel

    import ui

    app = QApplication.instance() or QApplication(sys.argv[:1])  # noqa: F841
    panel = ui.Panel(None, start_thread=False)
    item = {"rows": [{"name": "金块", "score": 99}], "slots": 2, "orientation": "V",
            "reason": "物品边框…", "confidence": 0.71, "source": "边框",
            "trigger_src": main_mod.TRIG_HOLD, "lock_state": "已锁 2格V@(1000,500)"}
    # 会话锁仍然在**数据通路**里（_result_status_line 保留，供日志/离线断言），
    # 但 task-14 起面板不再显示任何诊断：底栏只放检测状态，详情行只有 格数/朝向/来源。
    lock_line = main_mod._result_status_line(item, item["source"], item["trigger_src"])
    panel.set_active_state(True)                     # 监测中 → 底栏写检测状态
    panel.show_candidates(item["rows"], item["slots"], item["orientation"], item["reason"],
                          source=item["source"], trigger_src=item["trigger_src"])
    panel_line = panel.reason_label.text()
    status_line = panel.status_label.text()
    visible = [(lab.objectName(), lab.text()) for lab in panel.findChildren(QLabel)
               if lab.isVisible()]
    leaks = [(tok, obj, txt) for obj, txt in visible
             for tok in ("触发", "原因", "格宽", "锁定", "已锁", "trace", "看到", "受理")
             if tok in txt]
    print(f"      面板详情行：{panel_line!r}")
    print(f"      面板状态行：{status_line!r}")
    print(f"      锁信息（数据通路保留）：{lock_line!r}")
    print(f"      面板可见文本：{visible}")
    print(f"      诊断字样泄漏：{leaks if leaks else '0 命中'}")
    panel.close()
    ok = (lock_line.startswith("已锁 2格V@(1000,500)｜")           # 锁仍可读（日志/离线断言）
          and panel_line == "格数 2格 · 朝向 V · 来源 边框"        # 详情行只 3 个字段
          and status_line == ui.MONITORING_STATUS_TEXT            # 底栏只检测状态
          and panel._last_trigger_src == main_mod.TRIG_HOLD       # 触发源送达但不显示
          and not leaks                                          # 面板找不到诊断字样
          and main_mod._IDLE_STATUS == ui.IDLE_STATUS_TEXT)       # 空闲文案与 ui 逐字一致
    return ok, (f"{lock_line!r} / {panel_line!r} / {status_line!r} / "
                f"trig={panel._last_trigger_src!r} / leaks={leaks}")


def case_L9_stop_clears_session():
    """按住时停止监测 → 会话必须被丢弃，之后的音频触发不得继承陈旧会话（要求 5）。"""
    h = Harness().install()
    eng = make_engine(["--trigger", "both"])
    try:
        eng.set_active(True)
        sid = eng._press_session_new(A_SCREEN)
        open_before = eng._open_session_id()
        eng.set_active(False)                     # 按住时停止监测
        open_after = eng._open_session_id()
        eng.set_active(True)                      # 重新开始
        cur, sid_audio = eng._audio_session()
        out, item = run_trigger(eng, (time.time(), main_mod.TRIG_AUDIO, A_SCREEN, sid_audio))
        show(f"L9 按住时停止监测：会话 {sid} → _open_session {open_before} ⇒ {open_after}")
        show(f"L9 重新开始后未按下（光标 A）：_audio_session → cursor={cur} sid={sid_audio}")
        emit_evidence(out, item)
        ok = (open_before == sid and open_after == main_mod.SESSION_NONE
              and sid_audio == main_mod.SESSION_NONE
              and item is not None and int(item["slots"]) == 2
              and not item["lock_state"])          # 判出了物品，但无会话 → 不加锁
        return ok, (f"会话 {sid}→{open_after}；音频 sid={sid_audio}；{brief(item)}")
    finally:
        h.remove()
        eng.shutdown()


def case_L10_capture_center():
    """task-12：截图中心 = 判定中心 = 触发携带的光标（vision 已支持 center 时）。

    三个断言：
      · 处理前把实时光标挪到 B，触发携带的是 A → 截图收到的 center 必须是 **A**（不是实时光标 B）；
      · 按住续触（携带 B）→ center=B；
      · 旧格式（裸时间戳，未携带光标）→ **不传 center**（保持今天行为）。
    """
    h = Harness(support_center=True).install()
    eng = make_engine()
    try:
        eng.set_active(True)
        sid = eng._press_session_new(A_SCREEN)
        payload = (time.time(), main_mod.TRIG_PRESS, A_SCREEN, sid)
        h.world["pos"] = B_SCREEN                      # 处理前鼠标已经到 B（--settle 场景）
        out1, item1 = run_trigger(eng, payload)
        out2, item2 = run_trigger(eng, (time.time(), main_mod.TRIG_HOLD, B_SCREEN, sid))
        h.world["pos"] = B_SCREEN
        out3, item3 = run_trigger(eng, time.time())    # 旧格式：不携带光标
        print(f"      vision.capture_cursor_region 支持 center = "
              f"{main_mod.Engine._accepts_kwarg(vision.capture_cursor_region, 'center')}")
        print(f"      截图收到的 (center) 序列 = {h.centers}（第 3 次 None=未传）")
        print(f"      截图收到的 pad 序列    = {h.pads}")
        print(f"      截图 origin 序列       = {h.origins}")
        print(f"      判定中心（屏幕坐标）   = {h.det_screen}")
        show("L10 第 1 次（按下触发携带 A，实时光标已到 B）：")
        for ln in lines_of(out1, "[trace]"):
            show(f"[trace] {ln}")
        show("L10 第 3 次（旧格式裸时间戳 → 不传 center）：")
        for ln in lines_of(out3, "[trace]"):
            show(f"[trace] {ln}")
        ok = (h.centers[0] == A_SCREEN and h.centers[1] == B_SCREEN and h.centers[2] is None
              and h.det_screen == [A_SCREEN, B_SCREEN, B_SCREEN]
              and "截图中心=触发锚点" in out1 and "截图中心=触发锚点" in out2
              and "未携带光标" in out3
              and item1 is not None and int(item1["slots"]) == 2
              and item3 is not None and int(item3["slots"]) == 1)
        return ok, (f"center={h.centers}（含 1 次 None）｜判定中心={h.det_screen}；"
                    f"trace 标注『截图中心=触发锚点』/旧格式『未携带光标』")
    finally:
        h.remove()
        eng.shutdown()


def case_L11_vision_without_center():
    """task-12：vision 还没落地 `center=` 时，自动退回旧调用、不抛异常、结果仍正确。"""
    h = Harness(support_center=False).install()        # 桩签名 = 旧版 (pad)
    eng = make_engine()
    try:
        eng.set_active(True)
        supported = eng._capture_center_supported(vision)
        eng._press_session_new(A_SCREEN)
        h.world["pos"] = B_SCREEN                      # 处理时实时光标在 B
        payload = (time.time(), main_mod.TRIG_PRESS, A_SCREEN, 1)
        out, item = run_trigger(eng, payload)
        print(f"      _capture_center_supported(桩) = {supported}（应为 False）")
        print(f"      截图收到的 center 序列 = {h.centers}（None=主程序没传 center）")
        print(f"      截图 origin = {h.origins}（按实时光标 {B_SCREEN} 居中）")
        print(f"      判定中心（屏幕坐标） = {h.det_screen}（仍应为携带的 {A_SCREEN}）")
        show("L11 退回旧调用的 trace（应含『vision 不支持 center』）：")
        for ln in lines_of(out, "[trace]"):
            show(f"[trace] {ln}")
        ok = (supported is False
              and h.centers == [None]                     # 没传 center（否则旧签名会 TypeError）
              and h.det_screen == [A_SCREEN]              # 判定中心仍是触发携带的光标
              and item is not None and int(item["slots"]) == 2
              and item["orientation"] == "V"
              and "vision 不支持 center" in out
              and "异常" not in out)
        return ok, (f"支持 center={supported}｜center={h.centers}｜判定中心={h.det_screen}｜"
                    f"{brief(item)}｜trace 标注『vision 不支持 center』")
    finally:
        h.remove()
        eng.shutdown()


def case_L12_guard_wording():
    """task-12 要求 4：守卫保留，且日志能区分「vision 不支持 center」与「真的越界」。"""
    results = {}
    for label, kwargs in (("a", {"support_center": False}),          # 不支持 center（截图按实时光标）
                          ("b", {"support_center": True, "honor_center": False})):  # 已传 center 仍越界
        h = Harness(**kwargs).install()
        eng = make_engine()
        try:
            eng.set_active(True)
            eng._press_session_new(FAR_SCREEN)
            h.world["pos"] = B_SCREEN               # 实时光标在 B；锚点 FAR 远在 400x400 之外
            out, item = run_trigger(eng, (time.time(), main_mod.TRIG_PRESS, FAR_SCREEN, 1))
            note = ""
            for ln in lines_of(out, "[trace]"):
                if "不在截图内" in ln:
                    note = ln
            show(f"L12({label}) 守卫触发时 trace：{note or '(未触发守卫)'}")
            results[label] = (out, item, note, h.centers)
        finally:
            h.remove()
            eng.shutdown()
    out_a, item_a, note_a, centers_a = results["a"]
    out_b, item_b, note_b, centers_b = results["b"]
    ok = (item_a is not None and int(item_a["slots"]) == 0
          and item_b is not None and int(item_b["slots"]) == 0
          and "不在截图内" in note_a and "vision 不支持 center" in note_a
          and "不在截图内" in note_b and "已按锚点为中心截图" in note_b
          and centers_a == [None] and centers_b == [FAR_SCREEN])
    return ok, (f"(a) 不支持 center → {note_a[:120]} ／ (b) 已传 center 却越界 → {note_b[:120]}")


def main():
    section("task-9/12 探针：按下会话锁 + 截图中心=触发携带光标")
    print(f"  main.py sha256 = {__import__('hashlib').sha256((ROOT / 'main.py').read_bytes()).hexdigest()}")
    print(f"  世界模型（屏幕坐标）：A(2格V)={A_SCREEN}｜B(1格H)={B_SCREEN}"
          f"｜空格子={EMPTY_SCREEN}｜远点={FAR_SCREEN}；命中半径 {NEAR}px；pad=200 → 400x400")
    print(f"  截图桩按 center 算 origin（=center-pad），detect 的 cursor_xy 再换算回屏幕坐标"
          f" → 可直接断言『截图中心/判定中心到底是哪个位置』")
    print(f"  只读：假截图/假匹配器/脚本化键鼠状态；不抓屏、不开设备、不模拟输入、不联网")
    # 真实 vision 模块当下的签名（只读探测，不抓屏）：两个 lane 的接线是否已生效
    try:
        _sig = inspect.signature(vision.capture_cursor_region)
    except (TypeError, ValueError) as _exc:              # pragma: no cover
        _sig = f"<拿不到签名: {_exc}>"
    print(f"  真实 vision.capture_cursor_region 签名 = {_sig}"
          f" → 支持 center = "
          f"{main_mod.Engine._accepts_kwarg(vision.capture_cursor_region, 'center')}")
    print()

    section("L0 `_watch_loop` 真实接线（按下沿锚点 + 会话继承 + 新按下新会话）")
    ok, detail = case_L0_watch_loop_wiring()
    check("L0 按下沿带锚点、续触/松开继承同一会话、重新按下换新会话", ok, detail)
    print()

    section("L1 按住 A 后光标移到 B → 仍 2格V 且有 [lock] 行")
    ok, detail = case_L1_hold_ignores_other_item()
    check("L1 掠过 B 不改写已选 A（[lock] 只报告）", ok, detail)
    print()

    section("L2 松开时光标在别处 → 仍 A")
    ok, detail = case_L2_release_keeps_lock()
    check("L2 松开那次触发仍属于该会话", ok, detail)
    print()

    section("L3 重新按下 B → 显示 B（锁定被重置）")
    ok, detail = case_L3_repress_resets_lock()
    check("L3 重新按下才换目标", ok, detail)
    print()

    section("L4 纯音频触发（无按键会话）→ 不加锁、跟随当前光标")
    ok, detail = case_L4_audio_no_lock()
    check("L4 无会话不加锁；按下期间的音频触发继承会话", ok, detail)
    print()

    section("L5 会话内首次判定失败 → 后续成功才锁定")
    ok, detail = case_L5_first_fail_then_lock()
    check("L5 首次成功才锁定（slots>0 且 source!='')", ok, detail)
    print()

    section("L6 触发携带的光标位置生效（延迟处理也不偏）")
    ok, detail = case_L6_carried_cursor_wins()
    check("L6 用按下时刻的位置判定，不用处理时刻的实时光标", ok, detail)
    print()

    section("L7 旧格式（裸时间戳 / 2 元组 / 3 元组）仍能工作")
    ok, detail = case_L7_legacy_payloads()
    check("L7 旧格式回退实时光标、无会话不加锁", ok, detail)
    print()

    section("L8 面板/状态行可见性（已锁 2格V）")
    ok, detail = case_L8_status_visibility()
    check("L8 会话锁文案可读（数据通路）但面板只显示检测状态+三字段详情", ok, detail)
    print()

    section("L9 按住时停止监测 → 丢弃会话（不留下陈旧锁）")
    ok, detail = case_L9_stop_clears_session()
    check("L9 停止监测清空会话；未按下的音频触发不加锁", ok, detail)
    print()

    section("L10 截图中心 = 触发携带的光标（vision 支持 center 时）")
    ok, detail = case_L10_capture_center()
    check("L10 带光标→传 center=触发光标；旧格式→不传 center", ok, detail)
    print()

    section("L11 vision 不支持 center → 自动退回旧调用、不崩、结果仍对")
    ok, detail = case_L11_vision_without_center()
    check("L11 缺参不崩且判定中心仍是触发携带的光标", ok, detail)
    print()

    section("L12 守卫保留 + 日志区分「不支持 center」与「真的越界」")
    ok, detail = case_L12_guard_wording()
    check("L12 两种原因在日志里可区分", ok, detail)

    section("汇总")
    passed = sum(1 for _n, g, _d in RESULTS if g)
    print(f"检查项 {len(RESULTS)} 项，通过 {passed} 项，失败 {len(RESULTS) - passed} 项")
    for name, good, det in RESULTS:
        print(f"  [{'PASS' if good else 'FAIL'}] {name} :: {str(det)[:220]}")
    print("RESULT:", "ALL PASS" if passed == len(RESULTS) else "HAS FAILURES")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())