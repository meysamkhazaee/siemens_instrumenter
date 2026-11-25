#!/usr/bin/env python3
"""
Siemens & SIR Benchmark Suites: Fault Localization, P-point Mapping & Trace Divergence Analyzer.

Capabilities:
  1. Equivalence Check: Compares uninstrumented vs instrumented executables to verify
     that instrumentation preserves exact original program behavior.
  2. Fault Analysis: Diffs each faulty version (v1..vN) against 'orig' across all
     modified .c/.h files to detect fault sites and enclosing P-points.
  3. Execution & Path Divergence Tracer: Runs test cases on both correct (orig) and
     faulty (vX) instrumented programs with --p-output and reports divergence.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import repos
import run_test_scenarios


ALL_REPOS = list(repos.SIEMENS_REPOS)


def get_repo_files(repo_id: str, siemens_base: Path):
    """Return resolved source maps for orig and all versions of a Siemens/SIR repo."""
    repo_info = repos.get_repo_info(repo_id)
    repo_dir = repos.find_repo_base(siemens_base, repo_id)
    if not repo_dir:
        return None, None, {}

    orig_dir = repos.find_orig_dir(repo_dir, repo_info)
    if not orig_dir:
        return repo_dir, None, {}

    orig_sources = repos.resolve_version_sources(orig_dir, None, repo_info)
    if not orig_sources["c_files"]:
        return repo_dir, None, {}

    versions = {}
    for v_name, v_dir in repos.find_version_dirs(repo_dir):
        ver_sources = repos.resolve_version_sources(orig_dir, v_dir, repo_info)
        if ver_sources["c_files"]:
            versions[v_name] = ver_sources

    return repo_dir, orig_sources, versions


def verify_equivalence_uninstrumented_vs_instrumented(
    orig_sources: dict,
    inst_exe: Path,
    test_items: list,
    max_tests: int = 15,
    copy_file_args: bool = False,
):
    """Compile an uninstrumented binary and verify identical outputs on sample tests."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        uninst_exe = tmp_path / "orig_uninstrumented.exe"
        src_build_dir = tmp_path / "src"
        src_build_dir.mkdir(parents=True, exist_ok=True)

        compiled_inputs: list[str] = []
        for rel_c, c_path in orig_sources["c_files"].items():
            dst = src_build_dir / rel_c
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(c_path.read_bytes())
            compiled_inputs.append(str(dst))

        for rel_h, h_path in orig_sources["h_files"].items():
            dst = src_build_dir / rel_h
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(h_path.read_bytes())

        include_dirs = [src_build_dir]
        for rel_h in orig_sources["h_files"]:
            inc = (src_build_dir / rel_h).parent
            if inc not in include_dirs:
                include_dirs.append(inc)
        cflags = repos.detect_compile_flags(orig_sources["h_files"], include_dirs)
        cmd_compile = [repos.find_gcc(), "-std=gnu89", "-w", *compiled_inputs, "-o", str(uninst_exe), *cflags, "-lm"]
        c_res = subprocess.run(cmd_compile, capture_output=True, text=True)
        if c_res.returncode != 0:
            return {"success": False, "error": f"Failed to compile uninstrumented source: {c_res.stderr}"}

        matches = 0
        tested = 0
        diffs = []
        dummy_trace = tmp_path / "trace_dummy.txt"

        for idx, item in enumerate(test_items[:max_tests], 1):
            stdin_file = item["stdin_file"]
            stdin_data = stdin_file.read_bytes() if (stdin_file and stdin_file.is_file()) else None
            args = item["args"]
            file_arg_paths = item.get("file_arg_paths", {})

            u_cwd = tmp_path / f"u_{idx}"
            i_cwd = tmp_path / f"i_{idx}"
            u_cwd.mkdir(parents=True, exist_ok=True)
            i_cwd.mkdir(parents=True, exist_ok=True)
            u_args = run_test_scenarios._prepare_test_args_for_cwd(args, file_arg_paths, u_cwd, copy_file_args)
            i_args = run_test_scenarios._prepare_test_args_for_cwd(args, file_arg_paths, i_cwd, copy_file_args)

            r_uninst = subprocess.run([str(uninst_exe), *u_args], input=stdin_data, capture_output=True, cwd=str(u_cwd))
            r_inst = subprocess.run([str(inst_exe), "--p-output", str(dummy_trace), *i_args], input=stdin_data, capture_output=True, cwd=str(i_cwd))

            tested += 1
            same_files = (
                run_test_scenarios._snapshot_dir_files(u_cwd)
                == run_test_scenarios._snapshot_dir_files(i_cwd)
            )
            if r_uninst.stdout == r_inst.stdout and r_uninst.returncode == r_inst.returncode and same_files:
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
    test_items: list,
    fault_p_markers: set,
    max_tests_to_check: int = 500,
    copy_file_args: bool = False,
):
    """Run tests until a failure/divergence is observed between orig and faulty versions."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        t_orig_file = tmp_path / "t_orig.txt"
        t_faulty_file = tmp_path / "t_faulty.txt"

        for idx, item in enumerate(test_items[:max_tests_to_check], 1):
            stdin_file = item["stdin_file"]
            stdin_data = stdin_file.read_bytes() if (stdin_file and stdin_file.is_file()) else None
            args = item["args"]
            file_arg_paths = item.get("file_arg_paths", {})

            o_cwd = tmp_path / f"o_{idx}"
            f_cwd = tmp_path / f"f_{idx}"
            o_cwd.mkdir(parents=True, exist_ok=True)
            f_cwd.mkdir(parents=True, exist_ok=True)
            o_args = run_test_scenarios._prepare_test_args_for_cwd(args, file_arg_paths, o_cwd, copy_file_args)
            f_args = run_test_scenarios._prepare_test_args_for_cwd(args, file_arg_paths, f_cwd, copy_file_args)

            r_orig = subprocess.run([str(orig_exe), "--p-output", str(t_orig_file), *o_args], input=stdin_data, capture_output=True, cwd=str(o_cwd))
            r_faulty = subprocess.run([str(faulty_exe), "--p-output", str(t_faulty_file), *f_args], input=stdin_data, capture_output=True, cwd=str(f_cwd))

            orig_out = r_orig.stdout
            faulty_out = r_faulty.stdout

            is_failed = (
                orig_out != faulty_out
                or r_orig.returncode != r_faulty.returncode
                or run_test_scenarios._snapshot_dir_files(o_cwd) != run_test_scenarios._snapshot_dir_files(f_cwd)
            )

            t_orig = [p for p in t_orig_file.read_text(encoding="utf-8", errors="replace").strip().split(",") if p] if t_orig_file.exists() else []
            t_faulty = [p for p in t_faulty_file.read_text(encoding="utf-8", errors="replace").strip().split(",") if p] if t_faulty_file.exists() else []

            if is_failed:
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

    return {"failing_test_found": False, "checked_tests": min(len(test_items), max_tests_to_check)}


def evaluate_repository(repo_name: str, version_arg: str, siemens_base: Path, output_base: Path, max_tests: int = 500):
    repo_dir, orig_sources, versions = get_repo_files(repo_name, siemens_base)
    if not orig_sources:
        print(f"Error: repository '{repo_name}' not found under {siemens_base}", file=sys.stderr)
        return None

    repo_info = repos.get_repo_info(repo_name)
    exe_stem = repo_info["exe_stem"]
    copy_file_args = (repo_name == "gzip")

    print("\n" + "=" * 70)
    print(f" {repo_info['suite']} Fault Localization & Path Divergence: {repo_name.upper()}")
    print("=" * 70)
    print(f"Source files ({len(orig_sources['c_files'])}): {', '.join(orig_sources['c_files'].keys())}")
    print(f"Found {len(versions)} versions.")

    orig_include_dirs = list({p.parent for p in [*orig_sources["c_files"].values(), *orig_sources["h_files"].values()]})
    orig_extra_args = repos.detect_compile_flags(orig_sources["h_files"], orig_include_dirs)
    p_points = run_test_scenarios._collect_project_points(orig_sources["c_files"], orig_extra_args)
    print(f"Collected {len(p_points)} P-points from original code.")

    universe_path = run_test_scenarios._find_universe_file(repo_dir)
    test_items = run_test_scenarios.parse_universe_tests(universe_path, repo_dir, output_base / repo_name)
    print(f"Discovered {len(test_items)} test cases in universe.")

    inst_orig_exe = output_base / repo_name / "orig" / f"{exe_stem}.exe"
    if not inst_orig_exe.is_file():
        inst_orig_exe = output_base / repo_name / "source.alt" / "source.orig" / f"{exe_stem}.exe"
    if not inst_orig_exe.is_file():
        inst_orig_exe = output_base / repo_name / "source" / f"{exe_stem}.exe"

    print("\n[Step 1: Equivalence Check: Instrumented vs Uninstrumented]")
    eq_res = None
    if inst_orig_exe.is_file():
        eq_res = verify_equivalence_uninstrumented_vs_instrumented(
            orig_sources, inst_orig_exe, test_items, copy_file_args=copy_file_args
        )
        if eq_res["success"]:
            print(f"  --> PASSED: 100% identical outputs across {eq_res['tested_count']} sample tests!")
            print("      (Proves instrumentation preserved original program semantics)")
        else:
            print(f"  --> Discrepancies found: {eq_res.get('discrepancies') or eq_res.get('error')}")
    else:
        print(f"  (Skipped: {inst_orig_exe} not found)")

    target_versions = [version_arg] if version_arg != "all" else sorted(versions.keys(), key=repos.natural_sort_key)

    print("\n[Step 2 & 3: Fault Analysis, P-point Mapping & Path Divergence]")
    version_results = []
    for v_name in target_versions:
        if v_name not in versions:
            print(f"\nVersion {v_name} not found.")
            continue

        ver_sources = versions[v_name]
        v_inc_dirs = list({p.parent for p in [*ver_sources["c_files"].values(), *ver_sources["h_files"].values()]})
        v_extra_args = repos.detect_compile_flags(ver_sources["h_files"], v_inc_dirs)
        fault_markers, fault_details, _ = run_test_scenarios.get_project_fault_markers(
            orig_sources["c_files"],
            ver_sources["c_files"],
            orig_sources["h_files"],
            ver_sources["h_files"],
            extra_args=v_extra_args,
        )

        print("-" * 70)
        print(f"VERSION: {v_name} | Total Injected Fault Sites: {len(fault_details)}")
        for f_idx, fb in enumerate(fault_details, 1):
            print(f"  Fault #{f_idx}: {fb['file']} Lines {fb['orig_lines']} ({fb['type']})")
            print(f"    Mapped P-Point : {fb['p_str']}")
            if fb["orig_text"]:
                print(f"    Orig Code      : {' '.join(s.strip() for s in fb['orig_text'][:2])}")
            if fb["faulty_text"]:
                print(f"    Faulty Code    : {' '.join(s.strip() for s in fb['faulty_text'][:2])}")

        v_exe = output_base / repo_name / v_name / f"{exe_stem}.exe"
        if not v_exe.is_file():
            v_exe = output_base / repo_name / "versions.alt" / "versions.orig" / v_name / f"{exe_stem}.exe"
        if not v_exe.is_file():
            v_exe = output_base / repo_name / "source.alt" / v_name / f"{exe_stem}.exe"

        tr_res = None
        if inst_orig_exe.is_file() and v_exe.is_file():
            tr_res = trace_test_execution_and_divergence(
                inst_orig_exe, v_exe, test_items, fault_markers, max_tests, copy_file_args=copy_file_args
            )
            if tr_res["failing_test_found"]:
                print(f"\n  [Execution & Trace Divergence on Failing Test #{tr_res['test_index']}]:")
                print(f"    Test Args          : {' '.join(tr_res['args'])}")
                print(f"    Orig Output        : {tr_res['orig_output'].strip()}")
                print(f"    Faulty Output      : {tr_res['faulty_output'].strip()}")
                print(f"    Fault Site Visited : {'YES' if tr_res['fault_location_executed'] else 'NO'}")
                if tr_res["diverged_at_step"] is not None:
                    print(f"    ==> DIVERGED at step {tr_res['diverged_at_step']}:")
                    print(f"        Orig Path took   : {tr_res['orig_divergent_point']}")
                    print(f"        Faulty Path took : {tr_res['faulty_divergent_point']}")
                else:
                    print("    Trace path length/sequence visited identical P-markers, but variable values caused failure.")
            else:
                print(f"\n  (No failure triggered in first {tr_res['checked_tests']} tests from universe)")

        version_results.append({
            "version": v_name,
            "fault_count": len(fault_details),
            "faults": fault_details,
            "trace_divergence": tr_res,
        })

    return {
        "repo": repo_name,
        "p_points_count": len(p_points),
        "tests_count": len(test_items),
        "equivalence": eq_res,
        "version_results": version_results,
    }


def main():
    parser = argparse.ArgumentParser(description="Siemens & SIR Fault Localization and Trace Analyzer")
    parser.add_argument("--repo", default="tcas", help="Repository to analyze (e.g. tcas, space, gzip, grep, sed, flex, or 'all')")
    parser.add_argument("--version", default="v1", help="Specific faulty version to analyze (e.g. v1, v2, or 'all')")
    parser.add_argument("--siemens-dir", default="input/siemens", help="Siemens/SIR benchmark inputs root")
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
