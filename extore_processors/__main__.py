"""Single-job JSON protocol. stdout contains exactly one result on success."""

import json
import sys

from .catalog import ProcessorError, run

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
            - {"params", "configuration", "variant", "steps", "completed_steps"}
            or ("variant" in value and not isinstance(value["variant"], dict))
            or ("steps" in value and not isinstance(value["steps"], list))
            or (
                "completed_steps" in value
                and not isinstance(value["completed_steps"], list)
            )
        ):
            raise ProcessorError("invalid_job_envelope")
        result = run(sys.argv[1], value["params"], value["configuration"])
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


if __name__ == "__main__":
    raise SystemExit(main())
