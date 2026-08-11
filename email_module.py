"""Bezpieczny moduł powiadomień e-mail.

W tej wersji aplikacji automatyczne wiadomości są wyłączone. Funkcje zachowują
stabilny kontrakt wywołań, dzięki czemu import aplikacji i testy wdrożeniowe nie
kończą się błędem składniowym.
"""

from __future__ import annotations

import os
from typing import Any


def email_config_summary() -> dict[str, Any]:
    return {
        "configured": False,
        "enabled": False,
        "admin_email": (os.environ.get("ADMIN_EMAIL") or "").strip(),
        "missing": [],
        "provider": "disabled",
    }


def _disabled_result(*, to: str = "") -> dict[str, Any]:
    return {
        "ok": False,
        "skipped": True,
        "enabled": False,
        "to": to,
        "error": "Automatyczne powiadomienia e-mail są wyłączone.",
    }


def send_email(to: str, subject: str, html: str, text: str = "", **_: Any) -> dict[str, Any]:
    return _disabled_result(to=(to or "").strip())


def send_order_confirmation(order: dict[str, Any], items: list[dict[str, Any]], admin_email: str = "", **_: Any) -> dict[str, Any]:
    recipient = (order.get("customer_email") or admin_email or "").strip()
    return _disabled_result(to=recipient)


def send_invoice_available(invoice: dict[str, Any], **_: Any) -> dict[str, Any]:
    return _disabled_result(to=(invoice.get("buyer_email") or invoice.get("customer_email") or "").strip())


def send_payment_reminder(invoice: dict[str, Any], **_: Any) -> dict[str, Any]:
    return _disabled_result(to=(invoice.get("buyer_email") or invoice.get("customer_email") or "").strip())
