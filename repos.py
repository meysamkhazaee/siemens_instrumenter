#!/usr/bin/env python3
"""
Shared repository registry and SIR / Siemens directory layout discovery.

Supports:
  - Classic 7 Siemens C benchmarks:
      printtokens, printtokens2, replace, schedule, schedule2, tcas, totinfo
  - Larger UNIX/C benchmarks from SIR (Software-artifact Infrastructure Repository):
      space, gzip, grep, sed, flex
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import threading
import zipfile


BENCHMARK_REPOS = [
    # Classic Siemens Suite (7 single-file C programs)
    {
        "id": "printtokens",
        "suite": "Siemens",
        "top_dir": "printtokens_2.0",
        "canon": "printtokens",
        "source_name": "print_tokens.c",
        "exe_stem": "print_tokens",
        "multi_file": False,
        "exclude_sources": set(),
    },
    {
        "id": "printtokens2",
        "suite": "Siemens",
        "top_dir": "printtokens2_2.0",
        "canon": "printtokens2",
        "source_name": "print_tokens2.c",
        "exe_stem": "print_tokens2",
        "multi_file": False,
        "exclude_sources": set(),
    },
    {
        "id": "replace",
        "suite": "Siemens",
        "top_dir": "replace_2.1.tar_2",
        "canon": "replace",
        "source_name": "replace.c",
        "exe_stem": "replace",
        "multi_file": False,
        "exclude_sources": set(),
    },
    {
        "id": "schedule",
        "suite": "Siemens",
        "top_dir": "schedule_2.0",
        "canon": "schedule",
        "source_name": "schedule.c",
        "exe_stem": "schedule",
        "multi_file": False,
        "exclude_sources": set(),
    },
    {
        "id": "schedule2",
        "suite": "Siemens",
        "top_dir": "schedule2_2.0",
        "canon": "schedule2",
        "source_name": "schedule2.c",
        "exe_stem": "schedule2",
        "multi_file": False,
        "exclude_sources": set(),
    },
    {
        "id": "tcas",
        "suite": "Siemens",
        "top_dir": "tcas_2.0",
        "canon": "tcas",
        "source_name": "tcas.c",
        "exe_stem": "tcas",
        "multi_file": False,
        "exclude_sources": set(),
    },
    {
        "id": "totinfo",
        "suite": "Siemens",
        "top_dir": "totinfo_2.0",
        "canon": "totinfo",
        "source_name": "tot_info.c",
        "exe_stem": "tot_info",
        "multi_file": False,
        "exclude_sources": set(),
    },
    # SIR larger C subjects
    {
        "id": "space",
        "suite": "SIR",
        "top_dir": "space",
        "canon": "space",
        "source_name": "space.c",
        "exe_stem": "space",
        "multi_file": True,
        "exclude_sources": {"ansi2knr.c"},
    },
    {
        "id": "gzip",
        "suite": "SIR",
        "top_dir": "gzip",
        "canon": "gzip",
        "source_name": None,
        "exe_stem": "gzip",
        "multi_file": True,
        "exclude_sources": {
            "alloca.c",
            "amiga.c",
            "ansi2knr.c",
            "atari.c",
            "msdos.c",
            "nt.c",
            "os2.c",
            "primos.c",
            "vms.c",
        },
    },
    {
        "id": "grep",
        "suite": "SIR",
        "top_dir": "grep",
        "canon": "grep",
        "source_name": None,
        "exe_stem": "grep",
        "multi_file": True,
        "exclude_sources": {"alloca.c", "ansi2knr.c"},
    },
    {
        "id": "sed",
        "suite": "SIR",
        "top_dir": "sed",
        "canon": "sed",
        "source_name": None,
        "exe_stem": "sed",
        "multi_file": True,
        "exclude_sources": {"alloca.c", "ansi2knr.c"},
    },
    {
        "id": "flex",
        "suite": "SIR",
        "top_dir": "flex",
        "canon": "flex",
        "source_name": None,
        "exe_stem": "flex",
        "multi_file": True,
        "exclude_sources": {"alloca.c", "ansi2knr.c", "libmain.c", "libyywrap.c"},
    },
]

REPO_MAP = {entry["id"]: entry for entry in BENCHMARK_REPOS}
SIEMENS_REPOS = [entry["id"] for entry in BENCHMARK_REPOS]
CLASSIC_SIEMENS_REPOS = [entry["id"] for entry in BENCHMARK_REPOS if entry["suite"] == "Siemens"]
SIR_REPOS = [entry["id"] for entry in BENCHMARK_REPOS if entry["suite"] == "SIR"]

_VERSION_DIR_RE = re.compile(r"^v\d+$")


def natural_sort_key(value: str):
    """Sort v1, v2, ... v10 in natural numerical order."""
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r"(\d+)", value)]


def get_repo_info(repo_id: str) -> dict:
    """Return registry metadata for a known repo, or a sensible generic multi-file entry."""
    if repo_id in REPO_MAP:
        return REPO_MAP[repo_id]
    return {
        "id": repo_id,
        "suite": "SIR",
        "top_dir": repo_id,
        "canon": repo_id,
        "source_name": None,
        "exe_stem": repo_id,
        "multi_file": True,
        "exclude_sources": {"alloca.c", "ansi2knr.c"},
    }


def get_source_stem(repo_id: str) -> str:
    """Return executable / primary source stem for a repository."""
    return get_repo_info(repo_id)["exe_stem"]


def get_benchmark_suite(repo_id: str) -> str:
    """Return benchmark family name ('Siemens' or 'SIR')."""
    return get_repo_info(repo_id)["suite"]


_ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar", ".zip")
_WIN_INVALID_CHAR_MAP = {
    "<": "__lt__",
    ">": "__gt__",
    ":": "__colon__",
    '"': "__quot__",
    "|": "__pipe__",
    "?": "__qmark__",
    "*": "__ast__",
}
_EXTRACT_LOCK = threading.Lock()
_STAMP_NAME = ".archive_extracted.json"


def sanitize_windows_relpath(rel_path: str) -> str:
    """Replace Windows-forbidden filename characters segment-by-segment."""
    parts = [p for p in rel_path.replace("\\", "/").split("/") if p and p != "."]
    sanitized: list[str] = []
    for part in parts:
        fixed = part
        for ch, repl in _WIN_INVALID_CHAR_MAP.items():
            fixed = fixed.replace(ch, repl)
        fixed = fixed.rstrip(" .") or "_"
        sanitized.append(fixed)
    return "/".join(sanitized)


def _archive_stem(path: Path) -> str | None:
    lower = path.name.lower()
    for suffix in _ARCHIVE_SUFFIXES:
        if lower.endswith(suffix):
            return path.name[: -len(suffix)]
    return None


def find_repo_archive(root_dir: Path, repo_id: str) -> Path | None:
    """Locate a compressed archive (.tar.gz/.tgz/.zip/...) for repo_id under root_dir."""
    if not root_dir.is_dir():
        return None
    info = get_repo_info(repo_id)
    exact_names = {repo_id.lower(), info["canon"].lower(), info["top_dir"].lower()}
    candidates: list[Path] = []
    for entry in sorted(root_dir.iterdir()):
        if not entry.is_file():
            continue
        stem = _archive_stem(entry)
        if stem is None:
            continue
        stem_l = stem.lower()
        if stem_l in exact_names or stem_l.startswith(f"{repo_id.lower()}_") or stem_l.startswith(f"{repo_id.lower()}-"):
            candidates.append(entry)
    return candidates[0] if candidates else None


def _safe_member_parts(member_name: str) -> list[str] | None:
    normalized = member_name.replace("\\", "/")
    if normalized.startswith("/") or (len(normalized) >= 2 and normalized[1] == ":"):
        return None
    parts = [part for part in normalized.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        return None
    return parts


def _sanitize_parts_for_host(parts: list[str]) -> list[str]:
    if os.name != "nt":
        return parts
    sanitized: list[str] = []
    for part in parts:
        fixed = part
        for ch, repl in _WIN_INVALID_CHAR_MAP.items():
            fixed = fixed.replace(ch, repl)
        fixed = fixed.rstrip(" .") or "_"
        sanitized.append(fixed)
    return sanitized


def _extract_archive(archive_path: Path, root_dir: Path, repo_id: str, info: dict) -> Path:
    """Safely extract a Siemens/SIR archive (.tar.gz, .zip, ...) into root_dir."""
    stat = archive_path.stat()
    stamp_payload = {
        "archive": archive_path.name,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }

    with _EXTRACT_LOCK:
        # Re-check whether another caller already extracted this exact archive
        existing = _find_existing_repo_dir(root_dir, repo_id, info)
        if existing is not None:
            stamp_file = existing / _STAMP_NAME
            if stamp_file.is_file():
                try:
                    if json.loads(stamp_file.read_text(encoding="utf-8")) == stamp_payload:
                        return existing
                except Exception:
                    pass
            else:
                return existing

        lower = archive_path.name.lower()
        entries: list[tuple[list[str], bool, object]] = []

        if lower.endswith(".zip"):
            with zipfile.ZipFile(archive_path, "r") as zf:
                for zi in zf.infolist():
                    parts = _safe_member_parts(zi.filename)
                    if parts:
                        entries.append((parts, zi.is_dir(), zi))
                return _materialize_archive_entries(
                    archive_path, root_dir, repo_id, info, stamp_payload, entries,
                    reader=lambda obj: zf.open(obj, "r"),
                )

        with tarfile.open(archive_path, "r:*") as tf:
            for member in tf.getmembers():
                if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                    continue
                parts = _safe_member_parts(member.name)
                if parts:
                    entries.append((parts, member.isdir(), member))
            return _materialize_archive_entries(
                archive_path, root_dir, repo_id, info, stamp_payload, entries,
                reader=lambda obj: tf.extractfile(obj),
            )


def _materialize_archive_entries(
    archive_path: Path,
    root_dir: Path,
    repo_id: str,
    info: dict,
    stamp_payload: dict,
    entries: list[tuple[list[str], bool, object]],
    reader,
) -> Path:
    top_levels = {parts[0] for parts, _, _ in entries}
    known_tops = {repo_id, info["canon"], info["top_dir"]}

    if len(top_levels) == 1:
        sole_top = next(iter(top_levels))
        strip_prefix = True
        target_name = sole_top if sole_top in known_tops else repo_id
    else:
        strip_prefix = False
        target_name = repo_id

    dest_dir = root_dir / target_name
    stage_dir = root_dir / f".extracting_{target_name}_{os.getpid()}"
    if stage_dir.exists():
        shutil.rmtree(stage_dir, ignore_errors=True)
    stage_dir.mkdir(parents=True, exist_ok=True)

    print(f"[archive] Extracting {archive_path.name} -> {dest_dir}")
    try:
        for parts, is_dir, obj in entries:
            rel_parts = parts[1:] if strip_prefix else parts
            if not rel_parts:
                continue
            safe_parts = _sanitize_parts_for_host(rel_parts)
            target_path = stage_dir.joinpath(*safe_parts)
            if is_dir:
                target_path.mkdir(parents=True, exist_ok=True)
                continue
            target_path.parent.mkdir(parents=True, exist_ok=True)
            src_stream = reader(obj)
            if src_stream is None:
                continue
            with src_stream, target_path.open("wb") as out_f:
                shutil.copyfileobj(src_stream, out_f, length=1024 * 256)

        (stage_dir / _STAMP_NAME).write_text(json.dumps(stamp_payload, indent=2), encoding="utf-8")

        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        stage_dir.replace(dest_dir)
    finally:
        if stage_dir.exists():
            shutil.rmtree(stage_dir, ignore_errors=True)

    nested = dest_dir / info["canon"]
    if nested.is_dir() and dest_dir.name != info["canon"]:
        return nested
    return dest_dir


def _find_existing_repo_dir(root_dir: Path, repo_id: str, info: dict) -> Path | None:
    candidates = [
        root_dir / repo_id,
        root_dir / info["canon"],
        root_dir / info["top_dir"] / info["canon"],
        root_dir / info["top_dir"],
    ]
    for cand in candidates:
        if cand.is_dir():
            return cand

    if root_dir.is_dir():
        for entry in sorted(root_dir.iterdir()):
            if entry.is_dir() and (entry.name == repo_id or entry.name.startswith(f"{repo_id}_")):
                nested = entry / info["canon"]
                return nested if nested.is_dir() else entry
    return None


def find_repo_base(root_dir: Path, repo_id: str) -> Path | None:
    """Locate the root directory of a Siemens or SIR subject under root_dir, extracting archives if needed."""
    info = get_repo_info(repo_id)
    archive = find_repo_archive(root_dir, repo_id)
    existing = _find_existing_repo_dir(root_dir, repo_id, info)

    if existing is not None:
        if archive is not None:
            stamp_file = existing / _STAMP_NAME
            if stamp_file.is_file():
                try:
                    stamp = json.loads(stamp_file.read_text(encoding="utf-8"))
                    stat = archive.stat()
                    if (
                        stamp.get("archive") == archive.name
                        and stamp.get("size") == stat.st_size
                        and stamp.get("mtime_ns") == stat.st_mtime_ns
                    ):
                        return existing
                except Exception:
                    pass
                return _extract_archive(archive, root_dir, repo_id, info)
        return existing

    if archive is not None:
        return _extract_archive(archive, root_dir, repo_id, info)

    return None


def _has_c_sources(directory: Path) -> bool:
    return directory.is_dir() and any(directory.rglob("*.c"))


def find_orig_dir(base_path: Path, repo_info: dict) -> Path | None:
    """Locate the original (healthy) source directory for a repository."""
    candidates = [
        base_path / "source",
        base_path / "source.alt" / "source.orig",
        base_path / "source.alt" / "orig",
    ]
    source_name = repo_info.get("source_name")
    if source_name:
        for cand in candidates:
            if (cand / source_name).is_file():
                return cand
    for cand in candidates:
        if _has_c_sources(cand):
            return cand
    return None


def find_version_dirs(base_path: Path) -> list[tuple[str, Path]]:
    """Discover v1..vN directories across SIR and Siemens layouts."""
    search_roots = [
        base_path / "versions.alt" / "versions.orig",
        base_path / "versions.alt" / "versions.seeded",
        base_path / "versions.alt",
        base_path / "source.alt",
    ]
    discovered: dict[str, Path] = {}
    for root in search_roots:
        if not root.is_dir():
            continue
        for entry in root.iterdir():
            if entry.is_dir() and _VERSION_DIR_RE.match(entry.name):
                discovered.setdefault(entry.name, entry)
    return sorted(discovered.items(), key=lambda item: natural_sort_key(item[0]))


def _collect_dir_files(directory: Path, suffix: str) -> dict[str, Path]:
    """Return {posix_relative_path: Path} for files with suffix under directory."""
    files: dict[str, Path] = {}
    if not directory or not directory.is_dir():
        return files
    for path in sorted(directory.rglob(f"*{suffix}")):
        if path.is_file():
            rel = path.relative_to(directory).as_posix()
            files[rel] = path
    return files


def _filter_source_map(c_files: dict[str, Path], repo_info: dict) -> dict[str, Path]:
    exclude = {name.lower() for name in repo_info.get("exclude_sources", set())}
    filtered: dict[str, Path] = {}
    for rel, path in sorted(c_files.items()):
        if path.name.lower() in exclude:
            continue
        # Skip test/example subdirectories sometimes bundled in SIR tarballs
        parts_lower = {p.lower() for p in Path(rel).parts[:-1]}
        if parts_lower & {"test", "tests", "doc", "docs", "examples", "sample", "samples"}:
            continue
        filtered[rel] = path
    return filtered


def resolve_version_sources(
    orig_dir: Path,
    version_dir: Path | None,
    repo_info: dict,
) -> dict:
    """
    Resolve effective .c and .h files for either 'orig' (version_dir=None)
    or a faulty version (overlaying version_dir files onto orig_dir).
    """
    orig_c_all = _filter_source_map(_collect_dir_files(orig_dir, ".c"), repo_info)
    orig_h_all = _collect_dir_files(orig_dir, ".h")

    source_name = repo_info.get("source_name")
    if not repo_info.get("multi_file", True) and source_name:
        orig_c_all = {k: v for k, v in orig_c_all.items() if Path(k).name == source_name}

    effective_c = dict(orig_c_all)
    effective_h = dict(orig_h_all)

    if version_dir is not None:
        ver_c = _filter_source_map(_collect_dir_files(version_dir, ".c"), repo_info)
        ver_h = _collect_dir_files(version_dir, ".h")
        if not repo_info.get("multi_file", True) and source_name:
            ver_c = {k: v for k, v in ver_c.items() if Path(k).name == source_name}
        effective_c.update(ver_c)
        effective_h.update(ver_h)

    return {
        "c_files": dict(sorted(effective_c.items())),
        "h_files": dict(sorted(effective_h.items())),
        "orig_c_files": dict(sorted(orig_c_all.items())),
        "orig_h_files": dict(sorted(orig_h_all.items())),
    }


def find_gcc() -> str:
    """Locate gcc executable on PATH or standard Windows MSYS2 locations."""
    found = shutil.which("gcc")
    if found:
        return str(Path(found).resolve())
    if os.name == "nt":
        for cand in (
            Path(r"C:\msys64\ucrt64\bin\gcc.exe"),
            Path(r"C:\msys64\mingw64\bin\gcc.exe"),
            Path(r"C:\msys64\clang64\bin\gcc.exe"),
        ):
            if cand.is_file():
                return str(cand.resolve())
    return "gcc"


def detect_compile_flags(h_files: dict[str, Path], source_dirs: list[Path]) -> list[str]:
    """Infer standard SIR/GNU C compile flags (such as -DHAVE_CONFIG_H) when applicable."""
    flags: list[str] = []
    if any(Path(rel).name == "config.h" for rel in h_files):
        flags.append("-DHAVE_CONFIG_H")
    for sdir in source_dirs:
        if sdir and sdir.is_dir():
            flags.append(f"-I{sdir}")
    return flags
