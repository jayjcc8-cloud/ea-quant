from __future__ import annotations


def escape_diagnostic_label(value: object) -> str:
    """Render an input-derived label without terminal control characters."""
    escaped: list[str] = []
    for character in str(value):
        if character == "\\":
            escaped.append("\\\\")
        elif character == "'":
            escaped.append("\\'")
        elif character.isprintable():
            escaped.append(character)
        else:
            escaped.append(ascii(character)[1:-1])
    return "".join(escaped)
