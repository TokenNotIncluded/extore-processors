"""Small shared schema helpers for reviewed, offline processors."""


class ProcessorError(ValueError):
    """Safe error codes never interpolate customer input or shop settings."""

    def __init__(self, code: str, field: str | None = None):
        self.code = code
        self.field = field
        super().__init__(code + (f": {field}" if field else ""))


def field(key, zh, en, *, kind="text", required=True, zh_help="", en_help=""):
    return {
        "key": key,
        "label": {"zh-CN": zh, "en": en},
        "description": {"zh-CN": zh_help, "en": en_help},
        "collapsed": True,
        "required": required,
        "type": kind,
    }


def configuration(value, *, max_length=10000, default=None, secret=True):
    result = {**value, "secret": secret is not False, "max_length": max_length}
    if default is not None:
        result["default"] = default
    return result


def select(key, zh, en, choices, *, required=True, zh_help="", en_help=""):
    result = field(
        key,
        zh,
        en,
        kind="select",
        required=required,
        zh_help=zh_help,
        en_help=en_help,
    )
    result["options"] = [
        {"value": value, "label": {"zh-CN": cn, "en": english}}
        for value, cn, english in choices
    ]
    return result
