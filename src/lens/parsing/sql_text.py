"""Small SQL text utilities; syntax interpretation remains SQLGlot's job."""


def masked_comments(text: str) -> str:
    """Mask comments without changing offsets or touching string/identifier quotes."""
    result = list(text)
    i = 0
    quote = None
    while i < len(text):
        char = text[i]
        if quote:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                if i + 1 < len(text) and text[i + 1] == quote:
                    i += 2
                    continue
                quote = None
        elif char in {"'", '"', "`"}:
            quote = char
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = len(text) if end < 0 else end + 2
            for position in range(i, end):
                if text[position] != "\n":
                    result[position] = " "
            i = end
            continue
        elif char == "#" or (
            text.startswith("--", i) and (i + 2 == len(text) or text[i + 2].isspace())
        ):
            end = text.find("\n", i)
            end = len(text) if end < 0 else end
            result[i:end] = " " * (end - i)
            i = end
            continue
        i += 1
    return "".join(result)


def statements(text: str) -> list[tuple[int, str]]:
    """Split plain SQL on semicolons outside quotes/comments, retaining offsets."""
    masked = masked_comments(text)
    quote = None
    start = 0
    i = 0
    result = []
    while i < len(masked):
        char = masked[i]
        if quote:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                if i + 1 < len(masked) and masked[i + 1] == quote:
                    i += 2
                    continue
                quote = None
        elif char in {"'", '"', "`"}:
            quote = char
        elif char == ";":
            result.append((start, text[start : i + 1]))
            start = i + 1
        i += 1
    if masked[start:].strip():
        result.append((start, text[start:]))
    return result
