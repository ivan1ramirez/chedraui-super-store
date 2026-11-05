# 🛒 Súper en Línea — catálogo con precios sincronizados de Chedraui (Supermercado)

E-commerce en Flask + SQLite (SQLAlchemy) cuyo catálogo de **supermercado** se actualiza con un scraper de Python
sobre la tienda de [Chedraui México](https://www.chedraui.com.mx/), con carrito, checkout (Stripe en modo prueba o
pasarela simulada) y automatización diaria con GitHub Actions.

## Estructura

```
├── app.py                # Flask: catálogo, carrito, pago, recibo, /api/sync-chedraui
├── models.py             # Product, PriceHistory, Order
├── scraper.py            # Scraper VTEX (requests) + alternativa Playwright
├── datastore.py          # Export/seed de data/products.json y price_history.jsonl
├── requirements.txt
├── templates/            # index.html, checkout.html, receipt.html (Tailwind CDN + JS vanilla)
├── data/
│   ├── products.json         # catálogo (un producto por línea) — SE VERSIONA
│   ├── price_history.jsonl   # historial de precios (solo se añaden líneas) — SE VERSIONA
│   └── store.db              # BD SQLite local — NO se versiona
└── .github/workflows/chedraui_price_updater.yml
```

**Flujo de datos:** la Action siembra una BD temporal desde `products.json`, corre el scraper, y hace commit solo de
`products.json` y `price_history.jsonl` (texto: el repositorio crece poco). Al arrancar, `app.py` también crea la BD
desde esos archivos si está vacía.

## 1. Instalación local

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              # y edita los valores
python app.py                     # siembra la BD desde data/products.json
```

## 2. Variables de entorno (`.env`)

| Variable | Uso |
|---|---|
| `ADMIN_TOKEN` | Protege `POST /api/sync-chedraui` (header `X-Admin-Token`) |
| `STRIPE_SECRET_KEY` / `STRIPE_PUBLISHABLE_KEY` | Claves **de prueba** (`sk_test_…`). Vacías = pasarela simulada |
| `PUBLIC_URL` | URL pública (para las redirecciones de Stripe) |
| `IVA_RATE` | Tasa de IVA del carrito (0.16 por defecto). Muchos alimentos básicos van a tasa 0%; ajusta según tu caso fiscal |
| `MARKUP_PCT` | Margen (%) sobre el precio de Chedraui → `precio_actual` |
| `SCRAPER_SLEEP` / `SCRAPER_TIMEOUT` | Pausa entre peticiones y timeout (segundos) |

## 3. Scraper

```bash
python scraper.py --max-pages 1       # prueba corta (no retira productos)
python scraper.py                     # sincronización completa
python scraper.py --browser           # con Playwright (pip install playwright && playwright install chromium)
python scraper.py --categories 2/21   # rutas de categoría manuales (idRaiz/idHija)
```

- Solo recorre las categorías de supermercado definidas por `INCLUDE_KEYWORDS` / `EXCLUDE_KEYWORDS` / `SUPER_ROOT_NAMES`.
  Excluidas actualmente: Mascotas, Vinos y licores y Bebes (más tecnología, hogar, etc.).
- VTEX limita cada consulta a 2,500 resultados: si una categoría llega al tope, el scraper la divide en subcategorías.
- Cada categoría se consulta por su **ruta completa** (`idRaiz/idHija`), requisito de VTEX para subcategorías.
- En una sincronización **completa y sin errores**, los productos que ya no aparecen se marcan como no disponibles.
- Los cambios de precio se añaden a `data/price_history.jsonl`.

> ⚠️ Los endpoints son los estándar de VTEX y pueden cambiar o bloquear bots. Revisa los términos de uso de Chedraui
> y mantén un ritmo bajo de peticiones.

## 4. Ejecutar la web

```bash
python app.py                       # http://localhost:5000
gunicorn app:app -b 0.0.0.0:8000 --workers 2   # producción
```

Sync manual:

```bash
curl -X POST http://localhost:5000/api/sync-chedraui \
  -H "X-Admin-Token: TU_TOKEN" -H "Content-Type: application/json" -d '{"max_pages": 1}'
```

## 5. Pagos

- **Sin claves de Stripe**: pasarela simulada; el pedido se marca como pagado y se genera el recibo.
- **Con claves de prueba**: *Checkout Session* de Stripe; tarjeta `4242 4242 4242 4242`. El servidor verifica el pago con
  Stripe en `/payment/success` antes de marcar el pedido como pagado.
- Los totales se recalculan **siempre en el servidor** con los precios de la BD.
- Para producción real añade un webhook de Stripe (`checkout.session.completed`) como respaldo.

## 6. GitHub Actions

1. *Settings → Actions → General → Workflow permissions* → **Read and write permissions**.
2. (Opcional) Secret `CHEDRAUI_CATEGORY_IDS`; variables `MARKUP_PCT`, `SCRAPER_SLEEP`.
3. Corre a diario (11:00 UTC) o manualmente (*Actions → Run workflow*; deja `max_pages` vacío para la sincronización completa).
4. Hace commit de `data/products.json` y `data/price_history.jsonl` solo si hubo cambios.

## 7. Despliegue

Render, Railway o Fly.io con `gunicorn app:app`. La BD se siembra sola desde `data/products.json`, pero los **pedidos**
viven en SQLite: usa disco persistente o, para tráfico real, cambia `DATABASE_URL` a PostgreSQL. Define `ADMIN_TOKEN`,
claves de Stripe y `PUBLIC_URL` como variables de entorno del servicio.
