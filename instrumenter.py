#!/usr/bin/env python3

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


# ============================================================
# Supported AST constructs
# ============================================================

LOOP_KINDS = {
    "ForStmt",
    "WhileStmt",
    "DoStmt",
    "CXXForRangeStmt",
}

FUNCTION_KINDS = {
    "FunctionDecl",
    "CXXMethodDecl",
    "CXXConstructorDecl",
    "CXXDestructorDecl",
}


# ============================================================
# Platform helpers
# ============================================================

def is_windows():
    return os.name == "nt"


def platform_name():
    if is_windows():
        return "Windows"

    if sys.platform.startswith("linux"):
        return "Linux"

    return sys.platform


# ============================================================
# Compiler discovery
# ============================================================

def resolve_custom_compiler(value):
    if not value:
        return None

    found = shutil.which(value)

    if found:
        return str(Path(found).resolve())

    path = Path(value).expanduser()

    if path.is_file():
        return str(path.resolve())

    raise RuntimeError(
        f"Compiler was not found: {value}"
    )


def find_compiler(
    language,
    custom_compiler=None
):
    if custom_compiler:
        return resolve_custom_compiler(
            custom_compiler
        )

    compiler_name = (
        "clang++"
        if language == "cpp"
        else "clang"
    )

    # --------------------------------------------------------
    # Search PATH
    # --------------------------------------------------------

    found = shutil.which(
        compiler_name
    )

    if found:
        return str(
            Path(found).resolve()
        )

    # --------------------------------------------------------
    # Windows fallback locations
    # --------------------------------------------------------

    if is_windows():

        exe = (
            "clang++.exe"
            if language == "cpp"
            else "clang.exe"
        )

        candidates = [
            Path(
                rf"C:\msys64\ucrt64\bin\{exe}"
            ),
            Path(
                rf"C:\msys64\mingw64\bin\{exe}"
            ),
            Path(
                rf"C:\msys64\clang64\bin\{exe}"
            ),
            Path(
                rf"C:\Program Files\LLVM\bin\{exe}"
            ),
        ]

    # --------------------------------------------------------
    # Linux fallback locations
    # --------------------------------------------------------

    else:

        candidates = [
            Path(
                f"/usr/bin/{compiler_name}"
            ),
            Path(
                f"/usr/local/bin/{compiler_name}"
            ),
            Path(
                f"/opt/llvm/bin/{compiler_name}"
            ),
        ]

    for candidate in candidates:

        if candidate.is_file():
            return str(
                candidate.resolve()
            )

    raise RuntimeError(
        f"Cannot find {compiler_name}.\n"
        f"Install Clang or use:\n"
        f"  --compiler <path>"
    )


# ============================================================
# Language detection
# ============================================================

def detect_language(path):
    extension = path.suffix.lower()

    if extension == ".c":
        return "c"

    if extension in {
        ".cpp",
        ".cc",
        ".cxx",
        ".c++",
    }:
        return "cpp"

    raise RuntimeError(
        f"Unsupported source extension: "
        f"{extension}"
    )


# ============================================================
# Run Clang AST
# ============================================================

def run_clang_ast(
    source,
    language,
    compiler,
    extra_args
):
    command = [
        compiler
    ]

    if language == "cpp":
        command.append(
            "-std=c++17"
        )
    else:
        command.append(
            "-std=c11"
        )

    command += [
        "-fsyntax-only",
        "-Xclang",
        "-ast-dump=json",
    ]

    command += extra_args

    command.append(
        str(source)
    )

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if result.returncode != 0:

        raise RuntimeError(
            "Clang parsing failed:\n\n"
            + result.stderr
        )

    try:

        return json.loads(
            result.stdout
        )

    except json.JSONDecodeError as error:

        raise RuntimeError(
            "Could not parse Clang AST JSON:\n"
            + str(error)
        )


# ============================================================
# AST location helpers
# ============================================================

def unwrap_location(location):
    if not isinstance(
        location,
        dict
    ):
        return {}

    expansion = location.get(
        "expansionLoc"
    )

    if isinstance(
        expansion,
        dict
    ):
        return expansion

    return location


def get_begin(node):
    location = (
        node
        .get("range", {})
        .get("begin", {})
    )

    location = unwrap_location(
        location
    )

    offset = location.get(
        "offset"
    )

    if isinstance(
        offset,
        int
    ):
        return offset

    return None


def get_end(node):
    location = (
        node
        .get("range", {})
        .get("end", {})
    )

    location = unwrap_location(
        location
    )

    offset = location.get(
        "offset"
    )

    token_length = location.get(
        "tokLen",
        1
    )

    if not isinstance(
        offset,
        int
    ):
        return None

    if not isinstance(
        token_length,
        int
    ):
        token_length = 1

    return (
        offset
        + token_length
    )


def normalized_path(path):
    try:

        return os.path.normcase(
            os.path.abspath(
                str(path)
            )
        )

    except Exception:

        return str(path)


def is_main_file_node(
    node,
    source
):
    location = (
        node
        .get("range", {})
        .get("begin", {})
    )

    location = unwrap_location(
        location
    )

    if not location:
        return False

    # Header-originated node
    if "includedFrom" in location:
        return False

    filename = location.get(
        "file"
    )

    # Clang commonly omits file for the primary
    # translation unit.
    if filename is None:
        return True

    return (
        normalized_path(filename)
        ==
        normalized_path(source)
    )


def children(node):
    return [
        child
        for child
        in node.get("inner", [])
        if isinstance(child, dict)
    ]


# ============================================================
# Source helpers
# ============================================================

def line_number(
    source_data,
    offset
):
    return (
        source_data[:offset]
        .count(b"\n")
        + 1
    )


def indentation_at(
    source_data,
    offset
):
    line_start = source_data.rfind(
        b"\n",
        0,
        offset
    )

    if line_start < 0:
        line_start = 0
    else:
        line_start += 1

    position = line_start

    while (
        position < len(source_data)
        and
        source_data[
            position:position + 1
        ]
        in (
            b" ",
            b"\t"
        )
    ):
        position += 1

    return source_data[
        line_start:position
    ].decode(
        "utf-8",
        errors="ignore"
    )


# ============================================================
# Instrumentation point
# ============================================================

class Point:

    def __init__(
        self,
        offset,
        kind,
        position,
        line,
        data=None
    ):
        self.offset = offset
        self.kind = kind
        self.position = position
        self.line = line

        self.data = (
            data
            if data is not None
            else {}
        )

        self.marker = None


# ============================================================
# Collect instrumentation points
# ============================================================

def collect_points(
    ast,
    source,
    source_data
):
    points = []
    seen = set()
    warnings = []

    # ========================================================
    # Add instrumentation point
    # ========================================================

    def add(
        offset,
        kind,
        position,
        data=None
    ):
        if offset is None:
            return

        if offset < 0:
            return

        if offset > len(
            source_data
        ):
            return

        key = (
            offset,
            kind,
            position
        )

        if key in seen:
            return

        seen.add(
            key
        )

        points.append(
            Point(
                offset=offset,
                kind=kind,
                position=position,
                line=line_number(
                    source_data,
                    min(
                        offset,
                        len(source_data)
                    )
                ),
                data=data,
            )
        )

    # ========================================================
    # Find compound opening {
    # ========================================================

    def body_brace(node):
        if node is None:
            return None

        if (
            node.get("kind")
            != "CompoundStmt"
        ):
            return None

        offset = get_begin(
            node
        )

        if offset is None:
            return None

        if offset >= len(
            source_data
        ):
            return None

        if (
            source_data[
                offset:offset + 1
            ]
            != b"{"
        ):
            return None

        return offset

    # ========================================================
    # Process one standalone statement
    # ========================================================

    def process_single_statement(
        statement,
        covered
    ):
        kind = statement.get(
            "kind"
        )

        if kind == "IfStmt":

            return process_if(
                statement,
                covered,
                has_next=False
            )

        if kind in LOOP_KINDS:

            return process_loop(
                statement,
                covered
            )

        if kind == "CompoundStmt":

            process_compound(
                statement,
                entry_probe=not covered,
                entry_kind="BlockEntry",
                initial_covered=covered
            )

            return True

        return covered

    # ========================================================
    # Process IF
    # ========================================================

    def process_if(
        statement,
        covered,
        has_next
    ):
        items = children(
            statement
        )

        has_else = statement.get(
            "hasElse",
            False
        )

        if has_else:

            if len(items) < 2:
                return covered

            then_statement = (
                items[-2]
            )

            else_statement = (
                items[-1]
            )

        else:

            if not items:
                return covered

            then_statement = (
                items[-1]
            )

            else_statement = None

        # ----------------------------------------------------
        # Add an IF block marker only when the current
        # straight-line path has not already been covered.
        # ----------------------------------------------------

        if not covered:

            add(
                get_begin(
                    statement
                ),
                "IfBlock",
                "before-statement"
            )

            covered = True

        # ----------------------------------------------------
        # THEN branch
        # ----------------------------------------------------

        if (
            then_statement.get(
                "kind"
            )
            == "CompoundStmt"
        ):

            process_compound(
                then_statement,
                entry_probe=True,
                entry_kind="IfThen",
                initial_covered=False
            )

        else:

            warnings.append(
                (
                    get_begin(
                        then_statement
                    ),
                    "unbraced-if-then"
                )
            )

        # ----------------------------------------------------
        # ELSE branch
        # ----------------------------------------------------

        if else_statement is not None:

            if (
                else_statement.get(
                    "kind"
                )
                == "CompoundStmt"
            ):

                process_compound(
                    else_statement,
                    entry_probe=True,
                    entry_kind="IfElse",
                    initial_covered=False
                )

            elif (
                else_statement.get(
                    "kind"
                )
                == "IfStmt"
            ):

                # else if (...)
                process_single_statement(
                    else_statement,
                    covered=False
                )

            else:

                warnings.append(
                    (
                        get_begin(
                            else_statement
                        ),
                        "unbraced-if-else"
                    )
                )

        # ----------------------------------------------------
        # Merge point
        #
        # Only necessary if execution continues after IF.
        # ----------------------------------------------------

        if has_next:

            add(
                get_end(
                    statement
                ),
                "IfMerge",
                "after-statement"
            )

            return True

        return False

    # ========================================================
    # Process LOOP
    # ========================================================

    def process_loop(
        statement,
        covered
    ):
        kind = statement.get(
            "kind"
        )

        items = children(
            statement
        )

        if not items:
            return covered

        # ----------------------------------------------------
        # Avoid duplicate loop-header marker if current block
        # is already represented.
        # ----------------------------------------------------

        if not covered:

            add(
                get_begin(
                    statement
                ),
                "LoopHeader",
                "before-statement"
            )

            covered = True

        # ----------------------------------------------------
        # Find body
        # ----------------------------------------------------

        if kind == "DoStmt":
            body = items[0]
        else:
            body = items[-1]

        # ----------------------------------------------------
        # Loop body marker
        #
        # Executes once per loop iteration.
        # ----------------------------------------------------

        if (
            body.get("kind")
            == "CompoundStmt"
        ):

            process_compound(
                body,
                entry_probe=True,
                entry_kind=(
                    kind
                    + "Body"
                ),
                initial_covered=False
            )

        else:

            warnings.append(
                (
                    get_begin(body),
                    "unbraced-loop"
                )
            )

        # ----------------------------------------------------
        # Loop exit marker
        # ----------------------------------------------------

        add(
            get_end(
                statement
            ),
            kind + "Exit",
            "after-statement"
        )

        return True

    # ========================================================
    # Process compound { ... }
    # ========================================================

    def process_compound(
        compound,
        entry_probe=False,
        entry_kind="BlockEntry",
        initial_covered=False
    ):
        items = children(
            compound
        )

        covered = initial_covered

        brace = body_brace(
            compound
        )

        # ----------------------------------------------------
        # Function / IF branch / loop body entry
        # ----------------------------------------------------

        if (
            entry_probe
            and brace is not None
        ):

            add(
                brace + 1,
                entry_kind,
                "after-brace"
            )

            covered = True

        # ----------------------------------------------------
        # Sequential statements
        # ----------------------------------------------------

        for index, statement in enumerate(
            items
        ):
            kind = statement.get(
                "kind"
            )

            has_next = (
                index + 1
                < len(items)
            )

            # =================================================
            # IF
            # =================================================

            if kind == "IfStmt":

                covered = process_if(
                    statement,
                    covered,
                    has_next
                )

            # =================================================
            # LOOP
            # =================================================

            elif kind in LOOP_KINDS:

                covered = process_loop(
                    statement,
                    covered
                )

            # =================================================
            # RETURN
            # =================================================

            elif kind == "ReturnStmt":

                # A return does not need another marker when
                # its current path already has a marker.
                if not covered:

                    add(
                        get_begin(
                            statement
                        ),
                        "ReturnBlock",
                        "before-statement"
                    )

                # No continuation after return.
                covered = False

            # =================================================
            # BREAK
            # =================================================

            elif kind == "BreakStmt":

                if not covered:

                    add(
                        get_begin(
                            statement
                        ),
                        "BreakBlock",
                        "before-statement"
                    )

                covered = False

            # =================================================
            # CONTINUE
            # =================================================

            elif kind == "ContinueStmt":

                if not covered:

                    add(
                        get_begin(
                            statement
                        ),
                        "ContinueBlock",
                        "before-statement"
                    )

                covered = False

            # =================================================
            # Nested compound
            # =================================================

            elif kind == "CompoundStmt":

                process_compound(
                    statement,
                    entry_probe=not covered,
                    entry_kind="BlockEntry",
                    initial_covered=covered
                )

                covered = True

            # =================================================
            # Normal straight-line statement
            # =================================================

            else:
                pass

        return covered

    # ========================================================
    # Walk AST and find functions
    # ========================================================

    def walk(node):
        if not isinstance(
            node,
            dict
        ):
            return

        kind = node.get(
            "kind"
        )

        if (
            kind != "TranslationUnitDecl"
            and
            not is_main_file_node(
                node,
                source
            )
        ):
            return

        # ----------------------------------------------------
        # Function
        # ----------------------------------------------------

        if kind in FUNCTION_KINDS:

            function_children = (
                children(node)
            )

            body = None

            for child in reversed(
                function_children
            ):

                if (
                    child.get("kind")
                    == "CompoundStmt"
                ):

                    body = child
                    break

            if body is None:
                return

            function_name = node.get(
                "name",
                ""
            )

            # =================================================
            # main()
            # =================================================

            if function_name == "main":

                parameters = [
                    child
                    for child
                    in function_children
                    if (
                        child.get("kind")
                        == "ParmVarDecl"
                    )
                ]

                # Runtime argument requires:
                #
                # int main(int argc, char **argv)
                #
                if len(parameters) < 2:

                    raise RuntimeError(
                        "main() must provide argc and argv.\n\n"
                        "Supported form:\n"
                        "  int main(int argc, char *argv[])\n\n"
                        "This is required because the trace output "
                        "path is supplied using --p-output."
                    )

                argc_name = (
                    parameters[0]
                    .get("name")
                )

                argv_name = (
                    parameters[1]
                    .get("name")
                )

                if (
                    not argc_name
                    or
                    not argv_name
                ):

                    raise RuntimeError(
                        "Could not determine argc/argv "
                        "parameter names in main()."
                    )

                brace = body_brace(
                    body
                )

                if brace is None:

                    raise RuntimeError(
                        "Could not locate main() body."
                    )

                # --------------------------------------------
                # This single point performs:
                #
                # __P_INIT(&argc, argv);
                # __P_TRACE(P1);
                #
                # --------------------------------------------

                add(
                    brace + 1,
                    "MainEntry",
                    "after-brace",
                    data={
                        "argc": argc_name,
                        "argv": argv_name,
                    }
                )

                # MainEntry already covers the initial block.
                # Do not create a duplicate before the first IF.
                process_compound(
                    body,
                    entry_probe=False,
                    initial_covered=True
                )

            # =================================================
            # Other functions
            # =================================================

            else:

                process_compound(
                    body,
                    entry_probe=True,
                    entry_kind="FunctionEntry",
                    initial_covered=False
                )

            # Prevent function body from being processed twice.
            return

        # ----------------------------------------------------
        # Recursive AST traversal
        # ----------------------------------------------------

        for child in children(
            node
        ):
            walk(child)

    walk(
        ast
    )

    # ========================================================
    # Source order
    # ========================================================

    points.sort(
        key=lambda point:
        point.offset
    )

    return (
        points,
        warnings
    )


# ============================================================
# Remove physical duplicate locations
# ============================================================

def remove_physical_duplicates(
    points
):
    result = []
    used_offsets = set()

    for point in points:

        if (
            point.offset
            in used_offsets
        ):
            continue

        used_offsets.add(
            point.offset
        )

        result.append(
            point
        )

    return result


# ============================================================
# C++ tracing runtime
# ============================================================

def make_cpp_runtime():
    return r'''/* ===== AUTO-GENERATED INSTRUMENTATION ===== */

#include <fstream>
#include <iostream>
#include <cstring>

static std::ofstream __P_TRACE_FILE;
static bool __P_TRACE_READY = false;


/*
 * Instrumentation runtime argument:
 *
 *     --p-output <file>
 *
 * Example:
 *
 *     program.exe --p-output result/test1.txt 1 2 3
 *
 * --p-output and its value are removed from argc/argv
 * before the original program begins execution.
 */
static bool __P_INIT(int *argc, char **argv)
{
    const char *output_path = nullptr;

    for (int i = 1; i < *argc; ++i)
    {
        if (std::strcmp(argv[i], "--p-output") == 0)
        {
            if (i + 1 >= *argc)
            {
                std::cerr
                    << "Error: --p-output requires a file path"
                    << std::endl;

                return false;
            }

            output_path = argv[i + 1];

            /*
             * Remove:
             *
             *     --p-output <path>
             *
             * from argv.
             */
            for (int j = i; j + 2 < *argc; ++j)
            {
                argv[j] = argv[j + 2];
            }

            *argc -= 2;

            /*
             * argv[argc] must remain NULL.
             */
            argv[*argc] = nullptr;

            break;
        }
    }

    if (output_path == nullptr)
    {
        std::cerr
            << "Error: missing instrumentation argument:"
            << std::endl
            << "  --p-output <trace-file>"
            << std::endl;

        return false;
    }

    __P_TRACE_FILE.open(
        output_path,
        std::ios::out | std::ios::trunc
    );

    if (!__P_TRACE_FILE.is_open())
    {
        std::cerr
            << "Error: cannot open trace file: "
            << output_path
            << std::endl;

        return false;
    }

    __P_TRACE_READY = true;

    return true;
}


static inline void __P_TRACE(int id)
{
    if (!__P_TRACE_READY)
    {
        return;
    }

    __P_TRACE_FILE
        << "P"
        << id
        << ",";

    __P_TRACE_FILE.flush();
}

/* ===== END INSTRUMENTATION HEADER ===== */

'''


# ============================================================
# C tracing runtime
# ============================================================

def make_c_runtime():
    return r'''/* ===== AUTO-GENERATED INSTRUMENTATION ===== */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static FILE *__P_TRACE_FILE = NULL;


static void __P_TRACE_CLOSE(void)
{
    if (__P_TRACE_FILE != NULL)
    {
        fclose(__P_TRACE_FILE);
        __P_TRACE_FILE = NULL;
    }
}


/*
 * Instrumentation runtime argument:
 *
 *     --p-output <file>
 *
 * --p-output and its value are removed from argc/argv
 * before the original program executes.
 */
static int __P_INIT(int *argc, char **argv)
{
    const char *output_path = NULL;

    int i;
    int j;

    for (i = 1; i < *argc; ++i)
    {
        if (strcmp(argv[i], "--p-output") == 0)
        {
            if (i + 1 >= *argc)
            {
                fprintf(
                    stderr,
                    "Error: --p-output requires a file path\n"
                );

                return 0;
            }

            output_path = argv[i + 1];

            /*
             * Remove:
             *
             *     --p-output <path>
             */
            for (j = i; j + 2 < *argc; ++j)
            {
                argv[j] = argv[j + 2];
            }

            *argc -= 2;

            argv[*argc] = NULL;

            break;
        }
    }

    if (output_path == NULL)
    {
        fprintf(
            stderr,
            "Error: missing instrumentation argument:\n"
            "  --p-output <trace-file>\n"
        );

        return 0;
    }

    __P_TRACE_FILE = fopen(
        output_path,
        "w"
    );

    if (__P_TRACE_FILE == NULL)
    {
        fprintf(
            stderr,
            "Error: cannot open trace file: %s\n",
            output_path
        );

        return 0;
    }

    atexit(
        __P_TRACE_CLOSE
    );

    return 1;
}


static void __P_TRACE(int id)
{
    if (__P_TRACE_FILE == NULL)
    {
        return;
    }

    fprintf(
        __P_TRACE_FILE,
        "P%d,",
        id
    );

    fflush(
        __P_TRACE_FILE
    );
}

/* ===== END INSTRUMENTATION HEADER ===== */

'''


# ============================================================
# Generate instrumented source
# ============================================================

def instrument_source(
    source_data,
    points
):
    additions = []

    # --------------------------------------------------------
    # Assign P1, P2, P3...
    # --------------------------------------------------------

    for marker, point in enumerate(
        points,
        start=1
    ):
        point.marker = marker

    # --------------------------------------------------------
    # Generate source inserts
    # --------------------------------------------------------

    for point in points:

        offset = point.offset

        indent = indentation_at(
            source_data,
            min(
                offset,
                len(source_data)
            )
        )

        # ====================================================
        # MAIN ENTRY
        #
        # Initialize tracing before first P.
        # ====================================================

        if point.kind == "MainEntry":

            argc_name = (
                point.data["argc"]
            )

            argv_name = (
                point.data["argv"]
            )

            text = (
                "\n"
                + indent
                + "    "
                + f"if (!__P_INIT(&{argc_name}, {argv_name}))"
                + "\n"
                + indent
                + "    "
                + "    return 2;"
                + "\n"
                + indent
                + "    "
                + f"__P_TRACE({point.marker});"
            )

        # ====================================================
        # After {
        # ====================================================

        elif (
            point.position
            == "after-brace"
        ):

            text = (
                "\n"
                + indent
                + "    "
                + f"__P_TRACE({point.marker});"
            )

        # ====================================================
        # Before statement
        # ====================================================

        elif (
            point.position
            == "before-statement"
        ):

            text = (
                f"__P_TRACE({point.marker});"
                "\n"
                + indent
            )

        # ====================================================
        # After statement
        # ====================================================

        elif (
            point.position
            == "after-statement"
        ):

            text = (
                "\n"
                + indent
                + f"__P_TRACE({point.marker});"
            )

        else:
            continue

        additions.append(
            (
                offset,
                text.encode(
                    "utf-8"
                )
            )
        )

    # --------------------------------------------------------
    # Insert from end to beginning so Clang byte offsets
    # remain valid.
    # --------------------------------------------------------

    output = source_data

    for offset, data in sorted(
        additions,
        key=lambda item:
        item[0],
        reverse=True
    ):

        output = (
            output[:offset]
            + data
            + output[offset:]
        )

    return output


# ============================================================
# Main
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description=(
            "Cross-platform C/C++ "
            "basic-block/path coverage instrumenter"
        )
    )

    # --------------------------------------------------------
    # Input file
    # --------------------------------------------------------

    parser.add_argument(
        "input",
        help=(
            "Input .c/.cpp source file"
        )
    )

    # --------------------------------------------------------
    # Output directory
    # --------------------------------------------------------

    parser.add_argument(
        "-d",
        "--output-dir",
        default="output",
        help=(
            "Directory for generated "
            "instrumented source"
        )
    )

    # --------------------------------------------------------
    # Optional output source name
    # --------------------------------------------------------

    parser.add_argument(
        "-o",
        "--output",
        help=(
            "Optional instrumented source filename"
        )
    )

    # --------------------------------------------------------
    # Optional explicit compiler
    # --------------------------------------------------------

    parser.add_argument(
        "--compiler",
        help=(
            "Path/name of clang or clang++. "
            "Normally detected automatically."
        )
    )

    # --------------------------------------------------------
    # Version
    # --------------------------------------------------------

    parser.add_argument(
        "--version",
        action="version",
        version="instrumenter 1.1.0"
    )

    # --------------------------------------------------------
    # Extra Clang arguments
    #
    # Example:
    #
    # instrumenter source.cpp -d out -- -Iinclude -DDEBUG
    # --------------------------------------------------------

    args, extra_args = (
        parser.parse_known_args()
    )

    if (
        extra_args
        and
        extra_args[0] == "--"
    ):

        extra_args = (
            extra_args[1:]
        )

    # ========================================================
    # Resolve input
    # ========================================================

    source = Path(
        args.input
    ).expanduser().resolve()

    if not source.is_file():

        print(
            f"Error: input file not found:\n"
            f"  {source}",
            file=sys.stderr
        )

        return 1

    # ========================================================
    # Language / compiler
    # ========================================================

    try:

        language = (
            detect_language(
                source
            )
        )

        compiler = (
            find_compiler(
                language,
                args.compiler
            )
        )

    except Exception as error:

        print(
            f"Error: {error}",
            file=sys.stderr
        )

        return 1

    # ========================================================
    # Output directory
    # ========================================================

    output_dir = Path(
        args.output_dir
    ).expanduser().resolve()

    try:

        output_dir.mkdir(
            parents=True,
            exist_ok=True
        )

    except OSError as error:

        print(
            f"Error creating output directory:\n"
            f"  {error}",
            file=sys.stderr
        )

        return 1

    # ========================================================
    # Output source path
    # ========================================================

    if args.output:

        output = (
            output_dir
            /
            Path(
                args.output
            ).name
        )

    else:

        output = (
            output_dir
            /
            (
                source.stem
                + "_instrumented"
                + source.suffix
            )
        )

    # ========================================================
    # Read source
    # ========================================================

    try:

        source_data = (
            source.read_bytes()
        )

    except OSError as error:

        print(
            f"Error reading input source:\n"
            f"  {error}",
            file=sys.stderr
        )

        return 1

    # ========================================================
    # Status
    # ========================================================

    print(
        "======================================"
    )

    print(
        " C / C++ Basic-Block Instrumenter"
    )

    print(
        "======================================"
    )

    print(
        f"Platform    : {platform_name()}"
    )

    print(
        f"Input       : {source}"
    )

    print(
        f"Language    : {language}"
    )

    print(
        f"Compiler    : {compiler}"
    )

    print(
        f"Output dir  : {output_dir}"
    )

    print(
        f"Output file : {output}"
    )

    print()

    # ========================================================
    # Parse AST
    # ========================================================

    try:

        ast = run_clang_ast(
            source,
            language,
            compiler,
            extra_args
        )

    except Exception as error:

        print(
            f"Error:\n{error}",
            file=sys.stderr
        )

        return 1

    # ========================================================
    # Collect instrumentation points
    # ========================================================

    try:

        points, warnings = (
            collect_points(
                ast,
                source,
                source_data
            )
        )

    except Exception as error:

        print(
            f"Error while analyzing source:\n"
            f"{error}",
            file=sys.stderr
        )

        return 1

    points = (
        remove_physical_duplicates(
            points
        )
    )

    # ========================================================
    # Generate instrumented code
    # ========================================================

    instrumented = (
        instrument_source(
            source_data,
            points
        )
    )

    # ========================================================
    # Runtime
    # ========================================================

    if language == "cpp":

        runtime = (
            make_cpp_runtime()
        )

    else:

        runtime = (
            make_c_runtime()
        )

    # ========================================================
    # Save generated source
    # ========================================================

    try:

        output.write_bytes(
            runtime.encode(
                "utf-8"
            )
            + instrumented
        )

    except OSError as error:

        print(
            f"Error writing instrumented source:\n"
            f"  {error}",
            file=sys.stderr
        )

        return 1

    # ========================================================
    # Report points
    # ========================================================

    print(
        f"Coverage points: {len(points)}"
    )

    print()

    for point in points:

        print(
            f'P{point.marker:<4}'
            f' line={point.line:<4}'
            f' {point.kind}'
        )

    # ========================================================
    # Warnings
    # ========================================================

    if warnings:

        print()
        print(
            "Warnings:"
        )

        for (
            offset,
            warning_type
        ) in warnings:

            if offset is not None:

                line = line_number(
                    source_data,
                    offset
                )

                print(
                    f"  line {line}: "
                    f"{warning_type}"
                )

            else:

                print(
                    f"  {warning_type}"
                )

        print()

        print(
            "Use braces { } around control-flow "
            "bodies for reliable instrumentation."
        )

    # ========================================================
    # Done
    # ========================================================

    print()

    print(
        "======================================"
    )

    print(
        " Generation completed"
    )

    print(
        "======================================"
    )

    print(
        f"Instrumented source : {output}"
    )

    print()

    print(
        "Runtime usage:"
    )

    if is_windows():

        print(
            "  program.exe --p-output <trace-file> "
            "<original-program-arguments>"
        )

    else:

        print(
            "  ./program --p-output <trace-file> "
            "<original-program-arguments>"
        )

    return 0


# ============================================================
# Entry
# ============================================================

if __name__ == "__main__":

    raise SystemExit(
        main()
    )