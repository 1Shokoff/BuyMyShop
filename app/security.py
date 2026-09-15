"""CSRF-токены и простой лимит частоты запросов."""

from __future__ import annotations

import secrets
import time
from collections import defaultdict, deque

from itsdangerous import BadSignature, URLSafeTimedSerializer

CSRF_COOKIE = "csrf"
CSRF_FIELD = "csrf_token"
CSRF_MAX_AGE = 60 * 60 * 6


def _serializer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt="csrf")


def issue_csrf_token(secret: str) -> str:
    # Внутрь кладётся случайный nonce: без него два токена, выданных в одну
    # секунду, совпадали бы, и «двойная отправка» вырождалась бы в проверку
    # одной лишь подписи.
    return _serializer(secret).dumps(secrets.token_urlsafe(16))


def validate_csrf_token(secret: str, token: str | None, cookie: str | None) -> bool:
    """Двойная проверка: токен формы должен быть валиден и совпадать с cookie."""
    if not token or not cookie or token != cookie:
        return False
    try:
        _serializer(secret).loads(token, max_age=CSRF_MAX_AGE)
    except BadSignature:
        return False
    return True


class RateLimiter:
    """Скользящее окно в памяти процесса.

    Для одного веб-контейнера этого достаточно; при горизонтальном
    масштабировании счётчик надо будет вынести в Redis.
    """

    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        if self.limit <= 0:
            return True
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True
