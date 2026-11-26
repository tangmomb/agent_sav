"""What the graph explorer cannot read in the code: sentences, and where to draw things.

A test checks that every node, exit, state key and key a node reads has its note, and that no note is left over.
{MAX_STEPS} in a note stands for the value of that constant in the module of the graph, or in policy.py.
"""

ROLES = {
    "conversation": {
        "agent": "Appelle le modèle avec les consignes, les derniers messages et les outils. Il répond, lit ou propose.",
        "tools": "Exécute les lectures. Les actions ne sont pas exécutées : elles deviennent des propositions.",
        "policy": "Applique les règles de la boutique à chaque proposition, sans modèle.",
        "confirm": "Montre un récapitulatif et attend l’accord du client. Seul un oui franc confirme.",
        "apply": "Réalise une seule action par passage : un dossier SAV créé ou abandonné. Le nœud est relancé tant qu’il reste des propositions validées par le client à traiter (une création de dossier qui vient d’un tool_call escalate du LLM n’a pas besoin de la validation du client).",
        "guard": "Vérifie sans modèle que la réponse ne cite que ce que le chat contient. Sinon, la retire.",
        "give_up": "Budget épuisé : transmet la demande à la boutique au lieu de tourner en rond.",
    },
    "case": {
        "intake": "Vérifie que le dossier existe en base.",
        "submit": "Soumet le dossier à la boutique.",
        "shop_review": "Attend la décision de la boutique, ou sa réponse à une question.",
        "approve": "Accepte la demande. Pour un retour ou un échange, envoie l’étiquette et fixe les échéances.",
        "await_parcel": "Attend le colis du client. Les échéances arrivent comme des actions.",
        "remind": "Rappelle au client que son colis est attendu.",
        "settle": "Rembourse, ou transforme l’article acheté en l’article échangé.",
        "reject": "Refuse la demande et rend la variante réservée.",
        "answer": "Envoie la réponse de la boutique par mail.",
        "cancel": "Annule le dossier à la demande du client et rend la variante réservée.",
        "expire": "Clôt le dossier : le colis n’est pas arrivé à temps.",
    },
}

# What a node does with each key it reads, its exits aside.
READS = {
    "conversation": {
        ("agent", "messages"): "Est envoyé au modèle : les {HISTORY} derniers messages au plus.",
        ("agent", "rejected"): "Est envoyé au modèle si guard a retiré une réponse : la dernière raison, ajoutée aux consignes.",
        ("agent", "steps"): "N’est pas envoyé au modèle. Compte les appels LLM, {MAX_STEPS} au maximum : repart à 0 à chaque message du client, augmente jusqu’à la prochaine sollicitation du client.",
        ("agent", "tokens"): "N’est pas envoyé au modèle. Compte les tokens, {MAX_TOKENS} au maximum : repart à 0 et augmente comme steps.",
        ("tools", "messages"): "Le dernier, pour les outils que le modèle appelle.",
        ("policy", "pending_actions"): "Les actions proposées par le LLM, à soumettre aux règles.",
        ("confirm", "pending_actions"): "Pour montrer le récapitulatif au client. S’il ne confirme pas, pour répondre au LLM, appel par appel, que rien n’a été enregistré.",
        ("apply", "pending_actions"): "La première, à réaliser. Les autres sont gardées pour les passages suivants.",
        ("apply", "reply"): "Le oui du client, à garder avec le premier résultat.",
        ("guard", "messages"): "Le dernier est la réponse à vérifier ; les autres, ce qui doit la fonder.",
        ("guard", "rejected"): "Seulement s’il retire la réponse : pour ajouter sa raison à la liste, vide la première fois.",
        ("give_up", "messages"): "Le dernier message du client, à transmettre à la boutique.",
    },
    "case": {
        ("intake", "case_id"): "Pour chercher le dossier en base.",
        ("submit", "case_id"): "Le dossier à faire avancer.",
        ("shop_review", "case_id"): "Pour relire le dossier : une question n’attend pas les mêmes actions qu’une demande.",
        ("approve", "case_id"): "Pour relire le dossier : un remboursement n’attend pas de colis.",
        ("remind", "case_id"): "Pour relire le dossier à rappeler au client.",
        ("settle", "case_id"): "Pour relire le dossier : un échange ou un remboursement, et son montant.",
        ("reject", "case_id"): "Pour relire le dossier à refuser.",
        ("reject", "command"): "Le motif donné par la boutique.",
        ("answer", "case_id"): "Pour relire le dossier à clore.",
        ("answer", "command"): "Le texte de la réponse.",
        ("cancel", "case_id"): "Pour relire le dossier à annuler.",
        ("expire", "case_id"): "Pour relire le dossier à clore.",
    },
}

# What the value a node gives a key does not say by itself. Only where it helps.
PRODUCED = {
    "conversation": {
        ("policy", "pending_actions"): "summary est ajouté par policy, sans LLM, à partir de la base et des règles. call et args viennent de tools.",
        ("confirm", "steps"): "Remis à 0 pour traiter la suite.",
        ("confirm", "tokens"): "Remis à 0 pour traiter la suite.",
        ("confirm", "rejected"): "Vidé pour traiter la suite.",
        ("confirm", "pending_actions"): "Vidé si le client ne confirme pas.",
    },
    "case": {},
}

# What a statement is for: the node (None for any), the function, the operation, a part of the SQL. The first that fits.
WHY = [
    # tools
    (None, "Shop.orders", "SELECT", "shop.customers", "Vérifier que le client existe avant de chercher ses commandes."),
    (None, "Shop.orders", "SELECT", "shop.orders", "Donner au LLM les commandes du client et leurs articles, avec les item_id dont il a besoin pour proposer une action."),
    ("tools", "Cases.item", "SELECT", "", "Retrouver l’article dont le LLM veut connaître le stock, et vérifier qu’il appartient au client."),
    ("tools", "Cases.variants", "SELECT", "", "Donner au LLM les tailles et couleurs de l’article, et celles qui restent en stock, pour proposer un échange possible."),
    (None, "Cases.of_customer", "SELECT", "", "Donner au LLM les 10 derniers dossiers du client et leur statut, pour lui répondre ou en annuler un."),

    # The rules: policy, and again just before writing.
    ("give_up", "Cases.check", "SELECT", "route = 'auto'", "Lecture commune à toutes les actions. Sans effet ici : give_up n’ouvre que des questions, jamais acceptées automatiquement."),
    (None, "Cases.check", "SELECT", "route = 'auto'", "Compter les demandes récemment acceptées automatiquement : à partir de {AUTO_LIMIT} en {AUTO_LIMIT_DAYS} jours, la boutique doit valider les suivantes."),
    ("give_up", "Cases.check", "SELECT", "kind = 'question'", "Compter les questions du client en attente : à partir de {MAX_OPEN_QUESTIONS}, aucune nouvelle n’est transmise."),
    (None, "Cases.check", "SELECT", "kind = 'question'", "Pour escalate. Compter les questions du client en attente : à partir de {MAX_OPEN_QUESTIONS}, une nouvelle est refusée."),
    ("policy", "Cases.item", "SELECT", "", "Vérifier qu’un retour ou un échange est possible : article du client, livré, dans les délais, sans dossier en cours. Relu aussi pour écrire summary."),
    ("policy", "Cases.variants", "SELECT", "", "Vérifier que la taille et la couleur demandées pour l’échange existent et sont en stock."),
    ("policy", "Cases.order", "SELECT", "", "Vérifier qu’un remboursement est possible : commande du client, statut, total (qui fixe le montant), pas déjà remboursée pour ce motif."),
    ("policy", "Cases.get", "SELECT", "", "Vérifier que le dossier à annuler appartient au client et n’est pas clos. Relu aussi pour écrire summary."),
    ("apply", "Cases.item", "SELECT", "", "Revérifier le retour ou l’échange juste avant d’écrire : la situation a pu changer depuis policy, pendant que le client réfléchissait."),
    ("apply", "Cases.variants", "SELECT", "", "Revérifier que la taille et la couleur de l’échange sont encore en stock."),
    ("apply", "Cases.order", "SELECT", "", "Revérifier le remboursement juste avant d’écrire, et recalculer son montant."),
    ("apply", "Cases.get", "SELECT", "", "Jamais exécutée ici : une annulation ne passe pas par Cases.open. La requête apparaît parce que la fonction des règles est partagée avec policy."),
    ("give_up", "Cases.item", "SELECT", "", "Jamais exécutée ici : give_up n’ouvre que des questions. La requête apparaît parce que Cases.open est partagée avec apply."),

    # Opening a case.
    (None, "Cases.open", "SELECT", "shop.customers", "Verrouiller le client : deux demandes envoyées en même temps passent l’une après l’autre, sans contourner les limites."),
    (None, "Cases.open", "SELECT", "request_key", "Retrouver le dossier si cette demande l’a déjà créé, pour ne pas en ouvrir un deuxième quand le nœud est rejoué."),
    ("apply", "Cases.open", "INSERT", "", "Créer le dossier SAV : qui, quoi, quel montant, et s’il est accepté tout de suite ou soumis à la boutique. Le graphe dossier prend la suite."),
    ("give_up", "Cases.open", "INSERT", "", "Créer un dossier de type question, pour que la boutique réponde au client par mail."),
    ("apply", "Shop.move_stock", "UPDATE", "", "Réserver un exemplaire de la nouvelle taille ou couleur pour l’échange. Si le stock est épuisé, l’échange est refusé."),
    (None, "Cases.add_action", "INSERT", "", "Déposer l’action d’annulation, que le graphe dossier va traiter. La clé évite de la déposer deux fois."),
    (None, "Cases.add_action", "SELECT", "", "Retrouver le numéro de cette action, même si elle avait déjà été déposée."),

    # The case graph.
    ("intake", "Cases.get", "SELECT", "", "Lire le dossier : vérifier qu’il existe, puis choisir la sortie selon qu’il est accepté tout de suite ou soumis à la boutique."),
    ("shop_review", "Cases.get", "SELECT", "", "Savoir si c’est une question ou une demande : la boutique n’a pas les mêmes choix."),
    ("approve", "Cases.get", "SELECT", "", "Lire le dossier : un remboursement n’attend pas de colis, un retour ou un échange si. Sert aussi à écrire le mail."),
    (None, "Cases.get", "SELECT", "", "Lire le dossier, pour savoir ce qu’il y a à faire et écrire le mail."),
    (None, "Cases.item", "SELECT", "", "Retrouver le nom de l’article, pour l’écrire dans le mail au client."),
    (None, "Cases.advance", "SELECT", "", "Relire le dossier en le verrouillant : s’il a déjà changé de statut, le nœud rejoué ne refait rien."),
    ("submit", "Cases.advance", "UPDATE", "", "Passer le dossier en attente de la boutique."),
    ("approve", "Cases.advance", "UPDATE", "", "Passer le dossier en attente du colis."),
    ("settle", "Cases.advance", "UPDATE", "", "Clore le dossier comme réussi : colis reçu, ou remboursement fait."),
    ("reject", "Cases.advance", "UPDATE", "", "Clore le dossier comme refusé par la boutique."),
    ("answer", "Cases.advance", "UPDATE", "", "Clore la question : la boutique a répondu."),
    ("cancel", "Cases.advance", "UPDATE", "", "Clore le dossier comme annulé par le client."),
    ("expire", "Cases.advance", "UPDATE", "", "Clore le dossier comme expiré : le colis n’est pas arrivé à temps."),
    (None, "Cases.wait_for_parcel", "UPDATE", "", "Fixer les échéances du colis : rappel après {PARCEL_REMINDER_DAYS} jours, dossier clos après {PARCEL_EXPIRY_DAYS} jours."),
    (None, "Cases.remind", "UPDATE", "", "Noter que le rappel est fait, pour ne pas l’envoyer deux fois."),
    (None, "Cases.send_mail", "INSERT", "", "Enregistrer le mail destiné au client. La clé évite d’écrire deux fois le même."),
    (None, "Cases.settle", "SELECT", "", "Retrouver l’article renvoyé, pour le remettre en stock."),
    ("settle", "Shop.move_stock", "UPDATE", "", "Remettre l’article renvoyé en stock, sauf s’il est abîmé."),
    (None, "Cases.settle", "UPDATE", "", "Remplacer l’article de la commande par celui de l’échange : c’est celui que le client a maintenant."),
    (None, "Cases.settle", "INSERT", "", "Enregistrer le remboursement."),
    (None, "Cases.release_stock", "SELECT", "", "Retrouver le produit d’un échange abandonné, pour libérer l’exemplaire réservé."),
    (None, "Shop.move_stock", "UPDATE", "", "Remettre en stock l’exemplaire réservé pour l’échange, puisqu’il n’aura pas lieu."),
]

# When a node does not always produce the same keys: its pause if it has one, then each of its returns, in order.
SITUATIONS = {
    "conversation": {
        "guard": ["Si la réponse est fondée", "Si la réponse est retirée"],
        "confirm": ["1. Pause : ce que le client voit", "2. S’il dit oui", "2. Sinon (exemple d’un autre échange, le client change d’avis et ne valide pas la demande)"],
    },
    "case": {
        "shop_review": ["1. Pause : ce que le nœud attend", "2. À la reprise : l’action reçue"],
        "await_parcel": ["1. Pause : ce que le nœud attend", "2. À la reprise : l’action reçue"],
    },
}

# The cases of a node that always returns the same keys, told apart by those that are not empty. The first one goes with
# the values shown as read.
CASES = {
    "conversation": {
        "tools": {("messages",): "Si le modèle lit", ("pending_actions",): "S’il propose des actions",
                  ("messages", "pending_actions"): "S’il fait les deux, ou si une action est mal appelée"},
        "policy": {("pending_actions",): "Si tout est autorisé", ("messages",): "Si tout est refusé",
                   ("messages", "pending_actions"): "Si une partie est refusée"},
        "apply": {("messages", "pending_actions"): "S’il reste des actions", ("messages",): "Si c’était la dernière"},
    },
    "case": {},
}

# A key a node leaves as it is in one of its situations, counted from 0. Only where one would expect it to change.
KEPT = {
    "conversation": {("confirm", 1, "pending_actions"): "Reste intacte : apply va réaliser ces actions."},
    "case": {},
}

KEYS = {
    "conversation": {
        "messages": "La mémoire du chat : messages du client, réponses de l’agent et résultats d’outils. Les {HISTORY} derniers sont envoyés au modèle.",
        "steps": "Empêche l’agent de tourner en rond : compte les appels au modèle pour ce message du client. Maximum : {MAX_STEPS} (MAX_STEPS), vérifié avant de rappeler le modèle.",
        "tokens": "Limite le coût : compte les tokens consommés pour ce message du client. Maximum : {MAX_TOKENS} (MAX_TOKENS), vérifié avant de rappeler le modèle.",
        "rejected": "Dit au modèle pourquoi guard a retiré sa réponse, pour qu’il la corrige, et compte les essais. Maximum : {MAX_REJECTED} (MAX_REJECTED).",
        "taken_back": "Garde la trace de toutes les réponses retirées, pour les montrer dans l’historique du dossier et les étapes de la réponse. Lu hors du graphe, par Support.timeline et Support.answer.",
        "pending_actions": "Les actions SAV proposées par le LLM, transmises de nœud en nœud jusqu’à être refusées, abandonnées ou réalisées. Maximum : {MAX_PENDING_ACTIONS} à la fois (MAX_PENDING_ACTIONS).",
        "reply": "La preuve de l’accord du client : son oui et le récapitulatif qu’il a vu, gardés avec le premier résultat.",
    },
    "case": {
        "case_id": "Le dossier que le graphe fait avancer. C’est sa seule entrée : tout le reste est relu en base.",
        "command": "L’action qui a repris la dernière pause (accepter, refuser, colis reçu…) : elle décide de la suite.",
        "last_command": "Le numéro de la dernière action reçue, pour ne jamais la traiter deux fois. Lu hors du graphe, par Support.run_case.",
    },
}

# When a node runs the statements it does not always run.
SQL = {
    "conversation": {
        "tools": "Exemple de requête SQL si le LLM demande à consulter ces tables (dépend de l’outil et des arguments du tool_call) :",
        "policy": "Exemple de requête SQL pour vérifier l’action proposée (dépend de l’action et de ses arguments) :",
        "apply": "Exemple de requête SQL pour réaliser l’action acceptée (dépend de l’action et de ses arguments) :",
        "give_up": "Exemple de requête SQL pour transmettre la demande à la boutique (si ce n’est pas déjà fait) :",
    },
    "case": dict.fromkeys(("submit", "approve", "remind", "settle", "reject", "answer", "cancel", "expire"),
                          "Exemple de requête SQL pour ce dossier (dépend de son type et de son statut) :"),
}

EXITS = {
    "conversation": {
        ("__start__", "agent"): "Nouveau message du client.",
        ("agent", "tools"): "Le modèle appelle au moins un outil.",
        ("agent", "guard"): "Le modèle répond par du texte, sans appel d’outil.",
        ("tools", "policy"): "Au moins une action a été proposée (pending_actions n’est pas vide).",
        ("tools", "agent"): "Seulement des lectures ou des erreurs, et il reste du budget.",
        ("tools", "give_up"): "Seulement des lectures ou des erreurs, et le budget est épuisé ({MAX_STEPS} appels au modèle ou {MAX_TOKENS} tokens).",
        ("policy", "confirm"): "Au moins une proposition autorisée demande l’accord du client.",
        ("policy", "apply"): "Seules des questions à la boutique sont autorisées : pas d’accord à demander.",
        ("policy", "agent"): "Tout est refusé, et il reste du budget.",
        ("policy", "give_up"): "Tout est refusé, et le budget est épuisé ({MAX_STEPS} appels au modèle ou {MAX_TOKENS} tokens).",
        ("confirm", "apply"): "Le client a dit oui sans rien ajouter.",
        ("confirm", "agent"): "Toute autre réponse : les propositions sont abandonnées.",
        ("apply", "apply"): "Il reste une proposition confirmée.",
        ("apply", "agent"): "Toutes les propositions sont traitées.",
        ("guard", "__end__"): "La réponse est fondée : elle part au client.",
        ("guard", "agent"): "Réponse retirée, première fois, et il reste du budget.",
        ("guard", "give_up"): "Réponse retirée {MAX_REJECTED} fois, ou budget épuisé ({MAX_STEPS} appels au modèle ou {MAX_TOKENS} tokens).",
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
    ("conversation", "apply", "case_actions", "case", "shop_review", "L’annulation du client reprend la pause."),
    ("conversation", "apply", "case_actions", "case", "await_parcel", "L’annulation du client reprend la pause."),
)

# How the database is organised: each schema, and what its tables are for.
SCHEMAS = (
    ("shop", "Les données de la boutique, qui existent sans l’agent : ce qu’elle vend, et ce que ses clients ont acheté. "
             "L’agent les lit, et n’y écrit que le stock et l’article échangé.", {
        "customers": "Les clients.",
        "products": "Le catalogue : une ligne par couleur d’un produit, avec son prix.",
        "stock": "Ce qu’il reste de chaque produit, taille par taille.",
        "orders": "Les commandes : leur client, leur statut, leurs dates de commande et de livraison.",
        "order_items": "Les articles achetés, un par ligne : quel produit, quelle taille, dans quelle commande.",
    }),
    ("sav", "Les données de l’agent, dont la boutique n’a pas besoin pour vendre : les dossiers que les deux graphes créent "
            "et font avancer, et les chats. Ses tables renvoient aux clients, commandes et articles de shop.", {
        "cases": "Un dossier par action confirmée par le client (retour, échange, remboursement) ou par question confiée à la boutique : son type, son statut, le client, l’article ou la commande, le montant, les échéances.",
        "case_actions": "Les actions demandées sur un dossier : décision de la boutique, colis reçu, annulation du client, échéance passée. Chacune reprend une pause du graphe dossier, ou est ignorée si le dossier n’attend pas cette action.",
        "case_messages": "Les mails d’un dossier, de l’agent ou de la boutique.",
        "refunds": "Les remboursements faits, un au plus par dossier.",
        "chat_owners": "Quel client a ouvert chaque chat. Aucun nœud ne la lit : le serveur s’en sert avant de lancer le graphe conversation. La conversation elle-même n’est pas ici : elle est gardée par le graphe, dans le schéma langgraph.",
    }),
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

# Personal notes, shown at the very bottom of a node: questions that unfold. A blank line separates blocks; lines with " | " make a table,
# whose first line is the heading; `code` goes between backticks.
PERSONAL = {
    "conversation": {
        "policy": {
            "Comment le nœud sait quel SQL exécuter ?": """
Toutes les requêtes SQL sont écrites à l’avance dans le code, et le nom de l’action décide lesquelles partent. C’est un simple if / elif dans `Cases.check`.

Action proposée | Ce que le code lit
Toutes | Le nombre de demandes récemment acceptées automatiquement
`propose_return` | L’article et sa commande
`propose_exchange` | L’article et sa commande, puis les tailles et couleurs en stock si l’article est bien au client
`propose_refund` | La commande, son total et ses remboursements déjà demandés
`cancel_case` | Le dossier à annuler
`escalate` | Le nombre de questions déjà en attente

Si l’action n’est pas refusée, policy relit aussi l’article (retour, échange) ou le dossier (annulation) pour écrire `summary`.

Les arguments du LLM ne font que remplir les `%s` de ces requêtes : il ne peut ni écrire du SQL, ni choisir une table. Le `customer_id` vient du serveur : s’il donne l’article ou la commande d’un autre client, rien n’est trouvé. Un dossier est relu, puis le code vérifie qu’il appartient au client.
""",
            "Qui produit le summary ?": """
Le code, dans le nœud policy, jamais le LLM. policy appelle `Cases.propose`, qui applique les règles, puis remplit une phrase toute faite dans `Cases.summary`.

Ligne de l’exemple | D’où elle vient
Échange : T-shirt en coton épais (Écru · XL, commande DEMO-1001) | L’article, relu en base à partir de l’`item_id`
Contre : Écru · M | La taille et la couleur demandées, vérifiées par les règles. Le LLM n’avait pas donné la couleur (`color: null`)
Acceptation : immédiate | Le verdict des règles : immédiate, ou soumise à la boutique

`call` et `args` viennent du LLM, mis en forme par tools. `reason` et `details` n’apparaissent pas dans la phrase : le client voit le contenu réel de l’action, pas un texte écrit par le LLM.

Pour `escalate`, `summary` est vide : il n’y a pas d’accord à demander.
""",
            "D’où viennent les délais et les règles ?": """
De `agent_sav/policy.py` : des constantes et des `if`, sans LLM. Le fichier `politique_sav.md`, lui, n’est lu que par le LLM.

Fichier | Pour qui | Rôle
`policy.py` | Le code : policy, apply et give_up | Décide : accepte, soumet à la boutique ou refuse
`politique_sav.md` | Le LLM, avec l’outil `read_policy` | Informe : permet de répondre au client

Constante | Valeur | Ce qu’elle fixe
`RETURN_DAYS` | {RETURN_DAYS} jours | Délai de retour ou d’échange, après la livraison
`LATE_DAYS` | {LATE_DAYS} jours | Jusque-là, la boutique décide. Ensuite, c’est refusé
`WARRANTY_DAYS` | {WARRANTY_DAYS} jours | Article abîmé ou erreur de préparation : la boutique décide
`LOST_AFTER_DAYS` | {LOST_AFTER_DAYS} jours | Un colis est considéré perdu, à partir de la commande
`GOODWILL_PERCENT` | {GOODWILL_PERCENT} % | Geste commercial, au plus, sur le total de la commande

Le texte du `.md` a des trous (`{return_days}`…), remplis avec ces mêmes constantes : le LLM lit les délais que le code applique. S’il se trompe, ça ne change rien à l’action : policy recalcule tout avec les dates de la base.
""",
            "C’est quoi « Acceptation : immédiate » ?": """
L’acceptation de la boutique, pas celle du client. Le récapitulatif annonce ce qui se passera une fois que le client aura dit oui : son accord est nécessaire dans les deux cas.

Récapitulatif | Après le oui du client
Acceptation : immédiate | Les règles l’acceptent toutes seules : l’étiquette part, ou le remboursement est fait
Acceptation : soumise à la boutique | Le dossier est créé, mais quelqu’un à la boutique doit l’accepter ou le refuser. Réponse par mail

Demande | Immédiate si… | Sinon
Retour | Livré depuis {RETURN_DAYS} jours au plus, article de {AUTO_RETURN_CENTS} au plus, moins de {AUTO_LIMIT} demandes déjà acceptées automatiquement en {AUTO_LIMIT_DAYS} jours | À la boutique, jusqu’à {LATE_DAYS} jours
Échange | Pareil, sans limite de prix | À la boutique, jusqu’à {LATE_DAYS} jours
Article abîmé ou erreur de préparation | Jamais | À la boutique, pendant {WARRANTY_DAYS} jours
Colis perdu | Commande de {AUTO_REFUND_CENTS} au plus, moins de {AUTO_LIMIT} demandes déjà acceptées automatiquement en {AUTO_LIMIT_DAYS} jours | À la boutique
Geste commercial | Jamais | À la boutique, jusqu’à {GOODWILL_PERCENT} % de la commande

Au-delà de ces limites, ou si c’est impossible (plus de stock, par exemple), policy refuse directement.
""",
        },
        "confirm": {
            "Qu’est-ce qui compte comme un oui ?": """
Une liste écrite à la main dans `agent_sav/confirmation.py` décide, sans LLM. La réponse du client doit passer quatre contrôles. Majuscules et accents ne comptent pas.

Contrôle | Détail
Commencer par un oui connu | oui, ouai(s), ouep, yes, yep, ok, oki, go, 👍, (je suis) d’accord, bien sûr, c’est bon / ça / correct / exact, tout est bon / correct, parfait, exact, (je) confirme, (je) valide, vas-y, allez-y
Ne contenir aucun doute | Un « ? », 👎, ou : mais, non, pas, plutôt, sauf, attendez, finalement, annuler, enfin, aussi, avant, si
Ne contenir ni taille ni nombre | S, M, L, XL, 20…
N’ajouter aucun mot nouveau | Les mots de 4 lettres ou plus sont des mots de politesse, ou figurent déjà dans le récapitulatif

Compte comme un oui | N’en est pas un
ok, d’accord, parfait !, vas-y | oui, mais en L
ouais merci | oui en noir
oui s’il vous plaît | oui ?
oui pour l’échange de la jupe | oui 20
je suis d’accord, bien sûr, go, oki, 👍 | 👍 mais en L
""",
        },
        "apply": {
            "C’est quoi un dossier abandonné ?": """
Un dossier que le client annule : il revient sur une demande SAV en cours, et le LLM appelle l’outil `cancel_case`. Ce n’est pas l’annulation d’une commande : celle-là, l’agent la transmet à la boutique avec `escalate`.

apply n’annule rien lui-même : il dépose une action `cancel` dans la table `case_actions`, et le nœud `cancel` du graphe dossier annule vraiment. Si le dossier a été clos entre-temps, l’action est ignorée.

Dossier | Annulable ?
Retour ou échange | Oui, même accepté, jusqu’à la réception du colis
Remboursement | Oui, tant que la boutique n’a pas tranché
Question à la boutique | Non : policy refuse, la boutique répondra par mail
""",
        },
        "give_up": {
            "Pourquoi une question part en give_up plutôt qu’en escalate ?": """
La différence est qui décide : `escalate` est un choix du LLM, give_up une décision du code, quand l’agent n’y arrive pas. Dans les deux cas, un dossier de type question est créé, et la boutique répond par mail.

 | escalate | give_up
Qui décide | Le LLM, en appelant l’outil | Le code
Quand, sans parler des plafonds d’appels et de tokens | Ni les outils ni la politique ne répondent, ou le client demande un humain | guard a retiré {MAX_REJECTED} réponses de suite
Chemin | tools, policy, apply, puis l’agent répond | guard, give_up, puis fin du tour
Ce que reçoit la boutique | La question, formulée par le LLM | Le dernier message du client, mot pour mot
Ce que lit le client | Une réponse écrite par le LLM | Une phrase fixe, écrite dans le code

guard retire une réponse dans trois cas, sans LLM (`agent_sav/grounding.py`) :

Cas | Exemple
Un montant absent des résultats d’outils et des messages du client | « Je vous rembourse 50 € »
Un pourcentage absent des mêmes sources | « Je vous offre 15 % »
Une action annoncée comme faite, alors que rien n’a été enregistré | « Votre retour est enregistré »

À la première réponse retirée, l’agent réessaie en sachant pourquoi.
""",
        },
    },
    "case": {},
}
