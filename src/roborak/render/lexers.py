"""Guessing a syntax-highlighting language from a path.

Shared, because both the panels and the markdown report want to colour a
snippet and neither should have to know how the other spells `.tsx`.
"""

from __future__ import annotations

LEXERS = {
    "py": "python",
    "js": "javascript",
    "jsx": "jsx",
    "ts": "typescript",
    "tsx": "tsx",
    "php": "php",
    "go": "go",
    "rs": "rust",
    "rb": "ruby",
    "java": "java",
    "kt": "kotlin",
    "cs": "csharp",
    "c": "c",
    "h": "c",
    "cpp": "cpp",
    "sh": "bash",
    "sql": "sql",
    "yaml": "yaml",
    "yml": "yaml",
    "json": "json",
    "toml": "toml",
    "html": "html",
    "css": "css",
    "scss": "scss",
    "vue": "vue",
    "swift": "swift",
    "tf": "terraform",
}


def lexer_for(path: str) -> str:
    return LEXERS.get(path.rsplit(".", 1)[-1].lower(), "text")
