"""JSON boundary: identifiers stay exact decimal strings, never floats.

Return SafeJSONResponse for payloads containing NumPy scalar values. It is also
the app default response class for ordinary Python dictionaries. Use Gid in
Pydantic response models so validation cannot coerce an identifier to float.
"""

import json
import re
from collections.abc import Mapping
from datetime import date, datetime
from numbers import Integral, Real
from typing import Annotated, Any

from pydantic import BeforeValidator
from starlette.responses import JSONResponse

ID_FIELDS = frozenset({"gid", "src", "dst"})
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


def id_to_string(value: Any) -> str:
    """Accept only exact int64 integers or canonical decimal strings."""
    if isinstance(value, bool):
        raise ValueError("Идентификатор не может быть bool")
    if isinstance(value, Integral):
        number = int(value)
    elif isinstance(value, str) and re.fullmatch(r"-?(0|[1-9][0-9]*)", value):
        number = int(value)
    else:
        raise ValueError("Идентификатор должен быть целым int64 или строкой, не float")
    if not INT64_MIN <= number <= INT64_MAX:
        raise ValueError("Идентификатор выходит за диапазон int64")
    return str(number)


Gid = Annotated[str, BeforeValidator(id_to_string)]


def to_json_safe(value: Any) -> Any:
    """Normalize nested records without casting integer identifiers to float.

    Lists of IDs and graph ID keys (id/source/target) must explicitly use
    id_to_string at construction; gid/src/dst are handled recursively here.
    DataFrame conversion should use itertuples, never iterrows (float coercion).
    """
    if isinstance(value, Mapping):
        return {
            key: id_to_string(item) if key in ID_FIELDS else to_json_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [to_json_safe(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Real):
        return float(value)
    raise TypeError(f"Тип {type(value).__name__} не поддерживается JSON-сериализатором")


class SafeJSONResponse(JSONResponse):
    def render(self, content: Any) -> bytes:
        return json.dumps(
            to_json_safe(content), ensure_ascii=False, allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
