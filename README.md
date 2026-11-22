# Agent SAV e-commerce

Le prototype répond aux questions de suivi et traite les demandes de retour et d’échange après livraison. Mistral extrait l’intention, la commande, l’article et la variante souhaitée à partir des messages du client. LangGraph pilote le dossier et PostgreSQL conserve ses checkpoints, les demandes, le stock et les mails de démonstration.

Le [schéma Excalidraw](schema_langgraph/livree_uniquement.excalidraw) décrit le parcours. Le [diagramme Mermaid](pipeline_ecommerce.mmd) reprend ses transitions.
Le nœud `collect` accepte une demande complète en un seul message ou cumule les réponses tour après tour. Une nouvelle intention remplace la précédente. Les lignes, tailles et coloris cités alimentent les candidats du dossier. Si plusieurs valeurs restent possibles, le chat demande au client d’en choisir une.

Pour un renseignement, le nœud `request_support_question` demande au client de formuler sa question, la transmet à l’espace boutique et termine le parcours. La boutique peut ensuite répondre dans la boîte mail de démonstration du client. Pour un échange ou un retour, `validate_intent` récapitule la demande. Le client peut répondre « Oui », apporter une correction ou annuler. Après accord, un retour passe directement à `register_request` et un échange passe à `check_stock`. En cas de rupture, `offer_return` demande au client s’il souhaite poursuivre avec un retour.

Dans `collect`, un seul appel Mistral par message reçoit l’échange et le state, puis renvoie les champs extraits et une éventuelle `next_question`. LangGraph contrôle si une précision manque avant d’afficher la question. Le mode `--demo` fonctionne sans clé API.

## Essayer le parcours dans la boutique

Avec Docker et l’environnement Python installés, double-clique sur `lancer_demo.bat` ou lance ces commandes dans PowerShell.

```powershell
docker compose up -d --wait
.\.venv\Scripts\python.exe -m agent_sav web --demo
```

1. Ouvre [Mes achats](http://127.0.0.1:8000/orders). Choisis Camille Moreau et ouvre **Service client** en bas à droite. Pour tester un renseignement, écris « J’ai une question », puis saisis la question quand le chat te le demande.
2. Écris « Je voudrais échanger un article ». Réponds aux questions du chat par exemple avec « La commande DEMO-1001 », « Le pull vert », puis « En taille M ». Réponds « Oui » au récapitulatif pour lancer la vérification et la réservation du stock. Avec « En taille XL » dans le coloris d’origine, le chat propose un retour auquel tu peux répondre « Oui » ou « Non ».
3. Ouvre **Espace boutique**. Valide la demande ou refuse-la en indiquant un motif.
4. Ouvre **Boîte mail de démonstration**, choisis le compte Camille Moreau et clique sur **Ouvrir la boîte mail**. Après validation, le mail contient deux liens signés. Confirme pour recevoir le récapitulatif ou refuse pour annuler la demande. Un refus de la boutique donne lieu à un mail avec le motif.

Le sommaire de `lancer_demo.bat` donne accès à la boîte mail et au back-office boutique.

La boîte mail locale simule les e-mails du projet. Les données, les interfaces de démo et les liens sont servis sur la machine locale. Un refus libère le stock réservé pour l’échange. La confirmation crée le retour définitif et le mail récapitulatif dans la même transaction.

Pour interpréter les messages avec Z.ai GLM 5.3 Flash, hébergé par Mistral, renseigne `MISTRAL_API_KEY` dans `.env` et lance `web` sans `--demo`.

## Tester le graphe dans LangSmith Studio

Depuis le sommaire ouvert par `lancer_demo.bat`, clique sur **LangSmith Studio local** et sélectionne le graphe `support`. En mode **Graph**, lance par exemple cette entrée.

```json
{"messages": [{"type": "human", "content": "Je veux échanger un article."}]}
```

Renseigne le contexte d’exécution.

```json
{
  "customer_id": "CLI-001",
  "first_name": "Camille",
  "order": {"id": "DEMO-1001", "status": "delivered"},
  "items": [
    {"line_number": 1, "name": "Pull en laine côtelée", "color": "Vert mousse", "size": "S"},
    {"line_number": 2, "name": "T-shirt en coton épais", "color": "Écru", "size": "XL"},
    {"line_number": 3, "name": "Jupe en denim", "color": "Bleu brut", "size": "M"}
  ]
}
```

Reprends avec `{"message": "Le pull vert"}` pour identifier l’article, puis `{"message": "Du M"}` pour préciser la variante. Dans `validate_intent`, réponds `{"message": "Oui"}` pour lancer la demande. Les deux décisions suivantes se reprennent dans Studio avec `{"approved": true}` puis `true`. Pour les refus, utilise `{"approved": false, "reason": "Motif du refus"}` côté boutique et `false` côté client. Studio conserve ses propres checkpoints et écrit les demandes et les mails dans la base de démonstration.

Pour suivre les exécutions, renseigne `LANGSMITH_API_KEY`, `LANGSMITH_TRACING=true` et `LANGSMITH_PROJECT=agent-sav` dans `.env`. Le chat et les décisions de l’interface boutique utilisent le tag `shop` et reprennent le même `thread_id`.

## Démontrer la reprise après un commit

```powershell
.\.venv\Scripts\python.exe -m agent_sav init
.\.venv\Scripts\python.exe -m agent_sav open "Je souhaite retourner un article." --line-number 1 --demo return
$threadId = 'COLLER-LE-THREAD-ID'
.\.venv\Scripts\python.exe -m agent_sav clarify $threadId Oui
.\.venv\Scripts\python.exe -m agent_sav decide $threadId yes
$link = 'COLLER-LE-LIEN-confirm-DE-email_links'
.\.venv\Scripts\python.exe -m agent_sav reply $link --simulate-timeout
.\.venv\Scripts\python.exe -m agent_sav resume $threadId
```

`reply` s’arrête volontairement après le commit qui crée le retour et son récapitulatif. `resume` renvoie la même référence sans produire un deuxième retour. `decide $threadId no --reason "..."` simule un refus de la boutique. Pour sélectionner l’article après l’ouverture, omets `--line-number` et utilise `item $threadId 1`.

## Installation et tests

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
docker compose up -d --wait
.\.venv\Scripts\python.exe -m pytest -q
```

Les tests créent des bases PostgreSQL isolées et simulent Mistral. Avec `SAV_TEST_DATABASE_URL`, le rôle doit pouvoir créer des bases. Dans la démo, le schéma `shop` contient les données métier et le schéma `langgraph` contient les checkpoints. Démarre un nouveau dossier pour essayer le nouveau graphe.
