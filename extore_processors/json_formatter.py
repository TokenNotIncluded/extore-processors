"""Strict, bounded offline JSON formatting without binary floating point.

Numbers are validated with Decimal and emitted using their original JSON token.
Formatting changes whitespace and, only when requested, object key order. It does
not normalize strings or numbers, fetch URLs, evaluate expressions or open files.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .schema import ProcessorError, configuration, field, select

MAX_INPUT_CHARACTERS = 10000
MAX_INPUT_BYTES = 100000
MAX_OUTPUT_BYTES = 100000
MAX_DEPTH = 32
MAX_NODES = 10000
MAX_CONTAINER_ITEMS = 2000
MAX_NUMBER_CHARACTERS = 256
MAX_SIGNIFICANT_DIGITS = 128
MAX_EXPONENT = 1000

SPEC = {
    "id": "json_formatter",
    "schema_version": 1,
    "name": {"zh-CN": "JSON 校验与格式化", "en": "JSON validation and formatting"},
    "description": {
        "zh-CN": (
            "粘贴 API 配置或 JSON 数据，严格校验后得到可复制的格式化 JSON 与结构统计。"
            "支持对象、数组、字符串、数字、布尔值和 null；不执行代码、不联网。"
            "数字保留原文精度和指数写法，字符串内容保持不变。"
        ),
        "en": (
            "Validate pasted API configuration or JSON data and return formatted JSON "
            "with structural counts. Objects, arrays, strings, numbers, booleans and "
            "null are supported. Runs offline without executing code. Number tokens "
            "retain their precision and exponent notation; string content is preserved."
        ),
    },
    "delivery": "content",
    "parameters": [
        field(
            "json_text",
            "JSON 内容",
            "JSON content",
            kind="textarea",
            zh_help=(
                "粘贴一份完整 JSON，最多 10000 个字符。拒绝重复键、NaN、Infinity、"
                "孤立 Unicode 代理项、注释和尾逗号。最多 32 层容器、10000 个值、"
                "每个容器 2000 项；数字最多 256 字符、128 位有效数字、指数绝对值 1000。"
                "不要填写不希望出现在交付内容里的密码或密钥。"
            ),
            en_help=(
                "Paste one complete JSON value, up to 10000 characters. Duplicate "
                "keys, NaN, Infinity, lone Unicode surrogates, comments and trailing "
                "commas are rejected. Limits: 32 container levels, 10000 values, "
                "2000 entries per container, 256 characters and 128 significant "
                "digits per number, and an absolute exponent of 1000. Do not include "
                "credentials you do not want in the delivered content."
            ),
        ),
    ],
    "outputs": [
        field(
            "formatted_json",
            "格式化 JSON",
            "Formatted JSON",
            kind="textarea",
            zh_help="可以复制给下一个程序或处理者；只调整空白和配置要求的对象键顺序。",
            en_help="Copy to the next program or worker. Only whitespace and the configured object key order change.",
        ),
        field(
            "report",
            "校验与结构统计",
            "Validation and structure report",
            kind="textarea",
            zh_help="显示顶层类型、值数量、对象键和数组项总数、容器深度及 UTF-8 大小，不摘录内容。",
            en_help="Shows the root type, value count, total object keys and array items, container depth and UTF-8 sizes without excerpts.",
        ),
    ],
    "configuration": [
        configuration(
            select(
                "indent",
                "缩进",
                "Indentation",
                [
                    ("2", "2 个空格", "2 spaces"),
                    ("4", "4 个空格", "4 spaces"),
                    ("compact", "紧凑格式", "Compact"),
                ],
                zh_help="格式化结果和统计合计最多 100000 UTF-8 字节；缩进过大的结果会被拒绝。",
                en_help="Formatted JSON and its report together are limited to 100000 UTF-8 bytes. Oversized indented results are rejected.",
            ),
            default="2",
            secret=False,
        ),
        configuration(
            select(
                "sort_keys",
                "对象键排序",
                "Sort object keys",
                [
                    ("no", "保留输入顺序", "Preserve input order"),
                    ("yes", "按 Unicode 顺序排序", "Sort by Unicode order"),
                ],
                zh_help="只调整对象键的展示顺序，不改变数组顺序或任何值。",
                en_help="Changes object key presentation order only; arrays and all values retain their contents.",
            ),
            default="no",
            secret=False,
        ),
    ],
}


@dataclass(frozen=True)
class _Number:
    token: str
    value: Decimal


def _number(token):
    if len(token) > MAX_NUMBER_CHARACTERS:
        raise ProcessorError("json_number_too_long", "json_text")
    exponent = re.search(r"[eE]([+-]?\d+)$", token)
    if exponent and abs(int(exponent.group(1))) > MAX_EXPONENT:
        raise ProcessorError("json_number_out_of_range", "json_text")
    try:
        value = Decimal(token)
    except InvalidOperation:
        raise ProcessorError("invalid_json_number", "json_text") from None
    if not value.is_finite():
        raise ProcessorError("invalid_json_number", "json_text")
    if len(value.as_tuple().digits) > MAX_SIGNIFICANT_DIGITS:
        raise ProcessorError("json_number_too_precise", "json_text")
    # A long fractional coefficient or exponent must not evade the range bound.
    if not value.is_zero() and abs(value.adjusted()) > MAX_EXPONENT:
        raise ProcessorError("json_number_out_of_range", "json_text")
    return _Number(token, value)


def _constant(_token):
    raise ProcessorError("invalid_json_number", "json_text")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProcessorError("json_duplicate_key", "json_text")
        result[key] = value
    return result


def _check_text(text):
    if not isinstance(text, str):
        raise ProcessorError("expected_string", "json_text")
    if not text.strip():
        raise ProcessorError("required_field", "json_text")
    if len(text) > MAX_INPUT_CHARACTERS:
        raise ProcessorError("value_too_long", "json_text")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError:
        raise ProcessorError("invalid_json_unicode", "json_text") from None
    if size > MAX_INPUT_BYTES:
        raise ProcessorError("json_input_too_large", "json_text")
    # Check nesting before the recursive standard decoder runs. Brackets inside
    # strings do not count; malformed quoting/escapes are left to that decoder.
    depth = 0
    quoted = False
    escaped = False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                raise ProcessorError("json_too_deep", "json_text")
        elif character in "]}":
            depth -= 1
    return size


def _unicode(text):
    if any(0xD800 <= ord(character) <= 0xDFFF for character in text):
        raise ProcessorError("invalid_json_unicode", "json_text")


def _parse(text):
    input_bytes = _check_text(text)
    try:
        value = json.loads(
            text,
            parse_int=_number,
            parse_float=_number,
            parse_constant=_constant,
            object_pairs_hook=_object,
        )
    except (json.JSONDecodeError, UnicodeError, RecursionError):
        raise ProcessorError("invalid_json", "json_text") from None
    stats = {
        "nodes": 0,
        "object_keys": 0,
        "array_items": 0,
        "depth": 0,
        "input_bytes": input_bytes,
    }
    pending = [(value, 0)]
    while pending:
        node, ancestors = pending.pop()
        stats["nodes"] += 1
        if stats["nodes"] > MAX_NODES:
            raise ProcessorError("json_too_many_values", "json_text")
        if isinstance(node, (dict, list)):
            depth = ancestors + 1
            stats["depth"] = max(stats["depth"], depth)
            if depth > MAX_DEPTH:
                raise ProcessorError("json_too_deep", "json_text")
            if len(node) > MAX_CONTAINER_ITEMS:
                raise ProcessorError("json_too_many_items", "json_text")
            if isinstance(node, dict):
                stats["object_keys"] += len(node)
                for key, child in node.items():
                    _unicode(key)
                    pending.append((child, depth))
            else:
                stats["array_items"] += len(node)
                pending.extend((child, depth) for child in node)
        elif isinstance(node, str):
            _unicode(node)
    return value, stats


def validate_parameters(values):
    if not isinstance(values, Mapping):
        raise ProcessorError("expected_object")
    if set(values) != {"json_text"}:
        if set(values) - {"json_text"}:
            raise ProcessorError("unknown_fields")
        raise ProcessorError("required_field", "json_text")
    _parse(values["json_text"])
    return dict(values)


def validate_configuration(settings, *, allow_incomplete=False):
    if not isinstance(settings, Mapping):
        raise ProcessorError("expected_object")
    if set(settings) - {"indent", "sort_keys"}:
        raise ProcessorError("unknown_fields")
    result = {"indent": "2", "sort_keys": "no", **settings}
    for key, choices in (
        ("indent", {"2", "4", "compact"}),
        ("sort_keys", {"yes", "no"}),
    ):
        if not isinstance(result[key], str):
            raise ProcessorError("expected_string", key)
        if result[key] not in choices and not (allow_incomplete and result[key] == ""):
            raise ProcessorError("invalid_choice", key)
    return result


def _serialize(value, *, indent, sort_keys):
    chunks = []
    size = 0

    def append(text):
        nonlocal size
        size += len(text.encode("utf-8"))
        if size > MAX_OUTPUT_BYTES:
            raise ProcessorError("json_output_too_large", "json_text")
        chunks.append(text)

    def write(node, level):
        if isinstance(node, dict):
            entries = sorted(node.items()) if sort_keys else node.items()
            opening, closing = "{", "}"
        elif isinstance(node, list):
            entries = enumerate(node)
            opening, closing = "[", "]"
        else:
            if isinstance(node, _Number):
                append(node.token)
            else:
                append(json.dumps(node, ensure_ascii=False, allow_nan=False))
            return
        append(opening)
        for index, (key, child) in enumerate(entries):
            if index:
                append(",")
            if indent is not None:
                append("\n" + " " * (indent * (level + 1)))
            if isinstance(node, dict):
                append(json.dumps(key, ensure_ascii=False))
                append(":" if indent is None else ": ")
            write(child, level + 1)
        if node and indent is not None:
            append("\n" + " " * (indent * level))
        append(closing)

    write(value, 0)
    return "".join(chunks), size


def _type(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, _Number):
        return "number"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return "string"


def process(params, configuration):
    if not isinstance(params, Mapping) or set(params) != {"json_text"}:
        # Keep standalone calls as strict as registry-dispatched calls.
        validate_parameters(params)
    value, stats = _parse(params["json_text"])
    settings = validate_configuration(configuration)
    formatted, output_bytes = _serialize(
        value,
        indent=None if settings["indent"] == "compact" else int(settings["indent"]),
        sort_keys=settings["sort_keys"] == "yes",
    )
    report = (
        "JSON 校验通过 / Valid JSON\n"
        f"类型 / Type: {_type(value)}\n"
        f"值数量 / Values: {stats['nodes']}\n"
        f"对象键总数 / Object keys: {stats['object_keys']}\n"
        f"数组项总数 / Array items: {stats['array_items']}\n"
        f"容器深度 / Container depth: {stats['depth']}\n"
        f"输入 / Input UTF-8 bytes: {stats['input_bytes']}\n"
        f"JSON 输出 / JSON output UTF-8 bytes: {output_bytes}"
    )
    if output_bytes + len(report.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise ProcessorError("json_output_too_large", "json_text")
    return {"formatted_json": formatted, "report": report}
