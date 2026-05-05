"""Citation post-validator tests (Phase 3 Step 15 verifier)."""

from __future__ import annotations

from uuid import uuid4

from engram.llm.citations import validate_citations


def test_all_valid_citations_pass_through() -> None:
    a = str(uuid4())
    b = str(uuid4())
    text = f"See thought {a} and {b}."
    result = validate_citations(response_text=text, retrieved_ids=[a, b])
    assert result.text == text
    assert result.stripped_ids == []
    assert sorted(result.valid_ids) == sorted([a.lower(), b.lower()])


def test_hallucinated_citation_stripped() -> None:
    real = str(uuid4())
    hallucinated = str(uuid4())
    text = f"See {real}; also {hallucinated}."
    result = validate_citations(response_text=text, retrieved_ids=[real])
    assert real in result.text
    assert hallucinated not in result.text
    assert "[citation removed]" in result.text
    assert hallucinated.lower() in result.stripped_ids


def test_case_insensitive_match() -> None:
    real = str(uuid4()).lower()
    text_with_upper = f"See {real.upper()}."
    result = validate_citations(response_text=text_with_upper, retrieved_ids=[real])
    assert real.upper() in result.text  # Original casing preserved.
    assert result.stripped_ids == []


def test_response_with_no_uuids_unchanged() -> None:
    text = "No citations here, just commentary."
    result = validate_citations(response_text=text, retrieved_ids=[str(uuid4())])
    assert result.text == text
    assert result.stripped_ids == []
    assert result.valid_ids == []


def test_multiple_hallucinated_replaced_each() -> None:
    fakes = [str(uuid4()) for _ in range(3)]
    text = " ".join(f"See {f}." for f in fakes)
    result = validate_citations(response_text=text, retrieved_ids=[])
    assert text != result.text
    assert result.text.count("[citation removed]") == 3
    assert sorted(result.stripped_ids) == sorted(f.lower() for f in fakes)


def test_uuid_in_middle_of_word_still_matched() -> None:
    real = str(uuid4())
    text = f"prefix-{real}-suffix"
    result = validate_citations(response_text=text, retrieved_ids=[real])
    assert real in result.text
