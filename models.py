"""Modelos SQLAlchemy 2.0 para la tienda de supermercado."""
import os
from datetime import datetime, timezone

from sqlalchemy import (Boolean, DateTime, Float, ForeignKey, Integer, String,
                        Text, create_engine)
from sqlalchemy.orm import (DeclarativeBase, Mapped, mapped_column,
                            relationship, scoped_session, sessionmaker)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'data', 'store.db')}")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # itemId VTEX
    nombre: Mapped[str] = mapped_column(String(300), index=True)
    marca: Mapped[str | None] = mapped_column(String(120), nullable=True)
    precio_actual: Mapped[float] = mapped_column(Float)               # precio de venta en tu tienda
    precio_chedraui_referencia: Mapped[float] = mapped_column(Float)  # precio leído de Chedraui
    categoria_supermercado: Mapped[str | None] = mapped_column(String(200), index=True, nullable=True)
    url_imagen: Mapped[str | None] = mapped_column(Text, nullable=True)
    url_chedraui: Mapped[str | None] = mapped_column(Text, nullable=True)
    stock: Mapped[int] = mapped_column(Integer, default=0)
    disponible: Mapped[bool] = mapped_column(Boolean, default=True)
    actualizado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    historial: Mapped[list["PriceHistory"]] = relationship(
        back_populates="producto", cascade="all, delete-orphan", order_by="PriceHistory.fecha")

    def to_dict(self) -> dict:
        return {
            "id": self.id, "sku": self.sku, "nombre": self.nombre, "marca": self.marca,
            "precio": round(self.precio_actual, 2),
            "precio_referencia": round(self.precio_chedraui_referencia, 2),
            "categoria": self.categoria_supermercado, "imagen": self.url_imagen,
            "stock": self.stock, "disponible": self.disponible,
        }


class PriceHistory(Base):
    __tablename__ = "price_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    producto_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    precio_anterior: Mapped[float | None] = mapped_column(Float, nullable=True)
    precio_nuevo: Mapped[float] = mapped_column(Float)
    fecha: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    producto: Mapped[Product] = relationship(back_populates="historial")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre_cliente: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(200))
    direccion: Mapped[str] = mapped_column(Text)
    items_json: Mapped[str] = mapped_column(Text)  # snapshot de líneas con precio al momento de compra
    subtotal: Mapped[float] = mapped_column(Float)
    impuestos: Mapped[float] = mapped_column(Float)
    total: Mapped[float] = mapped_column(Float)
    estado: Mapped[str] = mapped_column(String(30), default="pendiente")  # pendiente|pagado|cancelado
    metodo_pago: Mapped[str] = mapped_column(String(30), default="simulado")  # stripe|simulado
    stripe_session_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)
engine = create_engine(DATABASE_URL, future=True)
SessionLocal = scoped_session(sessionmaker(bind=engine, expire_on_commit=False))


def init_db() -> None:
    Base.metadata.create_all(engine)
