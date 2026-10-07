import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from extore_processors import text_cleanup
from extore_processors.schema import ProcessorError

ROOT = Path(__file__).resolve().parents[1]


def clean(text, **settings):
    parameters = text_cleanup.validate_parameters({"text": text})
    configuration = text_cleanup.validate_configuration(
        {
            "trim_lines": "yes",
            "remove_blank_lines": "yes",
            "deduplicate": "exact",
            **settings,
        }
    )
    return text_cleanup.process(parameters, configuration)


class TextCleanupTests(unittest.TestCase):
    def test_default_cleaning_preserves_first_occurrence_order_and_counts(self):
        output = clean("  张三  \n\n李四\n张三\n 王五 \n\t\n李四")
        self.assertEqual(output["cleaned_text"], "张三\n李四\n王五")
        self.assertEqual(
            output["report"],
            (
                "原始行数 / Input lines: 7\n"
                "保留行数 / Retained lines: 3\n"
                "删除空行 / Blank lines removed: 2\n"
                "删除重复行 / Duplicate lines removed: 2"
            ),
        )

    def test_mixed_line_endings_are_normalized_and_trailing_ending_is_preserved(self):
        output = clean("first\r\n第二条\rthird\nfirst\r\n")
        self.assertEqual(output["cleaned_text"], "first\n第二条\nthird\n")
        self.assertIn("Input lines: 4", output["report"])
        self.assertIn("Duplicate lines removed: 1", output["report"])

    def test_no_cleanup_only_normalizes_crlf_and_cr(self):
        raw = "  a \r\n\r\n a\t\ra\r"
        output = clean(
            raw, trim_lines="no", remove_blank_lines="no", deduplicate="none"
        )
        self.assertEqual(output["cleaned_text"], "  a \n\n a\t\na\n")
        self.assertIn("Retained lines: 4", output["report"])

    def test_casefold_only_changes_comparison_not_output(self):
        output = clean(
            "Beta\nbeta\nStraße\nSTRASSE\nALPHA\nalpha", deduplicate="casefold"
        )
        self.assertEqual(output["cleaned_text"], "Beta\nStraße\nALPHA")
        self.assertIn("Duplicate lines removed: 3", output["report"])

    def test_exact_mode_distinguishes_case_and_unicode_forms(self):
        output = clean("Alpha\nalpha\né\ne\u0301\né")
        self.assertEqual(output["cleaned_text"], "Alpha\nalpha\né\ne\u0301")
        self.assertIn("Duplicate lines removed: 1", output["report"])
        folded = clean("é\ne\u0301", deduplicate="casefold")
        self.assertEqual(folded["cleaned_text"], "é\ne\u0301")

    def test_blank_removal_precedes_deduplication_and_trimming_can_be_disabled(self):
        output = clean("a\n a\n \n \n\n", trim_lines="no", remove_blank_lines="no")
        self.assertEqual(output["cleaned_text"], "a\n a\n \n\n")
        self.assertIn("Input lines: 5", output["report"])
        self.assertIn("Blank lines removed: 0", output["report"])
        self.assertIn("Duplicate lines removed: 1", output["report"])
        output = clean(" \n \n\n")
        self.assertEqual(output["cleaned_text"], "")
        self.assertIn("Blank lines removed: 3", output["report"])
        self.assertIn("Duplicate lines removed: 0", output["report"])

    def test_empty_results_remain_successful_with_real_zero_counts(self):
        output = clean("")
        self.assertEqual(output["cleaned_text"], "")
        for metric in (
            "Input lines",
            "Retained lines",
            "Blank lines removed",
            "Duplicate lines removed",
        ):
            self.assertIn(metric + ": 0", output["report"])
        cleaned = next(
            f for f in text_cleanup.SPEC["outputs"] if f["key"] == "cleaned_text"
        )
        self.assertIs(cleaned["required"], False)

    def test_html_shell_and_spreadsheet_formula_strings_are_literal_data(self):
        value = '$(touch /tmp/extore-should-not-exist)\n<script>alert(1)</script>\n=HYPERLINK("https://example.test")'
        with (
            patch("subprocess.run", side_effect=AssertionError("execution forbidden")),
            patch("builtins.open", side_effect=AssertionError("file access forbidden")),
        ):
            self.assertEqual(clean(value)["cleaned_text"], value)

    def test_nul_surrogates_and_oversized_inputs_raise_safe_errors(self):
        for text in (
            "secret\x00text",
            "secret\ud800text",
            "secret\udffftext",
            "secret" + "x" * 10000,
        ):
            with (
                self.subTest(text=repr(text)),
                self.assertRaises(ProcessorError) as error,
            ):
                clean(text)
            self.assertNotIn("secret", str(error.exception))
        self.assertEqual(clean("文" * 10000)["cleaned_text"], "文" * 10000)

    def test_line_and_output_bounds_are_enforced(self):
        with (
            patch.object(text_cleanup, "MAX_LINES", 2),
            self.assertRaises(ProcessorError) as error,
        ):
            clean("a\nb\nc")
        self.assertEqual(error.exception.code, "too_many_lines")
        with (
            patch.object(text_cleanup, "MAX_OUTPUT_BYTES", 100),
            self.assertRaises(ProcessorError) as error,
        ):
            clean("文" * 100)
        self.assertEqual(error.exception.code, "output_too_large")

    def test_invalid_configuration_never_echoes_values(self):
        for key in ("trim_lines", "remove_blank_lines", "deduplicate"):
            with self.subTest(key=key), self.assertRaises(ProcessorError) as error:
                clean("text", **{key: "private-setting"})
            self.assertEqual(error.exception.code, "invalid_option")
            self.assertNotIn("private-setting", str(error.exception))
        self.assertEqual(
            text_cleanup.validate_configuration(
                {"trim_lines": "", "remove_blank_lines": "", "deduplicate": ""},
                allow_incomplete=True,
            ),
            {"trim_lines": "", "remove_blank_lines": "", "deduplicate": ""},
        )
        with self.assertRaises(ProcessorError):
            text_cleanup.validate_configuration(
                {"trim_lines": "maybe"}, allow_incomplete=True
            )

    def test_schema_is_bilingual_and_settings_are_not_secrets(self):
        for collection in ("parameters", "outputs", "configuration"):
            for field in text_cleanup.SPEC[collection]:
                self.assertEqual(set(field["label"]), {"zh-CN", "en"})
                self.assertTrue(all(field["description"].values()))
        self.assertTrue(
            all(
                field["secret"] is False for field in text_cleanup.SPEC["configuration"]
            )
        )


class TextCleanupCLITests(unittest.TestCase):
    def invoke(self, text, settings=None):
        return subprocess.run(
            [sys.executable, "-m", "extore_processors", "text_cleanup"],
            input=json.dumps(
                {"params": {"text": text}, "configuration": settings or {}}
            ),
            text=True,
            capture_output=True,
            cwd=ROOT,
            timeout=10,
            check=False,
        )

    def test_jsonl_protocol_applies_defaults_and_produces_one_result(self):
        invocation = self.invoke(" 张三 \r\n李四\n张三\n\n")
        self.assertEqual(invocation.returncode, 0, invocation.stderr)
        messages = [json.loads(line) for line in invocation.stdout.splitlines()]
        results = [message for message in messages if message["kind"] == "result"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["state"], "succeeded")
        self.assertEqual(results[0]["output"]["cleaned_text"], "张三\n李四\n")
        self.assertIn("Input lines: 4", results[0]["output"]["report"])
        self.assertEqual(invocation.stderr, "")

    def test_empty_and_whitespace_inputs_succeed_through_catalog_validation(self):
        from extore_processors import run

        for text, count in (("", 0), (" \n\t\n\n", 3)):
            with self.subTest(text=text):
                result = run("text_cleanup", {"text": text}, {})
                self.assertEqual(result["status"], "succeeded")
                self.assertEqual(result["output"]["cleaned_text"], "")
                self.assertIn(f"Input lines: {count}", result["output"]["report"])
                self.assertIn(
                    f"Blank lines removed: {count}", result["output"]["report"]
                )

    def test_cli_preserves_executable_looking_content_as_literal_text(self):
        text = "$(echo command)\n<script>example</script>\n=1+1"
        invocation = self.invoke(text)
        self.assertEqual(invocation.returncode, 0, invocation.stderr)
        result = json.loads(invocation.stdout.splitlines()[-1])
        self.assertEqual(result["output"]["cleaned_text"], text)

    def test_cli_safe_error_does_not_echo_rejected_customer_input(self):
        invocation = self.invoke("private-customer-value\x00")
        self.assertEqual(invocation.returncode, 2)
        self.assertNotIn(
            "private-customer-value", invocation.stdout + invocation.stderr
        )
        self.assertEqual(
            json.loads(invocation.stderr), {"error": "invalid_text", "field": "text"}
        )


if __name__ == "__main__":
    unittest.main()
