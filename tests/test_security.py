import time

from app.security import RateLimiter, issue_csrf_token, validate_csrf_token

SECRET = "test-secret"


def test_valid_token_passes_double_submit_check():
    token = issue_csrf_token(SECRET)
    assert validate_csrf_token(SECRET, token, token) is True


def test_token_must_match_the_cookie():
    token = issue_csrf_token(SECRET)
    other = issue_csrf_token(SECRET)
    # Оба токена подписаны верно, но форма и cookie должны совпадать.
    assert validate_csrf_token(SECRET, token, other) is False


def test_forged_and_missing_tokens_are_rejected():
    assert validate_csrf_token(SECRET, "подделка", "подделка") is False
    assert validate_csrf_token(SECRET, None, None) is False
    token = issue_csrf_token(SECRET)
    assert validate_csrf_token("другой-секрет", token, token) is False


def test_rate_limiter_blocks_after_limit_and_is_per_key():
    limiter = RateLimiter(limit=3, window_seconds=60)
    assert [limiter.allow("1.2.3.4") for _ in range(4)] == [True, True, True, False]
    # Другой IP не затронут.
    assert limiter.allow("5.6.7.8") is True


def test_rate_limiter_window_slides():
    limiter = RateLimiter(limit=1, window_seconds=0.05)
    assert limiter.allow("ip") is True
    assert limiter.allow("ip") is False
    time.sleep(0.06)
    assert limiter.allow("ip") is True


def test_zero_limit_disables_the_check():
    limiter = RateLimiter(limit=0)
    assert all(limiter.allow("ip") for _ in range(100))
