from __future__ import annotations

from types import SimpleNamespace

import pytest

from pdf_bridge.http.problems import ProblemError
from pdf_bridge.http.security import require_idempotency_key


@pytest.mark.parametrize(
    "value",
    ["short", "contains space", "unicode-\N{LATIN SMALL LETTER E WITH ACUTE}", "line\nbreak"],
)
def test_idempotency_keys_require_bounded_visible_ascii(value: str) -> None:
    request = SimpleNamespace(headers={"idempotency-key": value})

    with pytest.raises(ProblemError) as raised:
        require_idempotency_key(request)  # type: ignore[arg-type]

    assert raised.value.code == "invalid_idempotency_key"


def test_idempotency_key_is_returned_without_normalization() -> None:
    request = SimpleNamespace(headers={"idempotency-key": "Case-Sensitive_Key.001"})

    assert require_idempotency_key(request) == "Case-Sensitive_Key.001"  # type: ignore[arg-type]
