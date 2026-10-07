"""Bounded immutable metadata; none of these values are executable instructions."""

import math
from collections.abc import Mapping
from types import MappingProxyType

MAX_SAFE_INTEGER = 2**53 - 1
MAX_REVISIONS = 1000
INITIAL_REVISION = MappingProxyType({"current": 0, "message": "", "is_revision": False})


def _text(value, limit, *, nonempty=False):
    if not isinstance(value, str) or len(value) > limit:
        return False
    if nonempty and not value.strip():
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _integer(value, minimum=0, maximum=MAX_SAFE_INTEGER):
    return type(value) is int and minimum <= value <= maximum


def freeze_attributes(value, error):
    if not isinstance(value, Mapping) or len(value) > 20:
        raise error("invalid_card_attributes")
    result = {}
    for key, item in value.items():
        valid = item is None or type(item) is bool
        if isinstance(item, str):
            valid = _text(item, 1000)
        elif type(item) is int:
            valid = abs(item) <= MAX_SAFE_INTEGER
        elif type(item) is float:
            valid = math.isfinite(item) and (
                abs(item) <= MAX_SAFE_INTEGER or not item.is_integer()
            )
        if not _text(key, 100, nonempty=True) or not valid:
            raise error("invalid_card_attributes")
        result[key] = item
    return MappingProxyType(result)


def freeze_revision(value, error):
    if (
        not isinstance(value, Mapping)
        or set(value) != {"current", "message", "is_revision"}
        or not _integer(value["current"], maximum=MAX_REVISIONS)
        or not _text(value["message"], 10000)
        or type(value["is_revision"]) is not bool
        or value["is_revision"] != (value["current"] > 0)
    ):
        raise error("invalid_delivery_context")
    return MappingProxyType(dict(value))


def freeze_entitlements(value, attributes, revision, error):
    if value is None:
        return None
    fields = {
        "attribute_key",
        "label",
        "total",
        "used",
        "remaining",
        "can_request",
        "reason",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise error("invalid_delivery_context")
    key, labels = value["attribute_key"], value["label"]
    if (
        not _text(key, 100, nonempty=True)
        or key != key.strip()
        or not isinstance(labels, Mapping)
        or not 1 <= len(labels) <= 20
        or any(
            not _text(locale, 40, nonempty=True) or not _text(label, 200, nonempty=True)
            for locale, label in labels.items()
        )
        or not all(
            _integer(value[name], maximum=MAX_REVISIONS)
            for name in ("total", "used", "remaining")
        )
        or type(attributes.get(key, 0)) is not int
        or attributes.get(key, 0) != value["total"]
        or value["used"] != revision["current"]
        or value["used"] + value["remaining"] != value["total"]
        or type(value["can_request"]) is not bool
        or value["reason"] is not None
        and not _text(value["reason"], 100, nonempty=True)
        or value["can_request"]
        and value["reason"] is not None
    ):
        raise error("invalid_delivery_context")
    return MappingProxyType({**value, "label": MappingProxyType(dict(labels))})


def freeze_delivery(value, error, *, detailed=False):
    fields = {"revision", "attempt", "created"}
    if detailed:
        fields |= {"revealed", "has_files"}
    if (
        not isinstance(value, Mapping)
        or set(value) != fields
        or not _integer(value["revision"], maximum=MAX_REVISIONS)
        or not _integer(value["attempt"], minimum=1)
        or type(value["created"]) not in (int, float)
        or not 0 <= value["created"] <= MAX_SAFE_INTEGER
        or detailed
        and any(type(value[name]) is not bool for name in ("revealed", "has_files"))
    ):
        raise error("invalid_delivery_context")
    return MappingProxyType(dict(value))


def freeze_deliveries(value, error):
    if not isinstance(value, (list, tuple)) or len(value) > MAX_REVISIONS + 1:
        raise error("invalid_delivery_context")
    result = tuple(freeze_delivery(item, error, detailed=True) for item in value)
    rounds = [item["revision"] for item in result]
    if rounds != sorted(set(rounds)):
        raise error("invalid_delivery_context")
    return result


def validate_identity(job_id, attempt, error):
    if job_id is not None and not _text(job_id, 100, nonempty=True):
        raise error("invalid_job_context")
    if attempt is not None and not _integer(attempt, minimum=1):
        raise error("invalid_job_context")
