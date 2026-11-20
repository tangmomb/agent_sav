from typing import Literal, TypedDict
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from .demo_data import DEMO_CUSTOMER, DEMO_ORDER as DEMO_ORDER, seed_demo
DEFAULT_DSN = "postgresql://sav:sav@127.0.0.1:55432/sav"


class OutOfStock(Exception):
    pass


OrderStatus = Literal["validee", "en_livraison", "livree"]


class OrderSummary(TypedDict):
    id: str
    status: OrderStatus


class OrderArticle(TypedDict):
    line_number: int
    name: str
    color: str
    size: str | None


class OrderContext(TypedDict):
    first_name: str
    order: OrderSummary
    items: list[OrderArticle]


ORDER_ITEM = """
    FROM shop.sav_orders o JOIN shop.sav_order_items i ON i.order_id = o.id
    JOIN shop.sav_products p ON p.id = i.product_id
    WHERE o.customer_id = %s AND o.id = %s AND i.line_number = %s
"""


class Shop:
    """Service marchand simulé, avec son propre commit PostgreSQL."""

    def __init__(self, dsn: str):
        self.dsn = dsn

    def setup(self) -> None:
        with psycopg.connect(self.dsn) as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('sav_shop_setup', 0))")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS shop.sav_customers (
                    id text PRIMARY KEY,
                    name text NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shop.sav_products (
                    id text PRIMARY KEY,
                    name text NOT NULL,
                    color text NOT NULL,
                    price_cents integer NOT NULL,
                    sized boolean NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shop.sav_orders (
                    id text PRIMARY KEY,
                    customer_id text NOT NULL REFERENCES shop.sav_customers,
                    ordered_on date NOT NULL,
                    status text NOT NULL CONSTRAINT sav_orders_fulfillment_status_check
                        CHECK (status IN ('validee', 'en_livraison', 'livree'))
                );
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT FROM pg_constraint
                        WHERE conrelid = 'shop.sav_orders'::regclass AND conname = 'sav_orders_status_check'
                    ) THEN
                        ALTER TABLE shop.sav_orders DROP CONSTRAINT sav_orders_status_check;
                        UPDATE shop.sav_orders SET status = 'en_livraison' WHERE status = 'expediee';
                        ALTER TABLE shop.sav_orders ADD CONSTRAINT sav_orders_fulfillment_status_check
                            CHECK (status IN ('validee', 'en_livraison', 'livree'));
                    END IF;
                END $$;
                CREATE TABLE IF NOT EXISTS shop.sav_order_items (
                    order_id text NOT NULL REFERENCES shop.sav_orders,
                    line_number integer NOT NULL CHECK (line_number > 0),
                    product_id text NOT NULL REFERENCES shop.sav_products,
                    size text,
                    price_cents integer NOT NULL CHECK (price_cents >= 0),
                    PRIMARY KEY (order_id, line_number)
                );
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT FROM pg_attribute
                        WHERE attrelid = 'shop.sav_orders'::regclass AND attname = 'product_id' AND NOT attisdropped
                    ) THEN
                        INSERT INTO shop.sav_order_items (order_id, line_number, product_id, size, price_cents)
                        SELECT o.id, 1, o.product_id, o.size, p.price_cents
                        FROM shop.sav_orders o JOIN shop.sav_products p ON p.id = o.product_id;
                        ALTER TABLE shop.sav_orders DROP COLUMN product_id, DROP COLUMN size;
                    END IF;
                END $$;
                CREATE TABLE IF NOT EXISTS shop.sav_stock (
                    product_id text NOT NULL DEFAULT 'ART-001',
                    size text NOT NULL,
                    quantity integer NOT NULL CHECK (quantity >= 0),
                    PRIMARY KEY (product_id, size)
                );
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT FROM pg_attribute
                        WHERE attrelid = 'shop.sav_stock'::regclass AND attname = 'product_id' AND NOT attisdropped
                    ) THEN
                        ALTER TABLE shop.sav_stock ADD COLUMN product_id text NOT NULL DEFAULT 'ART-001';
                        ALTER TABLE shop.sav_stock DROP CONSTRAINT sav_stock_pkey;
                        ALTER TABLE shop.sav_stock ADD PRIMARY KEY (product_id, size);
                    END IF;
                END $$;
                CREATE TABLE IF NOT EXISTS shop.sav_returns (
                    case_id uuid PRIMARY KEY,
                    id text UNIQUE NOT NULL,
                    order_id text NOT NULL,
                    action text NOT NULL,
                    size text
                );
            """)
            seed_demo(conn)
            conn.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT FROM pg_attribute WHERE attrelid = 'shop.sav_returns'::regclass
                        AND attname = 'line_number' AND NOT attisdropped
                    ) THEN
                        ALTER TABLE shop.sav_returns ADD COLUMN line_number integer NOT NULL DEFAULT 1;
                        ALTER TABLE shop.sav_returns ADD FOREIGN KEY (order_id, line_number)
                            REFERENCES shop.sav_order_items (order_id, line_number);
                    END IF;
                END $$;
            """)

    def customers(self) -> list[dict]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            return conn.execute("SELECT id, name FROM shop.sav_customers ORDER BY id").fetchall()

    def orders(self, customer_id: str) -> list[dict] | None:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            if conn.execute("SELECT 1 FROM shop.sav_customers WHERE id = %s", (customer_id,)).fetchone() is None:
                return None
            return conn.execute(
                """
                SELECT o.id, o.customer_id, o.ordered_on::text, o.status,
                       sum(i.price_cents) AS total_cents,
                       jsonb_agg(jsonb_build_object(
                           'line_number', i.line_number, 'product_id', i.product_id,
                           'size', i.size, 'price_cents', i.price_cents,
                           'name', p.name, 'color', p.color, 'sized', p.sized
                       ) ORDER BY i.line_number) AS items
                FROM shop.sav_orders o
                JOIN shop.sav_order_items i ON i.order_id = o.id
                JOIN shop.sav_products p ON p.id = i.product_id
                WHERE o.customer_id = %s
                GROUP BY o.id
                ORDER BY o.ordered_on DESC, o.id
                """,
                (customer_id,),
            ).fetchall()

    def order_context(self, customer_id: str, order_id: str) -> tuple[OrderContext, int] | None:
        orders = self.orders(customer_id) or []
        order = next((order for order in orders if order["id"] == order_id), None)
        if order is None:
            return None

        with psycopg.connect(self.dsn) as conn:
            row = conn.execute(
                "SELECT name FROM shop.sav_customers WHERE id = %s", (customer_id,),
            ).fetchone()
        if row is None:
            return None

        context: OrderContext = {
            "first_name": row[0].split()[0],
            "order": {"id": order["id"], "status": order["status"]},
            "items": [
                {"line_number": item["line_number"], "name": item["name"], "color": item["color"], "size": item["size"]}
                for item in order["items"]
            ],
        }
        active_order_count = sum(
            other["id"] != order_id and other["status"] in {"validee", "en_livraison"}
            for other in orders
        )
        return context, active_order_count

    def order_item(self, customer_id: str, order_id: str, line_number: int) -> dict | None:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            return conn.execute(
                "SELECT o.status, i.size, p.sized " + ORDER_ITEM,
                (customer_id, order_id, line_number),
            ).fetchone()

    def create_return(
        self, case_id: str, order_id: str, action: str, size: str | None, *,
        customer_id: str = DEMO_CUSTOMER, line_number: int = 1,
    ) -> tuple[str, bool]:
        """Retourne la référence et indique si cet appel a créé le retour."""
        return_id = f"RET-{uuid4().hex[:12].upper()}"
        with psycopg.connect(self.dsn) as conn:
            order = conn.execute(
                "SELECT i.product_id " + ORDER_ITEM, (customer_id, order_id, line_number),
            ).fetchone()
            if order is None:
                raise ValueError("Article introuvable dans cette commande.")
            inserted = conn.execute(
                """
                INSERT INTO shop.sav_returns (case_id, id, order_id, line_number, action, size)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (case_id) DO NOTHING
                RETURNING id
                """,
                (case_id, return_id, order_id, line_number, action, size),
            ).fetchone()

            if inserted is None:
                existing = conn.execute(
                    "SELECT id, order_id, line_number, action, size FROM shop.sav_returns WHERE case_id = %s",
                    (case_id,),
                ).fetchone()
                if existing is None or existing[1:] != (order_id, line_number, action, size):
                    raise ValueError("La clé d'idempotence correspond à une autre opération.")
                return existing[0], False

            if action == "echange":
                reserved = conn.execute(
                    """
                    UPDATE shop.sav_stock SET quantity = quantity - 1
                    WHERE product_id = %s AND size = %s AND quantity > 0
                    RETURNING size
                    """,
                    (order[0], size),
                ).fetchone()
                if reserved is None:
                    raise OutOfStock(size)

        return return_id, True
