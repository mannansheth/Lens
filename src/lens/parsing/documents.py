"""Section-oriented project document parsing with source locations and provenance."""

from hashlib import sha256
import re

from lens.core.models import CodeSymbol, Knowledge


HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)
CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?im)^(\s*(?:export\s+)?[\"']?[\w.-]*(?:password|passwd|pass|secret|token|api[_-]?key|connectionstring)[\w.-]*[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s#]+)"
)


def _redact(text):
    try:
        from lens.llm.diagnostics import redact
        text = redact(text)
    except ImportError:
        pass
    return CREDENTIAL_ASSIGNMENT.sub(r"\1[REDACTED]", text)


def _front_matter(source):
    match = FRONT_MATTER.match(source)
    metadata = {}
    if not match:
        return source, metadata, 0
    for line in match.group(1).splitlines():
        item = re.match(r"^\s*(version|date|updated|last_updated|status)\s*:\s*[\"']?([^\"'#]+)", line, re.I)
        if item:
            metadata[item[1].lower()] = item[2].strip()
    return source[match.end():], metadata, len(source[:match.end()].splitlines())


def _segments(body, line_offset):
    """Split Markdown at headings, then bound oversized sections at paragraphs."""
    sections = []
    stack = []
    current_title = ""
    current_level = 0
    lines = body.splitlines()
    start = 1
    content = []

    def emit(end):
        nonlocal content
        text = "\n".join(content).strip()
        if text:
            sections.append((" > ".join(stack) or "Document", line_offset + start,
                             line_offset + end, text))
        content = []

    for number, line in enumerate(lines, 1):
        match = HEADING.match(line)
        if match:
            emit(number - 1)
            current_level = len(match[1])
            current_title = match[2].strip()[:180]
            stack = stack[:current_level - 1]
            stack.append(current_title)
            start = number
            content = [line]
        else:
            if not content:
                start = number
            content.append(line)
    emit(len(lines))

    bounded = []
    for title, first, last, text in sections:
        if len(text) <= 5000:
            bounded.append((title, first, last, text))
            continue
        group, size, group_start = [], 0, first
        line_number = first
        for line in text.splitlines(keepends=True):
            if group and size + len(line) > 4200:
                piece = "".join(group).strip()
                if piece:
                    bounded.append((title, group_start, max(group_start, line_number - 1), piece))
                group, size, group_start = [], 0, line_number
            group.append(line)
            size += len(line)
            line_number += 1
        piece = "".join(group).strip()
        if piece:
            bounded.append((title, group_start, last, piece))
    return bounded


def parse(file, data):
    source = data.decode("utf-8-sig") if isinstance(data, bytes) else str(data)
    body, declared, offset = _front_matter(source)
    result = Knowledge()
    try:
        from lens.llm.diagnostics import redact
        safe_declared = {key: redact(value) for key, value in declared.items()}
    except ImportError:
        safe_declared = declared
    for ordinal, (title, first, last, text) in enumerate(_segments(body, offset), 1):
        # Documentation is untrusted input and may accidentally contain copied credentials.
        text = _redact(text)
        name = title.split(" > ")[-1][:180]
        identifier = sha256(f"{file.id}\0document\0{ordinal}\0{title}".encode()).hexdigest()
        result.symbols.append(CodeSymbol(identifier, name, "document_section", title, file.id,
            first, max(first, last), text, {
                "source_type": "documentation",
                "document_heading": title,
                "document_version": safe_declared.get("version"),
                "document_date": safe_declared.get("last_updated", safe_declared.get("updated", safe_declared.get("date"))),
                "document_status": safe_declared.get("status"),
                "freshness": "not_verified",
            }))
    return result
