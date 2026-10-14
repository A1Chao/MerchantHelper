# -*- coding: utf-8 -*-
r"""按住期间专用采样（红块兜底）—— 对应用户场景：

    作者原话：「边框识别效果不好，几乎还是靠红块与声音识别的」
    => 红块是唯一的视觉来源，而它只在**拖动过程中**存在；
       触发是"某一刻取样"，快拖可能整段错过 → 采样线程把窗口扩成"整个按住期间"。

用例：
  H1 按住期间采到红块 → 写进手势会话 → **触发自己没判出时**由会话锁兜底启用
  H2 对照：关掉采样（--no-hold-sample）→ 同样的手势结果照旧"无"（证明 H1 的兜底来自采样）
  H3 --no-hold-sample 时不启动采样线程
  H4 采样只采信「来源=红块 且 slots>0」；边框/空结果一律不采信
  H5 未开始监测时采样线程**不抓屏**（不消耗资源、更不会误判）

离线、只读：截图/匹配器全是桩，不抓屏、不开音频设备、不联网、不模拟输入。
跑法：
  cmd /d /c "D:\PY\python.exe D:\pycharm\UA\行商助手\tests\mprobe_hold_sample.py > tests\out_mprobe_hold_sample.txt 2>&1"
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (str(ROOT), str(ROOT / "deps"), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import main as main_mod  # noqa: E402
from mprobe_session_lock import (  # noqa: E402
    EMPTY_SCREEN, Harness, check, make_engine, section, show,
)

RESULTS = []


class _Drag:
    """假 DragInfo：只需 main.py 真正读取的那些字段。"""

    def __init__(self, slots=3, orientation="V", source="红块", confidence=0.88):
        self.slots = slots
        self.orientation = orientation
        self.confidence = confidence
        self.pitch = 85.0
        self.pitch_source = "按住采样(测试桩)"
        self.reason = f"合成红块 {slots}格{orientation}"
        self.source = source
        self.highlight = (120, 120, 169, 254)
        self.highlight_clipped = False
        self.rect = (120, 120, 169, 254)


def _install_full(eng, result, source="红块", calls=None):
    """替换整屏红块通道（真实实现里的 _press_from_fullscreen）。"""

    def fake(_vision, _image_f, _cur_f, _pad, _pitch, _ignore):
        if calls is not None:
            calls.append(1)
        if result is None:
            return None, ""
        return result, source

    eng._press_from_fullscreen = fake


def case_hold_sample_covers_missed_trigger(with_hit=True):
    """H1/H2：手势锚点在空格子（触发自己判不出），看采样能否补上。"""
    h = Harness().install()
    extra = ["--poll", "0.01"]
    if not with_hit:
        extra.append("--no-hold-sample")
    eng = make_engine(extra)
    try:
        h.world["pos"] = EMPTY_SCREEN            # 光标在空格子：触发那一刻什么都判不出
        eng.set_active(True)
        _install_full(eng, _Drag(3, "V") if with_hit else None)
        import vision  # noqa: F401  （Harness 已把 capture/detect 换成桩）

        sid = eng._press_session_new(EMPTY_SCREEN)      # 按下：开手势会话
        if with_hit:
            eng._hold_sample_once(vision)               # 按住期间采样（真实运行时周期调用）
        eng.handle_trigger(time.time(), main_mod.TRIG_PRESS,
                           cursor_xy=EMPTY_SCREEN, session_id=sid)
        sess = eng._press_session_get(sid)
        locked = getattr(sess, "locked", None)
        got = None if not locked else (int(locked["slots"]), str(locked["orientation"]),
                                       str(locked["source"]))
        item = None
        try:
            item = eng.results.get_nowait()
        except Exception:
            pass
        panel = None
        if item is not None:
            panel = (item.get("slots"), item.get("source"), item.get("lock_state"))
        return got, panel, eng.stats.get("hold_samples"), eng.stats.get("hold_hits")
    finally:
        h.remove()
        eng.shutdown()


def case_no_sample_thread_when_disabled():
    """H3：--no-hold-sample → 按下不启动采样线程。"""
    eng = make_engine(["--poll", "0.01", "--no-hold-sample"])
    try:
        eng.set_active(True)
        eng._hold_sampler_start()
        alive = eng._hold_thread is not None and eng._hold_thread.is_alive()
        return (not alive), f"线程对象={eng._hold_thread!r}"
    finally:
        eng.shutdown()


def case_only_red_block_accepted():
    """H4：边框/空结果不采信；红块才采信。"""
    h = Harness().install()
    eng = make_engine(["--poll", "0.01"])
    try:
        h.world["pos"] = EMPTY_SCREEN
        eng.set_active(True)
        import vision
        sid = eng._press_session_new(EMPTY_SCREEN)
        sess = eng._press_session_get(sid)

        _install_full(eng, _Drag(2, "H", source="边框"), source="边框")
        eng._hold_sample_once(vision)
        after_border = sess.hold_vis

        _install_full(eng, _Drag(0, "", source="红块"), source="红块")
        eng._hold_sample_once(vision)
        after_zero = sess.hold_vis

        _install_full(eng, _Drag(4, "正"), source="红块")
        eng._hold_sample_once(vision)
        after_red = None if sess.hold_vis is None else (sess.hold_vis["slots"],
                                                       sess.hold_vis["source"])
        ok = (after_border is None and after_zero is None and after_red == (4, "红块"))
        return ok, (f"边框→{after_border}；slots=0→{after_zero}；红块→{after_red}")
    finally:
        h.remove()
        eng.shutdown()


def case_no_capture_when_not_monitoring():
    """H5：未开始监测时，采样线程一次都不抓屏。"""
    h = Harness().install()
    eng = make_engine(["--poll", "0.01", "--hold-sample-rate", "0.05"])
    calls = []
    try:
        h.world["pos"] = EMPTY_SCREEN
        _install_full(eng, _Drag(2, "V"), calls=calls)
        eng.active.clear()                      # 未开始监测
        import vision
        sid = eng._press_session_new(EMPTY_SCREEN)
        eng._hold_sampler_start()
        time.sleep(0.35)                        # 足够跑 6 轮（若没被拦住）
        eng._hold_sampler_stop()
        sess = eng._press_session_get(sid)
        return (len(calls) == 0 and sess.hold_vis is None), \
            f"未监测期间整屏通道调用 {len(calls)} 次（应为 0）"
    finally:
        h.remove()
        eng.shutdown()


def main() -> int:
    print("=" * 104)
    print("按住期间专用采样（红块兜底）")
    print("=" * 104)

    section("H1 采样命中 → 触发没判出时由会话锁兜底启用")
    got, panel, samples, hits = case_hold_sample_covers_missed_trigger(True)
    show(f"会话锁定值={got}  面板结果={panel}  采样次数={samples} 命中={hits}")
    RESULTS.append(check("H1 采样补上了触发错过的红块", got == (3, "V", "红块")
                         and panel is not None and panel[0] == 3 and panel[1] == "红块",
                         f"锁定={got} 面板={panel}"))

    section("H2 对照：关掉采样 → 同样手势结果照旧「无」")
    got2, panel2, _, _ = case_hold_sample_covers_missed_trigger(False)
    show(f"会话锁定值={got2}  面板结果={panel2}")
    RESULTS.append(check("H2 没有采样时不给格数（证明 H1 来自采样兜底）",
                         got2 is None and (panel2 is None or not panel2[0]),
                         f"锁定={got2} 面板={panel2}"))

    section("H3 --no-hold-sample 不启动线程")
    ok3, d3 = case_no_sample_thread_when_disabled()
    show(d3)
    RESULTS.append(check("H3 关闭时不启动采样线程", ok3, d3))

    section("H4 只采信「来源=红块 且 slots>0」")
    ok4, d4 = case_only_red_block_accepted()
    show(d4)
    RESULTS.append(check("H4 边框/空结果不采信", ok4, d4))

    section("H5 未监测时不抓屏")
    ok5, d5 = case_no_capture_when_not_monitoring()
    show(d5)
    RESULTS.append(check("H5 未开始监测时零抓屏", ok5, d5))

    print()
    print("=" * 104)
    print(f"汇总: 通过 {sum(1 for r in RESULTS if r)}/{len(RESULTS)}")
    print("结果:", "ALL PASSED" if all(RESULTS) else "HAS FAILURES")
    print("=" * 104)
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())