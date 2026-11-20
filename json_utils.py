"""Write analysis artifacts as strict JSON: no NaN or Infinity, missing means null."""

import json
import math
from pathlib import Path


def json_safe(value):
    """Return a copy with non-finite floats replaced by None (JSON null)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def dumps(value, *, indent=None):
    return json.dumps(json_safe(value), ensure_ascii=False, indent=indent, allow_nan=False)


def dump(path, value, *, indent=None):
    """Write an artifact that every JSON reader can parse."""
    target = Path(path)
    target.write_text(dumps(value, indent=indent), encoding="utf-8")
    return target
