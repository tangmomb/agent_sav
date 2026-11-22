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


OrderStatus = Literal["confirmed", "in_transit", "delivered"]


class OrderSummary(TypedDict):
    id: str
    status: OrderStatus


class OrderArticle(TypedDict):
    item_id: str
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
    WHERE o.customer_id = %s AND o.id = %s AND i.item_id = %s
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
                        CHECK (status IN ('confirmed', 'in_transit', 'delivered'))
                );
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT FROM pg_constraint
                        WHERE conrelid = 'shop.sav_orders'::regclass
                            AND conname IN ('sav_orders_status_check', 'sav_orders_fulfillment_status_check')
                            AND pg_get_constraintdef(oid) ~ 'validee|en_livraison|livree|expediee|shipped'
                    ) THEN
                        ALTER TABLE shop.sav_orders DROP CONSTRAINT IF EXISTS sav_orders_status_check;
                        ALTER TABLE shop.sav_orders DROP CONSTRAINT IF EXISTS sav_orders_fulfillment_status_check;
                        UPDATE shop.sav_orders SET status = CASE status
                            WHEN 'validee' THEN 'confirmed'
                            WHEN 'en_livraison' THEN 'in_transit'
                            WHEN 'expediee' THEN 'in_transit'
                            WHEN 'shipped' THEN 'in_transit'
                            WHEN 'livree' THEN 'delivered'
                            ELSE status END;
                        ALTER TABLE shop.sav_orders ADD CONSTRAINT sav_orders_fulfillment_status_check
                            CHECK (status IN ('confirmed', 'in_transit', 'delivered'));
                    END IF;
                END $$;
                CREATE TABLE IF NOT EXISTS shop.sav_order_items (
                    order_id text NOT NULL REFERENCES shop.sav_orders,
                    item_id text PRIMARY KEY,
                    product_id text NOT NULL REFERENCES shop.sav_products,
                    size text,
                    price_cents integer NOT NULL CHECK (price_cents >= 0)
                );
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT FROM pg_attribute WHERE attrelid = 'shop.sav_order_items'::regclass
                            AND attname = 'line_number' AND NOT attisdropped
                    ) THEN
                        ALTER TABLE shop.sav_order_items ADD COLUMN item_id text;
                        UPDATE shop.sav_order_items SET item_id = 'ITEM-' || order_id || '-' || line_number;
                        IF to_regclass('shop.sav_returns') IS NOT NULL AND EXISTS (
                            SELECT FROM pg_attribute WHERE attrelid = to_regclass('shop.sav_returns')
                                AND attname = 'line_number' AND NOT attisdropped
                        ) THEN
                            ALTER TABLE shop.sav_returns ADD COLUMN item_id text;
                            UPDATE shop.sav_returns SET item_id = 'ITEM-' || order_id || '-' || line_number;
                            ALTER TABLE shop.sav_returns DROP COLUMN line_number;
                        END IF;
                        IF to_regclass('shop.sav_requests') IS NOT NULL THEN
                            ALTER TABLE shop.sav_requests ADD COLUMN item_id text;
                            UPDATE shop.sav_requests SET item_id = 'ITEM-' || order_id || '-' || line_number;
                            ALTER TABLE shop.sav_requests DROP COLUMN line_number;
                        END IF;
                        ALTER TABLE shop.sav_order_items DROP COLUMN line_number;
                        ALTER TABLE shop.sav_order_items ADD PRIMARY KEY (item_id);
                    END IF;
                END $$;
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT FROM pg_attribute
                        WHERE attrelid = 'shop.sav_orders'::regclass AND attname = 'product_id' AND NOT attisdropped
                    ) THEN
                        INSERT INTO shop.sav_order_items (order_id, item_id, product_id, size, price_cents)
                        SELECT o.id, 'ITEM-' || o.id || '-1', o.product_id, o.size, p.price_cents
                        FROM shop.sav_orders o JOIN shop.sav_products p ON p.id = o.product_id
                        ON CONFLICT (item_id) DO NOTHING;
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
                ALTER TABLE shop.sav_returns ADD COLUMN IF NOT EXISTS item_id text;
                UPDATE shop.sav_returns SET item_id = 'ITEM-' || order_id || '-1' WHERE item_id IS NULL;
                ALTER TABLE shop.sav_returns ALTER COLUMN item_id SET NOT NULL;
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
                    item_id text NOT NULL,
                    action text NOT NULL,
                    size text,
                    color text,
                    status text NOT NULL DEFAULT 'awaiting_shop',
                    secret text NOT NULL
                );
                ALTER TABLE shop.sav_requests ALTER COLUMN item_id SET NOT NULL;
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT FROM pg_constraint WHERE conrelid = 'shop.sav_returns'::regclass
                            AND conname = 'sav_returns_item_id_fkey'
                    ) THEN
                        ALTER TABLE shop.sav_returns ADD FOREIGN KEY (item_id) REFERENCES shop.sav_order_items;
                    END IF;
                    IF NOT EXISTS (
                        SELECT FROM pg_constraint WHERE conrelid = 'shop.sav_requests'::regclass
                            AND conname = 'sav_requests_item_id_fkey'
                    ) THEN
                        ALTER TABLE shop.sav_requests ADD FOREIGN KEY (item_id) REFERENCES shop.sav_order_items;
                    END IF;
                END $$;
                CREATE TABLE IF NOT EXISTS shop.sav_messages (
                    case_id uuid NOT NULL REFERENCES shop.sav_requests,
                    kind text NOT NULL,
                    subject text NOT NULL,
                    body text NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (case_id, kind)
                );
                CREATE TABLE IF NOT EXISTS shop.sav_inquiries (
                    case_id uuid PRIMARY KEY,
                    customer_id text NOT NULL REFERENCES shop.sav_customers,
                    question text NOT NULL,
                    answer text,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    answered_at timestamptz
                );
                ALTER TABLE shop.sav_requests ALTER COLUMN status SET DEFAULT 'awaiting_shop';
                UPDATE shop.sav_requests SET status = CASE status
                    WHEN 'stock_reserve' THEN 'stock_reserved'
                    WHEN 'attente_boutique' THEN 'awaiting_shop'
                    WHEN 'attente_client' THEN 'awaiting_customer'
                    WHEN 'refuse_boutique' THEN 'rejected_by_shop'
                    WHEN 'termine' THEN 'completed'
                    WHEN 'annule' THEN 'cancelled' END
                WHERE status IN ('stock_reserve', 'attente_boutique', 'attente_client', 'refuse_boutique', 'termine', 'annule');
                UPDATE shop.sav_requests SET action = CASE action WHEN 'retour' THEN 'return' ELSE 'exchange' END
                WHERE action IN ('retour', 'echange');
                UPDATE shop.sav_returns SET action = CASE action WHEN 'retour' THEN 'return' ELSE 'exchange' END
                WHERE action IN ('retour', 'echange');
                UPDATE shop.sav_messages SET kind = CASE kind
                    WHEN 'boutique' THEN 'shop_notification'
                    WHEN 'refus' THEN 'rejection'
                    WHEN 'recapitulatif' THEN 'summary' END
                WHERE kind IN ('boutique', 'refus', 'recapitulatif');
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
                           'item_id', i.item_id, 'product_id', i.product_id,
                           'size', i.size, 'price_cents', i.price_cents,
                           'name', p.name, 'color', p.color, 'sized', p.sized
                       ) ORDER BY i.item_id) AS items
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
                {"item_id": item["item_id"], "name": item["name"], "color": item["color"], "size": item["size"]}
                for item in order["items"]
            ],
        }
        active_order_count = sum(
            other["id"] != order_id and other["status"] in {"confirmed", "in_transit"}
            for other in orders
        )
        return context, active_order_count

    def order_item(self, customer_id: str, order_id: str, item_id: str) -> dict | None:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            return conn.execute(
                "SELECT o.status, i.size, p.sized, p.color, p.id AS product_id " + ORDER_ITEM,
                (customer_id, order_id, item_id),
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
            INSERT INTO shop.sav_requests (case_id, thread_id, customer_id, order_id, item_id, action, size, color, secret, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (case_id) DO NOTHING
        """, (state["case_id"], thread_id, state["customer_id"], state["order_id"], state["item_id"],
              state["action"], state["size"], state["color"], token_hex(32), status)).rowcount == 1

    @staticmethod
    def delivered_item(conn, state: dict) -> dict:
        item = conn.execute(
            "SELECT o.status, p.id AS product_id, p.color " + ORDER_ITEM,
            (state["customer_id"], state["order_id"], state["item_id"]),
        ).fetchone()
        if item is None or item["status"] != "delivered":
            raise ValueError("Article livré introuvable.")
        return item

    def reserve_exchange(self, state: dict, thread_id: str) -> None:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            item = self.delivered_item(conn, state)
            if not self.insert_request(conn, state, thread_id, "stock_reserved"):
                return
            table, where, params = self.stock_location(item["product_id"], state["size"], state["color"], item["color"])
            reserved = conn.execute(f"UPDATE {table} SET quantity = quantity - 1 WHERE {where} AND quantity > 0", params)
            if not reserved.rowcount:
                raise OutOfStock(state["size"])

    def register_request(self, state: dict, thread_id: str) -> None:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            if state["action"] == "return":
                self.delivered_item(conn, state)
                self.insert_request(conn, state, thread_id, "awaiting_shop")
                return
            request = conn.execute("""
                UPDATE shop.sav_requests
                SET status = CASE WHEN status = 'stock_reserved' THEN 'awaiting_shop' ELSE status END
                WHERE case_id = %s AND thread_id = %s AND action = 'exchange'
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
            self.record_message(conn, case_id, "shop_notification", "Demande SAV à traiter", "Une demande attend votre décision.")

    def finish_step(self, case_id: str, status: str, reason: str = "") -> tuple[str | None, bool]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            request = conn.execute("""
                SELECT r.*, i.product_id, p.color AS original_color, p.name AS item_name
                FROM shop.sav_requests r JOIN shop.sav_order_items i USING (order_id, item_id)
                JOIN shop.sav_products p ON p.id = i.product_id WHERE r.case_id = %s FOR UPDATE OF r
            """, (case_id,)).fetchone()
            if request is None:
                raise ValueError("Demande SAV introuvable.")
            if request["status"] == status:
                if status == "completed":
                    row = conn.execute("SELECT id FROM shop.sav_returns WHERE case_id = %s", (case_id,)).fetchone()
                    return row["id"], False
                return None, False

            expected = "awaiting_shop" if status in {"rejected_by_shop", "awaiting_customer"} else "awaiting_customer"
            if request["status"] != expected:
                raise ValueError("Cette étape du dossier est déjà close.")
            conn.execute("UPDATE shop.sav_requests SET status = %s WHERE case_id = %s", (status, case_id))
            if status in {"rejected_by_shop", "cancelled"} and request["action"] == "exchange":
                table, where, params = self.stock_location(
                    request["product_id"], request["size"], request["color"], request["original_color"],
                )
                conn.execute(f"UPDATE {table} SET quantity = quantity + 1 WHERE {where}", params)

            action_label = "Retour" if request["action"] == "return" else "Échange"
            detail = f"Commande {request['order_id']} · {action_label} · {request['item_name']}"
            if request["color"]:
                detail += f" · {request['color']}"
            if request["size"]:
                detail += f" · taille {request['size']}"
            if status == "rejected_by_shop":
                self.record_message(conn, case_id, "rejection", "Demande SAV refusée", f"{detail}\nMotif : {reason}")
            if status == "awaiting_customer":
                self.record_message(conn, case_id, "confirmation", "Confirmez votre demande SAV", detail)
            if status == "completed":
                return_id = f"RET-{uuid4().hex[:12].upper()}"
                conn.execute("""
                    INSERT INTO shop.sav_returns (case_id, id, order_id, item_id, action, size, color)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (case_id, return_id, request["order_id"], request["item_id"], request["action"],
                      request["size"], request["color"]))
                self.record_message(conn, case_id, "summary", "Récapitulatif de votre demande SAV", f"{detail}\nRéférence : {return_id}")
                return return_id, True
        return None, False

    def requests_for_shop(self) -> list[dict]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            return conn.execute("""
                SELECT r.case_id::text, r.customer_id, r.order_id, r.action, r.size, r.color, p.name
                FROM shop.sav_requests r JOIN shop.sav_messages m ON m.case_id = r.case_id AND m.kind = 'shop_notification'
                JOIN shop.sav_order_items i USING (order_id, item_id)
                JOIN shop.sav_products p ON p.id = i.product_id
                WHERE r.status = 'awaiting_shop' AND strpos(r.thread_id, ':') > 0 ORDER BY m.created_at
            """).fetchall()

    def submit_inquiry(self, case_id: str, customer_id: str, question: str) -> None:
        with psycopg.connect(self.dsn) as conn:
            conn.execute("""
                INSERT INTO shop.sav_inquiries (case_id, customer_id, question)
                VALUES (%s, %s, %s) ON CONFLICT (case_id) DO NOTHING
            """, (case_id, customer_id, question))

    def unanswered_inquiries(self) -> list[dict]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            return conn.execute("""
                SELECT case_id::text, customer_id, question
                FROM shop.sav_inquiries WHERE answered_at IS NULL ORDER BY created_at
            """).fetchall()

    def answer_inquiry(self, case_id: str, answer: str) -> bool:
        with psycopg.connect(self.dsn) as conn:
            return bool(conn.execute("""
                UPDATE shop.sav_inquiries SET answer = %s, answered_at = COALESCE(answered_at, now())
                WHERE case_id = %s AND (answer IS NULL OR answer = %s)
            """, (answer, case_id, answer)).rowcount)

    def request_thread(self, case_id: str, decision: str | None = None, signature: str = "") -> str | None:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            request = conn.execute(
                "SELECT thread_id, secret FROM shop.sav_requests WHERE case_id = %s AND status <> 'stock_reserved'",
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
                SELECT r.case_id::text, r.secret, r.status, m.kind, m.subject, m.body, m.created_at
                FROM shop.sav_messages m JOIN shop.sav_requests r USING (case_id)
                WHERE r.customer_id = %s AND m.kind <> 'shop_notification'
                    AND strpos(r.thread_id, ':') > 0
                UNION ALL
                SELECT NULL, NULL, NULL, 'inquiry', 'Réponse du SAV', answer, answered_at
                FROM shop.sav_inquiries WHERE customer_id = %s AND answered_at IS NOT NULL
                ORDER BY created_at DESC
            """, (customer_id, customer_id)).fetchall()
        for message in messages:
            message.pop("created_at")
            secret = message.pop("secret")
            case_id = message.pop("case_id")
            if message.pop("status") == "awaiting_customer" and message["kind"] == "confirmation":
                message["links"] = {
                    decision: f"/support/reply/{case_id}/{decision}/{self.sign_decision(secret, case_id, decision)}"
                    for decision in ("confirm", "decline")
                }
        return messages
