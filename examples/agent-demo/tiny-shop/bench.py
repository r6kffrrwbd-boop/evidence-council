"""Measure how long 1000 cart totals take (prints ms_per_1000_carts=...)."""

import time

from shop import cart_total

LINES = [("mug", 2), ("tee", 1), ("cap", 3), ("sticker", 12)]

start = time.perf_counter()
for _ in range(1000):
    cart_total(LINES)
print(f"ms_per_1000_carts={(time.perf_counter() - start) * 1000:.2f}")
