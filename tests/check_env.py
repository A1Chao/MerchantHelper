"""环境体检：报告解释器版本与行商助手所需依赖是否齐全（只读，不安装任何东西）。"""
import importlib
import sys

print(f"python   : {sys.version.split()[0]}  ({sys.executable})")

MODS = ["numpy", "scipy", "cv2", "PIL", "PyQt5", "soundfile", "soundcard", "comtypes", "PyInstaller"]
ok, missing = [], []
for name in MODS:
    try:
        m = importlib.import_module(name)
        ver = getattr(m, "__version__", "")
        if not ver and name == "cv2":
            ver = getattr(m, "version", "")
        ok.append(f"{name}={ver or 'ok'}")
    except Exception as exc:
        missing.append(f"{name}({type(exc).__name__})")
print(f"可用: {' '.join(ok) if ok else '（无）'}")
print(f"缺失: {' '.join(missing) if missing else '（无）'}")
print(f"结论: {'齐全' if not missing else '缺 ' + str(len(missing)) + ' 项'}")