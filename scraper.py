"""
Scraper del departamento de Supermercado de Chedraui México (VTEX).

Estrategia:
  1. Consulta el árbol de categorías público de VTEX y se queda SOLO con las ramas de supermercado
     (lista blanca + lista negra de palabras).
  2. Pagina el endpoint JSON de búsqueda. VTEX limita cada consulta a 2,500 resultados: si una categoría
     llega a ese tope, se divide automáticamente en sus subcategorías.
  3. Limpia precios y hace upsert idempotente por SKU (PriceHistory solo si cambió el precio).
  4. Exporta data/products.json (un producto por línea) y añade los cambios a data/price_history.jsonl.
  5. En una sincronización COMPLETA sin errores, los productos que ya no aparecen se marcan no disponibles.

Uso:
    python scraper.py                    # requests (rápido)
    python scraper.py --browser          # Playwright headless
    python scraper.py --max-pages 2      # prueba corta
    python scraper.py --categories 2/21  # rutas de categoría manuales (idRaiz/idHija)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
import unicodedata
from typing import Any, Iterable

import requests

from datastore import append_history, export_json, seed_if_empty
from models import PriceHistory, Product, SessionLocal, init_db, utcnow

BASE_URL = "https://www.chedraui.com.mx"
CATEGORY_TREE_URL = BASE_URL + "/api/catalog_system/pub/category/tree/{depth}"
SEARCH_URL = BASE_URL + "/api/catalog_system/pub/products/search"

PAGE_SIZE = 50            # VTEX permite máx. 50 por petición
VTEX_MAX_OFFSET = 2500    # VTEX no pagina más allá de 2,500 resultados por consulta
TREE_DEPTH = 4
REQUEST_TIMEOUT = float(os.getenv("SCRAPER_TIMEOUT", "20"))
SLEEP_SECONDS = float(os.getenv("SCRAPER_SLEEP", "1.5"))
MARKUP_PCT = float(os.getenv("MARKUP_PCT", "0"))  # margen sobre el precio de Chedraui

# Categorías raíz que SÍ interesan (comparación sin acentos, minúsculas)
INCLUDE_KEYWORDS = [
    "super", "despensa", "abarrotes", "lacteos", "fruta", "verdura", "carne",
    "pollo", "pescado", "marisco", "salchichoneria", "embutido", "panaderia", "tortiller",
    "bebidas", "cerveza", "congelado", "snack", "botana", "cereal",
    "galleta", "huevo", "queso", "yogur", "aceite", "arroz", "frijol", "pasta", "enlatado",
    "limpieza", "cuidado personal", "canasta",
]
# Departamentos / categorías que se IGNORAN explícitamente (nivel raíz y nivel 2)
EXCLUDE_KEYWORDS = [
    "tecnologia", "electronica", "electrodomestico", "linea blanca", "hogar", "muebles",
    "ropa", "moda", "calzado", "jugueteria", "juguete", "deportes", "automotriz", "ferreteria",
    "videojuego", "computo", "celular", "television", "pantalla", "colchon", "jardin",
    "libreria", "papeleria", "viaje",
    # excluidas por decisión del negocio:
    "mascota", "vinos", "licores", "bebes",
]
# Nombre de la(s) raíz(ces) del departamento de supermercado en el árbol VTEX
SUPER_ROOT_NAMES = ["supermercado", "super", "despensa"]

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-MX,es;q=0.9,en;q=0.5",
    "Referer": BASE_URL + "/",
}

log = logging.getLogger("scraper")


# --------------------------------------------------------------------------- utilidades
def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower().strip()


def clean_price(value: Any) -> float | None:
    """'$1,234.50 MXN' -> 1234.5 ; 12 -> 12.0 ; 'N/A' -> None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2) if value > 0 else None
    s = re.sub(r"(?i)mxn|mn|\$|\s", "", str(value))
    s = s.replace(",", "")
    m = re.search(r"\d+(?:\.\d+)?", s)
    if not m:
        return None
    price = float(m.group())
    return round(price, 2) if price > 0 else None


def is_excluded(name: str) -> bool:
    n = norm(name)
    return any(norm(k) in n for k in EXCLUDE_KEYWORDS)


def is_super_category(name: str) -> bool:
    n = norm(name)
    return not is_excluded(name) and any(norm(k) in n for k in INCLUDE_KEYWORDS)


# --------------------------------------------------------------------------- transporte
class Fetcher:
    """GET JSON con reintentos. Backend: requests o Playwright headless."""

    def __init__(self, use_browser: bool = False):
        self.use_browser = use_browser
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._pw = self._browser = self._ctx = None
        if use_browser:
            self._start_browser()

    def _start_browser(self) -> None:
        from playwright.sync_api import sync_playwright  # import perezoso (opcional)
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._ctx = self._browser.new_context(user_agent=HEADERS["User-Agent"], locale="es-MX")
        page = self._ctx.new_page()
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)  # obtiene cookies
        time.sleep(2)
        page.close()

    def get_json(self, url: str, params: dict | None = None, retries: int = 3):
        for attempt in range(1, retries + 1):
            try:
                if self.use_browser:
                    resp = self._ctx.request.get(url, params=params, timeout=REQUEST_TIMEOUT * 1000)
                    status, text = resp.status, resp.text()
                else:
                    resp = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
                    status, text = resp.status_code, resp.text
                if status in (200, 206):
                    return json.loads(text)
                if status in (403, 429, 503):
                    wait = SLEEP_SECONDS * attempt * 3
                    log.warning("HTTP %s en %s (intento %d) - esperando %.1fs", status, url, attempt, wait)
                    time.sleep(wait)
                    continue
                log.warning("HTTP %s en %s", status, url)
                return None
            except Exception as exc:  # noqa: BLE001
                log.warning("Error %s en %s (intento %d)", exc, url, attempt)
                time.sleep(SLEEP_SECONDS * attempt)
        return None

    def close(self) -> None:
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()


# --------------------------------------------------------------------------- categorías
def build_node(n: dict, parent_path: str = "") -> dict:
    path = f"{parent_path}/{n['id']}" if parent_path else str(n["id"])
    return {"path": path, "name": n["name"],
            "children": [build_node(c, path) for c in (n.get("children") or [])]}


def discover_categories(fetcher: Fetcher | None, tree: list | None = None) -> list[dict]:
    """Nodos {path, name, children} de supermercado. path = "idRaiz/idHija" (necesario en fq=C:/ruta/)."""
    if tree is None:
        tree = fetcher.get_json(CATEGORY_TREE_URL.format(depth=TREE_DEPTH)) or []
    result: list[dict] = []
    for root in tree:
        root_name = norm(root.get("name", ""))
        if any(root_name == r or root_name.startswith(r) for r in SUPER_ROOT_NAMES):
            children = root.get("children") or []
            if children:
                for ch in children:
                    if not is_excluded(ch["name"]):
                        result.append(build_node(ch, str(root["id"])))
            else:
                result.append(build_node(root))
        elif is_super_category(root.get("name", "")):
            result.append(build_node(root))  # raíz suelta tipo "Lácteos"
    seen, out = set(), []
    for node in result:
        if node["path"] not in seen:
            seen.add(node["path"])
            out.append(node)
    return out


# --------------------------------------------------------------------------- parseo
def parse_product(raw: dict, categoria: str) -> dict | None:
    try:
        item = raw["items"][0]
        offer = item["sellers"][0]["commertialOffer"]
    except (KeyError, IndexError, TypeError):
        return None
    price = clean_price(offer.get("Price"))
    if price is None:
        return None
    images = item.get("images") or []
    stock = int(offer.get("AvailableQuantity") or 0)
    return {
        "sku": str(item.get("itemId") or raw.get("productId")),
        "nombre": (raw.get("productName") or "").strip(),
        "marca": (raw.get("brand") or "").strip() or None,
        "precio_ref": price,
        "categoria": categoria,
        "imagen": images[0].get("imageUrl") if images else None,
        "url": raw.get("link") or None,
        "stock": stock,
        "disponible": stock > 0,
    }


def _paginate(fetcher: Fetcher, cat_path: str, categoria: str, max_pages: int | None) -> Iterable[tuple[bool, dict | None]]:
    """Genera (es_resultado_crudo, producto_parseado|None) por cada resultado que devuelve VTEX."""
    start, page = 0, 0
    while start < VTEX_MAX_OFFSET:
        if max_pages and page >= max_pages:
            break
        params = {"fq": f"C:/{cat_path}/", "_from": start, "_to": start + PAGE_SIZE - 1}
        data = fetcher.get_json(SEARCH_URL, params=params)
        time.sleep(SLEEP_SECONDS)  # cortesía / evitar bloqueos
        if not data:
            break
        for raw in data:
            yield True, parse_product(raw, categoria)
        if len(data) < PAGE_SIZE:
            break
        start += PAGE_SIZE
        page += 1


def fetch_node(fetcher: Fetcher, node: dict, categoria: str, max_pages: int | None, seen: set[str]) -> Iterable[dict]:
    """Productos de una categoría; si llega al tope de VTEX, baja a sus subcategorías."""
    raw_count = 0
    for _, parsed in _paginate(fetcher, node["path"], categoria, max_pages):
        raw_count += 1
        if parsed and parsed["sku"] not in seen:
            seen.add(parsed["sku"])
            yield parsed
    if raw_count >= VTEX_MAX_OFFSET and not max_pages:
        if node["children"]:
            log.info("'%s' alcanzó el tope de %d: dividiendo en %d subcategorías",
                     node["name"], VTEX_MAX_OFFSET, len(node["children"]))
            for child in node["children"]:
                yield from fetch_node(fetcher, child, categoria, max_pages, seen)
        else:
            log.warning("'%s' alcanzó el tope de %d y no tiene subcategorías: puede estar incompleta",
                        node["name"], VTEX_MAX_OFFSET)


# --------------------------------------------------------------------------- base de datos
def upsert_product(db, p: dict, hist: list[dict]) -> str:
    """Upsert idempotente. Retorna 'nuevo' | 'cambio' | 'igual' y anota cambios de precio en `hist`."""
    new_price = round(p["precio_ref"] * (1 + MARKUP_PCT / 100), 2)
    now = utcnow().isoformat()
    prod = db.query(Product).filter_by(sku=p["sku"]).one_or_none()
    if prod is None:
        prod = Product(
            sku=p["sku"], nombre=p["nombre"], marca=p["marca"], precio_actual=new_price,
            precio_chedraui_referencia=p["precio_ref"], categoria_supermercado=p["categoria"],
            url_imagen=p["imagen"], url_chedraui=p["url"], stock=p["stock"], disponible=p["disponible"])
        db.add(prod)
        db.flush()
        db.add(PriceHistory(producto_id=prod.id, precio_anterior=None, precio_nuevo=new_price))
        hist.append({"sku": p["sku"], "fecha": now, "anterior": None, "nuevo": new_price})
        return "nuevo"
    changed = abs(prod.precio_chedraui_referencia - p["precio_ref"]) > 0.001
    if changed:
        db.add(PriceHistory(producto_id=prod.id, precio_anterior=prod.precio_actual, precio_nuevo=new_price))
        hist.append({"sku": p["sku"], "fecha": now, "anterior": prod.precio_actual, "nuevo": new_price})
    prod.nombre, prod.marca = p["nombre"], p["marca"]
    prod.precio_actual, prod.precio_chedraui_referencia = new_price, p["precio_ref"]
    prod.categoria_supermercado, prod.url_imagen = p["categoria"], p["imagen"]
    prod.url_chedraui, prod.stock, prod.disponible = p["url"], p["stock"], p["disponible"]
    return "cambio" if changed else "igual"


# --------------------------------------------------------------------------- orquestación
def run_sync(use_browser: bool = False, max_pages: int | None = None,
             category_ids: list[str] | None = None) -> dict:
    init_db()
    fetcher = Fetcher(use_browser=use_browser)
    stats = {"nuevo": 0, "cambio": 0, "igual": 0, "retirados": 0, "categorias": 0, "errores": 0}
    db = SessionLocal()
    try:
        seeded = seed_if_empty(db)
        if seeded:
            log.info("BD sembrada desde data/products.json (%d productos)", seeded)
        cats = ([{"path": c, "name": f"cat-{c}", "children": []} for c in category_ids] if category_ids
                else discover_categories(fetcher))
        if not cats:
            raise RuntimeError("No se encontraron categorías de supermercado. "
                               "Usa --categories o ajusta SUPER_ROOT_NAMES.")
        log.info("Categorías de supermercado: %s", [c["name"] for c in cats])
        stats["categorias"] = len(cats)
        full_run = not max_pages and not category_ids
        seen_skus: set[str] = set()
        history: list[dict] = []
        for node in cats:
            cat_hist: list[dict] = []
            cat_seen: set[str] = set()
            cat_stats = {"nuevo": 0, "cambio": 0, "igual": 0}
            try:
                for p in fetch_node(fetcher, node, node["name"], max_pages, cat_seen):
                    cat_stats[upsert_product(db, p, cat_hist)] += 1
                if not cat_seen:
                    log.warning("Categoría SIN productos: %s (ruta %s)", node["name"], node["path"])
                db.commit()
                for k, v in cat_stats.items():
                    stats[k] += v
                history.extend(cat_hist)
                seen_skus |= cat_seen
                log.info("OK %s (%d productos) -> %s", node["name"], len(cat_seen), stats)
            except Exception:  # noqa: BLE001
                db.rollback()
                stats["errores"] += 1
                log.exception("Fallo en categoría %s", node["name"])
        if full_run and stats["errores"] == 0:
            for prod in db.query(Product).filter(Product.disponible.is_(True)):
                if prod.sku not in seen_skus:
                    prod.disponible, prod.stock = False, 0
                    stats["retirados"] += 1
            db.commit()
        append_history(history)
        export_json(db)
    finally:
        db.close()
        fetcher.close()
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Sync de precios Chedraui Supermercado")
    ap.add_argument("--browser", action="store_true", help="usar Playwright headless")
    ap.add_argument("--max-pages", type=int, default=None)
    ap.add_argument("--categories", type=str, default=None,
                    help="rutas separadas por coma, p. ej. 2/21,2/22 (o IDs simples)")
    args = ap.parse_args()
    ids = [x.strip() for x in args.categories.split(",") if x.strip()] if args.categories else None
    print(json.dumps(run_sync(args.browser, args.max_pages, ids), indent=2))
