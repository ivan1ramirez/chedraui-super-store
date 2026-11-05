# 🛒 Súper en Línea — catálogo con precios sincronizados de Chedraui (Supermercado)

E-commerce en Flask + SQLite (SQLAlchemy) cuyo catálogo de **supermercado** se actualiza con un scraper de Python
sobre la tienda de [Chedraui México](https://www.chedraui.com.mx/), con carrito, checkout (Stripe en modo prueba o
pasarela simulada) y automatización diaria con GitHub Actions.

## Estructura

```
├── app.py                # Flask: catálogo, carrito, pago, recibo, /api/sync-chedraui
├── models.py             # Product, PriceHistory, Order
├── scraper.py            # Scraper VTEX (requests) + alternativa Playwright
├── requirements.txt
├── templates/            # index.html, checkout.html, receipt.html (Tailwind CDN + JS vanilla)
├── data/                 # store.db y products.json (los actualiza la Action)
└── .github/workflows/chedraui_price_updater.yml
```

## 1. Instalación local

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              # y edita los valores
```

## 2. Variables de entorno (`.env`)

| Variable | Uso |
|---|---|
| `ADMIN_TOKEN` | Protege `POST /api/sync-chedraui` (header `X-Admin-Token`) |
| `STRIPE_SECRET_KEY` / `STRIPE_PUBLISHABLE_KEY` | Claves **de prueba** (`sk_test_…`). Vacías = pasarela simulada |
| `PUBLIC_URL` | URL pública (para las redirecciones de Stripe) |
| `IVA_RATE` | Tasa de IVA del carrito (0.16 por defecto). Ojo: muchos alimentos básicos están a tasa 0%; ajusta según tu caso fiscal |
| `MARKUP_PCT` | Margen (%) sobre el precio de Chedraui → `precio_actual` |
| `SCRAPER_SLEEP` / `SCRAPER_TIMEOUT` | Pausa entre peticiones y timeout (segundos) |

## 3. Poblar el catálogo

```bash
python scraper.py --max-pages 1     # prueba corta
python scraper.py                   # sincronización completa
python scraper.py --browser         # con Playwright (pip install playwright && playwright install chromium)
python scraper.py --categories 12,45  # IDs de categoría manuales
```

El scraper consulta el árbol de categorías de VTEX, conserva **solo** las ramas de supermercado
(listas `INCLUDE_KEYWORDS` / `EXCLUDE_KEYWORDS` / `SUPER_ROOT_NAMES` al inicio de `scraper.py`),
pagina `/api/catalog_system/pub/products/search`, normaliza precios y hace *upsert* por SKU
(guardando en `PriceHistory` solo cuando el precio cambia).

> ⚠️ **Verifícalo contra el sitio real.** Los endpoints son los estándar de VTEX y no pude probarlos en vivo desde
> el entorno donde se generó el código. Si Chedraui cambia la estructura o bloquea la petición, usa `--browser`
> o define los IDs con `--categories` (los verás en `GET /api/catalog_system/pub/category/tree/3`).
> Revisa también los términos de uso del sitio y respeta un ritmo bajo de peticiones.

## 4. Ejecutar la web

```bash
python app.py                       # http://localhost:5000
# producción:
gunicorn app:app -b 0.0.0.0:8000 --workers 2
```

Sync manual:

```bash
curl -X POST http://localhost:5000/api/sync-chedraui \
  -H "X-Admin-Token: TU_TOKEN" -H "Content-Type: application/json" -d '{"max_pages": 1}'
```

## 5. Pagos

- **Sin claves de Stripe**: pasarela simulada; el pedido se marca como pagado y se genera el recibo.
- **Con claves de prueba**: se crea una *Checkout Session*; usa la tarjeta `4242 4242 4242 4242`.
  El servidor verifica el pago con Stripe en `/payment/success` antes de marcar el pedido como pagado.
- Los totales se recalculan **siempre en el servidor** con los precios de la BD.
- Para producción real añade un webhook de Stripe (`checkout.session.completed`) como respaldo.

## 6. GitHub Actions

1. Sube el proyecto a un repositorio (incluye `data/store.db` si quieres versionar la base).
2. *Settings → Actions → General → Workflow permissions* → **Read and write permissions**.
3. (Opcional) En *Settings → Secrets and variables*: secret `CHEDRAUI_CATEGORY_IDS`; variables `MARKUP_PCT`, `SCRAPER_SLEEP`.
4. El workflow corre a diario (11:00 UTC) o manualmente (*Actions → Run workflow*), y hace commit de `data/` solo si hubo cambios.

## 7. Despliegue

Render, Railway o Fly.io con `gunicorn app:app`. SQLite requiere disco persistente; para tráfico real, cambia
`DATABASE_URL` a PostgreSQL. Define `ADMIN_TOKEN`, claves de Stripe y `PUBLIC_URL` como variables de entorno del servicio.
