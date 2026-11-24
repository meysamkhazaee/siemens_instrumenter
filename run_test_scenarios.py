#!/usr/bin/env python3
"""
Siemens Benchmark Suite: Test Scenario Runner, Path Divergence Tracer & Dataset Generator.

Capabilities:
  1. Multi-threaded test execution for both healthy (source/) and faulty (source.alt/vX/) versions.
  2. Automatic identification of fault lines and mapping to basic-block P-points.
  3. Classification of every test case into:
     - PASS (T): Output matches oracle, fault location was NOT visited.
     - COINCIDENTAL_CORRECT (CC / T): Output matches oracle, BUT fault location WAS visited!
     - FAIL (F): Output differs from oracle (or returncode mismatch).
  4. Generation of standard ML / FL dataset files:
     - Line 1..N: Full execution P-traces of each test case.
     - Line N+1: Test outcome verdicts (T,T,T,F,T,F,F,T,T,T).
  5. Export of detailed JSON / CSV metadata for deep analysis.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import difflib
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
import time

import instrumenter


SIEMENS_REPOS = ["printtokens", "printtokens2", "replace", "schedule", "schedule2", "tcas", "totinfo"]


def natural_sort_key(s):
    """Sort v1, v2, ... v10 in natural numerical order."""
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]


def parse_universe_tests(universe_file: Path, base_dir: Path):
    """Parse universe file into a list of test descriptors: (test_id, args, stdin_file)."""
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

            # Locate stdin file
            candidates = [
                base_dir / "inputs" / stdin_rel,
                base_dir / stdin_rel,
            ]
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
                "p_marker": p_cand.marker,
                "p_str": f"P{p_cand.marker} (line {p_cand.line}, {p_cand.kind})",
            })

    return fault_markers, fault_details, points


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

    try:
        # Run original (Oracle)
        cmd_orig = [str(exe_orig), "--p-output", str(trace_orig_file)] + args
        r_orig = subprocess.run(cmd_orig, input=stdin_data, capture_output=True, timeout=10)

        # Run faulty version
        cmd_faulty = [str(exe_faulty), "--p-output", str(trace_faulty_file)] + args
        r_faulty = subprocess.run(cmd_faulty, input=stdin_data, capture_output=True, timeout=10)
    except subprocess.TimeoutExpired:
        timed_out = True

    # Read traces
    t_orig = ""
    if trace_orig_file.exists():
        try:
            t_orig = trace_orig_file.read_text(encoding="utf-8", errors="replace").strip().rstrip(",")
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

    faulty_points = t_faulty.split(",") if t_faulty else []

    # Check outcome
    if timed_out:
        is_fail = True
    elif r_orig is None or r_faulty is None:
        is_fail = True
    else:
        is_fail = (r_orig.stdout != r_faulty.stdout or r_orig.returncode != r_faulty.returncode)

    # Check if fault location was visited in execution path
    fault_visited = any(f"P{m}" in faulty_points for m in fault_markers if m is not None)

    # Classification:
    # 1. FAIL (F): output mismatch
    # 2. COINCIDENTAL_CORRECT (CC): output matches, BUT fault location was visited
    # 3. PASS (T): output matches, and fault location was not visited
    if is_fail:
        tag_strict = "F"
        tag_rich = "F"
        classification = "FAIL"
    elif fault_visited:
        tag_strict = "T"
        tag_rich = "CC"
        classification = "COINCIDENTAL_CORRECT"
    else:
        tag_strict = "T"
        tag_rich = "T"
        classification = "PASS"

    return {
        "test_id": test_id,
        "args": args,
        "classification": classification,
        "tag_strict": tag_strict,
        "tag_rich": tag_rich,
        "trace_faulty": t_faulty,
        "trace_orig": t_orig,
        "fault_visited": fault_visited,
        "trace_len": len(faulty_points),
    }


def execute_version_tests(
    repo_name: str,
    version_name: str,
    siemens_output_base: Path,
    siemens_input_base: Path,
    output_datasets_base: Path,
    max_tests: int = None,
    workers: int = 6
):
    """Run all tests for a specific repo and version, and write datasets."""
    repo_dir = siemens_output_base / repo_name
    exe_orig = repo_dir / "source" / f"{get_source_stem(repo_name)}.exe"
    if not exe_orig.is_file():
        exe_orig = repo_dir / "source.alt" / "source.orig" / f"{get_source_stem(repo_name)}.exe"

    exe_faulty = repo_dir / "source.alt" / version_name / f"{get_source_stem(repo_name)}.exe"
    if not exe_faulty.is_file():
        exe_faulty = repo_dir / "versions.alt" / "versions.orig" / version_name / f"{get_source_stem(repo_name)}.exe"

    if not exe_orig.is_file() or not exe_faulty.is_file():
        print(f"[-] Executables not found for {repo_name} {version_name}: orig={exe_orig.exists()}, faulty={exe_faulty.exists()}")
        return None

    # Original uninstrumented sources to accurately map diff lines to AST P-markers
    in_repo_dir = siemens_input_base / repo_name
    orig_source = in_repo_dir / "source" / f"{get_source_stem(repo_name)}.c"
    if not orig_source.is_file():
        orig_source = in_repo_dir / "source.alt" / "source.orig" / f"{get_source_stem(repo_name)}.c"

    faulty_source = in_repo_dir / "source.alt" / version_name / f"{get_source_stem(repo_name)}.c"
    if not faulty_source.is_file():
        faulty_source = in_repo_dir / "versions.alt" / "versions.orig" / version_name / f"{get_source_stem(repo_name)}.c"

    # 1. Fault markers
    fault_markers, fault_details, all_points = get_fault_markers(orig_source, faulty_source)

    # 2. Parse universe tests
    universe_path = repo_dir / "testplans.alt" / "universe"
    test_items = parse_universe_tests(universe_path, repo_dir)
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

    # Summary statistics
    n_pass = sum(1 for r in results if r["classification"] == "PASS")
    n_cc = sum(1 for r in results if r["classification"] == "COINCIDENTAL_CORRECT")
    n_fail = sum(1 for r in results if r["classification"] == "FAIL")

    print(f"  --> Completed in {duration}s ({total_tests / max(duration, 0.001):.1f} tests/sec)")
    print(f"  --> PASS: {n_pass} | COINCIDENTAL_CORRECT (CC): {n_cc} | FAIL: {n_fail}")

    # Write dataset files
    dest_dir = output_datasets_base / repo_name
    dest_dir.mkdir(parents=True, exist_ok=True)

    # 1. Standard Dataset (Strict T/F verdict vector at the bottom)
    dataset_file = dest_dir / f"{version_name}_dataset.txt"
    with open(dataset_file, "w", encoding="utf-8") as f:
        for r in results:
            f.write(r["trace_faulty"] + "\n")
        # Final line: verdict vector
        verdict_line = ",".join(r["tag_strict"] for r in results)
        f.write(verdict_line + "\n")

    # 2. Tagged Dataset (with CC tag for coincidental correctness)
    tagged_file = dest_dir / f"{version_name}_tagged.txt"
    with open(tagged_file, "w", encoding="utf-8") as f:
        for r in results:
            f.write(r["trace_faulty"] + "\n")
        verdict_line_cc = ",".join(r["tag_rich"] for r in results)
        f.write(verdict_line_cc + "\n")

    # 3. Detailed JSON Dataset
    json_file = dest_dir / f"{version_name}_detailed.json"
    detailed_data = {
        "repo": repo_name,
        "version": version_name,
        "total_tests": total_tests,
        "duration_sec": duration,
        "fault_markers": [f"P{m}" for m in sorted(fault_markers)],
        "fault_details": fault_details,
        "summary": {
            "pass": n_pass,
            "coincidental_correct": n_cc,
            "fail": n_fail,
        },
        "verdict_vector_strict": [r["tag_strict"] for r in results],
        "verdict_vector_tagged": [r["tag_rich"] for r in results],
        "tests": [
            {
                "test_id": r["test_id"],
                "args": r["args"],
                "verdict": r["classification"],
                "fault_visited": r["fault_visited"],
                "trace_len": r["trace_len"],
                "trace": r["trace_faulty"],
            }
            for r in results
        ],
    }
    json_file.write_text(json.dumps(detailed_data, indent=2), encoding="utf-8")

    # 4. EFL-compatible 3-field CSV: cmp.v0.v<id>.<repo>.csv
    cmp_csv_file = dest_dir / f"cmp.v0.{version_name}.{repo_name}.csv"
    with open(cmp_csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for r in results:
            if not r["trace_faulty"].strip():
                continue
            writer.writerow([f"T{r['test_id']}", r["tag_strict"], r["trace_faulty"]])

    # 5. EFL source_map.csv
    source_map_file = dest_dir / "source_map.csv"
    with open(source_map_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["predicate", "id", "file", "line", "column", "function", "address"])
        for p in all_points:
            writer.writerow([f"P{p.marker}", p.marker, orig_source.name, p.line, getattr(p, "col", 0), getattr(p, "func", "main"), "0x0"])

    # 6. EFL source_changes.json
    source_changes_file = dest_dir / "source_changes.json"
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
            "reference_text": [],
            "faulty_text": [],
            "header": False,
        })
    source_changes_file.write_text(json.dumps(changes_list, indent=2), encoding="utf-8")

    # 7. EFL metadata.json
    v_num = int(version_name.lstrip("v"))
    metadata_file = dest_dir / "metadata.json"
    metadata_file.write_text(json.dumps({
        "program": repo_name,
        "version": v_num,
        "test_set": "full",
    }, indent=2), encoding="utf-8")

    # 8. EFL cache_manifest.json
    manifest_file = dest_dir / "cache_manifest.json"
    outputs = []
    for p in [cmp_csv_file, source_map_file, source_changes_file, metadata_file]:
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        outputs.append({"name": p.name, "size": p.stat().st_size, "sha256": h})
    manifest_file.write_text(json.dumps({
        "schema": 1,
        "fingerprint": f"siemens-{repo_name}-{version_name}",
        "options": {},
        "outputs": outputs,
    }, indent=2), encoding="utf-8")

    # 9. Mirror to standard EFL hierarchy: output/results/<repo>/<version>/full/
    results_ver_dir = output_datasets_base.parent / "results" / repo_name / version_name / "full"
    results_ver_dir.mkdir(parents=True, exist_ok=True)
    for p in [cmp_csv_file, source_map_file, source_changes_file, metadata_file, manifest_file, dataset_file, tagged_file, json_file]:
        try:
            shutil.copy2(p, results_ver_dir / p.name)
        except Exception:
            pass

    return {
        "repo": repo_name,
        "version": version_name,
        "total_tests": total_tests,
        "duration_sec": duration,
        "n_pass": n_pass,
        "n_cc": n_cc,
        "n_fail": n_fail,
        "dataset_file": str(dataset_file),
        "tagged_file": str(tagged_file),
        "json_file": str(json_file),
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
        "--out-datasets",
        default="output/datasets",
        help="Directory to write output test datasets (default: output/datasets)"
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
    datasets_base = Path(args.out_datasets).resolve()

    target_repos = SIEMENS_REPOS if args.repo == "all" else [args.repo]

    print("=" * 70)
    print(f" Siemens Suite Test Scenario Runner & Trace Dataset Generator (v{instrumenter.__version__})")
    print("=" * 70)
    print(f"Siemens base dir : {siemens_base}")
    print(f"Output datasets  : {datasets_base}")
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

        # Collect versions
        salt_dir = r_dir / "source.alt"
        if not salt_dir.is_dir():
            salt_dir = r_dir / "versions.alt" / "versions.orig"

        v_names = [d.name for d in salt_dir.iterdir() if d.is_dir() and re.match(r"^v\d+$", d.name)]
        v_names.sort(key=natural_sort_key)

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
                datasets_base,
                args.max_tests,
                args.workers
            )
            if summary:
                all_summaries.append(summary)

    # Save summary report
    summary_path = datasets_base / "execution_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(all_summaries, indent=2), encoding="utf-8")

    print("\n" + "=" * 70)
    print(f" All executions finished! Summary written to: {summary_path}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
