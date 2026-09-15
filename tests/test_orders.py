from decimal import Decimal

import pytest

from app.config import Settings
from app.services.orders import (
    ValidationProblem,
    amounts_match,
    generate_public_code,
    normalize_customer_ref,
    validate_customer_ref,
    validate_email,
)


def settings(**overrides) -> Settings:
    base = {
        "customer_field_pattern": r"^\d{15}$",
        "customer_field_error": "Введите ровно 15 цифр.",
        "require_email": False,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("123456789012345", "123456789012345"),
        ("  123456789012345  ", "123456789012345"),
        ("123 456 789 012 345", "123456789012345"),
        ("123-456-789-012-345", "123456789012345"),
    ],
)
def test_customer_ref_normalization(raw, expected):
    assert normalize_customer_ref(raw) == expected
    assert validate_customer_ref(raw, settings()) == expected


@pytest.mark.parametrize("raw", ["", "12345", "1234567890123456", "12345678901234a"])
def test_customer_ref_rejects_bad_input(raw):
    with pytest.raises(ValidationProblem) as exc:
        validate_customer_ref(raw, settings())
    assert exc.value.field == "customer_ref"


def test_email_optional_by_default():
    assert validate_email("", settings()) is None


def test_email_required_when_configured():
    with pytest.raises(ValidationProblem):
        validate_email("", settings(require_email=True))


def test_email_is_lowercased_and_validated():
    assert validate_email("  User@Example.COM ", settings()) == "user@example.com"
    with pytest.raises(ValidationProblem):
        validate_email("not-an-email", settings())


def test_amounts_match_guards_against_underpayment():
    assert amounts_match(Decimal("990.00"), Decimal("990.000")) is True
    assert amounts_match(Decimal("990.00"), Decimal("1.00")) is False
    # Агрегатор не прислал сумму — сверять нечем, доверяем подписи.
    assert amounts_match(Decimal("990.00"), None) is True


def test_public_code_shape():
    code = generate_public_code()
    assert len(code) == 8
    # Похожие символы (0/O, 1/I, 5/S, 8/B, 2/Z) исключены, чтобы код не путали на слух.
    assert not set(code) & set("012558BIOSZ")
