"""Frozen issuance attributes, revision metadata and real stdin compatibility."""

import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path

from extore_processors import ProcessorContext, ProcessorError, run

ROOT = Path(__file__).resolve().parents[1]


def metadata(current=1, attempt=2):
    return {
        "job_id": "job-a",
        "attempt": attempt,
        "card_attributes": {
            "edit_passes": 3,
            "name": "private-card-name",
            "ratio": 0.5,
            "enabled": True,
            "unset": None,
        },
        "entitlements": {
            "attribute_key": "edit_passes",
            "label": {"zh-CN": "修改次数", "en": "Revisions"},
            "total": 3,
            "used": current,
            "remaining": 3 - current,
            "can_request": False,
            "reason": "in_progress",
        },
        "revision": {
            "current": current,
            "message": "private-advice $name $(do-not-execute)",
            "is_revision": current > 0,
        },
        "deliveries": [
            {
                "revision": 0,
                "attempt": 1,
                "created": 100.5,
                "revealed": True,
                "has_files": False,
            }
        ],
        "last_delivery": {"revision": 0, "attempt": 1, "created": 100.5},
    }


class DeliveryContextTests(unittest.TestCase):
    def test_legacy_defaults_do_not_invent_task_identity_or_benefits(self):
        context = ProcessorContext()
        self.assertEqual(context.card_attributes, {})
        self.assertIsNone(context.entitlements)
        self.assertEqual(
            context.revision, {"current": 0, "message": "", "is_revision": False}
        )
        self.assertEqual(context.deliveries, ())
        self.assertIsNone(context.last_delivery)
        self.assertIsNone(context.job_id)
        self.assertIsNone(context.attempt)
        self.assertIsNone(context.idempotency_key)
        self.assertIsNone(context.delivery_idempotency_key)

    def test_metadata_is_a_defensive_deep_readonly_snapshot(self):
        value = metadata()
        context = ProcessorContext(**value)
        value["card_attributes"]["edit_passes"] = 99
        value["entitlements"]["label"]["en"] = "changed"
        value["revision"]["message"] = "changed"
        value["deliveries"][0]["revealed"] = False
        value["last_delivery"]["attempt"] = 99
        self.assertEqual(context.card_attributes["edit_passes"], 3)
        self.assertEqual(context.entitlements["label"]["en"], "Revisions")
        self.assertEqual(context.revision["message"], metadata()["revision"]["message"])
        self.assertTrue(context.deliveries[0]["revealed"])
        self.assertEqual(context.last_delivery["attempt"], 1)
        for target, name in [
            (context.card_attributes, "edit_passes"),
            (context.entitlements["label"], "en"),
            (context.revision, "message"),
            (context.deliveries[0], "revealed"),
            (context.last_delivery, "attempt"),
        ]:
            with self.subTest(name=name), self.assertRaises(TypeError):
                target[name] = "changed"
        for name in metadata():
            with self.subTest(name=name), self.assertRaises(AttributeError):
                setattr(context, name, None)

    def test_content_key_changes_per_round_and_payment_key_never_changes(self):
        initial = ProcessorContext(job_id="job-a", attempt=1)
        first = ProcessorContext(**metadata(current=1, attempt=2))
        retry = ProcessorContext(**metadata(current=1, attempt=3))
        second = ProcessorContext(**metadata(current=2, attempt=4))
        self.assertEqual(initial.delivery_idempotency_key, "job-a:revision:0")
        self.assertEqual(first.delivery_idempotency_key, "job-a:revision:1")
        self.assertEqual(retry.delivery_idempotency_key, first.delivery_idempotency_key)
        self.assertEqual(second.delivery_idempotency_key, "job-a:revision:2")
        for context in (initial, first, retry, second):
            self.assertEqual(context.idempotency_key, "job-a")

    def test_context_is_not_customer_input_or_automatic_output(self):
        progress = []
        context = ProcessorContext(**metadata(), emit=progress.append)
        result = run(
            "personalized_text",
            {"name": "Alice"},
            {"template": "Hello $name"},
            context=context,
        )
        self.assertEqual(result["output"], {"content": "Hello Alice"})
        serialized = json.dumps({"result": result, "progress": progress})
        self.assertNotIn("private-advice", serialized)
        self.assertNotIn("private-card-name", serialized)
        self.assertNotIn("do-not-execute", serialized)
        self.assertNotIn("private-advice", repr(context))

    def test_scalar_attribute_limits_do_not_echo_private_data(self):
        for attributes in (
            None,
            [],
            {"": "private-value"},
            {"x" * 101: "private-value"},
            {"name": "private-value" * 1000},
            {"name": {"private-value": 1}},
            {"name": ["private-value"]},
            {"name": "private-value\ud800"},
            {"value": float("inf")},
            {"value": float("nan")},
            {"value": 2**53},
            {"value": float(2**53)},
            {str(i): i for i in range(21)},
        ):
            with (
                self.subTest(attributes=attributes),
                self.assertRaises(ProcessorError) as error,
            ):
                ProcessorContext(card_attributes=attributes)
            self.assertNotIn("private-value", str(error.exception))
        boundary = ProcessorContext(card_attributes={"safe": 2**53 - 1})
        self.assertEqual(boundary.card_attributes["safe"], 2**53 - 1)

    def test_malformed_metadata_is_strict_and_safe(self):
        mutations = [
            ("job_id", []),
            ("job_id", ""),
            ("attempt", True),
            ("attempt", 0),
            ("revision", None),
            (
                "revision",
                {"current": 1, "message": "private-value", "is_revision": False},
            ),
            ("revision", {"current": True, "message": "", "is_revision": True}),
            ("revision", {"current": 1001, "message": "", "is_revision": True}),
            ("revision", {"current": 1, "message": "x" * 10001, "is_revision": True}),
            ("revision", {**metadata()["revision"], "run": "private-value"}),
            ("entitlements", []),
            ("entitlements", {**metadata()["entitlements"], "run": "private-value"}),
            ("entitlements", {**metadata()["entitlements"], "total": True}),
            ("entitlements", {**metadata()["entitlements"], "total": 4}),
            ("entitlements", {**metadata()["entitlements"], "remaining": 3}),
            ("entitlements", {**metadata()["entitlements"], "used": 2}),
            ("entitlements", {**metadata()["entitlements"], "can_request": True}),
            ("entitlements", {**metadata()["entitlements"], "label": {"en": ""}}),
            ("last_delivery", {"revision": 0, "attempt": 1, "created": float("nan")}),
            (
                "last_delivery",
                {**metadata()["last_delivery"], "content": "private-value"},
            ),
            ("deliveries", None),
            ("deliveries", [metadata()["deliveries"][0]] * 2),
            ("deliveries", [{**metadata()["deliveries"][0], "has_files": 1}]),
            ("deliveries", [{**metadata()["deliveries"][0], "revision": True}]),
        ]
        for name, invalid in mutations:
            value = metadata()
            value[name] = invalid
            with (
                self.subTest(name=name, value=invalid),
                self.assertRaises(ProcessorError) as error,
            ):
                ProcessorContext(**value)
            self.assertNotIn("private-value", str(error.exception))


class EnvelopeTests(unittest.TestCase):
    def call(self, extra=None):
        return subprocess.run(
            [sys.executable, "-m", "extore_processors", "personalized_text"],
            input=json.dumps(
                {
                    "params": {"name": "Alice"},
                    "configuration": {"template": "Hello $name"},
                    **(extra or {}),
                }
            ).encode(),
            capture_output=True,
            cwd=ROOT,
            check=False,
        )

    def test_real_old_and_new_envelopes_produce_single_result_without_context_leaks(
        self,
    ):
        compact = metadata()
        del compact["deliveries"]
        for extra in ({}, metadata(current=1), metadata(current=2, attempt=4), compact):
            with self.subTest(extra=extra):
                result = self.call(extra)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, b"")
                values = [json.loads(line) for line in result.stdout.splitlines()]
                self.assertEqual(sum(value["kind"] == "result" for value in values), 1)
                self.assertEqual(values[-1]["output"], {"content": "Hello Alice"})
                self.assertNotIn(b"private-advice", result.stdout)
                self.assertNotIn(b"private-card-name", result.stdout)

    def test_nested_and_top_level_unknown_fields_and_bad_types_fail_before_progress(
        self,
    ):
        for extra in (
            {**metadata(), "execute": "private-value"},
            {**metadata(), "card_attributes": {"edit_passes": [3]}},
            {
                **metadata(),
                "entitlements": {
                    **metadata()["entitlements"],
                    "execute": "private-value",
                },
            },
            {**metadata(), "revision": None},
        ):
            with self.subTest(extra=extra):
                result = self.call(copy.deepcopy(extra))
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, b"")
                self.assertIn("error", json.loads(result.stderr))
                self.assertNotIn(b"private-value", result.stderr)


if __name__ == "__main__":
    unittest.main()
