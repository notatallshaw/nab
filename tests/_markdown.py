"""Read examples and commands from the Markdown documents exercised by tests."""

from collections.abc import Iterator

from markdown_it import MarkdownIt

_parser = MarkdownIt("commonmark")


def fenced_blocks(text: str) -> Iterator[tuple[str, str]]:
    """Yield the language and literal contents of each fence in document order."""
    for token in _parser.parse(text):
        if token.type == "fence":
            info = token.info.split(maxsplit=1)
            yield info[0] if info else "", token.content


def commands(text: str) -> Iterator[str]:
    """Yield lines of code blocks and individual inline code spans."""
    for token in _parser.parse(text):
        if token.type == "fence":
            yield from token.content.splitlines()
        elif token.children:
            yield from (
                child.content for child in token.children if child.type == "code_inline"
            )
