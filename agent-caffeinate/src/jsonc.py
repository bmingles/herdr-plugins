"""A tiny JSONC tokenizer.

Copied verbatim from `vscode-workspace-sync/src/jsonc.py`. The duplication is
deliberate: `herdr plugin install <owner>/<repo>/<subdir>` fetches a single
subdirectory, so a plugin that imports across sibling directories cannot be installed.
Keep the two copies in sync by hand; the module is pure and has no plugin-specific
behaviour.

Here it only serves the plugin's own `config.json`, so the user may comment it.


VS Code `.code-workspace` files are JSONC: they may carry `//` and `/* */` comments and
trailing commas, and they belong to the user, so comments, key order, whitespace, and
every member other than `folders` must survive byte for byte. `json.loads` cannot help
with either half of that -- it rejects comments and trailing commas outright, and
round-tripping through `json.dumps` would destroy the user's formatting.

So instead of parsing, this module *locates* spans:

- `find_top_level_member(text, key)` -> the span of a depth-1 member's key and value.
- `strip_comments(text)` -> the same text with comment spans blanked to spaces.
- `strip_trailing_commas(text)` -> commas before a `}` or `]` blanked to spaces, so the
  combination of the two makes a JSONC document parseable by `json.loads`.
- `find_root_object_open(text)` -> the index of the root object's `{`.

Every scanner is string-aware (with `\\` escapes) and comment-aware, so a `]` or a `,`
inside a string value or inside a comment can never be mistaken for structure. Pure
functions over `str` with no dependency on the rest of the plugin.
"""

import json

_WS = " \t\r\n\f\v"
_LITERAL_STOP = ",}]" + _WS


class Member(object):
    """Where a top-level member lives in the text.

    `key_start` is the index of the key's opening quote, `value_start` the first
    character of the value (comments and whitespace after the `:` already skipped), and
    `value_end` the index one past the value's last character.
    """

    __slots__ = ("key_start", "value_start", "value_end")

    def __init__(self, key_start, value_start, value_end):
        self.key_start = key_start
        self.value_start = value_start
        self.value_end = value_end

    def __repr__(self):  # pragma: no cover - debugging aid
        return "Member(key_start=%d, value_start=%d, value_end=%d)" % (
            self.key_start,
            self.value_start,
            self.value_end,
        )

    def __eq__(self, other):
        if not isinstance(other, Member):
            return NotImplemented
        return (self.key_start, self.value_start, self.value_end) == (
            other.key_start,
            other.value_start,
            other.value_end,
        )


def _skip_ws_and_comments(text, i):
    """Advance past whitespace, `//` line comments and `/* */` block comments."""
    n = len(text)
    while i < n:
        c = text[i]
        if c in _WS:
            i += 1
            continue
        if c == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                i += 2
                while i < n and text[i] != "\n":
                    i += 1
                continue
            if nxt == "*":
                i += 2
                while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                    i += 1
                i = min(i + 2, n)
                continue
        return i
    return n


def _scan_string(text, i):
    """`i` is the opening quote. Return the index one past the closing quote."""
    n = len(text)
    i += 1
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        i += 1
    return n  # unterminated; treat the rest of the document as the string


def _scan_container(text, i):
    """`i` is a `{` or `[`. Return the index one past its matching close."""
    n = len(text)
    depth = 0
    while i < n:
        c = text[i]
        if c == '"':
            i = _scan_string(text, i)
            continue
        if c == "/" and i + 1 < n and text[i + 1] in "/*":
            i = _skip_ws_and_comments(text, i)
            continue
        if c in "{[":
            depth += 1
            i += 1
            continue
        if c in "}]":
            depth -= 1
            i += 1
            if depth <= 0:
                return i
            continue
        i += 1
    return n  # unterminated


def _scan_literal(text, i):
    """`i` is the start of a bare token (number, `true`, `false`, `null`)."""
    n = len(text)
    while i < n:
        c = text[i]
        if c in _LITERAL_STOP:
            break
        if c == "/" and i + 1 < n and text[i + 1] in "/*":
            break
        i += 1
    return i


def find_root_object_open(text):
    """Index of the root object's `{`, or -1 if the document's root is not an object."""
    i = _skip_ws_and_comments(text, 0)
    if i < len(text) and text[i] == "{":
        return i
    return -1


def find_top_level_member(text, key):
    """Find a depth-1 member of the root object by name.

    Returns a :class:`Member`, or ``None`` when the root is not an object or holds no
    such member. Members of nested objects never match: a depth-1 value that is itself a
    container is skipped whole.
    """
    n = len(text)
    i = find_root_object_open(text)
    if i < 0:
        return None
    i += 1
    state = "key"
    key_start = None
    key_matched = False
    value_start = None

    while i < n:
        i = _skip_ws_and_comments(text, i)
        if i >= n:
            break
        c = text[i]

        if c == "}":
            return None  # root object closed without a match

        if c == ",":
            state = "key"
            i += 1
            continue

        if c == ":":
            if state == "colon":
                i = _skip_ws_and_comments(text, i + 1)
                value_start = i
                state = "value"
                continue
            i += 1
            continue

        if state == "key":
            if c != '"':
                # Not a key where one was expected; step over whatever it is.
                i += 1
                continue
            end = _scan_string(text, i)
            try:
                name = json.loads(text[i:end])
            except ValueError:
                name = None
            key_matched = name == key
            key_start = i
            state = "colon"
            i = end
            continue

        if state == "value":
            if c == '"':
                end = _scan_string(text, i)
            elif c in "{[":
                end = _scan_container(text, i)
            else:
                end = _scan_literal(text, i)
                if end == i:  # nothing consumable; avoid spinning
                    end = i + 1
            if key_matched:
                return Member(key_start, value_start, end)
            state = "after"
            i = end
            continue

        # state == "colon" or "after": skip anything unexpected.
        i += 1

    return None


def strip_comments(text):
    """Blank every `//` and `/* */` comment to spaces, preserving length and newlines.

    Offsets in the result line up with the input, and comments inside string values are
    left alone. Trailing commas are *not* removed -- see :func:`strip_trailing_commas`.
    """
    n = len(text)
    out = list(text)
    i = 0
    while i < n:
        c = text[i]
        if c == '"':
            i = _scan_string(text, i)
            continue
        if c == "/" and i + 1 < n and text[i + 1] in "/*":
            end = _skip_ws_and_comments(text, i)
            for j in range(i, end):
                if out[j] != "\n":
                    out[j] = " "
            i = end
            continue
        i += 1
    return "".join(out)


def strip_trailing_commas(text):
    """Blank any comma that is followed only by whitespace/comments then `}` or `]`.

    Combined with :func:`strip_comments` this makes a JSONC document acceptable to
    `json.loads`, which is how the plugin reads its own config and the workspace file's
    existing `folders` array without giving up comment and trailing-comma tolerance.
    """
    n = len(text)
    out = list(text)
    i = 0
    while i < n:
        c = text[i]
        if c == '"':
            i = _scan_string(text, i)
            continue
        if c == "/" and i + 1 < n and text[i + 1] in "/*":
            i = _skip_ws_and_comments(text, i)
            continue
        if c == ",":
            j = _skip_ws_and_comments(text, i + 1)
            if j < n and text[j] in "}]":
                out[i] = " "
            i += 1
            continue
        i += 1
    return "".join(out)


def loads(text):
    """`json.loads` over JSONC: comments and trailing commas tolerated."""
    return json.loads(strip_trailing_commas(strip_comments(text)))
