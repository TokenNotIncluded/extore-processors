import json
import subprocess
import sys
import unittest
from pathlib import Path

from extore_processors import (
    ProcessorContext,
    ProcessorError,
    ShopContext,
    catalog,
    get_processor,
    get_spec,
    run,
    validate_configuration,
    validate_parameters,
)
from extore_processors.catalog import _configuration

ROOT = Path(__file__).resolve().parents[1]


class CatalogTests(unittest.TestCase):
    def test_closed_registry_has_bilingual_code_defined_schemas(self):
        specs = catalog()
        self.assertEqual(
            [spec["id"] for spec in specs],
            [
                "resource_link",
                "personalized_text",
                "csv_summary",
                "json_formatter",
                "text_cleanup",
                "document_template",
            ],
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
            self.assertTrue(
                all(type(f["secret"]) is bool for f in spec["configuration"])
            )

    def test_configuration_is_secret_unless_explicitly_marked_public(self):
        field = get_spec("resource_link")["outputs"][0]
        self.assertIs(_configuration(field)["secret"], True)
        self.assertIs(_configuration(field, secret=False)["secret"], False)
        for flag in [True, None, 0, "", "false", [], {}]:
            with self.subTest(flag=flag):
                self.assertIs(_configuration(field, secret=flag)["secret"], True)
        self.assertNotIn("secret", field)

    def test_shop_editor_can_edit_text_but_resource_url_remains_secret(self):
        expected = {
            "resource_link": {
                "resource_url": (True, "url"),
                "message": (False, "textarea"),
            },
            "personalized_text": {"template": (False, "textarea")},
        }
        for processor_id, fields in expected.items():
            with self.subTest(processor_id=processor_id):
                spec = get_spec(processor_id)
                self.assertEqual(
                    {
                        field["key"]: (field["secret"], field["type"])
                        for field in spec["configuration"]
                    },
                    fields,
                )
                self.assertEqual(spec["configuration"], spec["shop_configuration"])

    def test_configuration_classification_does_not_change_customer_schemas(self):
        expected = {
            "resource_link": {
                "parameters": [],
                "outputs": [
                    ("resource_url", "url", True),
                    ("message", "textarea", False),
                ],
            },
            "personalized_text": {
                "parameters": [("name", "text", True)],
                "outputs": [("content", "textarea", True)],
            },
        }
        for processor_id, schemas in expected.items():
            spec = get_spec(processor_id)
            for kind, fields in schemas.items():
                with self.subTest(processor_id=processor_id, kind=kind):
                    self.assertEqual(
                        [
                            (field["key"], field["type"], field["required"])
                            for field in spec[kind]
                        ],
                        fields,
                    )
                    for field in spec[kind]:
                        self.assertTrue(
                            {"secret", "default", "max_length"}.isdisjoint(field)
                        )

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
        self.assertEqual(len(catalog()), 6)

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


class ProcessorContextTests(unittest.TestCase):
    def test_environment_is_copied_readonly_and_not_inserted_into_result_or_progress(
        self,
    ):
        environment = {"NAME": "private-workflow-value", "API_TOKEN": "private-token"}
        values = []
        context = ProcessorContext(environment=environment, emit=values.append)
        environment["API_TOKEN"] = "changed-after-construction"
        self.assertEqual(context.environment["API_TOKEN"], "private-token")
        with self.assertRaises(TypeError):
            context.environment["API_TOKEN"] = "replaced"
        with self.assertRaises(AttributeError):
            context.environment = {}
        result = run(
            "personalized_text",
            {"name": "Alice"},
            {"template": "Hello $name"},
            context=context,
        )
        self.assertEqual(result["output"], {"content": "Hello Alice"})
        for value in [json.dumps(values), json.dumps(result), repr(context)]:
            self.assertNotIn("private-workflow-value", value)
            self.assertNotIn("private-token", value)
        self.assertEqual(ProcessorContext().environment, {})

    def test_environment_validation_is_bounded_and_errors_hide_secret_values(self):
        for environment in [
            None,
            [],
            {"lowercase": "private-environment-value"},
            {"BAD-NAME": "private-environment-value"},
            {"A" * 65: "private-environment-value"},
            {"API_TOKEN": None},
            {"API_TOKEN": {"value": "private-environment-value"}},
            {"API_TOKEN": "private-environment-value\x00"},
            {"API_TOKEN": "private-environment-value\ud800"},
            {"API_TOKEN": "x" * 8193},
            {"API_TOKEN": "文" * 2731},
            {f"VALUE_{i}": "" for i in range(129)},
            {f"VALUE_{i}": "x" * 8192 for i in range(9)},
        ]:
            with self.subTest(environment=environment):
                with self.assertRaises(ProcessorError) as error:
                    ProcessorContext(environment=environment)
                self.assertNotIn("private-environment-value", str(error.exception))
        environment = {f"VALUE_{i}": "" for i in range(128)}
        environment.update({f"VALUE_{i}": "x" * 8192 for i in range(8)})
        self.assertEqual(
            len(ProcessorContext(environment=environment).environment), 128
        )

    def test_code_declares_shop_configuration_and_default_step_plan(self):
        spec = get_spec("resource_link")
        self.assertEqual(spec["configuration"], spec["shop_configuration"])
        spec["shop_configuration"][0]["label"]["en"] = "Changed"
        self.assertNotEqual(spec["configuration"][0]["label"]["en"], "Changed")
        self.assertEqual(
            [step["id"] for step in spec["progress_steps"]],
            ["validate_input", "prepare_delivery"],
        )

    def test_empty_context_initializes_plan_and_emits_real_step_completion(self):
        values = []
        context = ProcessorContext(emit=values.append)
        result = run(
            "personalized_text",
            {"name": "Alice"},
            {"template": "Hello $name"},
            context=context,
        )
        self.assertEqual(result["output"], {"content": "Hello Alice"})
        self.assertEqual(
            values[0]["progress_steps"], get_spec("personalized_text")["progress_steps"]
        )
        self.assertEqual(values[1]["completed_steps"], ["validate_input"])
        self.assertEqual(
            values[2]["completed_steps"], ["validate_input", "prepare_delivery"]
        )
        self.assertEqual(
            context.completed_steps, ("validate_input", "prepare_delivery")
        )

    def test_existing_plan_is_frozen_and_unknown_steps_are_not_falsely_completed(self):
        plan = [{"id": "merchant_review", "label": {"en": "Merchant review"}}]
        values = []
        context = ProcessorContext(steps=plan, emit=values.append)
        run(
            "resource_link",
            {},
            {"resource_url": "https://example.test/item"},
            context=context,
        )
        self.assertTrue(
            all(
                "progress_steps" not in value and "completed_steps" not in value
                for value in values
            )
        )
        self.assertEqual([step["id"] for step in context.steps], ["merchant_review"])
        self.assertEqual(context.completed_steps, ())
        with self.assertRaises(ProcessorError) as error:
            context.define_steps(plan)
        self.assertEqual(error.exception.code, "progress_plan_frozen")

    def test_same_code_plan_can_resume_without_regressing_completed_ids(self):
        plan = get_spec("resource_link")["progress_steps"]
        values = []
        context = ProcessorContext(
            steps=plan,
            completed_steps=["validate_input", "prepare_delivery"],
            emit=values.append,
        )
        run(
            "resource_link",
            {},
            {"resource_url": "https://example.test/item"},
            context=context,
        )
        self.assertEqual(len(values), 2)
        self.assertTrue(
            all(
                value["completed_steps"] == ["validate_input", "prepare_delivery"]
                for value in values
            )
        )
        self.assertTrue(all("progress_steps" not in value for value in values))

    def test_context_freezes_metadata_and_keeps_private_values_out_of_progress(self):
        values = []
        context = ProcessorContext(
            shop_context={
                "shop_id": "shop-a",
                "profile_id": "profile-a",
                "revision": 3,
            },
            emit=values.append,
        )
        self.assertEqual(context.shop_context, ShopContext("shop-a", "profile-a", 3))
        with self.assertRaises(AttributeError):
            context.shop_context.shop_id = "shop-b"
        with self.assertRaises(AttributeError):
            context.shop_context = ShopContext("shop-b")
        run(
            "personalized_text",
            {"name": "private-customer"},
            {"template": "private-template: $name"},
            context=context,
        )
        self.assertNotIn("private-customer", json.dumps(values))
        self.assertNotIn("private-template", json.dumps(values))
        self.assertNotIn("profile-a", json.dumps(values))

    def test_progress_callback_cannot_mutate_saved_plan(self):
        values = []
        context = ProcessorContext(emit=values.append)
        context.define_steps(get_spec("resource_link")["progress_steps"])
        values[0]["progress_steps"][0]["label"]["en"] = "Modified output payload"
        self.assertEqual(context.steps[0]["label"]["en"], "Validate inputs")

    def test_invalid_plans_fail_before_any_progress_output(self):
        cases = [
            [],
            [{"id": "Bad ID", "label": {"en": "Step"}}],
            [{"id": "valid", "label": {}}],
            [{"id": "valid", "label": {"en": " "}}],
            [{"id": "same", "label": {"en": "Step"}}] * 2,
            [{"id": str(i), "label": {"en": "Step"}} for i in range(31)],
        ]
        for plan in cases:
            with self.subTest(plan=plan):
                values = []
                context = ProcessorContext(emit=values.append)
                with self.assertRaises(ProcessorError):
                    context.define_steps(plan)
                self.assertEqual(values, [])
                self.assertEqual(context.steps, ())

    def test_completed_ids_and_percentages_are_validated_without_regression(self):
        values = []
        context = ProcessorContext(
            steps=get_spec("resource_link")["progress_steps"],
            completed_steps=["validate_input"],
            emit=values.append,
        )
        for completed in [
            [],
            ["unknown"],
            ["validate_input", "validate_input"],
            ["Bad ID"],
        ]:
            with self.subTest(completed=completed), self.assertRaises(ProcessorError):
                context.progress(completed_steps=completed)
        for percent in [-1, 100, 0.5, True, "50"]:
            with self.subTest(percent=percent), self.assertRaises(ProcessorError):
                context.progress(percent)
        self.assertEqual(values, [])
        self.assertEqual(context.completed_steps, ("validate_input",))


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

    def test_progress_plan_and_success_use_worker_jsonl_protocol(self):
        result = self.call(
            {
                "params": {},
                "configuration": {"resource_url": "https://example.test/item"},
            }
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, b"")
        values = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(values), 4)
        self.assertEqual(values[0]["kind"], "progress")
        self.assertEqual(
            [step["id"] for step in values[0]["progress_steps"]],
            ["validate_input", "prepare_delivery"],
        )
        self.assertEqual(values[0]["completed_steps"], [])
        self.assertEqual(values[1]["completed_steps"], ["validate_input"])
        self.assertEqual(
            values[2]["completed_steps"], ["validate_input", "prepare_delivery"]
        )
        self.assertEqual(
            values[3],
            {
                "kind": "result",
                "state": "succeeded",
                "output": {"resource_url": "https://example.test/item", "message": ""},
            },
        )

    def test_environment_envelope_does_not_override_customer_name_or_copy_secrets(self):
        result = self.call(
            {
                "params": {"name": "Alice"},
                "configuration": {"template": "Hello $name"},
                "environment": {
                    "NAME": "private-env-name",
                    "API_TOKEN": "private-env-token",
                },
            },
            "personalized_text",
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, b"")
        values = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(values[-1]["output"], {"content": "Hello Alice"})
        self.assertNotIn(b"private-env", result.stdout)

    def test_invalid_environment_fails_before_output_without_echoing_values(self):
        for environment in [
            None,
            [],
            {"lowercase": "private-env-value"},
            {"API_TOKEN": "private-env-value\x00"},
            {"API_TOKEN": {"nested": "private-env-value"}},
            {"API_TOKEN": "private-env-value\ud800"},
        ]:
            with self.subTest(environment=environment):
                result = self.call(
                    {
                        "params": {},
                        "configuration": {"resource_url": "https://example.test/item"},
                        "environment": environment,
                    }
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, b"")
                self.assertEqual(
                    json.loads(result.stderr), {"error": "invalid_environment"}
                )
                self.assertNotIn(b"private-env-value", result.stderr)

    def test_total_input_budget_includes_other_fields_and_valid_environment(self):
        result = self.call(
            {
                "params": {"name": "Alice"},
                "configuration": {"template": "x" * 143000},
                "environment": {f"VALUE_{i}": "s" * 8192 for i in range(8)},
            },
            "personalized_text",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(json.loads(result.stderr), {"error": "input_too_large"})

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

    def test_server_variant_metadata_is_optional_and_does_not_override_inputs(self):
        result = self.call(
            {
                "params": {"name": "Alice"},
                "configuration": {"template": "Hello $name"},
                "variant": {
                    "id": "plus",
                    "name": "Plus",
                    "attributes": {"name": "Mallory"},
                },
                "steps": [{"id": "prepare", "label": {"en": "Prepare"}, "done": False}],
                "completed_steps": [],
            },
            "personalized_text",
        )
        self.assertEqual(result.returncode, 0)
        values = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(values[-1]["output"]["content"], "Hello Alice")
        self.assertTrue(all("progress_steps" not in value for value in values))
        self.assertTrue(all("completed_steps" not in value for value in values))

    def test_envelope_and_json_are_strict(self):
        for value in [
            b"not json",
            b"\xff",
            {},
            [],
            {"params": {}, "configuration": {}, "id": "extra"},
            {"params": {}},
            {"params": {}, "configuration": {}, "variant": []},
            {"params": {}, "configuration": {}, "variant": None},
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

    def test_frozen_plan_and_shop_context_validation_fail_without_leaking_values(self):
        base = {
            "params": {},
            "configuration": {"resource_url": "https://example.test/private-secret"},
        }
        for extra in [
            {"steps": [{"id": "invalid private-secret", "label": {"en": "Step"}}]},
            {"steps": [], "completed_steps": ["private-secret"]},
            {"shop_context": {"shop_id": "shop-a", "private-secret": "secret-value"}},
            {"shop_context": {"shop_id": "shop-a", "revision": True}},
            {"shop_context": "private-secret"},
        ]:
            with self.subTest(extra=extra):
                result = self.call({**base, **extra})
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, b"")
                self.assertNotIn(b"private-secret", result.stderr)
                self.assertNotIn(b"secret-value", result.stderr)

    def test_subprocess_progress_is_jsonl_flushed_before_single_result(self):
        process = subprocess.Popen(
            [sys.executable, "-m", "extore_processors", "personalized_text"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=ROOT,
        )
        try:
            process.stdin.write(
                json.dumps(
                    {
                        "params": {"name": "Alice"},
                        "configuration": {"template": "Hello $name"},
                        "shop_context": {
                            "shop_id": "shop-a",
                            "profile_id": None,
                            "revision": None,
                        },
                    }
                ).encode()
            )
            process.stdin.close()
            first = json.loads(process.stdout.readline())
            self.assertEqual(first["kind"], "progress")
            self.assertIn("progress_steps", first)
            remaining = [json.loads(line) for line in process.stdout]
            self.assertEqual(
                [line["kind"] for line in remaining], ["progress", "progress", "result"]
            )
            self.assertEqual(remaining[-1]["output"]["content"], "Hello Alice")
            self.assertEqual(process.wait(timeout=10), 0)
            self.assertEqual(process.stderr.read(), b"")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
            process.stderr.close()


if __name__ == "__main__":
    unittest.main()
