r"""一键诊断：确认"游戏内拖动完全没反应"到底卡在哪一步。

设计原则：**只读**。只用 GetAsyncKeyState / GetCursorPos / GetForegroundWindow /
GetWindowThreadProcessId / GetWindowTextW / IsUserAnAdmin / ImageGrab /
shell32.IsUserAnAdmin —— 不做任何输入模拟、不读进程内存、不联网（静态审计可验证）。

用法（游戏开着、光标停在游戏里时运行）：
  cmd /d /c "D:\PY\python.exe main.py --diag > diag.txt 2>&1"

它会依次判定：
  0) 我们的进程是不是管理员
  1) 当前前台窗口是谁（标题/PID/可见性）
  2) **按键能不能被看到**：请你配合按几次左键（最关键的一步）
  3) 屏幕能不能截到（独占全屏/权限会导致全黑）
  4) 不走鼠标，直接强制触发一次完整识别，验证音频+视觉+候选链路
最后给出结论与对应处置。
"""

from __future__ import annotations

import ctypes
import time

import numpy as np

_user32 = ctypes.windll.user32
_shell32 = ctypes.windll.shell32
VK_LBUTTON = 0x01


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _left_down() -> bool:
    return bool(_user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)


def _cursor() -> tuple[int, int]:
    pt = _POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def is_admin() -> bool:
    try:
        return bool(_shell32.IsUserAnAdmin())
    except Exception:
        return False


def foreground_window() -> dict:
    hwnd = int(_user32.GetForegroundWindow() or 0)
    pid = ctypes.c_ulong(0)
    if hwnd:
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    buf = ctypes.create_unicode_buffer(512)
    if hwnd:
        _user32.GetWindowTextW(hwnd, buf, 512)
    return {
        "hwnd": hwnd,
        "pid": int(pid.value),
        "title": buf.value or "(无标题)",
        "visible": bool(_user32.IsWindowVisible(hwnd)) if hwnd else False,
    }


def run_diag(args=None) -> int:
    print("=" * 72)
    print("行商助手 · 一键诊断（只读，不模拟任何输入）")
    print("=" * 72)

    # ---- 0. 权限 ----
    admin = is_admin()
    print(f"\n[0] 本工具权限: {'管理员 ✅' if admin else '普通用户 ⚠️'}")
    print("    为什么重要：Windows 的 UIPI 规定——**低权限进程看不到高权限进程的输入**。")
    print("    若游戏以管理员/反作弊高完整性运行，普通权限的本工具会：")
    print("      · GetAsyncKeyState 永远读不到左键 → 触发不了（面板毫无反应）")
    print("      · 截图可能全黑、悬浮窗可能被盖住")

    # ---- 1. 前台窗口 ----
    fg = foreground_window()
    print(f"\n[1] 当前前台窗口: {fg['title']!r}  pid={fg['pid']}  可见={fg['visible']}")
    import os as _os

    looks_like_tool = (fg["pid"] == _os.getpid()
                       or any(k in fg["title"] for k in ("main.py", "PowerShell", "cmd.exe",
                                                         "python", "PyCharm", "Code", "Edge",
                                                         "Chrome")))
    if looks_like_tool:
        print("    ⚠️⚠️ 前台**不是游戏**（看起来是终端/编辑器/浏览器）——"
              "第 2、3 步测的是桌面，结论不能代表游戏内！")
        print("        请：切到游戏 → 光标停在游戏里 → 让游戏保持前台 → 重新运行本诊断。")
    else:
        print("    （让游戏保持前台，第 3 步才截得到游戏画面）")
    cx, cy = _cursor()
    print(f"    当前光标: ({cx}, {cy})")

    # ---- 2. 按键检测（关键） ----
    print("\n[2] 按键检测：请在 5 秒内，**在游戏里**按下并松开左键 3 次…")
    seen, transitions, polls = 0, 0, 0
    was = _left_down()
    t0 = time.time()
    last_report = 0.0
    while time.time() - t0 < 5.0:
        d = _left_down()
        polls += 1
        if d:
            seen += 1
            if not was:
                transitions += 1
        was = d
        el = time.time() - t0
        if el - last_report >= 1.0:
            last_report = el
            print(f"    …已过 {el:.0f}s：采样 {polls} 次，看到按下 {seen} 次，"
                  f"上升沿 {transitions} 次", flush=True)
        time.sleep(args.poll if args is not None and getattr(args, "poll", None) else 0.012)
    key_ok = transitions > 0
    print(f"    结果：采样 {polls} 次｜按下采样 {seen}｜按下-松开次数 {transitions}")
    if key_ok:
        print("    ✅ 左键能被本工具看到 → 触发链路可用（问题不在按键检测）")
    else:
        print("    ❌ 完全看不到左键！这就是『没反应』的直接原因。"
              "最常见：游戏/反作弊以更高权限运行（UIPI 拦截）")

    # ---- 3. 截图 ----
    print("\n[3] 屏幕截图测试（光标附近 pad=200）…")
    cap_ok, cap_info = False, "未执行"
    try:
        import vision

        img, origin = vision.capture_cursor_region(pad=200)
        arr = np.asarray(img)
        if arr.ndim == 3:
            gray = arr[..., :3].mean(axis=2)
        else:
            gray = arr.astype(float)
        mean, std = float(gray.mean()), float(gray.std())
        dark_ratio = float((gray < 8).mean())
        uniq = int(len(np.unique((gray // 8).astype(np.uint8))))
        cap_info = (f"尺寸={arr.shape} 平均亮度={mean:.1f} 标准差={std:.1f} "
                    f"纯黑占比={dark_ratio * 100:.1f}% 灰度级数={uniq}")
        print(f"    {cap_info}  origin={origin}")
        cap_ok = dark_ratio < 0.95 and std > 3.0
        if cap_ok:
            print("    ✅ 能截到画面（不是全黑）")
        else:
            print("    ❌ 截图几乎全黑：常见于游戏**独占全屏**或权限不足（UIPI/DRM 保护）")
        try:
            info = vision.detect_drag_slots(img, (cx - origin[0], cy - origin[1]))
            print(f"    占格识别: slots={info.slots} 朝向={info.orientation!r} "
                  f"置信度={info.confidence} 高亮={info.highlight}")
            print(f"    原因: {info.reason}")
        except Exception as exc:
            print(f"    占格识别异常: {type(exc).__name__}: {exc}")
    except Exception as exc:
        print(f"    ❌ 截图失败: {type(exc).__name__}: {exc}")

    # ---- 4. 不走鼠标，强制触发一次完整识别 ----
    print("\n[4] 强制触发一次完整识别（不需要鼠标，验证音频/候选链路）…")
    audio_ok, audio_info = False, "未执行"
    try:
        import main as app

        a = args if args is not None else app.parse_args(["--limit", "5", "--verbose"])
        engine = app.Engine(a)
        engine.load()
        engine.audio.start()
        t0 = time.time()
        while engine.audio.windows < 4 and time.time() - t0 < 8 and not engine.audio.last_error:
            time.sleep(0.1)
        if engine.audio.last_error:
            audio_info = f"音频错误: {engine.audio.last_error}"
            print(f"    ❌ {audio_info}")
        else:
            print(f"    音频就绪: 设备={a.device or '(默认)'} 窗口={engine.audio.windows} "
                  f"峰值RMS={engine.audio.peak_rms:.5f} 丢帧={engine.audio.discontinuities}")
            if engine.audio.peak_rms < 0.0004:
                print("    ⚠️ 回环音频几乎没有信号：游戏声音可能从别的设备播放（--list-devices）")
            # 通路带宽：判断蓝牙免手持/音频增强是否把高频砍掉了
            try:
                import recorder as _rec

                sm = engine.audio.snapshot()
                bw = (_rec.spectral_rolloff(_rec.loudest_segment(sm[0]))
                      if sm is not None else 0.0)
                print(f"    音频通路带宽 ≈ {bw / 1000:.1f} kHz（正常应 >16kHz）")
                if bw and bw < 9000:
                    print("    ❌ 带宽异常低：音效高频被砍掉，识别必然不稳定。"
                          "常见原因：① 蓝牙耳机在『免手持/HFP 通话』通路；"
                          "② 输出设备开了音频增强/低音增强/响度均衡；③ 采样率是 16kHz。"
                          "处置：换有线/Realtek 输出，游戏输出与该设备一致后重试。")
            except Exception as exc:
                print(f"    带宽检测失败: {type(exc).__name__}: {exc}")
            engine.handle_trigger(time.time())
            try:
                item = engine.results.get_nowait()
            except Exception:
                item = None
            if item is None:
                audio_info = "强制触发没有产出结果"
                print(f"    ❌ {audio_info}")
            else:
                top = item["rows"][0] if item["rows"] else None
                audio_info = (f"slots={item['slots']} rows={len(item['rows'])} "
                              f"top={top['key'] if top else '-'}/"
                              f"{top['score'] if top else 0} 原因={item['reason'][:24]}")
                print(f"    结果: {audio_info}")
                audio_ok = True
                print("    ✅ 音频→识别→候选链路可用"
                      + ("" if item["rows"] else "（当前没音效，所以没有候选是正常的）"))
        engine.shutdown()
    except Exception as exc:
        audio_info = f"{type(exc).__name__}: {exc}"
        print(f"    ❌ 强制触发异常: {audio_info}")

    # ---- 结论 ----
    print("\n" + "=" * 72)
    print("结论与处置")
    print("=" * 72)
    if not key_ok:
        print("▶ 根因：**看不到鼠标按键**（不是音频、不是视觉的问题）")
        print("  1) 用管理员身份运行本工具：右键 终端/PowerShell →『以管理员身份运行』，再执行 main.py")
        print("  2) 若仍看不到：游戏反作弊在更高完整性上拦截输入读取")
        print("     → 用音频触发模式（不需要鼠标）：main.py --trigger audio")
        print(f"  3) 我们的权限={admin}（若为普通用户，第 1 步优先级最高）")
    elif not cap_ok:
        print("▶ 根因：**能看到按键但截不到画面**（独占全屏/DRM）")
        print("  1) 游戏改为『窗口化』或『无边框窗口』，再试")
        print("  2) 用管理员身份运行；仍全黑则只能依赖音频候选（--no-vision）")
    elif not audio_ok:
        print("▶ 根因：按键与截图正常，但**音频/识别链路**有问题")
        print(f"  细节: {audio_info}")
        print("  1) main.py --list-devices 确认回环设备 = 游戏实际播放的设备")
        print("  2) main.py --device \"设备名\" 指定后重试；确认游戏里能听到物品音效")
    else:
        print("▶ 按键、截图、音频、识别四条链路都通。")
        if looks_like_tool:
            print("  ⚠️ 但本次第 2/3 步是在**桌面**上测的，不能证明游戏内可用：")
            print("     请切到游戏、让游戏前台，重新运行 --diag。")
        else:
            print("  若游戏内仍无反应，请把本文件与游戏内控制台输出一起发回，"
                  "重点看 [hb] 心跳行里的『左键 看到/受理』计数。")
    print("=" * 72)
    return 0 if (key_ok and cap_ok and audio_ok) else 2