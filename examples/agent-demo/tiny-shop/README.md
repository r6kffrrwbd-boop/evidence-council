# tiny-shop (synthetic demo project)

A deliberately small project for trying the evidence-council coding agent. All data is synthetic.

- `shop.py` — cart pricing. Missing on purpose: a bulk discount. Slow on purpose: it re-reads the catalog file for every cart line.
- `server.py` — local page and JSON API on 127.0.0.1 (`python3 server.py --port 8765`).
- `tests/` — unittest suite (`python3 -m unittest discover -s tests -t . -v`).
- `bench.py` — prints `ms_per_1000_carts=...`.
