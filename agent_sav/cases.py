"""Support cases in the shop database.

Every write can be replayed: opening a case is keyed by the request that asked for it, moving a case
forward depends on its current status, and emails and commands are keyed too.
"""

import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from . import policy
from .policy import Verdict, euros
from .shop import Shop

ACTIVE = ("opened", "awaiting_shop", "awaiting_parcel")
KINDS = {"return": "Retour", "exchange": "Échange", "refund": "Remboursement", "question": "Renseignement"}


class WrongStatus(ValueError):
    pass


def variant(color: str | None, size: str | None) -> str:
    return " · ".join(value for value in (color, size) if value)


def article(item: dict) -> str:
    return f"{item['name']} ({variant(item['color'], item['size'])}, commande {item['order_id']})"


class Cases:
    def __init__(self, dsn: str, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.dsn = dsn
        self.clock = clock

    def connect(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def setup(self) -> None:
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('sav_cases_setup', 0))")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS shop.cases (
                    id text PRIMARY KEY,
                    request_key text NOT NULL UNIQUE,  -- what asked for it, so that a replay opens nothing twice
                    customer_id text NOT NULL REFERENCES shop.customers,
                    kind text NOT NULL CHECK (kind IN ('return', 'exchange', 'refund', 'question')),
                    route text NOT NULL CHECK (route IN ('auto', 'shop')),
                    rule text NOT NULL,  -- the policy rule that allowed it
                    status text NOT NULL DEFAULT 'opened' CHECK (status IN (
                        'opened', 'awaiting_shop', 'awaiting_parcel', 'completed', 'rejected', 'cancelled', 'expired'
                    )),
                    order_id text REFERENCES shop.orders,
                    item_id text REFERENCES shop.order_items,
                    size text,  -- exchange
                    color text,  -- exchange
                    reason text,
                    details text NOT NULL DEFAULT '',  -- what the customer said
                    amount_cents integer CHECK (amount_cents > 0),
                    remind_at timestamptz,
                    due_at timestamptz,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    CHECK ((kind IN ('return', 'exchange')) = (item_id IS NOT NULL)),
                    CHECK (kind <> 'refund' OR order_id IS NOT NULL)
                );
                -- Whatever the rules say, an article is sent back once at a time and returned only once.
                CREATE UNIQUE INDEX IF NOT EXISTS one_case_per_item ON shop.cases (item_id)
                    WHERE item_id IS NOT NULL AND (status IN ('opened', 'awaiting_shop', 'awaiting_parcel')
                        OR (kind = 'return' AND status = 'completed'));
                CREATE UNIQUE INDEX IF NOT EXISTS one_refund_per_order ON shop.cases (order_id, reason)
                    WHERE kind = 'refund' AND status IN ('opened', 'awaiting_shop', 'completed');
                -- Inputs of the case graph, handled in order and once each.
                CREATE TABLE IF NOT EXISTS shop.case_commands (
                    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    case_id text NOT NULL REFERENCES shop.cases,
                    kind text NOT NULL,
                    payload jsonb NOT NULL DEFAULT '{}',
                    key text NOT NULL UNIQUE,
                    outcome text CHECK (outcome IN ('applied', 'ignored')),
                    created_at timestamptz NOT NULL DEFAULT now()
                );
                CREATE TABLE IF NOT EXISTS shop.case_messages (
                    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    case_id text NOT NULL REFERENCES shop.cases,
                    sender text NOT NULL CHECK (sender IN ('sav', 'shop')),
                    body text NOT NULL,
                    key text NOT NULL UNIQUE,
                    created_at timestamptz NOT NULL DEFAULT now()
                );
                CREATE TABLE IF NOT EXISTS shop.refunds (
                    case_id text PRIMARY KEY REFERENCES shop.cases,
                    order_id text NOT NULL REFERENCES shop.orders,
                    amount_cents integer NOT NULL CHECK (amount_cents > 0),
                    created_at timestamptz NOT NULL DEFAULT now()
                );
            """)

    # What the rules look at.

    @staticmethod
    def item(conn, customer_id: str, item_id: str) -> dict | None:
        return conn.execute("""
            SELECT i.item_id, i.order_id, i.product_id, i.size, p.name, p.color, p.price_cents, o.status, o.delivered_on,
                   (SELECT c.id FROM shop.cases c WHERE c.item_id = i.item_id
                        AND c.status IN ('opened', 'awaiting_shop', 'awaiting_parcel')) AS active_case,
                   EXISTS (SELECT 1 FROM shop.cases c WHERE c.item_id = i.item_id
                        AND c.kind = 'return' AND c.status = 'completed') AS returned
            FROM shop.order_items i JOIN shop.orders o ON o.id = i.order_id JOIN shop.products p ON p.id = i.product_id
            WHERE i.item_id = %s AND o.customer_id = %s
        """, (item_id, customer_id)).fetchone()

    @staticmethod
    def variants(conn, product_id: str) -> list[dict]:
        """The sizes and colors of a product, with their stock. Unsized products have an empty size."""
        return conn.execute("""
            SELECT p.color, s.size, s.quantity FROM shop.products bought
            JOIN shop.products p ON p.name = bought.name JOIN shop.stock s ON s.product_id = p.id
            WHERE bought.id = %s ORDER BY p.id, array_position(ARRAY['S', 'M', 'L', 'XL'], s.size)
        """, (product_id,)).fetchall()

    @staticmethod
    def order(conn, customer_id: str, order_id: str) -> dict | None:
        return conn.execute("""
            SELECT o.id, o.status, o.ordered_on,
                   (SELECT sum(p.price_cents)::int FROM shop.order_items i JOIN shop.products p ON p.id = i.product_id
                    WHERE i.order_id = o.id) AS total_cents,
                   ARRAY(SELECT c.reason FROM shop.cases c WHERE c.order_id = o.id AND c.kind = 'refund'
                         AND c.status IN ('opened', 'awaiting_shop', 'completed')) AS refunds
            FROM shop.orders o WHERE o.id = %s AND o.customer_id = %s
        """, (order_id, customer_id)).fetchone()

    def check(self, conn, customer_id: str, action: str, args: dict) -> tuple[Verdict, dict]:
        """Applies the rules to a proposal. Returns the verdict and the columns of the case it would open."""
        now = self.clock()
        today = now.date()
        recent_auto = conn.execute("""
            SELECT count(*)::int AS total FROM shop.cases WHERE customer_id = %s AND route = 'auto'
                AND status NOT IN ('cancelled', 'expired') AND created_at > %s
        """, (customer_id, now - timedelta(days=policy.AUTO_LIMIT_DAYS))).fetchone()["total"]
        fields = {"reason": args.get("reason"), "details": args.get("details") or ""}

        if action == "propose_return":
            item = self.item(conn, customer_id, args["item_id"])
            verdict = policy.decide_return(item, args["reason"], today, recent_auto)
            fields |= {"kind": "return", "item_id": args["item_id"], "order_id": item and item["order_id"]}
        elif action == "propose_exchange":
            item = self.item(conn, customer_id, args["item_id"])
            wanted, quantity = None, 0
            if item:
                size = args.get("size") or item["size"] or ""
                color = (args.get("color") or item["color"]).casefold()
                found = next((each for each in self.variants(conn, item["product_id"])
                              if (each["color"].casefold(), each["size"]) == (color, size)), None)
                if found:
                    wanted, quantity = (found["size"] or None, found["color"]), found["quantity"]
            verdict = policy.decide_exchange(item, wanted, quantity > 0, args["reason"], today, recent_auto)
            fields |= {"kind": "exchange", "item_id": args["item_id"], "order_id": item and item["order_id"],
                       "size": wanted and wanted[0], "color": wanted and wanted[1]}
        elif action == "propose_refund":
            order = self.order(conn, customer_id, args["order_id"])
            amount = args.get("amount_euros")
            cents = None if amount is None else round(amount * 100)
            verdict = policy.decide_refund(order, args["reason"], cents, today, recent_auto)
            fields |= {"kind": "refund", "order_id": args["order_id"]}
        elif action == "cancel_case":
            case = self.get(args["case_id"], conn)
            own = case if case and case["customer_id"] == customer_id else None
            return policy.decide_cancel(own), {"case_id": args["case_id"], "kind": "cancel"}
        else:
            total = conn.execute("""
                SELECT count(*)::int AS total FROM shop.cases
                WHERE customer_id = %s AND kind = 'question' AND status = 'awaiting_shop'
            """, (customer_id,)).fetchone()["total"]
            verdict = policy.decide_question(total)
            fields |= {"kind": "question", "details": args["question"]}
        return verdict, fields | {"amount_cents": verdict.amount_cents}

    def propose(self, customer_id: str, action: str, args: dict) -> tuple[Verdict, str]:
        """The verdict on a proposal, and the summary the customer is asked to agree to."""
        with self.connect() as conn:
            verdict, fields = self.check(conn, customer_id, action, args)
            if verdict.outcome == "refused":
                return verdict, ""
            return verdict, self.summary(conn, customer_id, verdict, fields)

    def summary(self, conn, customer_id: str, verdict: Verdict, fields: dict) -> str:
        if fields["kind"] == "question":
            return ""
        if fields["kind"] == "cancel":
            case = self.get(fields["case_id"], conn)
            return f"Annulation du dossier {case['id']} ({KINDS[case['kind']].lower()})"
        if fields["kind"] == "refund":
            lines = [f"Remboursement : commande {fields['order_id']}", f"Montant : {euros(verdict.amount_cents)}"]
        else:
            item = self.item(conn, customer_id, fields["item_id"])
            lines = [f"{KINDS[fields['kind']]} : {article(item)}"]
            if fields["kind"] == "exchange":
                lines.append(f"Contre : {variant(fields['color'], fields['size'])}")
            else:
                lines.append(f"Remboursement : {euros(verdict.amount_cents)}, à réception du colis")
        lines.append("Acceptation : immédiate" if verdict.outcome == "auto" else "Acceptation : soumise à la boutique")
        return "\n".join(lines)

    # Writes.

    def open(self, request_key: str, customer_id: str, action: str, args: dict) -> tuple[dict | None, Verdict | None]:
        """Opens the case of an agreed proposal. The rules are applied again: things may have changed since.

        Returns the case, or the verdict that now refuses it.
        """
        with self.connect() as conn:
            # One opening at a time per customer.
            conn.execute("SELECT 1 FROM shop.customers WHERE id = %s FOR NO KEY UPDATE", (customer_id,))
            existing = conn.execute("SELECT * FROM shop.cases WHERE request_key = %s", (request_key,)).fetchone()
            if existing:
                return existing, None
            verdict, fields = self.check(conn, customer_id, action, args)
            if verdict.outcome == "refused":
                return None, verdict
            if fields["kind"] == "exchange":
                item = self.item(conn, customer_id, fields["item_id"])
                if not Shop.move_stock(conn, item["product_id"], fields["size"], fields["color"], -1):
                    return None, policy.refused("out_of_stock", "Cette variante vient de passer en rupture de stock.")
            case = conn.execute("""
                INSERT INTO shop.cases (id, request_key, customer_id, kind, route, rule, order_id, item_id, size, color,
                                        reason, details, amount_cents, created_at, updated_at)
                VALUES (%(id)s, %(request_key)s, %(customer_id)s, %(kind)s, %(route)s, %(rule)s, %(order_id)s, %(item_id)s,
                        %(size)s, %(color)s, %(reason)s, %(details)s, %(amount_cents)s, %(now)s, %(now)s)
                RETURNING *
            """, dict.fromkeys(("order_id", "item_id", "size", "color")) | fields | {
                "id": "SAV-" + hashlib.sha256(request_key.encode()).hexdigest()[:8].upper(),
                "request_key": request_key, "customer_id": customer_id,
                "route": verdict.outcome, "rule": verdict.rule, "now": self.clock(),
            }).fetchone()
            return case, None

    def advance(self, case_id: str, before: tuple[str, ...], after: str, mail: str = "", *, sender: str = "sav",
                effect: Callable | None = None) -> bool:
        """Moves a case to its next status, with its email and its effect, in one transaction.

        Returns False when the case is already there, so a replayed step changes nothing.
        """
        with self.connect() as conn:
            case = conn.execute("SELECT * FROM shop.cases WHERE id = %s FOR UPDATE", (case_id,)).fetchone()
            if case["status"] == after:
                return False
            if case["status"] not in before:
                raise WrongStatus(f"Le dossier {case_id} est « {case['status']} », pas {' ou '.join(before)}.")
            conn.execute("UPDATE shop.cases SET status = %s, remind_at = NULL, due_at = NULL, updated_at = %s WHERE id = %s",
                         (after, self.clock(), case_id))
            if effect:
                effect(conn, case)
            if mail:
                self.send_mail(conn, case_id, after, mail, sender)
            return True

    def send_mail(self, conn, case_id: str, step: str, body: str, sender: str = "sav") -> None:
        conn.execute("""
            INSERT INTO shop.case_messages (case_id, sender, body, key, created_at) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (key) DO NOTHING
        """, (case_id, sender, body, f"{case_id}:{step}", self.clock()))

    def wait_for_parcel(self, conn, case: dict) -> None:
        now = self.clock()
        conn.execute("UPDATE shop.cases SET remind_at = %s, due_at = %s WHERE id = %s", (
            now + timedelta(days=policy.PARCEL_REMINDER_DAYS), now + timedelta(days=policy.PARCEL_EXPIRY_DAYS), case["id"]))

    def remind(self, case_id: str, body: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE shop.cases SET remind_at = NULL WHERE id = %s", (case_id,))
            self.send_mail(conn, case_id, "reminder", body)

    @staticmethod
    def release_stock(conn, case: dict) -> None:
        """Gives back the variant an exchange had reserved."""
        if case["kind"] == "exchange":
            (product_id,) = conn.execute(
                "SELECT product_id FROM shop.order_items WHERE item_id = %s", (case["item_id"],)).fetchone().values()
            Shop.move_stock(conn, product_id, case["size"], case["color"], 1)

    @staticmethod
    def settle(conn, case: dict) -> None:
        """Pays the refund of a case, or turns the exchanged article into the new one."""
        if case["item_id"]:
            item = conn.execute("""
                SELECT i.product_id, i.size, p.color FROM shop.order_items i JOIN shop.products p ON p.id = i.product_id
                WHERE i.item_id = %s
            """, (case["item_id"],)).fetchone()
            if case["reason"] != "damaged":
                Shop.move_stock(conn, item["product_id"], item["size"], item["color"], 1)
        if case["kind"] == "exchange":
            conn.execute("""
                UPDATE shop.order_items i SET size = %s, product_id = wanted.id
                FROM shop.products bought JOIN shop.products wanted ON wanted.name = bought.name
                WHERE i.item_id = %s AND bought.id = i.product_id AND wanted.color = %s
            """, (case["size"], case["item_id"], case["color"]))
        else:
            conn.execute("INSERT INTO shop.refunds (case_id, order_id, amount_cents) VALUES (%s, %s, %s)",
                         (case["id"], case["order_id"], case["amount_cents"]))

    # Commands: what happens to a case from outside (shop, customer, parcel, time).

    def command(self, case_id: str, kind: str, key: str, payload: dict | None = None) -> int:
        with self.connect() as conn:
            conn.execute("""
                INSERT INTO shop.case_commands (case_id, kind, payload, key) VALUES (%s, %s, %s, %s)
                ON CONFLICT (key) DO NOTHING
            """, (case_id, kind, Json(payload or {}), key))
            return conn.execute("SELECT id FROM shop.case_commands WHERE key = %s", (key,)).fetchone()["id"]

    def next_command(self, case_id: str) -> dict | None:
        with self.connect() as conn:
            return conn.execute("""
                SELECT id, kind, payload FROM shop.case_commands
                WHERE case_id = %s AND outcome IS NULL ORDER BY id LIMIT 1
            """, (case_id,)).fetchone()

    def command_done(self, command_id: int, outcome: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE shop.case_commands SET outcome = %s WHERE id = %s", (outcome, command_id))

    def command_outcome(self, command_id: int) -> str | None:
        with self.connect() as conn:
            return conn.execute("SELECT outcome FROM shop.case_commands WHERE id = %s", (command_id,)).fetchone()["outcome"]

    def add_due_commands(self) -> None:
        """Turns the deadlines that have passed into commands, once each."""
        with self.connect() as conn:
            conn.execute("""
                INSERT INTO shop.case_commands (case_id, kind, key)
                SELECT id, due.kind, id || ':' || due.kind || ':' || due.at
                FROM shop.cases, LATERAL (VALUES ('expire', due_at), ('remind', remind_at)) AS due (kind, at)
                WHERE status = 'awaiting_parcel' AND due.at <= %(now)s AND (due.kind = 'expire' OR due_at > %(now)s)
                ON CONFLICT (key) DO NOTHING
            """, {"now": self.clock()})

    def to_run(self) -> list[str]:
        """Cases whose graph has something to do, including after a crash."""
        with self.connect() as conn:
            return [row["id"] for row in conn.execute("""
                SELECT id FROM shop.cases WHERE status = 'opened'
                    OR id IN (SELECT case_id FROM shop.case_commands WHERE outcome IS NULL)
                ORDER BY created_at
            """)]

    # Reads.

    def get(self, case_id: str, conn=None) -> dict | None:
        if conn is None:
            with self.connect() as conn:
                return self.get(case_id, conn)
        return conn.execute("SELECT * FROM shop.cases WHERE id = %s", (case_id,)).fetchone()

    def describe(self, case: dict) -> str:
        """One line naming what a case is about, for emails."""
        if case["kind"] == "question":
            return f"Dossier {case['id']}"
        text = f"Dossier {case['id']} · {KINDS[case['kind']]} · commande {case['order_id']}"
        if case["item_id"]:
            with self.connect() as conn:
                text += " · " + self.item(conn, case["customer_id"], case["item_id"])["name"]
        if case["kind"] == "exchange":
            text += " · " + variant(case["color"], case["size"])
        return text

    def of_customer(self, customer_id: str) -> list[dict]:
        with self.connect() as conn:
            return conn.execute("""
                SELECT c.id, c.kind, c.status, c.order_id, c.item_id, p.name AS article, c.size, c.color, c.amount_cents,
                       c.created_at, c.due_at,
                       (SELECT m.body FROM shop.case_messages m WHERE m.case_id = c.id ORDER BY m.id DESC LIMIT 1) AS last_mail
                FROM shop.cases c LEFT JOIN shop.order_items i USING (item_id) LEFT JOIN shop.products p ON p.id = i.product_id
                WHERE c.customer_id = %s ORDER BY c.created_at DESC LIMIT 10
            """, (customer_id,)).fetchall()

    def for_shop(self) -> list[dict]:
        """What the shop has to act on: decisions, answers and parcels, oldest first."""
        with self.connect() as conn:
            return conn.execute("""
                SELECT c.*, p.name, p.color AS bought_color, i.size AS bought_size, u.first_name, u.name AS last_name
                FROM shop.cases c JOIN shop.customers u ON u.id = c.customer_id
                LEFT JOIN shop.order_items i USING (item_id) LEFT JOIN shop.products p ON p.id = i.product_id
                WHERE c.status IN ('awaiting_shop', 'awaiting_parcel') ORDER BY c.created_at
            """).fetchall()

    def mailbox(self, customer_id: str) -> list[dict]:
        """Emails sent to the customer, newest first."""
        with self.connect() as conn:
            return conn.execute("""
                SELECT m.case_id, m.sender, m.body, m.created_at
                FROM shop.case_messages m JOIN shop.cases c ON c.id = m.case_id
                WHERE c.customer_id = %s ORDER BY m.created_at DESC, m.id DESC
            """, (customer_id,)).fetchall()
