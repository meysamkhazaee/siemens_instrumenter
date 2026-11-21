#!/usr/bin/env python3
"""
Batch Instrumenter for the Siemens Benchmark Suite.

Mirrored layout:
  - output/siemens/<repo>/source/               : Error-free version (instrumented .c, compiled .exe, headers)
  - output/siemens/<repo>/source.alt/source.orig: Error-free version (instrumented .c, compiled .exe, headers)
  - output/siemens/<repo>/source.alt/v1..vN/    : Faulty versions (instrumented .c, compiled .exe, headers)
  - output/siemens/<repo>/versions.alt/versions.orig/v1..vN/: Faulty versions (instrumented .c, compiled .exe, headers)
  - Companion directories: inputs, scripts, testplans.alt, info, outputs, newoutputs, traces
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

import instrumenter


SIEMENS_REPOS = [
    {
        "id": "printtokens",
        "top_dir": "printtokens_2.0",
        "canon": "printtokens",
        "source_name": "print_tokens.c",
    },
    {
        "id": "printtokens2",
        "top_dir": "printtokens2_2.0",
        "canon": "printtokens2",
        "source_name": "print_tokens2.c",
    },
    {
        "id": "replace",
        "top_dir": "replace_2.1.tar_2",
        "canon": "replace",
        "source_name": "replace.c",
    },
    {
        "id": "schedule",
        "top_dir": "schedule_2.0",
        "canon": "schedule",
        "source_name": "schedule.c",
    },
    {
        "id": "schedule2",
        "top_dir": "schedule2_2.0",
        "canon": "schedule2",
        "source_name": "schedule2.c",
    },
    {
        "id": "tcas",
        "top_dir": "tcas_2.0",
        "canon": "tcas",
        "source_name": "tcas.c",
    },
    {
        "id": "totinfo",
        "top_dir": "totinfo_2.0",
        "canon": "totinfo",
        "source_name": "tot_info.c",
    },
]


def natural_sort_key(s):
    """Sort v1, v2, ... v10 in natural numerical order."""
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]


def mirror_repository_structure(repo_id: str, siemens_base: Path, output_base: Path):
    """Mirror all auxiliary directories, test assets and empty directories to output."""
    in_repo = siemens_base / repo_id
    out_repo = output_base / repo_id
    out_repo.mkdir(parents=True, exist_ok=True)

    # Clean up old flat folders (orig, v1..vN at root) if they existed
    for item in list(out_repo.iterdir()):
        if item.is_dir() and (item.name == "orig" or re.match(r"^v\d+$", item.name)):
            shutil.rmtree(item, ignore_errors=True)

    # Copy auxiliary directories
    aux_dirs = ["info", "inputs", "scripts", "testplans", "testplans.alt"]
    for d_name in aux_dirs:
        src_d = in_repo / d_name
        dst_d = out_repo / d_name
        if src_d.is_dir() and not dst_d.exists():
            shutil.copytree(src_d, dst_d)

    # Ensure standard directories exist
    for d_name in ["source", "source.alt", "versions", "versions.alt", "outputs", "newoutputs", "traces"]:
        (out_repo / d_name).mkdir(parents=True, exist_ok=True)

    # Copy top-level files (e.g. README)
    for f in in_repo.iterdir():
        if f.is_file():
            shutil.copy2(f, out_repo / f.name)


def discover_targets(siemens_base: Path, repo_filter=None):
    """Scan all Siemens suite repositories and return targets."""
    targets = []
    for repo_info in SIEMENS_REPOS:
        repo_id = repo_info["id"]
        if repo_filter and repo_id not in repo_filter:
            continue

        base_path = siemens_base / repo_info["canon"]
        if not base_path.exists():
            base_path = siemens_base / repo_info["top_dir"] / repo_info["canon"]
        if not base_path.exists():
            continue

        source_name = repo_info["source_name"]

        # 1. Base / original (healthy) version
        # Try source/ first, then source.alt/source.orig/
        orig_dir = base_path / "source"
        if not (orig_dir / source_name).is_file():
            orig_dir = base_path / "source.alt" / "source.orig"
        orig_file = orig_dir / source_name

        if orig_file.is_file():
            # Destination directories: both output/<repo>/source and output/<repo>/source.alt/source.orig
            dest_dirs = [
                Path("source"),
                Path("source.alt") / "source.orig",
            ]
            targets.append({
                "repo": repo_id,
                "version": "orig",
                "source_path": orig_file,
                "source_dir": orig_dir,
                "source_name": source_name,
                "rel_dest_dirs": dest_dirs,
            })

        # 2. Buggy versions (v1, v2, ...)
        versions_base = base_path / "versions.alt" / "versions.orig"
        if not versions_base.is_dir():
            versions_base = base_path / "source.alt"
        if versions_base.is_dir():
            v_dirs = [d for d in os.listdir(versions_base) if (versions_base / d).is_dir() and re.match(r"^v\d+$", d)]
            v_dirs.sort(key=natural_sort_key)
            for v_name in v_dirs:
                v_dir = versions_base / v_name
                v_file = v_dir / source_name
                if v_file.is_file():
                    # Destination directories: both source.alt/<vName> and versions.alt/versions.orig/<vName>
                    dest_dirs = [
                        Path("source.alt") / v_name,
                        Path("versions.alt") / "versions.orig" / v_name,
                    ]
                    targets.append({
                        "repo": repo_id,
                        "version": v_name,
                        "source_path": v_file,
                        "source_dir": v_dir,
                        "source_name": source_name,
                        "rel_dest_dirs": dest_dirs,
                    })

    return targets


def instrument_single_target(target, output_base: Path, compiler: str, compile_exe: bool = True):
    """Instrument a single target C file and deploy to destination paths."""
    repo = target["repo"]
    version = target["version"]
    source_path = target["source_path"]
    source_dir = target["source_dir"]
    rel_dest_dirs = target["rel_dest_dirs"]

    start_time = time.time()
    result = {
        "repo": repo,
        "version": version,
        "source": str(source_path),
        "output_destinations": [str(output_base / repo / d) for d in rel_dest_dirs],
        "success": False,
        "coverage_points": 0,
        "compile_success": None,
        "error": None,
        "duration_sec": 0.0,
    }

    try:
        # Extra include flags
        extra_args = [f"-I{source_dir}"]
        orig_include = source_path.parent.parent.parent / "source.alt" / "source.orig"
        if orig_include.exists() and orig_include != source_dir:
            extra_args.append(f"-I{orig_include}")

        # Run Clang AST
        ast, parser_mode, legacy_c = instrumenter.run_clang_ast(
            source_path,
            "c",
            compiler,
            extra_args
        )

        source_data = source_path.read_bytes()

        # Collect points
        points = instrumenter.collect_points(
            ast,
            source_path,
            source_data,
            legacy_c
        )
        points = instrumenter.remove_physical_duplicates(points)

        for idx, point in enumerate(points, 1):
            point.marker = idx

        # Instrument source
        instrumented_bytes = instrumenter.instrument_source(
            source_data,
            points
        )

        runtime = instrumenter.make_c_runtime()
        final_content = runtime.encode("utf-8") + instrumented_bytes

        # Header files to copy
        headers = list(source_dir.glob("*.h"))
        if orig_include.exists() and orig_include != source_dir:
            for h in orig_include.glob("*.h"):
                if not any(x.name == h.name for x in headers):
                    headers.append(h)

        compile_ok = True
        compile_err = None

        # Write to all specified destinations
        for rel_d in rel_dest_dirs:
            dest_dir = output_base / repo / rel_d
            dest_dir.mkdir(parents=True, exist_ok=True)

            dest_c_file = dest_dir / source_path.name

            dest_c_file.write_bytes(final_content)

            for h in headers:
                (dest_dir / h.name).write_bytes(h.read_bytes())

            if compile_exe:
                exe_file = dest_dir / f"{source_path.stem}.exe"
                cmd = ["gcc", "-std=gnu89", "-w", str(dest_c_file), "-o", str(exe_file), "-I", str(dest_dir), "-lm"]
                comp_res = subprocess.run(cmd, capture_output=True, text=True)
                if comp_res.returncode != 0:
                    compile_ok = False
                    compile_err = comp_res.stderr

        result["success"] = True
        result["coverage_points"] = len(points)
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
        description="Batch Instrumenter for Siemens Suite C Programs (source & source.alt layout)"
    )
    parser.add_argument(
        "-i", "--input-dir",
        default="input/siemens",
        help="Siemens root directory (default: input/siemens)"
    )
    parser.add_argument(
        "-o", "--output-dir",
        default="output/siemens",
        help="Output directory (default: output/siemens)"
    )
    parser.add_argument(
        "--repos",
        nargs="+",
        help="Filter specific repos (e.g. printtokens replace totinfo)"
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

    args = parser.parse_args()

    input_dir = Path(args.input_dir).resolve()
    output_dir = Path(args.output_dir).resolve()

    if not input_dir.is_dir():
        print(f"Error: input directory not found: {input_dir}", file=sys.stderr)
        return 1

    compiler = instrumenter.find_compiler("c", args.compiler)
    print("=" * 60)
    print(" Siemens Suite Batch Instrumenter")
    print("=" * 60)
    print(f"Input dir       : {input_dir}")
    print(f"Output dir      : {output_dir}")
    print(f"Clang compiler  : {compiler}")
    print(f"Workers         : {args.workers}")
    print(f"Compile to exe  : {args.compile}")
    print("=" * 60)

    # 1. Mirror auxiliary structure
    target_repos = args.repos if args.repos else [r["id"] for r in SIEMENS_REPOS]
    print("[1/2] Mirroring Siemens suite structure and test assets...")
    for repo_id in target_repos:
        if (input_dir / repo_id).is_dir():
            mirror_repository_structure(repo_id, input_dir, output_dir)
    print("      --> Auxiliary folders & structure mirrored successfully.")

    # 2. Discover and instrument
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

    # Write summary JSON
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
