"""Write a local contribution draft; never register or execute its code."""

import argparse
import json
import keyword
import os
import re
import sys
from pathlib import Path

# The tool imports only this checkout's reviewed, fixed registry.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from extore_processors import catalog  # noqa: E402

MODULE = '''"""Offline example: prepend a shop-owned string to customer text."""

import unicodedata
from collections.abc import Mapping

from extore_processors.schema import ProcessorError, configuration, field

SPEC = {
    "id": "__PROCESSOR_ID__",
    "schema_version": 1,
    "name": {"zh-CN": "文本前缀示例", "en": "Text prefix example"},
    "description": {
        "zh-CN": "在顾客原文前添加本店配置的普通文本；不执行内容、不访问网址。",
        "en": "Prepend shop-owned plain text without executing content or visiting URLs.",
    },
    "delivery": "content",
    "parameters": [
        field(
            "text",
            "原文",
            "Text",
            kind="textarea",
            zh_help="必填，最多 10000 个字符，保留原文与换行。",
            en_help="Required, up to 10000 characters. Text and line breaks are preserved.",
        )
    ],
    "outputs": [field("content", "交付文本", "Delivery text", kind="textarea")],
    "configuration": [
        configuration(
            field(
                "prefix",
                "文本前缀",
                "Text prefix",
                required=False,
                zh_help="普通可编辑文本，最多 200 个字符。不要在这个公开配置字段填写密钥。",
                en_help="Readable plain text, up to 200 characters. Do not put secrets here.",
            ),
            default="Hello, ",
            max_length=200,
            secret=False,
        )
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
        if len(value) > item.get("max_length", 10000):
            raise ProcessorError("value_too_long", key)
        if any(
            unicodedata.category(char) in {"Cc", "Cs"} and char not in "\\t\\r\\n"
            for char in value
        ):
            raise ProcessorError("invalid_text", key)
        if item["required"] and not value.strip() and not allow_incomplete:
            raise ProcessorError("required_field", key)
        result[key] = value
    return result


def validate_parameters(values) -> dict[str, str]:
    return _strings(values, SPEC["parameters"])


def validate_configuration(settings, *, allow_incomplete=False) -> dict[str, str]:
    return _strings(
        settings,
        SPEC["configuration"],
        defaults=True,
        allow_incomplete=allow_incomplete,
    )


def process(params, configuration) -> dict[str, str]:
    # The fixed registry calls these validators too. Keep direct development
    # calls safe without reading files, process environment, or network state.
    values = validate_parameters(params)
    settings = validate_configuration(configuration)
    return {"content": settings["prefix"] + values["text"]}
'''


TEST = '''"""Explicitly test this trusted local draft through the real registry."""

import importlib.util
import unittest
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

from extore_processors import ProcessorContext, ProcessorError

# Only this developer test loads its fixed local draft file. Extore's runtime
# must continue to use reviewed, statically registered handlers.
MODULE_FILE = "__PROCESSOR_ID__.py"
MODULE_PATH = Path(__file__).resolve().parents[1] / "extore_processors" / MODULE_FILE
MODULE_SPEC = importlib.util.spec_from_file_location(
    "local_contribution_draft", MODULE_PATH
)
draft = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(draft)
registry = import_module("extore_processors.catalog")


@contextmanager
def registered_draft():
    # Fixture changes are temporary and never modify the registry's source.
    with (
        patch.object(
            registry, "_EXTENSIONS", {**registry._EXTENSIONS, draft.SPEC["id"]: draft}
        ),
        patch.object(
            registry, "_SPECS", {**registry._SPECS, draft.SPEC["id"]: draft.SPEC}
        ),
        patch.object(
            registry,
            "_HANDLERS",
            {**registry._HANDLERS, draft.SPEC["id"]: draft.process},
        ),
    ):
        yield


class DraftTests(unittest.TestCase):
    def invoke(self, params, settings, context=None):
        with registered_draft():
            return registry.run(draft.SPEC["id"], params, settings, context=context)

    def test_schema_and_defaults_use_the_real_normalization(self):
        with registered_draft():
            spec = registry.get_spec(draft.SPEC["id"])
            self.assertIs(spec["configuration"][0]["secret"], False)
            self.assertEqual(spec["configuration"], spec["shop_configuration"])
            self.assertEqual(
                registry.validate_configuration(draft.SPEC["id"], {}),
                {"prefix": "Hello, "},
            )
        self.assertEqual(
            self.invoke({"text": "  world\\n"}, {}),
            {
                "status": "succeeded",
                "output": {"content": "Hello,   world\\n"},
            },
        )

    def test_plain_text_and_configuration_are_not_interpreted(self):
        text = "$(touch sentinel)\\n<script>alert(1)</script>"
        self.assertEqual(
            self.invoke({"text": text}, {"prefix": "$TOKEN "})["output"],
            {
                "content": "$TOKEN " + text,
            },
        )

    def test_invalid_input_and_configuration_do_not_echo_values(self):
        private = "private-value-never-log"
        for params, settings in [
            ({"text": private, "unknown": private}, {}),
            ({"text": "x"}, {"unknown": private}),
            ({"text": private * 1000}, {}),
            ({"text": "x"}, {"prefix": private * 20}),
            ({"text": private + "\\x00"}, {}),
            ({"text": "x"}, {"prefix": private + "\\ud800"}),
            ({"text": 1}, {}),
            ({"text": " "}, {}),
        ]:
            with (
                self.subTest(params_type=type(params).__name__),
                self.assertRaises(ProcessorError) as error,
            ):
                self.invoke(params, settings)
            self.assertNotIn(private, str(error.exception))

    def test_incomplete_configuration_still_validates_supplied_values(self):
        self.assertEqual(
            draft.validate_configuration({}, allow_incomplete=True),
            {"prefix": "Hello, "},
        )
        with self.assertRaises(ProcessorError):
            draft.validate_configuration({"prefix": "x" * 201}, allow_incomplete=True)

    def test_context_defines_and_freezes_the_real_progress_plan(self):
        events = []
        context = ProcessorContext(emit=events.append)
        self.invoke({"text": "world"}, {}, context)
        self.assertEqual(events[0]["progress"], 0)
        self.assertEqual(events[-1]["progress"], 99)
        self.assertEqual(
            events[-1]["completed_steps"], ["validate_input", "prepare_delivery"]
        )
        with self.assertRaises(ProcessorError) as error:
            context.define_steps(
                [{"id": "replacement", "label": {"en": "Replacement"}}]
            )
        self.assertEqual(error.exception.code, "progress_plan_frozen")

    def test_existing_different_plan_is_not_falsely_marked_complete(self):
        events = []
        context = ProcessorContext(
            steps=[
                {"id": "review", "label": {"en": "Human review"}},
                {"id": "approve", "label": {"en": "Approval"}},
            ],
            completed_steps=["review"],
            emit=events.append,
        )
        self.invoke({"text": "world"}, {}, context)
        self.assertEqual(context.completed_steps, ("review",))
        self.assertTrue(all("completed_steps" not in event for event in events))
        self.assertTrue(all("progress_steps" not in event for event in events))

    def test_real_dispatch_checks_declared_output_and_jsonl_size(self):
        for bad_output, code in [
            ({"content": "x", "unknown": "private"}, "unknown_fields"),
            ({"content": "\\n" * 50001 + "x"}, "output_too_large"),
            ({"content": "private\\ud800"}, "invalid_output_text"),
        ]:
            events = []
            context = ProcessorContext(emit=events.append)
            with (
                registered_draft(),
                patch.object(
                    registry,
                    "_HANDLERS",
                    {
                        **registry._HANDLERS,
                        draft.SPEC["id"]: lambda _params, _settings: bad_output,
                    },
                ),
                self.assertRaises(ProcessorError) as error,
            ):
                registry.run(draft.SPEC["id"], {"text": "world"}, {}, context=context)
            self.assertEqual(error.exception.code, code)
            self.assertNotIn("private", str(error.exception))
            self.assertTrue(all(event["progress"] < 99 for event in events))


if __name__ == "__main__":
    unittest.main()
'''


README = """# __PROCESSOR_ID__ contribution draft

This directory is a local development draft, not an installed processor. The
generator did not execute these files, register a handler, or change Extore.

此目录是本地贡献草稿。生成时不运行新代码、不注册处理器，也不修改 Extore。
`prefix` 是普通可回读配置，不能放账号、令牌或其他密钥。输入文本只作内容处理。

## Test explicitly / 显式测试

Use Python 3.11 or later, from the `extore-processors` checkout that generated
this draft (or an environment with that reviewed package installed):

```sh
python -m unittest discover -s /absolute/path/to/this-draft/tests -v
```

The tests load only the fixed local draft module and temporarily patch the
registry in the test process. They use the real validation, result budget and
`ProcessorContext`; they do not create a runtime plugin loader.

## Run the example explicitly / 显式运行示例

Only run local code you have reviewed. From the processor checkout, replace
the draft path below; this command returns the example's output as JSON:

```sh
python - /absolute/path/to/this-draft <<'PY'
import importlib.util
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("local_draft", root / "extore_processors/__PROCESSOR_ID__.py")
draft = importlib.util.module_from_spec(spec)
spec.loader.exec_module(draft)
value = json.loads((root / "examples/__PROCESSOR_ID__.json").read_text(encoding="utf-8"))
print(json.dumps(draft.process(value["params"], value["configuration"]), ensure_ascii=False))
PY
```

This development command calls `process(params, configuration)` directly. The
production registry, not `process`, owns frozen progress plans and the JSONL
result envelope. The unit test above exercises that production dispatch too.

## Submit for review / 提交审查

1. Copy `extore_processors/__PROCESSOR_ID__.py`, `tests/test___PROCESSOR_ID__.py`
   and `examples/__PROCESSOR_ID__.json` into the corresponding checkout folders.
2. Add a static module import and an entry to `catalog.py`'s `_EXTENSIONS`.
   Do not add dynamic module names, uploaded code or package installation.
3. Run the generated tests and the full processor tests, then open a PR.
   Extore can use the processor only after review, merge and a pinned release.

Extend `SPEC` and its validators together. Keep errors as fixed safe codes and
never include customer text, environment values or shop credentials in errors.
Change a field's `secret` classification deliberately; secrets remain write-only.
"""


def _files(processor_id):
    return {
        f"extore_processors/{processor_id}.py": MODULE.replace(
            "__PROCESSOR_ID__", processor_id
        ),
        f"tests/test_{processor_id}.py": TEST.replace("__PROCESSOR_ID__", processor_id),
        f"examples/{processor_id}.json": json.dumps(
            {"params": {"text": "world\n"}, "configuration": {"prefix": "Hello, "}},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        "README.md": README.replace("__PROCESSOR_ID__", processor_id),
    }


def generate(processor_id, output):
    if (
        not isinstance(processor_id, str)
        or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", processor_id)
        or keyword.iskeyword(processor_id)
    ):
        raise ValueError("invalid_processor_id")
    if (
        processor_id in {spec["id"] for spec in catalog()}
        or (ROOT / "extore_processors" / f"{processor_id}.py").exists()
    ):
        raise ValueError("processor_id_exists")
    path = os.fspath(output)
    if not isinstance(path, str) or "\x00" in path:
        raise ValueError("invalid_output_path")
    parts = path.split(os.sep)
    if ".." in parts:
        raise ValueError("invalid_output_path")
    parts = [part for part in parts if part not in {"", "."}]
    if not parts:
        raise ValueError("invalid_output_path")

    # Each component is opened relative to a held directory descriptor. Neither
    # parent symlinks nor a replaced final symlink are followed. mkdir is the
    # atomic no-overwrite gate, including dangling symlink destinations.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    parent_fd = os.open(os.sep if os.path.isabs(path) else ".", flags)
    output_fd = None
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        os.mkdir(parts[-1], mode=0o700, dir_fd=parent_fd)
        output_fd = os.open(parts[-1], flags, dir_fd=parent_fd)
        directories = {}
        try:
            for name, content in _files(processor_id).items():
                directory, _, filename = name.rpartition("/")
                if directory and directory not in directories:
                    os.mkdir(directory, mode=0o700, dir_fd=output_fd)
                    directories[directory] = os.open(directory, flags, dir_fd=output_fd)
                file_fd = os.open(
                    filename,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=directories.get(directory, output_fd),
                )
                with os.fdopen(file_fd, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(content)
        finally:
            for directory_fd in directories.values():
                os.close(directory_fd)
    finally:
        if output_fd is not None:
            os.close(output_fd)
        os.close(parent_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Create a local processor contribution draft without running or registering it."
    )
    parser.add_argument("processor_id", help="New ID: [a-z][a-z0-9_]{0,39}")
    parser.add_argument(
        "--output",
        required=True,
        help="New directory; its parent must exist and its path must contain no symlinks or '..'.",
    )
    args = parser.parse_args(argv)
    try:
        generate(args.processor_id, args.output)
    except ValueError as error:
        parser.exit(2, f"error: {error}\n")
    except FileExistsError:
        parser.exit(2, "error: output_exists\n")
    except OSError:
        parser.exit(2, "error: output_path_unavailable\n")
    print("Created a local draft. Nothing was executed or registered. See README.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
