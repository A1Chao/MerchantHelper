# -*- coding: utf-8 -*-
r"""快拖回归：**按下沿每次都必须产生触发** + 手势未锁定时低优先级取样必须放行。

用户场景（实测日志）：`presses_seen=42 / presses_accepted=12` —— 30 次按下连触发都没产生；
而商人/出售页的占格**只出现在拖动红块里**（该页悬停/按下帧没有任何占地特征，实测 81 点 0 命中），
所以"没有触发" = **不可恢复**的一次识别机会。这一组用例把修复钉住：

  A. 4 组快速按下/松开、`--min-interval` 保持默认 0.9s → 4 次按下必须全部受理
     （修复前：0.9s 闸门只放行第 1 次 → 这正是游戏里"拖很快就没结果"的根因）
  B. `_enqueue_trigger(low_priority=True)` 的准入：
       · 手势**未锁定** → 即使队列忙也放行（快拖时续触/松开是补截红块的唯一机会）
       · 手势**已锁定** → 恢复"队列忙即丢"的节流
       · 无会话（session=0，纯音频）→ 保持旧行为（队列忙即丢）

跑法：
  cmd /d /c "D:\PY\python.exe D:\pycharm\UA\行商助手\tests\mprobe_fast_drag.py > tests\out_mprobe_fast_drag.txt 2>&1"
"""
from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (str(ROOT), str(ROOT / "deps"), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import main as main_mod  # noqa: E402
from mprobe_session_lock import (  # noqa: E402
    A_SCREEN, B_SCREEN, Harness, check, make_engine, section, show,
)

DEFAULT_MIN_INTERVAL = 0.9          # main.py 的默认值


def case_fast_presses_all_accepted():
    """A. 4 组快速按下/松开（默认 0.9s 音频节流）→ 4 次按下都要被受理。"""
    h = Harness().install()
    eng = make_engine(["--poll", "0.01", "--min-interval", str(DEFAULT_MIN_INTERVAL)])
    collected = []
    try:
        # 每组 = 一次按下 + 一次松开（脚本每轮消费一步，轮询 0.01s → 整段约 90ms）
        h.down_script = [(True, A_SCREEN), (False, A_SCREEN)] * 4 + [(False, A_SCREEN)]
        eng.set_active(True)
        th = threading.Thread(target=eng._watch_loop, daemon=True)
        th.start()
        deadline = time.time() + 2.0
        while time.time() < deadline and len(collected) < 8:
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
    presses = [p for p in parsed if p[1] == main_mod.TRIG_PRESS]
    releases = [p for p in parsed if p[1] == main_mod.TRIG_RELEASE]
    seen = int(eng.stats["presses_seen"])
    accepted = int(eng.stats["presses_accepted"])
    show(f"物理按下 {seen} 次 / 受理 {accepted} 次 / 入队按下 {len(presses)} 次 / 松开 {len(releases)} 次")
    show(f"入队来源序列 = {[p[1] for p in parsed]}")
    show(f"（修复前：--min-interval {DEFAULT_MIN_INTERVAL}s 会只放行第 1 次按下）")
    ok = (seen >= 4 and accepted >= 4 and len(presses) >= 4)
    return ok, f"看到 {seen} / 受理 {accepted} / 入队按下 {len(presses)}（要求 >=4）"


def case_low_priority_admission():
    """B. 低优先级准入：未锁定放行、已锁定丢弃、无会话保持旧行为。"""
    eng = make_engine(["--poll", "0.01"])
    try:
        sid = eng._press_session_new(A_SCREEN)
        ts = time.time()
        # 先占住队列，模拟"按下那一帧还在处理"
        eng.triggers.put_nowait((ts, main_mod.TRIG_PRESS, A_SCREEN, sid))
        base = eng.triggers.qsize()

        # B1) 手势未锁定 → 续触必须放行
        eng._enqueue_trigger(ts, low_priority=True, source=main_mod.TRIG_HOLD,
                             cursor_xy=B_SCREEN, session_id=sid)
        q_after_unlocked = eng.triggers.qsize()
        admitted_unlocked = (q_after_unlocked == base + 1)

        # B2) 手势已锁定 → 续触必须被丢
        sess = eng._press_session_get(sid)
        sess.locked = {"slots": 2, "orientation": "V", "cursor": A_SCREEN}
        eng._enqueue_trigger(ts, low_priority=True, source=main_mod.TRIG_HOLD,
                             cursor_xy=B_SCREEN, session_id=sid)
        dropped_locked = (eng.triggers.qsize() == q_after_unlocked)

        # B3) 无会话（session=0，纯音频）→ 保持旧行为：队列忙即丢
        eng._enqueue_trigger(ts, low_priority=True, source=main_mod.TRIG_RELEASE,
                             cursor_xy=B_SCREEN, session_id=main_mod.SESSION_NONE)
        dropped_no_session = (eng.triggers.qsize() == q_after_unlocked)

        show(f"未锁定 + 队列忙 → 入队 {base} → {q_after_unlocked}（放行={admitted_unlocked}）")
        show(f"已锁定 + 队列忙 → 仍 {eng.triggers.qsize()}（丢弃={dropped_locked}）")
        show(f"无会话 + 队列忙 → 仍 {eng.triggers.qsize()}（丢弃={dropped_no_session}）")
        ok = admitted_unlocked and dropped_locked and dropped_no_session
        return ok, (f"未锁定放行={admitted_unlocked} / 已锁定丢弃={dropped_locked} / "
                    f"无会话丢弃={dropped_no_session}")
    finally:
        eng.shutdown()


def main() -> int:
    print("=" * 104)
    print("快拖回归：按下沿必产生触发 + 低优先级准入")
    print("=" * 104)
    results = []
    section("A. 4 组快速按下（--min-interval 默认 0.9s）")
    ok, detail = case_fast_presses_all_accepted()
    results.append(check("A 每次按下都产生触发（不受音频节流吞掉）", ok, detail))
    section("B. low_priority 准入（未锁定 vs 已锁定 vs 无会话）")
    ok, detail = case_low_priority_admission()
    results.append(check("B 未锁定放行 / 已锁定丢弃 / 无会话丢弃", ok, detail))
    print()
    print("=" * 104)
    print(f"汇总: 通过 {sum(1 for r in results if r)}/{len(results)}")
    print("结果:", "ALL PASSED" if all(results) else "HAS FAILURES")
    print("=" * 104)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())