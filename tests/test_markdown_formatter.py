from __future__ import annotations

import hashlib
import json as jsonlib
from dataclasses import dataclass
from typing import Any, Callable

import pytest

from pdf_bridge.services.markdown_formatter import (
    MARKDOWN_FORMATTER_VERSION,
    FormatterConfig,
    LayoutPage,
    MarkdownFormattingError,
    fidelity_projection,
    format_markdown_document,
    normalize_layout_text,
)


@dataclass(slots=True)
class FakeResponse:
    payload: object
    status_code: int = 200

    def json(self) -> object:
        return self.payload


OutputFactory = Callable[[list[dict[str, Any]]], object]


class FakeFormatterClient:
    def __init__(
        self,
        *,
        outputs: list[OutputFactory | dict[str, Any] | str | None] | None = None,
        max_model_len: int = 200,
        tokenizer_class: str = "TestTokenizer",
        token_counter: Callable[[str], int] | None = None,
        raw_tokenizer: Callable[[str], list[int]] | None = None,
        token_overhead: int = 5,
        finish_reasons: list[str] | None = None,
    ) -> None:
        self.outputs = list(outputs or [])
        self.max_model_len = max_model_len
        self.tokenizer_class = tokenizer_class
        self.token_counter = token_counter or (lambda text: len(text.split()))
        self.raw_tokenizer = raw_tokenizer or (lambda text: [ord(character) for character in text])
        self.token_overhead = token_overhead
        self.finish_reasons = list(finish_reasons or [])
        self.get_calls: list[tuple[str, dict[str, str], float]] = []
        self.tokenize_calls: list[dict[str, Any]] = []
        self.chat_calls: list[dict[str, Any]] = []
        self.chat_headers: list[dict[str, str]] = []

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
    ) -> FakeResponse:
        self.get_calls.append((url, headers, timeout))
        return FakeResponse(
            {
                "tokenizer_class": self.tokenizer_class,
                "model": "formatter-model",
            }
        )

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> FakeResponse:
        del timeout
        if url.endswith("/tokenize"):
            self.tokenize_calls.append(json)
            if "prompt" in json:
                tokens = self.raw_tokenizer(json["prompt"])
                return FakeResponse(
                    {
                        "count": len(tokens),
                        "max_model_len": self.max_model_len,
                        "tokens": tokens,
                    }
                )
            pages = _source_pages(json)
            source_count = sum(
                self.token_counter(source_slice["source_text"])
                for page in pages
                for source_slice in page["slices"]
            )
            count = self.token_overhead + source_count
            return FakeResponse(
                {
                    "count": count,
                    "max_model_len": self.max_model_len,
                    "tokens": list(range(count)),
                }
            )
        if not url.endswith("/v1/chat/completions"):
            raise AssertionError(f"unexpected POST URL: {url}")

        self.chat_calls.append(json)
        self.chat_headers.append(headers)
        source_pages = _source_pages(json)
        output = self.outputs.pop(0) if self.outputs else None
        if callable(output):
            output = output(source_pages)
        if output is None:
            output = _valid_output(source_pages)
        content = output if isinstance(output, str) else jsonlib.dumps(output, ensure_ascii=False)
        finish_reason = self.finish_reasons.pop(0) if self.finish_reasons else "stop"
        return FakeResponse(
            {
                "model": json["model"],
                "choices": [
                    {
                        "finish_reason": finish_reason,
                        "message": {"content": content},
                    }
                ],
            }
        )


def _source_pages(request: dict[str, Any]) -> list[dict[str, Any]]:
    user_content = request["messages"][-1]["content"]
    return jsonlib.loads(user_content.split("\n", 1)[1])["pages"]


def _valid_output(source_pages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "pages": [
            {
                "page_number": page["page_number"],
                "slices": [
                    {
                        "slice_index": source_slice["slice_index"],
                        "source_text_sha256": source_slice["source_text_sha256"],
                        "markdown": source_slice["source_text"],
                    }
                    for source_slice in page["slices"]
                ],
            }
            for page in source_pages
        ]
    }


def _replace_markdown(markdown: str) -> OutputFactory:
    def replace(source_pages: list[dict[str, Any]]) -> dict[str, Any]:
        output = _valid_output(source_pages)
        output["pages"][0]["slices"][0]["markdown"] = markdown
        return output

    return replace


def _config(**overrides: object) -> FormatterConfig:
    values: dict[str, object] = {
        "api_url": "https://formatter.internal",
        "model_id": "formatter-model",
        "api_token": "secret-token",
        "timeout_seconds": 17.0,
        "max_input_tokens": 100,
        "max_output_tokens": 20,
        "token_safety_reserve": 5,
        "max_pages_per_request": 8,
        "max_attempts": 2,
        "expected_tokenizer_class": "TestTokenizer",
    }
    values.update(overrides)
    return FormatterConfig(**values)  # type: ignore[arg-type]


def test_formats_normalized_pages_with_strict_requests_and_canonical_boundaries() -> None:
    client = FakeFormatterClient()
    result = format_markdown_document(
        [
            LayoutPage(1, "Caf\u00e9  \uff11\uff12\r\nAlpha\x00 beta"),
            LayoutPage(2, "Second page"),
        ],
        _config(),
        client=client,
    )

    assert client.get_calls == [
        (
            "https://formatter.internal/tokenizer_info",
            {"Accept": "application/json", "Authorization": "Bearer secret-token"},
            17.0,
        )
    ]
    assert all(call["model"] == "formatter-model" for call in client.tokenize_calls)
    assert all(call["add_generation_prompt"] is True for call in client.tokenize_calls)
    assert len(client.chat_calls) == 1
    request = client.chat_calls[0]
    assert request["n"] == 1
    assert request["temperature"] == 0
    assert request["max_tokens"] == 20
    assert request["stream"] is False
    assert request["tools"] == []
    assert request["response_format"]["type"] == "json_schema"
    schema_wrapper = request["response_format"]["json_schema"]
    assert schema_wrapper["name"] == "page_markdown"
    assert schema_wrapper["strict"] is True
    assert schema_wrapper["schema"]["additionalProperties"] is False
    page_schema = schema_wrapper["schema"]["properties"]["pages"]["items"]
    assert page_schema["additionalProperties"] is False
    slice_schema = page_schema["properties"]["slices"]["items"]
    assert slice_schema["additionalProperties"] is False
    assert client.chat_headers == [
        {"Accept": "application/json", "Authorization": "Bearer secret-token"}
    ]

    assert result.formatter_version == MARKDOWN_FORMATTER_VERSION
    assert result.tokenizer_class == "TestTokenizer"
    assert result.max_model_len == 200
    assert result.pages[0].source_text == "Caf\u00e9  12\nAlpha beta"
    assert result.markdown == (
        "<!-- page:1 -->\nCaf\u00e9  12\nAlpha beta\n\n<!-- page:2 -->\nSecond page"
    )
    assert result.markdown_sha256 == hashlib.sha256(result.markdown.encode()).hexdigest()
    assert len(result.attempts) == 1
    assert result.attempts[0].valid is True
    assert result.attempts[0].diagnostic is None


def test_greedily_packs_consecutive_pages_with_a_secondary_page_cap() -> None:
    client = FakeFormatterClient()
    result = format_markdown_document(
        [
            LayoutPage(1, "page one"),
            LayoutPage(2, "page two"),
            LayoutPage(3, "page three"),
        ],
        _config(max_pages_per_request=2),
        client=client,
    )

    assert [
        [page["page_number"] for page in _source_pages(call)] for call in client.chat_calls
    ] == [[1, 2], [3]]
    assert [page.page_number for page in result.pages] == [1, 2, 3]
    assert [(attempt.batch_index, attempt.attempt_number) for attempt in result.attempts] == [
        (0, 1),
        (1, 1),
    ]


def test_oversized_page_slicing_prefers_newlines_then_uses_a_stable_text_boundary() -> None:
    page = LayoutPage(1, "aa\nbb\ncccccc")
    config = _config(
        max_input_tokens=9,
        max_output_tokens=2,
        token_safety_reserve=1,
    )

    first_client = FakeFormatterClient(token_counter=len)
    first = format_markdown_document([page], config, client=first_client)
    second = format_markdown_document(
        [page],
        config,
        client=FakeFormatterClient(token_counter=len),
    )

    first_slices = first.pages[0].slices
    assert [source_slice.source_text for source_slice in first_slices] == [
        "aa\n",
        "bb\n",
        "cccc",
        "cc",
    ]
    assert [source_slice.slice_index for source_slice in first_slices] == [0, 1, 2, 3]
    assert "".join(source_slice.source_text for source_slice in first_slices) == page.text
    assert [
        (source_slice.slice_index, source_slice.source_text_sha256) for source_slice in first_slices
    ] == [
        (source_slice.slice_index, source_slice.source_text_sha256)
        for source_slice in second.pages[0].slices
    ]
    assert all(call["max_tokens"] == 2 for call in first_client.chat_calls)


def test_single_oversized_line_splits_only_at_served_token_boundaries() -> None:
    def tokens_in_threes(text: str) -> list[int]:
        return [
            int.from_bytes(text[index : index + 3].encode(), "big")
            for index in range(0, len(text), 3)
        ]

    result = format_markdown_document(
        [LayoutPage(1, "abcdefgh")],
        _config(
            max_input_tokens=9,
            max_output_tokens=2,
            token_safety_reserve=1,
        ),
        client=FakeFormatterClient(
            token_counter=len,
            raw_tokenizer=tokens_in_threes,
        ),
    )

    assert [source_slice.source_text for source_slice in result.pages[0].slices] == [
        "abc",
        "def",
        "gh",
    ]


def test_invalid_fidelity_is_retried_with_identical_input_and_hash_only_diagnostics() -> None:
    client = FakeFormatterClient(outputs=[_replace_markdown("beta alpha"), None])
    result = format_markdown_document(
        [LayoutPage(1, "alpha beta")],
        _config(),
        client=client,
    )

    assert len(client.chat_calls) == 2
    assert client.chat_calls[0] == client.chat_calls[1]
    assert [attempt.valid for attempt in result.attempts] == [False, True]
    diagnostic = result.attempts[0].diagnostic or ""
    assert "fidelity mismatch" in diagnostic
    assert "source_projection_sha256=" in diagnostic
    assert "markdown_projection_sha256=" in diagnostic
    assert "alpha" not in diagnostic
    assert "beta" not in diagnostic
    assert len(diagnostic) <= 600


def test_retry_exhaustion_fails_without_returning_source_text() -> None:
    client = FakeFormatterClient(outputs=[{"pages": []}, {"pages": []}])
    source = "PRIVATE-SOURCE-90210"

    with pytest.raises(MarkdownFormattingError) as raised:
        format_markdown_document(
            [LayoutPage(1, source)],
            _config(max_attempts=2),
            client=client,
        )

    assert len(client.chat_calls) == 2
    assert len(raised.value.attempts) == 2
    assert all(not attempt.valid for attempt in raised.value.attempts)
    assert source not in str(raised.value)
    assert "failed validation after 2 attempts" in str(raised.value)


@pytest.mark.parametrize(
    ("source", "markdown", "diagnostic"),
    [
        ("alpha beta", "```\nalpha beta", "unbalanced fenced block"),
        ("alpha beta", "<span>alpha</span> beta", "raw HTML"),
        ("alpha beta", "![alpha](alpha) beta", "image syntax"),
        ("alpha beta", "![alpha] beta", "image syntax"),
        (
            "alpha beta",
            "[alpha](https://generated.invalid) beta",
            "generated link destination",
        ),
        ("alpha beta", "alpha\x00 beta", "disallowed control character"),
        ("alpha beta", "alpha\u200b beta", "disallowed control character"),
        (
            "Name Value A 1 extra",
            "| Name | Value |\n| --- | --- |\n| A | 1 | extra |",
            "table row with the wrong column count",
        ),
        (
            "Name Value A 1",
            "| Name | Value |\n| -- | --- |\n| A | 1 |",
            "invalid table delimiter",
        ),
    ],
)
def test_rejects_unsafe_or_malformed_markdown(
    source: str,
    markdown: str,
    diagnostic: str,
) -> None:
    client = FakeFormatterClient(outputs=[_replace_markdown(markdown)])

    with pytest.raises(MarkdownFormattingError) as raised:
        format_markdown_document(
            [LayoutPage(1, source)],
            _config(max_attempts=1),
            client=client,
        )

    assert diagnostic in str(raised.value)


def test_accepts_balanced_fences_valid_tables_and_source_backed_links() -> None:
    source = "Name Value\nA 1\nprint hello\nOpenAI https://openai.com"
    markdown = (
        "| Name | Value |\n"
        "| --- | --- |\n"
        "| A | 1 |\n\n"
        "```python\n"
        "print hello\n"
        "```\n\n"
        "[OpenAI](https://openai.com)"
    )
    result = format_markdown_document(
        [LayoutPage(1, source)],
        _config(max_attempts=1),
        client=FakeFormatterClient(outputs=[_replace_markdown(markdown)]),
    )

    assert result.pages[0].markdown == markdown
    assert result.pages[0].slices[0].source_projection_sha256 == (
        result.pages[0].slices[0].markdown_projection_sha256
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda output: {**output, "unexpected": True},
        lambda output: {
            "pages": [
                {
                    **output["pages"][0],
                    "slices": [
                        {
                            **output["pages"][0]["slices"][0],
                            "source_text_sha256": "0" * 64,
                        }
                    ],
                }
            ]
        },
        lambda output: {"pages": []},
    ],
)
def test_rejects_unknown_fields_hash_mismatch_and_incomplete_coverage(
    mutate: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    def invalid(source_pages: list[dict[str, Any]]) -> dict[str, Any]:
        return mutate(_valid_output(source_pages))

    with pytest.raises(MarkdownFormattingError):
        format_markdown_document(
            [LayoutPage(1, "alpha beta")],
            _config(max_attempts=1),
            client=FakeFormatterClient(outputs=[invalid]),
        )


def test_rejects_reordered_pages_even_when_all_pages_are_present() -> None:
    def reverse_pages(source_pages: list[dict[str, Any]]) -> dict[str, Any]:
        output = _valid_output(source_pages)
        output["pages"].reverse()
        return output

    with pytest.raises(MarkdownFormattingError, match="reordered"):
        format_markdown_document(
            [LayoutPage(1, "first page"), LayoutPage(2, "second page")],
            _config(max_attempts=1),
            client=FakeFormatterClient(outputs=[reverse_pages]),
        )


def test_unicode_projection_is_exact_ordered_and_multiplicity_sensitive() -> None:
    assert normalize_layout_text("Caf\u00e9 \uff11\uff12\r\nword") == "Caf\u00e9 12\nword"
    assert fidelity_projection("# Caf\u00e9 **12** word word", markdown=True) == (
        "Caf\u00e9",
        "12",
        "word",
        "word",
    )

    client = FakeFormatterClient(outputs=[_replace_markdown("word Caf\u00e9 12 word")])
    with pytest.raises(MarkdownFormattingError, match="fidelity mismatch"):
        format_markdown_document(
            [LayoutPage(1, "Caf\u00e9 12 word word")],
            _config(max_attempts=1),
            client=client,
        )


def test_tokenizer_identity_and_context_budget_fail_before_chat_completion() -> None:
    wrong_tokenizer = FakeFormatterClient(tokenizer_class="WrongTokenizer")
    with pytest.raises(MarkdownFormattingError, match="different tokenizer class"):
        format_markdown_document(
            [LayoutPage(1, "alpha")],
            _config(),
            client=wrong_tokenizer,
        )
    assert wrong_tokenizer.chat_calls == []

    insufficient_context = FakeFormatterClient(max_model_len=30)
    with pytest.raises(MarkdownFormattingError, match="prompt and output reserve"):
        format_markdown_document(
            [LayoutPage(1, "alpha")],
            _config(max_output_tokens=25, token_safety_reserve=1),
            client=insufficient_context,
        )
    assert insufficient_context.chat_calls == []


@pytest.mark.parametrize("value", ["", " TestTokenizer", "Test Tokenizer"])
def test_formatter_configuration_requires_a_stable_tokenizer_identity(value: str) -> None:
    with pytest.raises(ValueError, match="bounded stable identity"):
        _config(expected_tokenizer_class=value)


def test_non_stop_completion_is_retried_and_then_fails_hard() -> None:
    client = FakeFormatterClient(finish_reasons=["length", "length"])
    with pytest.raises(MarkdownFormattingError, match="normal stop") as raised:
        format_markdown_document(
            [LayoutPage(1, "alpha beta")],
            _config(max_attempts=2),
            client=client,
        )

    assert len(client.chat_calls) == 2
    assert len(raised.value.attempts) == 2
