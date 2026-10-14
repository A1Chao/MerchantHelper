# -*- coding: utf-8 -*-
"""行商助手 · 候选引擎（纯数据逻辑）

职责（接口契约 §2 / §3.4）：
    * 读取 ``assets/database.json``，为 ``items`` 与 ``special_items`` 建立 ``name -> 行`` 索引；
    * 把 ``recognition.SoundMatcher.match()`` 返回的 ``MatchRow`` 展开成候选 ``Candidate``；
    * 同物品跨声音组去重（保留最高分）、按 ``(score 降序, reference_value 降序)`` 排序；
    * ``slots > 0`` 时槽位匹配的候选排前，不匹配的**保留**但排后并加 ``note``；
    * ``strict_shape=True`` 时把不匹配格数/朝向的候选**丢弃**（玩家面向默认，见 ``resolve``）；
    * 品质 / 类别 / 朝向 / 最低分 筛选；
    * 单次会话内存历史。

边界：本模块是**纯数据逻辑**，不含任何 GUI 依赖、不读游戏内存、不联网、不做任何输入模拟。
"""

from __future__ import annotations

import io
import json
import os
import re
import time

__all__ = [
    "CandidateEngine",
    "DEFAULT_DATABASE",
    "IMAGES_DIR",
    "ASSETS_DIR",
    "parse_reference_value",
    "normalize_orientation",
]

#: 模块所在目录（= 项目根）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
IMAGES_DIR = os.path.join(ASSETS_DIR, "images")
DEFAULT_DATABASE = os.path.join(ASSETS_DIR, "database.json")

#: DB 里的朝向是中文单字；vision.DragInfo 用 "H"/"V"/""。两种写法都接受。
ORIENT_ALIASES = {
    "H": "横", "h": "横", "横": "横", "horizontal": "横",
    "V": "竖", "v": "竖", "竖": "竖", "vertical": "竖",
    "": "正", "正": "正", "normal": "正", "N": "正", "n": "正",
}

#: 中文朝向 -> vision 代码
ORIENT_TO_CODE = {"横": "H", "竖": "V", "正": ""}

QUALITY_COLORS = {"金色": "#d4af37", "红色": "#d9534f", "紫色": "#a06cd5"}

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def normalize_orientation(value) -> str:
    """把 ``H`` / ``V`` / ``横`` / ``竖`` / ``正`` / ``""`` 归一化成 DB 里的中文朝向。

    未知取值原样返回（不做猜测），方便调用方发现脏数据。
    """
    if value is None:
        return ""
    text = str(value).strip()
    if text in ORIENT_ALIASES:
        return ORIENT_ALIASES[text]
    return text


def parse_reference_value(raw):
    """解析 DB 的 ``reference_value``。

    DB 里它是字符串，绝大多数是纯数字（如 ``"1550000"``），但存在区间/带备注的行，
    例如 ``天线`` 的 ``"90000-200000（市场溢价严重）"``。

    返回 ``(number, raw_text)``：``number`` 是 ``int`` / ``float`` / ``None``
    （契约允许 ``None``，排序时视为最低值），``raw_text`` 保留原始串供 UI 显示。
    """
    if raw is None:
        return None, ""
    if isinstance(raw, bool):
        return None, str(raw)
    if isinstance(raw, (int, float)):
        value = float(raw)
        return (int(value) if value.is_integer() else value), str(raw)

    text = str(raw).strip()
    if not text:
        return None, ""

    cleaned = text.replace(",", "").replace("，", "")
    try:
        value = float(cleaned)
        return (int(value) if value.is_integer() else value), text
    except ValueError:
        pass

    match = _NUMBER_RE.search(cleaned)
    if not match:
        return None, text
    value = float(match.group(0))
    return (int(value) if value.is_integer() else value), text


def _to_int(value, default=0) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


class CandidateEngine:
    """候选引擎：DB 索引 + 展开/去重/排序 + 筛选 + 会话历史。"""

    def __init__(self, database_path=None):
        self.database_path = str(database_path) if database_path else DEFAULT_DATABASE
        self.images_dir = IMAGES_DIR
        self.sounds = []
        self.items = []
        self.special_items = []
        #: name -> [行, ...]（items 与 special_items 都在里面；实测名字唯一，仍用列表容错）
        self.name_index = {}
        #: alias，方便别的模块按契约用语访问
        self.index = self.name_index
        self.duplicate_names = []
        self.errors = []
        self.history = []
        self.max_history = 50
        self.last_stats = {}
        self._load()

    # ------------------------------------------------------------------ 加载
    def _load(self):
        if not os.path.exists(self.database_path):
            raise FileNotFoundError("数据库不存在: %s" % self.database_path)

        with io.open(self.database_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)

        if not isinstance(data, dict):
            raise ValueError("数据库根节点不是对象: %s" % self.database_path)

        self.sounds = list(data.get("sounds") or [])
        self.items = [r for r in (data.get("items") or []) if isinstance(r, dict)]
        self.special_items = [r for r in (data.get("special_items") or []) if isinstance(r, dict)]

        for row in self.items + self.special_items:
            name = str(row.get("name") or "").strip()
            if not name:
                self.errors.append("DB 行缺少 name: %r" % (row,))
                continue
            self.name_index.setdefault(name, []).append(row)

        self.duplicate_names = sorted(n for n, rows in self.name_index.items() if len(rows) > 1)

        # 取值集合（UI 下拉用）
        self.qualities = self._distinct_field("quality")
        self.categories = self._distinct_field("category")
        self.orientations = self._distinct_field("orientation")
        self.slots = sorted({_to_int(r.get("slot")) for r in self.items + self.special_items})

        # 预解析参考价值：name -> (number, raw_text)
        self.reference_values = {}
        for name, rows in self.name_index.items():
            self.reference_values[name] = parse_reference_value(rows[0].get("reference_value"))
        return self

    def _distinct_field(self, field):
        values = []
        for row in self.items + self.special_items:
            value = row.get(field)
            if value is None:
                continue
            value = str(value).strip()
            if value and value not in values:
                values.append(value)
        return values

    # -------------------------------------------------------------- 基础查询
    @property
    def rows(self):
        """items + special_items 的全部 DB 行。"""
        return self.items + self.special_items

    @property
    def row_count(self):
        return len(self.items) + len(self.special_items)

    def lookup(self, name):
        """按名字取 DB 行（先 items 后 special_items，与索引顺序一致）。"""
        rows = self.name_index.get(str(name or "").strip())
        return rows[0] if rows else None

    def image_path(self, image_name) -> str:
        """``assets/images/<image>`` 的绝对路径。"""
        return os.path.join(self.images_dir, str(image_name or ""))

    # ---------------------------------------------------------------- 展开
    def _row_for_item(self, item):
        """把一个 ``match_row["items"]`` 元素解析成权威 DB 行。

        recognition 契约里 items 已经是 DB 行；这里仍以本地 DB 索引为准
        （索引里没有该名字时才退回调用方给的 dict），保证 slot/quality 等字段可信。
        """
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            known = self.lookup(name) if name else None
            return known if known is not None else (item if name else None)
        if isinstance(item, str):
            return self.lookup(item)
        return None

    def _build_candidate(self, row, sound_key, score, reason):
        name = str(row.get("name") or "").strip()
        if not name:
            return None

        ref_value, ref_text = self.reference_values.get(
            name, parse_reference_value(row.get("reference_value"))
        )
        orientation = str(row.get("orientation") or "").strip()
        return {
            "name": name,
            "quality": str(row.get("quality") or "").strip(),
            "category": str(row.get("category") or "").strip(),
            "slot": _to_int(row.get("slot")),
            "orientation": orientation,
            "orientation_code": ORIENT_TO_CODE.get(orientation, ""),
            "reference_value": ref_value,
            "reference_value_text": ref_text or (
                "" if ref_value is None else str(ref_value)
            ),
            "image_name": str(row.get("image") or ""),
            "image_path": self.image_path(row.get("image")),
            "score": score,
            "sound_key": sound_key,
            "sound_type": str(row.get("sound_type") or "").strip(),
            "special": bool(row.get("special")),
            "reason": reason,
        }

    def resolve(self, match_rows, slots=0, orientation="", strict_shape=False) -> list:
        """把 ``match_rows`` 展开成候选列表。

        * 每个 match_row 的 ``items`` 逐行展开；
        * 同物品跨声音组去重，保留最高分（并列保留先出现的）；
        * ``slots > 0`` 时 ``slot == slots`` 的候选排在最前，其余保留但排后并加 ``note``；
        * ``orientation`` 非空时同样只做**软优先**（匹配的排前 + 不匹配的加 note），不丢弃；
        * 组内排序：``score`` 降序，其次 ``reference_value`` 降序。

        ``strict_shape=True``（**玩家面向的默认**，由 ``ui.Panel`` / ``--show-mismatch`` 决定）：
        ``slots > 0`` 时**丢弃** ``slot_match is False`` 的候选；``orientation`` 非空时同时
        丢弃 ``orientation_match is False`` 的候选。**绝不回退**：丢掉就是丢掉，过滤后为空
        由 ``last_stats["empty_after_shape"]`` 告知调用方（UI 显示"无与 X 相符的候选"，
        不显示不匹配项）。``strict_shape=False``（默认值）与旧行为**逐字一致**。

        为什么需要：一个音效键常对应**多个物品（同音组）**，纯音频分不出来；视觉判出的
        格数/朝向是唯一能把它们分开的信息 —— 只显示相符的候选，用户才不会看到
        "格数 2格V 的结果里混着 1格(H) 的物品"。
        """
        match_rows = list(match_rows or [])
        want_slots = _to_int(slots)
        want_orient = normalize_orientation(orientation) if str(orientation or "").strip() else ""

        raw_candidates = []
        skipped_rows = 0
        placeholder_rows = 0

        for match_row in match_rows:
            if not isinstance(match_row, dict):
                skipped_rows += 1
                continue
            sound_key = str(match_row.get("key") or "")
            reason = str(match_row.get("reason") or "")
            score = _to_int(match_row.get("score"), 0)
            if not match_row.get("score") and match_row.get("score_raw") is not None:
                score = _to_int(float(match_row.get("score_raw")) * 100.0, 0)
            score = max(0, min(100, score))

            items = match_row.get("items") or []
            if isinstance(items, dict):
                items = [items]
            for item in items:
                row = self._row_for_item(item)
                if row is None:
                    placeholder_rows += 1  # normal 组的占位行 / 未收录物品
                    continue
                candidate = self._build_candidate(row, sound_key, score, reason)
                if candidate is not None:
                    raw_candidates.append(candidate)

        # --- 去重：同物品跨声音组保留最高分 ---
        best = {}
        deduped_count = 0
        for candidate in raw_candidates:
            name = candidate["name"]
            current = best.get(name)
            if current is None:
                best[name] = candidate
            else:
                deduped_count += 1
                if candidate["score"] > current["score"]:
                    best[name] = candidate

        # --- 槽位 / 朝向软优先 + 排序 ---
        candidates = []
        for candidate in best.values():
            slot_match = True
            orientation_match = True
            notes = []

            if want_slots > 0:
                slot_match = candidate["slot"] == want_slots
                if not slot_match:
                    notes.append("槽位不匹配（期望 %d 格，实际 %d 格）" % (want_slots, candidate["slot"]))

            if want_orient:
                item_orient = normalize_orientation(candidate["orientation"])
                orientation_match = item_orient == want_orient
                if not orientation_match:
                    notes.append("朝向不匹配（期望 %s，实际 %s）" % (want_orient, item_orient or "?"))

            candidate = dict(candidate)
            candidate["slot_match"] = slot_match
            candidate["orientation_match"] = orientation_match
            candidate["note"] = "；".join(notes)
            candidates.append(candidate)

        def sort_key(candidate):
            ref = candidate.get("reference_value")
            ref_key = float(ref) if isinstance(ref, (int, float)) else -1.0
            return (
                0 if candidate["slot_match"] else 1,
                0 if candidate["orientation_match"] else 1,
                -candidate["score"],
                -ref_key,
                candidate["name"],
            )

        candidates.sort(key=sort_key)

        # --- 统计「原始解析结果」：**必须在严格丢弃之前**算，丢弃不影响这两个计数 ----
        pre_slot_matched = sum(1 for c in candidates if c["slot_match"])
        pre_slot_mismatched = sum(1 for c in candidates if not c["slot_match"])
        pre_orient_matched = sum(1 for c in candidates if c["orientation_match"])

        # --- 严格形状过滤（玩家面向默认；只决定"显示什么"，不改匹配优先的相对顺序）---
        dropped_names = []
        if strict_shape:
            kept = []
            for candidate in candidates:
                mismatch = ((want_slots > 0 and not candidate["slot_match"])
                            or (bool(want_orient) and not candidate["orientation_match"]))
                if mismatch:
                    # 供日志：名字(格数朝向,分数)，例 液压扳手(1格H,45)
                    code = ORIENT_TO_CODE.get(
                        normalize_orientation(candidate["orientation"]), "") or "正"
                    dropped_names.append(
                        "%s(%d格%s,%d)" % (candidate["name"], candidate["slot"], code,
                                           candidate["score"]))
                else:
                    kept.append(candidate)
            candidates = kept

        self.last_stats = {
            "match_rows": len(match_rows),
            "raw_candidates": len(raw_candidates),
            "deduped": deduped_count,
            "candidates": len(candidates),
            "slot_matched": pre_slot_matched,          # 原始解析结果（不受严格丢弃影响）
            "slot_mismatched": pre_slot_mismatched,    # 原始解析结果（不受严格丢弃影响）
            "orientation_matched": pre_orient_matched,
            "placeholder_rows": placeholder_rows,
            "skipped_rows": skipped_rows,
            "slots": want_slots,
            "orientation": want_orient,
            # ---- 严格形状过滤（task-16）----
            "strict_shape": bool(strict_shape),
            "dropped_shape": len(dropped_names),       # 丢弃条数
            "dropped_names": dropped_names,            # 被丢候选：名字(格数朝向,分数)，供日志
            # 严格过滤**丢掉了候选**且过滤后为空（= 有声匹配但形状都不符）→ UI 明确文案
            "empty_after_shape": bool(dropped_names) and not candidates,
        }
        return candidates

    # ---------------------------------------------------------------- 筛选
    def filter(self, candidates, quality=None, category=None, orientation=None, min_score=0) -> list:
        """按品质 / 类别 / 朝向 / 最低分筛选（``None`` 或空串表示不筛）。"""
        result = []
        want_quality = str(quality).strip() if quality not in (None, "") else ""
        want_category = str(category).strip() if category not in (None, "") else ""
        want_orientation = (
            normalize_orientation(orientation) if orientation not in (None, "") else ""
        )
        try:
            min_score = _to_int(min_score, 0)
        except (TypeError, ValueError):
            min_score = 0

        for candidate in candidates or []:
            if not isinstance(candidate, dict):
                continue
            if want_quality and str(candidate.get("quality") or "").strip() != want_quality:
                continue
            if want_category and str(candidate.get("category") or "").strip() != want_category:
                continue
            if want_orientation:
                item_orient = normalize_orientation(candidate.get("orientation"))
                if item_orient != want_orientation:
                    continue
            if _to_int(candidate.get("score"), 0) < min_score:
                continue
            result.append(candidate)
        return result

    def distinct(self, candidates, field) -> list:
        """候选集合里某字段的去重取值（保持出现顺序），供 UI 下拉使用。"""
        values = []
        for candidate in candidates or []:
            if not isinstance(candidate, dict):
                continue
            value = candidate.get(field)
            if value is None:
                continue
            value = str(value).strip()
            if value and value not in values:
                values.append(value)
        return values

    # ---------------------------------------------------------------- 历史
    def history_add(self, candidates, meta=None):
        """把一次识别结果记入**单次会话内存历史**（不写盘）。"""
        items = [dict(c) for c in (candidates or []) if isinstance(c, dict)]
        record = {
            "time": time.strftime("%H:%M:%S"),
            "timestamp": time.time(),
            "count": len(items),
            "items": items,
        }
        if isinstance(meta, dict):
            record.update(meta)
        self.history.append(record)
        if len(self.history) > self.max_history:
            del self.history[: len(self.history) - self.max_history]
        return record

    def history_clear(self):
        self.history = []
        return self.history

    def history_text(self) -> str:
        """历史的多行文本摘要（UI「历史」按钮用）。"""
        if not self.history:
            return "（暂无历史）"
        lines = []
        for index, record in enumerate(reversed(self.history), 1):
            names = "、".join(c.get("name", "") for c in record.get("items", [])[:5])
            more = "" if record.get("count", 0) <= 5 else " 等"
            lines.append(
                "#%d %s  候选 %d 条  %s%s"
                % (index, record.get("time", ""), record.get("count", 0), names, more)
            )
        return "\n".join(lines)