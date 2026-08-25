"""Tests for In-Reply-To / References normalization.

A Message-ID that reaches Gmail HTML-escaped (``&lt;id@host&gt;``) or without
angle brackets matches nothing, so the reply threads for no recipient while
still looking correct to whoever made the call. These tests pin the repair.
"""

import base64

import pytest

from gmail.gmail_tools import _normalize_message_ids, _prepare_gmail_message

MSGID = "9572b996-b65c-4ca9-82ca-0d6015f1d322@conference-publishing.com"
CANONICAL = f"<{MSGID}>"


def headers_of(raw_message):
    """Return the decoded header lines of a prepared message, unfolded."""
    text = base64.urlsafe_b64decode(raw_message).decode()
    head = text.split("\n\n", 1)[0]
    # RFC 5322 folding puts long values on a continuation line starting with
    # whitespace; join them back so a header is one string.
    return head.replace("\n ", " ").replace("\n\t", " ").splitlines()


def header_value(raw_message, name):
    for line in headers_of(raw_message):
        if line.lower().startswith(name.lower() + ":"):
            return line.split(":", 1)[1].strip()
    return None


@pytest.mark.parametrize(
    "supplied",
    [
        CANONICAL,
        f"&lt;{MSGID}&gt;",  # the mistake this guards against
        MSGID,  # brackets omitted
        f"  {CANONICAL}  ",  # stray whitespace
        f"&amp;lt;{MSGID}&amp;gt;".replace("&amp;", "&"),  # double-escaped source
    ],
)
def test_single_id_normalizes_to_canonical(supplied):
    assert _normalize_message_ids(supplied, "in_reply_to") == CANONICAL


def test_references_chain_is_preserved_in_order():
    chain = f"<a@x.test> <b@y.test> {CANONICAL}"
    assert _normalize_message_ids(chain, "references") == chain


@pytest.mark.parametrize(
    "supplied",
    [
        "&lt;a@x.test&gt;&lt;b@y.test&gt;",  # escaped, run together
        "<a@x.test><b@y.test>",  # bracketed, no separator
        "a@x.test, b@y.test",  # bare, comma separated
        "a@x.test b@y.test",  # bare, space separated
    ],
)
def test_chain_variants_all_normalize_the_same(supplied):
    assert _normalize_message_ids(supplied, "references") == "<a@x.test> <b@y.test>"


def test_none_and_empty_pass_through():
    assert _normalize_message_ids(None, "in_reply_to") is None
    assert _normalize_message_ids("", "in_reply_to") is None
    assert _normalize_message_ids("   ", "in_reply_to") is None


def test_gmail_api_id_is_rejected_with_a_pointed_hint():
    with pytest.raises(ValueError) as excinfo:
        _normalize_message_ids("1a0268a80193937d", "in_reply_to")
    message = str(excinfo.value)
    assert "Gmail API message id" in message
    assert "format='metadata'" in message


def test_non_message_id_is_rejected():
    with pytest.raises(ValueError) as excinfo:
        _normalize_message_ids("not a message id", "references")
    assert "references" in str(excinfo.value)


def test_prepare_message_repairs_escaped_ids_end_to_end():
    escaped, _ = _prepare_gmail_message(
        subject="Re: test",
        body="hi",
        to="someone@example.test",
        in_reply_to=f"&lt;{MSGID}&gt;",
        references=f"&lt;{MSGID}&gt;",
    )
    correct, _ = _prepare_gmail_message(
        subject="Re: test",
        body="hi",
        to="someone@example.test",
        in_reply_to=CANONICAL,
        references=CANONICAL,
    )

    assert header_value(escaped, "In-Reply-To") == CANONICAL
    assert header_value(escaped, "References") == CANONICAL
    # The whole message must be byte-identical to the one built correctly.
    assert escaped == correct


def test_prepare_message_still_adds_re_prefix_for_escaped_ids():
    raw, _ = _prepare_gmail_message(
        subject="test",
        body="hi",
        to="someone@example.test",
        in_reply_to=f"&lt;{MSGID}&gt;",
    )
    assert header_value(raw, "Subject") == "Re: test"


def test_prepare_message_without_threading_sets_no_headers():
    raw, _ = _prepare_gmail_message(
        subject="test", body="hi", to="someone@example.test"
    )
    assert header_value(raw, "In-Reply-To") is None
    assert header_value(raw, "References") is None
