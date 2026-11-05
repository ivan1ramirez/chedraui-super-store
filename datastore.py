"""Persistencia en archivos de texto (para versionar en Git sin inflar el repositorio).

  data/products.json        -> catálogo, un producto por línea (diffs pequeños)
  data/price_history.jsonl  -> historial de precios, solo se AÑADEN líneas

La base SQLite (data/store.db) es local/efímera: se siembra desde estos archivos cuando está vacía.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from models import BASE_DIR, PriceHistory, Product, SessionLocal

DATA_DIR = os.getenv("DATA_DIR", os.path.join(BASE_DIR, "data"))
PRODUCTS_JSON = os.path.join(DATA_DIR, "products.json")
HISTORY_JSONL = os.path.join(DATA_DIR, "price_history.jsonl")


def export_json(db) -> str:
    """Escribe el catálogo ordenado y con un producto por línea."""
    os.makedirs(DATA_DIR, exist_ok=True)
    rows = sorted((p.to_dict() | {"url": p.url_chedraui} for p in db.query(Product)),
                  key=lambda r: (r["categoria"] or "", r["nombre"], r["sku"]))
    with open(PRODUCTS_JSON, "w", encoding="utf-8") as f:
        f.write("[\n" + ",\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n]\n")
    return PRODUCTS_JSON


def append_history(rows: list[dict]) -> None:
    if not rows:
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HISTORY_JSONL, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def seed_if_empty(db=None) -> int:
    """Carga products.json (+ historial) en la BD si está vacía. Devuelve productos cargados."""
    db = db or SessionLocal()
    if db.query(Product).count() or not os.path.exists(PRODUCTS_JSON):
        return 0
    with open(PRODUCTS_JSON, encoding="utf-8") as f:
        rows = json.load(f)
    for r in rows:
        db.add(Product(
            sku=r["sku"], nombre=r["nombre"], marca=r.get("marca"), precio_actual=r["precio"],
            precio_chedraui_referencia=r.get("precio_referencia", r["precio"]),
            categoria_supermercado=r.get("categoria"), url_imagen=r.get("imagen"),
            url_chedraui=r.get("url"), stock=r.get("stock", 0), disponible=r.get("disponible", True)))
    db.flush()
    if os.path.exists(HISTORY_JSONL):
        ids = {sku: pid for sku, pid in db.query(Product.sku, Product.id)}
        with open(HISTORY_JSONL, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                h = json.loads(line)
                pid = ids.get(h["sku"])
                if pid:
                    fecha = datetime.fromisoformat(h["fecha"])
                    if fecha.tzinfo is None:
                        fecha = fecha.replace(tzinfo=timezone.utc)
                    db.add(PriceHistory(producto_id=pid, precio_anterior=h.get("anterior"),
                                        precio_nuevo=h["nuevo"], fecha=fecha))
    db.commit()
    return len(rows)


def load_catalog(db=None) -> dict:
    """Arranque de la web: BD vacía -> siembra completa; BD con datos -> actualiza precios/stock desde el JSON.

    Así, cada despliegue (p. ej. tras el commit diario de la Action) deja la tienda con los precios más recientes
    sin perder los pedidos guardados en la BD.
    """
    db = db or SessionLocal()
    if not os.path.exists(PRODUCTS_JSON):
        return {"nuevos": 0, "actualizados": 0}
    if db.query(Product).count() == 0:
        return {"nuevos": seed_if_empty(db), "actualizados": 0}
    with open(PRODUCTS_JSON, encoding="utf-8") as f:
        rows = json.load(f)
    existing = {p.sku: p for p in db.query(Product)}
    nuevos = actualizados = 0
    for r in rows:
        p = existing.get(r["sku"])
        stock, disp = r.get("stock", 0), r.get("disponible", True)
        if p is None:
            db.add(Product(
                sku=r["sku"], nombre=r["nombre"], marca=r.get("marca"), precio_actual=r["precio"],
                precio_chedraui_referencia=r.get("precio_referencia", r["precio"]),
                categoria_supermercado=r.get("categoria"), url_imagen=r.get("imagen"),
                url_chedraui=r.get("url"), stock=stock, disponible=disp))
            nuevos += 1
            continue
        price_changed = abs(p.precio_actual - r["precio"]) > 0.001
        if price_changed or p.stock != stock or p.disponible != disp or p.nombre != r["nombre"] \
                or p.categoria_supermercado != r.get("categoria"):
            if price_changed:
                db.add(PriceHistory(producto_id=p.id, precio_anterior=p.precio_actual, precio_nuevo=r["precio"]))
            p.nombre, p.marca = r["nombre"], r.get("marca")
            p.precio_actual = r["precio"]
            p.precio_chedraui_referencia = r.get("precio_referencia", r["precio"])
            p.categoria_supermercado, p.url_imagen = r.get("categoria"), r.get("imagen")
            p.url_chedraui, p.stock, p.disponible = r.get("url"), stock, disp
            actualizados += 1
    db.commit()
    return {"nuevos": nuevos, "actualizados": actualizados}
