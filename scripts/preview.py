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
import io
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
import fonts

TOOL_VERSION = "1.03"
GROUP_FIELDS = {
    "size": {"font_size_en", "font_size_zh", "font_size_onscreen"},
    "font": {"font_en", "font_zh", "font_onscreen"},
    "position": {"dialogue_position", "dialogue_align", "margin_bottom"},
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


def position_patch(patch):
    """New choices vary bottom distance; historical record styles stay intact."""
    if not isinstance(patch, dict) or not patch or not set(patch) <= GROUP_FIELDS["position"]:
        raise PreviewError("Position options may change only margin_bottom with fixed bottom/center alignment.")
    if patch.get("dialogue_position", "bottom") != "bottom" or patch.get("dialogue_align", "center") != "center":
        raise PreviewError("New position options must use dialogue_position=bottom and dialogue_align=center.")
    if "margin_bottom" not in patch:
        raise PreviewError("Position options require an explicit 1080p-base margin_bottom.")
    value = patch["margin_bottom"]
    if not subtitles.is_number(value) or not 0 <= value <= 1080:
        raise PreviewError("margin_bottom must be a finite 1080p-base distance in [0, 1080].")
    return {**patch, "dialogue_position": "bottom", "dialogue_align": "center"}


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
        if group == "position":
            option["style"] = position_patch(patch)
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
    data.pop("coverage", None)
    data["style"] = style
    # Preview has an artificial timeline; original cue timing/status is untouched.
    data["video"]["duration_ms"] = 2000
    data["cues"] = [{"id": "style-preview-sample", "kind": "dialogue", "start_ms": 0, "end_ms": 2000,
                     "en": sample["en"], "zh": sample["zh"], "include": True,
                     "verification": {name: {"status": "pending", "evidence": []}
                                      for name in ("english", "translation", "timing", "visual")}}]
    data["pending"] = []
    report = subtitles.lint_records(data, check_coverage=False)
    if report["errors"]:
        raise PreviewError("Invalid preview style/records: " + "; ".join(item["message"] for item in report["errors"]))
    return data, report["warnings"]


def family_key(value):
    return re.sub(r"[\s_-]", "", value.casefold())


def font_inventory():
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        result = fonts.list_fonts(argparse.Namespace(dir=None, json=True))
    inventory = json.loads(output.getvalue())
    if result or not inventory.get("fonts"):
        raise PreviewError("No accessible installed font inventory; inspect fonts.py list --json before choosing fonts.")
    return inventory


def candidate_faces(inventory, names, language):
    """Keep the current family first, then real recommended families only."""
    rows = []
    used = set()
    available = inventory["fonts"]
    if language == "zh":
        names = [*names, *sorted({item["family"] for item in available
                                 if isinstance(item.get("family"), str)
                                 and item["family"].startswith("Noto")
                                 and any(marker in item["family"] for marker in ("CJK", "SC", "Hans"))})]
    for preferred in names:
        if not isinstance(preferred, str) or not preferred:
            continue
        matches = [item for item in available if any(
            isinstance(alias, str) and family_key(alias) == family_key(preferred)
            for alias in [item.get("family"), *item.get("family_names", [])])]
        matches.sort(key=lambda item: (not any(word in (item.get("style") or "").casefold()
                                              for word in ("regular", "normal", "roman", "book", "常规", "標準", "标准")),
                                      str(item["path"]).casefold(), item["face"]))
        if not matches:
            continue
        actual_family = matches[0].get("family") or preferred
        if family_key(actual_family) in used:
            continue
        used.add(family_key(actual_family))
        # Regular/default face is checked, so a bold-only glyph map cannot
        # silently authorize a normal-weight family with different coverage.
        rows.append({**matches[0], "family": actual_family})
    return rows


def covered_families(candidates, text, checks_dir, language):
    text_path = checks_dir / f"{language}-sample.txt"
    text_path.write_text(text, encoding="utf-8")
    accepted, reports = [], []
    for index, candidate in enumerate(candidates):
        report_path = checks_dir / f"{language}-{index:02}.json"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = fonts.check_font(argparse.Namespace(font=candidate["path"], face=candidate["face"],
                                                         text=str(text_path), report=str(report_path)))
        report = subtitles.read_json(report_path)
        evidence = {"family": candidate["family"], "font_file": candidate["path"], "face": candidate["face"],
                    "report": str(report_path), "sample_sha256": sha(text.encode("utf-8")),
                    "all_covered": report.get("all_covered", False), "status": report.get("status"),
                    "missing": report.get("missing", []), "error": report.get("error")}
        reports.append(evidence)
        if result == 0 and report.get("all_covered") is True:
            accepted.append(evidence)
    return accepted, reports


def presets(args):
    """Create editable choices from actual installed fonts, without selecting."""
    records_path = Path(args.records).resolve(strict=True)
    original_bytes = records_path.read_bytes()
    original = load_records(records_path)
    dimensions(original)
    base, sample = full_style(original), example(original)
    destination = Path(args.out).resolve()
    if destination.suffix.lower() != ".json":
        raise PreviewError("Preset output must be a separate .json options file.")
    report_path = destination.with_name(destination.stem + ".font-checks.json")
    for path in (destination, report_path):
        subtitles.reject_links(path)
        if path == records_path or path.exists():
            raise PreviewError("Presets never overwrite records or existing options/font-check reports.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    inventory = font_inventory()
    checks_dir = Path(tempfile.mkdtemp(prefix=destination.stem + "-font-checks-", dir=destination.parent))
    en_candidates = candidate_faces(inventory, [base["font_en"], "Arial", "Segoe UI", "Tahoma",
                                                 "Georgia", "Consolas", "Times New Roman"], "en")
    zh_candidates = candidate_faces(inventory, [base["font_zh"], "Microsoft YaHei", "SimHei", "SimSun",
                                                 "DengXian", "KaiTi", "FangSong", "Noto Sans CJK SC", "Noto Sans SC"], "zh")
    zh_text = sample["zh"] + "\n【示意背景，非实际片源】【字幕样式预览】"
    english, en_reports = covered_families(en_candidates, sample["en"], checks_dir, "en")
    chinese, zh_reports = covered_families(zh_candidates, zh_text, checks_dir, "zh")
    onscreen, onscreen_reports = [], []
    if family_key(base["font_onscreen"]) != family_key(base["font_zh"]):
        candidates = candidate_faces(inventory, [base["font_onscreen"]], "onscreen")
        onscreen, onscreen_reports = covered_families(candidates, zh_text, checks_dir, "onscreen")
    font_report = {"tool_version": TOOL_VERSION, "records": str(records_path), "created_at": now(),
                   "sample": sample, "inventory_directories": inventory["directories"],
                   "inventory_errors": inventory["errors"], "english": en_reports, "chinese": zh_reports,
                   "current_onscreen": onscreen_reports,
                   "scope": "Installed-font inventory and sample cmap coverage; real FFmpeg font selection is still checked during render/select."}
    subtitles.atomic_write(report_path, subtitles.json_bytes(font_report), force=False)
    if not english or not chinese:
        raise PreviewError(f"No sample-covering English/Chinese font combination is available; no options selected or installed. Inspect {report_path}.")
    options = []
    for code, en_size, zh_size in (("xs", 28, 32), ("s", 34, 38), ("m", 42, 46), ("l", 50, 54), ("xl", 58, 62)):
        options.append({"id": "size-" + code, "label": f"{code.upper()}：英文 {en_size} / 中文 {zh_size}（1080p 基准）",
                        "group": "size", "style": {"font_size_en": en_size, "font_size_zh": zh_size},
                        "recommended": base["font_size_en"] == en_size and base["font_size_zh"] == zh_size})
    seen_pairs = set()
    for index in range(max(len(english), len(chinese))):
        en, zh = english[index % len(english)], chinese[index % len(chinese)]
        pair = (en["family"], zh["family"])
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        screen_font = onscreen[0] if len(seen_pairs) == 1 and onscreen else zh
        label = f"英文 {en['family']} / 中文 {zh['family']}"
        if screen_font["family"] != zh["family"]:
            label += f" / 画面字 {screen_font['family']}"
        options.append({"id": f"font-{len(seen_pairs):02}", "label": label,
                        "group": "font", "style": {"font_en": en["family"], "font_zh": zh["family"],
                                                     "font_onscreen": screen_font["family"]},
                        "recommended": len(seen_pairs) == 1, "font_checks": {"en": en, "zh": zh, "onscreen": screen_font}})
        if len(seen_pairs) == 6:
            break
    for distance in (40, 60, 90, 120, 160):
        options.append({"id": f"pos-bottom-{distance:03}",
                        "label": f"距底部 {distance}px（基准1080p）" + (" · 推荐" if distance == 60 else ""),
                        "group": "position", "style": {"dialogue_position": "bottom", "dialogue_align": "center",
                                                         "margin_bottom": distance},
                        "recommended": distance == 60})
    if records_path.read_bytes() != original_bytes:
        raise PreviewError("Records changed while preparing choices; no options file was committed.")
    subtitles.atomic_write(destination, subtitles.json_bytes(options), force=False)
    return {"options": str(destination), "font_checks": str(report_path), "counts": {"size": 5, "font": len(seen_pairs), "position": 5},
            "records_modified": False, "selection_status": "pending",
            "display_guidance": "按字号、字体、距底部距离分组分轮展示效果图，可先显示推荐和少量候选；收到实际回复后再组合选择，不能自动采用推荐。自定义位置选项只填margin_bottom，工具固定底部居中；距离以1080p中文预留语言带底边为基准，按视频高度缩放。"}


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
            if group == "position" and position_patch(patch) != patch:
                raise PreviewError(f"{option_id}: render the normalized bottom/center position option again before selecting it.")
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
            errors = subtitles.lint_records(check, check_coverage=False)["errors"]
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
    command = commands.add_parser("presets", help="Generate editable size/font/position choices from actual installed fonts.")
    command.add_argument("--records", required=True)
    command.add_argument("--out", required=True)
    command.set_defaults(run=presets)
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
