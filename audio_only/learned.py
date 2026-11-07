"""学习样本库：把每次真正送识别的窗口暂存下来，用户标一次"对/错"就进模板库。

为什么需要它：竞品（听大金）每组有 **16 条左右真实片段**，我们每组只有 1 条合成素材
（实测差距就在这里，见 `行商助手/docs/竞品-听大金-识别管线.md` §七）。人工录 300 条
不现实，但"每次识别都留一份、按一个键标注"几乎零成本，攒几天就能追平。

目录布局（默认在程序目录下 `learned/`，可用 `--learned-dir` 改）：

    learned/
        pending/last.wav        每次识别后覆盖为"刚刚那一窗"
        pending/last.json       该窗的元信息（判定结果/分数/时间）
        positive/<key>/<ts>.wav 标注为"对"的样本 → 会进模板库
        negative/<key>/<ts>.wav 标注为"错"的样本 → 只留档，不进模板库
        log.jsonl               每次标注一行（含当时判定、分数、路径）

设计约束：
  * 只有"开始监测"期间才会 stash（停止监测时不留任何文件）；
  * pending 只保留最近一份，磁盘占用恒定；
  * 进模板库的样本每类有上限（默认 12 条），超了按时间淘汰最旧的。
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import numpy as np


MAX_PER_KEY = 12

WAV_RATE = 16000


def _write_wav(path: Path, samples: np.ndarray, rate: int = WAV_RATE) -> None:
    """写 16-bit PCM wav（float32 输入，按峰值归一化到 -1.5dB 防削波）。"""
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    if peak > 0:
        x = x / peak * 0.84
    pcm = np.clip(x * 32767.0, -32768, 32767).astype(np.int16)
    import wave

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(pcm.tobytes())


class LearnedStore:
    """暂存窗口 + 标注 + 列出可进模板库的样本。"""

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.pending_dir = self.root / "pending"
        self.positive_dir = self.root / "positive"
        self.negative_dir = self.root / "negative"
        self.log_path = self.root / "log.jsonl"
        self._pending: tuple[np.ndarray, int, dict] | None = None
        self.last_mark = ""
        self.marked_total = 0


    def stash(self, samples, key: str | None, score: int, path: str = "",
              reason: str = "") -> str:
        """保存"刚刚送识别的那一窗"；返回 uid（面板反馈用）。"""
        x = np.asarray(samples, dtype=np.float32).reshape(-1)
        if len(x) < 128:
            return ""
        meta = {
            "uid": f"{int(time.time() * 1000)}",
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "key": key or "",
            "score": int(score),
            "path": path,
            "reason": (reason or "")[:120],
        }
        self._pending = (x, WAV_RATE, meta)
        try:
            self.pending_dir.mkdir(parents=True, exist_ok=True)
            _write_wav(self.pending_dir / "last.wav", x)
            (self.pending_dir / "last.json").write_text(
                json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        return meta["uid"]


    def mark(self, correct: bool) -> str:
        """把当前 pending 样本标为"对/错"；返回给用户看的一句话。"""
        if self._pending is None:
            self.last_mark = "还没有可标注的样本（先拖一次物品）"
            return self.last_mark
        x, rate, meta = self._pending
        key = meta.get("key") or "unknown"

        stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{meta.get('uid', '0')[-7:]}"
        target = (self.positive_dir if correct else self.negative_dir) / key / f"{stamp}.wav"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_wav(target, x, rate)
        except Exception as exc:
            self.last_mark = f"标注失败：{exc}"
            return self.last_mark
        record = dict(meta, correct=bool(correct), file=str(target))
        try:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass
        self._pending = None
        self.marked_total += 1
        if correct:
            trimmed = self._trim_key(key)
            tail = f"，该组已有 {trimmed} 条" if trimmed else ""
            self.last_mark = f"已存为「{key}」的正确样本{tail}"
        else:
            self.last_mark = f"已记为「{key}」的错误样本（不进模板库）"
        return self.last_mark

    def _trim_key(self, key: str) -> int:
        """每类超过上限就删最旧的；返回该组现有条数。"""
        files = sorted((self.positive_dir / key).glob("*.wav"))
        for old in files[:-MAX_PER_KEY] if len(files) > MAX_PER_KEY else []:
            try:
                old.unlink()
            except Exception:
                pass
        return len(sorted((self.positive_dir / key).glob("*.wav")))

    def has_pending(self) -> bool:
        return self._pending is not None


    def positive_files(self) -> list[tuple[str, Path]]:
        """[(key, wav路径)]，只含"标注为对"的样本，每类最多 MAX_PER_KEY 条。"""
        out: list[tuple[str, Path]] = []
        if not self.positive_dir.is_dir():
            return out
        for key_dir in sorted(self.positive_dir.iterdir()):
            if not key_dir.is_dir():
                continue
            files = sorted(key_dir.glob("*.wav"))[-MAX_PER_KEY:]
            for f in files:
                out.append((key_dir.name, f))
        return out

    def stats(self) -> dict:
        pos = sum(1 for _ in self.positive_dir.rglob("*.wav")) if self.positive_dir.is_dir() else 0
        neg = sum(1 for _ in self.negative_dir.rglob("*.wav")) if self.negative_dir.is_dir() else 0
        keys = len([d for d in self.positive_dir.iterdir() if d.is_dir()])\
            if self.positive_dir.is_dir() else 0
        return {"positive": pos, "negative": neg, "keys": keys}
