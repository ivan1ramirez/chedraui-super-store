"""Servidor Flask: catálogo, carrito, pago (Stripe test o simulado) y sync admin."""
import hmac
import json
import os
import re
import threading

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

load_dotenv()

from models import Order, PriceHistory, Product, SessionLocal, init_db  # noqa: E402

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

IVA_RATE = float(os.getenv("IVA_RATE", "0.16"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:5000")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

stripe = None
if STRIPE_SECRET_KEY:
    import stripe as _stripe
    _stripe.api_key = STRIPE_SECRET_KEY
    stripe = _stripe

init_db()
from datastore import seed_if_empty  # noqa: E402
seed_if_empty()  # si la BD está vacía, se carga desde data/products.json
_sync_lock = threading.Lock()


@app.teardown_appcontext
def shutdown_session(_exc=None):
    SessionLocal.remove()


# ----------------------------------------------------------------- páginas
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/checkout")
def checkout():
    return render_template("checkout.html", stripe_enabled=bool(stripe), iva_rate=IVA_RATE)


# ----------------------------------------------------------------- API catálogo
@app.route("/api/categories")
def api_categories():
    rows = (SessionLocal.query(Product.categoria_supermercado)
            .distinct().order_by(Product.categoria_supermercado).all())
    return jsonify([r[0] for r in rows if r[0]])


@app.route("/api/products")
def api_products():
    q = request.args.get("q", "").strip()
    cat = request.args.get("categoria", "").strip()
    page = max(int(request.args.get("page", 1) or 1), 1)
    per_page = min(int(request.args.get("per_page", 24) or 24), 100)
    query = SessionLocal.query(Product)
    if q:
        like = f"%{q}%"
        query = query.filter(Product.nombre.ilike(like) | Product.marca.ilike(like))
    if cat:
        query = query.filter(Product.categoria_supermercado == cat)
    total = query.count()
    items = (query.order_by(Product.disponible.desc(), Product.nombre)
             .offset((page - 1) * per_page).limit(per_page).all())
    return jsonify({"total": total, "page": page, "per_page": per_page,
                    "items": [p.to_dict() for p in items]})


@app.route("/api/products/<int:pid>/history")
def api_history(pid):
    rows = (SessionLocal.query(PriceHistory).filter_by(producto_id=pid)
            .order_by(PriceHistory.fecha).all())
    return jsonify([{"fecha": r.fecha.isoformat(), "anterior": r.precio_anterior,
                     "nuevo": r.precio_nuevo} for r in rows])


# ----------------------------------------------------------------- carrito / totales
def build_quote(cart_items):
    """Recalcula SIEMPRE con precios de la BD (nunca confiar en el cliente)."""
    lines, subtotal = [], 0.0
    for it in cart_items or []:
        try:
            pid, qty = int(it["id"]), int(it["qty"])
        except (KeyError, ValueError, TypeError):
            raise ValueError("Carrito inválido")
        if qty < 1 or qty > 50:
            raise ValueError("Cantidad inválida")
        p = SessionLocal.get(Product, pid)
        if not p or not p.disponible:
            raise ValueError(f"Producto no disponible (id {pid})")
        line_total = round(p.precio_actual * qty, 2)
        subtotal += line_total
        lines.append({"id": p.id, "sku": p.sku, "nombre": p.nombre, "precio": p.precio_actual,
                      "qty": qty, "total": line_total})
    if not lines:
        raise ValueError("El carrito está vacío")
    subtotal = round(subtotal, 2)
    iva = round(subtotal * IVA_RATE, 2)
    return {"lines": lines, "subtotal": subtotal, "iva": iva, "total": round(subtotal + iva, 2)}


@app.route("/api/cart/quote", methods=["POST"])
def api_quote():
    try:
        return jsonify(build_quote((request.get_json(silent=True) or {}).get("items")))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


# ----------------------------------------------------------------- pago
def validate_customer(c):
    errors = {}
    if len((c.get("nombre") or "").strip()) < 3:
        errors["nombre"] = "Ingresa tu nombre completo"
    if not EMAIL_RE.match((c.get("email") or "").strip()):
        errors["email"] = "Correo inválido"
    if len((c.get("direccion") or "").strip()) < 10:
        errors["direccion"] = "Ingresa una dirección de entrega completa"
    return errors


@app.route("/api/pay", methods=["POST"])
@app.route("/create-checkout-session", methods=["POST"])
def api_pay():
    data = request.get_json(silent=True) or {}
    customer = data.get("cliente") or {}
    errors = validate_customer(customer)
    if errors:
        return jsonify({"error": "Datos inválidos", "campos": errors}), 400
    try:
        quote = build_quote(data.get("items"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not data.get("confirmo"):
        return jsonify({"error": "Debes confirmar la compra"}), 400

    order = Order(nombre_cliente=customer["nombre"].strip(), email=customer["email"].strip(),
                  direccion=customer["direccion"].strip(), items_json=json.dumps(quote["lines"], ensure_ascii=False),
                  subtotal=quote["subtotal"], impuestos=quote["iva"], total=quote["total"],
                  estado="pendiente", metodo_pago="stripe" if stripe else "simulado")
    SessionLocal.add(order)
    SessionLocal.commit()

    if not stripe:  # --- pasarela simulada ---
        order.estado = "pagado"
        SessionLocal.commit()
        return jsonify({"url": url_for("receipt", order_id=order.id), "simulado": True})

    try:
        line_items = [{
            "price_data": {"currency": "mxn", "product_data": {"name": l["nombre"][:120]},
                           "unit_amount": int(round(l["precio"] * (1 + IVA_RATE) * 100))},
            "quantity": l["qty"]} for l in quote["lines"]]
        session = stripe.checkout.Session.create(
            mode="payment", line_items=line_items, customer_email=order.email,
            success_url=f"{PUBLIC_URL}/payment/success?order_id={order.id}&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{PUBLIC_URL}/checkout", metadata={"order_id": str(order.id)})
    except Exception as e:  # noqa: BLE001
        order.estado = "cancelado"
        SessionLocal.commit()
        return jsonify({"error": f"Error con Stripe: {e}"}), 502
    order.stripe_session_id = session.id
    SessionLocal.commit()
    return jsonify({"url": session.url})


@app.route("/payment/success")
def payment_success():
    order = SessionLocal.get(Order, int(request.args.get("order_id", 0)))
    sid = request.args.get("session_id", "")
    if not order or not stripe or order.stripe_session_id != sid:
        abort(404)
    session = stripe.checkout.Session.retrieve(sid)  # verifica con Stripe, no con la URL
    if session.payment_status == "paid":
        order.estado = "pagado"
        SessionLocal.commit()
    return redirect(url_for("receipt", order_id=order.id))


@app.route("/receipt/<int:order_id>")
def receipt(order_id):
    order = SessionLocal.get(Order, order_id)
    if not order:
        abort(404)
    return render_template("receipt.html", o=order, lines=json.loads(order.items_json))


# ----------------------------------------------------------------- admin: sync
@app.route("/api/sync-chedraui", methods=["POST"])
def api_sync():
    token = request.headers.get("X-Admin-Token", "")
    if not ADMIN_TOKEN or not hmac.compare_digest(token, ADMIN_TOKEN):
        return jsonify({"error": "No autorizado"}), 401
    if not _sync_lock.acquire(blocking=False):
        return jsonify({"error": "Ya hay una sincronización en curso"}), 409

    body = request.get_json(silent=True) or {}

    def job():
        try:
            from scraper import run_sync
            app.logger.info("Sync terminado: %s", run_sync(
                use_browser=bool(body.get("browser")), max_pages=body.get("max_pages")))
        except Exception:  # noqa: BLE001
            app.logger.exception("Sync falló")
        finally:
            _sync_lock.release()

    threading.Thread(target=job, daemon=True).start()
    return jsonify({"status": "iniciado"}), 202


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG") == "1", port=int(os.getenv("PORT", "5000")))
