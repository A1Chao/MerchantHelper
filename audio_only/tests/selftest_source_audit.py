# -*- coding: utf-8 -*-
"""自测：声控版源码**没有视觉**（AST 审计，不启动程序）。

这是本版存在的理由，所以用独立一条测试守住：源码里不许出现截图/找图/OCR/图像匹配
的调用，也不许出现键鼠模拟。检查的是 AST（语法树）里的真实调用，不是字符串文本 ——
注释里写"不截屏"不会被误判，而真写了 ``ImageGrab.grab()`` 一定被抓到。
"""
from __future__ import annotations

import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

#: 本版应有的源码清单（不是这些文件的 .py 也不该在树里）
PRODUCT_SOURCES = ("main.py", "ui.py", "shapes.py", "recognition.py", "recorder.py",
                   "candidates.py", "denoise.py")
#: 绝对不许出现的模块（视觉/截屏/OCR/图像匹配/键鼠模拟/网络）
FORBIDDEN_MODULES = {
    "vision": "视觉模块",
    "PIL": "图像库（截图/像素）",
    "PIL.ImageGrab": "截屏",
    "mss": "截屏",
    "pyautogui": "截屏+键鼠模拟",
    "pyscreeze": "截屏",
    "pytesseract": "OCR",
    "easyocr": "OCR",
    "cv2": "图像匹配",
    "dxcam": "DXGI 抓屏",
    "win32gui": "窗口抓屏",
    "win32ui": "窗口抓屏",
    "win32api": "Windows API（过高权限面）",
    "keyboard": "键盘钩子/模拟",
    "pynput": "键鼠钩子/模拟",
    "mouse": "鼠标模拟",
    "requests": "联网",
    "urllib": "联网",
    "socket": "联网",
    "http": "联网",
}
#: 绝对不许出现的符号调用（键鼠模拟 / 截屏 / 钩子）
FORBIDDEN_CALLS = {
    "SendInput": "模拟输入",
    "mouse_event": "模拟鼠标",
    "keybd_event": "模拟键盘",
    "SetWindowsHookEx": "全局钩子",
    "RegisterHotKey": "注册系统热键",
    "BitBlt": "截屏（位块传送）",
    "PrintWindow": "窗口抓屏",
    "GetDC": "取屏幕设备上下文",
}
#: 允许 import 的 ctypes 用法白名单：只读键态 / 进程优先级 / 自己的令牌，以及
#: ``recorder.py``（与完整版同一份音频采集代码，未改）做 WASAPI 回环所需的 COM+MMCSS。
#: 白名单是**穷举**的：任何新出现的 ctypes 调用都必须先在这里登记，防止有人悄悄
#: 加一个 BitBlt/SendInput 混进"纯音频"版本。
ALLOWED_CTYPES_CALLS = {
    # main.py：只读 F8 键态、进程/线程优先级、判断自己是否管理员（只读自己的令牌）
    "GetAsyncKeyState", "SetPriorityClass", "GetCurrentProcess",
    "SetThreadPriority", "GetCurrentThread", "IsUserAnAdmin",
    # recorder.py：WASAPI 采集必备（COM 初始化 + MMCSS 线程优先级）
    "CoInitialize", "CoInitializeEx", "CoUninitialize",
    "CoCreateInstance", "CoTaskMemFree",
    "AvSetMmThreadCharacteristics", "AvSetMmThreadCharacteristicsW",
    "AvRevertMmThreadCharacteristics",
}

FAILED = []


def check(condition, message):
    if not condition:
        FAILED.append(message)
        print("  [FAIL] %s" % message)
    return bool(condition)


def dotted(node) -> str:
    """把 ``a.b.c`` 形式的属性链拼成字符串；拼不出返回空串。"""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def audit(path):
    with open(path, "r", encoding="utf-8") as fp:
        tree = ast.parse(fp.read(), filename=path)
    name = os.path.basename(path)
    hits = []
    for node in ast.walk(tree):
        # import X / import X.Y
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name
                top = module.split(".")[0]
                for bad, why in FORBIDDEN_MODULES.items():
                    if module == bad or top == bad.split(".")[0]:
                        hits.append((node.lineno, "import %s（%s）" % (module, why)))
        # from X import Y
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            top = module.split(".")[0]
            for bad, why in FORBIDDEN_MODULES.items():
                if module == bad or top == bad.split(".")[0]:
                    hits.append((node.lineno, "from %s import（%s）" % (module, why)))
        # 调用点
        elif isinstance(node, ast.Call):
            target = dotted(node.func)
            short = target.rsplit(".", 1)[-1]
            for bad, why in FORBIDDEN_CALLS.items():
                if short == bad:
                    hits.append((node.lineno, "调用 %s（%s）" % (target, why)))
            if short.startswith("ImageGrab") or "grab" == short:
                hits.append((node.lineno, "调用 %s（截屏）" % target))
            # ctypes.windll.*: 只允许白名单里的只读调用
            if "windll" in target and short not in ALLOWED_CTYPES_CALLS:
                hits.append((node.lineno, "ctypes 调用 %s 不在只读白名单" % target))
    return hits


def main() -> int:
    print("== 源码审计：本版没有视觉 / 没有输入模拟 / 不联网 ==")

    print("[1] 源码清单（vision.py 必须不存在）")
    for name in PRODUCT_SOURCES:
        check(os.path.isfile(os.path.join(ROOT, name)), "缺少源码 %s" % name)
    check(not os.path.isfile(os.path.join(ROOT, "vision.py")),
          "声控版目录里不该有 vision.py（视觉模块必须彻底缺席）")

    print("[2] AST 审计")
    for name in PRODUCT_SOURCES:
        path = os.path.join(ROOT, name)
        if not os.path.isfile(path):
            continue
        hits = audit(path)
        if hits:
            for lineno, what in hits:
                check(False, "%s:%d %s" % (name, lineno, what))
        else:
            print("  [OK] %-16s 无视觉/模拟输入/联网调用" % name)

    print("[3] 必须存在的只读键盘读取（F8 开始/停止）")
    with open(os.path.join(ROOT, "main.py"), "r", encoding="utf-8") as fp:
        main_tree = ast.parse(fp.read())
    names = {dotted(n.func).rsplit(".", 1)[-1]
             for n in ast.walk(main_tree) if isinstance(n, ast.Call)}
    check("GetAsyncKeyState" in names, "main.py 应当只读 F8 键态（GetAsyncKeyState）")
    check("SendInput" not in names and "keybd_event" not in names,
          "main.py 不许模拟按键")

    print("[4] 面板不接收/不显示任何画面信息")
    with open(os.path.join(ROOT, "ui.py"), "r", encoding="utf-8") as fp:
        ui_text = fp.read()
    check("QPixmap(path)" in ui_text, "疑似只应保留缩略图/收款码的 QPixmap 读取")
    for bad in ("screen(", "grabWindow", "grab(", "pixelColor", "toImage"):
        check(bad not in ui_text, "ui.py 出现画面相关调用: %s" % bad)

    print()
    if FAILED:
        print("== FAIL ==  %d 项不通过" % len(FAILED))
        return 1
    print("== PASS ==  审计通过：纯音频、无视觉、无输入模拟、不联网")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
