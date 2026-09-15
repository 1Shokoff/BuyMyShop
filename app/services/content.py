"""Тексты сайта из каталога content/ (правятся без пересборки образа)."""

from __future__ import annotations

from pathlib import Path

from markdown_it import MarkdownIt

_md = MarkdownIt("commonmark", {"breaks": True, "linkify": True})
_cache: dict[Path, tuple[float, str]] = {}


def render_markdown_file(path: Path, default: str = "") -> str:
    """HTML из markdown-файла. Кэш инвалидируется по времени изменения файла."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return _md.render(default) if default else ""

    cached = _cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]

    html = _md.render(path.read_text(encoding="utf-8"))
    _cache[path] = (mtime, html)
    return html
