"""Общая подготовка тестов.

Настройки читаются из окружения, а рядом с проектом может лежать рабочий .env
разработчика. Чтобы тесты проверяли поведение кода, а не чужую локальную
конфигурацию, файл .env для них отключается целиком.
"""

import os

from app.config import Settings

Settings.model_config["env_file"] = None

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost:5432/test")
