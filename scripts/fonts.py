#!/usr/bin/env python3
"""List sfnt font families and check Unicode cmap coverage, without dependencies.

This inspects installed or supplied font files; it does not install fonts, render
text, or prove which font a subtitle player will select. Font files are read-only.
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
from typing import BinaryIO


FONT_SUFFIXES = {".ttf", ".otf", ".ttc", ".otc"}
SFNT_SIGNATURES = {b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1"}
MAX_FACES = 4096
MAX_TABLE_BYTES = 64 * 1024 * 1024


class FontError(ValueError):
    """Invalid, unsupported, or incompletely readable font data."""


def u16(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 2 > len(data):
        raise FontError("Truncated 16-bit font field")
    return struct.unpack_from(">H", data, offset)[0]


def u32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise FontError("Truncated 32-bit font field")
    return struct.unpack_from(">I", data, offset)[0]


class FontFile:
    def __init__(self, path: Path):
        self.path = path
        self.stream: BinaryIO = path.open("rb")
        self.size = os.fstat(self.stream.fileno()).st_size
        try:
            signature = self.read(0, 4)
            if signature == b"ttcf":
                header = self.read(0, 12)
                if u32(header, 4) not in (0x00010000, 0x00020000):
                    raise FontError("Unsupported TTC version")
                count = u32(header, 8)
                if not 1 <= count <= MAX_FACES:
                    raise FontError("Invalid or excessive TTC face count")
                offsets = self.read(12, 4 * count)
                self.face_offsets = [u32(offsets, 4 * i) for i in range(count)]
            elif signature in SFNT_SIGNATURES:
                self.face_offsets = [0]
            else:
                raise FontError("Not a supported TTF/OTF/TTC sfnt font")
        except BaseException:
            self.stream.close()
            raise

    def __enter__(self) -> FontFile:
        return self

    def __exit__(self, *_: object) -> None:
        self.stream.close()

    def read(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0 or offset > self.size or length > self.size - offset:
            raise FontError("Font offset or length lies outside the file")
        self.stream.seek(offset)
        value = self.stream.read(length)
        if len(value) != length:
            raise FontError("Font file was truncated while reading")
        return value

    def tables(self, face: int) -> dict[bytes, tuple[int, int]]:
        if not 0 <= face < len(self.face_offsets):
            raise FontError(f"Face {face} is outside 0..{len(self.face_offsets) - 1}")
        offset = self.face_offsets[face]
        header = self.read(offset, 12)
        if header[:4] not in SFNT_SIGNATURES:
            raise FontError("Invalid sfnt face signature")
        count = u16(header, 4)
        if not 1 <= count <= 4096:
            raise FontError("Invalid or excessive sfnt table count")
        records = self.read(offset + 12, count * 16)
        tables: dict[bytes, tuple[int, int]] = {}
        for i in range(count):
            pos = 16 * i
            tag = records[pos : pos + 4]
            start, length = u32(records, pos + 8), u32(records, pos + 12)
            if start > self.size or length > self.size - start:
                raise FontError(f"Table {tag!r} lies outside the file")
            if tag in tables:
                raise FontError(f"Duplicate sfnt table {tag!r}")
            tables[tag] = (start, length)
        return tables

    def table(self, tables: dict[bytes, tuple[int, int]], tag: bytes) -> bytes:
        if tag not in tables:
            raise FontError(f"Missing required table {tag.decode('ascii', 'replace')}")
        start, length = tables[tag]
        if length > MAX_TABLE_BYTES:
            raise FontError(f"Table {tag!r} exceeds the supported inspection size")
        return self.read(start, length)


def read_names(font: FontFile, tables: dict[bytes, tuple[int, int]]) -> dict:
    if b"name" not in tables:
        return {"family": None, "style": None, "full_name": None,
                "postscript_name": None, "family_names": []}
    data = font.table(tables, b"name")
    table_format, count, storage = u16(data, 0), u16(data, 2), u16(data, 4)
    if table_format not in (0, 1):
        raise FontError(f"Unsupported name table format {table_format}")
    records_end = 6 + 12 * count
    if records_end > len(data) or storage < records_end or storage > len(data):
        raise FontError("Invalid name record array or string storage")
    if table_format == 1:
        lang_count = u16(data, records_end)
        if records_end + 2 + 4 * lang_count > storage:
            raise FontError("Invalid name language-tag records")
    names: dict[int, list[tuple[int, str]]] = {}
    for i in range(count):
        pos = 6 + 12 * i
        platform, encoding, language, name_id, length, offset = struct.unpack_from(
            ">6H", data, pos
        )
        start = storage + offset
        if start > len(data) or length > len(data) - start:
            raise FontError("Name string lies outside the name table")
        codec = None
        if platform == 0 or (platform == 3 and encoding in (0, 1, 10)):
            codec = "utf-16-be"
        elif platform == 1 and encoding == 0:
            codec = "mac_roman"
        if codec is None or name_id not in (1, 2, 4, 6, 16, 17):
            continue
        try:
            value = data[start : start + length].decode(codec).strip("\x00 \t\r\n")
        except UnicodeDecodeError:
            continue
        if not value:
            continue
        # Favor English names suitable for an ASS Fontname, then Unicode names.
        score = (100 if platform == 3 and language == 0x0409 else 0)
        score += 80 if platform == 1 and language == 0 else 0
        score += 60 if platform == 0 and language == 0 else 0
        score += {0: 3, 3: 2, 1: 1}.get(platform, 0)
        names.setdefault(name_id, []).append((score, value))

    def best(primary: int, fallback: int | None = None) -> str | None:
        values = names.get(primary) or names.get(fallback, [])
        return max(values, key=lambda item: (item[0], item[1]))[1] if values else None

    family_id = 16 if names.get(16) else 1
    return {"family": best(16, 1), "style": best(17, 2), "full_name": best(4),
            "postscript_name": best(6),
            "family_names": sorted({value for _, value in names.get(family_id, [])})}


class Cmap4:
    format = 4

    def __init__(self, data: bytes):
        length, twice = u16(data, 2), u16(data, 6)
        if length > len(data) or length < 16 or twice == 0 or twice % 2:
            raise FontError("Invalid cmap format 4 header")
        self.data = data[:length]
        count = twice // 2
        if 16 + 8 * count > length:
            raise FontError("Truncated cmap format 4 segment arrays")
        self.ends = [u16(self.data, 14 + 2 * i) for i in range(count)]
        self.starts = [u16(self.data, 16 + 2 * count + 2 * i) for i in range(count)]
        self.deltas = [u16(self.data, 16 + 4 * count + 2 * i) for i in range(count)]
        self.range_base = 16 + 6 * count
        self.ranges = [u16(self.data, self.range_base + 2 * i) for i in range(count)]
        if self.ends[-1] != 0xFFFF:
            raise FontError("cmap format 4 lacks its terminating segment")
        for i, (start, end, relative) in enumerate(zip(self.starts, self.ends, self.ranges)):
            if start > end or (i and start <= self.ends[i - 1]):
                raise FontError("Invalid or overlapping cmap format 4 segments")
            if relative:
                first = self.range_base + 2 * i + relative
                last = first + 2 * (end - start)
                if relative % 2 or first < 16 + 8 * count or last + 2 > length:
                    raise FontError("Invalid cmap format 4 glyph-array offset")

    def glyph(self, codepoint: int) -> int:
        if codepoint > 0xFFFF:
            return 0
        i = bisect.bisect_left(self.ends, codepoint)
        if i == len(self.ends) or codepoint < self.starts[i]:
            return 0
        if self.ranges[i] == 0:
            return (codepoint + self.deltas[i]) & 0xFFFF
        offset = self.range_base + 2 * i + self.ranges[i] + 2 * (codepoint - self.starts[i])
        value = u16(self.data, offset)
        return ((value + self.deltas[i]) & 0xFFFF) if value else 0


class Cmap12:
    format = 12

    def __init__(self, data: bytes):
        length, count = u32(data, 4), u32(data, 12)
        if u16(data, 2) != 0 or length > len(data) or length < 16 or count > (length - 16) // 12:
            raise FontError("Invalid cmap format 12 header or group count")
        self.groups: list[tuple[int, int, int]] = []
        self.ends: list[int] = []
        for i in range(count):
            start, end, glyph = struct.unpack_from(">3I", data, 16 + 12 * i)
            if start > end or end > 0x10FFFF or (i and start <= self.ends[-1]):
                raise FontError("Invalid or overlapping cmap format 12 groups")
            if glyph + end - start > 0xFFFFFFFF:
                raise FontError("cmap format 12 glyph index overflows")
            self.groups.append((start, end, glyph))
            self.ends.append(end)

    def glyph(self, codepoint: int) -> int:
        i = bisect.bisect_left(self.ends, codepoint)
        if i == len(self.groups) or codepoint < self.groups[i][0]:
            return 0
        start, _, glyph = self.groups[i]
        return glyph + codepoint - start


def read_cmap(font: FontFile, tables: dict[bytes, tuple[int, int]]) -> tuple[object, dict, list[dict]]:
    data = font.table(tables, b"cmap")
    if u16(data, 0) != 0:
        raise FontError("Unsupported cmap table version")
    count = u16(data, 2)
    records_end = 4 + 8 * count
    if count == 0 or records_end > len(data):
        raise FontError("Missing or truncated cmap encoding records")
    supported: list[tuple[int, int, dict]] = []
    skipped: list[dict] = []
    for i in range(count):
        pos = 4 + 8 * i
        platform, encoding, offset = u16(data, pos), u16(data, pos + 2), u32(data, pos + 4)
        if offset < records_end or offset + 2 > len(data):
            raise FontError("cmap subtable offset lies outside the cmap table")
        form = u16(data, offset)
        info = {"platform": platform, "encoding": encoding, "format": form}
        if not (platform == 0 or (platform == 3 and encoding in (1, 10))):
            skipped.append({**info, "reason": "not a Unicode character map"})
            continue
        if form not in (4, 12):
            skipped.append({**info, "reason": "unsupported cmap format"})
            continue
        priority = (100 if form == 12 else 0) + (20 if platform == 3 else 10)
        supported.append((priority, offset, info))
    if not supported:
        formats = sorted({item["format"] for item in skipped})
        raise FontError(f"No supported Unicode cmap format 4/12; found formats {formats}")
    # Select a single Unicode charmap, favoring full-repertoire format 12. Unioning
    # conflicting charmaps could claim coverage unavailable in the selected map.
    _, offset, info = max(supported, key=lambda item: (item[0], -item[1]))
    cmap = Cmap12(data[offset:]) if info["format"] == 12 else Cmap4(data[offset:])
    return cmap, info, skipped


def default_directories() -> list[Path]:
    if os.name == "nt":
        values = []
        windows = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
        local = os.environ.get("LOCALAPPDATA")
        if windows:
            values.append(Path(windows) / "Fonts")
        if local:
            values.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
        return values
    if sys.platform == "darwin":
        return [Path("/System/Library/Fonts"), Path("/Library/Fonts"), Path.home() / "Library/Fonts"]
    user_data = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    return [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), user_data / "fonts", Path.home() / ".fonts"]


def list_fonts(args: argparse.Namespace) -> int:
    directories = [Path(value).expanduser().resolve() for value in args.dir] if args.dir else default_directories()
    found: dict[str, Path] = {}
    errors: list[dict] = []
    for directory in directories:
        if not directory.is_dir():
            if args.dir:
                errors.append({"path": str(directory), "error": "Not an accessible directory"})
            continue

        def walk_error(error: OSError) -> None:
            errors.append({"path": str(error.filename), "error": str(error)})

        for folder, _, files in os.walk(directory, followlinks=False, onerror=walk_error):
            for filename in files:
                path = Path(folder) / filename
                if path.suffix.lower() in FONT_SUFFIXES:
                    found[os.path.normcase(str(path.resolve()))] = path.resolve()
    faces: list[dict] = []
    for path in sorted(found.values(), key=lambda item: str(item).casefold()):
        try:
            with FontFile(path) as font:
                for face in range(len(font.face_offsets)):
                    try:
                        faces.append({"path": str(path), "face": face,
                                      **read_names(font, font.tables(face))})
                    except (OSError, FontError) as error:
                        errors.append({"path": str(path), "face": face, "error": str(error)})
        except (OSError, FontError) as error:
            errors.append({"path": str(path), "error": str(error)})
    if args.json:
        print(json.dumps({"directories": [str(path) for path in directories],
                          "fonts": faces, "errors": errors}, ensure_ascii=False, indent=2))
    else:
        for face in faces:
            print(f"{face['family'] or '[family unavailable]'}\t{face['style'] or ''}\tface={face['face']}\t{face['path']}")
        print(f"Listed {len(faces)} font faces; {len(errors)} inspection errors.", file=sys.stderr)
        for error in errors:
            print(f"{error['path']}: {error['error']}", file=sys.stderr)
    return 2 if not faces and errors else 0


def atomic_json(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def check_font(args: argparse.Namespace) -> int:
    font_path = Path(args.font).expanduser().resolve()
    text_path = Path(args.text).expanduser().resolve()
    report_path = Path(args.report).expanduser().resolve()
    if report_path.suffix.lower() != ".json" or report_path in (font_path, text_path):
        print("Report must be a separate .json file; inputs are read-only.", file=sys.stderr)
        return 2
    report: dict = {"font": str(font_path), "face": args.face, "text_file": str(text_path),
                    "status": "invalid", "all_covered": False,
                    "scope": "Unicode cmap inspection only; no rendering or player font-selection verification"}
    result = 2
    try:
        text = text_path.read_text(encoding="utf-8-sig", errors="strict")
        codepoints = sorted({ord(character) for character in text if not character.isspace()})
        report["unique_non_whitespace_characters"] = len(codepoints)
        if not codepoints:
            raise FontError("Text contains no non-whitespace characters to check")
        with FontFile(font_path) as font:
            tables = font.tables(args.face)
            report["names"] = read_names(font, tables)
            cmap, selected, skipped = read_cmap(font, tables)
            report["selected_cmap"] = selected
            report["skipped_cmaps"] = skipped
            glyph_count = None
            if b"maxp" in tables:
                maximum = font.table(tables, b"maxp")
                glyph_count = u16(maximum, 4)
                if glyph_count == 0:
                    raise FontError("maxp declares zero glyphs")
            missing = []
            for codepoint in codepoints:
                glyph = cmap.glyph(codepoint)
                if glyph_count is not None and glyph >= glyph_count:
                    raise FontError(f"U+{codepoint:04X} maps outside the maxp glyph count")
                if glyph == 0:
                    missing.append({"character": chr(codepoint), "codepoint": f"U+{codepoint:04X}"})
        report.update({"status": "missing_glyphs" if missing else "covered",
                       "all_covered": not missing, "missing": missing,
                       "missing_count": len(missing), "covered_count": len(codepoints) - len(missing)})
        if glyph_count is None:
            report["notes"] = ["Font has no maxp table; cmap nonzero mappings were not checked against a glyph count."]
        result = 1 if missing else 0
    except (OSError, UnicodeError, FontError, struct.error) as error:
        report["error"] = str(error)
    try:
        atomic_json(report_path, report)
    except OSError as error:
        print(f"Could not write report: {error}", file=sys.stderr)
        return 2
    if result == 2:
        print(f"Invalid or unsupported input: {report.get('error', 'unknown error')}", file=sys.stderr)
    else:
        print(f"{report['status']}: {report['covered_count']}/{len(codepoints)} unique characters covered; "
              f"report: {report_path}")
        for item in report["missing"]:
            print(f"{item['codepoint']} {item['character']}")
    return result


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List family names and face indexes; does not parse glyph maps")
    listing.add_argument("--dir", action="append", help="Search this directory instead of platform defaults; repeatable")
    listing.add_argument("--json", action="store_true", help="Write structured inventory to stdout")
    listing.set_defaults(handler=list_fonts)
    checking = commands.add_parser("check", help="Check UTF-8 text against the selected font face's Unicode cmap")
    checking.add_argument("--font", required=True, help="TTF/OTF/TTC/OTC font file")
    checking.add_argument("--face", type=int, default=0, help="Zero-based TTC face index (default: 0)")
    checking.add_argument("--text", required=True, help="UTF-8 or UTF-8 BOM text file")
    checking.add_argument("--report", required=True, help="Atomic JSON report path; may replace an old report")
    checking.set_defaults(handler=check_font)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
