"""Factory/workshop context and strict stdin compatibility regression cases."""

import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path

from extore_processors import ProcessorContext, ProcessorError, run

ROOT = Path(__file__).resolve().parents[1]


def guidance(**overrides):
    value = {
        "schema": "extore.work-instructions.v1",
        "shop_id": "shop-a",
        "product_id": "product-a",
        "factory_slogan": "工厂要求\nReview before delivery.",
        "workshop_slogan": "车间要求：保持来源可追踪。",
        **overrides,
    }
    raw = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return {**value, "revision": hashlib.sha256(raw.encode("utf-8")).hexdigest()}


def context(instructions):
    return ProcessorContext(
        shop_context={"shop_id": "shop-a"},
        product_id="product-a",
        instructions=instructions,
    )


class WorkInstructionTests(unittest.TestCase):
    def test_legacy_empty_context_remains_compatible(self):
        self.assertEqual(ProcessorContext().instructions, {})
        for value in (None, {}):
            self.assertEqual(context(value).instructions, {})

    def test_context_is_a_copied_readonly_snapshot_with_canonical_unicode_hash(self):
        value = guidance()
        frozen = context(value)
        value["factory_slogan"] = "Changed after construction"
        self.assertEqual(
            frozen.instructions["factory_slogan"], guidance()["factory_slogan"]
        )
        self.assertEqual(frozen.instructions["revision"], guidance()["revision"])
        with self.assertRaises(TypeError):
            frozen.instructions["factory_slogan"] = "Replacement"
        with self.assertRaises(AttributeError):
            frozen.instructions = {}

    def test_scope_is_checked_against_independent_server_metadata(self):
        for value in (
            guidance(shop_id="shop-b"),
            guidance(product_id="product-b"),
        ):
            with self.subTest(value=value), self.assertRaises(ProcessorError) as error:
                context(value)
            self.assertEqual(error.exception.code, "work_instructions_scope_mismatch")
        with self.assertRaises(ProcessorError):
            ProcessorContext(instructions=guidance())
        with self.assertRaises(ProcessorError):
            ProcessorContext(
                shop_context={"shop_id": "shop-a"}, instructions=guidance()
            )

    def test_schema_unknown_keys_bounds_and_revision_fail_without_echoing_text(self):
        invalid = [
            [],
            "private-guidance",
            {"factory_slogan": "private-guidance"},
            guidance(schema="other.schema"),
            guidance(extra="private-guidance"),
            guidance(factory_slogan="private-guidance" * 4000),
            guidance(workshop_slogan=7),
            guidance(factory_slogan="private-guidance\x00"),
            guidance(factory_slogan="private-guidance\ud800"),
            {**guidance(), "revision": "f" * 64},
            {**guidance(), "revision": "A" * 64},
            {**guidance(), "workshop_slogan": "Changed after hashing"},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ProcessorError) as error:
                context(value)
            self.assertNotIn("private-guidance", str(error.exception))
        self.assertEqual(
            len(
                context(guidance(factory_slogan="文" * 4000)).instructions[
                    "factory_slogan"
                ]
            ),
            4000,
        )

    def test_slogans_are_never_executed_interpolated_or_copied_to_progress(self):
        values = []
        text = "private-guidance ${name} __import__('os').system('false')"
        frozen = ProcessorContext(
            shop_context={"shop_id": "shop-a"},
            product_id="product-a",
            instructions=guidance(factory_slogan=text),
            emit=values.append,
        )
        result = run(
            "personalized_text",
            {"name": "Alice"},
            {"template": "Hello $name"},
            context=frozen,
        )
        self.assertEqual(result["output"]["content"], "Hello Alice")
        self.assertEqual(frozen.instructions["factory_slogan"], text)
        self.assertNotIn("private-guidance", json.dumps(values))
        self.assertNotIn("private-guidance", json.dumps(result))


class WorkInstructionStdinTests(unittest.TestCase):
    def invoke(self, payload):
        return subprocess.run(
            [sys.executable, "-m", "extore_processors", "personalized_text"],
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True,
            cwd=ROOT,
            check=False,
        )

    def payload(self):
        return {
            "params": {"name": "Alice"},
            "configuration": {"template": "Hello $name"},
            "shop_context": {"shop_id": "shop-a"},
            "product_id": "product-a",
            "instructions": guidance(),
        }

    def test_strict_envelope_accepts_scoped_instructions_without_reflecting_them(self):
        result = self.invoke(self.payload())
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, b"")
        output = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(output[-1]["output"]["content"], "Hello Alice")
        self.assertNotIn("instructions", json.dumps(output))
        self.assertNotIn("factory_slogan", json.dumps(output))

    def test_missing_independent_scope_or_changed_revision_fails_before_progress(self):
        for field in ("product_id", "shop_context", "revision"):
            payload = self.payload()
            if field == "revision":
                payload["instructions"]["revision"] = "0" * 64
            else:
                del payload[field]
            result = self.invoke(payload)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, b"")
            self.assertNotIn(b"Review before delivery", result.stderr)

    def test_optional_null_and_empty_instructions_preserve_old_protocol(self):
        for value in (None, {}):
            payload = self.payload()
            payload["instructions"] = value
            del payload["product_id"]
            result = self.invoke(payload)
            self.assertEqual(result.returncode, 0)

    def test_customer_params_do_not_supply_trusted_instructions(self):
        payload = self.payload()
        payload["params"]["instructions"] = "private-customer-guidance"
        result = self.invoke(payload)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b"")
        self.assertNotIn(b"private-customer-guidance", result.stderr)


if __name__ == "__main__":
    unittest.main()
