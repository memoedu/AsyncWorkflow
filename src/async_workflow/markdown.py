from markdown_it import MarkdownIt


def parse_outline(markdown: str) -> dict:
    return _parse_heading_outline(markdown)[0]


def compile_recording_outline(markdown: str) -> str:
    """Validate the model's H1 contract and emit the title as the first bullet."""
    markdown = markdown.strip()
    tree, heading_end = _parse_heading_outline(markdown)
    title = tree['text'].replace('\n', ' ')
    body = ''.join(markdown.splitlines(keepends=True)[heading_end:])
    return '- ' + title + ('\n' + body if body else '')


def parse_recording_outline(markdown: str) -> dict:
    """Read the first bullet as the title, including support for saved H1 outlines."""
    markdown = markdown.strip()
    if markdown.startswith('- '):
        title, _, body = markdown.partition('\n')
        return _parse_body(title[2:].rstrip('\r'), MarkdownIt('commonmark', {'html': False}).parse(body))
    return parse_outline(markdown)


def _parse_heading_outline(markdown: str) -> tuple[dict, int]:
    tokens = MarkdownIt("commonmark", {"html": False}).parse(markdown.strip())
    if len(tokens) < 3 or tokens[0].type != "heading_open" or tokens[0].tag != "h1":
        raise ValueError("outline must start with one H1")
    return _parse_body(tokens[1].content, tokens[3:]), tokens[0].map[1]


def _parse_body(title, tokens) -> dict:
    root = {"text": title, "children": []}
    stack = [root]
    for tok in tokens:
        if tok.type == "list_item_open":
            node = {"text": "", "children": []}
            stack[-1]["children"].append(node)
            stack.append(node)
        elif tok.type == "list_item_close":
            if not stack[-1]["text"].strip():
                raise ValueError("empty bullet")
            stack.pop()
        elif tok.type == "inline":
            if len(stack) == 1:
                raise ValueError("body must consist of bullets")
            if stack[-1]["text"]:
                stack[-1]["text"] += "\n"
            stack[-1]["text"] += tok.content
        elif tok.type not in {"bullet_list_open", "bullet_list_close", "paragraph_open", "paragraph_close"}:
            raise ValueError("unexpected outline token: " + tok.type)
    if len(stack) != 1 or not root["text"].strip():
        raise ValueError("invalid outline tree")
    return root
