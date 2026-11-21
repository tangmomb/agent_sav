# Agent SAV e-commerce

Le prototype traite les demandes de retour et d’échange après livraison. Mistral interprète les messages du client, LangGraph pilote le dossier et PostgreSQL conserve ses checkpoints, les demandes, le stock et les mails de démonstration.

Le [schéma Excalidraw](schema_langgraph/livree_uniquement.excalidraw) décrit le parcours. Le [diagramme Mermaid](pipeline_ecommerce.mmd) reprend ses transitions.
Dans Studio, les reformulations restent dans le bloc de la question en cours. Les ruptures passent par `check_stock`, qui réserve la variante disponible avant l’enregistrement de la demande.

## Essayer le parcours dans la boutique

Avec Docker et l’environnement Python installés, double-clique sur `lancer_demo.bat` ou lance ces commandes dans PowerShell.

```powershell
docker compose up -d --wait
.\.venv\Scripts\python.exe -m agent_sav web --demo
```

1. Ouvre [Mes achats](http://127.0.0.1:8000/mes-achats). Choisis Camille Moreau et contacte le SAV de la commande livrée `DEMO-1001`.
2. Choisis **Échanger un article**, puis le pull. Choisis **Taille M** ou un autre coloris. La boutique reçoit la demande après vérification et réservation du stock. Essaie **Taille XL** dans le coloris d’origine pour voir la proposition de retour en cas de rupture.
3. Ouvre **Espace boutique**. Valide la demande ou refuse-la en indiquant un motif.
4. Ouvre **Boîte mail de démonstration**, choisis le compte Camille Moreau et clique sur **Ouvrir la boîte mail**. Après validation, le mail contient deux liens signés. Confirme pour recevoir le récapitulatif ou refuse pour annuler la demande. Un refus de la boutique donne lieu à un mail avec le motif.

Le sommaire de `lancer_demo.bat` donne accès à la boîte mail et au back-office boutique.

La boîte mail locale simule les e-mails du projet. Les données, les interfaces de démo et les liens sont servis sur la machine locale. Un refus libère le stock réservé pour l’échange. La confirmation crée le retour définitif et le mail récapitulatif dans la même transaction.

Pour interpréter les messages avec Mistral Large 4, renseigne `MISTRAL_API_KEY` dans `.env` et lance `web` sans `--demo`.

## Tester le graphe dans LangSmith Studio

Depuis le sommaire ouvert par `lancer_demo.bat`, clique sur **LangSmith Studio local** et sélectionne le graphe `sav`. En mode **Graph**, lance par exemple cette entrée.

```json
{"messages": [{"type": "human", "content": "Je veux échanger un article."}]}
```

Renseigne le contexte d’exécution.

```json
{
  "customer_id": "CLI-001",
  "first_name": "Camille",
  "order": {"id": "DEMO-1001", "status": "livree"},
  "items": [
    {"line_number": 1, "name": "Pull en laine côtelée", "color": "Vert mousse", "size": "S"},
    {"line_number": 2, "name": "T-shirt en coton épais", "color": "Écru", "size": "XL"},
    {"line_number": 3, "name": "Jupe en denim", "color": "Bleu brut", "size": "M"}
  ]
}
```

Reprends avec `{"line_number": 1}` pour choisir le pull, puis `{"message": "Du M"}` pour préciser la variante. Les deux décisions métier se reprennent dans Studio avec `{"approved": true}` puis `true`. Pour les refus, utilise `{"approved": false, "reason": "Motif du refus"}` côté boutique et `false` côté client. Studio conserve ses propres checkpoints et écrit les demandes et les mails dans la base de démonstration.

Pour suivre les exécutions, renseigne `LANGSMITH_API_KEY`, `LANGSMITH_TRACING=true` et `LANGSMITH_PROJECT=agent-sav` dans `.env`. Le chat et les décisions de l’interface boutique utilisent le tag `boutique` et reprennent le même `thread_id`.

## Démontrer la reprise après un commit

```powershell
.\.venv\Scripts\python.exe -m agent_sav init
.\.venv\Scripts\python.exe -m agent_sav ouvrir "Je souhaite retourner un article." --ligne 1 --demo retour
$dossier = 'COLLER-LE-THREAD-ID'
.\.venv\Scripts\python.exe -m agent_sav decider $dossier oui
$lien = 'COLLER-LE-LIEN-confirmer-DE-liens_mail'
.\.venv\Scripts\python.exe -m agent_sav repondre $lien --simuler-timeout
.\.venv\Scripts\python.exe -m agent_sav reprendre $dossier
```

`repondre` s’arrête volontairement après le commit qui crée le retour et son récapitulatif. `reprendre` renvoie la même référence sans produire un deuxième retour. `decider $dossier non --motif "..."` simule un refus de la boutique. Pour sélectionner l’article après l’ouverture, omets `--ligne` et utilise `article $dossier 1`.

## Installation et tests

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
docker compose up -d --wait
.\.venv\Scripts\python.exe -m pytest -q
```

Les tests créent des bases PostgreSQL isolées et simulent Mistral. Avec `SAV_TEST_DATABASE_URL`, le rôle doit pouvoir créer des bases. Dans la démo, le schéma `shop` contient les données métier et le schéma `langgraph` contient les checkpoints. Démarre un nouveau dossier pour essayer le nouveau graphe.
