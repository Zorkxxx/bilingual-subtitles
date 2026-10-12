#!/usr/bin/env python3
"""A local, versioned subtitle slang library. Python 3.10+, standard library only."""

from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata
import uuid


SCHEMA_VERSION = 1
STATUSES = ("candidate", "verified", "deprecated")
BASES = ("audio_check", "reference_check", "human_correction")
SEMANTIC_FIELDS = {
    "expression", "variants", "sense", "region", "era", "register", "relationship",
    "scope", "work", "translations", "evidence", "verification",
}


class LibraryError(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def integer(value, label: str, minimum: int = 0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise LibraryError(f"{label} must be an integer >= {minimum}")


def string(value, label: str, nonempty: bool = False):
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise LibraryError(f"{label} must be {'a nonempty' if nonempty else 'a'} string")


def clean_json(value, label: str = "document"):
    """Reject damaged Unicode and control characters, including in extra fields."""
    if isinstance(value, str):
        for char in value:
            code = ord(char)
            if (char == "\ufffd" or 0xD800 <= code <= 0xDFFF or
                    code in {0xFFFE, 0xFFFF} or
                    (unicodedata.category(char) == "Cc" and char not in "\n\r\t")):
                raise LibraryError(f"{label} contains a control or damaged Unicode character")
        if "锟斤拷" in value:
            raise LibraryError(f"{label} contains a common damaged-decoding marker")
    elif isinstance(value, dict):
        for key, item in value.items():
            string(key, f"{label} key")
            clean_json(key, f"{label} key")
            clean_json(item, f"{label}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            clean_json(item, f"{label}[{index}]")


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LibraryError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path: Path):
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            result = json.load(stream, object_pairs_hook=no_duplicate_keys,
                               parse_constant=lambda value: (_ for _ in ()).throw(
                                   LibraryError(f"invalid JSON number: {value}")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LibraryError(f"cannot read UTF-8 JSON {path}: {exc}") from exc
    clean_json(result)
    return result


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = value.translate(str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"',
                                         "–": "-", "—": "-"}))
    return " ".join(value.split())


def validate_entry(entry, label: str = "entry"):
    if not isinstance(entry, dict):
        raise LibraryError(f"{label} must be an object")
    for field in ("id", "expression", "sense"):
        string(entry.get(field), f"{label}.{field}", nonempty=True)
    for field in ("region", "era", "register", "relationship", "work"):
        string(entry.get(field), f"{label}.{field}")
    if entry.get("scope") not in ("general", "work"):
        raise LibraryError(f"{label}.scope must be general or work")
    if entry["scope"] == "work" and not entry["work"].strip():
        raise LibraryError(f"{label}.work is required for work scope")
    if entry["scope"] == "general" and entry["work"].strip():
        raise LibraryError(f"{label}.work must be empty for general scope")
    if entry.get("status") not in STATUSES:
        raise LibraryError(f"{label}.status is invalid")
    integer(entry.get("revision"), f"{label}.revision", 1)
    variants = entry.get("variants")
    if not isinstance(variants, list):
        raise LibraryError(f"{label}.variants must be an array")
    for item in variants:
        string(item, f"{label}.variants[]", nonempty=True)
    translations = entry.get("translations")
    if not isinstance(translations, list):
        raise LibraryError(f"{label}.translations must be an array")
    for item in translations:
        if not isinstance(item, dict):
            raise LibraryError(f"{label}.translations[] must be an object")
        string(item.get("text"), f"{label}.translations[].text", nonempty=True)
        string(item.get("conditions", ""), f"{label}.translations[].conditions")
    evidence = entry.get("evidence")
    if not isinstance(evidence, list):
        raise LibraryError(f"{label}.evidence must be an array")
    for item in evidence:
        if not isinstance(item, dict):
            raise LibraryError(f"{label}.evidence[] must be an object")
        string(item.get("source"), f"{label}.evidence[].source", nonempty=True)
        for field in ("episode", "cue_id", "note"):
            if field in item:
                string(item[field], f"{label}.evidence[].{field}")
        if "time_ms" in item:
            integer(item["time_ms"], f"{label}.evidence[].time_ms")
    if not isinstance(entry.get("usage_refs"), list):
        raise LibraryError(f"{label}.usage_refs must be an array")
    for item in entry["usage_refs"]:
        if not isinstance(item, dict):
            raise LibraryError(f"{label}.usage_refs[] must be an object")
    for field in ("verification", "submitted_verification"):
        if field in entry and not isinstance(entry[field], dict):
            raise LibraryError(f"{label}.{field} must be an object")
    if entry["status"] == "verified":
        validate_verification(entry)
    clean_json(entry, label)


def validate_verification(entry):
    verification = entry.get("verification")
    if not isinstance(verification, dict):
        raise LibraryError("verified status requires verification={basis,source,note}")
    if verification.get("basis") not in BASES:
        raise LibraryError("verification.basis must be audio_check, reference_check or human_correction")
    for field in ("source", "note"):
        string(verification.get(field), f"verification.{field}", nonempty=True)
    if verification.get("invalidated_at"):
        raise LibraryError("an invalidated verification cannot authorize verified status")
    if not any(item.get("source") == verification["source"] for item in entry["evidence"]):
        raise LibraryError("verification.source must identify one of the entry's evidence sources")


def dedup_key(entry):
    return tuple(normalize(entry[key]) for key in ("expression", "sense", "scope", "work"))


def validate_library(document):
    if (not isinstance(document, dict) or type(document.get("schema_version")) is not int
            or document.get("schema_version") != SCHEMA_VERSION):
        raise LibraryError("unsupported library schema_version; migrate explicitly before use")
    integer(document.get("revision"), "library.revision")
    if not isinstance(document.get("entries"), list):
        raise LibraryError("library.entries must be an array")
    if not isinstance(document.get("history", []), list):
        raise LibraryError("library.history must be an array")
    ids, keys = set(), set()
    for entry in document["entries"]:
        validate_entry(entry)
        if entry["id"] in ids:
            raise LibraryError(f"duplicate entry id: {entry['id']}")
        key = dedup_key(entry)
        if key in keys:
            raise LibraryError(f"duplicate expression/sense/scope/work: {entry['id']}")
        ids.add(entry["id"])
        keys.add(key)
    clean_json(document)


def load_library(directory: Path):
    document = read_json(directory / "library.json")
    validate_library(document)
    return document


@contextmanager
def exclusive(directory: Path):
    if not directory.is_dir():
        raise LibraryError(f"library directory does not exist: {directory}; run init first")
    lock = directory / "library.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise LibraryError(f"library is locked: {lock}; retry after the other writer finishes. "
                           "For a stale lock, confirm no writer is running before removing it.") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "created_at": now()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        lock.unlink(missing_ok=True)


def atomic_write(directory: Path, document):
    validate_library(document)
    handle, name = tempfile.mkstemp(prefix=".library-", suffix=".tmp", dir=directory)
    temporary = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / "library.json")
    finally:
        temporary.unlink(missing_ok=True)


def record_change(document, action: str, before, after, reason: str):
    document["revision"] += 1
    document.setdefault("history", []).append({
        "action": action, "at": now(), "reason": reason,
        "entry_id": after["id"], "library_revision": document["revision"],
        "entry_revision": after["revision"],
        "previous_entry_revision": before["revision"] if before else None,
        "before": copy.deepcopy(before), "after": copy.deepcopy(after),
    })


def initialize(args):
    directory = Path(args.library).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    with exclusive(directory):
        if (directory / "library.json").exists():
            document = load_library(directory)
            return {"action": "existing", "library": str(directory), "revision": document["revision"]}
        document = {"schema_version": SCHEMA_VERSION, "revision": 0, "entries": [], "history": []}
        atomic_write(directory, document)
    return {"action": "initialized", "library": str(directory), "revision": 0}


def add(args):
    entry = read_json(Path(args.entry))
    if not isinstance(entry, dict):
        raise LibraryError("entry file must contain an object")
    requested_status = entry.get("status", "candidate")
    if requested_status not in STATUSES:
        raise LibraryError("entry.status is invalid")
    if "revision" in entry:
        raise LibraryError("new entry revision is assigned by the library; omit revision")
    entry.setdefault("id", f"slang-{uuid.uuid4().hex}")
    for field in ("variants", "translations", "evidence", "usage_refs"):
        entry.setdefault(field, [])
    for field in ("region", "era", "register", "relationship", "work"):
        entry.setdefault(field, "")
    entry.setdefault("scope", "general")
    entry["status"], entry["revision"] = "candidate", 1
    entry["created_at"] = entry["updated_at"] = now()
    if "verification" in entry:
        entry["submitted_verification"] = entry.pop("verification")
    validate_entry(entry)
    directory = Path(args.library).expanduser().resolve()
    with exclusive(directory):
        document = load_library(directory)
        for existing in document["entries"]:
            if existing["id"] == entry["id"]:
                raise LibraryError(f"id already exists: {entry['id']}; use revise")
            if dedup_key(existing) == dedup_key(entry):
                raise LibraryError(f"duplicate expression/sense/scope/work matches id {existing['id']}; "
                                   "existing entry was not changed")
        document["entries"].append(entry)
        record_change(document, "add", None, entry, "new candidate; verification requires explicit revise")
        atomic_write(directory, document)
    return {"action": "added", "entry": entry, "library_revision": document["revision"],
            "requested_status": requested_status, "verification_required": True}


def revise(args):
    patch = read_json(Path(args.patch))
    if not isinstance(patch, dict) or not patch:
        raise LibraryError("patch must be a nonempty object")
    string(args.reason, "reason", nonempty=True)
    reserved = {"id", "revision", "created_at", "updated_at"} & patch.keys()
    if reserved:
        raise LibraryError(f"patch cannot change immutable/managed fields: {', '.join(sorted(reserved))}")
    expected = patch.pop("_expected_revision", None)
    if expected is not None:
        integer(expected, "_expected_revision", 1)
    if not patch:
        raise LibraryError("patch has no changes after its expected revision is removed")
    directory = Path(args.library).expanduser().resolve()
    with exclusive(directory):
        document = load_library(directory)
        index = next((i for i, item in enumerate(document["entries"]) if item["id"] == args.id), None)
        if index is None:
            raise LibraryError(f"unknown entry id: {args.id}")
        before = copy.deepcopy(document["entries"][index])
        if expected is not None and expected != before["revision"]:
            raise LibraryError(f"stale patch: expected entry revision {expected}, actual {before['revision']}")
        after = copy.deepcopy(before)
        after.update(patch)
        if "verification" in after and not isinstance(after["verification"], dict):
            raise LibraryError("verification must be an object")
        changed = sorted(field for field in SEMANTIC_FIELDS if after.get(field) != before.get(field))
        invalidated = False
        if patch.get("status") == "verified":
            if "verification" not in patch:
                raise LibraryError("explicit verified revision requires a new verification object in the patch")
            validate_entry(after)
        elif before["status"] == "verified" and changed and after["status"] != "deprecated":
            after["status"] = "candidate"
            after.setdefault("verification", {})["invalidated_at"] = now()
            after["verification"]["invalidated_fields"] = changed
            invalidated = True
        after["revision"] += 1
        after["updated_at"] = now()
        validate_entry(after)
        document["entries"][index] = after
        record_change(document, "revise", before, after, args.reason)
        atomic_write(directory, document)
    return {"action": "revised", "entry": after, "library_revision": document["revision"],
            "verification_invalidated": invalidated, "changed_context_fields": changed,
            "check_affected_cues": True}


def contains_phrase(query: str, expression: str) -> bool:
    escaped = re.escape(normalize(expression)).replace(r"\ ", r"\s+")
    return bool(escaped and re.search(r"(?<!\w)" + escaped + r"(?!\w)", query))


def search(args):
    document = load_library(Path(args.library).expanduser().resolve())
    string(args.query, "query", nonempty=True)
    query = normalize(args.query)
    query_tokens = set(re.findall(r"\w+", query))
    results = []
    for entry in document["entries"]:
        if entry["status"] == "deprecated":
            continue
        if entry["status"] != "verified" and not args.include_candidates:
            continue
        if entry["scope"] == "work" and (not args.work or normalize(entry["work"]) != normalize(args.work)):
            continue
        if contains_phrase(query, entry["expression"]):
            score, match = 100, "expression"
        elif any(contains_phrase(query, item) for item in entry["variants"]):
            score, match = 90, "variant"
        else:
            expression_tokens = set(re.findall(r"\w+", normalize(entry["expression"])))
            overlap = expression_tokens & query_tokens
            if not overlap:
                continue
            score, match = round(35 * len(overlap) / max(1, len(expression_tokens))), "partial_keywords"
        hints = []
        if entry["scope"] == "work":
            score += 10
            hints.append("matching work scope")
        if args.region and entry["region"]:
            if normalize(args.region) == normalize(entry["region"]):
                score += 5
                hints.append("matching region label")
            else:
                score -= 5
                hints.append("region differs; check applicability")
        if entry["status"] == "candidate":
            hints.append("UNVERIFIED CANDIDATE")
        results.append({"score": score, "match": match, "context_hints": hints, "entry": entry})
    results.sort(key=lambda item: (-item["score"], item["entry"]["id"]))
    return {"library_revision": document["revision"], "query": args.query, "results": results,
            "retrieval": "literal phrases/variants/partial keywords; no semantic or context verification"}


def impacts(args):
    document = load_library(Path(args.library).expanduser().resolve())
    entry = next((item for item in document["entries"] if item["id"] == args.id), None)
    if entry is None:
        raise LibraryError(f"unknown entry id: {args.id}")
    affected = []
    scanned = []
    for name in args.records:
        path = Path(name).expanduser().resolve()
        records = read_json(path)
        if not isinstance(records, dict) or not isinstance(records.get("cues"), list):
            raise LibraryError(f"records must be an object containing cues[]: {path}")
        scanned.append(str(path))
        for cue in records["cues"]:
            if not isinstance(cue, dict):
                raise LibraryError(f"every cue must be an object: {path}")
            ids = cue.get("slang_ids", [])
            if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
                raise LibraryError(f"cue.slang_ids must be an array of strings: {path}")
            if args.id in ids:
                cue_id = cue.get("id", cue.get("cue_id"))
                string(cue_id, "affected cue.id", nonempty=True)
                affected.append({"records": str(path), "cue_id": cue_id, "needs_review": True})
    return {"entry_id": entry["id"], "entry_revision": entry["revision"],
            "entry_status": entry["status"], "scanned_records": scanned,
            "affected": affected, "modified_records": False,
            "limitation": "only explicit slang_ids links are found; unlinked usages require separate review"}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    command = commands.add_parser("init", help="create an empty local library")
    command.add_argument("--library", required=True)
    command.set_defaults(run=initialize)
    command = commands.add_parser("add", help="add a candidate without overwriting duplicates")
    command.add_argument("--library", required=True)
    command.add_argument("--entry", required=True)
    command.set_defaults(run=add)
    command = commands.add_parser("search", help="literal/context-labelled retrieval")
    command.add_argument("--library", required=True)
    command.add_argument("--query", required=True)
    command.add_argument("--work")
    command.add_argument("--region")
    command.add_argument("--include-candidates", action="store_true")
    command.set_defaults(run=search)
    command = commands.add_parser("revise", help="revise with preserved history and evidence")
    command.add_argument("--library", required=True)
    command.add_argument("--id", required=True)
    command.add_argument("--patch", required=True)
    command.add_argument("--reason", required=True)
    command.set_defaults(run=revise)
    command = commands.add_parser("impacts", help="find affected cue IDs without rewriting subtitles")
    command.add_argument("--library", required=True)
    command.add_argument("--id", required=True)
    command.add_argument("--records", required=True, action="append")
    command.set_defaults(run=impacts)
    return result


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = parser().parse_args()
    try:
        output = args.run(args)
    except (LibraryError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
