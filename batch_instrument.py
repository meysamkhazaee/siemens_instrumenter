#!/usr/bin/env python3
"""
Batch Instrumenter for Siemens & SIR C Benchmark Suites (single-file and multi-file).

Compact single-copy layout:
  - output/siemens/<repo>/orig/   : Error-free instrumented .c/.h, compiled <stem>.exe
  - output/siemens/<repo>/v1..vN/ : Faulty instrumented .c/.h, compiled <stem>.exe
Test inputs and universe testplans are read directly from input/siemens/<repo>/
without duplicating them into output/siemens/.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import subprocess
import sys
import time

import instrumenter
import repos


# Re-export registry symbols for backwards compatibility
SIEMENS_REPOS = [dict(entry) for entry in repos.BENCHMARK_REPOS]
natural_sort_key = repos.natural_sort_key


def prepare_repository_output(repo_id: str, output_base: Path):
    """Ensure compact output root exists without duplicating raw input assets."""
    out_repo = output_base / repo_id
    out_repo.mkdir(parents=True, exist_ok=True)


def discover_targets(siemens_base: Path, repo_filter=None):
    """Scan all configured Siemens and SIR repositories and return instrumentation targets."""
    targets = []
    for repo_info in repos.BENCHMARK_REPOS:
        repo_id = repo_info["id"]
        if repo_filter and repo_id not in repo_filter:
            continue

        base_path = repos.find_repo_base(siemens_base, repo_id)
        if not base_path:
            continue

        orig_dir = repos.find_orig_dir(base_path, repo_info)
        if not orig_dir:
            continue

        # 1. Base / original (healthy) version
        orig_sources = repos.resolve_version_sources(orig_dir, None, repo_info)
        if orig_sources["c_files"]:
            targets.append({
                "repo": repo_id,
                "suite": repo_info["suite"],
                "version": "orig",
                "exe_stem": repo_info["exe_stem"],
                "c_files": orig_sources["c_files"],
                "h_files": orig_sources["h_files"],
                "orig_dir": orig_dir,
                "source_dir": orig_dir,
                "rel_dest_dirs": [Path("orig")],
            })

        # 2. Buggy versions (v1, v2, ...)
        for v_name, v_dir in repos.find_version_dirs(base_path):
            ver_sources = repos.resolve_version_sources(orig_dir, v_dir, repo_info)
            if ver_sources["c_files"]:
                targets.append({
                    "repo": repo_id,
                    "suite": repo_info["suite"],
                    "version": v_name,
                    "exe_stem": repo_info["exe_stem"],
                    "c_files": ver_sources["c_files"],
                    "h_files": ver_sources["h_files"],
                    "orig_dir": orig_dir,
                    "source_dir": v_dir,
                    "rel_dest_dirs": [Path(v_name)],
                })

    return targets


def instrument_single_target(target, output_base: Path, compiler: str, compile_exe: bool = True):
    """Instrument a single-file or multi-file C target and deploy to destination paths."""
    repo = target["repo"]
    version = target["version"]
    exe_stem = target.get("exe_stem", repos.get_source_stem(repo))
    c_files: dict[str, Path] = target["c_files"]
    h_files: dict[str, Path] = target["h_files"]
    orig_dir: Path = target["orig_dir"]
    source_dir: Path = target["source_dir"]
    rel_dest_dirs = target["rel_dest_dirs"]

    start_time = time.time()
    primary_source = next(iter(c_files.values())) if c_files else source_dir
    result = {
        "repo": repo,
        "suite": target.get("suite", repos.get_benchmark_suite(repo)),
        "version": version,
        "source": str(primary_source),
        "source_files": list(c_files.keys()),
        "output_destinations": [str(output_base / repo / d) for d in rel_dest_dirs],
        "success": False,
        "coverage_points": 0,
        "compile_success": None,
        "error": None,
        "duration_sec": 0.0,
    }

    try:
        include_dirs = [source_dir]
        if orig_dir != source_dir:
            include_dirs.append(orig_dir)
        # Include subdirectories containing headers if multi-file
        for h_path in h_files.values():
            if h_path.parent not in include_dirs:
                include_dirs.append(h_path.parent)

        extra_args = repos.detect_compile_flags(h_files, include_dirs)
        is_multi_file = len(c_files) > 1

        instrumented_files: dict[str, bytes] = {}
        total_points = 0
        next_marker = 1
        parser_mode = None
        main_found_anywhere = False

        for rel_c, src_path in sorted(c_files.items()):
            ast, file_parser_mode, legacy_c = instrumenter.run_clang_ast(
                src_path,
                "c",
                compiler,
                extra_args,
            )
            if parser_mode is None:
                parser_mode = file_parser_mode

            source_data = src_path.read_bytes()
            points = instrumenter.collect_points(
                ast,
                src_path,
                source_data,
                legacy_c,
                require_main=False,
            )
            points = instrumenter.remove_physical_duplicates(points)

            if any(p.kind == "MainEntry" for p in points):
                main_found_anywhere = True

            instrumented_bytes = instrumenter.instrument_source(
                source_data,
                points,
                start_marker=next_marker,
            )
            next_marker += len(points)
            total_points += len(points)

            if is_multi_file:
                prefix = b'#include "efl_trace_runtime.h"\n'
            else:
                prefix = instrumenter.make_c_runtime().encode("utf-8")

            instrumented_files[rel_c] = prefix + instrumented_bytes

        if not main_found_anywhere:
            raise RuntimeError(
                "No main(argc, argv) function was found across target source files.\n"
                "This instrumenter requires main(argc, argv) because --p-output is initialized there."
            )

        compile_ok = True
        compile_err = None

        for rel_d in rel_dest_dirs:
            dest_dir = output_base / repo / rel_d
            dest_dir.mkdir(parents=True, exist_ok=True)

            compiled_inputs: list[str] = []
            for rel_c, content in instrumented_files.items():
                dest_c = dest_dir / rel_c
                dest_c.parent.mkdir(parents=True, exist_ok=True)
                dest_c.write_bytes(content)
                compiled_inputs.append(str(dest_c))

            for rel_h, h_path in h_files.items():
                dest_h = dest_dir / rel_h
                dest_h.parent.mkdir(parents=True, exist_ok=True)
                dest_h.write_bytes(h_path.read_bytes())

            if is_multi_file:
                (dest_dir / "efl_trace_runtime.h").write_text(
                    instrumenter.make_c_runtime_header(), encoding="utf-8"
                )
                runtime_c = dest_dir / "efl_trace_runtime.c"
                runtime_c.write_text(instrumenter.make_c_runtime_source(), encoding="utf-8")
                compiled_inputs.append(str(runtime_c))

            if compile_exe:
                exe_file = dest_dir / f"{exe_stem}.exe"
                dest_include_flags = ["-I", str(dest_dir)]
                seen_inc = {dest_dir}
                for rel_h in h_files:
                    inc_dir = (dest_dir / rel_h).parent
                    if inc_dir not in seen_inc:
                        seen_inc.add(inc_dir)
                        dest_include_flags.extend(["-I", str(inc_dir)])
                if any(Path(rel).name == "config.h" for rel in h_files):
                    dest_include_flags.append("-DHAVE_CONFIG_H")

                cmd = [repos.find_gcc(), "-std=gnu89", "-w", *compiled_inputs, "-o", str(exe_file), *dest_include_flags, "-lm"]
                comp_res = subprocess.run(cmd, capture_output=True, text=True)
                if comp_res.returncode != 0:
                    compile_ok = False
                    compile_err = comp_res.stderr

        result["success"] = True
        result["coverage_points"] = total_points
        result["parser_mode"] = parser_mode
        if compile_exe:
            result["compile_success"] = compile_ok
            if not compile_ok:
                result["compile_error"] = compile_err

    except Exception as exc:
        result["error"] = str(exc)

    result["duration_sec"] = round(time.time() - start_time, 3)
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Batch Instrumenter for Siemens & SIR C Programs (single-file and multi-file)"
    )
    parser.add_argument(
        "-i", "--input-dir",
        default="input/siemens",
        help="Siemens/SIR benchmark root directory (default: input/siemens)"
    )
    parser.add_argument(
        "-o", "--output-dir",
        default="output/siemens",
        help="Output directory (default: output/siemens)"
    )
    parser.add_argument(
        "--repos",
        nargs="+",
        help="Filter specific repos (e.g. tcas space gzip grep sed flex)"
    )
    parser.add_argument(
        "--compile",
        action="store_true",
        default=True,
        help="Also compile each instrumented version to an executable (.exe) (default: True)"
    )
    parser.add_argument(
        "-j", "--workers",
        type=int,
        default=4,
        help="Number of concurrent workers (default: 4)"
    )
    parser.add_argument(
        "--compiler",
        help="Custom compiler path (default: auto-detected Clang)"
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s (instrumenter {instrumenter.__version__})"
    )

    args = parser.parse_args()

    input_dir = Path(args.input_dir).resolve()
    output_dir = Path(args.output_dir).resolve()

    if not input_dir.is_dir():
        print(f"Error: input directory not found: {input_dir}", file=sys.stderr)
        return 1

    compiler = instrumenter.find_compiler("c", args.compiler)
    print("=" * 60)
    print(f" Siemens & SIR Batch Instrumenter (v{instrumenter.__version__})")
    print("=" * 60)
    print(f"Input dir       : {input_dir}")
    print(f"Output dir      : {output_dir}")
    print(f"Clang compiler  : {compiler}")
    print(f"Workers         : {args.workers}")
    print(f"Compile to exe  : {args.compile}")
    print("=" * 60)

    target_repos = args.repos if args.repos else repos.SIEMENS_REPOS
    print("[1/2] Preparing output directories...")
    for repo_id in target_repos:
        if repos.find_repo_base(input_dir, repo_id) is not None:
            prepare_repository_output(repo_id, output_dir)
    print("      --> Output structure ready (reading test assets directly from input dir).")

    print("[2/2] Discovering and instrumenting versions...")
    targets = discover_targets(input_dir, args.repos)
    print(f"Discovered {len(targets)} targets across repositories:")
    repo_counts = {}
    for t in targets:
        repo_counts[t["repo"]] = repo_counts.get(t["repo"], 0) + 1
    for r_id, count in sorted(repo_counts.items()):
        print(f"  - {r_id:<14}: {count} versions")
    print("-" * 60)

    start_all = time.time()
    results = []

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_map = {
            executor.submit(
                instrument_single_target, target, output_dir, compiler, args.compile
            ): target
            for target in targets
        }

        for future in as_completed(future_map):
            res = future.result()
            results.append(res)
            status_symbol = "[OK]" if res["success"] else "[FAILED]"
            comp_info = ""
            if args.compile:
                comp_info = " (compiled)" if res.get("compile_success") else " (compile failed)"
            print(
                f"{status_symbol} {res['repo']:<14} {res['version']:<6} "
                f"Points: {res['coverage_points']:<4} {comp_info} ({res['duration_sec']}s)"
            )

    total_time = round(time.time() - start_all, 2)
    success_count = sum(1 for r in results if r["success"])
    fail_count = len(results) - success_count

    print("=" * 60)
    print(" Instrumentation Summary")
    print("=" * 60)
    print(f"Total targets  : {len(results)}")
    print(f"Succeeded      : {success_count}")
    print(f"Failed         : {fail_count}")
    print(f"Total duration : {total_time}s")

    if args.compile:
        comp_ok = sum(1 for r in results if r.get("compile_success"))
        print(f"Compiled OK    : {comp_ok}/{len(results)}")

    summary_path = output_dir / "instrumentation_summary.json"
    summary_data = {
        "total": len(results),
        "succeeded": success_count,
        "failed": fail_count,
        "duration_sec": total_time,
        "results": sorted(results, key=lambda r: (r["repo"], natural_sort_key(r["version"]))),
    }
    summary_path.write_text(json.dumps(summary_data, indent=2), encoding="utf-8")
    print(f"Summary report written to: {summary_path}")

    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
