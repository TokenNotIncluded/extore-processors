"""Order-preserving line cleanup without executing or interpreting text."""

from .schema import ProcessorError, configuration, field, select

MAX_TEXT_CHARACTERS = 10000
MAX_LINES = 10000
MAX_OUTPUT_BYTES = 100000

SPEC = {
    "id": "text_cleanup",
    "schema_version": 1,
    "name": {"zh-CN": "文本与名单清理", "en": "Text and list cleanup"},
    "description": {
        "zh-CN": "按行清理名单、资料条目或字幕片段：可去除首尾空白、空行和重复项，保留首次出现的顺序与原文。只处理文本，不排序、不补造内容。",
        "en": "Clean lists, source entries or subtitle excerpts line by line. Optionally trim whitespace, remove blank lines and deduplicate while keeping the first occurrence in its original order. Text only; no sorting or invented content.",
    },
    "delivery": "content",
    "parameters": [
        field(
            "text",
            "待清理文本",
            "Text to clean",
            kind="textarea",
            required=False,
            zh_help="每行一条，最多 10,000 个字符、10,000 行。可留空或仅填空白：按店铺清理设置返回空文本和实际统计。CRLF 和 CR 换行转为 LF；不改写文字，不进行 Unicode 规范化。末尾换行只表示该行结束，不额外计一行。",
            en_help="One entry per line, up to 10,000 characters and 10,000 lines. Empty or whitespace-only input is allowed and yields empty text and actual counts under the shop's cleanup settings. CRLF and CR become LF. Wording and Unicode forms are preserved. A final newline ends the last line without adding another line.",
        )
    ],
    "outputs": [
        field(
            "cleaned_text",
            "清理后的文本",
            "Cleaned text",
            kind="textarea",
            required=False,
            zh_help="按原顺序保留的条目。忽略大小写去重只影响比较，保留首次出现的文字。全部被去除时为空。",
            en_help="Retained entries in their original order. Case-insensitive comparison keeps the text of the first occurrence. Empty when every line is removed.",
        ),
        field(
            "report",
            "清理统计",
            "Cleanup report",
            kind="textarea",
            zh_help="显示原始行数、保留行数、删除的空行与重复行数量；这些数值来自本次实际处理。",
            en_help="Actual counts of input lines, retained lines, removed blank lines and removed duplicate lines.",
        ),
    ],
    "configuration": [
        configuration(
            select(
                "trim_lines",
                "去除每行首尾空白",
                "Trim each line",
                [("yes", "是", "Yes"), ("no", "否", "No")],
                zh_help="只去除行首和行尾的空白；保留行内空格与文字。",
                en_help="Remove only leading and trailing whitespace; keep whitespace within entries.",
            ),
            default="yes",
            secret=False,
            max_length=3,
        ),
        configuration(
            select(
                "remove_blank_lines",
                "删除空行",
                "Remove blank lines",
                [("yes", "是", "Yes"), ("no", "否", "No")],
                zh_help="空行包含只由空白组成的行。空行删除先于去重；未删除的空行也参与去重。",
                en_help="Blank includes whitespace-only lines. Blank removal happens before deduplication; retained blank lines also participate in deduplication.",
            ),
            default="yes",
            secret=False,
            max_length=3,
        ),
        configuration(
            select(
                "deduplicate",
                "去重方式",
                "Deduplication",
                [
                    ("exact", "完全相同", "Exact match"),
                    ("casefold", "忽略大小写", "Case-insensitive"),
                    ("none", "不去重", "Keep duplicates"),
                ],
                zh_help="按清理首尾空白后的行比较。忽略大小写使用 Unicode casefold，只用于比较，保留第一次的原文，不排序或规范化 Unicode。",
                en_help="Compare lines after optional trimming. Case-insensitive mode uses Unicode casefold only for comparison, retains the first original entry, and never sorts or normalizes Unicode.",
            ),
            default="exact",
            secret=False,
            max_length=8,
        ),
    ],
}


def _lines(text: str) -> list[str]:
    if not text:
        return []
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if normalized.endswith("\n"):
        lines.pop()
    return lines


def validate_parameters(values) -> dict[str, str]:
    text = values.get("text")
    if not isinstance(text, str):
        raise ProcessorError("expected_string", "text")
    if len(text) > MAX_TEXT_CHARACTERS:
        raise ProcessorError("value_too_long", "text")
    if "\x00" in text:
        raise ProcessorError("invalid_text", "text")
    try:
        text.encode("utf-8")
    except UnicodeError:
        raise ProcessorError("invalid_text", "text") from None
    if len(_lines(text)) > MAX_LINES:
        raise ProcessorError("too_many_lines", "text")
    return values


def validate_configuration(settings, *, allow_incomplete=False) -> dict[str, str]:
    options = {
        "trim_lines": {"yes", "no"},
        "remove_blank_lines": {"yes", "no"},
        "deduplicate": {"exact", "casefold", "none"},
    }
    for key, allowed in options.items():
        value = settings.get(key, "")
        if allow_incomplete and value == "":
            continue
        if not isinstance(value, str) or value not in allowed:
            raise ProcessorError("invalid_option", key)
    return settings


def process(params, configuration) -> dict[str, str]:
    text = params["text"]
    lines = _lines(text)
    retained = []
    seen = set()
    blank_removed = duplicate_removed = 0
    for line in lines:
        candidate = line.strip() if configuration["trim_lines"] == "yes" else line
        if configuration["remove_blank_lines"] == "yes" and not candidate.strip():
            blank_removed += 1
            continue
        comparison = (
            candidate.casefold()
            if configuration["deduplicate"] == "casefold"
            else candidate
        )
        if configuration["deduplicate"] != "none" and comparison in seen:
            duplicate_removed += 1
            continue
        seen.add(comparison)
        retained.append(candidate)
    cleaned = "\n".join(retained)
    if retained and text.endswith(("\n", "\r")):
        cleaned += "\n"
    report = (
        f"原始行数 / Input lines: {len(lines)}\n"
        f"保留行数 / Retained lines: {len(retained)}\n"
        f"删除空行 / Blank lines removed: {blank_removed}\n"
        f"删除重复行 / Duplicate lines removed: {duplicate_removed}"
    )
    if len(cleaned.encode("utf-8")) + len(report.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise ProcessorError("output_too_large")
    return {"cleaned_text": cleaned, "report": report}
