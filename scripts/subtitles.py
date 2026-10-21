#!/usr/bin/env python3
"""Deterministic bilingual subtitle validation, export, and delivery packaging.

Python 3.10+, standard library only. This utility does not listen, translate,
OCR, check installed fonts, or promote review status. See references/export.md.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import unicodedata
import zipfile


SCHEMA_VERSION = 1
TOOL_VERSION = "1.03"
STATUSES = {"pending", "draft", "verified", "stale", "not_applicable"}
DIMENSIONS = ("english", "translation", "timing", "visual")
KINDS = {"dialogue", "onscreen", "lyric"}
DEFAULT_STYLE = {
    "font_en": "Arial", "font_zh": "Microsoft YaHei",
    "font_onscreen": "Microsoft YaHei", "font_size_en": 42,
    "font_size_zh": 46, "font_size_onscreen": 40, "outline": 1.5,
    "margin_bottom": 60, "color": "&H00FFFFFF",
    "dialogue_en_lines": 2, "dialogue_zh_lines": 2,
    "dialogue_line_height": 1.35, "dialogue_gap": 8,
    "dialogue_position": "bottom", "margin_top": 60,
    "dialogue_align": "center", "margin_side": 60,
}
ALLOWED_DELIVERY_SUFFIXES = {".srt", ".ass", ".md", ".txt"}
MOJIBAKE = re.compile(r"锟斤拷|鐨勬|涓|Ã[\x80-\xff]|â€|ä½|æ[\x80-\xbf]|\\u[0-9a-fA-F]{4}")
SRT_TIME = re.compile(r"^(\d{2,}):(\d{2}):(\d{2}),(\d{3}) --> (\d{2,}):(\d{2}):(\d{2}),(\d{3})$")


class SubtitleError(Exception):
    pass


def is_integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def is_number(value):
    return is_integer(value) or (isinstance(value, float) and math.isfinite(value))


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def subtitle_bytes(text):
    # Normalize once, then encode with a BOM and Windows-compatible line endings.
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\n", "\r\n").encode("utf-8-sig")


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SubtitleError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=no_duplicate_keys,
                          parse_constant=lambda value: (_ for _ in ()).throw(SubtitleError(f"Invalid JSON constant: {value}")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SubtitleError(f"Cannot read UTF-8 JSON {path}: {exc}") from exc


def safe_basename(name):
    if not isinstance(name, str) or not name or name in {".", ".."}:
        return False
    if name[-1] in ". " or re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', name):
        return False
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", name, re.I):
        return False
    return len(name.encode("utf-8", errors="replace")) <= 200


def display_text(value):
    # Empty paragraph lines cannot live inside an SRT block. Only layout spaces
    # and blank lines are normalized; original full letters remain in appendices.
    return "\n".join(line.strip() for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n") if line.strip())


def status_of(cue, dimension):
    item = cue.get("verification", {}).get(dimension, {})
    return item.get("status", "pending") if isinstance(item, dict) else "pending"


def applicable_dimensions(cue):
    return DIMENSIONS if cue.get("kind") == "onscreen" else DIMENSIONS[:3]


def evidence_present(value):
    return isinstance(value, list) and any(
        (isinstance(item, str) and bool(item.strip())) or
        (isinstance(item, dict) and bool(item)) for item in value
    )


_COVERAGE_MODULE = None


def coverage_module():
    global _COVERAGE_MODULE
    if _COVERAGE_MODULE is None:
        spec = importlib.util.spec_from_file_location("subtitle_coverage", Path(__file__).with_name("coverage.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _COVERAGE_MODULE = module
    return _COVERAGE_MODULE


def final_review_issues(records):
    """Unfinished work in the explicit task scope; call after structural lint.

    Pending windows have no resolved flag: move a repaired item out of pending
    and preserve its resolution in task history. This never verifies evidence.
    """
    issues = []
    duration = records.get("video", {}).get("duration_ms")
    cov = records.get("coverage", {})
    scope = cov.get("scope") if isinstance(cov, dict) else None
    if scope is None:
        scope = {"start_ms": 0, "end_ms": duration, "audio": True, "visual": True}
    if (not isinstance(scope, dict) or not is_integer(duration) or duration <= 0
            or not is_integer(scope.get("start_ms")) or not is_integer(scope.get("end_ms"))
            or not 0 <= scope["start_ms"] < scope["end_ms"] <= duration
            or any(type(scope.get(k)) is not bool for k in ("audio", "visual"))
            or not (scope["audio"] or scope["visual"])):
        return [{"code": "final_review_scope", "at": "coverage.scope", "message": "Cannot determine a valid final-review scope; fix structural errors first."}]

    def modality(kind):
        if kind in {"visual", "onscreen", "ocr"}:
            return {"visual"}
        if kind in {"audio", "dialogue", "lyric"}:
            return {"audio"}
        return set()

    def in_scope(item, modalities):
        if not any(scope[k] for k in modalities):
            return False
        a, b = item["start_ms"], item["end_ms"]
        if a == b:
            return scope["start_ms"] <= a < scope["end_ms"] or a == scope["end_ms"] == duration
        return max(a, scope["start_ms"]) < min(b, scope["end_ms"])

    def add(code, where, item, message):
        issues.append({"code": code, "at": where, "message": message,
                       "start_ms": item["start_ms"], "end_ms": item["end_ms"]})

    cues = records.get("cues", [])
    by_id = {cue["id"]: cue for cue in cues}
    for index, cue in enumerate(cues):
        if not cue.get("include", True) or not in_scope(cue, modality(cue["kind"])):
            continue
        where = f"cues[{index}]"
        for dimension in applicable_dimensions(cue):
            state = status_of(cue, dimension)
            if state != "verified":
                add("final_review_unverified", f"{where}.verification.{dimension}", cue,
                    f"{cue['id']}: {dimension}={state}; finish the actual review and record evidence before final delivery.")
        if cue.get("unresolved"):
            add("final_review_unresolved", where + ".unresolved", cue,
                f"{cue['id']}: unresolved notes remain; repair them and preserve the resolution before final delivery.")
    for index, item in enumerate(records.get("pending", [])):
        modalities = modality(item["kind"])
        if not modalities:
            for cid in item.get("cue_ids", []):
                modalities.update(modality(by_id[cid]["kind"]))
        if in_scope(item, modalities or {"audio", "visual"}):
            add("final_review_pending", f"pending[{index}]", item,
                f"{item['id']}: an unfinished task window remains in scope: {item['note']}")
    return issues


def lint_records(records, check_coverage=True):
    """Return structural errors and heuristic warnings without modifying records."""
    errors, warnings = [], []

    def issue(level, code, where, message):
        (errors if level == "error" else warnings).append({"code": code, "at": where, "message": message})

    def text_check(value, where, require=False):
        if not isinstance(value, str):
            issue("error", "text_type", where, "Expected a Unicode string.")
            return
        if require and not value.strip():
            issue("error", "empty_language", where, "Included subtitles require nonempty original and Chinese text; use an honest pending marker if needed.")
        if "\ufffd" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            issue("error", "broken_unicode", where, "Replacement characters or lone surrogates cannot be exported.")
        if any((ord(char) < 32 and char not in "\n\r\t") or ord(char) == 127 for char in value):
            issue("error", "control_character", where, "Unexpected control character.")
        if MOJIBAKE.search(value):
            issue("warning", "possible_mojibake", where, "Possible encoding damage or literal Unicode escape; inspect the source rather than blindly recoding.")
        if re.search(r"\?{3,}", value):
            issue("warning", "question_marks", where, "Repeated question marks may indicate lost characters or an unresolved passage.")

    if not isinstance(records, dict):
        return {"tool_version": TOOL_VERSION, "errors": [{"code": "root_type", "at": "$", "message": "Expected an object."}], "warnings": [], "counts": {}}
    if records.get("schema_version") != SCHEMA_VERSION or not is_integer(records.get("schema_version")):
        issue("error", "schema_version", "schema_version", "Supported schema_version is integer 1.")
    video = records.get("video")
    if not isinstance(video, dict):
        issue("error", "video_type", "video", "Expected video metadata object.")
        video = {}
    if not safe_basename(video.get("basename")):
        issue("error", "unsafe_basename", "video.basename", "Use a safe video stem without separators, reserved device names, trailing dots/spaces, or control characters (max 200 UTF-8 bytes).")
    for field in ("id", "path"):
        text_check(video.get(field), f"video.{field}", require=True)
    for field in ("duration_ms", "width", "height"):
        if not is_integer(video.get(field)) or video.get(field, 0) <= 0:
            issue("error", "video_number", f"video.{field}", "Expected a positive integer.")
    duration = video.get("duration_ms") if is_integer(video.get("duration_ms")) else None
    text_check(records.get("review_scope"), "review_scope", require=True)

    sources = records.get("sources", [])
    if not isinstance(sources, list):
        issue("error", "sources_type", "sources", "Expected a list.")
        sources = []
    source_ids = set()
    for index, source in enumerate(sources):
        where = f"sources[{index}]"
        if not isinstance(source, dict):
            issue("error", "source_type", where, "Expected a source object.")
            continue
        for field in ("id", "title", "url"):
            text_check(source.get(field), f"{where}.{field}", require=field != "url")
        source_id = source.get("id")
        if isinstance(source_id, str):
            if source_id in source_ids:
                issue("error", "duplicate_source_id", where, source_id)
            source_ids.add(source_id)
        if not isinstance(source.get("version_match"), (str, bool)):
            issue("error", "version_match", f"{where}.version_match", "Record how the episode and cut were compared (text or boolean).")
        if "note" in source:
            text_check(source["note"], f"{where}.note")

    style = records.get("style", {})
    if not isinstance(style, dict):
        issue("error", "style_type", "style", "Expected an object.")
        style = {}
    style = {**DEFAULT_STYLE, **style}
    for field in ("font_en", "font_zh", "font_onscreen"):
        font = style[field]
        if not isinstance(font, str) or not font.strip() or re.search(r"[,{}\\\r\n\x00-\x1f\x7f]", font):
            issue("error", "unsafe_font", f"style.{field}", "Font family must be nonempty and cannot contain ASS delimiters or control characters.")
    for field in ("font_size_en", "font_size_zh", "font_size_onscreen", "outline", "margin_bottom", "margin_top", "margin_side", "dialogue_gap"):
        number = style[field]
        lower = 0 if field in {"outline", "margin_bottom", "margin_top", "margin_side", "dialogue_gap"} else 1
        if not is_number(number) or number < lower or number > 1080:
            issue("error", "style_number", f"style.{field}", f"Expected a finite 1080p-base number in [{lower}, 1080].")
        elif field.startswith("font_size") and number > 108:
            issue("warning", "large_font", f"style.{field}", "Font size exceeds 10% of frame height; render and check occlusion.")
        elif field == "margin_bottom" and number > 540:
            issue("warning", "large_margin", f"style.{field}", "Bottom margin exceeds half of frame height.")
    for field in ("dialogue_en_lines", "dialogue_zh_lines"):
        if not is_integer(style[field]) or not 1 <= style[field] <= 8:
            issue("error", "dialogue_lines", f"style.{field}", "Expected an integer line capacity in [1, 8].")
    if not is_number(style["dialogue_line_height"]) or not 1 <= style["dialogue_line_height"] <= 3:
        issue("error", "dialogue_line_height", "style.dialogue_line_height", "Expected a font-height multiplier in [1, 3]; verify actual font metrics by rendering.")
    if not isinstance(style["dialogue_position"], str) or style["dialogue_position"] not in {"bottom", "middle", "top"}:
        issue("error", "dialogue_position", "style.dialogue_position", "Expected bottom, middle or top.")
    if not isinstance(style["dialogue_align"], str) or style["dialogue_align"] not in {"left", "center", "right"}:
        issue("error", "dialogue_align", "style.dialogue_align", "Expected left, center or right.")
    if not isinstance(style["color"], str) or not re.fullmatch(r"&H[0-9A-Fa-f]{8}", style["color"]):
        issue("error", "style_color", "style.color", "Expected ASS &HAABBGGRR, e.g. &H00FFFFFF.")
    selection = records.get("style_selection")
    if selection is not None:
        if not isinstance(selection, dict) or selection.get("status") not in {"pending", "confirmed", "waived"}:
            issue("error", "style_selection", "style_selection", "Expected pending, confirmed or explicitly waived style selection.")
        elif selection["status"] == "pending":
            issue("warning", "style_pending", "style_selection", "Show the size/font/position previews and obtain the user's choice before final export.")
        else:
            if not isinstance(selection.get("confirmation"), str) or not selection["confirmation"].strip():
                issue("error", "style_confirmation", "style_selection", "Record the user's actual choice or explicit decision to skip previews.")
            if not errors and selection.get("style_sha256") != style_digest(records):
                issue("error", "style_changed", "style_selection", "Current style differs from the user's confirmed preview; update the preview and selection before export.")
            if selection["status"] == "confirmed" and (
                    not isinstance(selection.get("preview_image"), str) or not selection["preview_image"].strip()
                    or not isinstance(selection.get("preview_sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", selection["preview_sha256"])):
                issue("error", "style_preview", "style_selection", "Confirmed selection requires its actual preview image and SHA256.")

    cues = records.get("cues")
    if not isinstance(cues, list):
        issue("error", "cues_type", "cues", "Expected a list.")
        cues = []
    cue_ids, valid_intervals, included = set(), [], []
    for index, cue in enumerate(cues):
        where = f"cues[{index}]"
        if not isinstance(cue, dict):
            issue("error", "cue_type", where, "Expected a cue object.")
            continue
        text_check(cue.get("id"), f"{where}.id", require=True)
        cue_id = cue.get("id")
        if isinstance(cue_id, str):
            if cue_id in cue_ids:
                issue("error", "duplicate_cue_id", where, cue_id)
            cue_ids.add(cue_id)
        if cue.get("kind") not in KINDS:
            issue("error", "cue_kind", f"{where}.kind", "Expected dialogue, onscreen, or lyric.")
        include = cue.get("include", True)
        if not isinstance(include, bool):
            issue("error", "include_type", f"{where}.include", "Expected a boolean.")
        elif include is False:
            if not isinstance(cue.get("exclusion_reason"), str) or not cue["exclusion_reason"].strip():
                issue("error", "exclusion_reason", where, "Excluded subtitles must retain a specific reason; never silently drop dialogue or on-screen text.")
            if not evidence_present(cue.get("exclusion_evidence")):
                issue("error", "exclusion_evidence", where, "Excluded subtitles require review evidence for the exclusion.")
        if include is True:
            included.append(cue)
        start, end = cue.get("start_ms"), cue.get("end_ms")
        valid_time = is_integer(start) and is_integer(end) and 0 <= start < end
        if not valid_time:
            issue("error", "cue_time", where, "Expected integer milliseconds with 0 <= start_ms < end_ms.")
        elif duration is not None and end > duration:
            issue("error", "out_of_range", where, "Cue extends beyond video.duration_ms.")
        elif include is True:
            valid_intervals.append(cue)
            if ass_milliseconds(end) <= ass_milliseconds(start):
                issue("error", "ass_zero_duration", where, "Cue becomes zero-length at ASS centisecond precision; review its timing before export.")
        for language in ("en", "zh"):
            value = cue.get(language)
            text_check(value, f"{where}.{language}", require=include is True)
            if isinstance(value, str) and include is True:
                capacity = style.get(f"dialogue_{language}_lines")
                if cue.get("kind") in {"dialogue", "lyric"} and is_integer(capacity) and len(display_text(value).split("\n")) > capacity:
                    issue("error", "dialogue_line_capacity", f"{where}.{language}", "Explicit lines exceed the fixed language band; rebreak the cue or increase the task-wide line capacity and rerender.")
                limit = 64 if language == "en" else 32
                if any(len(line) > limit for line in display_text(value).split("\n")):
                    issue("warning", "long_line", f"{where}.{language}", f"Line exceeds {limit} characters; rebreak against actual rendering.")
                if valid_time:
                    count = len(re.sub(r"\s", "", value))
                    cps = count * 1000 / (end - start)
                    if cps > (25 if language == "en" else 15):
                        issue("warning", "reading_speed", f"{where}.{language}", f"Estimated {cps:.1f} nonspace characters/s; timing is not changed automatically.")
        if include is True and isinstance(cue.get("zh"), str) and not re.search(r"[\u3400-\u9fff\U00020000-\U000323af]", cue["zh"]):
            issue("warning", "no_cjk", f"{where}.zh", "No Han characters; check whether this is an intentional number/name/foreign text.")
        verification = cue.get("verification", {})
        if not isinstance(verification, dict):
            issue("error", "verification_type", f"{where}.verification", "Expected an object.")
            verification = {}
        for dimension in DIMENSIONS:
            item = verification.get(dimension)
            if item is None:
                if dimension in applicable_dimensions(cue):
                    issue("warning", "missing_verification", f"{where}.verification.{dimension}", "Missing status is treated as pending, never verified.")
                continue
            if not isinstance(item, dict) or item.get("status") not in STATUSES:
                issue("error", "verification_status", f"{where}.verification.{dimension}", "Expected pending, draft, verified, stale, or not_applicable.")
                continue
            evidence = item.get("evidence", [])
            if not isinstance(evidence, list):
                issue("error", "evidence_type", f"{where}.verification.{dimension}.evidence", "Expected a list.")
            if item["status"] == "verified" and not evidence_present(evidence):
                issue("error", "verified_without_evidence", f"{where}.verification.{dimension}", "A verified label requires recorded evidence; the tool cannot establish that the evidence is true.")
            if item["status"] == "not_applicable" and dimension in applicable_dimensions(cue):
                issue("error", "inapplicable_status", f"{where}.verification.{dimension}", "This dimension applies to this cue kind; record pending/draft/verified/stale instead.")
        for field in ("unresolved", "slang_ids"):
            value = cue.get(field, [])
            if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
                issue("error", "cue_list", f"{where}.{field}", "Expected a list of nonempty strings.")
        onscreen = cue.get("onscreen", {})
        if not isinstance(onscreen, dict):
            issue("error", "onscreen_type", f"{where}.onscreen", "Expected an object.")
            onscreen = {}
        if cue.get("kind") == "onscreen":
            mode = onscreen.get("display_mode", "chinese")
            if mode not in {"bilingual", "chinese"}:
                issue("error", "onscreen_mode", f"{where}.onscreen.display_mode", "Expected bilingual or chinese.")
            if "appendix" in onscreen and not isinstance(onscreen["appendix"], bool):
                issue("error", "appendix_type", f"{where}.onscreen.appendix", "Expected a boolean.")
            for field in ("full_en", "full_zh"):
                if field in onscreen:
                    text_check(onscreen[field], f"{where}.onscreen.{field}", require=True)
            if ("full_en" in onscreen) != ("full_zh" in onscreen):
                issue("error", "incomplete_full_text", f"{where}.onscreen", "Record both full_en and full_zh, using an honest unresolved marker for unreadable passages.")
            region = onscreen.get("region")
            if region is not None:
                if not isinstance(region, list) or len(region) != 4 or any(not is_number(n) for n in region):
                    issue("error", "region_type", f"{where}.onscreen.region", "Expected normalized [x, y, width, height].")
                else:
                    x, y, w, h = region
                    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > 1.000001 or y + h > 1.000001:
                        issue("error", "region_bounds", f"{where}.onscreen.region", "Region must stay inside the normalized frame.")
                    elif y + h > 0.75:
                        issue("warning", "onscreen_dialogue_region", f"{where}.onscreen.region", "Region reaches the usual dialogue area; check concurrent subtitles on video.")
        elif onscreen:
            issue("error", "onscreen_kind", f"{where}.onscreen", "On-screen metadata belongs to kind=onscreen.")

    active = []
    for cue in sorted(valid_intervals, key=lambda item: (item["start_ms"], item["end_ms"], item.get("id", ""))):
        if cue.get("kind") not in {"dialogue", "lyric"}:
            continue
        active = [old for old in active if old["end_ms"] > cue["start_ms"]]
        for old in active:
            issue("warning", "dialogue_overlap", cue.get("id", ""), f"Overlaps {old.get('id', '')}; intentional simultaneous speech needs a visual check.")
        active.append(cue)
    pending = records.get("pending", [])
    if not isinstance(pending, list):
        issue("error", "pending_type", "pending", "Expected a list.")
        pending = []
    pending_ids = set()
    for index, item in enumerate(pending):
        where = f"pending[{index}]"
        if not isinstance(item, dict):
            issue("error", "pending_item", where, "Expected an object.")
            continue
        for field in ("id", "kind", "note"):
            text_check(item.get(field), f"{where}.{field}", require=True)
        item_id = item.get("id")
        if isinstance(item_id, str):
            if item_id in pending_ids:
                issue("error", "duplicate_pending_id", where, item_id)
            pending_ids.add(item_id)
        start, end = item.get("start_ms"), item.get("end_ms")
        if not is_integer(start) or not is_integer(end) or not 0 <= start <= end or (duration is not None and end > duration):
            issue("error", "pending_time", where, "Expected in-range milliseconds with start_ms <= end_ms; a point issue may have equal times.")
        ids = item.get("cue_ids", [])
        if not isinstance(ids, list) or any(not isinstance(cid, str) or cid not in cue_ids for cid in ids):
            issue("error", "pending_cue_ids", f"{where}.cue_ids", "Each linked cue_id must exist in this records file.")
    if not errors:
        try:
            dialogue_layout(records, included)
        except SubtitleError as exc:
            issue("error", "ass_layout", "style", str(exc))
    coverage = None
    if check_coverage and not errors:
        coverage = coverage_module().audit_coverage(records)
        errors.extend(coverage["errors"])
        warnings.extend(coverage["issues"])
    final_review = final_review_issues(records) if not errors else []
    warnings.extend(final_review)
    return {"tool_version": TOOL_VERSION, "schema_version": SCHEMA_VERSION, "errors": errors, "warnings": warnings,
            "final_review": final_review,
            "coverage": coverage,
            "counts": {"cues": len(cues), "included": len(included), "pending": len(pending), "sources": len(sources)},
            "verification_limit": "Structural/heuristic checks only; no audio, meaning, font availability, or visual verification."}


def srt_timestamp(milliseconds):
    hours, remainder = divmod(milliseconds, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def ass_milliseconds(milliseconds):
    # One mapping for both sides of every boundary prevents artificial overlap.
    return ((milliseconds + 5) // 10) * 10


def ass_timestamp(milliseconds):
    centiseconds = ass_milliseconds(milliseconds) // 10
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    seconds, centis = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centis:02d}"


def srt_cue_text(cue):
    en, zh = display_text(cue["en"]), display_text(cue["zh"])
    if cue["kind"] == "onscreen":
        mode = cue.get("onscreen", {}).get("display_mode", "chinese")
        text = "【画面文字】\n" + (zh if mode == "chinese" else en + "\n" + zh)
    else:
        text = en + "\n" + zh
    return html.escape(text, quote=False)


def srt_events(cues):
    """Partition all cue boundaries; preserve every active cue for its full interval."""
    changes = {}
    by_id = {cue["id"]: cue for cue in cues}
    for cue in cues:
        changes.setdefault(cue["start_ms"], {"start": [], "end": []})["start"].append(cue["id"])
        changes.setdefault(cue["end_ms"], {"start": [], "end": []})["end"].append(cue["id"])
    times = sorted(changes)
    active, result = set(), []
    kind_order = {"dialogue": 0, "lyric": 1, "onscreen": 2}
    for index, start in enumerate(times[:-1]):
        active.difference_update(changes[start]["end"])
        active.update(changes[start]["start"])
        end = times[index + 1]
        if not active:
            continue
        ordered = sorted(active, key=lambda cid: (kind_order[by_id[cid]["kind"]], by_id[cid]["start_ms"], cid))
        text = "\n".join(srt_cue_text(by_id[cid]) for cid in ordered)
        if result and result[-1]["end_ms"] == start and result[-1]["cue_ids"] == ordered and result[-1]["text"] == text:
            result[-1]["end_ms"] = end
        else:
            result.append({"start_ms": start, "end_ms": end, "cue_ids": ordered, "text": text})
    return result


def render_srt(events):
    return "".join(f"{index}\n{srt_timestamp(event['start_ms'])} --> {srt_timestamp(event['end_ms'])}\n{event['text']}\n\n" for index, event in enumerate(events, 1))


def ass_escape(text):
    # A doubled backslash can still turn the following N into a line break.
    # A zero-width word joiner after each source backslash prevents this while
    # preserving its visible glyph. Braces use libass's literal-brace escapes.
    text = display_text(text).replace("\\", "\\\u2060")
    return text.replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N")


def effective_style(records):
    return {**DEFAULT_STYLE, **records.get("style", {})}


def style_digest(records):
    payload = json.dumps(effective_style(records), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return sha256(payload)


def scaled_style(records):
    style = effective_style(records)
    scale = records["video"]["height"] / 1080
    for field in ("font_size_en", "font_size_zh", "font_size_onscreen", "outline", "margin_bottom", "margin_top", "margin_side", "dialogue_gap"):
        style[field] *= scale
    return style


def number(value):
    return f"{value:.3f}".rstrip("0").rstrip(".")


def dialogue_layout(records, cues):
    """Reserve fixed language bands and keep each overlapping cue in one slot."""
    video, style = records["video"], scaled_style(records)
    en_height = style["font_size_en"] * style["dialogue_line_height"] * style["dialogue_en_lines"]
    zh_height = style["font_size_zh"] * style["dialogue_line_height"] * style["dialogue_zh_lines"]
    slot_height = en_height + zh_height + 2 * style["dialogue_gap"]
    slot_ends, result = [], {}
    for cue in sorted(cues, key=lambda item: (item["start_ms"], item["end_ms"], item["id"])):
        if cue["kind"] == "onscreen":
            continue
        start, end = ass_milliseconds(cue["start_ms"]), ass_milliseconds(cue["end_ms"])
        if end <= start:
            raise SubtitleError(f"{cue['id']}: cue is zero-length at ASS precision.")
        slot = next((index for index, old_end in enumerate(slot_ends) if old_end <= start), len(slot_ends))
        if slot == len(slot_ends):
            slot_ends.append(end)
        else:
            slot_ends[slot] = end
        position = style["dialogue_position"]
        if position == "top":
            en_y = style["margin_top"] + en_height + slot * slot_height
            zh_y = en_y + style["dialogue_gap"]
        elif position == "middle":
            band_height = en_height + style["dialogue_gap"] + zh_height
            en_y = (video["height"] - band_height) / 2 + en_height - slot * slot_height
            zh_y = en_y + style["dialogue_gap"]
        else:
            zh_y = video["height"] - style["margin_bottom"] - zh_height - slot * slot_height
            en_y = zh_y - style["dialogue_gap"]
        if en_y - en_height < 0 or zh_y + zh_height > video["height"]:
            raise SubtitleError(f"{cue['id']}: fixed dialogue bands do not fit in the frame; review simultaneous speech, line capacities, fonts and margins.")
        alignment = style["dialogue_align"]
        x = style["margin_side"] if alignment == "left" else video["width"] - style["margin_side"] if alignment == "right" else video["width"] / 2
        if x < 0 or x > video["width"] or 2 * style["margin_side"] >= video["width"]:
            raise SubtitleError(f"{cue['id']}: horizontal margins do not fit in the frame.")
        en_alignment, zh_alignment = {"left": (1, 7), "center": (2, 8), "right": (3, 9)}[alignment]
        result[cue["id"]] = {"slot": slot, "x": round(x), "en_y": round(en_y), "zh_y": round(zh_y),
                              "en_alignment": en_alignment, "zh_alignment": zh_alignment}
    return result


def render_ass(records, cues):
    video, style = records["video"], scaled_style(records)
    width, height = video["width"], video["height"]
    side = max(1, round(style["margin_side"]))
    layout = dialogue_layout(records, cues)
    header = ["[Script Info]", "; Generated from records schema 1; verification statuses are not changed.",
              "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
              "WrapStyle: 0", "ScaledBorderAndShadow: yes", "Collisions: Normal", "", "[V4+ Styles]",
              "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"]
    definitions = [
        ("DialogueEnglish", style["font_en"], style["font_size_en"], 2, round(style["margin_bottom"])),
        ("DialogueChinese", style["font_zh"], style["font_size_zh"], 8, round(style["margin_bottom"])),
        ("OnscreenEnglish", style["font_en"], style["font_size_onscreen"], 8, round(height * .08)),
        ("OnscreenChinese", style["font_onscreen"], style["font_size_onscreen"], 8, round(height * .08)),
    ]
    for name, font, size, alignment, margin in definitions:
        header.append(f"Style: {name},{font},{number(size)},{style['color']},{style['color']},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,{number(style['outline'])},0,{alignment},{side},{side},{margin},1")
    header += ["", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for cue in sorted(cues, key=lambda item: (item["start_ms"], item["end_ms"], item["id"])):
        start, end = ass_timestamp(cue["start_ms"]), ass_timestamp(cue["end_ms"])
        margin_l = margin_r = margin_v = 0
        if cue["kind"] == "onscreen":
            info = cue.get("onscreen", {})
            x, y, w, h = info.get("region", [.1, .08, .8, .3])
            margin_l, margin_r, margin_v = round(x * width), round((1 - x - w) * width), round(y * height)
            anchor = "{\\an8\\pos(" + number((x + w / 2) * width) + "," + number(y * height) + ")}"
            if info.get("display_mode", "chinese") == "chinese":
                ass_style, text = "OnscreenChinese", anchor + ass_escape(cue["zh"])
            else:
                ass_style = "OnscreenEnglish"
                text = anchor + ass_escape(cue["en"]) + "\\N{\\rOnscreenChinese}" + ass_escape(cue["zh"])
            layer = 1
        else:
            position = layout[cue["id"]]
            for language, ass_style in (("en", "DialogueEnglish"), ("zh", "DialogueChinese")):
                alignment = position[f"{language}_alignment"]
                anchor = "{\\an" + str(alignment) + "\\pos(" + str(position["x"]) + "," + str(position[f"{language}_y"]) + ")}"
                text = anchor + ass_escape(cue[language])
                header.append(f"Dialogue: 0,{start},{end},{ass_style},,0,0,0,,{text}")
            continue
        header.append(f"Dialogue: {layer},{start},{end},{ass_style},,{margin_l},{margin_r},{margin_v},,{text}")
    return "\n".join(header) + "\n"


def pending_text(records):
    entries = []
    for cue in records["cues"]:
        missing = [f"{dimension}={status_of(cue, dimension)}" for dimension in applicable_dimensions(cue)
                   if status_of(cue, dimension) != "verified"]
        notes = cue.get("unresolved", [])
        if not missing and not notes:
            continue
        label = "；".join(missing + notes)
        if not cue.get("include", True):
            label = "未导出：" + label
        entries.append((cue["start_ms"], cue["end_ms"], cue["id"], label))
    for item in records.get("pending", []):
        linked = ", ".join(item.get("cue_ids", []))
        note = f"{item['kind']}：{item['note']}" + (f"；关联字幕：{linked}" if linked else "")
        entries.append((item["start_ms"], item["end_ms"], item["id"], note))
    lines = ["待确认事项", "本清单按记录中的独立核实状态生成；导出或格式检查不会把内容升级为已核实。", ""]
    for start, end, item_id, note in sorted(entries):
        lines.append(f"{srt_timestamp(start)} → {srt_timestamp(end)} [{item_id}] {note}")
    if not entries:
        lines.append("记录中未列出待确认项。这只反映已保存状态，脚本未核实证据的真实性。")
    return "\n".join(lines) + "\n", len(entries)


def appendix_text(cues):
    sections = []
    for cue in sorted(cues, key=lambda item: (item["start_ms"], item["id"])):
        if cue["kind"] != "onscreen":
            continue
        info = cue.get("onscreen", {})
        en, zh = info.get("full_en", cue["en"]), info.get("full_zh", cue["zh"])
        if not (info.get("appendix") or en != cue["en"] or zh != cue["zh"]):
            continue
        mode = info.get("display_mode", "chinese")
        shown = [cue["zh"]] if mode == "chinese" else [cue["en"], cue["zh"]]
        sections += [f"[{cue['id']}] {srt_timestamp(cue['start_ms'])} → {srt_timestamp(cue['end_ms'])}",
                     "原文：", en, "简体中文：", zh,
                     f"上屏模式：{mode}（{'仅中文' if mode == 'chinese' else '原文与中文'}）",
                     "原文对应范围：", cue["en"], "实际上屏内容：", *shown, ""]
    if not sections:
        return None
    return "画面文字全文\n完整可辨认文字与译文保留如下；上屏内容可能按镜头可读时长分段。未辨认内容须以记录中的标记保留。\n\n" + "\n".join(sections)


def notes_text(records, report, events, pending_count, has_appendix, draft=False):
    video = records["video"]
    included = [cue for cue in records["cues"] if cue.get("include", True)]
    lines = [f"# {video['basename']} 字幕说明" + ("（未完成草稿）" if draft else ""), "",
             "交付状态：内部未完成进度，默认不向用户交付；不能称为完整字幕或全片无漏项。" if draft else "交付状态：本次范围的来源对账、记录的原片复查与逐条内容核实已完成；实际音画及渲染验收仍以保存的真实依据为准。",
             "本状态只针对 coverage.scope 的时段和模态；范围外、被排除条目的状态仍完整列在待确认清单，不代表全片均已核实。",
             "漏项、排除依据和未复查范围见同名 .漏项检查.txt；程序不会发现尚未进入任何来源候选的内容。", "",
             f"视频：{video['width']} × {video['height']}；时长 {srt_timestamp(video['duration_ms'])}。",
             f"核实范围：{records['review_scope']}", "", f"导出记录 {len(included)} 条；SRT 在全部起止边界合并为 {len(events)} 个连续编号区间；ASS 按语言独立定位，事件数可不同。",
             f"待确认清单 {pending_count} 项；格式检查 {len(report['errors'])} 个错误、{len(report['warnings'])} 个提示。",
             "格式与编码检查仅检查记录及导出文本；不会替代实际听音、语义审校、字体可用性或画面核对。", "",
             "SRT 与 ASS 均为 UTF-8 BOM / CRLF。对白英文在上、简体中文在下；SRT 字体与样式取决于播放器。",
             "ASS 对白/歌词每条导出英文、中文两个独立定位事件，画面文字每条一个事件；位置不随另一语言的行数变化。",
             "ASS 字号、描边、边距和语言间距按视频高度从 1080p 基准缩放。默认每种语言预留两行，英文底边与中文顶边分别固定；实际字体行高和自动换行须渲染核对。",
             f"所用字体：英文 {effective_style(records)['font_en']}；中文 {effective_style(records)['font_zh']}。1080p 基准字号：英文 {effective_style(records)['font_size_en']}；中文 {effective_style(records)['font_size_zh']}。位置：{effective_style(records)['dialogue_position']} / {effective_style(records)['dialogue_align']}。",
             "同期对白使用独立固定槽位，每条在整个显示期间不换槽；底部/中部的额外槽位向上，顶部的额外槽位向下，需核对画面遮挡。字体未随包提供。",
             "ASS 全部边界统一就近取整到百分之一秒，误差不超过 5 ms，相邻边界不会因取整制造重叠；SRT 保留毫秒。",
             "画面文字在 ASS 独立放置，SRT 使用【画面文字】标记并与同期对白合并；事件数量和排版可以不同。",
             "SRT 以 HTML 实体保护字面尖括号和 &；ASS 对字面花括号转义，并在原有反斜杠后插入零宽连接符以防控制码被执行。",
             ""]
    if has_appendix:
        lines += ["长篇画面文字的完整可辨认原文及译文另见同名 .画面文字全文.txt；说明记录上屏范围。", ""]
    lines += ["## 核实状态", ""]
    for dimension in DIMENSIONS:
        counts = {}
        for cue in included:
            if dimension in applicable_dimensions(cue):
                status = status_of(cue, dimension)
                counts[status] = counts.get(status, 0) + 1
        lines.append(f"- {dimension}：" + ("，".join(f"{key} {counts[key]}" for key in sorted(counts)) or "不适用"))
    lines += ["", "## 参考来源与剪辑版本核对", ""]
    for source in records.get("sources", []):
        lines += [f"- [{source['id']}] {source['title']}", f"  来源：{source['url'] or '本地来源，未提供网页链接'}",
                  f"  版本核对：{source['version_match']}", f"  备注：{source.get('note', '')}"]
    if not records.get("sources"):
        lines.append("未登记外部参考来源。")
    if report["warnings"]:
        lines += ["", "## 格式与阅读提示", ""]
        lines += [f"- {item['at']} [{item['code']}] {item['message']}" for item in report["warnings"]]
    return "\n".join(lines) + "\n"


def validate_srt(data):
    text = validate_subtitle_encoding(data)
    blocks = text.replace("\r\n", "\n").strip().split("\n\n") if text.strip() else []
    previous_end = 0
    for number_, block in enumerate(blocks, 1):
        lines = block.split("\n")
        if len(lines) < 3 or lines[0] != str(number_):
            raise SubtitleError("Invalid SRT numbering or empty subtitle block.")
        match = SRT_TIME.fullmatch(lines[1])
        if not match:
            raise SubtitleError("Invalid SRT timestamp format.")
        values = [int(value) for value in match.groups()]
        for offset in (0, 4):
            if values[offset + 1] >= 60 or values[offset + 2] >= 60:
                raise SubtitleError("Invalid SRT minute or second.")
        start = values[0] * 3600000 + values[1] * 60000 + values[2] * 1000 + values[3]
        end = values[4] * 3600000 + values[5] * 60000 + values[6] * 1000 + values[7]
        if start < previous_end or end <= start:
            raise SubtitleError("SRT timeline has overlaps or nonpositive intervals.")
        previous_end = end
    return len(blocks)


def validate_subtitle_encoding(data):
    if not data.startswith(b"\xef\xbb\xbf"):
        raise SubtitleError("Subtitle is missing the UTF-8 BOM.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError as exc:
        raise SubtitleError("Subtitle is not valid UTF-8.") from exc
    if "\ufffd" in text or "\x00" in text:
        raise SubtitleError("Subtitle contains replacement or NUL characters.")
    without_crlf = text.replace("\r\n", "")
    if "\n" in without_crlf or "\r" in without_crlf:
        raise SubtitleError("Subtitle is not consistently CRLF.")
    return text


def validate_ass(data):
    text = validate_subtitle_encoding(data)
    if "[V4+ Styles]" not in text or "[Events]" not in text:
        raise SubtitleError("Missing ASS sections.")
    count = 0
    for line in text.splitlines():
        if line.startswith("Dialogue:"):
            fields = line.split(",", 9)
            if len(fields) != 10 or not fields[9]:
                raise SubtitleError("Invalid or empty ASS event.")
            count += 1
    return count


def is_link(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def reject_links(path):
    path = Path(os.path.abspath(path))
    for component in [path, *path.parents]:
        if is_link(component):
            raise SubtitleError(f"Symlink or reparse point is not accepted: {component}")


def atomic_write(path, data, force=False):
    """Atomically install one file; hard-link insertion protects no-force races."""
    path = Path(path)
    reject_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    reject_links(path.parent)
    if path.exists() and not force:
        raise SubtitleError(f"Refusing to overwrite {path}; use --force after reviewing the destination.")
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".subtitle-", suffix=".tmp", dir=path.parent, delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if force:
            os.replace(temp_path, path)
        else:
            try:
                os.link(temp_path, path)
            except FileExistsError as exc:
                raise SubtitleError(f"Destination appeared during export: {path}") from exc
            except OSError as exc:
                raise SubtitleError(f"Atomic no-overwrite insertion failed for {path}: {exc}") from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def export_records(records_path, out, force=False, draft=False):
    records = read_json(records_path)
    report = lint_records(records)
    if report["errors"]:
        raise SubtitleError("Export blocked by record errors:\n" + "\n".join(f"{item['at']}: {item['message']}" for item in report["errors"]))
    coverage = report["coverage"]
    if not draft and not coverage["ready"]:
        raise SubtitleError("Coverage is incomplete; run coverage.py audit to locate missing source items and unreviewed ranges. Use --draft only for explicitly unfinished progress.")
    if not draft and report["final_review"]:
        raise SubtitleError("Final review is incomplete; finish the in-scope content review before final delivery:\n" +
                            "\n".join(f"{item['at']}: {item['message']}" for item in report["final_review"]))
    if records.get("style_selection", {}).get("status") == "pending":
        raise SubtitleError("Show the subtitle size/font/position previews and wait for the user's choice before final export.")
    selection = records.get("style_selection", {})
    if selection.get("status") == "confirmed":
        preview_image = Path(selection["preview_image"])
        if not preview_image.is_absolute():
            preview_image = Path(records_path).parent / preview_image
        reject_links(preview_image)
        if not preview_image.is_file() or sha256(preview_image.read_bytes()) != selection["preview_sha256"]:
            raise SubtitleError("The confirmed preview image is missing or changed; restore it or render a fresh selection before export.")
    cues = [cue for cue in records["cues"] if cue.get("include", True)]
    if not cues:
        raise SubtitleError("No included cues to export.")
    events = srt_events(cues)
    basename = records["video"]["basename"] + (".草稿" if draft else "")
    pending, pending_count = pending_text(records)
    appendix = appendix_text(cues)
    ass_count = sum(1 if cue["kind"] == "onscreen" else 2 for cue in cues)
    payloads = {
        f"{basename}.srt": subtitle_bytes(render_srt(events)),
        f"{basename}.ass": subtitle_bytes(render_ass(records, cues)),
        f"{basename}.说明.md": subtitle_bytes(notes_text(records, report, events, pending_count, bool(appendix), draft=draft)),
        f"{basename}.待确认事项.txt": subtitle_bytes(pending),
        f"{basename}.漏项检查.txt": subtitle_bytes(coverage_module().report_text(records, coverage)),
    }
    if appendix:
        payloads[f"{basename}.画面文字全文.txt"] = subtitle_bytes(appendix)
    if validate_srt(payloads[f"{basename}.srt"]) != len(events) or validate_ass(payloads[f"{basename}.ass"]) != ass_count:
        raise SubtitleError("Internal event count verification failed.")
    manifest = {"schema_version": SCHEMA_VERSION, "tool_version": TOOL_VERSION, "kind": "subtitle_export",
                "video_id": records["video"]["id"], "basename": basename,
                "source_basename": records["video"]["basename"], "export_mode": "draft" if draft else "final",
                "coverage": {"ready": coverage["ready"], "summary": coverage["summary"],
                             "basis_sha256": coverage_module().coverage_digest(records)},
                "records_sha256": sha256(Path(records_path).read_bytes()), "review_scope": records["review_scope"],
                "style": effective_style(records), "style_sha256": style_digest(records),
                "files": [{"path": name, "size": len(data), "sha256": sha256(data)} for name, data in payloads.items()],
                "counts": {**report["counts"], "srt_events": len(events), "ass_events": ass_count, "pending_items": pending_count},
                "ass_dialogue_layout": dialogue_layout(records, cues),
                "warnings": report["warnings"],
                "srt_intervals": [{key: event[key] for key in ("start_ms", "end_ms", "cue_ids")} for event in events],
                "verification_limit": report["verification_limit"]}
    manifest_name = f"{basename}.export.json"
    payloads[manifest_name] = json_bytes(manifest)
    out = Path(out)
    reject_links(out)
    for name in payloads:
        destination = out / name
        if destination.resolve() == Path(records_path).resolve():
            raise SubtitleError("Export destination cannot replace source records.")
        reject_links(destination)
        if destination.exists() and (not force or not destination.is_file()):
            raise SubtitleError(f"Refusing to replace existing destination: {destination}")
    # The manifest is committed last, so packaging catches an interrupted export
    # through missing or mismatched file hashes rather than silently shipping it.
    out.mkdir(parents=True, exist_ok=True)
    for name, data in payloads.items():
        atomic_write(out / name, data, force=force)
    return {"out": str(out.resolve()), "manifest": str((out / manifest_name).resolve()),
            "files": [str((out / name).resolve()) for name in payloads], "counts": manifest["counts"],
            "warnings": len(report["warnings"]), "verification_limit": report["verification_limit"]}


def secure_member(delivery, name):
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise SubtitleError("Manifest member must be a nonempty POSIX relative path.")
    relative = PurePosixPath(name)
    if relative.is_absolute() or any(part in {".", ".."} for part in name.split("/")) or any(not safe_basename(part) for part in relative.parts):
        raise SubtitleError(f"Unsafe ZIP member: {name}")
    path = delivery.joinpath(*relative.parts)
    reject_links(path)
    if not path.is_file() or os.path.commonpath([str(delivery.resolve()), str(path.resolve())]) != str(delivery.resolve()):
        raise SubtitleError(f"Delivery member is missing or outside the delivery directory: {name}")
    if relative.suffix.lower() not in ALLOWED_DELIVERY_SUFFIXES:
        raise SubtitleError(f"Only intentional subtitle and explanation files may be packaged: {name}")
    return path


def package_delivery(delivery, zip_path, force=False):
    delivery, zip_path = Path(delivery), Path(zip_path)
    if zip_path.suffix.lower() != ".zip":
        raise SubtitleError("Archive destination must have a .zip extension.")
    reject_links(delivery)
    reject_links(zip_path)
    if not delivery.is_dir():
        raise SubtitleError(f"Delivery directory does not exist: {delivery}")
    manifest_paths = sorted(delivery.glob("*.export.json"))
    extra_manifest = delivery / "delivery_manifest.json"
    if extra_manifest.exists():
        manifest_paths.append(extra_manifest)
    if not manifest_paths:
        raise SubtitleError("No explicit delivery whitelist found (*.export.json or delivery_manifest.json).")
    members, keys = [], set()
    for manifest_path in manifest_paths:
        reject_links(manifest_path)
        manifest = read_json(manifest_path)
        if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION or not isinstance(manifest.get("files"), list):
            raise SubtitleError(f"Invalid delivery manifest: {manifest_path}")
        is_export = manifest_path.name != "delivery_manifest.json"
        if is_export and manifest.get("kind") != "subtitle_export":
            raise SubtitleError(f"Invalid export manifest kind: {manifest_path}")
        for item in manifest["files"]:
            if isinstance(item, str) and not is_export:
                name, expected = item, {}
            elif isinstance(item, dict):
                name, expected = item.get("path"), item
            else:
                raise SubtitleError(f"Invalid member entry in {manifest_path}")
            path = secure_member(delivery, name)
            key = unicodedata.normalize("NFC", name).casefold()
            if key in keys:
                raise SubtitleError(f"Duplicate or case/Unicode-ambiguous ZIP member: {name}")
            keys.add(key)
            data = path.read_bytes()
            digest = sha256(data)
            if is_export and ("sha256" not in expected or "size" not in expected):
                raise SubtitleError(f"Export member has no integrity record: {name}")
            if expected.get("sha256", digest) != digest or expected.get("size", len(data)) != len(data):
                raise SubtitleError(f"Delivery file changed after export; review and reexport: {name}")
            if path.suffix.lower() == ".srt":
                validate_srt(data)
            elif path.suffix.lower() == ".ass":
                validate_ass(data)
            else:
                try:
                    note = data.decode("utf-8-sig")
                except UnicodeError as exc:
                    raise SubtitleError(f"Explanation file is not UTF-8: {name}") from exc
                if "\ufffd" in note or "\x00" in note:
                    raise SubtitleError(f"Explanation file has damaged Unicode: {name}")
            members.append((name, data, digest))
    if not members:
        raise SubtitleError("Delivery whitelist is empty.")
    report_path = zip_path.with_name(zip_path.name + ".report.json")
    for target in (zip_path, report_path):
        reject_links(target)
        if target.exists() and (not force or not target.is_file()):
            raise SubtitleError(f"Refusing to overwrite {target}; use --force after reviewing it.")
    # A temporary archive is validated byte-for-byte before it replaces a target.
    reject_links(zip_path.parent)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".subtitle-zip-", suffix=".tmp", dir=zip_path.parent, delete=False) as handle:
            temporary = Path(handle.name)
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, strict_timestamps=True) as archive:
            for name, data, _ in sorted(members, key=lambda item: item[0]):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(info, data, compresslevel=6)
        checks = []
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None or len(archive.infolist()) != len(members):
                raise SubtitleError("ZIP integrity/entry-count check failed.")
            for name, data, digest in sorted(members, key=lambda item: item[0]):
                if archive.read(name) != data:
                    raise SubtitleError(f"ZIP member differs from source bytes: {name}")
                info = archive.getinfo(name)
                checks.append({"path": name, "size": len(data), "crc32": f"{info.CRC:08x}", "sha256": digest})
        zip_data = temporary.read_bytes()
        atomic_write(zip_path, zip_data, force=force)
        if sha256(zip_path.read_bytes()) != sha256(zip_data):
            raise SubtitleError("Installed ZIP hash differs from verified archive.")
        report = {"schema_version": SCHEMA_VERSION, "tool_version": TOOL_VERSION,
                  "zip": str(zip_path.resolve()), "size": len(zip_data), "sha256": sha256(zip_data),
                  "member_count": len(checks), "members": checks, "crc_and_bytes_verified": True,
                  "verification_limit": "Archive and text-format integrity only; no claim of audiovisual or translation correctness."}
        atomic_write(report_path, json_bytes(report), force=force)
        return report
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=TOOL_VERSION)
    sub = parser.add_subparsers(dest="command", required=True)
    lint = sub.add_parser("lint", help="Validate Unicode records; does not listen, translate, or edit them.")
    lint.add_argument("records", type=Path)
    lint.add_argument("--report", type=Path, help="Write a machine-readable report, replacing an existing report only.")
    export = sub.add_parser("export", help="Write SRT/ASS, per-video notes, pending list, and an integrity whitelist.")
    export.add_argument("records", type=Path)
    export.add_argument("--out", type=Path, required=True)
    export.add_argument("--force", action="store_true", help="Replace matching deliverable files; never changes source records.")
    export.add_argument("--draft", action="store_true", help="Save internal unfinished progress with .草稿 filenames and review reports; not a default user deliverable and never bypasses structural or style checks.")
    package = sub.add_parser("package", help="ZIP only files explicitly selected by delivery manifests.")
    package.add_argument("--delivery", type=Path, required=True)
    package.add_argument("--zip", dest="zip_path", type=Path, required=True)
    package.add_argument("--force", action="store_true", help="Replace the selected ZIP and its integrity report.")
    args = parser.parse_args(argv)
    try:
        if args.command == "lint":
            report = lint_records(read_json(args.records))
            if args.report:
                if args.report.resolve() == args.records.resolve():
                    raise SubtitleError("Report destination cannot replace source records.")
                atomic_write(args.report, json_bytes(report), force=True)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1 if report["errors"] else 0
        if args.command == "export":
            result = export_records(args.records, args.out, force=args.force, draft=args.draft)
        else:
            result = package_delivery(args.delivery, args.zip_path, force=args.force)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (SubtitleError, OSError, UnicodeError, ValueError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
