"""Tiny shop pricing (synthetic demo project for the evidence-council coding agent)."""

import json
from pathlib import Path

CATALOG_PATH = Path(__file__).with_name("catalog.json")


def load_catalog():
    with CATALOG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def price_of(sku):
    # Looks up one product's price in cents.
    return load_catalog()[sku]["price"]


def cart_total(lines):
    """lines: list of (sku, quantity) pairs. Returns amounts in cents."""
    subtotal = 0
    for sku, qty in lines:
        if qty <= 0:
            raise ValueError("quantity must be positive")
        subtotal += price_of(sku) * qty
    discount = 0
    total = subtotal - discount
    return {"subtotal": subtotal, "discount": discount, "total": total, "label": ""}
