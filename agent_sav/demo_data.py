from datetime import date, timedelta

DEMO_CUSTOMER = "CLI-001"
DEMO_ORDER = "DEMO-1001"
DEMO_PRODUCT = "ART-001"
SIZES = ("S", "M", "L", "XL")

CUSTOMERS = (
    "Camille Moreau", "Noah Laurent", "Inès Martin", "Jules Bernard",
    "Lina Petit", "Adam Robert", "Zoé Richard", "Gabriel Durand",
    "Léa Dubois", "Hugo Simon", "Maya Michel", "Louis Leroy",
    "Sarah Roux", "Arthur David", "Jade Mercier", "Émile Garnier",
    "Nina Fontaine", "Oscar Chevalier", "Anna Girard", "Sacha André",
)

# Nom, couleur, prix en centimes, article décliné en tailles.
PRODUCTS = (
    ("Pull en laine côtelée", "Vert mousse", 8900, True),
    ("Chemise Oxford", "Bleu ciel", 6500, True),
    ("Jean droit", "Indigo", 7900, True),
    ("T-shirt en coton épais", "Écru", 2900, True),
    ("Veste de travail", "Sable", 11900, True),
    ("Cardigan en mérinos", "Gris chiné", 9900, True),
    ("Pantalon chino", "Marine", 7500, True),
    ("Sweat à col rond", "Bordeaux", 5900, True),
    ("Robe chemise", "Noir", 9500, True),
    ("Jupe en denim", "Bleu brut", 6900, True),
    ("Polo en maille", "Olive", 5500, True),
    ("Short en lin", "Argile", 4900, True),
    ("Blouson matelassé", "Anthracite", 13900, True),
    ("Débardeur côtelé", "Blanc cassé", 2500, True),
    ("Surchemise en flanelle", "Carreaux marine", 8500, True),
    ("Écharpe en laine", "Caramel", 4200, False),
    ("Bonnet en mérinos", "Gris ardoise", 3200, False),
    ("Sac cabas en toile", "Écru", 3900, False),
    ("Pochette zippée", "Terracotta", 2400, False),
    ("Foulard imprimé", "Bleu nuit", 3500, False),
)


def seed_demo(conn) -> None:
    customers = [(f"CLI-{index:03}", name) for index, name in enumerate(CUSTOMERS, 1)]
    products = [(f"ART-{index:03}", *product) for index, product in enumerate(PRODUCTS, 1)]
    orders = []
    items = []
    today = date.today()
    for index, (customer_id, _) in enumerate(customers):
        order_count = 2 + index % 4
        has_pending_delivery = index % 4 == 2
        for position in range(order_count + int(has_pending_delivery)):
            order_id = f"DEMO-{1001 + index * 5 + position}"
            ordered_on = today - timedelta(days=2 + (index % 4) + position * 21)
            status = "livree"
            if has_pending_delivery and position == 0:
                status = "en_livraison"
            elif position == order_count:
                ordered_on = today
                status = "validee"
            orders.append((order_id, customer_id, ordered_on, status))
            pattern = (index + position) % 8
            offsets = (0, 3, 9) if pattern == 0 else (0, 3) if pattern == 4 else (0,)
            for line_number, offset in enumerate(offsets, 1):
                product_id, _, _, price_cents, sized = products[(index + position * 7 + offset) % len(products)]
                size = SIZES[(index + position + offset) % len(SIZES)] if sized else None
                items.append((order_id, line_number, product_id, size, price_cents))

    with conn.cursor() as cursor:
        cursor.executemany("INSERT INTO shop.sav_customers VALUES (%s, %s) ON CONFLICT DO NOTHING", customers)
        cursor.executemany("INSERT INTO shop.sav_products VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING", products)
        cursor.executemany(
            """INSERT INTO shop.sav_orders (id, customer_id, ordered_on, status)
               VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
            orders,
        )
        cursor.executemany(
            """INSERT INTO shop.sav_order_items (order_id, line_number, product_id, size, price_cents)
               VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
            items,
        )
        cursor.executemany(
            "INSERT INTO shop.sav_stock (product_id, size, quantity) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            [(product_id, size, 0 if size == "XL" else 20)
             for product_id, _, _, _, sized in products if sized for size in SIZES],
        )
