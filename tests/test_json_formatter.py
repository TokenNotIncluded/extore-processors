import builtins
import json
import socket
import subprocess
import sys
import unittest
from decimal import Decimal, localcontext
from pathlib import Path
from unittest.mock import patch

from extore_processors.json_formatter import (
    SPEC,
    process,
    validate_configuration,
    validate_parameters,
)
from extore_processors.schema import ProcessorError

ROOT = Path(__file__).resolve().parents[1]


def format_json(text, **settings):
    return process({"json_text": text}, settings)


class JsonFormatterTests(unittest.TestCase):
    def test_schemas_are_bilingual_and_settings_are_not_secrets(self):
        self.assertEqual(SPEC["id"], "json_formatter")
        self.assertEqual(SPEC["schema_version"], 1)
        for kind in ("parameters", "outputs", "configuration"):
            for value in SPEC[kind]:
                self.assertEqual(set(value["label"]), {"zh-CN", "en"})
                self.assertEqual(set(value["description"]), {"zh-CN", "en"})
                self.assertTrue(all(value["description"].values()))
        self.assertTrue(
            all(value["secret"] is False for value in SPEC["configuration"])
        )
        self.assertEqual(SPEC["parameters"][0]["key"], "json_text")
        self.assertNotIn("max_length", SPEC["parameters"][0])

    def test_defaults_and_explicit_configuration(self):
        self.assertEqual(validate_configuration({}), {"indent": "2", "sort_keys": "no"})
        self.assertEqual(
            validate_configuration({"indent": "compact", "sort_keys": "yes"}),
            {"indent": "compact", "sort_keys": "yes"},
        )
        self.assertEqual(
            validate_configuration({"indent": ""}, allow_incomplete=True),
            {"indent": "", "sort_keys": "no"},
        )
        for settings in (
            [],
            None,
            {"indent": 2},
            {"sort_keys": True},
            {"indent": "8"},
            {"sort_keys": "true"},
            {"unknown": "private-setting"},
        ):
            with self.subTest(settings=settings), self.assertRaises(ProcessorError):
                validate_configuration(settings, allow_incomplete=True)
        with self.assertRaises(ProcessorError):
            validate_configuration({"indent": ""})

    def test_formatted_structure_and_counts(self):
        result = format_json('{"b":[1,true,null],"a":{"empty":[]}}')
        self.assertEqual(
            result["formatted_json"],
            '{\n  "b": [\n    1,\n    true,\n    null\n  ],\n  "a": {\n    "empty": []\n  }\n}',
        )
        self.assertIn("Type: object", result["report"])
        self.assertIn("Values: 7", result["report"])
        self.assertIn("Object keys: 3", result["report"])
        self.assertIn("Array items: 3", result["report"])
        self.assertIn("Container depth: 3", result["report"])

    def test_sorting_is_recursive_and_arrays_keep_order(self):
        result = format_json(
            '{"z":[{"b":2,"a":1},3,2,1],"a":0}', indent="compact", sort_keys="yes"
        )
        self.assertEqual(result["formatted_json"], '{"a":0,"z":[{"a":1,"b":2},3,2,1]}')

    def test_four_space_indent_and_empty_containers(self):
        self.assertEqual(
            format_json('{"a":[],"b":{}}', indent="4")["formatted_json"],
            '{\n    "a": [],\n    "b": {}\n}',
        )

    def test_every_json_top_level_type_is_supported(self):
        cases = {
            "{}": "object",
            "[]": "array",
            '"text"': "string",
            "-0": "number",
            "true": "boolean",
            "false": "boolean",
            "null": "null",
        }
        for text, kind in cases.items():
            with self.subTest(text=text):
                result = format_json(text, indent="compact")
                self.assertEqual(result["formatted_json"], text)
                self.assertIn(f"Type: {kind}", result["report"])
                self.assertIn("Values: 1", result["report"])

    def test_decimal_precision_and_number_tokens_are_preserved(self):
        tokens = [
            "9007199254740993",
            "1234567890123456789012345678901234567890",
            "0.1234567890123456789012345678901234567890",
            "1e+3",
            "1E-003",
            "-0",
            "-0.000e+000",
            "1.2300",
            "1e-1000",
            "1e1000",
            "1" * 128,
        ]
        text = "[" + ",".join(tokens) + "]"
        result = format_json(text, indent="compact")["formatted_json"]
        self.assertEqual(result, text)
        self.assertEqual(
            json.loads(result, parse_int=Decimal, parse_float=Decimal),
            [Decimal(token) for token in tokens],
        )

    def test_decimal_context_cannot_round_input_numbers(self):
        text = "[123456789012345678901234567890,0.123456789012345678901234567890]"
        with localcontext() as context:
            context.prec = 2
            self.assertEqual(
                format_json(text, indent="compact")["formatted_json"], text
            )

    def test_strings_preserve_content_not_only_ascii(self):
        value = {
            "标题": '你好 👋\n/\\"',
            "literal": "${name}; __import__('os')",
            "controls": "\x00\t\r\b\f",
            "url": "https://example.test/private",
        }
        result = format_json(json.dumps(value, ensure_ascii=False))
        self.assertEqual(json.loads(result["formatted_json"]), value)
        self.assertNotIn("private", result["report"])
        self.assertNotIn("标题", result["report"])

    def test_valid_surrogate_pair_becomes_valid_utf8(self):
        result = format_json('{"emoji":"\\ud83d\\ude00"}')
        self.assertEqual(json.loads(result["formatted_json"]), {"emoji": "😀"})
        result["formatted_json"].encode("utf-8")

    def test_lone_surrogates_are_rejected_in_keys_values_and_raw_input(self):
        for text in (
            '"\\ud800"',
            '"\\udfff"',
            '{"\\ud800":1}',
            '"\\ud800x"',
            '"\\udc00\\ud800"',
            '"\ud800"',
        ):
            with (
                self.subTest(text=ascii(text)),
                self.assertRaises(ProcessorError) as error,
            ):
                format_json(text)
            self.assertEqual(error.exception.code, "invalid_json_unicode")

    def test_duplicate_decoded_keys_are_rejected_at_every_level(self):
        for text in (
            '{"a":1,"a":2}',
            '{"a":1,"\\u0061":2}',
            '{"😀":1,"\\ud83d\\ude00":2}',
            '[{"safe":{"private-password":1,"private-password":2}}]',
        ):
            with self.subTest(text=text), self.assertRaises(ProcessorError) as error:
                format_json(text)
            self.assertEqual(error.exception.code, "json_duplicate_key")
            self.assertNotIn("private-password", str(error.exception))

    def test_nonstandard_numbers_and_invalid_json_are_rejected_without_excerpts(self):
        cases = [
            "NaN",
            "Infinity",
            "-Infinity",
            '{"private": NaN}',
            "01",
            "+1",
            "1.",
            ".1",
            "1e",
            "[1,]",
            '{"a":1,}',
            '{"a":}',
            "true false",
            '//private-token\n{"a":1}',
            "{private-password:1}",
            "\ufeff{}",
        ]
        for text in cases:
            with self.subTest(text=text), self.assertRaises(ProcessorError) as error:
                format_json(text)
            self.assertEqual(error.exception.field, "json_text")
            self.assertNotIn("private", str(error.exception))

    def test_only_ascii_json_numbers_and_standard_json_whitespace_are_accepted(self):
        for text in ("١", "１", "1e٢", "1.٣", "\u00a0{}", "{}\u2003", "\u0085null"):
            with self.subTest(text=text), self.assertRaises(ProcessorError) as error:
                format_json(text)
            self.assertEqual(error.exception.code, "invalid_json")

    def test_bounded_numbers_reject_oversized_coefficients_and_exponents(self):
        cases = [
            "1" * 129,
            "0." + "1" * 129,
            "1" * 257,
            "1e1001",
            "1e-1001",
            "1e" + "9" * 250,
            "0e999999",
            "0." + "0" * 128 + "1e-1000",
        ]
        for text in cases:
            with self.subTest(text=text), self.assertRaises(ProcessorError) as error:
                format_json(text)
            self.assertIn(
                error.exception.code,
                {
                    "json_number_too_precise",
                    "json_number_too_long",
                    "json_number_out_of_range",
                },
            )

    def test_nesting_limit_and_brackets_inside_strings(self):
        deepest = "[" * 32 + "0" + "]" * 32
        self.assertEqual(
            format_json(deepest, indent="compact")["formatted_json"], deepest
        )
        with self.assertRaises(ProcessorError) as error:
            format_json("[" * 33 + "0" + "]" * 33)
        self.assertEqual(error.exception.code, "json_too_deep")
        text = json.dumps("[" * 200 + '\\"' + "]" * 200)
        self.assertEqual(
            json.loads(format_json(text)["formatted_json"]), json.loads(text)
        )

    def test_per_container_limit_accepts_boundary_and_rejects_more(self):
        text = "[" + ",".join(["0"] * 2000) + "]"
        self.assertEqual(format_json(text, indent="compact")["formatted_json"], text)
        with self.assertRaises(ProcessorError) as error:
            format_json("[" + ",".join(["0"] * 2001) + "]")
        self.assertEqual(error.exception.code, "json_too_many_items")
        with (
            patch("extore_processors.json_formatter.MAX_CONTAINER_ITEMS", 1),
            self.assertRaises(ProcessorError) as error,
        ):
            format_json('{"first":0,"second":0}')
        self.assertEqual(error.exception.code, "json_too_many_items")

    def test_total_node_limit_is_independent_of_container_limit(self):
        with (
            patch("extore_processors.json_formatter.MAX_NODES", 4),
            self.assertRaises(ProcessorError) as error,
        ):
            format_json("[[0],[1]]")
        self.assertEqual(error.exception.code, "json_too_many_values")

    def test_character_and_utf8_input_limits(self):
        self.assertEqual(
            json.loads(format_json('"' + "汉" * 9998 + '"')["formatted_json"]),
            "汉" * 9998,
        )
        for text in ('"' + "a" * 9999 + '"', '"' + "😀" * 9999 + '"'):
            with (
                self.subTest(size=len(text)),
                self.assertRaises(ProcessorError) as error,
            ):
                format_json(text)
            self.assertEqual(error.exception.code, "value_too_long")
        with (
            patch("extore_processors.json_formatter.MAX_INPUT_BYTES", 10),
            self.assertRaises(ProcessorError) as error,
        ):
            format_json('"汉汉汉"')
        self.assertEqual(error.exception.code, "json_input_too_large")

    def test_indent_expansion_is_bounded_and_compact_remains_available(self):
        text = "[" * 32 + ",".join(["0"] * 2000) + "]" * 32
        with self.assertRaises(ProcessorError) as error:
            format_json(text, indent="4")
        self.assertEqual(error.exception.code, "json_output_too_large")
        self.assertEqual(format_json(text, indent="compact")["formatted_json"], text)

    def test_report_bytes_are_included_in_delivery_limit(self):
        result = format_json('"汉字"', indent="compact")
        size = sum(len(value.encode("utf-8")) for value in result.values())
        with patch("extore_processors.json_formatter.MAX_OUTPUT_BYTES", size):
            self.assertEqual(format_json('"汉字"', indent="compact"), result)
        with (
            patch("extore_processors.json_formatter.MAX_OUTPUT_BYTES", size - 1),
            self.assertRaises(ProcessorError) as error,
        ):
            format_json('"汉字"', indent="compact")
        self.assertEqual(error.exception.code, "json_output_too_large")

    def test_report_uses_real_utf8_lengths_without_content(self):
        text = '{"secret-source":"👋汉"}'
        result = format_json(text, indent="compact")
        self.assertIn(
            f"Input UTF-8 bytes: {len(text.encode('utf-8'))}", result["report"]
        )
        self.assertIn(
            f"JSON output UTF-8 bytes: {len(result['formatted_json'].encode('utf-8'))}",
            result["report"],
        )
        self.assertNotIn("secret-source", result["report"])
        self.assertNotIn("👋汉", result["report"])

    def test_submit_validation_and_runtime_validation_match(self):
        params = {"json_text": ' {"a":1} '}
        self.assertEqual(validate_parameters(params), params)
        for params in (
            [],
            None,
            {},
            {"json_text": 1},
            {"json_text": " "},
            {"json_text": "{}", "extra": "private"},
        ):
            with self.subTest(params=params):
                with self.assertRaises(ProcessorError):
                    validate_parameters(params)
                with self.assertRaises(ProcessorError):
                    process(params, {})

    def test_no_files_network_or_subprocesses_are_used(self):
        with (
            patch.object(builtins, "open", side_effect=AssertionError("file access")),
            patch.object(
                socket, "socket", side_effect=AssertionError("network access")
            ),
            patch.object(
                subprocess, "Popen", side_effect=AssertionError("command access")
            ),
        ):
            result = format_json(
                '{"command":"cat /etc/passwd","url":"https://example.test"}'
            )
        self.assertEqual(
            json.loads(result["formatted_json"])["command"], "cat /etc/passwd"
        )


class JsonFormatterCliTests(unittest.TestCase):
    def test_successful_cli_does_not_copy_shop_environment_into_delivery(self):
        payload = {
            "params": {"json_text": '{"safe":"ordinary data"}'},
            "configuration": {},
            "environment": {"PRIVATE_KEY": "private-shop-credential"},
        }
        result = subprocess.run(
            [sys.executable, "-m", "extore_processors", "json_formatter"],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            cwd=ROOT,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("PRIVATE_KEY", result.stdout)
        self.assertNotIn("private-shop-credential", result.stdout + result.stderr)
        output = json.loads(result.stdout.splitlines()[-1])["output"]
        self.assertEqual(
            json.loads(output["formatted_json"]), {"safe": "ordinary data"}
        )

    def test_cli_error_does_not_expose_customer_input_or_environment(self):
        payload = {
            "params": {"json_text": '{"private-customer-data":NaN}'},
            "configuration": {"indent": "2", "sort_keys": "no"},
            "environment": {"PRIVATE_KEY": "private-shop-credential"},
        }
        result = subprocess.run(
            [sys.executable, "-m", "extore_processors", "json_formatter"],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            cwd=ROOT,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(
            json.loads(result.stderr),
            {"error": "invalid_json_number", "field": "json_text"},
        )
        self.assertNotIn("private-customer-data", result.stderr)
        self.assertNotIn("private-shop-credential", result.stderr)

    def test_cli_progress_then_exact_result_without_binary_float_loss(self):
        payload = {
            "params": {"json_text": '{"precise":9007199254740993,"exp":1e+3}'},
            "configuration": {"indent": "compact", "sort_keys": "no"},
        }
        result = subprocess.run(
            [sys.executable, "-m", "extore_processors", "json_formatter"],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            cwd=ROOT,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(sum(event.get("kind") == "result" for event in events), 1)
        self.assertEqual(events[-1]["kind"], "result")
        self.assertEqual(events[-1]["state"], "succeeded")
        self.assertTrue(any(event.get("kind") == "progress" for event in events[:-1]))
        self.assertEqual(
            events[-1]["output"]["formatted_json"], payload["params"]["json_text"]
        )
        self.assertTrue(all(event.get("progress", 0) < 100 for event in events[:-1]))


if __name__ == "__main__":
    unittest.main()
