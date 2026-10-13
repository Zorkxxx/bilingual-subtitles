#!/usr/bin/env python3
"""Render comparable subtitle style PNGs, then save an actual user selection.

Python 3.10+, standard library and FFmpeg/libass. No transcription, generative
images, font installation, automatic font replacement, or verification upgrade.
Call select only after the user has replied to the displayed previews.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import subtitles

TOOL_VERSION = "1.01"
GROUP_FIELDS = {
    "size": {"font_size_en", "font_size_zh", "font_size_onscreen"},
    "font": {"font_en", "font_zh", "font_onscreen"},
    "position": {"dialogue_position", "margin_bottom", "margin_top", "dialogue_gap",
                 "dialogue_en_lines", "dialogue_zh_lines", "dialogue_line_height"},
}


class PreviewError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(data):
    return subtitles.sha256(data)


def full_style(records):
    return subtitles.effective_style(records)


def style_hash(style):
    return subtitles.style_digest({"style": style})


def load_records(path):
    subtitles.reject_links(path)
    data = subtitles.read_json(path)
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise PreviewError("Unsupported records schema; migrate explicitly before changing it.")
    if not isinstance(data.get("style", {}), dict):
        raise PreviewError("records.style must be an object.")
    return data


def dimensions(records):
    video = records.get("video", {})
    width, height = video.get("width"), video.get("height")
    if type(width) is not int or type(height) is not int or min(width, height) <= 0:
        raise PreviewError("Probe the video first: previews need positive actual video.width and video.height.")
    return width, height


def read_options(path):
    options = subtitles.read_json(path)
    if not isinstance(options, list) or not options:
        raise PreviewError("Options must be a nonempty JSON array.")
    ids, groups = set(), set()
    for option in options:
        if not isinstance(option, dict):
            raise PreviewError("Each option must be an object.")
        option_id, group = option.get("id"), option.get("group")
        if not isinstance(option_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", option_id):
            raise PreviewError("Option IDs must be safe ASCII identifiers (1–64 characters).")
        if option_id.casefold() in ids:
            raise PreviewError(f"Duplicate option ID: {option_id}")
        ids.add(option_id.casefold())
        if group not in GROUP_FIELDS:
            raise PreviewError(f"{option_id}: group must be size, font, or position.")
        groups.add(group)
        if not isinstance(option.get("label"), str) or not option["label"].strip():
            raise PreviewError(f"{option_id}: label is required.")
        patch = option.get("style")
        if not isinstance(patch, dict) or not patch or not set(patch) <= GROUP_FIELDS[group]:
            raise PreviewError(f"{option_id}: {group} option changes fields outside its group or has an empty patch.")
    if groups != set(GROUP_FIELDS):
        raise PreviewError("Provide at least one option in each of size, font, and position.")
    return options


def example(records):
    items = sorted((cue for cue in records.get("cues", [])
                    if isinstance(cue, dict) and cue.get("include", True)
                    and cue.get("kind") in {"dialogue", "lyric"}
                    and isinstance(cue.get("en"), str) and cue["en"].strip()
                    and isinstance(cue.get("zh"), str) and cue["zh"].strip()),
                   key=lambda item: (item.get("start_ms", 0), item.get("id", "")))
    if items:
        return {"en": items[0]["en"], "zh": items[0]["zh"],
                "kind": "recorded_dialogue", "source_cue_id": items[0].get("id")}
    return {"en": "[STYLE SAMPLE] This is a subtitle style preview.",
            "zh": "【样式示例】这是字幕字号、字体与位置效果。",
            "kind": "labelled_example", "source_cue_id": None}


def sample_records(original, style, sample):
    data = copy.deepcopy(original)
    data.pop("style_selection", None)
    data["style"] = style
    # Preview has an artificial timeline; original cue timing/status is untouched.
    data["video"]["duration_ms"] = 2000
    data["cues"] = [{"id": "style-preview-sample", "kind": "dialogue", "start_ms": 0, "end_ms": 2000,
                     "en": sample["en"], "zh": sample["zh"], "include": True,
                     "verification": {name: {"status": "pending", "evidence": []}
                                      for name in ("english", "translation", "timing", "visual")}}]
    data["pending"] = []
    report = subtitles.lint_records(data)
    if report["errors"]:
        raise PreviewError("Invalid preview style/records: " + "; ".join(item["message"] for item in report["errors"]))
    return data, report["warnings"]


def executable(path):
    resolved = Path(path).expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise PreviewError("FFmpeg path is not a file.")
    completed = subprocess.run([str(resolved), "-version"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=15, check=False)
    version = completed.stdout.splitlines()[0] if completed.stdout else ""
    if completed.returncode or not version.startswith("ffmpeg version"):
        raise PreviewError("The selected executable did not identify itself as FFmpeg.")
    return resolved, {"executable": str(resolved), "version": version, "subtitle_renderer": "FFmpeg ass/libass"}


def run_ffmpeg(ffmpeg, arguments, cwd, log_name):
    completed = subprocess.run([str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "info", "-n", *arguments],
                               cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=60, check=False)
    log = completed.stderr + completed.stdout
    (cwd / log_name).write_text(log, encoding="utf-8")
    if completed.returncode:
        raise PreviewError(f"FFmpeg failed (exit {completed.returncode}); inspect {cwd / log_name}: " + log[-1200:])
    return log


def png_dimensions(path):
    header = path.read_bytes()[:24]
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise PreviewError(f"FFmpeg did not produce a valid PNG: {path}")
    return struct.unpack(">II", header[16:24])


def background(ffmpeg, stage, width, height, frame):
    if frame and Path(frame).is_file():
        frame_path = Path(frame).resolve(strict=True)
        subtitles.reject_links(frame_path)
        run_ffmpeg(ffmpeg, ["-i", str(frame_path), "-vf",
                           f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
                           "-frames:v", "1", "-update", "1", "background.png"], stage, "background-render.log")
        info = {"kind": "provided_frame", "source": str(frame_path), "source_sha256": sha(frame_path.read_bytes()),
                "note": "Caller-provided image; source-video identity is not verified by this tool."}
    else:
        run_ffmpeg(ffmpeg, ["-f", "lavfi", "-i", f"color=c=0x1b2330:s={width}x{height}:r=1",
                           "-vf", f"drawbox=x=0:y={height // 3}:w=iw:h={height // 3}:color=0x29384a:t=fill",
                           "-frames:v", "1", "-update", "1", "background.png"], stage, "background-render.log")
        info = {"kind": "abstract", "source": None, "requested_frame": str(frame) if frame else None,
                "note": "示意背景，非实际片源。"}
    if png_dimensions(stage / "background.png") != (width, height):
        raise PreviewError("Background resolution differs from records.video.")
    info.update({"sha256": sha((stage / "background.png").read_bytes()), "width": width, "height": height})
    return info


def font_log(log):
    selections, warnings = [], []
    for line in log.splitlines():
        if "fontselect:" in line:
            selections.append(line.strip())
            match = re.search(r"fontselect: \((.*?),\s*\d+,\s*\d+\) -> (.*?),\s*\d+,\s*(.*)", line)
            if match:
                requested, resolved, postscript = match.groups()
                normal = lambda value: re.sub(r"[^a-z0-9]", "", value.lower())
                family = normal(requested)
                if not any(normal(value).startswith(family) for value in (resolved, postscript)):
                    warnings.append("Possible font substitution: " + line.strip())
            else:
                warnings.append("Font selection log could not be interpreted; manual review required: " + line.strip())
        if re.search(r"glyph .*not found|failed to find.*fallback|font.*not found|font.*substitut", line, re.I):
            warnings.append(line.strip())
    if not selections:
        warnings.append("Renderer produced no fontselect log; inspect font resolution manually.")
    return selections, list(dict.fromkeys(warnings))


def render_one(original, style, sample, bg_info, ffmpeg, stage, stem):
    data, lint_warnings = sample_records(original, style, sample)
    ass_text = subtitles.render_ass(data, data["cues"])
    width, height = dimensions(original)
    label = "【示意背景，非实际片源】" if bg_info["kind"] == "abstract" else "【字幕样式预览】"
    # Fixed small corner label is informational, separate from the final layout.
    size = max(12, round(22 * height / 1080))
    label_text = "{\\an7\\pos(12,12)\\fs" + str(size) + "}" + subtitles.ass_escape(label)
    ass_text += f"Dialogue: 9,0:00:00.00,0:00:02.00,OnscreenChinese,,0,0,0,,{label_text}\n"
    (stage / f"{stem}.ass").write_bytes(subtitles.subtitle_bytes(ass_text))
    log = run_ffmpeg(ffmpeg, ["-i", "background.png", "-vf", f"ass=filename={stem}.ass",
                              "-frames:v", "1", "-update", "1", f"{stem}.png"], stage, f"{stem}.ffmpeg.log")
    if png_dimensions(stage / f"{stem}.png") != (width, height):
        raise PreviewError("Preview PNG dimensions differ from the final ASS resolution.")
    selections, warnings = font_log(log)
    return {"image_sha256": sha((stage / f"{stem}.png").read_bytes()),
            "actual_fontselect": selections, "font_warnings": warnings, "lint_warnings": lint_warnings}


def clean_stage(stage, parent):
    if stage.resolve().parent != parent.resolve():
        raise PreviewError("Unexpected staging location.")
    for item in stage.iterdir():
        if not item.is_file() or item.is_symlink():
            raise PreviewError("Unexpected item in staging directory.")
        item.unlink()
    stage.rmdir()


def publish_stage(stage, out):
    for path in sorted(stage.iterdir()):
        subtitles.atomic_write(out / path.name, path.read_bytes(), force=False)


def render(args):
    records_path, out = Path(args.records).resolve(strict=True), Path(args.out).resolve()
    original_bytes = records_path.read_bytes()
    original = load_records(records_path)
    width, height = dimensions(original)
    options, sample, base = read_options(args.options), example(original), full_style(original)
    ffmpeg, renderer = executable(args.ffmpeg)
    subtitles.reject_links(out)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise PreviewError("Preview output must be a new or empty directory; existing previews are never overwritten.")
    out.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".preview-", dir=out))
    try:
        bg_info = background(ffmpeg, stage, width, height, args.frame)
        bg_info["image"] = str(out / "background.png")
        rendered_options = []
        for option in options:
            style = {**base, **option["style"]}
            stem = "option-" + option["id"]
            result = render_one(original, style, sample, bg_info, ffmpeg, stage, stem)
            rendered_options.append({**option, "effective_style": style, "style_sha256": style_hash(style),
                                     "image": str(out / f"{stem}.png"), "ass": str(out / f"{stem}.ass"),
                                     "render_log": str(out / f"{stem}.ffmpeg.log"), **result})
        if records_path.read_bytes() != original_bytes:
            raise PreviewError("Records changed while rendering; no preview manifest was committed.")
        manifest = {"schema_version": 1, "tool_version": TOOL_VERSION, "created_at": now(),
                    "records": str(records_path), "records_sha256": sha(original_bytes),
                    "base_style": base, "base_style_sha256": style_hash(base),
                    "background": bg_info, "renderer": renderer, "sample": sample,
                    "options": rendered_options, "font_validation": "Caller must check actual fonts and glyph coverage with fonts.py; fontselect logs are recorded here."}
        publish_stage(stage, out)
        manifest_path = out / "preview-manifest.json"
        subtitles.atomic_write(manifest_path, subtitles.json_bytes(manifest), force=False)
        return {"manifest": str(manifest_path), "background_kind": bg_info["kind"],
                "options": [{"id": item["id"], "group": item["group"], "image": item["image"],
                             "font_warnings": item["font_warnings"]} for item in rendered_options],
                "selection_status": "pending", "records_modified": False}
    finally:
        clean_stage(stage, out)


@contextlib.contextmanager
def record_lock(path):
    lock = Path(str(path) + ".lock")
    subtitles.reject_links(lock)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise PreviewError(f"Records are busy: {lock}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"preview pid={os.getpid()} created={now()}\n")
        yield
    finally:
        lock.unlink(missing_ok=True)


def checked_asset(value, parent, expected_hash):
    path = Path(value).resolve(strict=True)
    if path.parent != parent.resolve():
        raise PreviewError("Manifest preview assets must be in its own directory.")
    subtitles.reject_links(path)
    if sha(path.read_bytes()) != expected_hash:
        raise PreviewError(f"Preview asset changed: {path}")
    return path


def select(args):
    confirmation = args.confirmation.strip()
    if not confirmation:
        raise PreviewError("A nonempty actual user confirmation is required; never manufacture a reply.")
    records_path = Path(args.records).resolve(strict=True)
    manifest_path = Path(args.manifest).resolve(strict=True)
    subtitles.reject_links(manifest_path)
    manifest = subtitles.read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or manifest.get("tool_version") != TOOL_VERSION:
        raise PreviewError("Unsupported preview manifest.")
    out = manifest_path.parent
    selected_image = out / "selected-preview.png"
    if any((out / ("selected-preview" + suffix)).exists() for suffix in (".png", ".ass", ".ffmpeg.log")):
        raise PreviewError("A selected preview already exists; render a new preview session to revise the choice.")
    ffmpeg, renderer = executable(args.ffmpeg or manifest["renderer"]["executable"])
    with record_lock(records_path):
        original = load_records(records_path)
        original_bytes = records_path.read_bytes()
        if Path(manifest["records"]).resolve() != records_path or sha(original_bytes) != manifest["records_sha256"]:
            raise PreviewError("Records changed since the previews; render a fresh session before confirming.")
        base = full_style(original)
        if style_hash(base) != manifest["base_style_sha256"] or base != manifest["base_style"]:
            raise PreviewError("The preview base style no longer matches these records.")
        option_ids = {"size": args.size, "font": args.font, "position": args.position}
        candidates = manifest.get("options", [])
        # Reuse the same group/ID validation for saved options without mutating them.
        original_options = [{key: item.get(key) for key in ("id", "label", "group", "style")} for item in candidates]
        ids = [item["id"] for item in original_options]
        if len(ids) != len(set(ids)):
            raise PreviewError("Duplicate IDs in preview manifest.")
        style = base.copy()
        for group, option_id in option_ids.items():
            matches = [item for item in candidates if item.get("id") == option_id and item.get("group") == group]
            if len(matches) != 1:
                raise PreviewError(f"No unique {group} option named {option_id}.")
            item = matches[0]
            patch = item.get("style")
            if not isinstance(patch, dict) or not patch or not set(patch) <= GROUP_FIELDS[group]:
                raise PreviewError(f"{option_id}: invalid cross-group style patch.")
            if item.get("effective_style") != {**base, **patch} or item.get("style_sha256") != style_hash({**base, **patch}):
                raise PreviewError(f"{option_id}: style metadata is inconsistent.")
            checked_asset(item["image"], out, item["image_sha256"])
            style.update(patch)
        bg_info = manifest["background"]
        background_path = checked_asset(bg_info["image"], out, bg_info["sha256"])
        if png_dimensions(background_path) != dimensions(original):
            raise PreviewError("Preview background resolution changed.")
        if type(original.get("revision", 0)) is not int or not isinstance(original.get("history", []), list):
            raise PreviewError("Records revision/history has an unsupported structure.")
        stage = Path(tempfile.mkdtemp(prefix=".selection-", dir=out))
        try:
            (stage / "background.png").write_bytes(background_path.read_bytes())
            result = render_one(original, style, manifest["sample"], bg_info, ffmpeg, stage, "selected-preview")
            if result["font_warnings"]:
                raise PreviewError("Selected fonts require review; no confirmation was saved. "
                                   "Choose a new font option with validated actual fonts/glyphs: "
                                   + "; ".join(result["font_warnings"]))
            updated = copy.deepcopy(original)
            updated["style"] = style
            updated["style_selection"] = {"status": "confirmed", "style_sha256": style_hash(style),
                "option_ids": option_ids, "confirmation": confirmation, "preview_image": str(selected_image),
                "preview_sha256": result["image_sha256"], "confirmed_at": now(), "renderer": renderer,
                "actual_fontselect": result["actual_fontselect"], "font_warnings": result["font_warnings"]}
            updated["revision"] = original.get("revision", 0) + 1
            updated.setdefault("history", []).append({"action": "style_selection", "at": now(),
                "tool_version": TOOL_VERSION, "before_style_sha256": style_hash(base),
                "style_sha256": style_hash(style), "option_ids": option_ids, "confirmation": confirmation,
                "preview_image": str(selected_image), "preview_sha256": result["image_sha256"]})
            # Validate the same style hash contract used by final export.
            check, _ = sample_records(updated, style, manifest["sample"])
            check["style_selection"] = updated["style_selection"]
            errors = subtitles.lint_records(check)["errors"]
            if errors:
                raise PreviewError("Selection validation failed: " + "; ".join(item["message"] for item in errors))
            if records_path.read_bytes() != original_bytes:
                raise PreviewError("Records changed during rendering; no selection was saved.")
            for suffix in (".png", ".ass", ".ffmpeg.log"):
                path = stage / ("selected-preview" + suffix)
                subtitles.atomic_write(out / path.name, path.read_bytes(), force=False)
            subtitles.atomic_write(records_path, subtitles.json_bytes(updated), force=True)
            return {"records": str(records_path), "revision": updated["revision"],
                    "style_selection": updated["style_selection"], "verification_modified": False}
        finally:
            clean_stage(stage, out)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=TOOL_VERSION)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("render", help="Render options without changing records; fonts must be checked first.")
    command.add_argument("--records", required=True)
    command.add_argument("--options", required=True)
    command.add_argument("--out", required=True)
    command.add_argument("--ffmpeg", required=True)
    command.add_argument("--frame")
    command.set_defaults(run=render)
    command = commands.add_parser("select", help="Apply choices only after the user has actually replied.")
    for name in ("records", "manifest", "size", "font", "position", "confirmation"):
        command.add_argument("--" + name, required=True)
    command.add_argument("--ffmpeg")
    command.set_defaults(run=select)
    args = parser.parse_args()
    try:
        print(json.dumps(args.run(args), ensure_ascii=False, indent=2))
        return 0
    except (PreviewError, subtitles.SubtitleError, OSError, ValueError, KeyError, TypeError,
            subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
