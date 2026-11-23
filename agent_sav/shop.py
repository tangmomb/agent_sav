import psycopg
from psycopg.rows import dict_row

from .demo_data import seed_demo

DEFAULT_DSN = "postgresql://sav:sav@127.0.0.1:55432/sav"


class OutOfStock(Exception):
    pass


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
                -- One row per support case: a return or exchange of an article, or a question.
                CREATE TABLE IF NOT EXISTS shop.sav_requests (
                    thread_id text PRIMARY KEY,
                    customer_id text NOT NULL REFERENCES shop.customers,
                    action text NOT NULL CHECK (action IN ('return', 'exchange', 'information')),
                    item_id text REFERENCES shop.order_items,
                    size text,
                    color text,
                    status text NOT NULL DEFAULT 'awaiting_shop' CHECK (status IN (
                        'awaiting_shop', 'rejected_by_shop', 'completed'
                    )),
                    created_at timestamptz NOT NULL DEFAULT now(),
                    CHECK ((action = 'information') = (item_id IS NULL))
                );
                -- Messages of a case: the customer's question, and the emails sent to the
                -- customer by the support agent (sav) or by the shop.
                CREATE TABLE IF NOT EXISTS shop.sav_messages (
                    thread_id text NOT NULL REFERENCES shop.sav_requests,
                    sender text NOT NULL CHECK (sender IN ('customer', 'sav', 'shop')),
                    body text NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (thread_id, created_at)
                );
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
                SELECT o.id, o.customer_id, o.ordered_on::text, o.status,
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

    def delivered_items(self, customer_id: str) -> list[dict]:
        """Articles of the customer's delivered orders: the ones a return or exchange can be about."""
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            return conn.execute("""
                SELECT i.item_id, i.order_id, o.ordered_on::text, i.product_id, p.name, p.color, i.size
                FROM shop.orders o JOIN shop.order_items i ON i.order_id = o.id
                JOIN shop.products p ON p.id = i.product_id
                WHERE o.customer_id = %s AND o.status = 'delivered'
                ORDER BY o.ordered_on DESC, i.item_id
            """, (customer_id,)).fetchall()

    def variants(self, product_id: str) -> list[str]:
        """Other colors this product exists in."""
        with psycopg.connect(self.dsn) as conn:
            return [row[0] for row in conn.execute("""
                SELECT other.color FROM shop.products p JOIN shop.products other ON other.name = p.name
                WHERE p.id = %s AND other.id <> p.id ORDER BY other.color
            """, (product_id,))]

    def in_stock(self, product_id: str, size: str | None, color: str) -> bool:
        """Whether product_id's product is available in this size and color."""
        with psycopg.connect(self.dsn) as conn:
            return conn.execute("""
                SELECT 1 FROM shop.stock s
                JOIN shop.products wanted ON wanted.id = s.product_id
                JOIN shop.products bought ON bought.name = wanted.name
                WHERE bought.id = %s AND wanted.color = %s AND s.size = %s AND s.quantity > 0
            """, (product_id, color, size or "")).fetchone() is not None

    @staticmethod
    def move_stock(conn, product_id: str, size: str | None, color: str, change: int) -> bool:
        """Changes the stock of product_id's product in the given color and size."""
        return bool(conn.execute("""
            UPDATE shop.stock s SET quantity = quantity + %s
            FROM shop.products bought JOIN shop.products wanted ON wanted.name = bought.name
            WHERE bought.id = %s AND wanted.color = %s AND s.product_id = wanted.id
                AND s.size = %s AND s.quantity + %s >= 0
        """, (change, product_id, color, size or "", change)).rowcount)

    def register(self, thread_id: str, customer_id: str, request: dict) -> None:
        """Records a return or exchange for the shop; an exchange reserves the wanted variant."""
        exchange = request["action"] == "exchange"
        with psycopg.connect(self.dsn) as conn:
            created = conn.execute("""
                INSERT INTO shop.sav_requests (thread_id, customer_id, action, item_id, size, color)
                SELECT %s, o.customer_id, %s, i.item_id, %s, %s
                FROM shop.order_items i JOIN shop.orders o ON o.id = i.order_id
                WHERE i.item_id = %s AND o.customer_id = %s AND o.status = 'delivered'
                ON CONFLICT (thread_id) DO NOTHING
            """, (thread_id, request["action"], request["size"] if exchange else None,
                  request["color"] if exchange else None, request["item_id"], customer_id)).rowcount
            if not created:
                if conn.execute("SELECT 1 FROM shop.sav_requests WHERE thread_id = %s", (thread_id,)).fetchone():
                    return
                raise ValueError("Article livré introuvable.")
            if exchange:
                (product_id,) = conn.execute(
                    "SELECT product_id FROM shop.order_items WHERE item_id = %s", (request["item_id"],),
                ).fetchone()
                if not self.move_stock(conn, product_id, request["size"], request["color"], -1):
                    raise OutOfStock(request["size"])

    @staticmethod
    def send_message(conn, thread_id: str, sender: str, body: str) -> None:
        conn.execute("INSERT INTO shop.sav_messages (thread_id, sender, body) VALUES (%s, %s, %s)", (thread_id, sender, body))

    def finish_step(self, thread_id: str, status: str, reason: str = "") -> bool:
        """Records the shop's decision on a return or exchange and emails the customer.

        Returns False if the decision was already recorded, so a replayed step sends nothing twice.
        """
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            request = conn.execute("""
                SELECT r.*, i.order_id, i.product_id, p.name AS item_name
                FROM shop.sav_requests r JOIN shop.order_items i USING (item_id)
                JOIN shop.products p ON p.id = i.product_id WHERE r.thread_id = %s FOR UPDATE OF r
            """, (thread_id,)).fetchone()
            if request is None:
                raise ValueError("Demande SAV introuvable.")
            if request["status"] == status:
                return False
            if request["status"] != "awaiting_shop":
                raise ValueError("Cette demande a déjà reçu une décision.")
            conn.execute("UPDATE shop.sav_requests SET status = %s WHERE thread_id = %s", (status, thread_id))

            action_label = "Retour" if request["action"] == "return" else "Échange"
            detail = f"Commande {request['order_id']} · {action_label} · {request['item_name']}"
            detail += "".join(f" · {value}" for value in (request["color"], request["size"] and f"taille {request['size']}") if value)
            if status == "rejected_by_shop":
                if request["action"] == "exchange":
                    self.move_stock(conn, request["product_id"], request["size"], request["color"], 1)
                self.send_message(conn, thread_id, "shop", f"Demande SAV refusée\n{detail}\nMotif : {reason}")
            else:
                self.send_message(conn, thread_id, "shop",
                                  f"Demande SAV acceptée\n{detail}\nRéférence : {self.reference(thread_id)}")
            return True

    def requests_for_shop(self) -> list[dict]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            return conn.execute("""
                SELECT r.thread_id, r.customer_id, i.order_id, r.action, r.size, r.color, p.name, r.created_at,
                       i.size AS bought_size, p.color AS bought_color, c.first_name, c.name AS last_name
                FROM shop.sav_requests r JOIN shop.order_items i USING (item_id)
                JOIN shop.products p ON p.id = i.product_id JOIN shop.customers c ON c.id = r.customer_id
                WHERE r.status = 'awaiting_shop' AND strpos(r.thread_id, ':') > 0 ORDER BY r.created_at
            """).fetchall()

    @staticmethod
    def reference(thread_id: str) -> str:
        """Return reference given to the customer, taken from the thread's UUID."""
        return "RET-" + thread_id[-36:].replace("-", "")[:12].upper()

    def submit_inquiry(self, thread_id: str, customer_id: str, question: str) -> None:
        with psycopg.connect(self.dsn) as conn:
            created = conn.execute("""
                INSERT INTO shop.sav_requests (thread_id, customer_id, action)
                VALUES (%s, %s, 'information') ON CONFLICT (thread_id) DO NOTHING
            """, (thread_id, customer_id)).rowcount
            if created:
                self.send_message(conn, thread_id, "customer", question)

    def unanswered_inquiries(self) -> list[dict]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            return conn.execute("""
                SELECT r.thread_id, r.customer_id, m.body AS question, r.created_at, c.first_name, c.name AS last_name
                FROM shop.sav_requests r JOIN shop.sav_messages m ON m.thread_id = r.thread_id AND m.sender = 'customer'
                JOIN shop.customers c ON c.id = r.customer_id
                WHERE r.action = 'information' AND r.status = 'awaiting_shop' ORDER BY r.created_at
            """).fetchall()

    def answer_inquiry(self, thread_id: str, answer: str) -> bool:
        """Records the shop's answer once; sending the same answer again is accepted."""
        body = f"Réponse de la boutique\n{answer}"
        with psycopg.connect(self.dsn) as conn:
            request = conn.execute(
                "SELECT status FROM shop.sav_requests WHERE thread_id = %s AND action = 'information' FOR UPDATE",
                (thread_id,),
            ).fetchone()
            if request is None:
                return False
            if request[0] == "completed":
                return conn.execute(
                    "SELECT 1 FROM shop.sav_messages WHERE thread_id = %s AND body = %s", (thread_id, body),
                ).fetchone() is not None
            conn.execute("UPDATE shop.sav_requests SET status = 'completed' WHERE thread_id = %s", (thread_id,))
            self.send_message(conn, thread_id, "shop", body)
            return True

    def request_thread(self, thread_id: str) -> str | None:
        """Returns the thread if it holds a return or exchange."""
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute(
                "SELECT 1 FROM shop.sav_requests WHERE thread_id = %s AND action <> 'information'", (thread_id,),
            ).fetchone()
        return thread_id if row else None

    def mailbox(self, customer_id: str) -> list[dict]:
        """Emails sent to the customer, newest first."""
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            return conn.execute("""
                SELECT m.thread_id, m.sender, m.body, m.created_at
                FROM shop.sav_messages m JOIN shop.sav_requests r USING (thread_id)
                WHERE r.customer_id = %s AND m.sender <> 'customer'
                    AND (r.action = 'information' OR strpos(r.thread_id, ':') > 0)
                ORDER BY m.created_at DESC
            """, (customer_id,)).fetchall()
