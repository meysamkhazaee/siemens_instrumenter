#!/usr/bin/env python3
"""
Convert any existing legacy Siemens v*_detailed.json datasets into compact EFL v2 structure.

For every legacy dataset in output/datasets/<repo>/v<id>_detailed.json, writes
into output/results/<repo>/v<id>/full/:
  1. traces.tsv.gz (gzip-compressed TSV: test_id, status, cc, path)
  2. tests.tsv (test_id -> command/args mapping and execution metadata)
  3. summary.json (compact dataset summary without trace sequences)
  4. source_map.csv (AST predicate mapping)
  5. source_changes.json (fault lines metadata)
  6. metadata.json
  7. cache_manifest.json
"""

import csv
import gzip
import hashlib
import json
from pathlib import Path
import re
import sys
import time

import instrumenter
import run_test_scenarios


def natural_sort_key(s):
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]


def get_points_cache(input_base: Path):
    """Pre-compute P-points for all Siemens repos to make syncing instant."""
    cache = {}
    for repo in run_test_scenarios.SIEMENS_REPOS:
        stem = run_test_scenarios.get_source_stem(repo)
        in_repo = input_base / repo
        orig_source = in_repo / "source" / f"{stem}.c"
        if not orig_source.is_file():
            orig_source = in_repo / "source.alt" / "source.orig" / f"{stem}.c"
        if not orig_source.is_file():
            continue

        compiler = instrumenter.find_compiler("c")
        include_arg = [f"-I{orig_source.parent}"]
        ast, _, legacy = instrumenter.run_clang_ast(orig_source, "c", compiler, include_arg)
        source_data = orig_source.read_bytes()
        points = instrumenter.collect_points(ast, orig_source, source_data, legacy)
        points = instrumenter.remove_physical_duplicates(points)
        for idx, p in enumerate(points, 1):
            p.marker = idx
        cache[repo] = (orig_source, points)
    return cache


def sync_all():
    base_dir = Path(__file__).resolve().parent
    datasets_base = base_dir / "output" / "datasets"
    results_base = base_dir / "output" / "results"
    input_base = base_dir / "input" / "siemens"

    if not datasets_base.is_dir():
        print(f"[-] Legacy datasets directory not found: {datasets_base}")
        return 1

    print("[*] Pre-computing AST P-points for all Siemens programs...")
    points_cache = get_points_cache(input_base)
    print(f"[+] Loaded points for {len(points_cache)} repositories.")

    synced_count = 0
    start_time = time.time()

    for repo_dir in sorted(datasets_base.iterdir()):
        if not repo_dir.is_dir():
            continue
        repo = repo_dir.name
        if repo not in points_cache:
            continue

        orig_source, all_points = points_cache[repo]
        detailed_files = sorted(repo_dir.glob("v*_detailed.json"), key=lambda p: natural_sort_key(p.name))

        for detailed_file in detailed_files:
            v_match = re.match(r"(v\d+)_detailed\.json", detailed_file.name)
            if not v_match:
                continue
            version_name = v_match.group(1)
            v_num = int(version_name.lstrip("v"))

            try:
                data = json.loads(detailed_file.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"[-] Error reading {detailed_file}: {exc}")
                continue

            tests = data.get("tests", [])
            fault_details = data.get("fault_details", [])
            results_ver_dir = results_base / repo / version_name / "full"
            results_ver_dir.mkdir(parents=True, exist_ok=True)

            # 1. traces.tsv.gz
            traces_file = results_ver_dir / "traces.tsv.gz"
            included_count = 0
            excluded_count = 0
            trace_lengths = []
            n_pass = 0
            n_cc = 0
            n_fail = 0
            with gzip.open(traces_file, "wt", encoding="utf-8", newline="\n", compresslevel=6) as gz_out:
                gz_out.write("test_id\tstatus\tcc\tpath\n")
                for t in tests:
                    t_id = t.get("test_id", 1)
                    verdict = t.get("verdict", "PASS")
                    verdict_str = "F" if verdict == "FAIL" else "T"
                    cc_flag = 1 if verdict == "COINCIDENTAL_CORRECT" else 0
                    if verdict_str == "F":
                        n_fail += 1
                    elif cc_flag:
                        n_cc += 1
                    else:
                        n_pass += 1
                    trace_ids = run_test_scenarios._parse_trace_ids(t.get("trace", ""))
                    if not trace_ids:
                        excluded_count += 1
                        continue
                    included_count += 1
                    trace_lengths.append(len(trace_ids))
                    gz_out.write(f"T{t_id}\t{verdict_str}\t{cc_flag}\t{' '.join(trace_ids)}\n")

            # 2. tests.tsv
            tests_tsv_file = results_ver_dir / "tests.tsv"
            with open(tests_tsv_file, "w", encoding="utf-8", newline="\n") as f:
                f.write("test_id\ttest_name\tstatus\tcc\tincluded\ttrace_len\tduration_sec\treturn_code\ttimed_out\treason\n")
                for t in tests:
                    t_id = t.get("test_id", 1)
                    verdict = t.get("verdict", "PASS")
                    verdict_str = "F" if verdict == "FAIL" else "T"
                    cc_flag = 1 if verdict == "COINCIDENTAL_CORRECT" else 0
                    trace_ids = run_test_scenarios._parse_trace_ids(t.get("trace", ""))
                    included = 1 if trace_ids else 0
                    reason = "" if included else "empty_trace"
                    args_str = " ".join(str(a) for a in t.get("args", [])).replace("\t", " ")
                    f.write(f"T{t_id}\t{args_str}\t{verdict_str}\t{cc_flag}\t{included}\t{len(trace_ids)}\t0.0\t0\t0\t{reason}\n")

            # 3. source_map.csv
            source_map_file = results_ver_dir / "source_map.csv"
            with open(source_map_file, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["predicate", "id", "file", "line", "column", "function", "kind", "address"])
                for p in all_points:
                    writer.writerow([f"P{p.marker}", p.marker, orig_source.name, p.line, getattr(p, "col", 0), getattr(p, "func", "main"), getattr(p, "kind", ""), "0x0"])

            # 4. source_changes.json
            source_changes_file = results_ver_dir / "source_changes.json"
            changes_list = []
            for fd in fault_details:
                orig_lines_str = str(fd.get("orig_lines", "1"))
                if "-" in orig_lines_str:
                    parts = orig_lines_str.split("-")
                    f_lines = list(range(int(parts[0]), int(parts[1]) + 1))
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
            metadata_file = results_ver_dir / "metadata.json"
            metadata_file.write_text(json.dumps({
                "benchmark": "Siemens",
                "program": repo,
                "version": v_num,
                "test_set": "full",
                "source_file": orig_source.name,
            }, indent=2), encoding="utf-8")

            # 6. summary.json
            summary_file = results_ver_dir / "summary.json"
            fault_markers = data.get("fault_markers", [])
            summary_file.write_text(json.dumps({
                "schema": "efl-dataset-v2",
                "benchmark": "Siemens",
                "program": repo,
                "version": version_name,
                "test_set": "full",
                "tests_discovered": len(tests),
                "tests_included": included_count,
                "tests_excluded": excluded_count,
                "total_trace_events": sum(trace_lengths),
                "max_trace_events": max(trace_lengths, default=0),
                "min_trace_events": min(trace_lengths, default=0),
                "fault_predicates": fault_markers,
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
                "fingerprint": f"siemens-{repo}-{version_name}",
                "options": {"format": "efl-dataset-v2"},
                "outputs": outputs,
            }, indent=2), encoding="utf-8")

            synced_count += 1

    elapsed = round(time.time() - start_time, 2)
    print(f"\n[+] Successfully converted {synced_count} Siemens datasets to EFL v2 in {elapsed}s!")
    print(f"    Results standard: {results_base}")
    return 0


if __name__ == "__main__":
    sys.exit(sync_all())
