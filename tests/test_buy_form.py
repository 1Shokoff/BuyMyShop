"""Проверки формы заказа без обращения к базе."""

from app.config import Settings
from app.routes.public import _form_state


def defaults(monkeypatch) -> Settings:
    """Настройки по умолчанию: переменные окружения оболочки не учитываются."""
    for name in ("REQUIRE_REF_CONFIRMATION", "CUSTOMER_FIELD_LABEL"):
        monkeypatch.delenv(name, raising=False)
    return Settings()


def test_confirmation_is_required_by_default(monkeypatch):
    # ID нельзя исправить после оплаты, поэтому подтверждение включено из коробки.
    assert defaults(monkeypatch).require_ref_confirmation is True


def test_default_field_label_names_the_client_id(monkeypatch):
    assert defaults(monkeypatch).customer_field_label == "ID клиента"


def test_form_state_keeps_user_input_after_a_validation_error():
    state = _form_state("123 456", "User@Example.com", "robokassa", "1")
    assert state == {
        "customer_ref": "123 456",
        "email": "User@Example.com",
        "provider": "robokassa",
        "ref_confirmed": True,
    }


def test_form_state_marks_unchecked_confirmation():
    assert _form_state("1", "", "lava", "")["ref_confirmed"] is False
