"""
Scraper del departamento de Supermercado de Chedraui México (VTEX).

Estrategia:
  1. Consulta el árbol de categorías público de VTEX (/api/catalog_system/pub/category/tree/N)
     y se queda SOLO con las ramas de supermercado (lista blanca + lista negra de palabras).
  2. Pagina el endpoint de búsqueda JSON (/api/catalog_system/pub/products/search).
  3. Limpia precios, hace upsert idempotente por SKU y registra PriceHistory sólo si cambió.
  4. Exporta data/products.json.

Uso:
    python scraper.py                  # requests (rápido)
    python scraper.py --browser        # Playwright headless (si requests es bloqueado)
    python scraper.py --max-pages 2    # prueba corta
    python scraper.py --categories 12/45,12/46   # rutas de categoría manuales (sobrescribe autodescubrimiento)

NOTA: los endpoints son los típicos de VTEX; Chedraui puede cambiarlos o bloquear bots.
Revisa los términos de uso del sitio y mantén un ritmo de peticiones bajo.
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

from models import BASE_DIR, PriceHistory, Product, SessionLocal, init_db

BASE_URL = "https://www.chedraui.com.mx"
CATEGORY_TREE_URL = BASE_URL + "/api/catalog_system/pub/category/tree/{depth}"
SEARCH_URL = BASE_URL + "/api/catalog_system/pub/products/search"

PAGE_SIZE = 50            # VTEX permite máx. 50 por petición
VTEX_MAX_OFFSET = 2500    # VTEX no pagina más allá de ~2500 resultados por consulta
REQUEST_TIMEOUT = float(os.getenv("SCRAPER_TIMEOUT", "20"))
SLEEP_SECONDS = float(os.getenv("SCRAPER_SLEEP", "1.5"))
MARKUP_PCT = float(os.getenv("MARKUP_PCT", "0"))  # margen sobre el precio de Chedraui

# Categorías raíz/hijas que SÍ nos interesan (comparación sin acentos, minúsculas)
INCLUDE_KEYWORDS = [
    "super", "despensa", "abarrotes", "lacteos", "lácteos", "fruta", "verdura", "carne",
    "pollo", "pescado", "marisco", "salchichoneria", "embutido", "panaderia", "tortiller",
    "bebidas", "cerveza", "vinos", "licores", "congelado", "snack", "botana", "cereal",
    "galleta", "huevo", "queso", "yogur", "aceite", "arroz", "frijol", "pasta", "enlatado",
    "limpieza", "cuidado personal", "bebes", "bebés", "mascota", "canasta",
]
# Departamentos que se IGNORAN explícitamente
EXCLUDE_KEYWORDS = [
    "tecnologia", "electronica", "electrodomestico", "linea blanca", "hogar", "muebles",
    "ropa", "moda", "calzado", "jugueteria", "juguete", "deportes", "automotriz", "ferreteria",
    "videojuego", "computo", "celular", "television", "pantalla", "colchon", "jardin",
    "libreria", "papeleria", "viaje",
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


def is_super_category(name: str) -> bool:
    n = norm(name)
    if any(norm(k) in n for k in EXCLUDE_KEYWORDS):
        return False
    return any(norm(k) in n for k in INCLUDE_KEYWORDS)


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
            except (requests.RequestException, json.JSONDecodeError, Exception) as exc:  # noqa: BLE001
                log.warning("Error %s en %s (intento %d)", exc, url, attempt)
                time.sleep(SLEEP_SECONDS * attempt)
        return None

    def close(self) -> None:
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()


# --------------------------------------------------------------------------- categorías
def discover_categories(fetcher: Fetcher, tree: list | None = None) -> list[tuple[str, str]]:
    """Devuelve [(ruta_vtex, nombre)]; ruta = "idRaiz/idHija" para subcategorías (necesario en fq=C:/ruta/)."""
    if tree is None:
        tree = fetcher.get_json(CATEGORY_TREE_URL.format(depth=3)) or []
    result: list[tuple[str, str]] = []
    for root in tree:
        root_name = norm(root.get("name", ""))
        if any(root_name == r or root_name.startswith(r) for r in SUPER_ROOT_NAMES):
            children = root.get("children") or []
            if children:
                for ch in children:
                    if not any(norm(k) in norm(ch["name"]) for k in EXCLUDE_KEYWORDS):
                        result.append((f'{root["id"]}/{ch["id"]}', ch["name"]))
            else:
                result.append((root["id"], root["name"]))
        elif is_super_category(root.get("name", "")):
            result.append((str(root["id"]), root["name"]))  # raíz suelta tipo "Lácteos"
    # dedup
    seen, out = set(), []
    for cid, name in result:
        if cid not in seen:
            seen.add(cid)
            out.append((cid, name))
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
    # Ruta de categoría más específica si existe
    cats = raw.get("categories") or []
    cat_name = categoria
    if cats:
        parts = [p for p in cats[0].split("/") if p]
        if parts:
            cat_name = parts[-1] if len(parts) < 2 else f"{parts[0]} / {parts[-1]}"
    return {
        "sku": str(item.get("itemId") or raw.get("productId")),
        "nombre": (raw.get("productName") or "").strip(),
        "marca": (raw.get("brand") or "").strip() or None,
        "precio_ref": price,
        "categoria": cat_name,
        "imagen": images[0].get("imageUrl") if images else None,
        "url": raw.get("link") or None,
        "stock": stock,
        "disponible": stock > 0,
    }


def fetch_category_products(fetcher: Fetcher, cat_path: str, cat_name: str, max_pages: int | None) -> Iterable[dict]:
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
            parsed = parse_product(raw, cat_name)
            if parsed:
                yield parsed
        if len(data) < PAGE_SIZE:
            break
        start += PAGE_SIZE
        page += 1


# --------------------------------------------------------------------------- base de datos
def upsert_product(db, p: dict) -> str:
    """Upsert idempotente. Retorna 'nuevo' | 'cambio' | 'igual'."""
    new_price = round(p["precio_ref"] * (1 + MARKUP_PCT / 100), 2)
    prod = db.query(Product).filter_by(sku=p["sku"]).one_or_none()
    if prod is None:
        prod = Product(
            sku=p["sku"], nombre=p["nombre"], marca=p["marca"], precio_actual=new_price,
            precio_chedraui_referencia=p["precio_ref"], categoria_supermercado=p["categoria"],
            url_imagen=p["imagen"], url_chedraui=p["url"], stock=p["stock"], disponible=p["disponible"])
        db.add(prod)
        db.flush()
        db.add(PriceHistory(producto_id=prod.id, precio_anterior=None, precio_nuevo=new_price))
        return "nuevo"
    changed = abs(prod.precio_chedraui_referencia - p["precio_ref"]) > 0.001
    if changed:
        db.add(PriceHistory(producto_id=prod.id, precio_anterior=prod.precio_actual, precio_nuevo=new_price))
    prod.nombre, prod.marca = p["nombre"], p["marca"]
    prod.precio_actual, prod.precio_chedraui_referencia = new_price, p["precio_ref"]
    prod.categoria_supermercado, prod.url_imagen = p["categoria"], p["imagen"]
    prod.url_chedraui, prod.stock, prod.disponible = p["url"], p["stock"], p["disponible"]
    return "cambio" if changed else "igual"


def export_json(db) -> str:
    path = os.path.join(BASE_DIR, "data", "products.json")
    rows = [p.to_dict() for p in db.query(Product).order_by(Product.categoria_supermercado, Product.nombre)]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    return path


# --------------------------------------------------------------------------- orquestación
def run_sync(use_browser: bool = False, max_pages: int | None = None,
             category_ids: list[str] | None = None) -> dict:
    init_db()
    fetcher = Fetcher(use_browser=use_browser)
    stats = {"nuevo": 0, "cambio": 0, "igual": 0, "categorias": 0, "errores": 0}
    db = SessionLocal()
    try:
        cats = ([(c, f"cat-{c}") for c in category_ids] if category_ids
                else discover_categories(fetcher))
        if not cats:
            raise RuntimeError("No se encontraron categorías de supermercado. "
                               "Usa --categories o ajusta SUPER_ROOT_NAMES.")
        log.info("Categorías de supermercado: %s", [n for _, n in cats])
        stats["categorias"] = len(cats)
        for cat_path, cat_name in cats:
            try:
                n = 0
                for p in fetch_category_products(fetcher, cat_path, cat_name, max_pages):
                    stats[upsert_product(db, p)] += 1
                    n += 1
                if n == 0:
                    log.warning("Categoría SIN productos: %s (ruta %s)", cat_name, cat_path)
                db.commit()
                log.info("OK %s -> %s", cat_name, stats)
            except Exception:  # noqa: BLE001
                db.rollback()
                stats["errores"] += 1
                log.exception("Fallo en categoría %s", cat_name)
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
    ap.add_argument("--categories", type=str, default=None, help="rutas separadas por coma, p. ej. 12/45,12/46 (o IDs simples)")
    args = ap.parse_args()
    ids = [x.strip() for x in args.categories.split(",") if x.strip()] if args.categories else None
    print(json.dumps(run_sync(args.browser, args.max_pages, ids), indent=2))
