# -*- coding: utf-8 -*-
"""一次跑完声控版 1.1.1 识别移植版的十一个自测（不需要声卡，也不需要游戏）。

    D:\\PY\\python.exe tests\\run_all.py

每个子测试都是独立进程（Qt/音频状态互不影响），任一失败则总退出码非 0。
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

TESTS = (
    ("selftest_shapes.py", "格数/朝向语义"),
    ("selftest_source_audit.py", "源码审计：无视觉 / 无输入模拟 / 不联网"),
    ("selftest_audio_compat.py", "采集兼容层：设备格式补丁 / 48k 重采样 / 错误信息"),
    ("selftest_engine_111.py", "1.1.1 判定 + 提速实现：33/33 自识别 / 静音拒绝 / 实验开关默认关"),
    ("selftest_trigger.py", "音效触发：每次拖动都要触发"),
    ("selftest_trigger_rearm.py", "音效触发：上一声还在窗口里时下一次也必须触发"),
    ("selftest_ui.py", "面板：格数图标按钮 / 空态 / 赞助"),
    ("selftest_pipeline.py", "端到端：wav → 识别 → 候选 → 格数筛选"),
    ("selftest_weak_display.py", "低置信显示：--show-weak 给出百分制候选"),
    ("selftest_noise_gate.py", "环境音门控：没响过的窗口不报物品"),
    ("selftest_stability.py", "稳定性：设备恢复 / 资源清理 / 退出 / 监测代次 / 关闭确认"),
)


def main() -> int:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    failed = []
    for filename, title in TESTS:
        print("\n" + "=" * 66)
        print("  %s   (%s)" % (filename, title))
        print("=" * 66, flush=True)
        rc = subprocess.call([sys.executable, os.path.join(HERE, filename)],
                             cwd=ROOT, env=env)
        if rc != 0:
            failed.append(filename)
    print("\n" + "=" * 66)
    if failed:
        print("== FAIL ==  %d/%d 失败: %s" % (len(failed), len(TESTS), " ".join(failed)))
        return 1
    print("== PASS ==  %d/%d 全部通过" % (len(TESTS), len(TESTS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
