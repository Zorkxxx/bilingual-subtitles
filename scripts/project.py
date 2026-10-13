#!/usr/bin/env python3
"""Portable task state and conflict-aware subtitle edits; Python 3.10+, stdlib only."""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
import uuid

VERSION = "1.01"
STAGES = ("inspect", "references", "transcription", "translation", "timing", "visual", "qa", "delivery")
STAGE_STATES = ("pending", "in_progress", "draft", "complete", "stale")
DEFAULT_STYLE = {"font_en": "Arial", "font_zh": "Microsoft YaHei", "font_onscreen": "Microsoft YaHei",
                 "font_size_en": 42, "font_size_zh": 46, "font_size_onscreen": 40,
                 "outline": 1.5, "margin_bottom": 60, "color": "&H00FFFFFF",
                 "dialogue_en_lines": 2, "dialogue_zh_lines": 2,
                 "dialogue_line_height": 1.35, "dialogue_gap": 8,
                 "dialogue_position": "bottom", "margin_top": 60}
DIMENSIONS = {
    "media": ("english", "translation", "timing", "visual"),
    "english": ("english", "translation", "timing"),
    "translation": ("translation",), "timing": ("timing",), "visual": ("visual", "translation"), "style": ()}
DEPENDENCIES = {
    "media": STAGES, "english": ("transcription", "translation", "timing", "qa", "delivery"),
    "translation": ("translation", "qa", "delivery"), "timing": ("timing", "visual", "qa", "delivery"),
    "visual": ("visual", "translation", "qa", "delivery"), "style": ("visual", "qa", "delivery")}


def now():
    return datetime.now(timezone.utc).isoformat()


def default_library():
    codex_data_root = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    return codex_data_root / "data" / "bilingual-subtitles" / "slang"


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=no_duplicate_keys,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"Invalid JSON constant: {value}")))


def read(path):
    obj = load_json(path)
    if not isinstance(obj, dict) or type(obj.get("schema_version")) is not int or obj.get("schema_version") != 1:
        raise ValueError(f"Unsupported schema in {path}; migrate explicitly before modifying it")
    return obj


def write(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(obj, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextlib.contextmanager
def locked(path):
    lock = Path(str(path) + ".lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ValueError(f"Busy: {lock}. Retry after the other writer finishes; inspect stale locks manually")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(f"pid={os.getpid()} created={now()}\n")
        yield
    finally:
        lock.unlink(missing_ok=True)


def fingerprint(path, full=False):
    path = Path(path).resolve(strict=True)
    stat = path.stat()
    if not path.is_file():
        raise ValueError(f"Not a file: {path}")
    result = {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
              "method": "sha256" if full else "path-size-mtime (change detector, not content identity)"}
    if full:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        result["sha256"] = digest.hexdigest()
    return result


def validate_cues(records):
    if type(records.get("schema_version")) is not int or records.get("schema_version") != 1 or not isinstance(records.get("cues"), list):
        raise ValueError("Invalid records schema/cues")
    ids = set()
    for cue in records["cues"]:
        cid = cue.get("id")
        if not isinstance(cid, str) or not cid.strip() or cid in ids:
            raise ValueError(f"Missing/duplicate cue id: {cid}")
        ids.add(cid)
        for key in ("en", "zh"):
            if not isinstance(cue.get(key), str):
                raise ValueError(f"{cid}: {key} must be a string")
        start, end = cue.get("start_ms"), cue.get("end_ms")
        if type(start) is not int or type(end) is not int or start < 0 or end <= start:
            raise ValueError(f"{cid}: invalid interval")
        if cue.get("kind", "dialogue") not in ("dialogue", "onscreen", "lyric"):
            raise ValueError(f"{cid}: invalid kind")
        if "include" in cue and type(cue["include"]) is not bool:
            raise ValueError(f"{cid}: include must be boolean")
        for key, state in cue.get("verification", {}).items():
            if key not in ("english", "translation", "timing", "visual") or not isinstance(state, dict):
                raise ValueError(f"{cid}: invalid verification dimension")
            if state.get("status") not in ("pending", "draft", "verified", "stale", "not_applicable"):
                raise ValueError(f"{cid}: invalid verification status")
            if state.get("status") == "verified" and not state.get("evidence"):
                raise ValueError(f"{cid}: verified {key} requires evidence")


def mark_cues(records, change, cue_ids=None):
    selected = set(cue_ids or [])
    known = {c["id"] for c in records["cues"]}
    if selected - known:
        raise ValueError(f"Unknown cue IDs: {sorted(selected - known)}")
    for cue in records["cues"]:
        if selected and cue["id"] not in selected:
            continue
        for dim in DIMENSIONS[change]:
            if dim == "visual" and cue.get("kind") != "onscreen":
                continue
            old = cue.setdefault("verification", {}).get(dim, {"status": "pending", "evidence": []})
            if old.get("status") != "not_applicable":
                cue["verification"][dim] = {**old, "status": "stale", "invalidated_at": now(),
                                             "reason": f"Upstream change: {change}"}


def locate(manifest, work, video_id):
    for video in manifest["videos"]:
        if video["id"] == video_id:
            path = (work / video["records"]).resolve()
            if not path.is_relative_to(work.resolve()):
                raise ValueError("Records path leaves task directory")
            return video, path
    raise ValueError(f"Unknown video ID: {video_id}")


def init(args):
    work = Path(args.work_dir).resolve()
    task_path = work / "project.json"
    paths = [Path(p).resolve(strict=True) for p in args.video]
    if len(set(paths)) != len(paths) or len({p.stem.casefold() for p in paths}) != len(paths):
        raise ValueError("Duplicate video or output basename; use separate task directories")
    work.mkdir(parents=True, exist_ok=True)
    with locked(task_path):
        if task_path.exists():
            raise ValueError("project.json already exists; use status/continue, never reinitialize over existing work")
        records_dir = work / "records"
        if records_dir.exists() and any(records_dir.iterdir()):
            raise ValueError("Nonempty records directory; inspect existing work before initializing")
        videos = []
        for path in paths:
            vid = "video-" + uuid.uuid4().hex[:12]
            fp = fingerprint(path, args.hash)
            records = {"schema_version": 1, "revision": 0, "skill_version": VERSION,
                       "video": {"id": vid, "path": str(path), "basename": path.stem,
                                 "duration_ms": None, "width": None, "height": None, "fingerprint": fp},
                       "review_scope": "尚未进行听辨、翻译或画面核验。", "sources": [], "cues": [], "pending": [],
                       "style": DEFAULT_STYLE.copy(), "style_selection": {"status": "pending"}, "history": []}
            relative = "records/" + vid + ".json"
            write(work / relative, records)
            videos.append({"id": vid, "path": str(path), "records": relative, "fingerprint": fp,
                           "stages": {s: {"status": "pending"} for s in STAGES}})
        manifest = {"schema_version": 1, "skill_version": VERSION, "task_id": uuid.uuid4().hex,
                    "created_at": now(), "revision": 0, "videos": videos,
                    "config": {"language_original": "en", "language_translation": "zh-Hans",
                               "encoding": "utf-8-sig", "newline": "CRLF", "style": DEFAULT_STYLE.copy(),
                               "onscreen_mode": "chinese", "library_path": str(Path(args.library).resolve()) if args.library else str(default_library()),
                               "decisions_revision": 4, "style_selection": {"status": "pending"}}, "glossary": [], "history": []}
        write(task_path, manifest)
    return {"project": str(task_path), "videos": [{"id": v["id"], "records": str(work / v["records"])} for v in videos]}


def status(args):
    work = Path(args.work_dir).resolve()
    manifest = read(work / "project.json")
    out = []
    for video in manifest["videos"]:
        records = read(work / video["records"])
        issues = []
        try:
            current = fingerprint(video["path"], "sha256" in video["fingerprint"])
            if current != video["fingerprint"]:
                issues.append("Input changed: explicitly invalidate media and re-probe before reusing evidence")
        except (OSError, ValueError) as exc:
            issues.append(str(exc))
        for stage, state in video["stages"].items():
            for artifact in state.get("artifacts", []):
                try:
                    if fingerprint(artifact["path"], True)["sha256"] != artifact["sha256"]:
                        issues.append(f"{stage}: artifact changed: {artifact['path']}")
                except (OSError, ValueError) as exc:
                    issues.append(f"{stage}: {exc}")
            if stage in ("qa", "delivery") and state.get("status") == "complete" and state.get("records_revision") != records.get("revision", 0):
                issues.append(f"{stage}: records changed since checkpoint; repeat acceptance/export")
        out.append({"id": video["id"], "records_revision": records.get("revision", 0), "stages": video["stages"], "issues": issues})
    return {"skill_version": manifest["skill_version"], "task_id": manifest["task_id"], "videos": out}


def mutate(args, operation):
    work = Path(args.work_dir).resolve()
    task_path = work / "project.json"
    with locked(task_path):
        manifest = read(task_path)
        video, record_path = locate(manifest, work, args.video_id)
        # Lock records as well so apply-patch cannot interleave with a checkpoint/invalidation.
        with locked(record_path):
            records = read(record_path)
            if operation == "probe":
                tool = args.ffprobe or shutil.which("ffprobe")
                if not tool:
                    raise ValueError("ffprobe unavailable; supply --ffprobe or inspect using another media tool")
                result = subprocess.run([tool, "-v", "error", "-show_streams", "-show_format", "-of", "json", video["path"]],
                                        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
                if result.returncode:
                    raise ValueError(result.stderr.strip())
                info = json.loads(result.stdout)
                streams = info.get("streams", [])
                vs = next((s for s in streams if s.get("codec_type") == "video"), None)
                if not vs:
                    raise ValueError("No video stream")
                duration = info.get("format", {}).get("duration") or vs.get("duration")
                if not duration:
                    raise ValueError("No measurable duration; inspect media manually")
                fp = fingerprint(video["path"], "sha256" in video["fingerprint"])
                if fp != video["fingerprint"]:
                    mark_cues(records, "media")
                    for stage in STAGES:
                        video["stages"][stage] = {**video["stages"][stage], "status": "stale", "reason": "media changed"}
                records["video"].update(duration_ms=round(float(duration) * 1000), width=vs["width"], height=vs["height"],
                                        fingerprint=fp, streams=streams)
                video["fingerprint"] = fp
                video["stages"]["inspect"] = {"status": "complete", "tool": tool, "at": now(), "evidence": "ffprobe metadata only"}
            elif operation == "checkpoint":
                if args.status == "complete" and not args.evidence:
                    raise ValueError("A complete stage requires --evidence describing what was actually checked")
                parameters = load_json(args.parameters) if args.parameters else {}
                artifacts = [fingerprint(p, True) for p in args.artifact]
                video["stages"][args.stage] = {"status": args.status, "at": now(), "evidence": args.evidence,
                                                "artifacts": artifacts, "parameters": parameters,
                                                "records_revision": records.get("revision", 0), "skill_version": VERSION}
            elif operation == "invalidate":
                mark_cues(records, args.change, args.cue_id)
                for stage in DEPENDENCIES[args.change]:
                    video["stages"][stage] = {**video["stages"][stage], "status": "stale", "reason": args.reason, "at": now()}
            if operation != "checkpoint":
                records["revision"] = records.get("revision", 0) + 1
                records.setdefault("history", []).append({"at": now(), "operation": operation, "reason": getattr(args, "reason", None)})
                validate_cues(records)
                write(record_path, records)
        manifest["revision"] += 1
        manifest.setdefault("history", []).append({"at": now(), "operation": operation, "video_id": args.video_id})
        write(task_path, manifest)
    return {"video_id": args.video_id, "records": str(record_path), "records_revision": records["revision"], "operation": operation}


def apply_patch(args):
    path = Path(args.records).resolve(strict=True)
    patch = load_json(args.patch)
    if not isinstance(patch, dict) or type(patch.get("base_revision")) is not int:
        raise ValueError("Patch base_revision must be an integer")
    with locked(path):
        original = read(path)
        if patch.get("base_revision") != original.get("revision", 0):
            raise ValueError(f"Conflict: base revision {patch.get('base_revision')} != current {original.get('revision', 0)}")
        if not patch.get("reason"):
            raise ValueError("Patch requires a reason")
        records = copy.deepcopy(original)
        cues = {c["id"]: c for c in records["cues"]}
        touched = set()
        allowed = {"en", "zh", "start_ms", "end_ms", "include", "kind", "speaker", "verification", "unresolved", "onscreen", "slang_ids", "slang_refs", "evidence", "notes"}
        for edit in patch.get("edits", []):
            cid = edit["cue_id"]
            if cid not in cues or cid in touched:
                raise ValueError(f"Unknown or repeated edit: {cid}")
            touched.add(cid)
            cue = cues[cid]
            values, expected = edit.get("set", {}), edit.get("expected", {})
            if not values or set(values) - allowed or set(values) - set(expected):
                raise ValueError(f"{cid}: editable fields must each have an expected prior value")
            for key, value in expected.items():
                if cue.get(key) != value:
                    raise ValueError(f"Conflict: {cid}.{key} does not match expected value")
            previous = copy.deepcopy(cue)
            cue.update(values)
            changes = []
            if previous["en"] != cue["en"]:
                changes.append("visual" if cue.get("kind") == "onscreen" else "english")
            if previous["zh"] != cue["zh"]:
                changes.append("translation")
            if any(previous[k] != cue[k] for k in ("start_ms", "end_ms")):
                changes.append("timing")
            if previous.get("onscreen") != cue.get("onscreen"):
                changes.append("visual")
            for change in changes:
                mark_cues(records, change, [cid])
        for cue in patch.get("add", []):
            if cue.get("id") in cues:
                raise ValueError(f"Conflict: addition ID already exists: {cue.get('id')}")
            records["cues"].append(cue)
            cues[cue.get("id")] = cue
        if not patch.get("edits") and not patch.get("add"):
            raise ValueError("Empty patch")
        validate_cues(records)
        records["revision"] = records.get("revision", 0) + 1
        records.setdefault("history", []).append({"at": now(), "operation": "apply-patch", "base_revision": patch["base_revision"],
                                                   "reason": patch["reason"], "cue_ids": sorted(touched), "added": [c["id"] for c in patch.get("add", [])]})
        write(path, records)
    return {"records": str(path), "revision": records["revision"], "changed": sorted(touched), "added": len(patch.get("add", [])),
            "note": "Changed content is marked stale; record fresh evidence in a separate verification-only patch after rechecking."}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    p = subs.add_parser("init", help="Create task metadata; does not transcribe or translate")
    p.add_argument("--work-dir", required=True)
    p.add_argument("--video", action="append", required=True)
    p.add_argument("--library")
    p.add_argument("--hash", action="store_true", help="Hash complete input files instead of stat-based change detection")
    p = subs.add_parser("status")
    p.add_argument("--work-dir", required=True)
    for command in ("probe", "checkpoint", "invalidate"):
        p = subs.add_parser(command)
        p.add_argument("--work-dir", required=True)
        p.add_argument("--video-id", required=True)
        if command == "probe":
            p.add_argument("--ffprobe")
        elif command == "checkpoint":
            p.add_argument("--stage", choices=STAGES, required=True)
            p.add_argument("--status", choices=STAGE_STATES, required=True)
            p.add_argument("--evidence")
            p.add_argument("--artifact", action="append", default=[])
            p.add_argument("--parameters", help="JSON file with actual tool/model parameters")
        else:
            p.add_argument("--change", choices=DIMENSIONS, required=True)
            p.add_argument("--cue-id", action="append", default=[])
            p.add_argument("--reason", required=True)
    p = subs.add_parser("apply-patch")
    p.add_argument("--records", required=True)
    p.add_argument("--patch", required=True)
    subs.add_parser("preflight", help="Read-only runtime inventory; does not install tools")
    args = parser.parse_args()
    try:
        if args.command == "init":
            result = init(args)
        elif args.command == "status":
            result = status(args)
        elif args.command == "apply-patch":
            result = apply_patch(args)
        elif args.command == "preflight":
            result = {"python": sys.executable, "python_version": sys.version.split()[0],
                      "tools": {x: shutil.which(x) for x in ("ffmpeg", "ffprobe", "tesseract")},
                      "optional_modules": {x: importlib.util.find_spec(x) is not None for x in ("faster_whisper", "PIL", "pytesseract", "paddleocr")},
                      "default_library": str(default_library()),
                      "note": "Presence is not a model, GPU, audio-access, OCR or rendering test. Supply tool paths explicitly when not on PATH."}
        else:
            result = mutate(args, args.command)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
