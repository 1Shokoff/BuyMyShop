"""Общий объект шаблонов Jinja2."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.config import get_settings

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["settings"] = get_settings()
