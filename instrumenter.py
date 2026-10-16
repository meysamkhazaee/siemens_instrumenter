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
# Platform
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
        f"Compiler not found: {value}"
    )


def find_compiler(language, custom_compiler=None):

    if custom_compiler:
        return resolve_custom_compiler(
            custom_compiler
        )

    compiler_name = (
        "clang++"
        if language == "cpp"
        else "clang"
    )

    # PATH
    found = shutil.which(compiler_name)

    if found:
        return str(Path(found).resolve())

    # Windows
    if is_windows():

        exe = (
            "clang++.exe"
            if language == "cpp"
            else "clang.exe"
        )

        candidates = [
            Path(rf"C:\msys64\ucrt64\bin\{exe}"),
            Path(rf"C:\msys64\mingw64\bin\{exe}"),
            Path(rf"C:\msys64\clang64\bin\{exe}"),
            Path(rf"C:\Program Files\LLVM\bin\{exe}"),
        ]

    # Linux
    else:

        candidates = [
            Path(f"/usr/bin/{compiler_name}"),
            Path(f"/usr/local/bin/{compiler_name}"),
            Path(f"/opt/llvm/bin/{compiler_name}"),
        ]

    for candidate in candidates:

        if candidate.is_file():
            return str(candidate.resolve())

    raise RuntimeError(
        f"Cannot find {compiler_name}.\n"
        f"Install Clang or use:\n"
        f"  --compiler <path>"
    )


# ============================================================
# Language
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
        f"Unsupported source extension: {extension}"
    )


# ============================================================
# Process helper
# ============================================================

def run_process(command):

    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


# ============================================================
# Clang AST
# ============================================================

def run_clang_ast(
    source,
    language,
    compiler,
    extra_args
):

    common_flags = [
        "-fsyntax-only",
        "-Xclang",
        "-ast-dump=json",
    ]

    # ========================================================
    # C++
    # ========================================================

    if language == "cpp":

        command = [
            compiler,
            "-std=c++17",
            *common_flags,
            *extra_args,
            str(source),
        ]

        result = run_process(command)

        if result.returncode != 0:

            raise RuntimeError(
                "Clang C++ parsing failed:\n\n"
                + result.stderr
            )

        try:
            ast = json.loads(result.stdout)

        except json.JSONDecodeError as error:

            raise RuntimeError(
                "Could not parse Clang AST JSON:\n"
                + str(error)
            )

        return (
            ast,
            "C++17",
            False,
        )

    # ========================================================
    # C
    #
    # First:
    #   GNU C11
    #
    # Fallback:
    #   GNU89 / K&R
    # ========================================================

    modes = [

        (
            "GNU C11",
            [
                "-std=gnu11",
            ],
            False,
        ),

        (
            "GNU89 / K&R C",
            [
                "-std=gnu89",

                "-Wno-implicit-int",
                "-Wno-implicit-function-declaration",
                "-Wno-return-mismatch",
                "-Wno-return-type",
                "-Wno-deprecated-non-prototype",
                "-Wno-empty-body",
            ],
            True,
        ),
    ]

    failures = []

    for (
        mode_name,
        mode_flags,
        legacy_c
    ) in modes:

        command = [
            compiler,
            *mode_flags,
            *common_flags,
            *extra_args,
            str(source),
        ]

        result = run_process(command)

        if result.returncode == 0:

            try:
                ast = json.loads(
                    result.stdout
                )

            except json.JSONDecodeError as error:

                raise RuntimeError(
                    "Could not parse Clang AST JSON:\n"
                    + str(error)
                )

            return (
                ast,
                mode_name,
                legacy_c,
            )

        failures.append(
            (
                mode_name,
                result.stderr
            )
        )

    message = (
        "Clang could not parse the C source.\n\n"
        "Tried parser modes:\n"
    )

    for mode_name, _ in failures:
        message += f"  - {mode_name}\n"

    message += (
        "\nLast Clang error:\n\n"
        + failures[-1][1]
    )

    raise RuntimeError(message)


# ============================================================
# AST location helpers
# ============================================================

def unwrap_location(location):

    if not isinstance(location, dict):
        return {}

    expansion = location.get(
        "expansionLoc"
    )

    if isinstance(expansion, dict):
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

    offset = location.get("offset")

    if isinstance(offset, int):
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

    offset = location.get("offset")

    token_length = location.get(
        "tokLen",
        1
    )

    if not isinstance(offset, int):
        return None

    if not isinstance(token_length, int):
        token_length = 1

    return (
        offset
        + token_length
    )


def get_end_token_offset(node):

    location = (
        node
        .get("range", {})
        .get("end", {})
    )

    location = unwrap_location(
        location
    )

    offset = location.get("offset")

    if isinstance(offset, int):
        return offset

    return None


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

    # Header-originated AST node.
    if "includedFrom" in location:
        return False

    filename = location.get("file")

    # Main translation unit frequently has no filename here.
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
            b"\t",
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
        self.data = data or {}

        self.marker = None


# ============================================================
# Collect instrumentation points
# ============================================================

def collect_points(
    ast,
    source,
    source_data,
    legacy_c
):

    points = []
    seen = set()

    main_found = False

    # ========================================================
    # Add P point
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

        if offset > len(source_data):
            return

        key = (
            offset,
            kind,
            position,
        )

        if key in seen:
            return

        seen.add(key)

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
    # Compound {
    # ========================================================

    def body_brace(node):

        if node is None:
            return None

        if node.get("kind") != "CompoundStmt":
            return None

        offset = get_begin(node)

        if offset is None:
            return None

        if offset >= len(source_data):
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
    # Full source statement end
    #
    # Clang often reports:
    #
    #     x++
    #
    # instead of:
    #
    #     x++;
    #
    # This extends through the trailing semicolon.
    # ========================================================

    def statement_end(statement):

        end = get_end(statement)

        if end is None:
            return None

        position = end

        while position < len(source_data):

            current = source_data[
                position:position + 1
            ]

            # whitespace
            if current in (
                b" ",
                b"\t",
                b"\r",
                b"\n",
            ):
                position += 1
                continue

            # /* ... */
            if (
                source_data[
                    position:position + 2
                ]
                == b"/*"
            ):

                close = source_data.find(
                    b"*/",
                    position + 2
                )

                if close < 0:
                    break

                position = close + 2
                continue

            # // ...
            if (
                source_data[
                    position:position + 2
                ]
                == b"//"
            ):

                close = source_data.find(
                    b"\n",
                    position + 2
                )

                if close < 0:
                    return len(source_data)

                position = close + 1
                continue

            break

        if (
            position < len(source_data)
            and
            source_data[
                position:position + 1
            ]
            == b";"
        ):

            return position + 1

        return end

    # ========================================================
    # Block entry
    #
    # For old C89/K&R code we MUST NOT insert a statement
    # before local declarations.
    #
    # Bad C89:
    #
    # {
    #     __P_TRACE(...);
    #     int x;
    # }
    #
    # Therefore in legacy C we insert after all leading
    # DeclStmt nodes.
    # ========================================================

    def block_entry_location(compound):

        brace = body_brace(compound)

        if brace is None:
            return None, None

        if not legacy_c:

            return (
                brace + 1,
                "after-brace"
            )

        for child in children(compound):

            if child.get("kind") == "DeclStmt":
                continue

            offset = get_begin(child)

            if offset is not None:

                return (
                    offset,
                    "before-statement"
                )

        # Block contains declarations only.
        closing_brace = get_end_token_offset(
            compound
        )

        if closing_brace is not None:

            return (
                closing_brace,
                "before-statement"
            )

        return (
            brace + 1,
            "after-brace"
        )

    def add_block_entry(
        compound,
        kind,
        data=None
    ):

        offset, position = (
            block_entry_location(
                compound
            )
        )

        add(
            offset,
            kind,
            position,
            data
        )

    # ========================================================
    # Wrap unbraced body
    #
    # Original:
    #
    # if (x)
    #     foo();
    #
    # Generated:
    #
    # if (x)
    # {
    #     P;
    #     foo();
    # }
    # ========================================================

    def add_wrapped_body(
        statement,
        kind
    ):

        begin = get_begin(statement)
        end = statement_end(statement)

        if (
            begin is None
            or end is None
        ):
            return

        add(
            begin,
            kind,
            "wrap-statement",
            {
                "end": end
            }
        )

    # ========================================================
    # Single nested statement
    # ========================================================

    def process_single_statement(
        statement,
        covered
    ):

        kind = statement.get("kind")

        if kind == "IfStmt":

            return process_if(
                statement,
                covered,
                False
            )

        if kind in LOOP_KINDS:

            return process_loop(
                statement,
                covered
            )

        if kind == "SwitchStmt":

            return process_switch(
                statement,
                covered,
                False
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
    # IF
    # ========================================================

    def process_if(
        statement,
        covered,
        has_next
    ):

        items = children(statement)

        has_else = statement.get(
            "hasElse",
            False
        )

        if has_else:

            if len(items) < 2:
                return covered

            then_statement = items[-2]
            else_statement = items[-1]

        else:

            if not items:
                return covered

            then_statement = items[-1]
            else_statement = None

        # Current straight-line block does not yet have a P.
        if not covered:

            add(
                get_begin(statement),
                "IfBlock",
                "before-statement"
            )

            covered = True

        # ----------------------------------------------------
        # THEN
        # ----------------------------------------------------

        if (
            then_statement.get("kind")
            == "CompoundStmt"
        ):

            process_compound(
                then_statement,
                entry_probe=True,
                entry_kind="IfThen",
                initial_covered=False
            )

        else:

            add_wrapped_body(
                then_statement,
                "IfThen"
            )

            process_single_statement(
                then_statement,
                True
            )

        # ----------------------------------------------------
        # ELSE
        # ----------------------------------------------------

        if else_statement is not None:

            if (
                else_statement.get("kind")
                == "CompoundStmt"
            ):

                process_compound(
                    else_statement,
                    entry_probe=True,
                    entry_kind="IfElse",
                    initial_covered=False
                )

            elif (
                else_statement.get("kind")
                == "IfStmt"
            ):

                # else if(...)
                process_single_statement(
                    else_statement,
                    False
                )

            else:

                add_wrapped_body(
                    else_statement,
                    "IfElse"
                )

                process_single_statement(
                    else_statement,
                    True
                )

        # ----------------------------------------------------
        # Merge point
        # ----------------------------------------------------

        if has_next:

            add(
                statement_end(statement),
                "IfMerge",
                "after-statement"
            )

            return True

        return False

    # ========================================================
    # LOOP
    # ========================================================

    def process_loop(
        statement,
        covered
    ):

        kind = statement.get("kind")

        items = children(statement)

        if not items:
            return covered

        # No duplicate marker when preceding block already
        # represents entry to this loop.
        if not covered:

            add(
                get_begin(statement),
                "LoopHeader",
                "before-statement"
            )

            covered = True

        # ----------------------------------------------------
        # Body
        # ----------------------------------------------------

        if kind == "DoStmt":
            body = items[0]
        else:
            body = items[-1]

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

            # Support:
            #
            # while(...)
            #     statement;
            #
            # for(...);
            #
            add_wrapped_body(
                body,
                kind + "Body"
            )

            process_single_statement(
                body,
                True
            )

        # Loop completion / zero-iteration path.
        add(
            statement_end(statement),
            kind + "Exit",
            "after-statement"
        )

        return True

    # ========================================================
    # SWITCH / CASE
    # ========================================================

    def process_switch_label(label):

        items = children(label)

        if not items:
            return True

        # Last child is the statement following:
        #
        # case X:
        #
        # or:
        #
        # default:
        sub_statement = items[-1]

        label_kind = label.get("kind")

        add(
            get_begin(sub_statement),
            (
                "DefaultEntry"
                if label_kind == "DefaultStmt"
                else "CaseEntry"
            ),
            "before-statement"
        )

        kind = sub_statement.get("kind")

        # case 1:
        # case 2:
        if kind in {
            "CaseStmt",
            "DefaultStmt",
        }:

            return process_switch_label(
                sub_statement
            )

        if kind == "IfStmt":

            return process_if(
                sub_statement,
                True,
                False
            )

        if kind in LOOP_KINDS:

            return process_loop(
                sub_statement,
                True
            )

        if kind == "SwitchStmt":

            return process_switch(
                sub_statement,
                True,
                False
            )

        if kind == "CompoundStmt":

            process_compound(
                sub_statement,
                entry_probe=False,
                initial_covered=True
            )

            return True

        if kind in {
            "ReturnStmt",
            "BreakStmt",
            "ContinueStmt",
        }:

            return False

        return True

    def process_switch(
        statement,
        covered,
        has_next
    ):

        items = children(statement)

        if not items:
            return covered

        if not covered:

            add(
                get_begin(statement),
                "SwitchBlock",
                "before-statement"
            )

            covered = True

        body = items[-1]

        if body.get("kind") == "CompoundStmt":

            switch_covered = False

            body_items = children(body)

            for index, child in enumerate(
                body_items
            ):

                kind = child.get("kind")

                child_has_next = (
                    index + 1
                    < len(body_items)
                )

                if kind in {
                    "CaseStmt",
                    "DefaultStmt",
                }:

                    switch_covered = (
                        process_switch_label(
                            child
                        )
                    )

                elif kind == "IfStmt":

                    switch_covered = (
                        process_if(
                            child,
                            switch_covered,
                            child_has_next
                        )
                    )

                elif kind in LOOP_KINDS:

                    switch_covered = (
                        process_loop(
                            child,
                            switch_covered
                        )
                    )

                elif kind == "SwitchStmt":

                    switch_covered = (
                        process_switch(
                            child,
                            switch_covered,
                            child_has_next
                        )
                    )

                elif kind in {
                    "ReturnStmt",
                    "BreakStmt",
                    "ContinueStmt",
                }:

                    if not switch_covered:

                        add(
                            get_begin(child),
                            kind.replace(
                                "Stmt",
                                ""
                            )
                            + "Block",
                            "before-statement"
                        )

                    switch_covered = False

                elif kind == "CompoundStmt":

                    process_compound(
                        child,
                        entry_probe=not switch_covered,
                        entry_kind="BlockEntry",
                        initial_covered=switch_covered
                    )

                    switch_covered = True

        if has_next:

            add(
                statement_end(statement),
                "SwitchExit",
                "after-statement"
            )

            return True

        return False

    # ========================================================
    # Compound statement
    # ========================================================

    def process_compound(
        compound,
        entry_probe=False,
        entry_kind="BlockEntry",
        initial_covered=False
    ):

        covered = initial_covered

        statements = children(
            compound
        )

        if entry_probe:

            add_block_entry(
                compound,
                entry_kind
            )

            covered = True

        for index, statement in enumerate(
            statements
        ):

            kind = statement.get("kind")

            has_next = (
                index + 1
                < len(statements)
            )

            # IF
            if kind == "IfStmt":

                covered = process_if(
                    statement,
                    covered,
                    has_next
                )

            # LOOP
            elif kind in LOOP_KINDS:

                covered = process_loop(
                    statement,
                    covered
                )

            # SWITCH
            elif kind == "SwitchStmt":

                covered = process_switch(
                    statement,
                    covered,
                    has_next
                )

            # RETURN
            elif kind == "ReturnStmt":

                if not covered:

                    add(
                        get_begin(statement),
                        "ReturnBlock",
                        "before-statement"
                    )

                covered = False

            # BREAK
            elif kind == "BreakStmt":

                if not covered:

                    add(
                        get_begin(statement),
                        "BreakBlock",
                        "before-statement"
                    )

                covered = False

            # CONTINUE
            elif kind == "ContinueStmt":

                if not covered:

                    add(
                        get_begin(statement),
                        "ContinueBlock",
                        "before-statement"
                    )

                covered = False

            # Nested block
            elif kind == "CompoundStmt":

                process_compound(
                    statement,
                    entry_probe=not covered,
                    entry_kind="BlockEntry",
                    initial_covered=covered
                )

                covered = True

            # Declaration / expression / assignment / call...
            else:
                pass

        return covered

    # ========================================================
    # AST walk
    # ========================================================

    def walk(node):

        nonlocal main_found

        if not isinstance(node, dict):
            return

        kind = node.get("kind")

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

                main_found = True

                parameters = [
                    child
                    for child
                    in function_children
                    if (
                        child.get("kind")
                        == "ParmVarDecl"
                    )
                ]

                if len(parameters) < 2:

                    raise RuntimeError(
                        "main() must provide argc and argv.\n\n"
                        "Supported forms include:\n"
                        "  int main(int argc, char *argv[])\n"
                        "  main(argc, argv) ...  /* K&R C */\n\n"
                        "argc/argv are required because the "
                        "trace file is supplied using --p-output."
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
                        "Could not determine argc/argv names "
                        "in main()."
                    )

                entry_offset, entry_position = (
                    block_entry_location(
                        body
                    )
                )

                add(
                    entry_offset,
                    "MainEntry",
                    entry_position,
                    {
                        "argc": argc_name,
                        "argv": argv_name,
                    }
                )

                # MainEntry already covers the initial block.
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

            # Don't process this function twice.
            return

        for child in children(node):
            walk(child)

    walk(ast)

    if not main_found:

        raise RuntimeError(
            "No main(argc, argv) function was found.\n"
            "This instrumenter currently requires main() "
            "because --p-output is initialized there."
        )

    points.sort(
        key=lambda point:
        point.offset
    )

    return points


# ============================================================
# Remove duplicate physical locations
# ============================================================

def remove_physical_duplicates(points):

    result = []
    used_offsets = set()

    for point in points:

        if point.offset in used_offsets:
            continue

        used_offsets.add(
            point.offset
        )

        result.append(point)

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
 * Runtime usage:
 *
 * program --p-output trace.txt <original arguments>
 *
 * --p-output and its path are removed from argc/argv before
 * the original application logic executes.
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

            for (int j = i; j + 2 < *argc; ++j)
            {
                argv[j] = argv[j + 2];
            }

            *argc -= 2;

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
#
# Intentionally C89 compatible.
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
 * Runtime usage:
 *
 * program --p-output trace.txt <original arguments>
 *
 * --p-output and its path are removed from argc/argv before
 * the original application logic executes.
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

    insertions = []

    # P1, P2, P3...
    for marker, point in enumerate(
        points,
        start=1
    ):
        point.marker = marker

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
        # main()
        # ====================================================

        if point.kind == "MainEntry":

            argc_name = point.data["argc"]
            argv_name = point.data["argv"]

            if point.position == "after-brace":

                inner_indent = (
                    indent
                    + "    "
                )

                text = (
                    "\n"
                    + inner_indent
                    + f"if (!__P_INIT(&{argc_name}, {argv_name}))"
                    + "\n"
                    + inner_indent
                    + "    return 2;"
                    + "\n"
                    + inner_indent
                    + f"__P_TRACE({point.marker});"
                )

            else:

                text = (
                    f"if (!__P_INIT(&{argc_name}, {argv_name})) "
                    f"return 2;"
                    + "\n"
                    + indent
                    + f"__P_TRACE({point.marker});"
                    + "\n"
                    + indent
                )

        # ====================================================
        # Braced block entry
        # ====================================================

        elif point.position == "after-brace":

            text = (
                "\n"
                + indent
                + "    "
                + f"__P_TRACE({point.marker});"
            )

        # ====================================================
        # Before statement
        # ====================================================

        elif point.position == "before-statement":

            text = (
                f"__P_TRACE({point.marker});"
                + "\n"
                + indent
            )

        # ====================================================
        # After statement
        # ====================================================

        elif point.position == "after-statement":

            text = (
                "\n"
                + indent
                + f"__P_TRACE({point.marker});"
            )

        # ====================================================
        # Unbraced body
        # ====================================================

        elif point.position == "wrap-statement":

            body_end = point.data["end"]

            opening = (
                "{"
                + "\n"
                + indent
                + "    "
                + f"__P_TRACE({point.marker});"
                + "\n"
                + indent
            )

            closing = (
                "\n"
                + indent
                + "}"
            )

            # Closing brace.
            #
            # Priority 0 is intentional. If a loop-exit marker
            # exists at the same original byte offset, the
            # closing brace must appear before that marker.
            insertions.append(
                (
                    body_end,
                    0,
                    closing.encode("utf-8")
                )
            )

            text = opening

        else:
            continue

        insertions.append(
            (
                offset,
                1,
                text.encode("utf-8")
            )
        )

    # ========================================================
    # Apply backwards.
    #
    # Original Clang offsets remain valid because later source
    # positions are modified first.
    # ========================================================

    output = source_data

    for (
        offset,
        priority,
        data
    ) in sorted(
        insertions,
        key=lambda item: (
            item[0],
            item[1]
        ),
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
            "Cross-platform C/C++ basic-block/path instrumenter"
        )
    )

    parser.add_argument(
        "input",
        help="Input C/C++ source file"
    )

    parser.add_argument(
        "-d",
        "--output-dir",
        default="output",
        help="Directory for generated instrumented source"
    )

    parser.add_argument(
        "-o",
        "--output",
        help="Optional output source filename"
    )

    parser.add_argument(
        "--compiler",
        help=(
            "Path/name of clang or clang++. "
            "Normally detected automatically."
        )
    )

    parser.add_argument(
        "--version",
        action="version",
        version="instrumenter 1.2.0"
    )

    # Anything after -- is passed directly to Clang.
    #
    # Example:
    #
    # instrumenter main.c -d out -- -Iinclude -DDEBUG
    #
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
    # Input source
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

        language = detect_language(
            source
        )

        compiler = find_compiler(
            language,
            args.compiler
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

    raw_output_dir = args.output_dir

    output_dir = Path(
        raw_output_dir
    ).expanduser()

    if not output_dir.is_absolute():

        output_dir = (
            Path.cwd()
            / output_dir
        )

    output_dir = output_dir.resolve()

    try:

        output_dir.mkdir(
            parents=True,
            exist_ok=True
        )

    except PermissionError:

        print(
            "\nError: permission denied while creating "
            "output directory:",
            file=sys.stderr
        )

        print(
            f"  {output_dir}",
            file=sys.stderr
        )

        if is_windows():

            print(
                "\nWhen using Git Bash with a Windows EXE, "
                "prefer a relative path:",
                file=sys.stderr
            )

            print(
                "  -d ./output/siemens",
                file=sys.stderr
            )

        return 1

    except OSError as error:

        print(
            f"\nError creating output directory:\n"
            f"  {output_dir}\n"
            f"  {error}",
            file=sys.stderr
        )

        return 1

    # ========================================================
    # Output source
    # ========================================================

    if args.output:

        output = (
            output_dir
            /
            Path(args.output).name
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
        f"Platform     : {platform_name()}"
    )

    print(
        f"Input        : {source}"
    )

    print(
        f"Language     : {language}"
    )

    print(
        f"Compiler     : {compiler}"
    )

    print(
        f"Output dir   : {output_dir}"
    )

    print(
        f"Output file  : {output}"
    )

    print(
        "Trace output : runtime --p-output <file>"
    )

    print()

    # ========================================================
    # Parse source
    # ========================================================

    try:

        (
            ast,
            parser_mode,
            legacy_c
        ) = run_clang_ast(
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

    print(
        f"Parser mode  : {parser_mode}"
    )

    print()

    # ========================================================
    # Collect P points
    # ========================================================

    try:

        points = collect_points(
            ast,
            source,
            source_data,
            legacy_c
        )

    except Exception as error:

        print(
            f"Error while analyzing source:\n"
            f"{error}",
            file=sys.stderr
        )

        return 1

    points = remove_physical_duplicates(
        points
    )

    # ========================================================
    # Generate
    # ========================================================

    instrumented = instrument_source(
        source_data,
        points
    )

    runtime = (
        make_cpp_runtime()
        if language == "cpp"
        else make_c_runtime()
    )

    # ========================================================
    # Write
    # ========================================================

    try:

        output.write_bytes(
            runtime.encode("utf-8")
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
    # Report
    # ========================================================

    print(
        f"Coverage points: {len(points)}"
    )

    print()

    for point in points:

        print(
            f"P{point.marker:<4}"
            f" line={point.line:<4}"
            f" {point.kind}"
        )

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
        "Runtime trace usage:"
    )

    if is_windows():

        print(
            "  program.exe --p-output <trace.txt> "
            "<original arguments>"
        )

    else:

        print(
            "  ./program --p-output <trace.txt> "
            "<original arguments>"
        )

    if legacy_c:

        print()

        print(
            "Legacy C detected."
        )

        print(
            "Compile the generated source using GNU89 "
            "compatibility when required."
        )

    return 0


# ============================================================
# Entry
# ============================================================

if __name__ == "__main__":

    raise SystemExit(
        main()
    )