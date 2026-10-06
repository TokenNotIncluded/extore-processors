"""Closed registry: accepting a processor ID never imports merchant code."""

import copy
import re
from collections.abc import Mapping
from dataclasses import dataclass
from string import Template
from types import MappingProxyType
from urllib.parse import urlsplit


class ProcessorError(ValueError):
    """A safe validation error that never includes customer or secret values."""

    def __init__(self, code: str, field: str | None = None):
        self.code = code
        self.field = field
        super().__init__(code + (f": {field}" if field else ""))


def _field(key, zh, en, *, kind="text", required=True, zh_help="", en_help=""):
    return {
        "key": key,
        "label": {"zh-CN": zh, "en": en},
        "description": {"zh-CN": zh_help, "en": en_help},
        "collapsed": True,
        "required": required,
        "type": kind,
    }


_RESOURCE_URL = _field(
    "resource_url",
    "资源链接",
    "Resource URL",
    kind="url",
    zh_help="填写你要交付的 HTTPS 资源地址。处理器只返回链接，不访问这个地址。",
    en_help="Enter the HTTPS resource URL to deliver. The processor returns it without making a network request.",
)
_MESSAGE = _field(
    "message",
    "领取说明",
    "Instructions",
    kind="textarea",
    required=False,
    zh_help="可选。补充资源的使用方法；支持普通多行文本。",
    en_help="Optional. Add instructions for using the resource as plain text.",
)
_CONTENT = _field("content", "交付内容", "Delivery content", kind="textarea")
_NAME = _field(
    "name",
    "称呼",
    "Name",
    zh_help="填写你希望使用的称呼，最多 200 个字符。它只用于生成这次领取的文本。",
    en_help="Enter the name to use in your delivery text, up to 200 characters.",
)
_TEMPLATE = _field(
    "template",
    "交付文本模板",
    "Delivery text template",
    kind="textarea",
    zh_help=(
        "用 `$name` 或 `${name}` 插入顾客的称呼，用 `$$` 表示一个美元符号。\n\n"
        "例如：`你好，$name！你的商品已准备好。`\n\n"
        "这是纯文本替换，不执行 Python、HTML 或其他代码。"
    ),
    en_help=(
        "Use `$name` or `${name}` for the customer name, and `$$` for a literal dollar sign.\n\n"
        "Example: `Hello, $name! Your item is ready.`\n\n"
        "This is plain text substitution. It does not execute Python, HTML, or other code."
    ),
)


def _configuration(field, *, max_length=10000, default=None):
    value = {**field, "secret": True, "max_length": max_length}
    if default is not None:
        value["default"] = default
    return value


_SPECS = MappingProxyType(
    {
        "resource_link": {
            "id": "resource_link",
            "schema_version": 1,
            "name": {"zh-CN": "资源链接", "en": "Resource link"},
            "description": {
                "zh-CN": "顾客无需填写额外信息，兑换后领取商家配置的资源链接与使用说明。",
                "en": "Deliver a configured resource URL and optional instructions without asking the customer for extra information.",
            },
            "delivery": "content",
            "parameters": [],
            "outputs": [_RESOURCE_URL, _MESSAGE],
            "configuration": [
                _configuration(_RESOURCE_URL, max_length=2000),
                _configuration(_MESSAGE, default=""),
            ],
        },
        "personalized_text": {
            "id": "personalized_text",
            "schema_version": 1,
            "name": {"zh-CN": "个性化文本", "en": "Personalized text"},
            "description": {
                "zh-CN": "顾客填写称呼，处理器按商家设定的纯文本模板生成交付内容。",
                "en": "Ask for a customer name and generate delivery text from the configured plain text template.",
            },
            "delivery": "content",
            "parameters": [_NAME],
            "outputs": [_CONTENT],
            "configuration": [
                _configuration(_TEMPLATE, default="你好，$name！\n你的商品已准备好。")
            ],
        },
    }
)


def get_spec(processor_id: str) -> dict:
    """Return a copy: caller changes cannot alter the running registry."""
    if not isinstance(processor_id, str) or processor_id not in _SPECS:
        raise ProcessorError("unknown_processor")
    return copy.deepcopy(_SPECS[processor_id])


def catalog() -> list[dict]:
    return [get_spec(processor_id) for processor_id in _SPECS]


def _strings(values, fields, *, defaults=False, allow_incomplete=False):
    if not isinstance(values, Mapping):
        raise ProcessorError("expected_object")
    keys = {field["key"] for field in fields}
    if any(key not in keys for key in values):
        raise ProcessorError("unknown_fields")
    result = {}
    for field in fields:
        key = field["key"]
        value = values.get(key, field.get("default", "") if defaults else "")
        if not isinstance(value, str):
            raise ProcessorError("expected_string", key)
        # Preserve intentionally formatted delivery templates and instructions.
        if field["type"] != "textarea":
            value = value.strip()
        if field["required"] and not value.strip() and not allow_incomplete:
            raise ProcessorError("required_field", key)
        if len(value) > field.get("max_length", 10000):
            raise ProcessorError("value_too_long", key)
        result[key] = value
    return result


def _https_url(value):
    if not value:
        return
    if re.search(r"[\s\x00-\x1f\x7f]", value):
        raise ProcessorError("invalid_https_url", "resource_url")
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and "\\" not in value
        )
        # Accessing port performs urllib's numeric and range validation.
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ProcessorError("invalid_https_url", "resource_url")


def _template(value):
    template = Template(value)
    if not template.is_valid():
        raise ProcessorError("invalid_template", "template")
    if set(template.get_identifiers()) - {"name"}:
        raise ProcessorError("unknown_template_variable", "template")
    return template


def validate_configuration(
    processor_id: str, configuration, *, allow_incomplete: bool = False
) -> dict[str, str]:
    """Validate secret settings, optionally permitting an unfinished draft.

    Optional defaults declared by the processor are applied. Incomplete mode
    permits empty required values; it never skips validation of supplied values.
    """
    spec = get_spec(processor_id)
    result = _strings(
        configuration,
        spec["configuration"],
        defaults=True,
        allow_incomplete=allow_incomplete,
    )
    if processor_id == "resource_link":
        _https_url(result["resource_url"])
    else:
        _template(result["template"])
    return result


def _resource_link(params, configuration):
    return {
        "resource_url": configuration["resource_url"],
        "message": configuration["message"],
    }


def _personalized_text(params, configuration):
    return {"content": _template(configuration["template"]).substitute(params)}


_HANDLERS = MappingProxyType(
    {"resource_link": _resource_link, "personalized_text": _personalized_text}
)


def validate_parameters(processor_id: str, params) -> dict[str, str]:
    """Validate customer input before accepting a job as well as at runtime."""
    spec = get_spec(processor_id)
    values = _strings(params, spec["parameters"])
    if processor_id == "personalized_text" and len(values["name"]) > 200:
        raise ProcessorError("value_too_long", "name")
    return values


def run(processor_id: str, params, configuration) -> dict:
    """Run only an explicit, reviewed handler with code-defined inputs/outputs."""
    spec = get_spec(processor_id)
    values = validate_parameters(processor_id, params)
    settings = validate_configuration(processor_id, configuration)
    output = _HANDLERS[processor_id](values, settings)
    # Validate the declared result even though the handler itself is trusted.
    fields = [dict(field, max_length=100000) for field in spec["outputs"]]
    output = _strings(output, fields)
    if processor_id == "resource_link":
        _https_url(output["resource_url"])
    return {"status": "succeeded", "output": output}


@dataclass(frozen=True)
class Processor:
    id: str

    @property
    def spec(self) -> dict:
        return get_spec(self.id)

    def run(self, params, configuration) -> dict:
        return run(self.id, params, configuration)


def get_processor(processor_id: str) -> Processor:
    get_spec(processor_id)
    return Processor(processor_id)
