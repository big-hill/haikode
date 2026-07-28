"""
The apply_patch envelope format — a direct port of opencode's
`packages/opencode/src/patch/index.ts` (itself a port of the Rust
implementation used by codex).

    *** Begin Patch
    *** Add File: hello.txt
    +Hello world
    *** Update File: src/app.py
    *** Move to: src/main.py
    @@ def greet():
    -print("Hi")
    +print("Hello, world!")
    *** Delete File: obsolete.txt
    *** End Patch

This module is pure: it parses text and derives new file contents. It never
touches the filesystem — apply_patch.py does that, only after every hunk in
the patch has been validated.
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

BEGIN_MARKER = "*** Begin Patch"
END_MARKER = "*** End Patch"
EOF_MARKER = "*** End of File"

ADD_HEADER = "*** Add File:"
DELETE_HEADER = "*** Delete File:"
UPDATE_HEADER = "*** Update File:"
MOVE_HEADER = "*** Move to:"


class PatchError(Exception):
    """A patch that cannot be parsed or cannot be applied to the file it names."""


@dataclass
class UpdateChunk:
    old_lines: List[str] = field(default_factory=list)
    new_lines: List[str] = field(default_factory=list)
    change_context: Optional[str] = None
    is_end_of_file: bool = False


@dataclass
class Hunk:
    type: str                     # "add" | "delete" | "update"
    path: str
    contents: str = ""            # add only
    move_path: Optional[str] = None
    chunks: List[UpdateChunk] = field(default_factory=list)


# --- parsing -----------------------------------------------------------

_HEREDOC = re.compile(
    r"^(?:cat\s+)?<<['\"]?(\w+)['\"]?[ \t]*\n(.*)\n\1[ \t]*$", re.DOTALL)


def strip_heredoc(text: str) -> str:
    """`apply_patch <<'EOF' ... EOF` is a shape models emit; unwrap it."""
    match = _HEREDOC.match(text)
    return match.group(2) if match else text


def _parse_header(lines: List[str], index: int):
    """(path, move_path, next_index) for a file header, or None."""
    line = lines[index]

    if line.startswith(ADD_HEADER):
        path = line[len(ADD_HEADER):].strip()
        return (path, None, index + 1) if path else None

    if line.startswith(DELETE_HEADER):
        path = line[len(DELETE_HEADER):].strip()
        return (path, None, index + 1) if path else None

    if line.startswith(UPDATE_HEADER):
        path = line[len(UPDATE_HEADER):].strip()
        move_path = None
        next_index = index + 1
        if next_index < len(lines) and lines[next_index].startswith(MOVE_HEADER):
            move_path = lines[next_index][len(MOVE_HEADER):].strip()
            next_index += 1
        return (path, move_path, next_index) if path else None

    return None


def _parse_update_chunks(lines: List[str], start: int) -> Tuple[List[UpdateChunk], int]:
    chunks: List[UpdateChunk] = []
    i = start

    while i < len(lines) and not lines[i].startswith("***"):
        if not lines[i].startswith("@@"):
            i += 1
            continue

        context = lines[i][2:].strip()
        i += 1

        old_lines: List[str] = []
        new_lines: List[str] = []
        end_of_file = False

        while (i < len(lines) and not lines[i].startswith("@@")
               and not lines[i].startswith("***")):
            line = lines[i]
            if line == EOF_MARKER:
                end_of_file = True
                i += 1
                break
            if line.startswith(" "):
                old_lines.append(line[1:])
                new_lines.append(line[1:])
            elif line.startswith("-"):
                old_lines.append(line[1:])
            elif line.startswith("+"):
                new_lines.append(line[1:])
            # Anything else (a bare empty line, say) is context noise; the
            # reference parser drops it and so do we.
            i += 1

        chunks.append(UpdateChunk(old_lines=old_lines, new_lines=new_lines,
                                  change_context=context or None,
                                  is_end_of_file=end_of_file))

    return chunks, i


def _parse_add_contents(lines: List[str], start: int) -> Tuple[str, int]:
    content = ""
    i = start
    while i < len(lines) and not lines[i].startswith("***"):
        if lines[i].startswith("+"):
            content += lines[i][1:] + "\n"
        i += 1
    if content.endswith("\n"):
        content = content[:-1]
    return content, i


def parse_patch(patch_text: str) -> List[Hunk]:
    """Hunks in the order the patch declares them. Raises PatchError."""
    cleaned = strip_heredoc(patch_text.strip())
    lines = cleaned.split("\n")

    begin = end = -1
    for index, line in enumerate(lines):
        if begin == -1 and line.strip() == BEGIN_MARKER:
            begin = index
        if end == -1 and line.strip() == END_MARKER:
            end = index
    if begin == -1 or end == -1 or begin >= end:
        raise PatchError("Invalid patch format: missing Begin/End markers")

    hunks: List[Hunk] = []
    i = begin + 1
    while i < end:
        header = _parse_header(lines, i)
        if header is None:
            i += 1
            continue
        path, move_path, next_index = header

        if lines[i].startswith(ADD_HEADER):
            contents, i = _parse_add_contents(lines, next_index)
            hunks.append(Hunk(type="add", path=path, contents=contents))
        elif lines[i].startswith(DELETE_HEADER):
            hunks.append(Hunk(type="delete", path=path))
            i = next_index
        elif lines[i].startswith(UPDATE_HEADER):
            chunks, i = _parse_update_chunks(lines, next_index)
            hunks.append(Hunk(type="update", path=path, move_path=move_path,
                              chunks=chunks))
        else:
            i += 1

    return hunks


def is_empty_patch(patch_text: str) -> bool:
    """True for the exact `*** Begin Patch\\n*** End Patch` no-op."""
    normalised = patch_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return normalised == BEGIN_MARKER + "\n" + END_MARKER


# --- byte-order-mark handling -----------------------------------------

BOM = "\ufeff"


def split_bom(text: str) -> Tuple[str, bool]:
    if text.startswith(BOM):
        return text[len(BOM):], True
    return text, False


def join_bom(text: str, bom: bool) -> str:
    if bom and not text.startswith(BOM):
        return BOM + text
    return text


# --- matching ----------------------------------------------------------

_UNICODE_MAP = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
    "\u2014": "-", "\u2015": "-",
    "\u00a0": " ",
}
_UNICODE_RE = re.compile("|".join(re.escape(k) for k in _UNICODE_MAP))


def normalize_unicode(text: str) -> str:
    """Smart quotes/dashes back to ASCII — models paste them constantly."""
    text = _UNICODE_RE.sub(lambda m: _UNICODE_MAP[m.group(0)], text)
    return text.replace("\u2026", "...")


def _try_match(lines: List[str], pattern: List[str], start: int,
               compare, eof: bool) -> int:
    if eof:
        from_end = len(lines) - len(pattern)
        if from_end >= start:
            if all(compare(lines[from_end + j], pattern[j])
                   for j in range(len(pattern))):
                return from_end

    for i in range(start, len(lines) - len(pattern) + 1):
        if all(compare(lines[i + j], pattern[j]) for j in range(len(pattern))):
            return i
    return -1


def seek_sequence(lines: List[str], pattern: List[str], start: int,
                  eof: bool = False) -> int:
    """
    Index of `pattern` in `lines` at or after `start`, tried four ways:
    exact, then trailing-whitespace-insensitive, then fully trimmed, then with
    unicode punctuation normalised. Same ladder as opencode. -1 if absent.
    """
    if not pattern:
        return -1

    for compare in (
        lambda a, b: a == b,
        lambda a, b: a.rstrip() == b.rstrip(),
        lambda a, b: a.strip() == b.strip(),
        lambda a, b: normalize_unicode(a.strip()) == normalize_unicode(b.strip()),
    ):
        found = _try_match(lines, pattern, start, compare, eof)
        if found != -1:
            return found
    return -1


def compute_replacements(original_lines: List[str], file_path: str,
                         chunks: List[UpdateChunk]):
    """[(start, old_len, new_lines)] — raises PatchError when a chunk misses."""
    replacements = []
    line_index = 0

    for chunk in chunks:
        if chunk.change_context:
            context_index = seek_sequence(original_lines, [chunk.change_context],
                                          line_index)
            if context_index == -1:
                raise PatchError(
                    "Failed to find context '%s' in %s"
                    % (chunk.change_context, file_path))
            line_index = context_index + 1

        if not chunk.old_lines:
            # Pure insertion: land it before a trailing blank line if there is one.
            insert_at = (len(original_lines) - 1
                         if original_lines and original_lines[-1] == ""
                         else len(original_lines))
            replacements.append((insert_at, 0, list(chunk.new_lines)))
            continue

        pattern = list(chunk.old_lines)
        new_slice = list(chunk.new_lines)
        found = seek_sequence(original_lines, pattern, line_index,
                              chunk.is_end_of_file)

        if found == -1 and pattern and pattern[-1] == "":
            pattern = pattern[:-1]
            if new_slice and new_slice[-1] == "":
                new_slice = new_slice[:-1]
            found = seek_sequence(original_lines, pattern, line_index,
                                  chunk.is_end_of_file)

        if found == -1:
            raise PatchError("Failed to find expected lines in %s:\n%s"
                             % (file_path, "\n".join(chunk.old_lines)))

        replacements.append((found, len(pattern), new_slice))
        line_index = found + len(pattern)

    replacements.sort(key=lambda item: item[0])
    return replacements


def apply_replacements(lines: List[str], replacements) -> List[str]:
    result = list(lines)
    for start, old_len, segment in reversed(replacements):
        result[start:start + old_len] = list(segment)
    return result


def derive_new_contents(file_path: str, chunks: List[UpdateChunk],
                        original_text: str) -> Tuple[str, bool]:
    """
    (new_text, had_bom) for an "*** Update File:" hunk. Raises PatchError if
    any chunk does not match the file, which is what makes validate-then-write
    possible: nothing is written until every file has produced its new text.
    """
    text, bom = split_bom(original_text)
    original_lines = text.split("\n")
    if original_lines and original_lines[-1] == "":
        original_lines.pop()

    replacements = compute_replacements(original_lines, file_path, chunks)
    new_lines = apply_replacements(original_lines, replacements)

    if not new_lines or new_lines[-1] != "":
        new_lines.append("")

    new_text, new_bom = split_bom("\n".join(new_lines))
    return new_text, (bom or new_bom)
