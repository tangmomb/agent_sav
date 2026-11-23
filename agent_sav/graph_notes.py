"""What the graph explorer cannot read in the code: sentences, and where to draw things.

A test checks that every node, exit and state key has its note, and that no note is left over.
"""

ROLES = {
    "conversation": {
        "agent": "Appelle le modèle avec les consignes, les derniers messages et les outils. Il répond, lit ou propose.",
        "tools": "Exécute les lectures. Les actions ne sont pas exécutées : elles deviennent des propositions.",
        "policy": "Applique les règles de la boutique à chaque proposition, sans modèle.",
        "confirm": "Montre un récapitulatif et attend l’accord du client. Seul un oui franc confirme.",
        "apply": "Réalise une proposition par passage. Rejouer le nœud retrouve ce qu’il a déjà écrit.",
        "guard": "Vérifie sans modèle que la réponse ne cite que ce que le chat contient. Sinon, la retire.",
        "give_up": "Budget épuisé : transmet la demande à la boutique au lieu de tourner en rond.",
    },
    "case": {
        "intake": "Vérifie que le dossier existe en base.",
        "submit": "Soumet le dossier à la boutique.",
        "shop_review": "Attend la décision de la boutique, ou sa réponse à une question.",
        "approve": "Accepte la demande. Pour un retour ou un échange, envoie l’étiquette et fixe les échéances.",
        "await_parcel": "Attend le colis du client. Les échéances arrivent comme des commandes.",
        "remind": "Rappelle au client que son colis est attendu.",
        "settle": "Rembourse, ou transforme l’article acheté en l’article échangé.",
        "reject": "Refuse la demande et rend la variante réservée.",
        "answer": "Envoie la réponse de la boutique par mail.",
        "cancel": "Annule le dossier à la demande du client et rend la variante réservée.",
        "expire": "Clôt le dossier : le colis n’est pas arrivé à temps.",
    },
}

KEYS = {
    "conversation": {
        "messages": "Le fil : client, agent et résultats d’outils.",
        "steps": "Appels au modèle pour ce message du client. Plafond : MAX_STEPS.",
        "tokens": "Tokens consommés pour ce message du client. Plafond : MAX_TOKENS.",
        "rejected": "Pourquoi guard a retiré les réponses à ce message.",
        "taken_back": "Toutes les réponses retirées. Lu hors du graphe, par Support.timeline.",
        "proposals": "Les actions en cours, de l’appel du modèle à leur résultat.",
        "reply": "Le oui du client, gardé jusqu’au premier résultat.",
    },
    "case": {
        "case_id": "Le dossier en base. C’est la seule entrée du graphe.",
        "command": "La commande qui a repris la dernière pause.",
        "last_command": "Son numéro : elle n’est jamais traitée deux fois. Lu hors du graphe, par Support.run_case.",
    },
}

EXITS = {
    "conversation": {
        ("__start__", "agent"): "Nouveau message du client.",
        ("agent", "tools"): "Le modèle appelle au moins un outil.",
        ("agent", "guard"): "Le modèle répond par du texte.",
        ("tools", "policy"): "Au moins une action a été proposée.",
        ("tools", "agent"): "Seulement des lectures ou des erreurs, et il reste du budget.",
        ("tools", "give_up"): "Seulement des lectures ou des erreurs, et le budget est épuisé.",
        ("policy", "confirm"): "Au moins une proposition autorisée demande l’accord du client.",
        ("policy", "apply"): "Seules des questions à la boutique sont autorisées : pas d’accord à demander.",
        ("policy", "agent"): "Tout est refusé, et il reste du budget.",
        ("policy", "give_up"): "Tout est refusé, et le budget est épuisé.",
        ("confirm", "apply"): "Le client a dit oui sans rien ajouter.",
        ("confirm", "agent"): "Toute autre réponse : les propositions sont abandonnées.",
        ("apply", "apply"): "Il reste une proposition confirmée.",
        ("apply", "agent"): "Toutes les propositions sont traitées.",
        ("guard", "__end__"): "La réponse est fondée : elle part au client.",
        ("guard", "agent"): "Réponse retirée, première fois, et il reste du budget.",
        ("guard", "give_up"): "Réponse retirée MAX_REJECTED fois, ou budget épuisé.",
        ("give_up", "__end__"): "Toujours.",
    },
    "case": {
        ("__start__", "intake"): "Le dossier vient d’être ouvert.",
        ("intake", "approve"): "Les règles l’ont accepté (route = auto).",
        ("intake", "submit"): "La boutique doit décider (route = shop).",
        ("submit", "shop_review"): "Toujours.",
        ("shop_review", "approve"): "Commande approve.",
        ("shop_review", "reject"): "Commande reject.",
        ("shop_review", "answer"): "Commande answer, pour une question.",
        ("shop_review", "cancel"): "Commande cancel, du client.",
        ("approve", "settle"): "Remboursement sans retour d’article.",
        ("approve", "await_parcel"): "Retour ou échange.",
        ("await_parcel", "settle"): "Commande received.",
        ("await_parcel", "remind"): "Commande remind, au bout de 7 jours.",
        ("await_parcel", "expire"): "Commande expire, au bout de 14 jours.",
        ("await_parcel", "cancel"): "Commande cancel, du client.",
        ("remind", "await_parcel"): "Toujours.",
        ("settle", "__end__"): "Toujours.",
        ("reject", "__end__"): "Toujours.",
        ("answer", "__end__"): "Toujours.",
        ("cancel", "__end__"): "Toujours.",
        ("expire", "__end__"): "Toujours.",
    },
}

# How the two graphs, which never call each other, meet in the database.
BRIDGES = (
    ("conversation", "apply", "cases", "case", "intake", "Le dossier ouvert fait démarrer son graphe (Support.run_cases)."),
    ("conversation", "give_up", "cases", "case", "intake", "La demande transmise ouvre un dossier de type question."),
    ("conversation", "apply", "case_commands", "case", "shop_review", "L’annulation du client reprend la pause."),
    ("conversation", "apply", "case_commands", "case", "await_parcel", "L’annulation du client reprend la pause."),
)

# Statements the analysis reaches but this node cannot run: the reason is in an argument it does not follow.
IMPOSSIBLE = {
    ("give_up", "UPDATE shop.stock"): "give_up n’ouvre que des questions, jamais un échange.",
}

# Where nodes are drawn: the center of each.
POSITIONS = {
    "conversation": {
        "__start__": (70, 190), "agent": (220, 190), "tools": (430, 190), "policy": (640, 190), "confirm": (850, 190),
        "apply": (1060, 190), "guard": (220, 330), "give_up": (640, 330), "__end__": (70, 330),
    },
    "case": {
        "__start__": (70, 220), "intake": (220, 220), "submit": (430, 300), "shop_review": (640, 300), "approve": (640, 140),
        "await_parcel": (850, 140), "remind": (850, 45), "settle": (1060, 140), "expire": (1060, 220), "cancel": (1060, 300),
        "reject": (1060, 380), "answer": (1060, 460), "__end__": (1205, 300),
    },
}

# How exits are drawn: side and offset where they leave, the corners, side and offset where they arrive.
ROUTES = {
    "conversation": {
        ("__start__", "agent"): ("r", 0, (), "l", 0),
        ("agent", "tools"): ("r", -12, (), "l", -12),
        ("tools", "agent"): ("l", 12, (), "r", 12),
        ("agent", "guard"): ("b", -18, (), "t", -18),
        ("guard", "agent"): ("t", 18, (), "b", 18),
        ("tools", "policy"): ("r", 0, (), "l", 0),
        ("tools", "give_up"): ("b", 0, ((430, 318),), "l", -12),
        ("policy", "give_up"): ("b", 0, (), "t", 0),
        ("policy", "confirm"): ("r", 0, (), "l", 0),
        ("policy", "apply"): ("b", 40, ((680, 262), (1040, 262)), "b", -20),
        ("policy", "agent"): ("t", 0, ((640, 122), (252, 122)), "t", 32),
        ("confirm", "apply"): ("r", 0, (), "l", 0),
        ("confirm", "agent"): ("t", 0, ((850, 88), (220, 88)), "t", 0),
        ("apply", "apply"): ("r", -14, ((1172, 176), (1172, 204)), "r", 14),
        ("apply", "agent"): ("t", 0, ((1060, 54), (188, 54)), "t", -32),
        ("guard", "__end__"): ("l", 0, (), "r", 0),
        ("guard", "give_up"): ("r", 12, (), "l", 12),
        ("give_up", "__end__"): ("b", 0, ((640, 402), (70, 402)), "b", 0),
    },
    "case": {
        ("__start__", "intake"): ("r", 0, (), "l", 0),
        ("intake", "approve"): ("t", 0, ((220, 140),), "l", 0),
        ("intake", "submit"): ("b", 0, ((220, 300),), "l", 0),
        ("submit", "shop_review"): ("r", 0, (), "l", 0),
        ("shop_review", "approve"): ("t", 0, (), "b", 0),
        ("shop_review", "reject"): ("b", 24, ((664, 380),), "l", 0),
        ("shop_review", "answer"): ("b", -24, ((616, 460),), "l", 0),
        ("shop_review", "cancel"): ("r", 0, (), "l", 0),
        ("approve", "settle"): ("b", 44, ((684, 192), (950, 192), (950, 154)), "l", 14),
        ("approve", "await_parcel"): ("r", 0, (), "l", 0),
        ("await_parcel", "settle"): ("r", -10, (), "l", -10),
        ("await_parcel", "remind"): ("t", -18, (), "b", -18),
        ("remind", "await_parcel"): ("b", 18, (), "t", 18),
        ("await_parcel", "expire"): ("b", 24, ((874, 220),), "l", 0),
        ("await_parcel", "cancel"): ("b", -24, ((826, 284),), "l", -16),
        ("settle", "__end__"): ("r", 0, ((1213, 140),), "t", 8),
        ("expire", "__end__"): ("r", 0, ((1197, 220),), "t", -8),
        ("cancel", "__end__"): ("r", 0, (), "l", 0),
        ("reject", "__end__"): ("r", 0, ((1197, 380),), "b", -8),
        ("answer", "__end__"): ("r", 0, ((1213, 460),), "b", 8),
    },
}
