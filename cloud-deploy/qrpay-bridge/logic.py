from __future__ import annotations

import hashlib
import hmac
import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable


CENT = Decimal("0.01")
DOWN = 0
UP = 1
PENDING = 2
MAINTENANCE = 3


def money_to_decimal(value: str | int | float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def money_to_cents(value: str | int | float | Decimal) -> int:
    return int(money_to_decimal(value) * 100)


def cents_to_money(cents: int) -> Decimal:
    return (Decimal(cents) / Decimal(100)).quantize(CENT, rounding=ROUND_HALF_UP)


def allocate_unique_amount(
    base_amount: str | int | float | Decimal,
    occupied_amounts: Iterable[str | int | float | Decimal],
    jitter_cents: int,
) -> Decimal:
    base_cents = money_to_cents(base_amount)
    occupied = {money_to_cents(amount) for amount in occupied_amounts}
    if jitter_cents <= 0 or base_cents not in occupied:
        return cents_to_money(base_cents)

    for offset in range(1, jitter_cents + 1):
        candidate = base_cents + offset
        if candidate not in occupied:
            return cents_to_money(candidate)

    raise ValueError("no unique payment amount is available in the configured jitter window")


def normalize_epay_alipay_memo(memo: str | None) -> str:
    if not memo:
        return ""
    text = str(memo).strip()
    for prefix in (
        "请勿添加备注-",
        "請勿添加備註-",
        "请勿添加备注:",
        "请勿添加备注：",
        "备注-",
    ):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    return text.strip()


def is_amount_match(expected: str | int | float | Decimal, paid: str | int | float | Decimal) -> bool:
    return money_to_cents(expected) == money_to_cents(paid)


def build_vmq_sign(pay_id: str, pay_type: str, price: str, really_price: str, key: str) -> str:
    raw = f"{pay_id}{pay_type}{price}{really_price}{key}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def verify_vmq_sign(pay_id: str, pay_type: str, price: str, really_price: str, key: str, sign: str) -> bool:
    expected = build_vmq_sign(pay_id, pay_type, price, really_price, key)
    return bool(sign) and hmac.compare_digest(expected.lower(), sign.lower())


def next_notify_interval_minutes(attempt: int) -> int | None:
    schedule = {
        1: 1,
        2: 2,
        3: 16,
        4: 36,
        5: 60,
    }
    return schedule.get(attempt)


def compute_validity_days(value: int, unit: str | None) -> int:
    unit = (unit or "day").strip().lower()
    if unit == "week":
        return value * 7
    if unit == "month":
        return value * 30
    return value


def safe_order_no(value: str) -> str:
    value = (value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{6,80}", value):
        raise ValueError("invalid order number")
    return value


def monitor_status_name(status: int | None) -> str:
    names = {
        DOWN: "DOWN",
        UP: "UP",
        PENDING: "PENDING",
        MAINTENANCE: "MAINTENANCE",
    }
    return names.get(status, "UNKNOWN")


def next_monitor_state(ok: bool, previous_retries: int, max_retries: int) -> tuple[int, int]:
    if ok:
        return UP, 0
    if max_retries > 0 and previous_retries < max_retries:
        return PENDING, previous_retries + 1
    return DOWN, previous_retries + 1


def is_important_beat(is_first: bool, previous_status: int | None, current_status: int) -> bool:
    return (
        is_first
        or (previous_status == DOWN and current_status == MAINTENANCE)
        or (previous_status == UP and current_status == MAINTENANCE)
        or (previous_status == MAINTENANCE and current_status == DOWN)
        or (previous_status == MAINTENANCE and current_status == UP)
        or (previous_status == UP and current_status == DOWN)
        or (previous_status == DOWN and current_status == UP)
        or (previous_status == PENDING and current_status == DOWN)
    )


def should_notify_beat(is_first: bool, previous_status: int | None, current_status: int) -> bool:
    return (
        is_first
        or (previous_status == MAINTENANCE and current_status == DOWN)
        or (previous_status == UP and current_status == DOWN)
        or (previous_status == DOWN and current_status == UP)
        or (previous_status == PENDING and current_status == DOWN)
    )
