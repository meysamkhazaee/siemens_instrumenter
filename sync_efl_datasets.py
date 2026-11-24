#!/usr/bin/env python3
"""
Synchronize all existing Siemens datasets into standard EFL structure.

For every dataset in output/datasets/<repo>/v<id>_detailed.json:
  1. Generates standard 3-field CSV: cmp.v0.v<id>.<repo>.csv
  2. Generates source_map.csv from AST points
  3. Generates source_changes.json from fault details & mutated lines
  4. Generates metadata.json (program, version, test_set)
  5. Generates cache_manifest.json (hashes & sizes)
  6. Mirrors everything into output/results/<repo>/v<id>/full/
"""

import csv
import hashlib
import json
from pathlib import Path
import re
import shutil
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
        print(f"[-] Datasets directory not found: {datasets_base}")
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

            # 1. Standard 3-field CSV: cmp.v0.v<id>.<repo>.csv
            cmp_csv_name = f"cmp.v0.{version_name}.{repo}.csv"
            cmp_csv_file = repo_dir / cmp_csv_name
            with open(cmp_csv_file, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                for t in tests:
                    t_id = t.get("test_id", 1)
                    verdict_str = "F" if t.get("verdict") == "FAIL" else "T"
                    trace = t.get("trace", "")
                    if not trace.strip():
                        continue
                    writer.writerow([f"T{t_id}", verdict_str, trace])

            # 2. source_map.csv
            source_map_file = repo_dir / "source_map.csv"
            with open(source_map_file, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["predicate", "id", "file", "line", "column", "function", "address"])
                for p in all_points:
                    writer.writerow([f"P{p.marker}", p.marker, orig_source.name, p.line, getattr(p, "col", 0), getattr(p, "func", "main"), "0x0"])

            # 3. source_changes.json
            source_changes_file = repo_dir / "source_changes.json"
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
                    "reference_text": [],
                    "faulty_text": [],
                    "header": False,
                })
            source_changes_file.write_text(json.dumps(changes_list, indent=2), encoding="utf-8")

            # 4. metadata.json
            metadata_file = repo_dir / "metadata.json"
            metadata_file.write_text(json.dumps({
                "program": repo,
                "version": v_num,
                "test_set": "full",
            }, indent=2), encoding="utf-8")

            # 5. cache_manifest.json
            manifest_file = repo_dir / "cache_manifest.json"
            outputs = []
            for p in [cmp_csv_file, source_map_file, source_changes_file, metadata_file]:
                h = hashlib.sha256(p.read_bytes()).hexdigest()
                outputs.append({"name": p.name, "size": p.stat().st_size, "sha256": h})
            manifest_file.write_text(json.dumps({
                "schema": 1,
                "fingerprint": f"siemens-{repo}-{version_name}",
                "options": {},
                "outputs": outputs,
            }, indent=2), encoding="utf-8")

            # 6. Mirror to results/<repo>/v<version>/full/
            results_ver_dir = results_base / repo / version_name / "full"
            results_ver_dir.mkdir(parents=True, exist_ok=True)

            dataset_file = repo_dir / f"{version_name}_dataset.txt"
            tagged_file = repo_dir / f"{version_name}_tagged.txt"

            for src in [cmp_csv_file, source_map_file, source_changes_file, metadata_file, manifest_file, detailed_file, dataset_file, tagged_file]:
                if src.exists():
                    shutil.copy2(src, results_ver_dir / src.name)

            synced_count += 1

    elapsed = round(time.time() - start_time, 2)
    print(f"\n[+] Successfully synced {synced_count} Siemens datasets in {elapsed}s!")
    print(f"    Datasets flat:    {datasets_base}")
    print(f"    Results standard: {results_base}")
    return 0


if __name__ == "__main__":
    sys.exit(sync_all())
