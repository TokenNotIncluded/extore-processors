"""Closed registry: accepting a processor ID never imports merchant code."""

import copy
import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from string import Template
from types import MappingProxyType
from urllib.parse import urlsplit

from . import csv_summary, document_template, json_formatter, text_cleanup
from .delivery_context import (
    INITIAL_REVISION,
    freeze_attributes,
    freeze_deliveries,
    freeze_delivery,
    freeze_entitlements,
    freeze_revision,
    validate_identity,
)
from .schema import (
    ProcessorError,
)
from .schema import (
    configuration as _configuration,
)
from .schema import (
    field as _field,
)

MAX_ENVIRONMENT_FIELDS = 128
MAX_ENVIRONMENT_VALUE_BYTES = 8192
MAX_ENVIRONMENT_BYTES = 65536
WORK_INSTRUCTIONS_SCHEMA = "extore.work-instructions.v1"
MAX_WORK_INSTRUCTION_LENGTH = 4000


def _freeze_environment(value):
    if not isinstance(value, Mapping) or len(value) > MAX_ENVIRONMENT_FIELDS:
        raise ProcessorError("invalid_environment")
    result = {}
    total_bytes = 0
    for name, content in value.items():
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name)
            or not isinstance(content, str)
            or "\x00" in content
        ):
            raise ProcessorError("invalid_environment")
        try:
            content_bytes = len(content.encode("utf-8"))
        except UnicodeError:
            raise ProcessorError("invalid_environment") from None
        total_bytes += content_bytes
        if (
            content_bytes > MAX_ENVIRONMENT_VALUE_BYTES
            or total_bytes > MAX_ENVIRONMENT_BYTES
        ):
            raise ProcessorError("environment_too_large")
        result[name] = content
    return MappingProxyType(result)


def _freeze_instructions(value, *, shop_id, product_id):
    """Validate trusted routing separately; preserve instruction text as data."""
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise ProcessorError("invalid_work_instructions")
    if not value:
        return MappingProxyType({})
    fields = {
        "schema",
        "shop_id",
        "product_id",
        "factory_slogan",
        "workshop_slogan",
        "revision",
    }
    if set(value) != fields or value["schema"] != WORK_INSTRUCTIONS_SCHEMA:
        raise ProcessorError("invalid_work_instructions")
    if (
        not isinstance(shop_id, str)
        or not isinstance(product_id, str)
        or value["shop_id"] != shop_id
        or value["product_id"] != product_id
    ):
        raise ProcessorError("work_instructions_scope_mismatch")
    for name in ("factory_slogan", "workshop_slogan"):
        content = value[name]
        if (
            not isinstance(content, str)
            or len(content) > MAX_WORK_INSTRUCTION_LENGTH
            or any(
                unicodedata.category(char) in {"Cc", "Cs"} and char not in "\t\r\n"
                for char in content
            )
        ):
            raise ProcessorError("invalid_work_instructions")
    revision = value["revision"]
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision):
        raise ProcessorError("invalid_work_instructions")
    body = {name: value[name] for name in fields if name != "revision"}
    canonical = json.dumps(
        body, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).encode("ascii")
    if hashlib.sha256(canonical).hexdigest() != revision:
        raise ProcessorError("work_instructions_revision_mismatch")
    return MappingProxyType({**body, "revision": revision})


def _step_plan(value, *, allow_empty=False):
    if (
        not isinstance(value, (list, tuple))
        or not (0 if allow_empty else 1) <= len(value) <= 30
    ):
        raise ProcessorError("invalid_progress_plan")
    result = []
    for step in value:
        if not isinstance(step, Mapping) or set(step) - {"id", "label", "done"}:
            raise ProcessorError("invalid_progress_plan")
        sid, labels = step.get("id"), step.get("label")
        if not isinstance(sid, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9_-]{0,39}", sid
        ):
            raise ProcessorError("invalid_progress_plan")
        if (
            not isinstance(labels, Mapping)
            or not 1 <= len(labels) <= 20
            or any(
                not isinstance(locale, str)
                or not locale.strip()
                or len(locale) > 40
                or not isinstance(label, str)
                or not label.strip()
                or len(label) > 200
                for locale, label in labels.items()
            )
        ):
            raise ProcessorError("invalid_progress_plan")
        result.append({"id": sid, "label": dict(labels)})
    if len({step["id"] for step in result}) != len(result):
        raise ProcessorError("invalid_progress_plan")
    return result


def _completed_steps(value, plan, previous=()):
    if (
        not isinstance(value, (list, tuple))
        or len(value) > 30
        or any(
            not isinstance(sid, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", sid)
            for sid in value
        )
        or len(set(value)) != len(value)
    ):
        raise ProcessorError("invalid_completed_steps")
    ids = [step["id"] for step in plan]
    if not set(value) <= set(ids) or not set(previous) <= set(value):
        raise ProcessorError("invalid_completed_steps")
    return tuple(sid for sid in ids if sid in value)


@dataclass(frozen=True)
class ShopContext:
    """Trusted server routing metadata, never customer input or credentials."""

    shop_id: str | None = None
    profile_id: str | None = None
    revision: int | None = None

    def __post_init__(self):
        if any(
            value is not None
            and (not isinstance(value, str) or not 1 <= len(value) <= 100)
            for value in (self.shop_id, self.profile_id)
        ):
            raise ProcessorError("invalid_shop_context")
        if self.revision is not None and (
            type(self.revision) is not int or self.revision < 1
        ):
            raise ProcessorError("invalid_shop_context")


class ProcessorContext:
    """Define a task's frozen plan and emit bounded, validated JSON progress."""

    def __init__(
        self,
        *,
        steps=(),
        completed_steps=(),
        shop_context=None,
        environment=MappingProxyType({}),
        product_id=None,
        instructions=None,
        job_id=None,
        attempt=None,
        card_attributes=MappingProxyType({}),
        entitlements=None,
        revision=INITIAL_REVISION,
        deliveries=(),
        last_delivery=None,
        emit=None,
    ):
        self._steps = self._freeze(_step_plan(steps, allow_empty=True))
        self._completed = _completed_steps(completed_steps, self.steps)
        if shop_context is None:
            shop_context = ShopContext()
        elif isinstance(shop_context, Mapping):
            if set(shop_context) - {"shop_id", "profile_id", "revision"}:
                raise ProcessorError("invalid_shop_context")
            shop_context = ShopContext(**shop_context)
        elif not isinstance(shop_context, ShopContext):
            raise ProcessorError("invalid_shop_context")
        self._shop_context = shop_context
        self._environment = _freeze_environment(environment)
        if product_id is not None and (
            not isinstance(product_id, str) or not 1 <= len(product_id) <= 100
        ):
            raise ProcessorError("invalid_product_context")
        self._instructions = _freeze_instructions(
            instructions, shop_id=shop_context.shop_id, product_id=product_id
        )
        validate_identity(job_id, attempt, ProcessorError)
        self._job_id, self._attempt = job_id, attempt
        self._card_attributes = freeze_attributes(card_attributes, ProcessorError)
        self._revision = freeze_revision(revision, ProcessorError)
        self._entitlements = freeze_entitlements(
            entitlements, self.card_attributes, self.revision, ProcessorError
        )
        self._deliveries = freeze_deliveries(deliveries, ProcessorError)
        self._last_delivery = (
            freeze_delivery(last_delivery, ProcessorError)
            if last_delivery is not None
            else None
        )
        self._emit = emit or (lambda value: None)

    @staticmethod
    def _freeze(plan):
        return tuple(
            MappingProxyType(
                {"id": step["id"], "label": MappingProxyType(dict(step["label"]))}
            )
            for step in plan
        )

    @property
    def steps(self):
        return self._steps

    @property
    def completed_steps(self):
        return self._completed

    @property
    def shop_context(self):
        return self._shop_context

    @property
    def environment(self):
        """Frozen workflow variables and secrets, separate from customer inputs."""
        return self._environment

    @property
    def instructions(self):
        """Frozen factory/workshop guidance, independent from customer params."""
        return self._instructions

    @property
    def job_id(self):
        return self._job_id

    @property
    def attempt(self):
        return self._attempt

    @property
    def card_attributes(self):
        return self._card_attributes

    @property
    def entitlements(self):
        return self._entitlements

    @property
    def revision(self):
        return self._revision

    @property
    def deliveries(self):
        return self._deliveries

    @property
    def last_delivery(self):
        return self._last_delivery

    @property
    def idempotency_key(self):
        """Stable task identity for payment and one-time external side effects."""
        return self.job_id

    @property
    def delivery_idempotency_key(self):
        """Stable content version identity, independent of technical retry count."""
        if self.job_id is None:
            return None
        return f"{self.job_id}:revision:{self.revision['current']}"

    @staticmethod
    def _message(value):
        if not isinstance(value, str) or len(value) > 1000:
            raise ProcessorError("invalid_progress_message")
        return value

    def define_steps(self, plan, message=""):
        if self.steps or self.completed_steps:
            raise ProcessorError("progress_plan_frozen")
        plan = _step_plan(plan)
        message = self._message(message)
        self._steps = self._freeze(plan)
        self._emit(
            {
                "kind": "progress",
                "progress_steps": plan,
                "progress": 0,
                "completed_steps": [],
                "message": message,
            }
        )

    def progress(self, percent=None, message="", *, completed_steps=None):
        if percent is not None and (type(percent) is not int or not 0 <= percent <= 99):
            raise ProcessorError("invalid_progress")
        if percent is None and completed_steps is None:
            raise ProcessorError("invalid_progress")
        payload = {"kind": "progress", "message": self._message(message)}
        if percent is not None:
            payload["progress"] = percent
        if completed_steps is not None:
            completed = _completed_steps(
                completed_steps, self.steps, self.completed_steps
            )
            self._completed = completed
            payload["completed_steps"] = list(completed)
        self._emit(payload)


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

_EXTENSIONS = MappingProxyType(
    {
        "csv_summary": csv_summary,
        "json_formatter": json_formatter,
        "text_cleanup": text_cleanup,
        "document_template": document_template,
    }
)


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
                _configuration(_MESSAGE, default="", secret=False),
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
                _configuration(
                    _TEMPLATE,
                    default="你好，$name！\n你的商品已准备好。",
                    secret=False,
                )
            ],
        },
        **{key: module.SPEC for key, module in _EXTENSIONS.items()},
    }
)


def get_spec(processor_id: str) -> dict:
    """Return a copy: caller changes cannot alter the running registry."""
    if not isinstance(processor_id, str) or processor_id not in _SPECS:
        raise ProcessorError("unknown_processor")
    result = copy.deepcopy(_SPECS[processor_id])
    result["shop_configuration"] = copy.deepcopy(result["configuration"])
    result["progress_steps"] = [
        {
            "id": "validate_input",
            "label": {"zh-CN": "核对信息", "en": "Validate inputs"},
        },
        {
            "id": "prepare_delivery",
            "label": {"zh-CN": "生成交付", "en": "Prepare delivery"},
        },
    ]
    return result


def catalog() -> list[dict]:
    return [get_spec(processor_id) for processor_id in _SPECS]


def _strings(
    values,
    fields,
    *,
    defaults=False,
    allow_incomplete=False,
    preserve_whitespace=False,
):
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
        if field["type"] != "textarea" and not preserve_whitespace:
            value = value.strip()
        if field["required"] and not value.strip() and not allow_incomplete:
            raise ProcessorError("required_field", key)
        if len(value) > field.get("max_length", 10000):
            raise ProcessorError("value_too_long", key)
        if (
            field["type"] == "select"
            and value
            and value not in {option["value"] for option in field["options"]}
        ):
            raise ProcessorError("invalid_option", key)
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
    """Validate shop settings, optionally permitting an unfinished draft.

    Optional defaults declared by the processor are applied. Incomplete mode
    permits empty required values; it never skips validation of supplied values.
    """
    spec = get_spec(processor_id)
    result = _strings(
        configuration,
        spec["configuration"],
        defaults=True,
        allow_incomplete=allow_incomplete,
        preserve_whitespace=processor_id in _EXTENSIONS,
    )
    if processor_id == "resource_link":
        _https_url(result["resource_url"])
    elif processor_id == "personalized_text":
        _template(result["template"])
    else:
        result = _EXTENSIONS[processor_id].validate_configuration(
            result, allow_incomplete=allow_incomplete
        )
    return result


def _resource_link(params, configuration):
    return {
        "resource_url": configuration["resource_url"],
        "message": configuration["message"],
    }


def _personalized_text(params, configuration):
    return {"content": _template(configuration["template"]).substitute(params)}


_HANDLERS = MappingProxyType(
    {
        "resource_link": _resource_link,
        "personalized_text": _personalized_text,
        **{key: module.process for key, module in _EXTENSIONS.items()},
    }
)


def validate_parameters(processor_id: str, params) -> dict[str, str]:
    """Validate customer input before accepting a job as well as at runtime."""
    spec = get_spec(processor_id)
    values = _strings(
        params, spec["parameters"], preserve_whitespace=processor_id in _EXTENSIONS
    )
    if processor_id == "personalized_text" and len(values["name"]) > 200:
        raise ProcessorError("value_too_long", "name")
    if processor_id in _EXTENSIONS:
        values = _EXTENSIONS[processor_id].validate_parameters(values)
    return values


def run(
    processor_id: str, params, configuration, *, context: ProcessorContext | None = None
) -> dict:
    """Run only an explicit, reviewed handler with code-defined inputs/outputs."""
    spec = get_spec(processor_id)
    values = validate_parameters(processor_id, params)
    settings = validate_configuration(processor_id, configuration)
    if context is None:
        context = ProcessorContext()
    elif not isinstance(context, ProcessorContext):
        raise ProcessorError("invalid_processor_context")
    if not context.steps:
        context.define_steps(spec["progress_steps"], "开始处理")
    own_plan = [step["id"] for step in context.steps] == [
        step["id"] for step in spec["progress_steps"]
    ]
    first_done = list(context.completed_steps)
    if own_plan and "validate_input" not in first_done:
        first_done.append("validate_input")
    context.progress(
        50, "已核对商品信息", completed_steps=first_done if own_plan else None
    )
    output = _HANDLERS[processor_id](values, settings)
    # Validate the declared result even though the handler itself is trusted.
    fields = [dict(field, max_length=100000) for field in spec["outputs"]]
    output = _strings(output, fields)
    if processor_id == "resource_link":
        _https_url(output["resource_url"])
    try:
        result_bytes = len(
            json.dumps(
                {"kind": "result", "state": "succeeded", "output": output},
                ensure_ascii=False,
            ).encode("utf-8")
        )
    except UnicodeError:
        raise ProcessorError("invalid_output_text") from None
    if result_bytes > 100000:
        raise ProcessorError("output_too_large")
    context.progress(
        99,
        "交付内容已准备好",
        completed_steps=[step["id"] for step in context.steps] if own_plan else None,
    )
    return {"status": "succeeded", "output": output}


@dataclass(frozen=True)
class Processor:
    id: str

    @property
    def spec(self) -> dict:
        return get_spec(self.id)

    def run(
        self, params, configuration, *, context: ProcessorContext | None = None
    ) -> dict:
        return run(self.id, params, configuration, context=context)


def get_processor(processor_id: str) -> Processor:
    get_spec(processor_id)
    return Processor(processor_id)
