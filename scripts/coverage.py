#!/usr/bin/env python3
"""Candidate reconciliation and recorded media review; Python 3.10+, stdlib.

This tool never recognizes, translates, or watches media. Its completeness
decision depends on independent candidates and honest human/agent review notes.
"""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

VERSION = "1.02"
KINDS = {"dialogue", "lyric", "onscreen"}
REVIEW_CODES = {"coverage_review_pending", "coverage_review_stale", "coverage_review_evidence"}


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _evidence(value):
    return isinstance(value, list) and any(_text(x) or (isinstance(x, dict) and bool(x)) for x in value)


def _time(obj, duration):
    a, b = obj.get("start_ms"), obj.get("end_ms")
    return type(a) is int and type(b) is int and 0 <= a < b <= duration


def _modality(kind):
    return "visual" if kind == "onscreen" else "audio"


def _overlap(a, b):
    return max(a["start_ms"], b["start_ms"]) < min(a["end_ms"], b["end_ms"])


def _active(obj, scope):
    return bool(scope.get(_modality(obj.get("kind")))) and _overlap(obj, scope)


def coverage_digest(records):
    """Bind review to media, candidates, scans, scope and final subtitle content."""
    video = records.get("video", {})
    cov = records.get("coverage", {})
    cue_keys = ("id", "kind", "en", "zh", "start_ms", "end_ms", "include", "exclusion_reason", "exclusion_evidence")
    screen_keys = ("full_en", "full_zh", "display_mode", "appendix")
    cues = []
    for cue in records.get("cues", []):
        row = {key: cue.get(key) for key in cue_keys}
        row["include"] = cue.get("include", True)
        screen = cue.get("onscreen", {})
        row["onscreen"] = {key: screen.get(key) for key in screen_keys} if isinstance(screen, dict) else screen
        cues.append(row)
    basis = {"video": {k: video.get(k) for k in ("id", "path", "basename", "duration_ms", "width", "height", "fingerprint")},
             "coverage": {k: cov.get(k) for k in ("schema_version", "scope", "items", "scans", "expected_item_ids", "inventory_evidence")}, "cues": cues}
    data = json.dumps(basis, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _gaps(intervals, start, end):
    cursor, gaps, covered = start, [], 0
    for a, b in sorted(intervals):
        a, b = max(a, start), min(b, end)
        if b <= a or b <= cursor:
            continue
        if a > cursor:
            gaps.append((cursor, a))
        covered += b - max(cursor, a)
        cursor = b
    if cursor < end:
        gaps.append((cursor, end))
    return gaps, covered


def audit_coverage(records):
    """Return structural errors, unresolved work, scope counts and ready flag."""
    errors, issues = [], []
    summary = {"items_total": 0, "items_in_scope": 0, "items_out_of_scope": 0,
               "items_pending": 0, "items_mapped": 0, "items_excluded": 0,
               "included_cues_in_scope": 0, "mapped_cues_in_scope": 0,
               "scans_total": 0, "reviewed_ms": {"audio": 0, "visual": 0}}

    def error(code, at, message):
        errors.append({"code": code, "at": at, "message": message})

    def issue(code, at, message, obj=None, kind=None):
        row = {"code": code, "at": at, "message": message}
        if obj is not None:
            row.update(start_ms=obj["start_ms"], end_ms=obj["end_ms"])
        if kind is not None:
            row["kind"] = kind
        issues.append(row)

    def result():
        return {"schema_version": 1, "errors": errors, "issues": issues,
                "summary": summary, "ready": not errors and not issues}

    if not isinstance(records, dict):
        error("records_type", "$", "任务记录必须为 JSON 对象。")
        return result()
    if type(records.get("schema_version")) is not int or records["schema_version"] != 1:
        error("records_schema", "schema_version", "仅支持整数 schema_version=1。")
    video = records.get("video")
    duration = video.get("duration_ms") if isinstance(video, dict) else None
    if type(duration) is not int or duration < 1:
        error("coverage_duration", "video.duration_ms", "漏项核验要求已校准的正整数全片时长（毫秒）。")
        return result()
    cue_list = records.get("cues")
    cues = {}
    if not isinstance(cue_list, list):
        error("coverage_cues", "cues", "最终字幕 cues 必须为数组。")
        cue_list = []
    for n, cue in enumerate(cue_list):
        at = f"cues[{n}]"
        if not isinstance(cue, dict):
            error("coverage_cue", at, "字幕条目必须为对象。")
            continue
        cid = cue.get("id")
        valid_id = _text(cid) and cid not in cues
        if not valid_id:
            error("coverage_cue_id", at + ".id", "字幕 ID 必须非空且唯一。")
        valid = _time(cue, duration) and isinstance(cue.get("kind"), str) and cue["kind"] in KINDS
        if not valid:
            error("coverage_cue_interval", at, "字幕 kind 必须合法，时段须在全片范围内且为正长度整数毫秒。")
        if type(cue.get("include", True)) is not bool or not all(isinstance(cue.get(k), str) for k in ("en", "zh")):
            error("coverage_cue_content", at, "include 必须为布尔值，en/zh 必须为字符串。")
            valid = False
        if valid_id:
            cues[cid] = (cue, at, valid)
    cov = records.get("coverage")
    if "coverage" not in records:
        issue("coverage_missing", "coverage", "缺少独立候选清单与原片复查记录；不能仅检查已有字幕就认定没有漏项。")
        return result()
    if not isinstance(cov, dict) or type(cov.get("schema_version")) is not int or cov.get("schema_version") != 1:
        error("coverage_schema", "coverage", "coverage 必须为 schema_version=1 的对象。")
        return result()
    scope = {"start_ms": 0, "end_ms": duration, "audio": True, "visual": True, "reason": "全片双模态复查"}
    if "scope" in cov:
        raw = cov["scope"]
        if not isinstance(raw, dict) or not _time(raw, duration) or any(type(raw.get(k)) is not bool for k in ("audio", "visual")):
            error("coverage_scope", "coverage.scope", "scope 需要合法时段及 audio/visual 布尔值。")
            return result()
        scope = dict(raw)
        if not scope["audio"] and not scope["visual"]:
            error("coverage_scope_empty", "coverage.scope", "本次复查不能同时关闭对白和画面文字。")
        partial = scope["start_ms"] != 0 or scope["end_ms"] != duration or not scope["audio"] or not scope["visual"]
        if (partial and not _text(scope.get("reason"))) or ("reason" in scope and not isinstance(scope["reason"], str)):
            error("coverage_scope_reason", "coverage.scope.reason", "局部任务或关闭模态须记录用户要求/实际任务依据。")
    summary["scope"] = scope
    summary["full_video_dual_review"] = scope["start_ms"] == 0 and scope["end_ms"] == duration and scope["audio"] and scope["visual"]
    item_list, scans = cov.get("items"), cov.get("scans")
    if not isinstance(item_list, list) or not isinstance(scans, list):
        error("coverage_lists", "coverage", "items/scans 必须为数组。")
        return result()
    summary.update(items_total=len(item_list), scans_total=len(scans))
    item_ids, mapped = set(), set()
    for n, item in enumerate(item_list):
        at = f"coverage.items[{n}]"
        if not isinstance(item, dict):
            error("coverage_item", at, "候选必须为对象。")
            continue
        iid = item.get("id")
        if not _text(iid) or iid in item_ids:
            error("coverage_item_id", at + ".id", "独立候选 ID 必须非空且唯一。")
        elif isinstance(iid, str):
            item_ids.add(iid)
        kind, status = item.get("kind"), item.get("status")
        if not isinstance(kind, str) or kind not in KINDS or not _time(item, duration):
            error("coverage_item_interval", at, "候选 kind 必须合法，时段须在全片范围内且为正长度整数毫秒。")
            continue
        if not _text(item.get("source")):
            error("coverage_item_source", at + ".source", "候选须记录具体出处，不能从最终字幕反推候选。")
        if not isinstance(status, str) or status not in {"pending", "mapped", "excluded"}:
            error("coverage_item_status", at + ".status", "候选状态为 pending/mapped/excluded。")
            continue
        links = item.get("cue_ids")
        if not isinstance(links, list) or any(not _text(x) for x in links) or len(set(links)) != len(links):
            error("coverage_item_links", at + ".cue_ids", "cue_ids 必须为不重复的非空字符串数组。")
            continue
        if not isinstance(item.get("evidence"), list) or ("text" in item and not isinstance(item["text"], str)) or ("reason" in item and not isinstance(item["reason"], str)):
            error("coverage_item_fields", at, "evidence 必须为数组，text/reason（若有）须为字符串。")
        if not _active(item, scope):
            summary["items_out_of_scope"] += 1
            continue
        summary["items_in_scope"] += 1
        summary["items_" + status] += 1
        if status == "pending":
            issue("coverage_item_pending", at, f"候选 {iid} 尚未确认字幕去向。", item, kind)
        elif status == "excluded":
            if not _text(item.get("reason")) or not _evidence(item.get("evidence")):
                issue("coverage_exclusion_evidence", at, f"排除候选 {iid} 须保留明确理由及核验依据。", item, kind)
            if links:
                issue("coverage_excluded_links", at, "已排除候选不得继续链接最终字幕。", item, kind)
        else:
            if not links:
                issue("coverage_mapping_missing", at, f"候选 {iid} 没有链接最终字幕。", item, kind)
            if not _evidence(item.get("evidence")):
                issue("coverage_mapping_evidence", at, "须记录整项内容已核对去向的依据；仅时间重合不足以证明语义完整。", item, kind)
            relevant = False
            for cid in links:
                target = cues.get(cid)
                if not target:
                    issue("coverage_cue_missing", at, f"链接字幕 {cid} 已不存在，独立候选仍须处理。", item, kind)
                    continue
                cue, _, valid = target
                if not valid:
                    continue
                if cue.get("include", True) is not True:
                    issue("coverage_cue_excluded", at, f"链接字幕 {cid} 被排除，不能作为候选已翻译的依据。", item, kind)
                elif cue["kind"] != kind:
                    issue("coverage_cue_kind", at, f"链接字幕 {cid} 类型与候选不符。", item, kind)
                elif not _overlap(item, cue):
                    issue("coverage_cue_disjoint", at, f"链接字幕 {cid} 与候选时段没有正长度交集。", item, kind)
                else:
                    mapped.add(cid)
                    relevant |= max(item["start_ms"], cue["start_ms"], scope["start_ms"]) < min(item["end_ms"], cue["end_ms"], scope["end_ms"])
            if links and not relevant:
                issue("coverage_mapping_outside_scope", at, "没有有效映射覆盖候选在本次任务范围内的部分。", item, kind)
    for cid, (cue, at, valid) in cues.items():
        if valid and cue.get("include", True) and _active(cue, scope):
            summary["included_cues_in_scope"] += 1
            if cid in mapped:
                summary["mapped_cues_in_scope"] += 1
            else:
                issue("coverage_cue_untracked", at, f"最终字幕 {cid} 没有独立候选来源映射。", cue, cue["kind"])
    expected = cov.get("expected_item_ids", [])
    inventory_evidence = cov.get("inventory_evidence", [])
    if not isinstance(expected, list) or any(not _text(x) for x in expected) or len(set(expected)) != len(expected) or not isinstance(inventory_evidence, list):
        error("coverage_inventory", "coverage.expected_item_ids", "期待候选 ID 必须为唯一非空字符串数组，inventory_evidence 必须为数组。")
    else:
        summary["expected_items"] = len(expected)
        if "expected_item_ids" not in cov or not _evidence(inventory_evidence):
            issue("coverage_inventory_unfrozen", "coverage.inventory_evidence", "独立候选清单尚未冻结；收集候选后使用 freeze 或严格参考字幕导入保存独立基准及依据。")
        for n, iid in enumerate(expected):
            if iid not in item_ids:
                issue("source_item_lost", f"coverage.expected_item_ids[{n}]", f"独立基准候选 {iid} 已从清单消失；须恢复候选，不能删除基准后重新确认。")
        for iid in sorted(item_ids - set(expected)):
            issue("source_item_unregistered", "coverage.items", f"新增候选 {iid} 未登记独立基准；使用 freeze 扩展基准。")
    intervals, scan_ids = {"audio": [], "visual": []}, set()
    for n, scan in enumerate(scans):
        at = f"coverage.scans[{n}]"
        if not isinstance(scan, dict):
            error("coverage_scan", at, "复查区间必须为对象。")
            continue
        sid, kind, status = scan.get("id"), scan.get("kind"), scan.get("status")
        if not _text(sid) or sid in scan_ids:
            error("coverage_scan_id", at + ".id", "复查区间 ID 必须非空且唯一。")
        else:
            scan_ids.add(sid)
        if not isinstance(kind, str) or kind not in intervals or not _time(scan, duration):
            error("coverage_scan_interval", at, "scan.kind 为 audio/visual，时段须在全片范围内且为正长度整数毫秒。")
            continue
        if not isinstance(status, str) or status not in {"pending", "reviewed"} or not _text(scan.get("method")) or not isinstance(scan.get("evidence"), list):
            error("coverage_scan_fields", at, "复查需要 pending/reviewed 状态、非空 method 和 evidence 数组。")
            continue
        if not scope[kind] or not _overlap(scan, scope):
            continue
        if status != "reviewed":
            issue("coverage_scan_pending", at, "该区间尚未实际复查原片。", scan, kind)
        elif scan["method"] != kind + "_review" or not _evidence(scan["evidence"]):
            issue("coverage_scan_evidence", at, "只有实际 audio_review/visual_review 且有依据的复查可覆盖区间；ASR/VAD/OCR/参考字幕/抽样不能代替。", scan, kind)
        else:
            intervals[kind].append((scan["start_ms"], scan["end_ms"]))
    for kind in intervals:
        if scope[kind]:
            gaps, covered = _gaps(intervals[kind], scope["start_ms"], scope["end_ms"])
            summary["reviewed_ms"][kind] = covered
            for a, b in gaps:
                issue("coverage_scan_gap", "coverage.scans", "本次范围内存在未实际复查的原片区间。", {"start_ms": a, "end_ms": b}, kind)
    review = cov.get("review")
    if not isinstance(review, dict) or not isinstance(review.get("status"), str) or review.get("status") not in {"pending", "complete"} or not isinstance(review.get("evidence"), list):
        error("coverage_review", "coverage.review", "review 需要 pending/complete 状态和 evidence 数组。")
    elif "basis_sha256" in review and (not isinstance(review["basis_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", review["basis_sha256"])):
        error("coverage_review_basis", "coverage.review.basis_sha256", "basis_sha256（若有）必须为小写 SHA-256。")
    elif review["status"] != "complete":
        issue("coverage_review_pending", "coverage.review", "候选对账及本次范围原片复查尚未最终确认。")
    else:
        if not _evidence(review["evidence"]):
            issue("coverage_review_evidence", "coverage.review", "最终复查确认须有真实依据。")
        if not errors:
            try:
                basis = coverage_digest(records)
            except (ValueError, TypeError, UnicodeError):
                error("coverage_digest", "coverage.review", "摘要绑定字段包含不能编码的 Unicode/非 JSON 数据，须修复记录。")
            else:
                if review.get("basis_sha256") != basis:
                    issue("coverage_review_stale", "coverage.review", "候选、复查区间、媒体或最终字幕已改变/未绑定，须重新核验并确认。")
    return result()


def report_text(records, report):
    """Readable, honest audit; distinguishes candidate accounting from reviews."""
    s = report["summary"]
    lines = ["双语字幕漏项核验报告", "状态：" + ("本次范围核验完成" if report["ready"] else "未完成，不能作为本次正式交付"),
             "本报告核对独立候选的去向及已记录的原片实际复查范围。脚本不能自动识别未进入任何候选的对白或画面文字。"]
    scope = s.get("scope")
    if scope:
        lines += [f"本次范围：{scope['start_ms']}–{scope['end_ms']} ms；对白/歌曲={scope['audio']}；画面文字={scope['visual']}；依据={scope.get('reason', '')}",
                  "范围外候选保留，不纳入本次完成判定；局部或单模态完成不代表全片无遗漏。"]
    lines += [f"候选：总计 {s['items_total']}；范围内 {s['items_in_scope']}（待处理 {s['items_pending']}，已映射 {s['items_mapped']}，已排除 {s['items_excluded']}）；范围外 {s['items_out_of_scope']}",
              f"范围内最终字幕：{s['included_cues_in_scope']}；有候选映射：{s['mapped_cues_in_scope']}；实际复查毫秒数：{s['reviewed_ms']}"]
    for label, rows in (("结构错误", report["errors"]), ("待处理漏项/复查", report["issues"])):
        lines.append(label + "：")
        for row in rows:
            timing = f" ({row['start_ms']}–{row['end_ms']} ms; {row.get('kind', '')})" if "start_ms" in row else ""
            lines.append(f"- [{row['code']}] {row['at']}{timing}：{row['message']}")
        if not rows:
            lines.append("- 无")
    cov = records.get("coverage", {}) if isinstance(records, dict) else {}
    if isinstance(cov, dict):
        for key, title in (("items", "候选清单（保留所有排除及范围外项）"), ("scans", "原片复查记录")):
            lines.append(title + "：")
            if isinstance(cov.get(key), list):
                lines.extend("- " + json.dumps(x, ensure_ascii=False, sort_keys=True) for x in cov[key])
        lines.append("最终确认记录：" + json.dumps(cov.get("review"), ensure_ascii=False, sort_keys=True))
        lines.append("独立候选基准 ID：" + json.dumps(cov.get("expected_item_ids"), ensure_ascii=False))
        lines.append("独立候选收集依据：" + json.dumps(cov.get("inventory_evidence"), ensure_ascii=False))
    return "\n".join(lines) + "\n"


def _read(path):
    def pairs(values):
        obj = {}
        for key, value in values:
            if key in obj:
                raise ValueError("重复 JSON 字段：" + key)
            obj[key] = value
        return obj
    return json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=pairs,
                      parse_constant=lambda x: (_ for _ in ()).throw(ValueError("非法 JSON 常量：" + x)))


def _write(path, obj):
    path = Path(path)
    data = (json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix="." + path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextlib.contextmanager
def _locked(path):
    lock = Path(str(path) + ".lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise ValueError(f"记录正在编辑：{lock}；旧锁须先人工检查。") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(f"pid={os.getpid()}\n")
        yield
    finally:
        lock.unlink(missing_ok=True)


def _srt(path, offset, duration):
    data = Path(path).read_bytes()
    text = data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise ValueError("参考 SRT 不能为空。")
    seen, rows = set(), []
    time_pattern = re.compile(r"(\d{2,}):([0-5]\d):([0-5]\d),(\d{3}) --> (\d{2,}):([0-5]\d):([0-5]\d),(\d{3})")
    for block in re.split(r"\n[ \t]*\n", text):
        lines = block.split("\n")
        if len(lines) < 3 or not re.fullmatch(r"[1-9]\d*", lines[0]) or lines[0] in seen or not any(x.strip() for x in lines[2:]):
            raise ValueError("严格 SRT 解析失败：需要唯一正整数序号、时间行和非空正文。")
        seen.add(lines[0])
        match = time_pattern.fullmatch(lines[1])
        if not match:
            raise ValueError(f"SRT #{lines[0]} 时间格式错误。")
        values = list(map(int, match.groups()))
        start, end = [(values[n] * 3600000 + values[n + 1] * 60000 + values[n + 2] * 1000 + values[n + 3] + offset) for n in (0, 4)]
        if not 0 <= start < end <= duration:
            raise ValueError(f"SRT #{lines[0]} 超出原片时长/不是正长度；须按实际剪辑校准后导入。")
        rows.append((lines[0], start, end, "\n".join(lines[2:])))
    return hashlib.sha256(data).hexdigest(), rows


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    audit = sub.add_parser("audit")
    audit.add_argument("records")
    audit.add_argument("--report")
    for name in ("init", "import-srt", "freeze", "review"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--records", required=True)
        if name == "import-srt":
            cmd.add_argument("--srt", required=True)
            cmd.add_argument("--source-id", required=True)
            cmd.add_argument("--offset-ms", type=int, default=0)
        elif name in ("review", "freeze"):
            cmd.add_argument("--evidence", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "audit":
            records = _read(args.records)
            report = audit_coverage(records)
            if args.report:
                if Path(args.report).resolve() == Path(args.records).resolve():
                    raise ValueError("报告路径不能覆盖输入记录。")
                _write(args.report, report)
            print(report_text(records, report), end="")
            return 2 if report["errors"] else (0 if report["ready"] else 1)
        path = Path(args.records)
        with _locked(path):
            records = _read(path)
            report = audit_coverage(records)
            if report["errors"]:
                raise ValueError("结构异常，先修复记录：" + "; ".join(x["message"] for x in report["errors"]))
            if type(records.get("revision", 0)) is not int or records.get("revision", 0) < 0 or not isinstance(records.get("history", []), list):
                raise ValueError("revision/history 结构异常，未修改记录。")
            detail = {}
            if args.command == "init":
                if "coverage" in records:
                    raise ValueError("coverage 已存在；不能重新初始化抹掉候选/核验记录。")
                records["coverage"] = {"schema_version": 1, "items": [], "scans": [], "expected_item_ids": [], "inventory_evidence": [], "review": {"status": "pending", "evidence": []}}
            elif args.command == "import-srt":
                if "coverage" not in records:
                    raise ValueError("先执行 init；禁止导入时隐式覆盖旧状态。")
                if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.source_id):
                    raise ValueError("source-id 仅使用英文字母、数字、下划线、点和连字符。")
                digest, rows = _srt(args.srt, args.offset_ms, records["video"]["duration_ms"])
                items = records["coverage"]["items"]
                known = {x["id"]: x for x in items}
                expected = list(records["coverage"].get("expected_item_ids", []))
                registered = []
                added = []
                for index, start, end, text in rows:
                    iid = f"ref-{args.source_id}-{index}"
                    if iid not in expected:
                        expected.append(iid)
                        registered.append(iid)
                    if iid in known:
                        if f"sha256={digest};" not in known[iid]["source"]:
                            raise ValueError(f"已有候选 {iid} 的参考文件哈希不同；保留旧候选并使用新的 source-id。")
                        continue
                    added.append({"id": iid, "kind": "dialogue", "start_ms": start, "end_ms": end, "text": text,
                                  "source": f"source-id={args.source_id}; path={Path(args.srt).resolve()}; sha256={digest}; index={index}",
                                  "status": "pending", "cue_ids": [], "evidence": []})
                note = f"参考SRT逐条收集：source-id={args.source_id}; path={Path(args.srt).resolve()}; sha256={digest}; count={len(rows)}；仍需实际原片复查。"
                inventory_evidence = records["coverage"].get("inventory_evidence", [])
                if not added and not registered and note in inventory_evidence:
                    print("相同参考候选已存在；保留全部原有校准及状态，未修改记录。")
                    return 0
                items.extend(added)
                records["coverage"]["expected_item_ids"] = expected
                records["coverage"]["inventory_evidence"] = inventory_evidence + ([] if note in inventory_evidence else [note])
                records["coverage"]["review"]["status"] = "pending"
                detail = {"source_id": args.source_id, "source_sha256": digest, "offset_ms": args.offset_ms, "added": len(added), "registered": len(registered)}
            elif args.command == "freeze":
                if not _text(args.evidence) or "coverage" not in records:
                    raise ValueError("freeze 需要已初始化的独立候选及非空真实收集依据。")
                cov = records["coverage"]
                expected = list(cov.get("expected_item_ids", []))
                current = [x["id"] for x in cov["items"]]
                if set(expected) - set(current):
                    raise ValueError("已有独立基准候选消失；先恢复候选，禁止缩减基准。")
                cov["expected_item_ids"] = expected + [x for x in current if x not in expected]
                cov["inventory_evidence"] = cov.get("inventory_evidence", []) + [args.evidence]
                cov["review"]["status"] = "pending"
                detail = {"registered": len(cov["expected_item_ids"]) - len(expected)}
            else:
                if not _text(args.evidence):
                    raise ValueError("review --evidence 须记录本次真实复查依据。")
                blocking = [x for x in report["issues"] if x["code"] not in REVIEW_CODES]
                if blocking:
                    print(report_text(records, report), end="")
                    return 1
                records["coverage"]["review"] = {"status": "complete", "basis_sha256": coverage_digest(records), "evidence": [args.evidence]}
            final = audit_coverage(records)
            if final["errors"]:
                raise ValueError("修改后结构异常，未写入记录。")
            records["revision"] = records.get("revision", 0) + 1
            records.setdefault("history", []).append({"at": datetime.now(timezone.utc).isoformat(),
                                                       "operation": "coverage-" + args.command, "skill_version": VERSION, **detail})
            _write(path, records)
            print(json.dumps({"records": str(path.resolve()), "revision": records["revision"], "operation": args.command, **detail}, ensure_ascii=False))
            return 0
    except (ValueError, OSError, UnicodeError, TypeError) as exc:
        print("错误：" + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
