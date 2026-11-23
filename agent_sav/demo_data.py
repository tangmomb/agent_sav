from datetime import date, timedelta

DEMO_CUSTOMER = "CLI-001"
DEMO_ORDER = "DEMO-1001"
DEMO_PRODUCT = "ART-001"
SIZES = ("S", "M", "L", "XL")

# Prénom, nom.
CUSTOMERS = (
    ("Camille", "Moreau"), ("Noah", "Laurent"), ("Inès", "Martin"), ("Jules", "Bernard"),
    ("Lina", "Petit"), ("Adam", "Robert"), ("Zoé", "Richard"), ("Gabriel", "Durand"),
    ("Léa", "Dubois"), ("Hugo", "Simon"), ("Maya", "Michel"), ("Louis", "Leroy"),
    ("Sarah", "Roux"), ("Arthur", "David"), ("Jade", "Mercier"), ("Émile", "Garnier"),
    ("Nina", "Fontaine"), ("Oscar", "Chevalier"), ("Anna", "Girard"), ("Sacha", "André"),
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


def purchased_item_id(order_id: str, position: int) -> str:
    return f"ITEM-{order_id}-{position}"


def seed_demo(conn) -> None:
    customers = [(f"CLI-{index:03}", *customer) for index, customer in enumerate(CUSTOMERS, 1)]
    # One id per product color: ART-001 to ART-020 in their catalog color, ART-021 to ART-040
    # are the same products in one other color.
    products = [(f"ART-{index:03}", *product) for index, product in enumerate(PRODUCTS, 1)]
    products += [
        (f"ART-{index + len(PRODUCTS):03}", name, "Blanc" if color == "Noir" else "Noir", price_cents, sized)
        for index, (name, color, price_cents, sized) in enumerate(PRODUCTS, 1)
    ]
    orders = []
    items = []
    today = date.today()
    for index, (customer_id, _, _) in enumerate(customers):
        order_count = 2 + index % 4
        has_pending_delivery = index % 4 == 2
        for position in range(order_count + int(has_pending_delivery)):
            order_id = f"DEMO-{1001 + index * 5 + position}"
            ordered_on = today - timedelta(days=2 + (index % 4) + position * 21)
            status = "delivered"
            if has_pending_delivery and position == 0:
                status = "in_transit"
            elif position == order_count:
                ordered_on = today
                status = "confirmed"
            orders.append((order_id, customer_id, ordered_on, status))
            pattern = (index + position) % 8
            offsets = (0, 3, 9) if pattern == 0 else (0, 3) if pattern == 4 else (0,)
            for position_in_order, offset in enumerate(offsets, 1):
                product_id, _, _, _, sized = products[(index + position * 7 + offset) % len(PRODUCTS)]
                size = SIZES[(index + position + offset) % len(SIZES)] if sized else None
                items.append((order_id, purchased_item_id(order_id, position_in_order), product_id, size))
        if index % 4 == 0:  # a parcel that never arrived: a scarf, or a padded jacket
            order_id = f"DEMO-{1005 + index * 5}"
            product_id, _, _, _, sized = products[12 if index % 8 else 15]
            orders.append((order_id, customer_id, today - timedelta(days=16), "in_transit"))
            items.append((order_id, purchased_item_id(order_id, 1), product_id, "M" if sized else None))

    with conn.cursor() as cursor:
        cursor.executemany("INSERT INTO shop.customers (id, first_name, name) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", customers)
        cursor.executemany(
            "INSERT INTO shop.products (id, name, color, price_cents) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            [(product_id, name, color, price_cents) for product_id, name, color, price_cents, _ in products],
        )
        cursor.executemany(
            """INSERT INTO shop.orders (id, customer_id, ordered_on, status)
               VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
            orders,
        )
        cursor.execute("""UPDATE shop.orders SET delivered_on = ordered_on + 2
                          WHERE status = 'delivered' AND delivered_on IS NULL""")
        cursor.executemany(
            """INSERT INTO shop.order_items (order_id, item_id, product_id, size)
               VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
            items,
        )
        # Catalog colors are stocked in every size except XL; the other colors have 10 of each.
        cursor.executemany(
            "INSERT INTO shop.stock (product_id, size, quantity) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            [(product_id, size, 0 if size == "XL" else 20)
             for product_id, _, _, _, sized in products[:len(PRODUCTS)] if sized for size in SIZES]
            + [(product_id, size, 10)
               for product_id, _, _, _, sized in products[len(PRODUCTS):] for size in (SIZES if sized else ("",))],
        )
