"""Single-job JSONL protocol: validated progress, then exactly one result."""

import json
import sys

from .catalog import ProcessorContext, ProcessorError, run
from .delivery_context import INITIAL_REVISION

MAX_INPUT_BYTES = 200000


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise ProcessorError("expected_processor_id")
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ProcessorError("input_too_large")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise ProcessorError("invalid_json") from None
        if (
            not isinstance(value, dict)
            or not {"params", "configuration"} <= set(value)
            or set(value)
            - {
                "params",
                "configuration",
                "variant",
                "steps",
                "completed_steps",
                "shop_context",
                "environment",
                "product_id",
                "instructions",
                "job_id",
                "attempt",
                "card_attributes",
                "entitlements",
                "revision",
                "deliveries",
                "last_delivery",
            }
            or ("variant" in value and not isinstance(value["variant"], dict))
            or ("steps" in value and not isinstance(value["steps"], list))
            or (
                "completed_steps" in value
                and not isinstance(value["completed_steps"], list)
            )
        ):
            raise ProcessorError("invalid_job_envelope")

        def emit(progress):
            print(json.dumps(progress, ensure_ascii=False), flush=True)

        context = ProcessorContext(
            steps=value.get("steps", []),
            completed_steps=value.get("completed_steps", []),
            shop_context=value.get("shop_context"),
            environment=value.get("environment", {}),
            product_id=value.get("product_id"),
            instructions=value.get("instructions"),
            job_id=value.get("job_id"),
            attempt=value.get("attempt"),
            card_attributes=value.get("card_attributes", {}),
            entitlements=value.get("entitlements"),
            revision=value.get("revision", INITIAL_REVISION),
            deliveries=value.get("deliveries", []),
            last_delivery=value.get("last_delivery"),
            emit=emit,
        )
        result = run(
            sys.argv[1], value["params"], value["configuration"], context=context
        )
        print(
            json.dumps(
                {
                    "kind": "result",
                    "state": result["status"],
                    "output": result["output"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0
    except ProcessorError as exc:
        error = {"error": exc.code}
        if exc.field:
            error["field"] = exc.field
        print(json.dumps(error), file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001 - handler exceptions may contain credentials
        # Keep implementation failures and any credentials out of logs/stdout.
        print(json.dumps({"error": "processor_failed"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
