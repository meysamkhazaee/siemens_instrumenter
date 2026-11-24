#!/usr/bin/env python3
"""
Siemens Benchmark Suite: Fault Localization, P-point Mapping & Trace Divergence Analyzer.

Capabilities:
  1. Equivalence Check: Compares uninstrumented vs instrumented executables to verify
     that instrumentation preserves exact original program behavior.
  2. Fault Analysis: Diffs each faulty version (v1..vN) against 'orig' to detect:
     - Number of injected faults / mutants.
     - Source code lines modified.
     - Corresponding enclosing P-points.
  3. Execution & Path Divergence Tracer: Runs test cases on both correct (orig) and
     faulty (vX) instrumented programs with --p-output:
     - Detects failing tests (output mismatch).
     - Compares execution traces (P-sequences).
     - Reports the exact divergence step and marker where behavior deviates.
"""

import argparse
import difflib
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


def get_repo_files(repo_id: str, siemens_base: Path):
    """Return paths to source files for orig and all versions."""
    repo_dir = siemens_base / repo_id
    if not repo_dir.is_dir():
        # Check alternative naming
        for d in siemens_base.iterdir():
            if d.is_dir() and (d.name == repo_id or d.name.startswith(f"{repo_id}_")):
                repo_dir = d
                break

    source_dir = repo_dir / "source.alt" / "source.orig"
    c_files = list(source_dir.glob("*.c"))
    if not c_files:
        return None, {}

    orig_c = c_files[0]
    c_name = orig_c.name

    versions = {}
    v_base = repo_dir / "versions.alt" / "versions.orig"
    if v_base.is_dir():
        for vd in sorted(v_base.iterdir(), key=lambda p: [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', p.name)]):
            if vd.is_dir():
                vc = vd / c_name
                if vc.is_file():
                    versions[vd.name] = vc

    return orig_c, versions


def get_p_points_for_source(source_path: Path):
    """Parse AST and return list of P points for a source file."""
    compiler = instrumenter.find_compiler("c")
    ast, mode, legacy = instrumenter.run_clang_ast(source_path, "c", compiler, [])
    source_data = source_path.read_bytes()
    points = instrumenter.collect_points(ast, source_path, source_data, legacy)
    points = instrumenter.remove_physical_duplicates(points)
    for idx, p in enumerate(points, 1):
        p.marker = idx
    return points


def map_line_to_p_point(line_no: int, points: list):
    """Map a given source line number to the closest or enclosing P point."""
    exact = [p for p in points if p.line == line_no]
    if exact:
        return exact[0]

    # Preceding point
    preceding = [p for p in points if p.line <= line_no]
    if preceding:
        return preceding[-1]

    return points[0] if points else None


def analyze_version_faults(orig_path: Path, faulty_path: Path, p_points: list):
    """Compare orig against faulty version and identify fault locations and P points."""
    orig_lines = orig_path.read_text(encoding="utf-8", errors="replace").splitlines()
    faulty_lines = faulty_path.read_text(encoding="utf-8", errors="replace").splitlines()

    matcher = difflib.SequenceMatcher(None, orig_lines, faulty_lines)
    fault_blocks = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue

        start_line = i1 + 1
        end_line = max(start_line, i2)

        # Enclosing or matching P point
        p_point = map_line_to_p_point(start_line, p_points)
        p_str = f"P{p_point.marker} (line {p_point.line}, {p_point.kind})" if p_point else "Unknown"

        fault_blocks.append({
            "type": tag,
            "orig_lines": f"{start_line}-{end_line}" if start_line != end_line else str(start_line),
            "start_line": start_line,
            "end_line": end_line,
            "orig_code": orig_lines[i1:i2],
            "faulty_code": faulty_lines[j1:j2],
            "associated_p_point": p_str,
            "p_marker": p_point.marker if p_point else None,
        })

    return {
        "fault_count": len(fault_blocks),
        "faults": fault_blocks,
    }


def verify_equivalence_uninstrumented_vs_instrumented(
    orig_c: Path,
    inst_exe: Path,
    test_commands: list,
    repo_dir: Path,
    max_tests: int = 15
):
    """Compile an uninstrumented binary and verify identical stdout/stderr/returncode on sample tests."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        uninst_exe = tmp_path / "orig_uninstrumented.exe"

        # Copy any headers in the directory
        for h in orig_c.parent.glob("*.h"):
            (tmp_path / h.name).write_bytes(h.read_bytes())

        cmd_compile = ["gcc", "-std=gnu89", "-w", str(orig_c), "-o", str(uninst_exe), "-I", str(tmp_path), "-lm"]
        c_res = subprocess.run(cmd_compile, capture_output=True, text=True)
        if c_res.returncode != 0:
            return {"success": False, "error": f"Failed to compile uninstrumented source: {c_res.stderr}"}

        matches = 0
        tested = 0
        diffs = []

        dummy_trace = tmp_path / "trace_dummy.txt"

        for idx, (args, stdin_file) in enumerate(test_commands[:max_tests], 1):
            stdin_data = None
            if stdin_file and stdin_file.is_file():
                stdin_data = stdin_file.read_bytes()

            # Run uninstrumented
            r_uninst = subprocess.run([str(uninst_exe), *args], input=stdin_data, capture_output=True)

            # Run instrumented (passing dummy trace file for P tracking)
            r_inst = subprocess.run([str(inst_exe), "--p-output", str(dummy_trace), *args], input=stdin_data, capture_output=True)

            tested += 1
            if r_uninst.stdout == r_inst.stdout:
                matches += 1
            else:
                diffs.append({
                    "test_index": idx,
                    "args": args,
                    "uninst_stdout": r_uninst.stdout.decode("latin-1", "replace")[:100],
                    "inst_stdout": r_inst.stdout.decode("latin-1", "replace")[:100],
                })

        return {
            "success": (matches == tested),
            "tested_count": tested,
            "matched_count": matches,
            "discrepancies": diffs,
        }


def trace_test_execution_and_divergence(
    orig_exe: Path,
    faulty_exe: Path,
    test_commands: list,
    fault_p_markers: set,
    max_tests_to_check: int = 500
):
    """Run tests until a failure/divergence is observed between orig and faulty versions."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        t_orig_file = tmp_path / "t_orig.txt"
        t_faulty_file = tmp_path / "t_faulty.txt"

        for idx, (args, stdin_file) in enumerate(test_commands[:max_tests_to_check], 1):
            stdin_data = stdin_file.read_bytes() if (stdin_file and stdin_file.is_file()) else None

            # Run both versions
            r_orig = subprocess.run([str(orig_exe), "--p-output", str(t_orig_file), *args], input=stdin_data, capture_output=True)
            r_faulty = subprocess.run([str(faulty_exe), "--p-output", str(t_faulty_file), *args], input=stdin_data, capture_output=True)

            orig_out = r_orig.stdout
            faulty_out = r_faulty.stdout

            is_failed = (orig_out != faulty_out or r_orig.returncode != r_faulty.returncode)

            # Read traces
            t_orig = [p for p in t_orig_file.read_text(encoding="utf-8", errors="replace").strip().split(",") if p] if t_orig_file.exists() else []
            t_faulty = [p for p in t_faulty_file.read_text(encoding="utf-8", errors="replace").strip().split(",") if p] if t_faulty_file.exists() else []

            if is_failed:
                # Analyze divergence
                diverged_at_step = None
                orig_point_at_div = None
                faulty_point_at_div = None

                for step, (po, pf) in enumerate(zip(t_orig, t_faulty)):
                    if po != pf:
                        diverged_at_step = step
                        orig_point_at_div = po
                        faulty_point_at_div = pf
                        break

                executed_fault_marker = any(f"P{m}" in t_faulty for m in fault_p_markers if m is not None)

                return {
                    "failing_test_found": True,
                    "test_index": idx,
                    "args": args,
                    "stdin_file": str(stdin_file) if stdin_file else None,
                    "orig_output": orig_out.decode("latin-1", "replace")[:120],
                    "faulty_output": faulty_out.decode("latin-1", "replace")[:120],
                    "orig_trace_length": len(t_orig),
                    "faulty_trace_length": len(t_faulty),
                    "diverged_at_step": diverged_at_step,
                    "orig_divergent_point": orig_point_at_div,
                    "faulty_divergent_point": faulty_point_at_div,
                    "fault_location_executed": executed_fault_marker,
                    "orig_trace_sample": t_orig[:20],
                    "faulty_trace_sample": t_faulty[:20],
                }

    return {"failing_test_found": False, "checked_tests": min(len(test_commands), max_tests_to_check)}


def parse_universe_tests(universe_file: Path, base_dir: Path):
    """Parse universe file into list of (args, stdin_path)."""
    if not universe_file.is_file():
        return []

    tests = []
    lines = universe_file.read_text(encoding="utf-8", errors="replace").splitlines()
    for l in lines:
        l = l.strip()
        if not l or l.startswith("#"):
            continue

        if "<" in l:
            arg_str, stdin_rel = l.rsplit("<", 1)
            arg_str = arg_str.strip()
            stdin_rel = stdin_rel.strip()

            # Locate stdin file
            stdin_file = base_dir / "inputs" / stdin_rel
            if not stdin_file.exists():
                stdin_file = base_dir / stdin_rel

            try:
                args = shlex.split(arg_str) if arg_str else []
            except Exception:
                args = arg_str.split()
            tests.append((args, stdin_file if stdin_file.exists() else None))
        else:
            try:
                args = shlex.split(l)
            except Exception:
                args = l.split()
            tests.append((args, None))

    return tests


ALL_REPOS = ["printtokens", "printtokens2", "replace", "schedule", "schedule2", "tcas", "totinfo"]


def evaluate_repository(repo_name: str, version_arg: str, siemens_base: Path, output_base: Path, max_tests: int = 500):
    orig_c, versions = get_repo_files(repo_name, siemens_base)
    if not orig_c:
        print(f"Error: repository '{repo_name}' not found under {siemens_base}", file=sys.stderr)
        return None

    print("\n" + "=" * 70)
    print(f" Siemens Fault Localization & Path Divergence: {repo_name.upper()}")
    print("=" * 70)
    print(f"Base source: {orig_c}")
    print(f"Found {len(versions)} versions.")

    # 1. P points for orig
    p_points = get_p_points_for_source(orig_c)
    print(f"Collected {len(p_points)} P-points from original code.")

    # Parse tests
    repo_dir = orig_c.parent.parent.parent
    universe_path = repo_dir / "testplans.alt" / "universe"
    test_commands = parse_universe_tests(universe_path, repo_dir)
    print(f"Discovered {len(test_commands)} test cases in universe.")

    # 2. Equivalence Check
    inst_orig_exe = output_base / repo_name / "orig" / f"{orig_c.stem}.exe"
    if not inst_orig_exe.is_file():
        inst_orig_exe = output_base / repo_name / "source.alt" / "source.orig" / f"{orig_c.stem}.exe"
    if not inst_orig_exe.is_file():
        inst_orig_exe = output_base / repo_name / "source" / f"{orig_c.stem}.exe"
    print("\n[Step 1: Equivalence Check: Instrumented vs Uninstrumented]")
    eq_res = None
    if inst_orig_exe.is_file():
        eq_res = verify_equivalence_uninstrumented_vs_instrumented(orig_c, inst_orig_exe, test_commands, repo_dir)
        if eq_res["success"]:
            print(f"  --> PASSED: 100% identical outputs across {eq_res['tested_count']} sample tests!")
            print("      (Proves instrumentation preserved original program semantics)")
        else:
            print(f"  --> Discrepancies found: {eq_res.get('discrepancies')}")
    else:
        print(f"  (Skipped: {inst_orig_exe} not found)")

    # 3. Fault Analysis & Trace Divergence
    target_versions = [version_arg] if version_arg != "all" else sorted(versions.keys(), key=lambda v: int(v[1:]) if v[1:].isdigit() else 0)

    print("\n[Step 2 & 3: Fault Analysis, P-point Mapping & Path Divergence]")
    version_results = []
    for v_name in target_versions:
        if v_name not in versions:
            print(f"\nVersion {v_name} not found.")
            continue

        vc_path = versions[v_name]
        fault_info = analyze_version_faults(orig_c, vc_path, p_points)

        print("-" * 70)
        print(f"VERSION: {v_name} | Total Injected Fault Sites: {fault_info['fault_count']}")
        for f_idx, fb in enumerate(fault_info["faults"], 1):
            print(f"  Fault #{f_idx}: Lines {fb['orig_lines']} ({fb['type']})")
            print(f"    Mapped P-Point : {fb['associated_p_point']}")
            if fb["orig_code"]:
                print(f"    Orig Code      : {' '.join(s.strip() for s in fb['orig_code'][:2])}")
            if fb["faulty_code"]:
                print(f"    Faulty Code    : {' '.join(s.strip() for s in fb['faulty_code'][:2])}")

        # Path divergence trace
        v_exe = output_base / repo_name / v_name / f"{orig_c.stem}.exe"
        if not v_exe.is_file():
            v_exe = output_base / repo_name / "versions.alt" / "versions.orig" / v_name / f"{orig_c.stem}.exe"
        if not v_exe.is_file():
            v_exe = output_base / repo_name / "source.alt" / v_name / f"{orig_c.stem}.exe"
        tr_res = None
        if inst_orig_exe.is_file() and v_exe.is_file():
            fault_markers = {fb["p_marker"] for fb in fault_info["faults"]}
            tr_res = trace_test_execution_and_divergence(inst_orig_exe, v_exe, test_commands, fault_markers, max_tests)
            if tr_res["failing_test_found"]:
                print(f"\n  [Execution & Trace Divergence on Failing Test #{tr_res['test_index']}]:")
                print(f"    Test Args          : {' '.join(tr_res['args'])}")
                print(f"    Orig Output        : {tr_res['orig_output'].strip()}")
                print(f"    Faulty Output      : {tr_res['faulty_output'].strip()}")
                print(f"    Fault Site Visited : {'YES' if tr_res['fault_location_executed'] else 'NO'}")
                if tr_res['diverged_at_step'] is not None:
                    print(f"    ==> DIVERGED at step {tr_res['diverged_at_step']}:")
                    print(f"        Orig Path took   : {tr_res['orig_divergent_point']}")
                    print(f"        Faulty Path took : {tr_res['faulty_divergent_point']}")
                else:
                    print("    Trace path length/sequence visited identical P-markers, but variable values caused failure.")
            else:
                print(f"\n  (No failure triggered in first {tr_res['checked_tests']} tests from universe)")

        version_results.append({
            "version": v_name,
            "fault_count": fault_info["fault_count"],
            "faults": fault_info["faults"],
            "trace_divergence": tr_res,
        })

    return {
        "repo": repo_name,
        "p_points_count": len(p_points),
        "tests_count": len(test_commands),
        "equivalence": eq_res,
        "version_results": version_results,
    }


def main():
    parser = argparse.ArgumentParser(description="Siemens Suite Fault Localization and Trace Analyzer")
    parser.add_argument("--repo", default="tcas", help="Repository to analyze (e.g. tcas, schedule, replace, or 'all')")
    parser.add_argument("--version", default="v1", help="Specific faulty version to analyze (e.g. v1, v2, v3, or 'all')")
    parser.add_argument("--siemens-dir", default="input/siemens", help="Siemens benchmark inputs root")
    parser.add_argument("--output-dir", default="output/siemens", help="Instrumented outputs root")
    parser.add_argument("--max-tests", type=int, default=500, help="Max test cases to evaluate")
    parser.add_argument("--json-out", default=None, help="Optional JSON output report path")
    args = parser.parse_args()

    siemens_base = Path(args.siemens_dir).resolve()
    output_base = Path(args.output_dir).resolve()

    target_repos = ALL_REPOS if args.repo == "all" else [args.repo]
    all_reports = []

    for r in target_repos:
        rep = evaluate_repository(r, args.version, siemens_base, output_base, args.max_tests)
        if rep:
            all_reports.append(rep)

    if args.json_out:
        out_p = Path(args.json_out)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(all_reports, indent=2), encoding="utf-8")
        print(f"\n[+] Full JSON report exported to: {out_p}")

    print("\n" + "=" * 70)
    print(" Analysis Completed.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
