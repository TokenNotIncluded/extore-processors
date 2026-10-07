"""Offline document text from a shop-owned, single-pass dollar template."""

import re
import string
import unicodedata
from collections.abc import Mapping
from string import Template

from .schema import ProcessorError, configuration, field, select

MAX_OUTPUT_BYTES = 100000
DEFAULT_TEMPLATE = "# $title\n\n$name\n\n$body"
_VARIABLES = frozenset({"title", "name", "body"})
_MARKDOWN_PUNCTUATION = re.compile("([" + re.escape(string.punctuation) + "])")

SPEC = {
    "id": "document_template",
    "schema_version": 1,
    "name": {"zh-CN": "文档文本模板", "en": "Document text template"},
    "description": {
        "zh-CN": (
            "把顾客提供的标题、称呼和正文填入本店文本模板，制作项目说明、交付说明、"
            "受理单或证书文本草稿。只交付纯文本或 Markdown，不生成 DOCX/PPTX 文件，"
            "不提供真实证书签名。无需联网或 AI。"
        ),
        "en": (
            "Fill a shop-owned text template with a customer's title, name and body "
            "for project briefs, delivery notes, intake records or certificate text "
            "drafts. Delivers plain text or Markdown, not DOCX/PPTX files or signed "
            "certificates. No network connection or AI is required."
        ),
    },
    "delivery": "content",
    "parameters": [
        field(
            "title",
            "标题",
            "Title",
            zh_help="最多 200 个字符。Markdown 格式会将标题作为单行文字并转义标点。",
            en_help=(
                "Up to 200 characters. Markdown output treats the title as a single "
                "line of escaped text."
            ),
        ),
        field(
            "name",
            "称呼（可选）",
            "Name (optional)",
            required=False,
            zh_help="最多 200 个字符；留空即可。Markdown 格式会作为单行文字处理。",
            en_help=(
                "Up to 200 characters; may be left empty. Markdown output treats "
                "the name as a single line of escaped text."
            ),
        ),
        field(
            "body",
            "正文内容",
            "Body",
            kind="textarea",
            zh_help=(
                "最多 10000 个字符。正文按原文填入；Markdown 格式保留正文的 "
                "Markdown 内容。本处理器不会执行内容、访问其中的网址或渲染 HTML。"
            ),
            en_help=(
                "Up to 10000 characters. The body is inserted as written, including "
                "Markdown when that format is selected. The processor does not "
                "execute content, visit its URLs or render HTML."
            ),
        ),
    ],
    "outputs": [
        field(
            "content",
            "生成的文档文本",
            "Generated document text",
            kind="textarea",
            zh_help=(
                "根据本店模板生成的纯文本或 Markdown，最多 100000 UTF-8 字节。"
                "可复制保存；不是 Word 或 PowerPoint 文件。"
            ),
            en_help=(
                "Plain text or Markdown generated from the shop template, limited "
                "to 100000 UTF-8 bytes. Copy it to save; it is not a Word or "
                "PowerPoint file."
            ),
        ),
        field(
            "format",
            "文本格式",
            "Text format",
            zh_help="plain 表示纯文本；markdown 表示 Markdown 文本。",
            en_help="plain means plain text; markdown means Markdown text.",
        ),
    ],
    "configuration": [
        configuration(
            field(
                "template",
                "文档文本模板",
                "Document text template",
                kind="textarea",
                zh_help=(
                    "可随时查看和编辑。最多 10000 个字符。仅支持 $title、$name、$body "
                    "（也可写成 ${title} 等）与 $$ 字面美元符号；只替换一次。"
                    "不支持代码、环境变量、密钥、模板表达式或 HTML 渲染。"
                ),
                en_help=(
                    "Readable and editable. Up to 10000 characters. Supports only "
                    "$title, $name, $body (also ${title}, etc.) and $$ for a literal "
                    "dollar sign. Substitutes once, without code, environment "
                    "variables, secrets, template expressions or HTML rendering."
                ),
            ),
            default=DEFAULT_TEMPLATE,
            secret=False,
        ),
        configuration(
            select(
                "output_format",
                "交付文本格式",
                "Delivery text format",
                [
                    ("plain", "纯文本", "Plain text"),
                    ("markdown", "Markdown", "Markdown"),
                ],
                zh_help=(
                    "纯文本保留字段文字；Markdown 转义标题与称呼，保留正文的 "
                    "Markdown 内容。格式不会将文本转换成文件或渲染 HTML。"
                ),
                en_help=(
                    "Plain text preserves field text. Markdown escapes the title "
                    "and name while preserving body Markdown. Neither format "
                    "creates a document file or renders HTML."
                ),
            ),
            default="markdown",
            max_length=10,
            secret=False,
        ),
    ],
}


def _strings(values, fields, *, defaults=False, allow_incomplete=False):
    if not isinstance(values, Mapping):
        raise ProcessorError("expected_object")
    if set(values) - {item["key"] for item in fields}:
        raise ProcessorError("unknown_fields")
    result = {}
    for item in fields:
        key = item["key"]
        value = values.get(key, item.get("default", "") if defaults else "")
        if not isinstance(value, str):
            raise ProcessorError("expected_string", key)
        if any(
            unicodedata.category(char) in {"Cc", "Cs"} and char not in "\t\r\n"
            for char in value
        ):
            raise ProcessorError("invalid_text", key)
        limit = 200 if key in {"title", "name"} else item.get("max_length", 10000)
        if len(value) > limit:
            raise ProcessorError("value_too_long", key)
        if item["type"] != "textarea":
            value = value.strip()
        if item["required"] and not value.strip() and not allow_incomplete:
            raise ProcessorError("required_field", key)
        result[key] = value
    return result


def _template(value):
    template = Template(value)
    if not template.is_valid():
        raise ProcessorError("invalid_template", "template")
    if set(template.get_identifiers()) - _VARIABLES:
        raise ProcessorError("unknown_template_variable", "template")
    return template


def validate_parameters(values) -> dict[str, str]:
    """Keep customer content as data; reject unsupported fields and controls."""
    return _strings(values, SPEC["parameters"])


def validate_configuration(settings, allow_incomplete=False) -> dict[str, str]:
    """Defaults are shop-specific values, not process environment variables."""
    result = _strings(
        settings,
        SPEC["configuration"],
        defaults=True,
        allow_incomplete=allow_incomplete,
    )
    _template(result["template"])
    output_format = result["output_format"]
    if output_format not in {"plain", "markdown"} and not (
        allow_incomplete and output_format == ""
    ):
        raise ProcessorError("invalid_option", "output_format")
    return result


def _markdown_text(value):
    # splitlines includes Unicode line/paragraph separators as well as CR/LF.
    single_line = " ".join(value.splitlines()).replace("\t", " ")
    return _MARKDOWN_PUNCTUATION.sub(r"\\\1", single_line)


def _check_output_size(template, values):
    # Count the single-pass result before allocating a repeatedly expanded body.
    # Template syntax has already been validated, so each match has one kind.
    byte_count = len(template.template.encode("utf-8"))
    sizes = {key: len(value.encode("utf-8")) for key, value in values.items()}
    for match in template.pattern.finditer(template.template):
        name = match.group("named") or match.group("braced")
        replacement_size = sizes[name] if name else 1  # $$ -> $
        byte_count += replacement_size - len(match.group().encode("utf-8"))
    if byte_count > MAX_OUTPUT_BYTES:
        raise ProcessorError("output_too_large", "content")


def process(params, configuration) -> dict[str, str]:
    """Render once with an explicit map; never read environment or execute text."""
    values = validate_parameters(params)
    settings = validate_configuration(configuration)
    if settings["output_format"] == "markdown":
        values = {
            **values,
            "title": _markdown_text(values["title"]),
            "name": _markdown_text(values["name"]),
        }
    template = _template(settings["template"])
    _check_output_size(template, values)
    return {
        "content": template.substitute(values),
        "format": settings["output_format"],
    }
