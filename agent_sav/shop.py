import psycopg
from psycopg.rows import dict_row

from .demo_data import seed_demo

DEFAULT_DSN = "postgresql://sav:sav@127.0.0.1:55432/sav"


class Shop:
    """Service marchand simulé, avec son propre commit PostgreSQL."""

    def __init__(self, dsn: str):
        self.dsn = dsn

    def setup(self) -> None:
        with psycopg.connect(self.dsn) as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('sav_shop_setup', 0))")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS shop.customers (
                    id text PRIMARY KEY,
                    first_name text NOT NULL,
                    name text NOT NULL
                );
                -- One row per product color; rows sharing a name are colors of the same product.
                CREATE TABLE IF NOT EXISTS shop.products (
                    id text PRIMARY KEY,
                    name text NOT NULL,
                    color text NOT NULL,
                    price_cents integer NOT NULL CHECK (price_cents >= 0),
                    UNIQUE (name, color)
                );
                CREATE TABLE IF NOT EXISTS shop.orders (
                    id text PRIMARY KEY,
                    customer_id text NOT NULL REFERENCES shop.customers,
                    ordered_on date NOT NULL,
                    status text NOT NULL CHECK (status IN ('confirmed', 'in_transit', 'delivered'))
                );
                -- One row per purchased article. Unsized products (scarf, bag...) have no size.
                CREATE TABLE IF NOT EXISTS shop.order_items (
                    item_id text PRIMARY KEY,
                    order_id text NOT NULL REFERENCES shop.orders,
                    product_id text NOT NULL REFERENCES shop.products,
                    size text
                );
                -- Unsized products use an empty size.
                CREATE TABLE IF NOT EXISTS shop.stock (
                    product_id text NOT NULL REFERENCES shop.products,
                    size text NOT NULL,
                    quantity integer NOT NULL CHECK (quantity >= 0),
                    PRIMARY KEY (product_id, size)
                );
                ALTER TABLE shop.orders ADD COLUMN IF NOT EXISTS delivered_on date;
            """)
            seed_demo(conn)

    def customers(self) -> list[dict]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            return conn.execute("SELECT id, first_name, name FROM shop.customers ORDER BY id").fetchall()

    def products(self) -> list[dict]:
        """The catalog: one row per product color, with its stock by size ("" for unsized products)."""
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            return conn.execute("""
                SELECT p.id, p.name, p.color, p.price_cents,
                       coalesce(jsonb_object_agg(s.size, s.quantity) FILTER (WHERE s.size IS NOT NULL), '{}') AS stock
                FROM shop.products p LEFT JOIN shop.stock s ON s.product_id = p.id
                GROUP BY p.id ORDER BY p.id
            """).fetchall()

    def orders(self, customer_id: str) -> list[dict] | None:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            if conn.execute("SELECT 1 FROM shop.customers WHERE id = %s", (customer_id,)).fetchone() is None:
                return None
            return conn.execute(
                """
                SELECT o.id, o.customer_id, o.ordered_on::text, o.delivered_on::text, o.status,
                       sum(p.price_cents) AS total_cents,
                       jsonb_agg(jsonb_build_object(
                           'item_id', i.item_id, 'product_id', i.product_id,
                           'size', i.size, 'price_cents', p.price_cents,
                           'name', p.name, 'color', p.color, 'sized', i.size IS NOT NULL
                       ) ORDER BY i.item_id) AS items
                FROM shop.orders o
                JOIN shop.order_items i ON i.order_id = o.id
                JOIN shop.products p ON p.id = i.product_id
                WHERE o.customer_id = %s
                GROUP BY o.id
                ORDER BY o.ordered_on DESC, o.id
                """,
                (customer_id,),
            ).fetchall()

    @staticmethod
    def move_stock(conn, product_id: str, size: str | None, color: str, change: int) -> bool:
        """Changes the stock of product_id's product in the given color and size."""
        return bool(conn.execute("""
            UPDATE shop.stock s SET quantity = quantity + %s
            FROM shop.products bought JOIN shop.products wanted ON wanted.name = bought.name
            WHERE bought.id = %s AND wanted.color = %s AND s.product_id = wanted.id
                AND s.size = %s AND s.quantity + %s >= 0
        """, (change, product_id, color, size or "", change)).rowcount)
