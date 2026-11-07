"""Public source-only exit logging; no licence, guard or pack-decryption code."""
from __future__ import annotations
import os
import sys
import time
from pathlib import Path
_exit_written = False

def _write(text):
    try:
        target = Path(sys.argv[0]).resolve().parent / "logs"
        target.mkdir(parents=True, exist_ok=True)
        with (target / "exit_trace.log").open("a", encoding="utf-8") as stream:
            stream.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
    except OSError:
        pass

def install(tag="main", argv=None):
    _write(f"=== start pid={os.getpid()} tag={tag} exe={sys.argv[0]} ===")

def _hard_exit(code=0, reason="source main done"):
    global _exit_written
    if not _exit_written:
        _write(f"=== exit reason={reason} code={code} ===")
        _exit_written = True
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    os._exit(int(code) & 0xff)
