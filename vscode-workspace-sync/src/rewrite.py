"""Render the `folders` array and splice it into the workspace file's text.

The workspace file is the user's: comments, trailing commas, key order and every member
other than `folders` must survive byte for byte. So there is no parse-and-reserialise
step anywhere -- `jsonc.find_top_level_member` locates the `folders` value's span and
the text around it is left exactly alone.

Everything here is a pure function over `str`.
"""

import json
import os

import jsonc

WS = " \t"


def _line_indent(text, idx):
    """The leading whitespace of the line containing `idx`."""
    start = text.rfind("\n", 0, idx) + 1
    out = []
    for ch in text[start:]:
        if ch in WS:
            out.append(ch)
        else:
            break
    return "".join(out)


def render_entry(entry):
    """One folder object, inline: `{ "path": "..." }` or with a `"name"` after it."""
    parts = ['"path": %s' % json.dumps(entry.path)]
    if entry.name:
        parts.append('"name": %s' % json.dumps(entry.name))
    return "{ %s }" % ", ".join(parts)


def render_folders(entries, base_indent=""):
    """Render the array *value* -- from `[` to `]` inclusive.

    One entry per line at `base_indent` plus two spaces, closing `]` at `base_indent`,
    and **no trailing comma** after the last entry regardless of what the file had: the
    plugin owns this array outright and strict JSON is safer for other tooling.
    """
    if not entries:
        return "[]"
    item_indent = base_indent + "  "
    lines = ["["]
    for i, entry in enumerate(entries):
        comma = "," if i < len(entries) - 1 else ""
        lines.append("%s%s%s" % (item_indent, render_entry(entry), comma))
    lines.append("%s]" % base_indent)
    return "\n".join(lines)


def _root_inner_indent(text, open_idx):
    """The indentation the root object's members sit at."""
    root_indent = _line_indent(text, open_idx)
    i = jsonc._skip_ws_and_comments(text, open_idx + 1)
    if i < len(text) and text[i] != "}":
        line_start = text.rfind("\n", 0, i) + 1
        if text[line_start:i].strip() == "":
            return text[line_start:i]
    return root_indent + "  "


def splice_folders(text, entries):
    """Return `text` with its top-level `folders` member set to `entries`.

    When the file has no `folders` member, one is inserted as the **first** member of
    the root object, at the root's inner indentation.
    """
    member = jsonc.find_top_level_member(text, "folders")
    if member is not None:
        base_indent = _line_indent(text, member.key_start)
        rendered = render_folders(entries, base_indent)
        return text[: member.value_start] + rendered + text[member.value_end :]

    open_idx = jsonc.find_root_object_open(text)
    if open_idx < 0:
        raise ValueError("workspace file's root is not a JSON object")
    inner = _root_inner_indent(text, open_idx)
    rendered = render_folders(entries, inner)
    insertion = "\n%s\"folders\": %s," % (inner, rendered)
    rest = text[open_idx + 1 :]
    if rest.strip() == "" or rest.lstrip(" \t").startswith("}"):
        # Empty root object: the inserted member needs its own newline before `}`.
        insertion += "\n"
    return text[: open_idx + 1] + insertion + rest


def read_folders(text):
    """Parse the existing top-level `folders` array.

    Returns a list of member dicts (each with at least a `path`), or `None` when there
    is no `folders` member or it cannot be parsed -- the caller then falls back to a
    plain text comparison. Comments and trailing commas inside the array are tolerated.
    """
    member = jsonc.find_top_level_member(text, "folders")
    if member is None:
        return None
    raw = text[member.value_start : member.value_end]
    try:
        value = jsonc.loads(raw)
    except ValueError:
        return None
    if not isinstance(value, list):
        return None
    out = []
    for item in value:
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, str):
            # Not a shape VS Code writes, but harmless to accept.
            out.append({"path": item})
        else:
            return None
    return out


def resolved_existing_folders(text, workspace_dir):
    """The existing `folders` as `(resolved_path, name_or_None)` pairs, or `None`.

    VS Code writes newly added folder paths **relative** to the workspace file's own
    directory (confirmed via `code --add`), so a file the UI has touched holds relative
    paths that are equal-but-not-identical to the plugin's absolute ones. Resolving here
    is what stops the unchanged-check from rewriting the file on every single run after
    one UI folder-add.
    """
    items = read_folders(text)
    if items is None:
        return None
    out = []
    for item in items:
        path = item.get("path")
        if not isinstance(path, str):
            return None
        expanded = os.path.expanduser(path)
        if not os.path.isabs(expanded):
            expanded = os.path.join(workspace_dir, expanded)
        name = item.get("name")
        out.append((os.path.normpath(expanded), name if isinstance(name, str) else None))
    return out
