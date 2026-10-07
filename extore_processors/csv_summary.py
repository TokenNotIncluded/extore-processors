"""Bounded offline CSV statistics; customer values are data, never formulas."""

import csv
import html
import io
import json
import re
import unicodedata
from collections.abc import Mapping
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext

from .schema import ProcessorError, configuration, field, select

MAX_INPUT_LENGTH = 10000
MAX_DATA_ROWS = 500
MAX_RECORDS = 1001
MAX_COLUMNS = 30
MAX_GROUPS = 50
MAX_HEADER_LENGTH = 100
MAX_GROUP_LENGTH = 200
MAX_OUTPUT_LENGTH = 100000
# This guard precision relies on the enforced input bounds: at most 500 values,
# 30 significant digits and magnitudes between 1e-30 and 1e12 (or zero).
# Tiny contributions can decide which side of a 12-digit rounding boundary a
# large standard deviation falls on; 80 digits discard such contributions.
CALCULATION_PRECISION = 200
DISPLAY_PRECISION = 12
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE]([+-]?[0-9]+))?\Z")

SPEC = {
    "id": "csv_summary",
    "schema_version": 1,
    "name": {"zh-CN": "CSV 数据统计", "en": "CSV data summary"},
    "description": {
        "zh-CN": "粘贴实验或业务 CSV，统计一个数值列，并可按另一列分组。仅计算提供的数据，不检索、不执行公式、不推断因果或编造结论。",
        "en": "Summarize a numeric column in pasted experiment or business CSV, optionally by group. Uses only supplied data; no browsing, formula execution, causal inference or invented conclusions.",
    },
    "delivery": "content",
    "parameters": [
        field(
            "csv_text",
            "CSV 数据",
            "CSV data",
            kind="textarea",
            zh_help=(
                "首行为表头，以英文逗号分隔，含逗号或换行的字段须用双引号包围。"
                "最多 10000 字符、500 数据行、30 列；空白记录忽略，列数必须一致。"
                "不要粘贴密码、密钥或无需统计的隐私数据。仅处理文本，不执行单元格中的公式。"
            ),
            en_help=(
                "Use a header row and comma-separated fields; quote fields containing commas or line breaks. "
                "Limits: 10000 characters, 500 data rows and 30 columns. Blank records are ignored; column counts must match. "
                "Do not paste credentials or unnecessary personal data. Cells are text; formulas are never executed."
            ),
        ),
        field(
            "value_column",
            "数值列表头",
            "Numeric column header",
            zh_help=(
                "填写完整表头，大小写需一致。数值支持 ASCII 十进制与科学计数法，"
                "最多 30 位有效数字，非零绝对值须在 1e-30 到 1e12 之间，指数绝对值不超过 30。"
                "空值不参与统计；货币符号、千位逗号、NaN、Infinity 和公式不是有效数值。"
            ),
            en_help=(
                "Enter the complete, case-sensitive header. ASCII decimal and scientific notation are accepted, "
                "with at most 30 significant digits, nonzero magnitude from 1e-30 to 1e12 and exponent magnitude at most 30. "
                "Empty cells are excluded; currency signs, thousands separators, NaN, Infinity and formulas are invalid numbers."
            ),
        ),
        field(
            "group_column",
            "分组列表头（可选）",
            "Group column header (optional)",
            required=False,
            zh_help="留空统计全部数据；填写完整表头后另附各组统计。最多 50 组，每个分组值最多 200 字符；空字符串也是独立分组。",
            en_help="Leave blank for an overall summary, or enter an exact header to add group statistics. Up to 50 groups and 200 characters per group value; an empty string is a separate group.",
        ),
    ],
    "outputs": [
        field(
            "report",
            "统计报告",
            "Statistical report",
            kind="textarea",
            zh_help="列出真实有效样本数、空值数、无效值数、均值、最小/最大值，以及总体和样本标准差。统计值显示最多 12 位有效数字，不提供因果或显著性判断。",
            en_help="Reports valid, missing and invalid counts, mean, minimum, maximum, and population and sample standard deviations. Calculated statistics display up to 12 significant digits; no causal or significance claims.",
        ),
        field(
            "summary",
            "机器可读统计 JSON",
            "Machine-readable summary JSON",
            kind="textarea",
            required=False,
            zh_help="JSON 文本；十进制统计值以字符串保存，缺少足够样本时为 null。不会回显跳过的无效单元格。",
            en_help="JSON text with decimal values represented as strings and null when there are insufficient samples. Skipped invalid cells are not echoed.",
        ),
    ],
    "configuration": [
        configuration(
            select(
                "invalid_policy",
                "无效数值处理",
                "Invalid numeric value policy",
                [
                    ("reject", "拒绝处理并提示修正", "Reject and request correction"),
                    ("skip", "跳过并计数", "Skip and count"),
                ],
                required=False,
                zh_help="默认拒绝无效数值。跳过时报告会明确排除数量；空值始终排除并单独计数。CSV 结构错误、超限和全部无有效数值始终拒绝。",
                en_help="Invalid numbers are rejected by default. Skip mode reports how many were excluded; missing values are always excluded and counted separately. Structural errors, exceeded limits and no valid numeric values always fail.",
            ),
            max_length=6,
            default="reject",
            secret=False,
        ),
    ],
}


def _safe_text(value, key):
    if not isinstance(value, str):
        raise ProcessorError("expected_string", key)
    if any(
        unicodedata.category(char) in {"Cf", "Cs"}
        or (unicodedata.category(char) == "Cc" and char not in "\t\r\n")
        for char in value
    ):
        raise ProcessorError("csv_unsafe_characters", key)
    return value


def _validate_quotes(value):
    """Enforce comma CSV quoting, including quotes in otherwise unquoted cells."""
    quoted = after_quote = False
    at_start = True
    index = 0
    while index < len(value):
        char = value[index]
        if quoted:
            if char == '"':
                if index + 1 < len(value) and value[index + 1] == '"':
                    index += 2
                    continue
                quoted = False
                after_quote = True
            index += 1
            continue
        if after_quote:
            if char not in ",\r\n":
                raise ProcessorError("csv_invalid_structure", "csv_text")
            after_quote = False
            at_start = True
        elif char == '"':
            if not at_start:
                raise ProcessorError("csv_invalid_structure", "csv_text")
            quoted = True
            at_start = False
        elif char in ",\r\n":
            at_start = True
        else:
            at_start = False
        index += 1
    if quoted:
        raise ProcessorError("csv_invalid_structure", "csv_text")


def _read(values):
    text = values["csv_text"]
    text = text.removeprefix("\ufeff")
    _safe_text(text, "csv_text")
    _validate_quotes(text)
    records = []
    blank_records = 0
    record_count = 0
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        for row in reader:
            record_count += 1
            if record_count > MAX_RECORDS:
                raise ProcessorError("csv_too_many_records", "csv_text")
            if not row:
                blank_records += 1
                continue
            if len(records) > MAX_DATA_ROWS:
                raise ProcessorError("csv_too_many_rows", "csv_text")
            records.append(row)
    except (csv.Error, UnicodeError):
        raise ProcessorError("csv_invalid_structure", "csv_text") from None
    if not records:
        raise ProcessorError("csv_empty_input", "csv_text")
    headers = [unicodedata.normalize("NFC", header.strip()) for header in records[0]]
    if not 1 <= len(headers) <= MAX_COLUMNS:
        raise ProcessorError("csv_too_many_columns", "csv_text")
    if any(not header or len(header) > MAX_HEADER_LENGTH for header in headers):
        raise ProcessorError("csv_invalid_header", "csv_text")
    canonical = [unicodedata.normalize("NFKC", header).casefold() for header in headers]
    if len(set(canonical)) != len(headers):
        raise ProcessorError("csv_duplicate_headers", "csv_text")
    rows = records[1:]
    if not rows:
        raise ProcessorError("csv_no_data_rows", "csv_text")
    if any(len(row) != len(headers) for row in rows):
        raise ProcessorError("csv_inconsistent_columns", "csv_text")
    try:
        value_index = headers.index(values["value_column"])
    except ValueError:
        raise ProcessorError("csv_unknown_column", "value_column") from None
    group_index = None
    if values["group_column"]:
        try:
            group_index = headers.index(values["group_column"])
        except ValueError:
            raise ProcessorError("csv_unknown_column", "group_column") from None
        groups = set()
        for row in rows:
            group_value = row[group_index]
            if len(group_value) > MAX_GROUP_LENGTH:
                raise ProcessorError("csv_group_value_too_long", "group_column")
            groups.add(group_value)
            if len(groups) > MAX_GROUPS:
                raise ProcessorError("csv_too_many_groups", "group_column")
    return headers, rows, value_index, group_index, blank_records


def validate_parameters(values):
    if not isinstance(values, Mapping):
        raise ProcessorError("expected_object")
    if set(values) - {"csv_text", "value_column", "group_column"}:
        raise ProcessorError("unknown_fields")
    result = {}
    for key in ("csv_text", "value_column", "group_column"):
        value = values.get(key, "")
        if not isinstance(value, str):
            raise ProcessorError("expected_string", key)
        if len(value) > MAX_INPUT_LENGTH:
            raise ProcessorError("value_too_long", key)
        if key != "csv_text":
            value = unicodedata.normalize("NFC", _safe_text(value, key).strip())
        if key != "group_column" and not value.strip():
            raise ProcessorError("required_field", key)
        result[key] = value
    _read(result)
    return result


def validate_configuration(settings, *, allow_incomplete=False):
    if not isinstance(settings, Mapping):
        raise ProcessorError("expected_object")
    if set(settings) - {"invalid_policy"}:
        raise ProcessorError("unknown_fields")
    policy = settings.get("invalid_policy", "reject")
    if not isinstance(policy, str):
        raise ProcessorError("expected_string", "invalid_policy")
    policy = policy.strip()
    if not policy and allow_incomplete:
        return {"invalid_policy": "reject"}
    if policy not in {"reject", "skip"}:
        raise ProcessorError("csv_invalid_policy", "invalid_policy")
    return {"invalid_policy": policy}


def _number(text):
    text = text.strip()
    if not text or len(text) > 128:
        return None
    match = _NUMBER.fullmatch(text)
    if not match or (match.group(1) and abs(int(match.group(1))) > 30):
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    if (
        not value.is_finite()
        or len(value.as_tuple().digits) > 30
        or abs(value) > Decimal("1e12")
        or (value != 0 and abs(value) < Decimal("1e-30"))
    ):
        return None
    return value


def _decimal_text(value, *, rounded=True):
    if value is None:
        return None
    if not value:
        return "0"
    with localcontext() as context:
        context.prec = DISPLAY_PRECISION if rounded else CALCULATION_PRECISION
        context.rounding = ROUND_HALF_EVEN
        return str(value.normalize())


def _stats(values, missing, invalid):
    count = len(values)
    with localcontext() as context:
        context.prec = CALCULATION_PRECISION
        context.rounding = ROUND_HALF_EVEN
        mean = sum(values, Decimal(0)) / count if count else None
        squared = (
            sum(((value - mean) ** 2 for value in values), Decimal(0))
            if count
            else None
        )
        population = (squared / count).sqrt() if count else None
        sample = (squared / (count - 1)).sqrt() if count > 1 else None
        return {
            "rows": count + missing + invalid,
            "valid_count": count,
            "missing_count": missing,
            "invalid_count": invalid,
            "mean": _decimal_text(mean),
            "minimum": _decimal_text(min(values), rounded=False) if count else None,
            "maximum": _decimal_text(max(values), rounded=False) if count else None,
            "population_standard_deviation": _decimal_text(population),
            "sample_standard_deviation": _decimal_text(sample),
        }


def _markdown(value):
    value = html.escape(value, quote=False)
    return (
        re.sub(r"([\\`*_{}\[\]()#+.!|~\-])", r"\\\1", value)
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )


def _report(summary):
    overall = summary["overall"]
    lines = [
        "# CSV 数据统计 / CSV data summary",
        "",
        f"数值列 / Numeric column：{_markdown(json.dumps(summary['value_column'], ensure_ascii=False))}",
        f"分组列 / Group column：{_markdown(json.dumps(summary['group_column'], ensure_ascii=False)) if summary['group_column'] is not None else '无 / None'}",
        f"无效值策略 / Invalid value policy：{summary['invalid_policy']}",
        f"数据行 / Data rows：{summary['data_rows']}；忽略空记录 / Ignored blank records：{summary['ignored_blank_records']}",
        "",
        "| 范围 / Scope | 有效 / Valid | 空值 / Missing | 无效 / Invalid | 均值 / Mean | 最小 / Min | 最大 / Max | 总体标准差 / Population SD | 样本标准差 / Sample SD |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    scopes = [("全部 / Overall", overall)] + [
        (_markdown(json.dumps(group["value"], ensure_ascii=False)), group["statistics"])
        for group in summary["groups"]
    ]
    keys = [
        "valid_count",
        "missing_count",
        "invalid_count",
        "mean",
        "minimum",
        "maximum",
        "population_standard_deviation",
        "sample_standard_deviation",
    ]
    for label, stats in scopes:
        lines.append(
            "| "
            + label
            + " | "
            + " | ".join(
                str(stats[key]) if stats[key] is not None else "—" for key in keys
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## 计算口径 / Calculation notes",
            "",
            "- 有效样本数 n 仅包含数值列中的有效数字。空值和跳过的无效值均不进入计算；分组统计使用同一规则。",
            "- n counts valid numeric values only. Missing and skipped invalid values are excluded from all calculations, including group statistics.",
            "- 总体标准差 = sqrt(sum((x − mean)²) / n)；样本标准差 = sqrt(sum((x − mean)²) / (n − 1))。n = 1 时总体标准差为 0，样本标准差不可用；n = 0 的分组统计不可用。",
            "- Population SD = sqrt(sum((x − mean)²) / n); sample SD uses n − 1. For n = 1, population SD is 0 and sample SD is unavailable. Groups with n = 0 have no numeric statistics.",
            f"- 在最多 500 行、30 位有效数字、非零绝对值 1e-30 到 1e12 的输入范围内，Decimal 使用 {CALCULATION_PRECISION} 位计算精度，保留可能影响大数舍入边界的微小贡献。均值和标准差显示最多 {DISPLAY_PRECISION} 位有效数字，使用半偶舍入。最小值与最大值保留输入数值精度。",
            f"- Within the enforced bounds of 500 rows, 30 significant input digits and nonzero magnitudes from 1e-30 to 1e12, Decimal calculations use {CALCULATION_PRECISION}-digit precision to retain tiny contributions that can affect large-number rounding boundaries. Mean and SD display up to {DISPLAY_PRECISION} significant digits using half-even rounding; extrema retain the supplied numeric precision.",
            "- 只统计提交的数据；没有进行外部检索，也没有检验显著性或推断因果。分组值保留原始字符串，包括空字符串和空格差异。",
            "- Only supplied data were summarized. No external retrieval, significance testing or causal inference was performed. Group values retain their original strings, including empty strings and spacing differences.",
        ]
    )
    return "\n".join(lines)


def process(params, configuration):
    values = validate_parameters(params)
    settings = validate_configuration(configuration)
    headers, rows, value_index, group_index, blank_records = _read(values)
    all_values = []
    missing = invalid = 0
    groups = {}
    with localcontext() as context:
        context.prec = CALCULATION_PRECISION
        for row in rows:
            group_value = row[group_index] if group_index is not None else None
            bucket = groups.setdefault(
                group_value, {"values": [], "missing": 0, "invalid": 0}
            )
            if not row[value_index].strip():
                missing += 1
                bucket["missing"] += 1
                continue
            value = _number(row[value_index])
            if value is None:
                if settings["invalid_policy"] == "reject":
                    raise ProcessorError("csv_invalid_numeric_value", "csv_text")
                invalid += 1
                bucket["invalid"] += 1
                continue
            all_values.append(value)
            bucket["values"].append(value)
    if not all_values:
        raise ProcessorError("csv_no_numeric_values", "csv_text")
    summary = {
        "schema": "extore.csv-summary.v1",
        "value_column": headers[value_index],
        "group_column": headers[group_index] if group_index is not None else None,
        "invalid_policy": settings["invalid_policy"],
        "data_rows": len(rows),
        "ignored_blank_records": blank_records,
        "calculation_precision": CALCULATION_PRECISION,
        "display_significant_digits": DISPLAY_PRECISION,
        "rounding": "ROUND_HALF_EVEN",
        "overall": _stats(all_values, missing, invalid),
        "groups": [
            {
                "value": group_value,
                "statistics": _stats(
                    bucket["values"], bucket["missing"], bucket["invalid"]
                ),
            }
            for group_value, bucket in groups.items()
        ]
        if group_index is not None
        else [],
    }
    output = {
        "report": _report(summary),
        "summary": json.dumps(
            summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
    }
    if any(len(value) > MAX_OUTPUT_LENGTH for value in output.values()):
        raise ProcessorError("csv_output_too_large")
    return output
