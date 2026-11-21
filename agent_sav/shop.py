import hmac
from secrets import token_hex
from typing import Literal, NotRequired, TypedDict
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
    customer_id: NotRequired[str]
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
                ALTER TABLE shop.sav_returns ADD COLUMN IF NOT EXISTS color text;
                CREATE TABLE IF NOT EXISTS shop.sav_color_stock (
                    product_id text NOT NULL REFERENCES shop.sav_products,
                    size text NOT NULL,
                    color text NOT NULL,
                    quantity integer NOT NULL CHECK (quantity >= 0),
                    PRIMARY KEY (product_id, size, color)
                );
                INSERT INTO shop.sav_color_stock (product_id, size, color, quantity)
                SELECT p.id, sizes.size, CASE WHEN p.color = 'Noir' THEN 'Blanc' ELSE 'Noir' END, 10
                FROM shop.sav_products p CROSS JOIN (VALUES (''), ('S'), ('M'), ('L'), ('XL')) sizes(size)
                WHERE p.sized = (sizes.size <> '') ON CONFLICT DO NOTHING;
                CREATE TABLE IF NOT EXISTS shop.sav_requests (
                    case_id uuid PRIMARY KEY,
                    thread_id text NOT NULL UNIQUE,
                    customer_id text NOT NULL REFERENCES shop.sav_customers,
                    order_id text NOT NULL,
                    line_number integer NOT NULL,
                    action text NOT NULL,
                    size text,
                    color text,
                    status text NOT NULL DEFAULT 'attente_boutique',
                    secret text NOT NULL,
                    FOREIGN KEY (order_id, line_number) REFERENCES shop.sav_order_items (order_id, line_number)
                );
                CREATE TABLE IF NOT EXISTS shop.sav_messages (
                    case_id uuid NOT NULL REFERENCES shop.sav_requests,
                    kind text NOT NULL,
                    subject text NOT NULL,
                    body text NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (case_id, kind)
                );
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
            "customer_id": customer_id,
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
                "SELECT o.status, i.size, p.sized, p.color, p.id AS product_id " + ORDER_ITEM,
                (customer_id, order_id, line_number),
            ).fetchone()

    def variants(self, product_id: str) -> list[str]:
        with psycopg.connect(self.dsn) as conn:
            return [row[0] for row in conn.execute(
                "SELECT DISTINCT color FROM shop.sav_color_stock WHERE product_id = %s ORDER BY color", (product_id,),
            )]

    @staticmethod
    def stock_location(product_id, size, color, original_color):
        if color == original_color:
            return "shop.sav_stock", "product_id = %s AND size = %s", (product_id, size)
        return "shop.sav_color_stock", "product_id = %s AND size = %s AND color = %s", (product_id, size or "", color)

    @staticmethod
    def insert_request(conn, state: dict, thread_id: str, status: str) -> bool:
        return conn.execute("""
            INSERT INTO shop.sav_requests (case_id, thread_id, customer_id, order_id, line_number, action, size, color, secret, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (case_id) DO NOTHING
        """, (state["case_id"], thread_id, state["customer_id"], state["order_id"], state["line_number"],
              state["action"], state["size"], state["color"], token_hex(32), status)).rowcount == 1

    @staticmethod
    def delivered_item(conn, state: dict) -> dict:
        item = conn.execute(
            "SELECT o.status, p.id AS product_id, p.color " + ORDER_ITEM,
            (state["customer_id"], state["order_id"], state["line_number"]),
        ).fetchone()
        if item is None or item["status"] != "livree":
            raise ValueError("Article livré introuvable.")
        return item

    def reserve_exchange(self, state: dict, thread_id: str) -> None:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            item = self.delivered_item(conn, state)
            if not self.insert_request(conn, state, thread_id, "stock_reserve"):
                return
            table, where, params = self.stock_location(item["product_id"], state["size"], state["color"], item["color"])
            reserved = conn.execute(f"UPDATE {table} SET quantity = quantity - 1 WHERE {where} AND quantity > 0", params)
            if not reserved.rowcount:
                raise OutOfStock(state["size"])

    def register_request(self, state: dict, thread_id: str) -> None:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            if state["action"] == "retour":
                self.delivered_item(conn, state)
                self.insert_request(conn, state, thread_id, "attente_boutique")
                return
            request = conn.execute("""
                UPDATE shop.sav_requests
                SET status = CASE WHEN status = 'stock_reserve' THEN 'attente_boutique' ELSE status END
                WHERE case_id = %s AND thread_id = %s AND action = 'echange'
            """, (state["case_id"], thread_id))
            if not request.rowcount:
                raise ValueError("Réservation de stock introuvable.")

    @staticmethod
    def record_message(conn, case_id: str, kind: str, subject: str, body: str) -> None:
        conn.execute("""
            INSERT INTO shop.sav_messages (case_id, kind, subject, body) VALUES (%s, %s, %s, %s)
            ON CONFLICT (case_id, kind) DO NOTHING
        """, (case_id, kind, subject, body))

    def notify_shop(self, case_id: str) -> None:
        with psycopg.connect(self.dsn) as conn:
            self.record_message(conn, case_id, "boutique", "Demande SAV à traiter", "Une demande attend votre décision.")

    def finish_step(self, case_id: str, status: str, reason: str = "") -> tuple[str | None, bool]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            request = conn.execute("""
                SELECT r.*, i.product_id, p.color AS original_color, p.name AS item_name
                FROM shop.sav_requests r JOIN shop.sav_order_items i USING (order_id, line_number)
                JOIN shop.sav_products p ON p.id = i.product_id WHERE r.case_id = %s FOR UPDATE OF r
            """, (case_id,)).fetchone()
            if request is None:
                raise ValueError("Demande SAV introuvable.")
            if request["status"] == status:
                if status == "termine":
                    row = conn.execute("SELECT id FROM shop.sav_returns WHERE case_id = %s", (case_id,)).fetchone()
                    return row["id"], False
                return None, False

            expected = "attente_boutique" if status in {"refuse_boutique", "attente_client"} else "attente_client"
            if request["status"] != expected:
                raise ValueError("Cette étape du dossier est déjà close.")
            conn.execute("UPDATE shop.sav_requests SET status = %s WHERE case_id = %s", (status, case_id))
            if status in {"refuse_boutique", "annule"} and request["action"] == "echange":
                table, where, params = self.stock_location(
                    request["product_id"], request["size"], request["color"], request["original_color"],
                )
                conn.execute(f"UPDATE {table} SET quantity = quantity + 1 WHERE {where}", params)

            detail = f"Commande {request['order_id']} · {request['action']} · {request['item_name']}"
            if request["color"]:
                detail += f" · {request['color']}"
            if request["size"]:
                detail += f" · taille {request['size']}"
            if status == "refuse_boutique":
                self.record_message(conn, case_id, "refus", "Demande SAV refusée", f"{detail}\nMotif : {reason}")
            if status == "attente_client":
                self.record_message(conn, case_id, "confirmation", "Confirmez votre demande SAV", detail)
            if status == "termine":
                return_id = f"RET-{uuid4().hex[:12].upper()}"
                conn.execute("""
                    INSERT INTO shop.sav_returns (case_id, id, order_id, line_number, action, size, color)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (case_id, return_id, request["order_id"], request["line_number"], request["action"],
                      request["size"], request["color"]))
                self.record_message(conn, case_id, "recapitulatif", "Récapitulatif de votre demande SAV", f"{detail}\nRéférence : {return_id}")
                return return_id, True
        return None, False

    def requests_for_shop(self) -> list[dict]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            return conn.execute("""
                SELECT r.case_id::text, r.customer_id, r.order_id, r.action, r.size, r.color, p.name
                FROM shop.sav_requests r JOIN shop.sav_messages m ON m.case_id = r.case_id AND m.kind = 'boutique'
                JOIN shop.sav_order_items i USING (order_id, line_number)
                JOIN shop.sav_products p ON p.id = i.product_id
                WHERE r.status = 'attente_boutique' AND strpos(r.thread_id, ':') > 0 ORDER BY m.created_at
            """).fetchall()

    def request_thread(self, case_id: str, decision: str | None = None, signature: str = "") -> str | None:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            request = conn.execute(
                "SELECT thread_id, secret FROM shop.sav_requests WHERE case_id = %s AND status <> 'stock_reserve'",
                (case_id,),
            ).fetchone()
        if request and (decision is None or hmac.compare_digest(
            signature, self.sign_decision(request["secret"], case_id, decision),
        )):
            return request["thread_id"]
        return None

    @staticmethod
    def sign_decision(secret: str, case_id: str, decision: str) -> str:
        return hmac.digest(bytes.fromhex(secret), f"{case_id}:{decision}".encode(), "sha256").hex()

    def mailbox(self, customer_id: str) -> list[dict]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            messages = conn.execute("""
                SELECT r.case_id::text, r.secret, r.status, m.kind, m.subject, m.body
                FROM shop.sav_messages m JOIN shop.sav_requests r USING (case_id)
                WHERE r.customer_id = %s AND m.kind <> 'boutique'
                    AND strpos(r.thread_id, ':') > 0
                ORDER BY m.created_at DESC
            """, (customer_id,)).fetchall()
        for message in messages:
            secret = message.pop("secret")
            case_id = message.pop("case_id")
            if message.pop("status") == "attente_client" and message["kind"] == "confirmation":
                message["links"] = {
                    decision: f"/sav/reponse/{case_id}/{decision}/{self.sign_decision(secret, case_id, decision)}"
                    for decision in ("confirmer", "refuser")
                }
        return messages
