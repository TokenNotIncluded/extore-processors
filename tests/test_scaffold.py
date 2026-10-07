"""Scaffold only trusted local drafts, without modifying the runtime registry."""

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from extore_processors import catalog

ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "tools" / "new_processor.py"
TOOL_SPEC = importlib.util.spec_from_file_location("contribution_scaffold", TOOL_PATH)
tool = importlib.util.module_from_spec(TOOL_SPEC)
TOOL_SPEC.loader.exec_module(tool)


class ScaffoldTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "new-contribution"

    def test_cli_writes_only_the_four_expected_development_files(self):
        before = catalog()
        result = subprocess.run(
            [
                sys.executable,
                str(TOOL_PATH),
                "text_example",
                "--output",
                str(self.output),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Nothing was executed or registered", result.stdout)
        self.assertEqual(
            {
                path.relative_to(self.output).as_posix()
                for path in self.output.rglob("*")
                if path.is_file()
            },
            {
                "extore_processors/text_example.py",
                "tests/test_text_example.py",
                "examples/text_example.json",
                "README.md",
            },
        )
        self.assertEqual(catalog(), before)
        value = json.loads(
            (self.output / "examples" / "text_example.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            value,
            {"params": {"text": "world\n"}, "configuration": {"prefix": "Hello, "}},
        )
        self.assertFalse(list(self.output.rglob("__pycache__")))

    def test_generated_tests_exercise_the_real_dispatch_and_context(self):
        before = catalog()
        tool.generate("text_example", self.output)
        path = self.output / "tests" / "test_text_example.py"
        spec = importlib.util.spec_from_file_location("trusted_generated_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        suite = unittest.defaultTestLoader.loadTestsFromModule(module)
        stream = io.StringIO()
        result = unittest.TextTestRunner(stream=stream).run(suite)
        self.assertEqual(result.testsRun, 7)
        self.assertTrue(result.wasSuccessful(), stream.getvalue())
        self.assertEqual(catalog(), before)

    def test_identifier_validation_rejects_escape_and_injection(self):
        for processor_id in [
            "",
            "../escape",
            "id/path",
            "Text",
            "name-with-hyphens",
            "name.py",
            "class",
            "a" * 41,
            "1name",
            "名称",
            "x\nimport os",
            None,
        ]:
            with self.subTest(processor_id=processor_id):
                with self.assertRaisesRegex(ValueError, "^invalid_processor_id$"):
                    tool.generate(processor_id, self.output)
                self.assertFalse(self.output.exists())

    def test_every_current_registry_identifier_is_reserved(self):
        for spec in catalog():
            with self.subTest(processor_id=spec["id"]):
                with self.assertRaisesRegex(ValueError, "^processor_id_exists$"):
                    tool.generate(spec["id"], self.output)
                self.assertFalse(self.output.exists())

    def test_package_support_module_names_are_not_generated(self):
        for processor_id in ["catalog", "schema", "delivery_context"]:
            with self.subTest(processor_id=processor_id):
                with self.assertRaisesRegex(ValueError, "^processor_id_exists$"):
                    tool.generate(processor_id, self.output)
                self.assertFalse(self.output.exists())

    def test_existing_file_or_directory_is_not_overwritten(self):
        for kind in ["file", "directory", "nonempty_directory"]:
            path = self.root / kind
            if kind == "file":
                path.write_text("preserve", encoding="utf-8")
            else:
                path.mkdir()
                if kind == "nonempty_directory":
                    (path / "preserve").write_text("keep", encoding="utf-8")
            with self.subTest(kind=kind), self.assertRaises(FileExistsError):
                tool.generate("text_example", path)
            if kind == "file":
                self.assertEqual(path.read_text(encoding="utf-8"), "preserve")
            elif kind == "nonempty_directory":
                self.assertEqual(
                    (path / "preserve").read_text(encoding="utf-8"), "keep"
                )
            else:
                self.assertEqual(list(path.iterdir()), [])

    def test_final_symlink_or_dangling_symlink_is_not_followed(self):
        destination = self.root / "destination"
        destination.mkdir()
        for name, target in [
            ("link", destination),
            ("dangling", self.root / "not-created"),
        ]:
            path = self.root / name
            path.symlink_to(target, target_is_directory=True)
            with self.subTest(name=name), self.assertRaises(FileExistsError):
                tool.generate("text_example", path)
            self.assertTrue(path.is_symlink())
        self.assertEqual(list(destination.iterdir()), [])
        self.assertFalse((self.root / "not-created").exists())

    def test_symlink_in_any_parent_is_not_followed(self):
        destination = self.root / "destination"
        destination.mkdir()
        (destination / "nested").mkdir()
        link = self.root / "link"
        link.symlink_to(destination, target_is_directory=True)
        for path in [link / "output", link / "nested" / "output"]:
            with self.subTest(path=path), self.assertRaises(OSError):
                tool.generate("text_example", path)
        self.assertEqual(list(destination.iterdir()), [destination / "nested"])
        self.assertEqual(list((destination / "nested").iterdir()), [])

    def test_final_symlink_replacement_after_mkdir_is_not_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        original_mkdir = tool.os.mkdir

        def replace_with_symlink(path, mode=0o777, *, dir_fd=None):
            original_mkdir(path, mode, dir_fd=dir_fd)
            if path == self.output.name:
                tool.os.rename(
                    path, "created-directory", src_dir_fd=dir_fd, dst_dir_fd=dir_fd
                )
                tool.os.symlink(str(outside), path, dir_fd=dir_fd)

        with patch.object(tool.os, "mkdir", replace_with_symlink):
            with self.assertRaises(OSError):
                tool.generate("text_example", self.output)
        self.assertTrue(self.output.is_symlink())
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(list((self.root / "created-directory").iterdir()), [])

    def test_parent_escape_is_rejected_before_path_normalization(self):
        existing = self.root / "existing"
        existing.mkdir()
        path = str(existing) + "/../escaped"
        with self.assertRaisesRegex(ValueError, "^invalid_output_path$"):
            tool.generate("text_example", path)
        self.assertFalse((self.root / "escaped").exists())

    def test_missing_parent_does_not_create_extra_directories(self):
        with self.assertRaises(FileNotFoundError):
            tool.generate("text_example", self.root / "missing" / "output")
        self.assertFalse((self.root / "missing").exists())

    def test_cli_errors_do_not_include_private_paths(self):
        private = self.root / "private-directory-name"
        private.mkdir()
        result = subprocess.run(
            [
                sys.executable,
                str(TOOL_PATH),
                "text_example",
                "--output",
                str(private),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, "error: output_exists\n")
        self.assertNotIn(str(private), result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
