"""JSON whitespace formatting without reserializing user data."""

from __future__ import annotations

import io
import json
from typing import NoReturn

from .syntax import Error, Text, tokenize_json

MAX_INPUT_CHARS = 1024 * 1024
# Keep successful output within the input bound so a second format remains valid.
MAX_OUTPUT_CHARS = MAX_INPUT_CHARS
MAX_NESTING_DEPTH = 64


class JsonFormatLimitError(ValueError):
    """The document exceeds the limits for formatting in the GUI thread."""


def _check_nesting(text: str) -> None:
    # Check before json.loads or tokenization allocates a nested object/token tree.
    # Quotes and escapes matter here; bracket-like text inside strings does not.
    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
            if depth > MAX_NESTING_DEPTH:
                raise JsonFormatLimitError("JSON nesting is too deep")
        elif char in "}]":
            depth = max(0, depth - 1)


def _reject_constant(text: str, constant: str) -> NoReturn:
    # The stdlib callback has no offset. Locate the first unquoted occurrence
    # only after the decoder reaches it, preserving earlier syntax errors.
    position = 0
    for kind, token in tokenize_json(text):
        if kind == Error and text.startswith(constant, position):
            raise json.JSONDecodeError("Expecting value", text, position)
        position += len(token)
    raise AssertionError("Non-finite JSON constant was not found")


def format_json(text: str) -> str:
    """Indent strict JSON by two spaces, changing only whitespace outside strings.

    Preserve key order, duplicate keys, number spellings and string escapes.
    Existing trailing line breaks become one LF; no final LF is otherwise added.
    Syntax errors retain stdlib ``JSONDecodeError`` offsets in the original text.
    Inputs or outputs over 1 Mi characters and containers
    nested more than 64 levels raise ``JsonFormatLimitError``.
    """
    if len(text) > MAX_INPUT_CHARS:
        raise JsonFormatLimitError("JSON input is too large")
    _check_nesting(text)

    # Validate only: converting numbers can lose precision, overflow, or hit
    # Python's integer digit limit. Never serialize this temporary parsed value.
    json.loads(
        text,
        parse_int=str,
        parse_float=str,
        parse_constant=lambda constant: _reject_constant(text, constant),
    )

    output = io.StringIO()
    output_length = 0

    def append(value: str) -> None:
        nonlocal output_length
        output_length += len(value)
        if output_length > MAX_OUTPUT_CHARS:
            raise JsonFormatLimitError("Formatted JSON is too large")
        output.write(value)

    depth = 0
    previous = ""
    for kind, token in tokenize_json(text):
        if kind == Text:
            continue
        if token in ("}", "]"):
            depth -= 1
            if previous not in ("{", "["):
                append("\n" + "  " * depth)
        elif previous in ("{", "[", ","):
            append("\n" + "  " * depth)

        append(": " if token == ":" else token)
        if token in ("{", "["):
            depth += 1
        previous = token

    if text.rstrip(" \t").endswith(("\n", "\r")):
        append("\n")
    return output.getvalue()


__all__ = ["JsonFormatLimitError", "format_json"]
