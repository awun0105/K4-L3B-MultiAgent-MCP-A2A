from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

ORDER_ID_KEYS = {
    "order_id",
    "order_ids",
    "candidate_order_id",
    "candidate_order_ids",
    "resolved_order_id",
}
CUSTOMER_KEYS = {
    "customer_unique_id",
    "customer_id",
    "customeruniqueid",
    "customer_unique_id_hint",
}
SELLER_KEYS = {"seller_id", "seller_ids"}
ITEM_KEYS = {"order_item_id", "item_id", "order_item_ids", "item_ids"}
PAYMENT_KEYS = {"payment_id", "payment_sequential", "payment_reference", "payment_references"}
SHIPMENT_KEYS = {"shipment_id", "tracking_number", "freight_id", "shipment_ids"}


def as_str(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not value.is_integer():
            text = str(value)
        else:
            text = str(int(value) if isinstance(value, float) else value)
        return text or None
    text = str(value).strip()
    return text or None


def unique_ids(values: Iterable[Any], *, limit: int = 20) -> list[str]:
    seen: list[str] = []
    for value in values:
        text = as_str(value)
        if text and text not in seen:
            seen.append(text)
        if len(seen) >= limit:
            break
    return seen


def collect_ids(payload: Any, keys: set[str], *, limit: int = 20) -> list[str]:
    found: list[str] = []

    def walk(node: Any) -> None:
        if len(found) >= limit:
            return
        if isinstance(node, Mapping):
            for key, value in node.items():
                key_l = str(key).lower()
                if key_l in keys or key_l.rstrip("s") in keys:
                    if isinstance(value, list):
                        found.extend(unique_ids(value, limit=limit - len(found)))
                    else:
                        found.extend(unique_ids([value], limit=limit - len(found)))
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return unique_ids(found, limit=limit)


def deep_get(payload: Any, *names: str) -> Any:
    wanted = {name.lower() for name in names}
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if str(key).lower() in wanted and value not in (None, "", []):
                return value
        for value in payload.values():
            found = deep_get(value, *names)
            if found not in (None, "", []):
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = deep_get(item, *names)
            if found not in (None, "", []):
                return found
    return None


def as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def parse_dt(value: Any) -> datetime | None:
    text = as_str(value)
    if not text:
        return None
    cleaned = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
    return None


def case_order_candidates(case: Mapping[str, Any]) -> list[str]:
    ids = collect_ids(case, ORDER_ID_KEYS)
    hints = case.get("entity_hints") or case.get("candidates") or case.get("candidate_entities")
    if isinstance(hints, list):
        for item in hints:
            if isinstance(item, str):
                ids.extend(unique_ids([item]))
            elif isinstance(item, Mapping):
                ids.extend(collect_ids(item, ORDER_ID_KEYS))
    return unique_ids(ids)


def case_customer_id(case: Mapping[str, Any]) -> str | None:
    ids = collect_ids(case, CUSTOMER_KEYS, limit=1)
    return ids[0] if ids else None


def money_sum(payload: Any, *field_names: str) -> float | None:
    total = 0.0
    found = False
    wanted = {name.lower() for name in field_names}

    def walk(node: Any) -> None:
        nonlocal total, found
        if isinstance(node, Mapping):
            for key, value in node.items():
                if str(key).lower() in wanted:
                    amount = as_float(value)
                    if amount is not None:
                        total += amount
                        found = True
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return round(total, 2) if found else None
