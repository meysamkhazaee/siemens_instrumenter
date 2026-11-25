#!/usr/bin/env python3
"""Unit tests for SIR (space, gzip, grep, sed, flex) and multi-file C instrumentation support."""

import io
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import instrumenter
import repos
import run_test_scenarios


class SirSupportTests(unittest.TestCase):
    def test_registry_includes_siemens_and_sir_subjects(self):
        for name in ("printtokens", "printtokens2", "replace", "schedule", "schedule2", "tcas", "totinfo"):
            self.assertIn(name, repos.REPO_MAP)
            self.assertEqual(repos.get_benchmark_suite(name), "Siemens")

        for name in ("space", "gzip", "grep", "sed", "flex"):
            self.assertIn(name, repos.REPO_MAP)
            self.assertEqual(repos.get_benchmark_suite(name), "SIR")
            self.assertTrue(repos.get_repo_info(name)["multi_file"])

    def test_multifile_overlay_discovery_and_exclusions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            flex_dir = root / "flex"
            orig_dir = flex_dir / "source"
            v1_dir = flex_dir / "versions.alt" / "versions.seeded" / "v1"
            orig_dir.mkdir(parents=True)
            v1_dir.mkdir(parents=True)

            for fname in ("main.c", "dfa.c", "parse.c", "libmain.c", "libyywrap.c"):
                (orig_dir / fname).write_text("int x;\n", encoding="utf-8")
            (orig_dir / "flexdef.h").write_text("#define X 1\n", encoding="utf-8")
            (orig_dir / "config.h").write_text("#define HAVE_STDLIB_H 1\n", encoding="utf-8")

            # v1 only overrides dfa.c (partial SIR version overlay)
            (v1_dir / "dfa.c").write_text("int x = 2;\n", encoding="utf-8")

            found_base = repos.find_repo_base(root, "flex")
            self.assertEqual(found_base, flex_dir)

            found_orig = repos.find_orig_dir(found_base, repos.get_repo_info("flex"))
            self.assertEqual(found_orig, orig_dir)

            versions = dict(repos.find_version_dirs(found_base))
            self.assertIn("v1", versions)

            resolved = repos.resolve_version_sources(found_orig, versions["v1"], repos.get_repo_info("flex"))
            self.assertEqual(sorted(resolved["c_files"].keys()), ["dfa.c", "main.c", "parse.c"])
            self.assertEqual(resolved["c_files"]["dfa.c"], v1_dir / "dfa.c")
            self.assertEqual(resolved["c_files"]["main.c"], orig_dir / "main.c")
            self.assertIn("config.h", resolved["h_files"])
            flags = repos.detect_compile_flags(resolved["h_files"], [orig_dir])
            self.assertIn("-DHAVE_CONFIG_H", flags)

    def test_universe_parsing_for_sir_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = root / "inputs"
            inputs.mkdir()
            sample_in = inputs / "sample.txt"
            sample_in.write_text("hello\n", encoding="utf-8")
            num_in = inputs / "1"
            num_in.write_text("numeric stdin\n", encoding="utf-8")
            adl_in = inputs / "gr1.adl"
            adl_in.write_text("ARRAY\n", encoding="utf-8")

            universe = root / "universe"
            universe.write_text(
                "\n".join([
                    "# comment",
                    "-n 'hello' ../inputs/sample.txt > out.txt 2>&1",
                    "-P [-E 'a+' ../inputs/sample.txt] -I [sample.txt]",
                    "< 1",
                    "958 1 gr1.adl",
                ]),
                encoding="utf-8",
            )

            tests = run_test_scenarios.parse_universe_tests(universe, root)
            self.assertEqual(len(tests), 4)

            self.assertEqual(tests[0]["args"][:2], ["-n", "hello"])
            self.assertEqual(Path(tests[0]["args"][2]), sample_in.resolve())
            self.assertNotIn(">", tests[0]["args"])

            self.assertEqual(tests[1]["args"][0], "-E")
            self.assertEqual(tests[1]["stdin_file"], sample_in.resolve())

            self.assertEqual(tests[2]["args"], [])
            self.assertEqual(tests[2]["stdin_file"], num_in.resolve())

            self.assertEqual(tests[3]["args"][:2], ["958", "1"])
            self.assertEqual(Path(tests[3]["args"][2]), adl_in.resolve())

    def test_multifile_marker_offset_and_yacc_line_node(self):
        src = b"int f() {\n    return 1;\n}\n"
        points = [
            instrumenter.Point(offset=9, kind="FunctionEntry", position="after-brace", line=1),
        ]
        out = instrumenter.instrument_source(src, points, start_marker=15)
        self.assertEqual(points[0].marker, 15)
        self.assertIn(b"__P_TRACE(15);", out)

        node = {"range": {"begin": {"file": "parse.y", "offset": 10}}}
        self.assertTrue(instrumenter.is_main_file_node(node, Path("parse.c")))

        header = instrumenter.make_c_runtime_header()
        runtime_c = instrumenter.make_c_runtime_source()
        self.assertIn("int __P_INIT(", header)
        self.assertIn("int __P_INIT(", runtime_c)
        self.assertNotIn("static int __P_INIT(", runtime_c)

    def test_tar_gz_archive_discovery_and_windows_sanitization(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_path = root / "totinfo.tar.gz"
            with tarfile.open(archive_path, "w:gz") as tf:
                files = {
                    "./totinfo/source.alt/source.orig/tot_info.c": b"int main(void) { return 0; }\n",
                    "./totinfo/versions.alt/versions.orig/v1/tot_info.c": b"int main(void) { return 1; }\n",
                    "./totinfo/testplans.alt/universe": b"< universe/tst2:.mat\n",
                    "./totinfo/inputs/universe/tst2:.mat": b"1 2 3\n",
                }
                for name, data in files.items():
                    info = tarfile.TarInfo(name=name)
                    info.size = len(data)
                    tf.addfile(info, io.BytesIO(data))

            base = repos.find_repo_base(root, "totinfo")
            self.assertIsNotNone(base)
            self.assertTrue((base / "source.alt" / "source.orig" / "tot_info.c").is_file())
            self.assertTrue((base / ".archive_extracted.json").is_file())

            orig_dir = repos.find_orig_dir(base, repos.get_repo_info("totinfo"))
            self.assertEqual(orig_dir, base / "source.alt" / "source.orig")

            versions = dict(repos.find_version_dirs(base))
            self.assertIn("v1", versions)

            tests = run_test_scenarios.parse_universe_tests(base / "testplans.alt" / "universe", base)
            self.assertEqual(len(tests), 1)
            self.assertIsNotNone(tests[0]["stdin_file"])
            self.assertTrue(tests[0]["stdin_file"].is_file())
            if os.name == "nt":
                self.assertIn("__colon__", tests[0]["stdin_file"].name)

    def test_fallthrough_casestmt_and_multiline_fault_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orig_c = root / "orig.c"
            faulty_c = root / "faulty.c"
            orig_code = (
                "int f(int x) {\n"
                "    switch (x) {\n"
                "    case 1:\n"
                "    case 2:\n"
                "        return 10;\n"
                "    default:\n"
                "        return 0;\n"
                "    }\n"
                "}\n"
                "int main(int argc, char *argv[]) { return f(argc); }\n"
            )
            faulty_code = (
                "int f(int x) {\n"
                "    switch (x) {\n"
                "    case 1:\n"
                "    case 3:\n"
                "    case 4:\n"
                "    case 2:\n"
                "        return 10;\n"
                "    default:\n"
                "        return 0;\n"
                "    }\n"
                "}\n"
                "int main(int argc, char *argv[]) { return f(argc); }\n"
            )
            orig_c.write_text(orig_code, encoding="utf-8")
            faulty_c.write_text(faulty_code, encoding="utf-8")

            markers, details, all_points = run_test_scenarios.get_project_fault_markers(
                {"tok.c": orig_c},
                {"tok.c": faulty_c},
            )
            case_entries = [p for p in all_points if p.kind == "CaseEntry"]
            # Lines 3 (case 1:), 4 (case 3:), 5 (case 4:), 6 (case 2:) must keep their own label lines.
            self.assertEqual([p.line for p in case_entries], [3, 4, 5, 6])
            self.assertEqual(len(markers), 2)
            self.assertEqual(details[0]["faulty_lines"], [4, 5])


if __name__ == "__main__":
    unittest.main()
