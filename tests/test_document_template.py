import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from extore_processors.document_template import (
    DEFAULT_TEMPLATE,
    SPEC,
    process,
    validate_configuration,
    validate_parameters,
)
from extore_processors.schema import ProcessorError

ROOT = Path(__file__).resolve().parents[1]
PARAMS = {"title": "交付说明", "name": "张三", "body": "第一步：下载文件。"}


class DocumentTemplateTests(unittest.TestCase):
    def test_schema_has_bilingual_help_and_readable_shop_settings(self):
        self.assertEqual(SPEC["id"], "document_template")
        self.assertEqual(SPEC["delivery"], "content")
        self.assertEqual(SPEC["schema_version"], 1)
        for field_type in ("parameters", "outputs", "configuration"):
            for item in SPEC[field_type]:
                with self.subTest(field_type=field_type, key=item["key"]):
                    self.assertTrue({"zh-CN", "en"} <= item["label"].keys())
                    self.assertTrue(
                        all(item["description"].get(x) for x in ("zh-CN", "en"))
                    )
        self.assertTrue(all(not item["secret"] for item in SPEC["configuration"]))
        self.assertTrue(all("max_length" not in item for item in SPEC["parameters"]))
        self.assertIn("DOCX/PPTX", SPEC["description"]["zh-CN"])
        self.assertIn("signed certificates", SPEC["description"]["en"])

    def test_default_template_and_optional_name(self):
        self.assertEqual(
            validate_configuration({}),
            {"template": DEFAULT_TEMPLATE, "output_format": "markdown"},
        )
        self.assertEqual(
            process({"title": "项目说明", "body": "项目内容"}, {}),
            {"content": "# 项目说明\n\n\n\n项目内容", "format": "markdown"},
        )

    def test_text_template_remains_editable_and_shop_specific(self):
        shop_a = {
            "template": "受理单：$title\n申请人：$name\n$body",
            "output_format": "plain",
        }
        shop_b = {
            "template": "## ${title}\n\n${body}\n\n由 ${name} 提交",
            "output_format": "markdown",
        }
        self.assertEqual(validate_configuration(shop_a), shop_a)
        self.assertEqual(validate_configuration(shop_b), shop_b)
        self.assertEqual(
            process(PARAMS, shop_a)["content"],
            "受理单：交付说明\n申请人：张三\n第一步：下载文件。",
        )
        self.assertEqual(
            process(PARAMS, shop_b)["content"],
            "## 交付说明\n\n第一步：下载文件。\n\n由 张三 提交",
        )
        updated = {**shop_a, "template": "新版：$body"}
        self.assertEqual(
            process(PARAMS, updated)["content"], "新版：第一步：下载文件。"
        )
        self.assertEqual(
            process(PARAMS, shop_b)["content"],
            "## 交付说明\n\n第一步：下载文件。\n\n由 张三 提交",
        )

    def test_plain_text_preserves_line_breaks_markup_and_body_whitespace(self):
        values = {
            "title": "A\r\n# B",
            "name": "*张三*\n[名字](url)",
            "body": "  正文\r\n<script>alert(1)</script>\t\n  ",
        }
        result = process(
            values, {"template": "$title\n$name\n$body", "output_format": "plain"}
        )
        self.assertEqual(
            result["content"],
            "A\r\n# B\n*张三*\n[名字](url)\n  正文\r\n<script>alert(1)</script>\t\n  ",
        )
        self.assertEqual(result["format"], "plain")

    def test_markdown_escapes_heading_and_name_without_rewriting_body(self):
        body = "## 已有正文\n<script>bad()</script>\n[引用](javascript:alert(1))"
        result = process(
            {
                "title": "第一行\r\n# 第二行",
                "name": "[张三](url)\t*姓名*",
                "body": body,
            },
            {"template": "# $title\n\n$name\n\n$body"},
        )
        self.assertEqual(
            result["content"],
            "# 第一行 \\# 第二行\n\n\\[张三\\]\\(url\\) \\*姓名\\*\n\n" + body,
        )

    def test_unicode_line_and_paragraph_separators_cannot_inject_headings(self):
        result = process(
            {"title": "A\u2028# B", "name": "C\u2029## D", "body": "保留\u2028正文"},
            {"template": "$title|$name|$body"},
        )
        self.assertEqual(result["content"], "A \\# B|C \\#\\# D|保留\u2028正文")

    def test_substitution_is_single_pass_and_does_not_access_environment(self):
        body = "${name} $HOME $(touch /tmp/not-executed) {{ secrets.TOKEN }} <script>x()</script>"
        params = {"title": "${body}", "name": "$HOME", "body": body}
        with patch.dict(os.environ, {"HOME": "do-not-leak", "TOKEN": "do-not-leak"}):
            result = process(
                params,
                {"template": "$title\n$name\n$body\n$$25", "output_format": "plain"},
            )
        self.assertEqual(result["content"], "${body}\n$HOME\n" + body + "\n$25")
        self.assertNotIn("do-not-leak", result["content"])
        self.assertEqual(params["body"], body)

    def test_fixed_template_needs_no_placeholders_and_dollar_escape_is_literal(self):
        self.assertEqual(
            process(PARAMS, {"template": "固定说明：$$25"})["content"], "固定说明：$25"
        )

    def test_unknown_and_malformed_placeholders_are_safe_errors(self):
        for template in (
            "$HOME",
            "${secret}",
            "${name.__class__}",
            "${body[0]}",
            "$9",
            "$()",
            "${title",
            "$",
            "${title:-fallback}",
        ):
            with (
                self.subTest(template=template),
                self.assertRaises(ProcessorError) as error,
            ):
                validate_configuration({"template": template})
            self.assertIn(
                error.exception.code, {"invalid_template", "unknown_template_variable"}
            )
            self.assertEqual(error.exception.field, "template")
            self.assertNotIn(template, str(error.exception))

    def test_validation_rejects_objects_non_strings_and_extra_fields(self):
        for values in (
            None,
            [],
            "text",
            {**PARAMS, "unknown": "secret"},
            {**PARAMS, "title": 1},
            {**PARAMS, "name": None},
            {**PARAMS, "body": {"text": "secret"}},
        ):
            with self.subTest(values=values), self.assertRaises(ProcessorError):
                validate_parameters(values)
        for settings in (
            None,
            [],
            {"unknown": "secret"},
            {"template": 1},
            {"output_format": []},
        ):
            with self.subTest(settings=settings), self.assertRaises(ProcessorError):
                validate_configuration(settings)

    def test_required_fields_and_supplied_invalid_drafts_are_checked(self):
        for key in ("title", "body"):
            with self.subTest(key=key), self.assertRaises(ProcessorError) as error:
                validate_parameters({**PARAMS, key: " \t\r\n"})
            self.assertEqual(error.exception.code, "required_field")
        self.assertEqual(
            validate_parameters({"title": " 项目 ", "body": " 内容 "}),
            {"title": "项目", "name": "", "body": " 内容 "},
        )
        self.assertEqual(
            validate_configuration(
                {"template": "", "output_format": ""}, allow_incomplete=True
            ),
            {"template": "", "output_format": ""},
        )
        for settings in (
            {"template": ""},
            {"output_format": ""},
            {"output_format": "html"},
        ):
            with self.subTest(settings=settings), self.assertRaises(ProcessorError):
                validate_configuration(settings)
        for settings in (
            {"template": "$TOKEN"},
            {"template": "${title"},
            {"output_format": "html"},
        ):
            with self.subTest(settings=settings), self.assertRaises(ProcessorError):
                validate_configuration(settings, allow_incomplete=True)

    def test_character_boundaries_for_all_inputs_and_template(self):
        for key, length in (("title", 200), ("name", 200), ("body", 10000)):
            with self.subTest(key=key):
                self.assertEqual(
                    validate_parameters({**PARAMS, key: "文" * length})[key],
                    "文" * length,
                )
                with self.assertRaises(ProcessorError) as error:
                    validate_parameters({**PARAMS, key: "文" * (length + 1)})
                self.assertEqual(error.exception.code, "value_too_long")
        self.assertEqual(
            len(validate_configuration({"template": "文" * 10000})["template"]), 10000
        )
        with self.assertRaises(ProcessorError):
            validate_configuration({"template": "文" * 10001})

    def test_controls_and_surrogates_are_rejected_before_stripping(self):
        for character in (
            "\x00",
            "\x01",
            "\x0b",
            "\x0c",
            "\x1b",
            "\x1c",
            "\x1f",
            "\x7f",
            "\x85",
            "\ud800",
            "\udfff",
        ):
            for key in ("title", "name", "body"):
                with (
                    self.subTest(character=repr(character), key=key),
                    self.assertRaises(ProcessorError) as error,
                ):
                    validate_parameters({**PARAMS, key: character + "confidential"})
                self.assertEqual(error.exception.code, "invalid_text")
                self.assertNotIn("confidential", str(error.exception))
            with (
                self.subTest(character=repr(character)),
                self.assertRaises(ProcessorError),
            ):
                validate_configuration({"template": "confidential" + character})
        for key in ("title", "name"):
            with self.subTest(key=key), self.assertRaises(ProcessorError):
                validate_parameters({**PARAMS, key: "\t" * 201 + "A"})

    def test_valid_unicode_is_preserved(self):
        body = "日本語 中文 café 👩🏽‍💻 العربية e\u0301\n\t第二行"
        self.assertEqual(
            process({"title": "说明 👩🏽‍💻", "body": body}, {"template": "$body"})[
                "content"
            ],
            body,
        )

    def test_expansion_limit_is_utf8_bytes_and_checked_before_substitution(self):
        exact = process({**PARAMS, "body": "a" * 10000}, {"template": "${body}" * 10})
        self.assertEqual(len(exact["content"].encode("utf-8")), 100000)
        with (
            patch(
                "extore_processors.document_template.Template.substitute",
                side_effect=AssertionError("must not allocate oversized result"),
            ),
            self.assertRaises(ProcessorError) as error,
        ):
            process({**PARAMS, "body": "a" * 10000}, {"template": "${body}" * 10 + "a"})
        self.assertEqual(error.exception.code, "output_too_large")
        exact = process(
            {**PARAMS, "body": "文" * 3333}, {"template": "${body}" * 10 + "a" * 10}
        )
        self.assertEqual(len(exact["content"].encode("utf-8")), 100000)
        with self.assertRaises(ProcessorError):
            process(
                {**PARAMS, "body": "文" * 3333}, {"template": "${body}" * 10 + "a" * 11}
            )
        with self.assertRaises(ProcessorError):
            process({**PARAMS, "body": "😀" * 10000}, {"template": "${body}" * 3})

    def test_process_does_not_mutate_customer_or_shop_dictionaries(self):
        params = dict(PARAMS)
        settings = {"template": "$title\n$body", "output_format": "markdown"}
        before = dict(settings)
        process(params, settings)
        self.assertEqual(params, PARAMS)
        self.assertEqual(settings, before)


class DocumentTemplateProtocolTests(unittest.TestCase):
    def invoke(self, envelope):
        return subprocess.run(
            [sys.executable, "-m", "extore_processors", "document_template"],
            input=json.dumps(envelope, ensure_ascii=False),
            text=True,
            capture_output=True,
            cwd=ROOT,
            timeout=10,
            check=False,
        )

    def test_cli_delivers_only_the_declared_text_and_format(self):
        result = self.invoke(
            {
                "params": PARAMS,
                "configuration": {
                    "template": "$title\n$body",
                    "output_format": "plain",
                },
                "environment": {"TOKEN": "environment-secret"},
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        messages = [json.loads(line) for line in result.stdout.splitlines()]
        terminal = [item for item in messages if item["kind"] == "result"]
        self.assertEqual(
            terminal,
            [
                {
                    "kind": "result",
                    "state": "succeeded",
                    "output": {
                        "content": "交付说明\n第一步：下载文件。",
                        "format": "plain",
                    },
                }
            ],
        )
        self.assertNotIn("environment-secret", result.stdout + result.stderr)

    def test_cli_rejects_secret_lookup_without_echoing_configuration(self):
        result = self.invoke(
            {
                "params": PARAMS,
                "configuration": {"template": "$TOKEN confidential-shop-text"},
            }
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(
            json.loads(result.stderr),
            {"error": "unknown_template_variable", "field": "template"},
        )
        self.assertNotIn("confidential-shop-text", result.stderr)


if __name__ == "__main__":
    unittest.main()
