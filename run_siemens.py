#!/usr/bin/env python3
"""
Instrument and compile every C file of the Siemens suite.

Stage 1: run the instrumenter executable on every source.
Stage 2: compile every instrumented source; the executable is placed
         next to it (tcas_instrumented.c -> tcas_instrumented[.exe]).

The output mirrors the Siemens directory layout:

    input/siemens/tcas/versions.alt/versions.orig/v1/tcas.c
 -> output/siemens_instrumented/tcas/versions.alt/versions.orig/v1/tcas_instrumented.c

Header files (*.h) next to each source are copied too, so the
instrumented sources can be compiled in place.

Usage:
    python run_siemens.py                      # run everything
    python run_siemens.py --dry-run            # only print the commands
    python run_siemens.py --commands-file run_all.sh
    python run_siemens.py --exe dist/instrumenter.exe --only tcas schedule
    python run_siemens.py --skip-instrument    # only compile (stage 2)
    python run_siemens.py --skip-compile       # only instrument (stage 1)
    python run_siemens.py --cc clang
"""

from pathlib import Path
import argparse
import os
import shlex
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parent

# Directories that contain test data, not program sources.
SKIP_DIRS = {"inputs", "outputs", "newoutputs", "traces", "testplans",
             "testplans.alt", "scripts", "info"}

# Extra Clang flags needed by some programs.
# replace.c defines its own getline(), which clashes with the POSIX
# one in modern <stdio.h>; strict C89 hides the POSIX declaration.
PROGRAM_FLAGS = {
    "replace": ["-std=c89"],
}

# The Siemens programs are K&R C: implicit int, implicit function
# declarations, ... GNU C89 accepts them on every gcc/clang version.
DEFAULT_COMPILE_FLAGS = ["-std=gnu89"]

EXE_SUFFIX = ".exe" if os.name == "nt" else ""


def find_exe(explicit):

    if explicit:
        return Path(explicit).expanduser().resolve()

    for candidate in (
        ROOT / "dist" / "instrumenter.exe",
        ROOT / "dist" / "instrumenter",
        ROOT / "linux_exe" / "instrumenter",
    ):
        if candidate.is_file():
            return candidate

    return None


def find_cc(explicit):

    if explicit:
        return explicit

    for name in ("gcc", "clang", "cc"):
        if shutil.which(name):
            return name

    return None


def collect_sources(siemens_dir, only):

    sources = []

    for path in sorted(siemens_dir.rglob("*.c")):

        rel = path.relative_to(siemens_dir)

        if only and rel.parts[0] not in only:
            continue

        if SKIP_DIRS.intersection(rel.parts[:-1]):
            continue

        sources.append(path)

    return sources


def quote(parts):

    if os.name == "nt":
        return subprocess.list2cmdline(parts)

    return " ".join(shlex.quote(p) for p in parts)


def main():

    parser = argparse.ArgumentParser(
        description="Instrument all Siemens C sources"
    )

    parser.add_argument(
        "--exe",
        help="Instrumenter executable (default: dist/instrumenter[.exe])"
    )

    parser.add_argument(
        "--input",
        default=str(ROOT / "input" / "siemens"),
        help="Siemens suite root"
    )

    parser.add_argument(
        "--output",
        default=str(ROOT / "output" / "siemens_instrumented"),
        help="Output root"
    )

    parser.add_argument(
        "--only",
        nargs="+",
        help="Only these programs (e.g. tcas schedule)"
    )

    parser.add_argument(
        "--cc",
        help="C compiler for stage 2 (default: gcc, then clang)"
    )

    parser.add_argument(
        "--skip-instrument",
        action="store_true",
        help="Skip stage 1, only compile existing instrumented sources"
    )

    parser.add_argument(
        "--skip-compile",
        action="store_true",
        help="Skip stage 2, only instrument"
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the commands without running them"
    )

    parser.add_argument(
        "--commands-file",
        help="Write all commands to this .sh/.bat file instead of running"
    )

    args, extra_args = parser.parse_known_args()

    # Anything after -- is forwarded to the instrumenter (and Clang).
    if extra_args and extra_args[0] == "--":
        extra_args = extra_args[1:]

    siemens_dir = Path(args.input).expanduser().resolve()
    output_root = Path(args.output).expanduser().resolve()

    if not siemens_dir.is_dir():
        print(f"Error: Siemens dir not found: {siemens_dir}", file=sys.stderr)
        return 1

    exe = find_exe(args.exe)

    if not args.skip_instrument and (exe is None or not exe.is_file()):
        print("Error: instrumenter executable not found "
              "(build it with `python build.py` or pass --exe)",
              file=sys.stderr)
        return 1

    cc = find_cc(args.cc)

    if not args.skip_compile and cc is None:
        print("Error: no C compiler found (install gcc/clang or pass --cc)",
              file=sys.stderr)
        return 1

    sources = collect_sources(siemens_dir, args.only)

    if not sources:
        print("No C sources found.", file=sys.stderr)
        return 1

    jobs = []

    for source in sources:

        out_dir = output_root / source.parent.relative_to(siemens_dir)

        program = source.relative_to(siemens_dir).parts[0]
        clang_args = PROGRAM_FLAGS.get(program, []) + extra_args

        commands = []
        instrumented = out_dir / f"{source.stem}_instrumented.c"

        if not args.skip_instrument:

            command = [
                str(exe),
                str(source),
                "-d", str(out_dir),
            ]

            if clang_args:
                command += ["--", *clang_args]

            commands.append(command)

        if not args.skip_compile:

            binary = out_dir / f"{source.stem}_instrumented{EXE_SUFFIX}"

            commands.append([
                cc,
                *PROGRAM_FLAGS.get(program, DEFAULT_COMPILE_FLAGS),
                str(instrumented),
                "-o", str(binary),
                "-lm",
            ])

        jobs.append((source, out_dir, commands, instrumented))

    # --------------------------------------------------------
    # Only generate the command list
    # --------------------------------------------------------

    if args.dry_run or args.commands_file:

        lines = [
            "\n".join(quote(command) for command in commands)
            for _, _, commands, _ in jobs
        ]

        if args.commands_file:

            target = Path(args.commands_file)
            is_bat = target.suffix.lower() in (".bat", ".cmd")

            header = ["@echo off"] if is_bat else ["#!/usr/bin/env bash", "set -u"]
            body = []

            for (_, out_dir, _, _), line in zip(jobs, lines):
                if is_bat:
                    body.append(f'if not exist "{out_dir}" mkdir "{out_dir}"')
                else:
                    body.append(f"mkdir -p {shlex.quote(str(out_dir))}")
                body.append(line)

            target.write_text("\n".join(header + body) + "\n")

            if not is_bat:
                target.chmod(0o755)

            print(f"Wrote {len(jobs)} commands to {target}")

        else:
            print("\n".join(lines))

        return 0

    # --------------------------------------------------------
    # Run: stage 1 (instrument) for all, then stage 2 (compile)
    # --------------------------------------------------------

    stages = []

    if not args.skip_instrument:
        stages.append(("Instrument", "instrumenter"))

    if not args.skip_compile:
        stages.append(("Compile", "compile"))

    failed = {name: [] for name, _ in stages}

    for stage_index, (name, log_name) in enumerate(stages):

        print(f"===== Stage {stage_index + 1}: {name} =====")

        for index, (source, out_dir, commands, instrumented) in enumerate(
            jobs, 1
        ):

            rel = source.relative_to(siemens_dir)
            command = commands[stage_index]

            print(f"[{index}/{len(jobs)}] {rel}", flush=True)

            if name == "Instrument":

                out_dir.mkdir(parents=True, exist_ok=True)

                for header in source.parent.glob("*.h"):
                    shutil.copy2(header, out_dir / header.name)

            elif rel in failed.get("Instrument", []):
                print("    skipped (instrumentation failed)")
                failed[name].append(rel)
                continue

            elif not instrumented.is_file():
                print(f"    FAILED (not found: {instrumented})")
                failed[name].append(rel)
                continue

            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            (out_dir / f"{source.stem}_{log_name}.log").write_text(
                result.stdout
            )

            if result.returncode != 0:
                print(f"    FAILED (exit {result.returncode})")
                failed[name].append(rel)

        print()

    for name, _ in stages:
        print(f"{name}: {len(jobs) - len(failed[name])} ok, "
              f"{len(failed[name])} failed")

        for rel in failed[name]:
            print(f"  failed: {rel}")

    print(f"Output: {output_root}")

    return 1 if any(failed.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
