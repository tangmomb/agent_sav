from uuid import uuid4

import psycopg

DEMO_ORDER = "DEMO-1001"
DEFAULT_DSN = "postgresql://sav:sav@127.0.0.1:55432/sav"


class OutOfStock(Exception):
    pass


class Shop:
    """Service marchand simulé, avec son propre commit PostgreSQL."""

    def __init__(self, dsn: str):
        self.dsn = dsn

    def setup(self) -> None:
        with psycopg.connect(self.dsn) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sav_stock (
                    size text PRIMARY KEY,
                    quantity integer NOT NULL CHECK (quantity >= 0)
                );
                CREATE TABLE IF NOT EXISTS sav_returns (
                    case_id uuid PRIMARY KEY,
                    id text UNIQUE NOT NULL,
                    order_id text NOT NULL,
                    action text NOT NULL,
                    size text
                );
                INSERT INTO sav_stock (size, quantity)
                VALUES ('S', 20), ('M', 20), ('L', 20), ('XL', 0)
                ON CONFLICT DO NOTHING;
            """)

    def create_return(
        self, case_id: str, order_id: str, action: str, size: str | None
    ) -> tuple[str, bool]:
        """Retourne la référence et indique si cet appel a créé le retour."""
        return_id = f"RET-{uuid4().hex[:12].upper()}"
        with psycopg.connect(self.dsn) as conn:
            inserted = conn.execute(
                """
                INSERT INTO sav_returns (case_id, id, order_id, action, size)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (case_id) DO NOTHING
                RETURNING id
                """,
                (case_id, return_id, order_id, action, size),
            ).fetchone()

            if inserted is None:
                existing = conn.execute(
                    "SELECT id, order_id, action, size FROM sav_returns WHERE case_id = %s",
                    (case_id,),
                ).fetchone()
                if existing is None or existing[1:] != (order_id, action, size):
                    raise ValueError("La clé d'idempotence correspond à une autre opération.")
                return existing[0], False

            if action == "echange":
                reserved = conn.execute(
                    """
                    UPDATE sav_stock SET quantity = quantity - 1
                    WHERE size = %s AND quantity > 0
                    RETURNING size
                    """,
                    (size,),
                ).fetchone()
                if reserved is None:
                    raise OutOfStock(size)

        return return_id, True
