"""Register actual Markdown destinations in an outbound copy, never in native history."""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.rules_block import StateBlock, reference
from markdown_it.rules_core import StateCore
from markdown_it.rules_inline import StateInline, image, link
from mdit_py_plugins.footnote import footnote_plugin

from app.contracts.conversation import HistoryPage
from app.contracts.files import FileFailure
from app.worker.state.amp_attachments import AmpAttachment
from app.worker.state.history import MAX_PAGE

MAX_LOCAL_FILES = 16


def _local_path(destination: str) -> Path | None:
    if len(destination) > 1024:
        return None
    try:
        url = urlsplit(destination)
        if url.scheme not in ("", "file") or url.netloc.lower() not in ("", "localhost"):
            return None
        if (not url.scheme and url.netloc) or url.query:
            return None
        path = unquote(url.path, errors="strict")
        if not path.startswith("/") or any(ord(char) < 32 for char in path):
            return None
        return Path(path)
    except ValueError:
        return None


def _destinations(text: str) -> dict[tuple[int, int], Path | AmpAttachment]:
    """Use CommonMark rules to collect source spans, including used reference definitions.

    markdown-it strips list/quote prefixes from inline tokens. Map those unchanged
    content lines back to source, and retain CRLF offsets, rather than reserializing
    Markdown (which would change labels, titles, whitespace and code).
    """
    md = MarkdownIt("commonmark", {"store_labels": True, "inline_definitions": True})
    md.use(footnote_plugin, inline=False, move_to_end=False)
    validate = md.validateLink
    md.validateLink = lambda url: _local_path(url) is not None or validate(url)  # type: ignore[method-assign]
    spans: dict[tuple[int, int], Path | AmpAttachment] = {}
    definitions: dict[str, tuple[int, int, Path | AmpAttachment]] = {}
    used: set[str] = set()
    inline_source = ""
    inline_positions: list[int] = []

    def record(
        source: str, start: int, end: int, positions: list[int],
    ) -> tuple[int, int, Path | AmpAttachment] | None:
        result = md.helpers.parseLinkDestination(source, start, end)
        if not result.ok:
            return None
        path: Path | AmpAttachment | None = _local_path(result.str)
        if path is None:
            try:
                path = AmpAttachment(url=result.str)
            except ValueError:
                return None
        angled = source[start] == "<"
        return positions[start + angled], positions[result.pos - 1 - angled] + 1, path

    def definition(state: StateBlock, start: int, end: int, silent: bool) -> bool:
        accepted = reference(state, start, end, silent)
        if not accepted or silent:
            return accepted
        token = state.tokens[-1]
        label = token.meta["id"]
        if label in definitions:
            return True
        positions = [
            index for line in range(start, state.line)
            for index in range(state.bMarks[line] + state.tShift[line], state.eMarks[line] + 1)
            if index < len(state.src)
        ]
        source = "".join(state.src[index] for index in positions)
        pos = len(token.meta["label"]) + 3  # [label]:
        while pos < len(source) and source[pos] in " \t\n":
            pos += 1
        destination = record(source, pos, len(source), positions)
        # Only the first definition of a reference is effective, even if it is external.
        if destination and state.env["references"][label]["map"] == token.map:
            definitions[label] = destination
        return True

    def destination(state: StateInline, silent: bool) -> bool:
        start = state.pos
        is_image = state.src.startswith("![", start)
        if not is_image and state.src[start] != "[":
            return False
        label_end = md.helpers.parseLinkLabel(state, start + is_image, not is_image)
        first_token = len(state.tokens)
        accepted = (image if is_image else link)(state, silent)
        # Image alt text has its own inline source; it is not a clickable destination.
        if not accepted or silent or state.src is not inline_source:
            return accepted
        token = state.tokens[first_token]
        if label := token.meta.get("label"):
            used.add(label)
        elif label_end >= 0 and state.src[label_end + 1:label_end + 2] == "(":
            pos = label_end + 2
            while pos < state.pos and state.src[pos] in " \t\n":
                pos += 1
            found = record(state.src, pos, state.pos, inline_positions)
            if found:
                spans[found[:2]] = found[2]
        return True

    def inlines(state: StateCore) -> None:
        nonlocal inline_source, inline_positions
        # CommonMark counts only LF as a line break, unlike str.splitlines().
        lines = state.src.split("\n")
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line) + 1)
        footnote = False
        footnote_ranges: list[tuple[int, int]] = []
        for token in state.tokens:
            if token.type == "footnote_reference_open":
                footnote = True
                assert token.map is not None
                footnote_ranges.append((offsets[token.map[0]], offsets[token.map[1]]))
            elif token.type == "footnote_reference_close":
                footnote = False
            elif token.type == "inline" and token.map and not footnote:
                inline_source = token.content
                inline_positions = []
                content_lines = token.content.split("\n")
                for index, line in enumerate(content_lines):
                    number = token.map[0] + index
                    column = lines[number].find(line)
                    if column < 0:
                        break  # Unmappable tab-expanded indentation: leave this token unchanged.
                    offset = offsets[number] + column
                    inline_positions.extend(range(offset, offset + len(line)))
                    if index + 1 < len(content_lines):
                        inline_positions.append(offsets[number] + len(lines[number]))
                if len(inline_positions) != len(token.content):
                    continue
                token.children = []
                state.md.inline.parse(token.content, state.md, state.env, token.children)
        for label in used:
            if ((found := definitions.get(label))
                    and not any(start <= found[0] < end for start, end in footnote_ranges)):
                spans[found[:2]] = found[2]

    md.block.ruler.at("reference", definition)
    md.inline.ruler.at("link", destination)
    md.inline.ruler.at("image", destination)
    md.core.ruler.at("inline", inlines)
    md.parse(text)
    # The parser normalizes CRLF/CR before running rules. Convert only the collected
    # offsets back; all bytes outside a destination remain exactly as authored.
    raw_positions = []
    index = 0
    while index < len(text):
        raw_positions.append(index)
        index += 2 if text[index:index + 2] == "\r\n" else 1
    return {(raw_positions[start], raw_positions[end - 1] + 1): path
            for (start, end), path in sorted(spans.items())}


async def link_history(
    page: HistoryPage, register: Callable[[Path | AmpAttachment], Awaitable[str]], *, deadline: float,
) -> HistoryPage:
    """Best-effort registration within the history request's remaining time and size budgets."""
    # Amp's page contains shared cache objects, and its cursor hashes their native text.
    page = page.model_copy(deep=True)
    destinations = [
        _destinations(item.content.text) if item.kind == "message" and item.role in {"user", "assistant"}
        else {} for item in page.items
    ]
    paths = dict.fromkeys(path for spans in destinations for path in spans.values())
    paths = dict(list(paths.items())[:MAX_LOCAL_FILES])
    links: dict[Path | AmpAttachment, str] = {}
    slots = asyncio.Semaphore(4)

    async def register_one(path: Path | AmpAttachment) -> None:
        async with slots:
            try:
                # Basename is display-only; fragments never reach the opaque attachment endpoint.
                links[path] = f"{await register(path)}#name={quote(path.name, safe='')}"
                if isinstance(path, AmpAttachment):
                    links[path] += f"&amp={quote(path.url, safe='')}"
            except FileFailure:
                pass  # Optional attachments must not discard readable conversation text.

    try:
        async with asyncio.timeout_at(deadline):
            async with asyncio.TaskGroup() as group:
                for path in paths:
                    group.create_task(register_one(path))
    except TimeoutError:
        pass  # Children have settled; retain already registered destinations.

    size = sum(len(item.model_dump_json().encode()) for item in page.items)
    for item, spans in zip(page.items, destinations, strict=True):
        for (start, end), path in sorted(spans.items(), reverse=True):
            if path not in links:
                continue
            previous = item.content.text
            text = previous[:start] + links[path] + previous[end:]
            if len(text) > 8192:
                continue
            before = len(item.model_dump_json().encode())
            item.content.text = text
            after = len(item.model_dump_json().encode())
            if size + after - before > MAX_PAGE or len(page.model_dump_json().encode()) > 512 * 1024:
                item.content.text = previous
            else:
                size += after - before
    return HistoryPage.model_validate(page.model_dump())
