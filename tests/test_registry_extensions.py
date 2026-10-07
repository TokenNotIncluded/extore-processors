import json
import unittest
from unittest.mock import patch

from extore_processors import (
    ProcessorContext,
    ProcessorError,
    run,
    validate_configuration,
)


class RegistryResultBudgetTests(unittest.TestCase):
    def invoke_with_output(self, output, events):
        context = ProcessorContext(emit=events.append)
        with patch(
            "extore_processors.catalog._HANDLERS",
            {"document_template": lambda _params, _settings: output},
        ):
            return run(
                "document_template",
                {"title": "Test", "body": "Test"},
                {},
                context=context,
            )

    def test_exact_result_boundary_includes_json_envelope(self):
        empty = {"content": "", "format": "plain"}
        overhead = len(
            json.dumps(
                {"kind": "result", "state": "succeeded", "output": empty},
                ensure_ascii=False,
            ).encode("utf-8")
        )
        output = {**empty, "content": "x" * (100000 - overhead)}
        events = []
        self.assertEqual(self.invoke_with_output(output, events)["output"], output)
        self.assertEqual(events[-1]["progress"], 99)
        with self.assertRaises(ProcessorError) as error:
            self.invoke_with_output({**output, "content": output["content"] + "x"}, [])
        self.assertEqual(error.exception.code, "output_too_large")

    def test_budget_counts_utf8_json_escapes_and_all_output_fields(self):
        for output in [
            {"content": "汉" * 34000, "format": "plain"},
            {"content": "\n" * 51000 + "x", "format": "plain"},
            {"content": "x" * 51000, "format": "x" * 51000},
        ]:
            events = []
            with self.subTest(
                output_lengths={key: len(value) for key, value in output.items()}
            ):
                with self.assertRaises(ProcessorError) as error:
                    self.invoke_with_output(output, events)
                self.assertEqual(error.exception.code, "output_too_large")
                self.assertTrue(all(event["progress"] < 99 for event in events))

    def test_invalid_unicode_result_has_safe_error(self):
        with self.assertRaises(ProcessorError) as error:
            self.invoke_with_output({"content": "private\ud800", "format": "plain"}, [])
        self.assertEqual(error.exception.code, "invalid_output_text")
        self.assertNotIn("private", str(error.exception))


class RegistryConfigurationTests(unittest.TestCase):
    def test_new_select_values_are_validated_before_whitespace_removal(self):
        for processor_id, field, value in [
            ("csv_summary", "invalid_policy", "\x0breject"),
            ("json_formatter", "indent", "\x0b2"),
            ("text_cleanup", "trim_lines", "\x0byes"),
            ("document_template", "output_format", "\x0bplain"),
        ]:
            with (
                self.subTest(processor_id=processor_id),
                self.assertRaises(ProcessorError),
            ):
                validate_configuration(processor_id, {field: value})

    def test_existing_processor_text_input_still_normalizes(self):
        output = run(
            "personalized_text", {"name": "  Ada  "}, {"template": "Hello $name"}
        )
        self.assertEqual(output["output"]["content"], "Hello Ada")


if __name__ == "__main__":
    unittest.main()
