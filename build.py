#!/usr/bin/env python3

from pathlib import Path
import subprocess
import sys
import os


def main():
    root = Path(
        __file__
    ).resolve().parent

    source = (
        root
        / "instrumenter.py"
    )

    if not source.is_file():
        print(
            f"instrumenter.py not found: {source}"
        )
        return 1

    name = "instrumenter"

    command = [
        sys.executable,
        "-m",
        "PyInstaller",

        "--clean",
        "--noconfirm",
        "--onefile",

        "--name",
        name,
    ]

    version_file = root / "VERSION"
    if version_file.is_file():
        command.extend(["--add-data", f"{version_file}{os.pathsep}."])

    command.append(str(source))

    print(
        "Building instrumenter..."
    )

    print(
        " ".join(command)
    )

    result = subprocess.run(
        command,
        cwd=str(root),
    )

    if result.returncode != 0:
        return result.returncode

    if os.name == "nt":
        executable = (
            root
            / "dist"
            / "instrumenter.exe"
        )
    else:
        executable = (
            root
            / "dist"
            / "instrumenter"
        )

    print()
    print(
        "Build completed."
    )

    print(
        f"Executable: {executable}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )