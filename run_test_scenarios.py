#!/usr/bin/env python3
"""
Siemens Benchmark Suite: Test Scenario Runner, Path Divergence Tracer & Dataset Generator.

Capabilities:
  1. Multi-threaded test execution for both healthy (orig/) and faulty (vX/) versions.
  2. Automatic identification of fault lines and mapping to basic-block P-points.
  3. Classification of every test case into:
     - PASS (T): Output matches oracle, fault location was NOT visited.
     - COINCIDENTAL_CORRECT (CC): Output matches oracle, BUT fault location WAS visited.
     - FAIL (F): Output differs from oracle (or returncode mismatch / timeout).
  4. Generation of compact, self-describing EFL v2 datasets under
     output/results/<repo>/<version>/full/:
     - traces.tsv.gz: Gzip-compressed TSV with header (test_id, status, cc, path).
     - tests.tsv: Mapping from T1..TN to exact universe test invocations & metadata.
     - summary.json: Compact version summary and fault predicates.
     - source_map.csv, source_changes.json, metadata.json, cache_manifest.json.
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
import subprocess
import sys
import tempfile
import time

import instrumenter


SIEMENS_REPOS = ["printtokens", "printtokens2", "replace", "schedule", "schedule2", "tcas", "totinfo"]


def natural_sort_key(s):
    """Sort v1, v2, ... v10 in natural numerical order."""
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]


def parse_universe_tests(universe_file: Path, *asset_roots: Path):
    """Parse universe file into a list of test descriptors."""
    if not universe_file.is_file():
        return []

    tests = []
    lines = universe_file.read_text(encoding="utf-8", errors="replace").splitlines()
    for idx, l in enumerate(lines, 1):
        l = l.strip()
        if not l or l.startswith("#"):
            continue

        if "<" in l:
            arg_str, stdin_rel = l.rsplit("<", 1)
            arg_str = arg_str.strip()
            stdin_rel = stdin_rel.strip()

            candidates = []
            for base_dir in asset_roots:
                candidates.append(base_dir / "inputs" / stdin_rel)
                candidates.append(base_dir / stdin_rel)
            stdin_file = None
            for c in candidates:
                if c.is_file():
                    stdin_file = c
                    break

            try:
                args = shlex.split(arg_str) if arg_str else []
            except Exception:
                args = arg_str.split()

            tests.append({
                "test_id": idx,
                "args": args,
                "stdin_file": stdin_file,
                "raw_line": l,
            })
        else:
            try:
                args = shlex.split(l)
            except Exception:
                args = l.split()
            tests.append({
                "test_id": idx,
                "args": args,
                "stdin_file": None,
                "raw_line": l,
            })

    return tests


def get_fault_markers(orig_source: Path, faulty_source: Path):
    """Parse AST, map P points and identify which P-markers correspond to mutated lines."""
    compiler = instrumenter.find_compiler("c")
    include_arg = [f"-I{orig_source.parent}"]
    ast, _, legacy = instrumenter.run_clang_ast(orig_source, "c", compiler, include_arg)
    source_data = orig_source.read_bytes()
    points = instrumenter.collect_points(ast, orig_source, source_data, legacy)
    points = instrumenter.remove_physical_duplicates(points)
    for idx, p in enumerate(points, 1):
        p.marker = idx

    orig_lines = orig_source.read_text(encoding="utf-8", errors="replace").splitlines()
    faulty_lines = faulty_source.read_text(encoding="utf-8", errors="replace").splitlines()

    matcher = difflib.SequenceMatcher(None, orig_lines, faulty_lines)
    fault_markers = set()
    fault_details = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue

        start_line = i1 + 1
        end_line = max(start_line, i2)

        # Map to closest / enclosing P-point
        exact = [p for p in points if p.line == start_line]
        if exact:
            p_cand = exact[0]
        else:
            preceding = [p for p in points if p.line <= start_line]
            p_cand = preceding[-1] if preceding else (points[0] if points else None)

        if p_cand:
            fault_markers.add(p_cand.marker)
            fault_details.append({
                "type": tag,
                "orig_lines": f"{start_line}-{end_line}" if start_line != end_line else str(start_line),
                "orig_text": orig_lines[i1:i2],
                "faulty_lines": f"{j1 + 1}-{max(j1 + 1, j2)}" if j2 > j1 + 1 else ([str(j1 + 1)] if j2 > j1 else []),
                "faulty_text": faulty_lines[j1:j2],
                "p_marker": p_cand.marker,
                "p_str": f"P{p_cand.marker} (line {p_cand.line}, {p_cand.kind})",
            })

    return fault_markers, fault_details, points


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


def run_single_test_pair(test_item, exe_orig: Path, exe_faulty: Path, fault_markers: set, temp_dir: Path):
    """Execute a single test case on both versions and evaluate outcomes and traces."""
    test_id = test_item["test_id"]
    args = test_item["args"]
    stdin_file = test_item["stdin_file"]
    stdin_data = stdin_file.read_bytes() if (stdin_file and stdin_file.is_file()) else None

    trace_orig_file = temp_dir / f"trace_orig_{test_id}_{os.getpid()}.txt"
    trace_faulty_file = temp_dir / f"trace_faulty_{test_id}_{os.getpid()}.txt"

    r_orig = None
    r_faulty = None
    timed_out = False
    started = time.perf_counter()

    try:
        cmd_orig = [str(exe_orig), "--p-output", str(trace_orig_file)] + args
        r_orig = subprocess.run(cmd_orig, input=stdin_data, capture_output=True, timeout=10)

        cmd_faulty = [str(exe_faulty), "--p-output", str(trace_faulty_file)] + args
        r_faulty = subprocess.run(cmd_faulty, input=stdin_data, capture_output=True, timeout=10)
    except subprocess.TimeoutExpired:
        timed_out = True
    duration_sec = round(time.perf_counter() - started, 4)

    if trace_orig_file.exists():
        try:
            trace_orig_file.unlink(missing_ok=True)
        except Exception:
            pass

    t_faulty = ""
    if trace_faulty_file.exists():
        try:
            t_faulty = trace_faulty_file.read_text(encoding="utf-8", errors="replace").strip().rstrip(",")
            trace_faulty_file.unlink(missing_ok=True)
        except Exception:
            pass

    trace_ids = _parse_trace_ids(t_faulty)
    fault_marker_strs = {str(m) for m in fault_markers if m is not None}

    if timed_out:
        is_fail = True
    elif r_orig is None or r_faulty is None:
        is_fail = True
    else:
        is_fail = (r_orig.stdout != r_faulty.stdout or r_orig.returncode != r_faulty.returncode)

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
    candidates = [
        input_base / repo_name,
        output_repo_dir,
    ]
    for cand in candidates:
        if (cand / "testplans.alt" / "universe").is_file() or (cand / "testplans" / "universe").is_file():
            return cand
    return input_base / repo_name


def execute_version_tests(
    repo_name: str,
    version_name: str,
    siemens_output_base: Path,
    siemens_input_base: Path,
    output_results_base: Path,
    max_tests: int = None,
    workers: int = 6
):
    """Run all tests for a specific repo and version, and write compact EFL v2 datasets."""
    repo_dir = siemens_output_base / repo_name
    stem = get_source_stem(repo_name)
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

    in_repo_dir = siemens_input_base / repo_name
    orig_source = in_repo_dir / "source" / f"{stem}.c"
    if not orig_source.is_file():
        orig_source = in_repo_dir / "source.alt" / "source.orig" / f"{stem}.c"

    faulty_source = in_repo_dir / "source.alt" / version_name / f"{stem}.c"
    if not faulty_source.is_file():
        faulty_source = in_repo_dir / "versions.alt" / "versions.orig" / version_name / f"{stem}.c"

    # 1. Fault markers
    fault_markers, fault_details, all_points = get_fault_markers(orig_source, faulty_source)

    # 2. Parse universe tests directly from input assets (fallback to output dir if needed)
    assets_dir = _find_siemens_assets_dir(siemens_input_base, repo_dir, repo_name)
    universe_path = assets_dir / "testplans.alt" / "universe"
    if not universe_path.is_file():
        universe_path = assets_dir / "testplans" / "universe"
    test_items = parse_universe_tests(universe_path, assets_dir, repo_dir)
    if max_tests and max_tests > 0:
        test_items = test_items[:max_tests]

    total_tests = len(test_items)
    print(f"\n[{repo_name.upper()} - {version_name}] Running {total_tests} tests with {workers} workers...")
    print(f"  Fault markers: {[f'P{m}' for m in sorted(fault_markers)]}")

    start_time = time.time()
    results = [None] * total_tests

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_idx = {
                executor.submit(
                    run_single_test_pair, item, exe_orig, exe_faulty, fault_markers, tmp_path
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

    # Write ONLY the compact canonical EFL v2 output under results/<repo>/<version>/full/
    results_ver_dir = output_results_base / repo_name / version_name / "full"
    results_ver_dir.mkdir(parents=True, exist_ok=True)

    # 1. traces.tsv.gz (canonical gzip-compressed TSV trace table)
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

    # 2. tests.tsv (complete test inventory & mapping T<id> -> exact universe command)
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
    with open(source_map_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["predicate", "id", "file", "line", "column", "function", "kind", "address"])
        for p in all_points:
            writer.writerow([
                f"P{p.marker}",
                p.marker,
                orig_source.name,
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
        orig_lines_str = str(fd.get("orig_lines", "1"))
        if "-" in orig_lines_str:
            start_l, end_l = map(int, orig_lines_str.split("-"))
            f_lines = list(range(start_l, end_l + 1))
        else:
            f_lines = [int(orig_lines_str)]
        p_cand_line = next((p.line for p in all_points if p.marker == fd.get("p_marker")), None)
        if p_cand_line and p_cand_line not in f_lines:
            f_lines.append(p_cand_line)
        changes_list.append({
            "file": orig_source.name,
            "kind": fd.get("type", "replace"),
            "reference_lines": f_lines,
            "faulty_lines": f_lines,
            "reference_text": fd.get("orig_text", []),
            "faulty_text": fd.get("faulty_text", []),
            "header": False,
        })
    source_changes_file.write_text(json.dumps(changes_list, indent=2), encoding="utf-8")

    # 5. metadata.json
    v_num = int(version_name.lstrip("v"))
    metadata_file = results_ver_dir / "metadata.json"
    metadata_file.write_text(json.dumps({
        "benchmark": "Siemens",
        "program": repo_name,
        "version": v_num,
        "test_set": "full",
        "source_file": orig_source.name,
        "orig_source": str(orig_source),
        "faulty_source": str(faulty_source),
    }, indent=2), encoding="utf-8")

    # 6. summary.json (lightweight summary without repeating traces)
    summary_file = results_ver_dir / "summary.json"
    summary_file.write_text(json.dumps({
        "schema": "efl-dataset-v2",
        "benchmark": "Siemens",
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
        "fingerprint": f"siemens-{repo_name}-{version_name}",
        "options": {"format": "efl-dataset-v2"},
        "outputs": outputs,
    }, indent=2), encoding="utf-8")

    return {
        "repo": repo_name,
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


def get_source_stem(repo_name: str) -> str:
    stems = {
        "printtokens": "print_tokens",
        "printtokens2": "print_tokens2",
        "replace": "replace",
        "schedule": "schedule",
        "schedule2": "schedule2",
        "tcas": "tcas",
        "totinfo": "tot_info",
    }
    return stems.get(repo_name, repo_name)


def main():
    parser = argparse.ArgumentParser(
        description="Siemens Suite Test Scenario Runner and Trace Dataset Generator"
    )
    parser.add_argument(
        "--repo",
        default="tcas",
        help="Repository to test (e.g. tcas, schedule, replace, or 'all')"
    )
    parser.add_argument(
        "--version",
        default="v1",
        help="Version to test (e.g. v1, v2, or 'all')"
    )
    parser.add_argument(
        "--input-dir",
        default="input/siemens",
        help="Siemens root directory in input with original uninstrumented sources (default: input/siemens)"
    )
    parser.add_argument(
        "--siemens-dir",
        default="output/siemens",
        help="Siemens root directory in output (default: output/siemens)"
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

    args = parser.parse_args()

    input_base = Path(args.input_dir).resolve()
    siemens_base = Path(args.siemens_dir).resolve()
    results_base = Path(args.out_results).resolve()

    target_repos = SIEMENS_REPOS if args.repo == "all" else [args.repo]

    print("=" * 70)
    print(f" Siemens Suite Test Scenario Runner & Trace Dataset Generator (v{instrumenter.__version__})")
    print("=" * 70)
    print(f"Siemens base dir : {siemens_base}")
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

        # Collect versions from compact layout (output/siemens/<repo>/vN) or legacy layouts
        v_names_set = {
            d.name for d in r_dir.iterdir()
            if d.is_dir() and re.match(r"^v\d+$", d.name)
        }
        for legacy_base in (r_dir / "source.alt", r_dir / "versions.alt" / "versions.orig"):
            if legacy_base.is_dir():
                v_names_set.update(
                    d.name for d in legacy_base.iterdir()
                    if d.is_dir() and re.match(r"^v\d+$", d.name)
                )
        v_names = sorted(v_names_set, key=natural_sort_key)

        target_versions = v_names if args.version == "all" else [args.version]

        for v_name in target_versions:
            if v_name not in v_names:
                print(f"[-] Version {v_name} not found in {r_name}")
                continue

            summary = execute_version_tests(
                r_name,
                v_name,
                siemens_base,
                input_base,
                results_base,
                args.max_tests,
                args.workers
            )
            if summary:
                all_summaries.append(summary)

    # Save summary report and dataset format README
    summary_path = results_base / "execution_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(all_summaries, indent=2), encoding="utf-8")

    readme_candidates = [
        Path(__file__).resolve().parent / "DATASET_FORMAT_README.md",
        Path(__file__).resolve().parent.parent / "DATASET_FORMAT_README.md",
    ]
    readme_dst = results_base / "DATASET_README.md"
    for readme_src in readme_candidates:
        if readme_src.is_file():
            readme_dst.write_text(readme_src.read_text(encoding="utf-8"), encoding="utf-8")
            break

    print("\n" + "=" * 70)
    print(f" All executions finished! Summary written to: {summary_path}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
