import json
import os
import subprocess
import sys
import unittest
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from pathlib import Path
from unittest.mock import patch

from extore_processors.csv_summary import (
    CALCULATION_PRECISION,
    MAX_COLUMNS,
    MAX_DATA_ROWS,
    MAX_GROUPS,
    SPEC,
    process,
    validate_configuration,
    validate_parameters,
)
from extore_processors.schema import ProcessorError

ROOT = Path(__file__).resolve().parents[1]


def params(csv_text="value\n1\n3\n", value_column="value", group_column=""):
    return {
        "csv_text": csv_text,
        "value_column": value_column,
        "group_column": group_column,
    }


def summary(values=None, settings=None):
    return json.loads(process(values or params(), settings or {})["summary"])


class CSVSummaryTests(unittest.TestCase):
    def assert_safe_error(self, values, code, settings=None, forbidden=()):
        with self.assertRaises(ProcessorError) as caught:
            process(values, settings or {})
        self.assertEqual(caught.exception.code, code)
        for text in forbidden:
            self.assertNotIn(text, str(caught.exception))

    def test_schema_defines_bilingual_useful_inputs_and_outputs(self):
        self.assertEqual(SPEC["id"], "csv_summary")
        self.assertEqual(SPEC["schema_version"], 1)
        self.assertEqual(SPEC["delivery"], "content")
        self.assertEqual(
            [value["key"] for value in SPEC["parameters"]],
            ["csv_text", "value_column", "group_column"],
        )
        self.assertEqual(
            [value["key"] for value in SPEC["outputs"]], ["report", "summary"]
        )
        for key in ["name", "description"]:
            self.assertTrue({"zh-CN", "en"} <= set(SPEC[key]))
        for kind in ["parameters", "outputs", "configuration"]:
            for value in SPEC[kind]:
                self.assertTrue({"zh-CN", "en"} <= set(value["label"]))
                self.assertTrue(all(value["description"].values()))
        self.assertFalse(SPEC["configuration"][0]["secret"])
        self.assertEqual(SPEC["configuration"][0]["default"], "reject")

    def test_real_grouped_arithmetic_counts_missing_and_skipped_values(self):
        values = params(
            "group,value\nA,1\nA,3\nB,2\nB,4\nA,\nB,not-a-number\n",
            group_column="group",
        )
        output = process(values, {"invalid_policy": "skip"})
        result = json.loads(output["summary"])
        self.assertEqual(result["data_rows"], 6)
        self.assertEqual(
            result["overall"],
            {
                "rows": 6,
                "valid_count": 4,
                "missing_count": 1,
                "invalid_count": 1,
                "mean": "2.5",
                "minimum": "1",
                "maximum": "4",
                "population_standard_deviation": "1.11803398875",
                "sample_standard_deviation": "1.29099444874",
            },
        )
        groups = {group["value"]: group["statistics"] for group in result["groups"]}
        self.assertEqual(groups["A"]["mean"], "2")
        self.assertEqual(groups["A"]["missing_count"], 1)
        self.assertEqual(groups["B"]["mean"], "3")
        self.assertEqual(groups["B"]["invalid_count"], 1)
        self.assertNotIn("not-a-number", output["report"])
        self.assertNotIn("not-a-number", output["summary"])
        self.assertIn("样本标准差", output["report"])
        self.assertIn("n − 1", output["report"])
        self.assertIn("No external retrieval", output["report"])

    def test_decimals_do_not_acquire_binary_float_errors(self):
        stats = summary(params("value\n0.1\n0.2\n0.3\n"))["overall"]
        self.assertEqual(stats["mean"], "0.2")
        self.assertEqual(stats["population_standard_deviation"], "0.0816496580928")
        self.assertEqual(stats["sample_standard_deviation"], "0.1")

    def test_population_and_sample_stddev_distinguish_single_sample(self):
        stats = summary(params("value\n7\n"))["overall"]
        self.assertEqual(stats["population_standard_deviation"], "0")
        self.assertIsNone(stats["sample_standard_deviation"])
        self.assertEqual(stats["mean"], "7")

    def test_groups_without_samples_are_explicitly_null_not_zero(self):
        result = summary(
            params("group,value\nempty,\nvalid,2\ninvalid,NaN\n", group_column="group"),
            {"invalid_policy": "skip"},
        )
        groups = {group["value"]: group["statistics"] for group in result["groups"]}
        for label in ("empty", "invalid"):
            self.assertEqual(groups[label]["valid_count"], 0)
            self.assertIsNone(groups[label]["mean"])
            self.assertIsNone(groups[label]["population_standard_deviation"])

    def test_scientific_notation_negative_zero_and_signs(self):
        stats = summary(params("value\n-0\n+1.5e1\n.5\n1.\n"))["overall"]
        self.assertEqual(stats["mean"], "4.125")
        self.assertEqual(stats["minimum"], "0")
        self.assertEqual(stats["maximum"], "15")

    def test_small_values_are_not_silently_rounded_to_zero(self):
        stats = summary(params("value\n1e-30\n3e-30\n"))["overall"]
        self.assertEqual(stats["mean"].lower(), "2e-30")
        self.assertEqual(stats["population_standard_deviation"].lower(), "1e-30")

    def test_display_rounding_does_not_truncate_extrema_precision(self):
        value = "1.12345678901234567890123456789"
        stats = summary(params(f"value\n{value}\n"))["overall"]
        self.assertEqual(stats["mean"], "1.12345678901")
        self.assertEqual(stats["minimum"], value)

    def test_tiny_contribution_correctly_decides_large_stddev_rounding_boundary(self):
        for tiny in ["1e-30", "1.00000000000000000000000000001e-30"]:
            with self.subTest(tiny=tiny):
                values = [
                    "200000000001",
                    "-200000000001",
                    tiny,
                    "0",
                    "0",
                    "0",
                    "0",
                    "0",
                ]
                stats = summary(params("value\n" + "\n".join(values) + "\n"))["overall"]
                with localcontext() as context:
                    context.prec = 250
                    numbers = [Decimal(value) for value in values]
                    mean = sum(numbers, Decimal(0)) / len(numbers)
                    oracle = (
                        sum(((value - mean) ** 2 for value in numbers), Decimal(0))
                        / len(numbers)
                    ).sqrt()
                    self.assertGreater(oracle, Decimal("100000000000.5"))
                    context.prec = 12
                    context.rounding = ROUND_HALF_EVEN
                    rounded = +oracle
                self.assertEqual(rounded, Decimal("100000000001"))
                self.assertEqual(
                    Decimal(stats["population_standard_deviation"]), rounded
                )
        self.assertEqual(CALCULATION_PRECISION, 200)

    def test_group_strings_preserve_empty_whitespace_and_unicode(self):
        result = summary(
            params(
                '类别,数值\n,1\n" ",2\n实验甲,3\n',
                value_column="数值",
                group_column="类别",
            )
        )
        self.assertEqual(
            [group["value"] for group in result["groups"]], ["", " ", "实验甲"]
        )

    def test_unicode_headers_are_nfc_normalized_and_outer_space_trimmed(self):
        result = summary(params(" e\u0301 \n2\n4\n", value_column="é"))
        self.assertEqual(result["value_column"], "é")
        self.assertEqual(result["overall"]["mean"], "3")

    def test_utf8_bom_is_allowed_only_at_start(self):
        result = summary(params("\ufeffvalue\r\n1\r\n3\r\n"))
        self.assertEqual(result["overall"]["mean"], "2")
        self.assert_safe_error(params("value\n1\ufeff\n"), "csv_unsafe_characters")

    def test_quoted_commas_quotes_and_multiline_groups_are_parsed(self):
        values = params(
            'group,value\n"A, B",1\n"say ""hello""",3\n"multi\nline",5\n',
            group_column="group",
        )
        result = summary(values)
        self.assertEqual(
            [group["value"] for group in result["groups"]],
            ["A, B", 'say "hello"', "multi\nline"],
        )
        self.assertEqual(result["overall"]["mean"], "3")

    def test_blank_records_are_counted_and_ignored(self):
        result = summary(params("\nvalue\n\n1\n3\n\n"))
        self.assertEqual(result["ignored_blank_records"], 3)
        self.assertEqual(result["data_rows"], 2)

    def test_numeric_errors_are_rejected_by_default_without_echo(self):
        bad_values = [
            "NaN",
            "Infinity",
            "-Infinity",
            "sNaN",
            "1e999999999",
            "1e31",
            "1e-31",
            "1000000000001",
            "1e-100",
            "１２",
            "١٢",
            "0x10",
            "1,000",
            "￥12",
            "=SUM(1,2)",
            "'1",
            "--1",
            "1_000",
            "1.234567890123456789012345678901",
        ]
        for value in bad_values:
            with self.subTest(value=value):
                csv_value = json.dumps(value) if "," in value else value
                self.assert_safe_error(
                    params(f"value\n2\n{csv_value}\n"),
                    "csv_invalid_numeric_value",
                    forbidden=[value],
                )

    def test_skip_invalid_numbers_does_not_skip_structural_errors(self):
        self.assert_safe_error(
            params("value,other\n1,x\n2\n"),
            "csv_inconsistent_columns",
            {"invalid_policy": "skip"},
        )

    def test_whitespace_only_numeric_cells_are_missing(self):
        result = summary(params('value\n"   "\n2\n"\t"\n'))
        self.assertEqual(result["overall"]["valid_count"], 1)
        self.assertEqual(result["overall"]["missing_count"], 2)
        self.assertEqual(result["overall"]["invalid_count"], 0)

    def test_all_missing_and_all_invalid_data_do_not_succeed(self):
        self.assert_safe_error(
            params('value,other\n,x\n" ",y\n'), "csv_no_numeric_values"
        )
        self.assert_safe_error(
            params("value\nNaN\nInfinity\n"),
            "csv_no_numeric_values",
            {"invalid_policy": "skip"},
        )

    def test_formula_groups_are_inert_and_markdown_html_is_escaped(self):
        group = "<img src=x onerror=alert(1)>|[click](javascript:alert(1))`*#"
        values = params(
            f'group,value\n"{group}",2\n=HYPERLINK(),4\n', group_column="group"
        )
        output = process(values, {})
        self.assertNotIn("<img", output["report"])
        self.assertNotIn("[click](", output["report"])
        self.assertIn("&lt;img", output["report"])
        self.assertIn("\\|", output["report"])
        self.assertIn("\\`", output["report"])
        self.assertEqual(json.loads(output["summary"])["groups"][0]["value"], group)

    def test_header_markdown_and_html_are_escaped(self):
        header = "<script>|[x]`"
        output = process(params(f'"{header}"\n2\n', value_column=header), {})
        self.assertNotIn("<script>", output["report"])
        self.assertIn("&lt;script&gt;", output["report"])
        self.assertIn("\\|", output["report"])

    def test_gfm_strikethrough_in_header_and_group_is_literal_text(self):
        label = "~~owned~~"
        output = process(
            params(f'"{label}",value\n"{label}",2\n', group_column=label), {}
        )
        self.assertNotIn(label, output["report"])
        self.assertEqual(output["report"].count("\\~\\~owned\\~\\~"), 2)
        result = json.loads(output["summary"])
        self.assertEqual(result["group_column"], label)
        self.assertEqual(result["groups"][0]["value"], label)

    def test_control_surrogate_and_bidi_characters_are_rejected(self):
        for char in ["\x00", "\x1b", "\x7f", "\u202e", "\u200b", "\ud800"]:
            with self.subTest(char=repr(char)):
                self.assert_safe_error(
                    params(f"value\n1{char}\n"), "csv_unsafe_characters"
                )

    def test_malformed_quoting_is_rejected(self):
        for content in [
            'value\n"1\n',
            'value\n1"2\n',
            'value\n"1"extra\n',
            'value\n "1"\n',
        ]:
            with self.subTest(content=content):
                self.assert_safe_error(params(content), "csv_invalid_structure")

    def test_duplicate_ambiguous_and_empty_headers_are_rejected(self):
        for headers in [
            "value,value",
            "value,VALUE",
            "value, value ",
            "value,ｖａｌｕｅ",
            "é,e\u0301",
            "ß,ss",
        ]:
            with self.subTest(headers=headers):
                self.assert_safe_error(
                    params(f"{headers}\n1,2\n"), "csv_duplicate_headers"
                )
        self.assert_safe_error(params("value,\n1,2\n"), "csv_invalid_header")
        self.assert_safe_error(
            params(f"{'x' * 101}\n1\n", value_column="x" * 101), "csv_invalid_header"
        )

    def test_selector_must_identify_exact_header(self):
        self.assert_safe_error(
            params(value_column="VALUE"), "csv_unknown_column", forbidden=["VALUE"]
        )
        self.assert_safe_error(
            params(group_column="not-present-secret"),
            "csv_unknown_column",
            forbidden=["not-present-secret"],
        )

    def test_empty_and_header_only_inputs_fail(self):
        self.assert_safe_error(params(""), "required_field")
        self.assert_safe_error(params("\ufeff\n"), "csv_empty_input")
        self.assert_safe_error(params("value\n"), "csv_no_data_rows")

    def test_row_column_group_and_input_limits(self):
        self.assertEqual(
            summary(params("value\n" + "1\n" * MAX_DATA_ROWS))["data_rows"],
            MAX_DATA_ROWS,
        )
        self.assert_safe_error(
            params("value\n" + "1\n" * (MAX_DATA_ROWS + 1)), "csv_too_many_rows"
        )
        headers = [f"v{i}" for i in range(MAX_COLUMNS + 1)]
        self.assert_safe_error(
            params(
                ",".join(headers) + "\n" + ",".join("1" for _ in headers) + "\n",
                value_column="v0",
            ),
            "csv_too_many_columns",
        )
        groups = "group,value\n" + "".join(f"g{i},1\n" for i in range(MAX_GROUPS + 1))
        self.assert_safe_error(
            params(groups, group_column="group"), "csv_too_many_groups"
        )
        self.assert_safe_error(
            params("group,value\n" + "g" * 201 + ",1\n", group_column="group"),
            "csv_group_value_too_long",
        )
        self.assert_safe_error(params("value\n" + "1" * 10001), "value_too_long")
        self.assert_safe_error(
            params("value\n" + "\n" * 1002 + "1\n"), "csv_too_many_records"
        )

    def test_non_string_or_unknown_parameters_are_rejected(self):
        for values in [
            None,
            [],
            {"csv_text": 123},
            {"value_column": False},
            {**params(), "script": "not-allowed"},
        ]:
            with self.subTest(values=values), self.assertRaises(ProcessorError):
                validate_parameters(values)

    def test_policy_default_choices_and_incomplete_drafts(self):
        self.assertEqual(validate_configuration({}), {"invalid_policy": "reject"})
        self.assertEqual(
            validate_configuration({"invalid_policy": " skip "}),
            {"invalid_policy": "skip"},
        )
        self.assertEqual(
            validate_configuration({"invalid_policy": ""}, allow_incomplete=True),
            {"invalid_policy": "reject"},
        )
        for settings in [
            None,
            [],
            {"invalid_policy": 1},
            {"invalid_policy": "exec"},
            {"invalid_policy": ""},
            {"secret": "do-not-echo"},
        ]:
            with self.subTest(settings=settings):
                with self.assertRaises(ProcessorError) as caught:
                    validate_configuration(settings)
                self.assertNotIn("do-not-echo", str(caught.exception))

    def test_offline_processing_has_no_environment_file_network_or_execution_access(
        self,
    ):
        secret = "PRIVATE-ENVIRONMENT-MUST-NOT-BE-READ"
        values = params(
            "value,irrelevant\n1,irrelevant-private-text\n3,another-private-text\n"
        )
        with (
            patch.dict(os.environ, {"SECRET": secret}),
            patch("os.getenv", side_effect=AssertionError("environment read")),
            patch("builtins.open", side_effect=AssertionError("file read")),
            patch("socket.socket", side_effect=AssertionError("network access")),
            patch("subprocess.run", side_effect=AssertionError("program execution")),
            patch("builtins.eval", side_effect=AssertionError("evaluation")),
        ):
            output = process(values, {})
        self.assertEqual(json.loads(output["summary"])["overall"]["mean"], "2")
        for text in [secret, "irrelevant-private-text", "another-private-text"]:
            self.assertNotIn(text, json.dumps(output))

    def test_cli_jsonl_produces_progress_one_result_and_no_workflow_secrets(self):
        secret = "PRIVATE-CLI-ENVIRONMENT-DO-NOT-ECHO"
        request = {
            "params": params("group,value\nA,1\nA,3\n", group_column="group"),
            "configuration": {},
            "environment": {"WORKFLOW_SECRET": secret},
        }
        completed = subprocess.run(
            [sys.executable, "-m", "extore_processors", "csv_summary"],
            cwd=ROOT,
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "SECRET": secret},
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        events = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual(sum(event["kind"] == "result" for event in events), 1)
        self.assertTrue(all(event["kind"] == "progress" for event in events[:-1]))
        self.assertEqual(events[-1]["kind"], "result")
        self.assertEqual(events[-1]["state"], "succeeded")
        stats = json.loads(events[-1]["output"]["summary"])["overall"]
        self.assertEqual(stats["mean"], "2")
        self.assertEqual(stats["valid_count"], 2)
        self.assertNotIn(secret, completed.stdout + completed.stderr)

    def test_cli_numeric_failure_has_safe_error_without_input_or_secret_echo(self):
        secret = "PRIVATE-CLI-INVALID-CELL-DO-NOT-ECHO"
        completed = subprocess.run(
            [sys.executable, "-m", "extore_processors", "csv_summary"],
            cwd=ROOT,
            input=json.dumps(
                {
                    "params": params(f"value\n1\n{secret}\n"),
                    "configuration": {"invalid_policy": "reject"},
                    "environment": {"WORKFLOW_SECRET": secret},
                }
            ),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(
            json.loads(completed.stderr),
            {"error": "csv_invalid_numeric_value", "field": "csv_text"},
        )
        self.assertNotIn(secret, completed.stdout + completed.stderr)
        events = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertTrue(all(event["kind"] != "result" for event in events))


if __name__ == "__main__":
    unittest.main()
