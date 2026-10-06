import json
import subprocess
import sys
import unittest
from pathlib import Path

from extore_processors import (
    ProcessorError,
    catalog,
    get_processor,
    get_spec,
    run,
    validate_configuration,
    validate_parameters,
)

ROOT = Path(__file__).resolve().parents[1]


class CatalogTests(unittest.TestCase):
    def test_closed_registry_has_bilingual_code_defined_schemas(self):
        specs = catalog()
        self.assertEqual(
            [spec["id"] for spec in specs], ["resource_link", "personalized_text"]
        )
        for spec in specs:
            self.assertEqual(spec["schema_version"], 1)
            self.assertEqual(spec["delivery"], "content")
            self.assertTrue({"zh-CN", "en"} <= set(spec["name"]))
            for kind in ["parameters", "outputs", "configuration"]:
                for field in spec[kind]:
                    self.assertTrue({"zh-CN", "en"} <= set(field["label"]))
                    self.assertIn("collapsed", field)
                    self.assertIn("required", field)
                    self.assertIn("description", field)
            self.assertTrue(all(f["secret"] for f in spec["configuration"]))

    def test_specs_are_defensive_deep_copies(self):
        spec = get_spec("resource_link")
        spec["outputs"][0]["label"]["zh-CN"] = "changed"
        spec["configuration"].clear()
        self.assertEqual(
            get_spec("resource_link")["outputs"][0]["label"]["zh-CN"], "资源链接"
        )
        self.assertEqual(len(get_spec("resource_link")["configuration"]), 2)
        specs = catalog()
        specs.clear()
        self.assertEqual(len(catalog()), 2)

    def test_unknown_identifier_cannot_be_a_filename_or_module(self):
        for processor_id in ["../handler", "os.system", "handler.py", "", None, []]:
            with self.subTest(processor_id=processor_id):
                with self.assertRaises(ProcessorError) as error:
                    get_processor(processor_id)
                self.assertEqual(error.exception.code, "unknown_processor")

    def test_processor_helper_is_frozen_and_runs_explicit_handler(self):
        processor = get_processor("resource_link")
        self.assertEqual(processor.spec["id"], "resource_link")
        with self.assertRaises(AttributeError):
            processor.id = "other"
        self.assertEqual(
            processor.run({}, {"resource_url": "https://example.test/item"})["output"],
            {"resource_url": "https://example.test/item", "message": ""},
        )


class ResourceLinkTests(unittest.TestCase):
    def test_delivers_configured_url_without_customer_input(self):
        config = {
            "resource_url": "https://example.test/item?token=secret#instructions",
            "message": "第一步：打开链接。\n第二步：下载资源。",
        }
        result = run("resource_link", {}, config)
        self.assertEqual(result, {"status": "succeeded", "output": config})

    def test_missing_required_url_and_unknown_values_are_rejected(self):
        cases = [
            ({}, {}),
            ({"email": "a@example.test"}, {"resource_url": "https://example.test"}),
            ({}, {"resource_url": "https://example.test", "script": "evil.py"}),
        ]
        for params, configuration in cases:
            with (
                self.subTest(params=params, configuration=configuration),
                self.assertRaises(ProcessorError),
            ):
                run("resource_link", params, configuration)

    def test_url_must_be_https_without_credentials_or_ambiguous_characters(self):
        urls = [
            "http://example.test/item",
            "javascript:alert(1)",
            "file:///etc/passwd",
            "https://user:password@example.test/item",
            "https://example.test/a b",
            "https://example.test/a\nInjected: yes",
            "https://example.test\\@other.test",
            "https://",
            "https://[invalid]/item",
            "https://example.test:99999/item",
            "https://example.test:not-a-port/item",
        ]
        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(ProcessorError) as error:
                    run("resource_link", {}, {"resource_url": url})
                self.assertEqual(error.exception.code, "invalid_https_url")
                self.assertNotIn(url, str(error.exception))

    def test_no_network_or_local_file_access_is_needed(self):
        # These are not reachable. Resource delivery only returns the string.
        for url in [
            "https://localhost/private",
            "https://[::1]/item",
            "https://invalid.invalid/item",
        ]:
            with self.subTest(url=url):
                self.assertEqual(
                    run("resource_link", {}, {"resource_url": url})["output"][
                        "resource_url"
                    ],
                    url,
                )

    def test_configuration_size_and_type_limits(self):
        for configuration in [
            {"resource_url": "https://example.test/" + "x" * 2000},
            {"resource_url": 123},
            {"resource_url": "https://example.test", "message": "x" * 10001},
            [],
            None,
        ]:
            with (
                self.subTest(configuration=configuration),
                self.assertRaises(ProcessorError),
            ):
                validate_configuration("resource_link", configuration)

    def test_incomplete_draft_still_checks_supplied_values(self):
        self.assertEqual(
            validate_configuration("resource_link", {}, allow_incomplete=True),
            {"resource_url": "", "message": ""},
        )
        with self.assertRaises(ProcessorError):
            validate_configuration(
                "resource_link",
                {"resource_url": "http://example.test"},
                allow_incomplete=True,
            )


class PersonalizedTextTests(unittest.TestCase):
    def test_submit_validation_matches_runtime_customer_input_rules(self):
        self.assertEqual(
            validate_parameters("personalized_text", {"name": " Alice "}),
            {"name": "Alice"},
        )
        self.assertEqual(validate_parameters("resource_link", {}), {})
        for params in [
            {"name": "x" * 201},
            {"name": " "},
            {"name": "Alice", "script": "unexpected"},
        ]:
            with self.subTest(params=params), self.assertRaises(ProcessorError):
                validate_parameters("personalized_text", params)

    def test_customer_name_and_literal_dollar_are_plain_text(self):
        result = run(
            "personalized_text",
            {"name": "  张三  "},
            {"template": "你好，${name}！\n价格：$$5。 $name"},
        )
        self.assertEqual(result["output"], {"content": "你好，张三！\n价格：$5。 张三"})

    def test_default_template_is_declared_and_applied(self):
        field = get_spec("personalized_text")["configuration"][0]
        result = run("personalized_text", {"name": "Alice"}, {})
        self.assertEqual(
            result["output"]["content"], field["default"].replace("$name", "Alice")
        )

    def test_inserted_name_is_not_recursively_interpreted(self):
        name = "$other ${__class__} $(touch /tmp/not-executed) <script>evil()</script>"
        self.assertEqual(
            run("personalized_text", {"name": name}, {"template": "$name"})["output"],
            {"content": name},
        )

    def test_no_attribute_access_unknown_names_or_invalid_placeholders(self):
        for template in [
            "${__class__}",
            "$password",
            "${name.__class__}",
            "${",
            "$",
            "$()",
        ]:
            with self.subTest(template=template), self.assertRaises(ProcessorError):
                run("personalized_text", {"name": "Alice"}, {"template": template})

    def test_missing_name_non_string_or_long_name_is_rejected(self):
        for params in [
            {},
            {"name": " "},
            {"name": None},
            {"name": "x" * 201},
            {"name": "Alice", "other": "unexpected"},
        ]:
            with self.subTest(params=params), self.assertRaises(ProcessorError):
                run("personalized_text", params, {"template": "$name"})

    def test_template_length_and_output_expansion_are_bounded(self):
        for template in ["x" * 10001, "$name" * 1000]:
            with self.subTest(template=template[:20]):
                with self.assertRaises(ProcessorError) as error:
                    run(
                        "personalized_text", {"name": "x" * 200}, {"template": template}
                    )
                self.assertEqual(error.exception.code, "value_too_long")

    def test_whitespace_result_cannot_count_as_delivery(self):
        with self.assertRaises(ProcessorError):
            run("personalized_text", {"name": "Alice"}, {"template": " \n "})

    def test_configuration_and_inputs_are_not_mutated(self):
        params = {"name": " Alice "}
        configuration = {"template": "$name"}
        run("personalized_text", params, configuration)
        self.assertEqual(params, {"name": " Alice "})
        self.assertEqual(configuration, {"template": "$name"})


class CliTests(unittest.TestCase):
    def call(self, value, processor_id="resource_link"):
        raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        return subprocess.run(
            [sys.executable, "-m", "extore_processors", processor_id],
            input=raw,
            capture_output=True,
            cwd=ROOT,
            check=False,
        )

    def test_one_line_success_uses_worker_protocol(self):
        result = self.call(
            {
                "params": {},
                "configuration": {"resource_url": "https://example.test/item"},
            }
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, b"")
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "kind": "result",
                "state": "succeeded",
                "output": {"resource_url": "https://example.test/item", "message": ""},
            },
        )

    def test_failure_does_not_echo_secrets_or_emit_success(self):
        secret = "http://user:secret-password@example.test/private-secret"
        result = self.call({"params": {}, "configuration": {"resource_url": secret}})
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(
            json.loads(result.stderr),
            {"error": "invalid_https_url", "field": "resource_url"},
        )
        self.assertNotIn(secret.encode(), result.stderr)
        self.assertNotIn(b"secret-password", result.stderr)

    def test_envelope_and_json_are_strict(self):
        for value in [
            b"not json",
            b"\xff",
            {},
            [],
            {"params": {}, "configuration": {}, "id": "extra"},
            {"params": {}},
        ]:
            with self.subTest(value=value):
                result = self.call(value)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, b"")
                self.assertIn("error", json.loads(result.stderr))

    def test_oversized_and_deeply_nested_input_are_safe_failures(self):
        for raw in [b" " * 200001, b"[" * 2000 + b"]" * 2000]:
            with self.subTest(size=len(raw)):
                result = self.call(raw)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, b"")
                self.assertIn("error", json.loads(result.stderr))


if __name__ == "__main__":
    unittest.main()
