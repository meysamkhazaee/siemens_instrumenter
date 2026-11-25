#!/usr/bin/env python3
"""
Siemens & SIR Benchmark Suites: Test Scenario Runner, Path Divergence Tracer & Dataset Generator.

Capabilities:
  1. Multi-threaded test execution for both healthy (orig/) and faulty (vX/) versions,
     including single-file Siemens and multi-file SIR programs (space, gzip, grep, sed, flex).
  2. Automatic identification of fault lines across modified .c/.h files and mapping to
     global basic-block P-points.
  3. Classification of every test case into:
     - PASS (T): Output & side-effect files match oracle, fault location was NOT visited.
     - COINCIDENTAL_CORRECT (CC): Output matches oracle, BUT fault location WAS visited.
     - FAIL (F): Output, return code, timeout, or generated file differs from oracle.
  4. Generation of compact EFL v2 datasets under output/results/<repo>/<version>/full/:
     - traces.tsv.gz, tests.tsv, source_map.csv, source_changes.json,
       metadata.json, summary.json, cache_manifest.json.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import difflib
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import instrumenter
import repos


SIEMENS_REPOS = list(repos.SIEMENS_REPOS)
natural_sort_key = repos.natural_sort_key
get_source_stem = repos.get_source_stem

_MTS_BRACKET_RE = re.compile(r"-([PIFO])\s*\[([^\]]*)\]")


def _resolve_input_path(token: str, asset_roots: tuple[Path, ...], *, allow_bare_filename: bool = False) -> Path | None:
    """Resolve an input file token against SIR/Siemens asset directories."""
    if not token or token.startswith("-"):
        return None
    cleaned = token.strip()
    while cleaned.startswith("../"):
        cleaned = cleaned[3:]
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]

    rel_variants = [cleaned]
    sanitized = repos.sanitize_windows_relpath(cleaned)
    if sanitized != cleaned:
        rel_variants.append(sanitized)

    candidates: list[Path] = []
    for variant in rel_variants:
        has_sep = ("/" in variant) or ("\\" in variant)
        has_ext = bool(Path(variant).suffix)
        for base_dir in asset_roots:
            if has_sep or allow_bare_filename or has_ext:
                candidates.append(base_dir / variant)
                candidates.append(base_dir / "inputs" / variant)
            if allow_bare_filename or (not variant.startswith("inputs/") and (has_sep or has_ext)):
                candidates.append(base_dir / "inputs" / Path(variant).name)
            if has_sep or allow_bare_filename:
                candidates.append(base_dir / "testplans.alt" / variant)

    for cand in candidates:
        try:
            if cand.is_file():
                return cand.resolve()
        except OSError:
            continue
    return None


def _strip_shell_redirections(tokens: list[str]) -> tuple[list[str], str | None]:
    """Extract optional '< stdin' and strip output redirection tokens like '> out', '2>&1'."""
    cleaned_args: list[str] = []
    stdin_rel: str | None = None
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        if tok in {"2>&1", ">&2", "1>&2"}:
            idx += 1
            continue
        if tok == "<":
            if idx + 1 < len(tokens):
                stdin_rel = tokens[idx + 1]
                idx += 2
                continue
        if tok.startswith("<") and len(tok) > 1:
            stdin_rel = tok[1:]
            idx += 1
            continue
        if tok in {">", ">>", "1>", "1>>", "2>", "2>>", "&>"}:
            idx += 2
            continue
        if re.match(r"^(?:1|2|&)?>>?.+", tok):
            idx += 1
            continue
        cleaned_args.append(tok)
        idx += 1
    return cleaned_args, stdin_rel


def parse_universe_tests(universe_file: Path, *asset_roots: Path):
    """Parse Siemens or SIR universe files into executable test descriptors."""
    if not universe_file.is_file():
        return []

    tests = []
    lines = universe_file.read_text(encoding="utf-8", errors="replace").splitlines()
    for idx, line in enumerate(lines, 1):
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue

        stdin_rel: str | None = None
        arg_tokens: list[str] = []

        # Support SIR MTS format: -P [args] -I [stdin_file]
        mts_matches = list(_MTS_BRACKET_RE.finditer(raw))
        if mts_matches and raw.lstrip().startswith("-"):
            for m in mts_matches:
                tag, body = m.group(1), m.group(2).strip()
                if tag == "P" and body:
                    try:
                        arg_tokens.extend(shlex.split(body))
                    except Exception:
                        arg_tokens.extend(body.split())
                elif tag == "I" and body:
                    stdin_rel = body
        else:
            if "<" in raw and not re.search(r"['\"].*<.*['\"]", raw):
                arg_part, stdin_part = raw.rsplit("<", 1)
                # Keep any trailing redirection after stdin_part stripped
                try:
                    stdin_tokens = shlex.split(stdin_part.strip())
                except Exception:
                    stdin_tokens = stdin_part.strip().split()
                if stdin_tokens:
                    stdin_rel = stdin_tokens[0]
                try:
                    raw_tokens = shlex.split(arg_part.strip()) if arg_part.strip() else []
                except Exception:
                    raw_tokens = arg_part.strip().split()
                arg_tokens, _ = _strip_shell_redirections(raw_tokens)
            else:
                try:
                    raw_tokens = shlex.split(raw)
                except Exception:
                    raw_tokens = raw.split()
                arg_tokens, stdin_rel = _strip_shell_redirections(raw_tokens)

        stdin_file = (
            _resolve_input_path(stdin_rel, asset_roots, allow_bare_filename=True)
            if stdin_rel
            else None
        )

        # Resolve any file arguments referencing SIR inputs/ without altering non-file flags
        resolved_args: list[str] = []
        file_arg_paths: dict[int, Path] = {}
        for arg_idx, tok in enumerate(arg_tokens):
            resolved = _resolve_input_path(tok, asset_roots)
            if resolved is not None:
                resolved_args.append(str(resolved))
                file_arg_paths[arg_idx] = resolved
            else:
                resolved_args.append(tok)

        tests.append({
            "test_id": idx,
            "args": resolved_args,
            "file_arg_paths": file_arg_paths,
            "stdin_file": stdin_file,
            "raw_line": raw,
        })

    return tests


def _collect_project_points(c_files: dict[str, Path], extra_args: list[str]):
    """Parse AST and assign contiguous global P-markers across one or more C files."""
    compiler = instrumenter.find_compiler("c")
    all_points = []
    next_marker = 1

    for rel_c, src_path in sorted(c_files.items()):
        ast, _, legacy = instrumenter.run_clang_ast(src_path, "c", compiler, extra_args)
        source_data = src_path.read_bytes()
        points = instrumenter.collect_points(
            ast,
            src_path,
            source_data,
            legacy,
            require_main=False,
        )
        points = instrumenter.remove_physical_duplicates(points)
        for p in points:
            p.marker = next_marker
            p.file_name = rel_c
            next_marker += 1
        all_points.extend(points)

    return all_points


def _map_line_to_point(line_no: int, file_points: list, fallback_points: list):
    """Map a source line number in a file to its exact or enclosing P-point."""
    candidates = file_points if file_points else fallback_points
    if not candidates:
        return None
    exact = [p for p in candidates if p.line == line_no]
    if exact:
        return exact[0]
    preceding = [p for p in candidates if p.line <= line_no]
    return preceding[-1] if preceding else candidates[0]


def get_project_fault_markers(
    orig_c_files: dict[str, Path],
    faulty_c_files: dict[str, Path],
    orig_h_files: dict[str, Path] | None = None,
    faulty_h_files: dict[str, Path] | None = None,
    extra_args: list[str] | None = None,
):
    """Collect global P-points for the faulty version and map changed lines across all files."""
    orig_h_files = orig_h_files or {}
    faulty_h_files = faulty_h_files or {}
    extra_args = extra_args or []

    all_points = _collect_project_points(faulty_c_files, extra_args)
    points_by_file: dict[str, list] = {}
    for p in all_points:
        points_by_file.setdefault(p.file_name, []).append(p)

    fault_markers: set[int] = set()
    fault_details: list[dict] = []

    for rel_c in sorted(set(orig_c_files) | set(faulty_c_files)):
        orig_path = orig_c_files.get(rel_c)
        faulty_path = faulty_c_files.get(rel_c)
        orig_lines = orig_path.read_text(encoding="utf-8", errors="replace").splitlines() if orig_path else []
        faulty_lines = faulty_path.read_text(encoding="utf-8", errors="replace").splitlines() if faulty_path else []
        if orig_lines == faulty_lines:
            continue

        matcher = difflib.SequenceMatcher(None, orig_lines, faulty_lines)
        file_points = points_by_file.get(rel_c, [])

        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue

            orig_start = i1 + 1
            orig_end = max(orig_start, i2) if i2 > i1 else i1
            if j2 > j1:
                faulty_line_list = list(range(j1 + 1, j2 + 1))
                anchor_line = j1 + 1
            else:
                # Pure deletion relative to orig: anchor on nearest surviving line in faulty
                faulty_line_list = [max(1, j1)] if faulty_lines else []
                anchor_line = max(1, j1)

            # Collect all exact point matches across changed faulty lines; fall back to anchor line if none match.
            matched_points = [
                pt
                for line_no in faulty_line_list
                for pt in file_points
                if pt.line == line_no
            ]
            if matched_points:
                fault_markers.update(pt.marker for pt in matched_points)
                p_cand = matched_points[0]
            else:
                p_cand = _map_line_to_point(anchor_line, file_points, all_points)
                if p_cand:
                    fault_markers.add(p_cand.marker)

            fault_details.append({
                "file": rel_c,
                "header": False,
                "type": tag,
                "orig_lines": f"{orig_start}-{orig_end}" if orig_start != orig_end else str(orig_start),
                "reference_lines": list(range(i1 + 1, i2 + 1)) if i2 > i1 else [],
                "faulty_lines": faulty_line_list,
                "orig_text": orig_lines[i1:i2],
                "faulty_text": faulty_lines[j1:j2],
                "p_marker": p_cand.marker if p_cand else None,
                "p_str": (
                    f"P{p_cand.marker} ({rel_c}:{p_cand.line}, {p_cand.kind})"
                    if p_cand
                    else "Unknown"
                ),
            })

    # Record modified header files too (marked header=True per ground_truth schema)
    for rel_h in sorted(set(orig_h_files) | set(faulty_h_files)):
        orig_path = orig_h_files.get(rel_h)
        faulty_path = faulty_h_files.get(rel_h)
        orig_lines = orig_path.read_text(encoding="utf-8", errors="replace").splitlines() if orig_path else []
        faulty_lines = faulty_path.read_text(encoding="utf-8", errors="replace").splitlines() if faulty_path else []
        if orig_lines == faulty_lines:
            continue

        matcher = difflib.SequenceMatcher(None, orig_lines, faulty_lines)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            fault_details.append({
                "file": rel_h,
                "header": True,
                "type": tag,
                "orig_lines": f"{i1 + 1}-{max(i1 + 1, i2)}",
                "reference_lines": list(range(i1 + 1, i2 + 1)) if i2 > i1 else [],
                "faulty_lines": list(range(j1 + 1, j2 + 1)) if j2 > j1 else [],
                "orig_text": orig_lines[i1:i2],
                "faulty_text": faulty_lines[j1:j2],
                "p_marker": None,
                "p_str": f"{rel_h} (header)",
            })

    return fault_markers, fault_details, all_points


def get_fault_markers(orig_source: Path, faulty_source: Path):
    """Backwards-compatible single-file wrapper for get_project_fault_markers."""
    include_arg = [f"-I{orig_source.parent}"]
    return get_project_fault_markers(
        {orig_source.name: orig_source},
        {faulty_source.name: faulty_source},
        extra_args=include_arg,
    )


def _parse_trace_ids(raw_trace: str) -> list[str]:
    if not raw_trace:
        return []
    out: list[str] = []
    for token in raw_trace.split(","):
        t = token.strip()
        if not t:
            continue
        if (t.startswith("P") or t.startswith("p")) and t[1:].isdigit():
            out.append(t[1:])
        else:
            out.append(t)
    return out


def _snapshot_dir_files(directory: Path) -> dict[str, bytes]:
    """Capture generated files in an isolated working directory for side-effect comparison."""
    files: dict[str, bytes] = {}
    for p in sorted(directory.rglob("*")):
        if p.is_file():
            files[p.relative_to(directory).as_posix()] = p.read_bytes()
    return files


def _prepare_test_args_for_cwd(
    args: list[str],
    file_arg_paths: dict[int, Path],
    work_dir: Path,
    copy_file_args: bool,
) -> list[str]:
    if not copy_file_args or not file_arg_paths:
        return list(args)
    prepared = list(args)
    for idx, src_path in file_arg_paths.items():
        local_name = f"in_{idx}_{src_path.name}"
        shutil.copy2(src_path, work_dir / local_name)
        prepared[idx] = local_name
    return prepared


_WORKER_LOCAL = threading.local()


def _get_worker_cwd(temp_dir: Path, label: str) -> Path:
    """Return a reusable isolated per-thread working directory, emptied before each use."""
    cwds = getattr(_WORKER_LOCAL, "cwds", None)
    if cwds is None:
        cwds = {}
        _WORKER_LOCAL.cwds = cwds
    key = (str(temp_dir), label)
    cwd = cwds.get(key)
    if cwd is None or not cwd.is_dir():
        cwd = temp_dir / f"cwd_{label}_{os.getpid()}_{threading.get_ident()}"
        cwd.mkdir(parents=True, exist_ok=True)
        cwds[key] = cwd
        return cwd
    for entry in list(cwd.iterdir()):
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
    return cwd


def _run_orig_oracle_item(
    test_item,
    exe_orig: Path,
    temp_dir: Path,
    copy_file_args: bool,
) -> tuple[bool, bytes, int | None, dict[str, bytes]]:
    args = test_item["args"]
    file_arg_paths = test_item.get("file_arg_paths", {})
    stdin_file = test_item["stdin_file"]
    stdin_data = stdin_file.read_bytes() if (stdin_file and stdin_file.is_file()) else None

    orig_cwd = _get_worker_cwd(temp_dir, "orig")
    orig_args = _prepare_test_args_for_cwd(args, file_arg_paths, orig_cwd, copy_file_args)
    try:
        r_orig = subprocess.run([str(exe_orig), "--p-output", os.devnull, *orig_args], input=stdin_data, capture_output=True, timeout=15, cwd=str(orig_cwd))
        snap = _snapshot_dir_files(orig_cwd) if copy_file_args or any(orig_cwd.iterdir()) else {}
        return (False, r_orig.stdout, r_orig.returncode, snap)
    except subprocess.TimeoutExpired:
        return (True, b"", None, {})


def run_single_test_pair(
    test_item,
    exe_orig: Path,
    exe_faulty: Path,
    fault_markers: set,
    temp_dir: Path,
    *,
    copy_file_args: bool = False,
    orig_outcome: tuple[bool, bytes, int | None, dict[str, bytes]] | None = None,
):
    """Execute a single test case in isolated working directories and evaluate outcomes and traces."""
    test_id = test_item["test_id"]
    args = test_item["args"]
    file_arg_paths = test_item.get("file_arg_paths", {})
    stdin_file = test_item["stdin_file"]
    stdin_data = stdin_file.read_bytes() if (stdin_file and stdin_file.is_file()) else None

    trace_faulty_file = temp_dir / f"trace_faulty_{test_id}_{os.getpid()}.txt"
    faulty_cwd = _get_worker_cwd(temp_dir, "faulty")

    r_faulty = None
    orig_timed_out = False
    orig_stdout = b""
    orig_rc: int | None = None
    orig_files: dict[str, bytes] = {}
    faulty_timed_out = False
    started = time.perf_counter()

    if orig_outcome is not None:
        orig_timed_out, orig_stdout, orig_rc, orig_files = orig_outcome
    else:
        orig_timed_out, orig_stdout, orig_rc, orig_files = _run_orig_oracle_item(
            test_item, exe_orig, temp_dir, copy_file_args
        )

    try:
        faulty_args = _prepare_test_args_for_cwd(args, file_arg_paths, faulty_cwd, copy_file_args)
        cmd_faulty = [str(exe_faulty), "--p-output", str(trace_faulty_file)] + faulty_args
        r_faulty = subprocess.run(cmd_faulty, input=stdin_data, capture_output=True, timeout=15, cwd=str(faulty_cwd))
    except subprocess.TimeoutExpired:
        faulty_timed_out = True
    duration_sec = round(time.perf_counter() - started, 4)

    timed_out = orig_timed_out or faulty_timed_out

    t_faulty = ""
    if trace_faulty_file.exists():
        try:
            t_faulty = trace_faulty_file.read_text(encoding="utf-8", errors="replace").strip().rstrip(",")
            trace_faulty_file.unlink(missing_ok=True)
        except Exception:
            pass

    trace_ids = _parse_trace_ids(t_faulty)
    fault_marker_strs = {str(m) for m in fault_markers if m is not None}

    if timed_out or orig_rc is None or r_faulty is None:
        is_fail = True
    else:
        is_fail = (orig_stdout != r_faulty.stdout or orig_rc != r_faulty.returncode)
        if not is_fail and (orig_files or copy_file_args or any(faulty_cwd.iterdir())):
            # Also detect side-effect output differences (e.g., flex's lex.yy.c or gzip .gz outputs)
            is_fail = (orig_files != _snapshot_dir_files(faulty_cwd))

    fault_visited = bool(fault_marker_strs and any(tid in fault_marker_strs for tid in trace_ids))

    if is_fail:
        tag_strict = "F"
        cc_flag = 0
        classification = "FAIL"
    elif fault_visited:
        tag_strict = "T"
        cc_flag = 1
        classification = "COINCIDENTAL_CORRECT"
    else:
        tag_strict = "T"
        cc_flag = 0
        classification = "PASS"

    return {
        "test_id": test_id,
        "raw_line": test_item.get("raw_line", " ".join(args)),
        "args": args,
        "classification": classification,
        "tag_strict": tag_strict,
        "cc": cc_flag,
        "trace_ids": trace_ids,
        "fault_visited": fault_visited,
        "trace_len": len(trace_ids),
        "duration_sec": duration_sec,
        "return_code": r_faulty.returncode if r_faulty is not None else 124,
        "timed_out": int(timed_out),
    }


def _find_siemens_assets_dir(input_base: Path, output_repo_dir: Path, repo_name: str) -> Path:
    repo_base = repos.find_repo_base(input_base, repo_name)
    candidates = [repo_base] if repo_base else []
    candidates.extend([input_base / repo_name, output_repo_dir])
    for cand in candidates:
        if cand and (
            (cand / "testplans.alt" / "universe").is_file()
            or (cand / "testplans" / "universe").is_file()
            or any(cand.glob("testplans.alt/*universe*"))
        ):
            return cand
    return repo_base or (input_base / repo_name)


def _find_universe_file(assets_dir: Path) -> Path:
    candidates = [
        assets_dir / "testplans.alt" / "universe",
        assets_dir / "testplans" / "universe",
        assets_dir / "testplans.alt" / "testplans.orig" / "universe",
    ]
    for cand in candidates:
        if cand.is_file():
            return cand
    for cand in sorted(assets_dir.glob("testplans.alt/*universe*")):
        if cand.is_file():
            return cand
    return assets_dir / "testplans.alt" / "universe"


def execute_version_tests(
    repo_name: str,
    version_name: str,
    siemens_output_base: Path,
    siemens_input_base: Path,
    output_results_base: Path,
    max_tests: int = None,
    workers: int = 6,
    orig_cache: dict | None = None,
):
    """Run all tests for a specific repo and version, and write compact EFL v2 datasets."""
    repo_info = repos.get_repo_info(repo_name)
    suite_name = repo_info["suite"]
    repo_dir = siemens_output_base / repo_name
    stem = repo_info["exe_stem"]

    exe_orig = repo_dir / "orig" / f"{stem}.exe"
    if not exe_orig.is_file():
        exe_orig = repo_dir / "source" / f"{stem}.exe"
    if not exe_orig.is_file():
        exe_orig = repo_dir / "source.alt" / "source.orig" / f"{stem}.exe"

    exe_faulty = repo_dir / version_name / f"{stem}.exe"
    if not exe_faulty.is_file():
        exe_faulty = repo_dir / "source.alt" / version_name / f"{stem}.exe"
    if not exe_faulty.is_file():
        exe_faulty = repo_dir / "versions.alt" / "versions.orig" / version_name / f"{stem}.exe"

    if not exe_orig.is_file() or not exe_faulty.is_file():
        print(f"[-] Executables not found for {repo_name} {version_name}: orig={exe_orig.exists()}, faulty={exe_faulty.exists()}")
        return None

    in_repo_dir = repos.find_repo_base(siemens_input_base, repo_name) or (siemens_input_base / repo_name)
    orig_dir = repos.find_orig_dir(in_repo_dir, repo_info)
    version_dirs = dict(repos.find_version_dirs(in_repo_dir))
    faulty_dir = version_dirs.get(version_name)

    if orig_dir is None or faulty_dir is None:
        print(f"[-] Source directories not found for {repo_name} {version_name}")
        return None

    orig_sources = repos.resolve_version_sources(orig_dir, None, repo_info)
    faulty_sources = repos.resolve_version_sources(orig_dir, faulty_dir, repo_info)

    include_dirs = [faulty_dir, orig_dir]
    for h_path in faulty_sources["h_files"].values():
        if h_path.parent not in include_dirs:
            include_dirs.append(h_path.parent)
    extra_args = repos.detect_compile_flags(faulty_sources["h_files"], include_dirs)

    # 1. Fault markers & global source_map across all files
    fault_markers, fault_details, all_points = get_project_fault_markers(
        orig_sources["c_files"],
        faulty_sources["c_files"],
        orig_sources["h_files"],
        faulty_sources["h_files"],
        extra_args=extra_args,
    )

    # 2. Parse universe tests directly from input assets
    assets_dir = _find_siemens_assets_dir(siemens_input_base, repo_dir, repo_name)
    universe_path = _find_universe_file(assets_dir)
    test_items = parse_universe_tests(universe_path, assets_dir, repo_dir)
    if max_tests and max_tests > 0:
        test_items = test_items[:max_tests]

    total_tests = len(test_items)
    print(f"\n[{repo_name.upper()} - {version_name}] Running {total_tests} tests with {workers} workers...")
    print(f"  Fault markers: {[f'P{m}' for m in sorted(fault_markers)]}")

    copy_file_args = (repo_name == "gzip")
    start_time = time.time()
    results = [None] * total_tests

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            if orig_cache is not None:
                missing_orig = [item for item in test_items if item["test_id"] not in orig_cache]
                if missing_orig:
                    orig_start = time.time()
                    orig_futures = {
                        executor.submit(
                            _run_orig_oracle_item,
                            item,
                            exe_orig,
                            tmp_path,
                            copy_file_args,
                        ): item["test_id"]
                        for item in missing_orig
                    }
                    for future in as_completed(orig_futures):
                        tid = orig_futures[future]
                        orig_cache[tid] = future.result()
                    print(f"  Cached {len(missing_orig)} orig oracle runs in {round(time.time() - orig_start, 2)}s")

            future_to_idx = {
                executor.submit(
                    run_single_test_pair,
                    item,
                    exe_orig,
                    exe_faulty,
                    fault_markers,
                    tmp_path,
                    copy_file_args=copy_file_args,
                    orig_outcome=orig_cache.get(item["test_id"]) if orig_cache is not None else None,
                ): i
                for i, item in enumerate(test_items)
            }

            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                res = future.result()
                results[idx] = res

    duration = round(time.time() - start_time, 2)

    n_pass = sum(1 for r in results if r["classification"] == "PASS")
    n_cc = sum(1 for r in results if r["classification"] == "COINCIDENTAL_CORRECT")
    n_fail = sum(1 for r in results if r["classification"] == "FAIL")

    print(f"  --> Completed in {duration}s ({total_tests / max(duration, 0.001):.1f} tests/sec)")
    print(f"  --> PASS: {n_pass} | COINCIDENTAL_CORRECT (CC): {n_cc} | FAIL: {n_fail}")

    # Write compact canonical EFL v2 output under results/<repo>/<version>/full/
    results_ver_dir = output_results_base / repo_name / version_name / "full"
    results_ver_dir.mkdir(parents=True, exist_ok=True)

    # 1. traces.tsv.gz
    traces_file = results_ver_dir / "traces.tsv.gz"
    included_count = 0
    excluded_count = 0
    trace_lengths: list[int] = []
    with gzip.open(traces_file, "wt", encoding="utf-8", newline="\n", compresslevel=6) as gz_out:
        gz_out.write("test_id\tstatus\tcc\tpath\n")
        for r in results:
            if not r["trace_ids"]:
                excluded_count += 1
                continue
            included_count += 1
            trace_lengths.append(r["trace_len"])
            gz_out.write(f"T{r['test_id']}\t{r['tag_strict']}\t{r['cc']}\t{' '.join(r['trace_ids'])}\n")

    # 2. tests.tsv
    tests_tsv_file = results_ver_dir / "tests.tsv"
    with open(tests_tsv_file, "w", encoding="utf-8", newline="\n") as f:
        f.write("test_id\ttest_name\tstatus\tcc\tincluded\ttrace_len\tduration_sec\treturn_code\ttimed_out\treason\n")
        for r in results:
            included = 1 if r["trace_ids"] else 0
            reason = "" if included else "empty_trace"
            safe_name = str(r["raw_line"]).replace("\t", " ").replace("\r", " ").replace("\n", " ")
            f.write(
                f"T{r['test_id']}\t{safe_name}\t{r['tag_strict']}\t{r['cc']}\t{included}\t"
                f"{r['trace_len']}\t{r['duration_sec']}\t{r['return_code']}\t{r['timed_out']}\t{reason}\n"
            )

    # 3. source_map.csv
    source_map_file = results_ver_dir / "source_map.csv"
    primary_file = next(iter(faulty_sources["c_files"]), f"{stem}.c")
    with open(source_map_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["predicate", "id", "file", "line", "column", "function", "kind", "address"])
        for p in all_points:
            writer.writerow([
                f"P{p.marker}",
                p.marker,
                getattr(p, "file_name", "") or primary_file,
                p.line,
                getattr(p, "col", 0),
                getattr(p, "func", "main"),
                getattr(p, "kind", ""),
                "0x0",
            ])

    # 4. source_changes.json
    source_changes_file = results_ver_dir / "source_changes.json"
    changes_list = []
    for fd in fault_details:
        f_lines = list(fd.get("faulty_lines", []))
        ref_lines = list(fd.get("reference_lines", []))
        if not fd.get("header", False):
            p_cand_line = next((p.line for p in all_points if p.marker == fd.get("p_marker")), None)
            if p_cand_line and not f_lines:
                f_lines.append(p_cand_line)
        changes_list.append({
            "file": fd.get("file", primary_file),
            "kind": fd.get("type", "replace"),
            "reference_lines": ref_lines,
            "faulty_lines": f_lines,
            "reference_text": fd.get("orig_text", []),
            "faulty_text": fd.get("faulty_text", []),
            "header": bool(fd.get("header", False)),
        })
    source_changes_file.write_text(json.dumps(changes_list, indent=2), encoding="utf-8")

    # 5. metadata.json
    v_num = int(version_name.lstrip("v"))
    metadata_file = results_ver_dir / "metadata.json"
    metadata_file.write_text(json.dumps({
        "benchmark": suite_name,
        "program": repo_name,
        "version": v_num,
        "test_set": "full",
        "source_file": primary_file,
        "source_files": list(faulty_sources["c_files"].keys()),
        "orig_source": str(orig_dir),
        "faulty_source": str(faulty_dir),
    }, indent=2), encoding="utf-8")

    # 6. summary.json
    summary_file = results_ver_dir / "summary.json"
    summary_file.write_text(json.dumps({
        "schema": "efl-dataset-v2",
        "benchmark": suite_name,
        "program": repo_name,
        "version": version_name,
        "test_set": "full",
        "instrumentation": {
            "granularity": "ast_basic_block_control_flow",
            "engine": "clang_ast_c_instrumenter",
            "predicate_format": "integer_id_matching_source_map_id",
        },
        "files": {
            "traces": "traces.tsv.gz",
            "tests": "tests.tsv",
            "source_map": "source_map.csv",
            "source_changes": "source_changes.json",
            "metadata": "metadata.json",
        },
        "duration_sec": duration,
        "tests_discovered": total_tests,
        "tests_included": included_count,
        "tests_excluded": excluded_count,
        "total_trace_events": sum(trace_lengths),
        "max_trace_events": max(trace_lengths, default=0),
        "min_trace_events": min(trace_lengths, default=0),
        "fault_predicates": [f"P{m}" for m in sorted(fault_markers)],
        "fault_predicate_ids": sorted(fault_markers),
        "counts": {
            "pass_clean": n_pass,
            "coincidental_correct": n_cc,
            "fail": n_fail,
        },
    }, indent=2), encoding="utf-8")

    # 7. cache_manifest.json
    manifest_file = results_ver_dir / "cache_manifest.json"
    outputs = []
    for p in [traces_file, tests_tsv_file, summary_file, source_map_file, source_changes_file, metadata_file]:
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        outputs.append({"name": p.name, "size": p.stat().st_size, "sha256": h})
    manifest_file.write_text(json.dumps({
        "schema": 1,
        "fingerprint": f"{suite_name.lower()}-{repo_name}-{version_name}",
        "options": {"format": "efl-dataset-v2"},
        "outputs": outputs,
    }, indent=2), encoding="utf-8")

    return {
        "repo": repo_name,
        "suite": suite_name,
        "version": version_name,
        "total_tests": total_tests,
        "included_tests": included_count,
        "duration_sec": duration,
        "n_pass": n_pass,
        "n_cc": n_cc,
        "n_fail": n_fail,
        "traces_file": str(traces_file),
        "summary_file": str(summary_file),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Siemens & SIR Test Scenario Runner and Trace Dataset Generator"
    )
    parser.add_argument(
        "--repo",
        default="tcas",
        help="Repository to test (e.g. tcas, schedule, space, gzip, grep, sed, flex, or 'all')"
    )
    parser.add_argument(
        "--version",
        default="v1",
        help="Version to test (e.g. v1, v2, or 'all')"
    )
    parser.add_argument(
        "--input-dir",
        default="input/siemens",
        help="Siemens/SIR root directory with original uninstrumented sources (default: input/siemens)"
    )
    parser.add_argument(
        "--siemens-dir",
        default="output/siemens",
        help="Instrumented binaries root directory in output (default: output/siemens)"
    )
    parser.add_argument(
        "--out-results",
        "--out-datasets",
        dest="out_results",
        default="output/results",
        help="Directory to write compact EFL v2 results (default: output/results)",
    )
    parser.add_argument(
        "--max-tests",
        type=int,
        default=None,
        help="Limit number of tests to execute per version (default: None, runs full universe)"
    )
    parser.add_argument(
        "-j", "--workers",
        type=int,
        default=6,
        help="Parallel worker threads (default: 6)"
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip versions whose output summary.json and traces.tsv.gz already exist"
    )

    args = parser.parse_args()

    input_base = Path(args.input_dir).resolve()
    siemens_base = Path(args.siemens_dir).resolve()
    results_base = Path(args.out_results).resolve()

    if args.repo == "all":
        target_repos = SIEMENS_REPOS
    else:
        target_repos = [r.strip() for r in args.repo.split(",") if r.strip()]

    print("=" * 70)
    print(f" Siemens & SIR Test Scenario Runner & Dataset Generator (v{instrumenter.__version__})")
    print("=" * 70)
    print(f"Benchmark root   : {siemens_base}")
    print(f"Output results   : {results_base}")
    print(f"Target repos     : {', '.join(target_repos)}")
    print(f"Target version   : {args.version}")
    print(f"Max tests limit  : {args.max_tests if args.max_tests else 'Full Universe'}")
    print(f"Worker threads   : {args.workers}")
    print("=" * 70)

    all_summaries = []

    for r_name in target_repos:
        r_dir = siemens_base / r_name
        if not r_dir.is_dir():
            print(f"[-] Repo directory not found: {r_dir}")
            continue

        v_names_set = {
            d.name for d in r_dir.iterdir()
            if d.is_dir() and re.match(r"^v\d+$", d.name)
        }
        for legacy_base in (
            r_dir / "source.alt",
            r_dir / "versions.alt" / "versions.orig",
            r_dir / "versions.alt" / "versions.seeded",
        ):
            if legacy_base.is_dir():
                v_names_set.update(
                    d.name for d in legacy_base.iterdir()
                    if d.is_dir() and re.match(r"^v\d+$", d.name)
                )
        v_names = sorted(v_names_set, key=natural_sort_key)

        target_versions = v_names if args.version == "all" else [args.version]
        repo_orig_cache = {}

        for v_name in target_versions:
            if v_name not in v_names:
                print(f"[-] Version {v_name} not found in {r_name}")
                continue

            ver_out = results_base / r_name / v_name / "full"
            if args.skip_existing and (ver_out / "summary.json").is_file() and (ver_out / "traces.tsv.gz").is_file():
                print(f"[SKIP] {r_name} {v_name} (already exists)")
                continue

            summary = execute_version_tests(
                r_name,
                v_name,
                siemens_base,
                input_base,
                results_base,
                args.max_tests,
                args.workers,
                orig_cache=repo_orig_cache,
            )
            if summary:
                all_summaries.append(summary)

    summary_path = results_base / "execution_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(all_summaries, indent=2), encoding="utf-8")

    readme_src = Path(__file__).resolve().parent.parent / "DATASET_FORMAT_README.md"
    readme_dst = results_base / "DATASET_README.md"
    if readme_src.is_file():
        readme_dst.write_text(readme_src.read_text(encoding="utf-8"), encoding="utf-8")

    print("\n" + "=" * 70)
    print(f" All executions finished! Summary written to: {summary_path}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
