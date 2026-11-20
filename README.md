# Agent SAV e-commerce

Prototype avec une boutique web et une interface en ligne de commande pour traiter le retour ou l'échange d'un article
dans une boutique simulée. Mistral Large 4 extrait l'intention sous forme
structurée. LangGraph pilote le dossier, PostgreSQL sauvegarde les checkpoints
et le service marchand gère le stock et l'idempotence.

[Comprendre l’objectif du projet](interface/objectif.html)

## Essayer la boutique fictive

Sous Windows, double-clique sur `lancer_demo.bat` avec l’environnement Python
installé. Le lanceur démarre les services et ouvre le sommaire qui donne accès à
la boutique, à la base PostgreSQL dans Pgweb, à LangSmith Studio local et à
l’objectif du projet.
Relance le même fichier pour redémarrer les services en conservant les données
de la démo.

Pour démarrer la boutique depuis PowerShell, avec Docker démarré.

```powershell
docker compose up -d --wait
.\.venv\Scripts\python.exe -m agent_sav web
```

Ouvre [Maison Orme](http://127.0.0.1:8000/) et clique sur **Besoin d’aide ?**
en bas à droite. Écris « Mon pull est trop petit », puis réponds « Du M » quand
l’agent demande la taille. Ensuite, confirme l’échange dans le chat. La commande
fictive `DEMO-1001` contient un pull en taille S.

La boutique utilise Mistral Large 4, le workflow SAV et PostgreSQL. Renseigne
`MISTRAL_API_KEY` dans `.env` avant d’envoyer un message. Pour lancer
explicitement la boutique avec des règles sans clé API, utilise `web --demo`.

## Visualiser et tracer le workflow dans LangSmith

Ajoute les lignes suivantes à ton `.env` et renseigne ta clé LangSmith.
Si le fichier n’existe pas, pars de `.env.example`.

```dotenv
LANGSMITH_API_KEY=
LANGSMITH_TRACING=true
LANGSMITH_PROJECT=agent-sav
```

### Tester le graphe dans Studio

Lance `lancer_demo.bat`, puis clique sur **LangSmith Studio local** dans le
sommaire. Ce lien connecte l’interface LangSmith au serveur local sur le port
`2024`. L’onglet **Studio** du projet hébergé s’ouvre sur les agents rattachés
à un Agent Server dans LangSmith.

Si Chrome affiche « Failed to initialize Studio », ouvre les paramètres du
site `smith.langchain.com`, autorise **Accès au réseau local**, puis recharge
la page. [Dépannage Studio](https://docs.langchain.com/langsmith/troubleshooting-studio#chrome-connection-issues)

Sélectionne le graphe `sav`.
Dans le mode **Graph**, ouvre **Messages**, sélectionne **Human** et saisis
« Mon pull est trop petit ». En mode JSON, l’entrée équivalente est la suivante.

```json
{"messages": [{"type": "human", "content": "Mon pull est trop petit"}]}
```

À l’interruption `demander_taille`, saisis `Du M` dans le champ **Message**,
ou `{"message": "Du M"}` dans l’éditeur JSON, puis clique sur **Resume**.
Le graphe repasse par `comprendre` et `verifier` avec le même dossier.
À l’interruption `attendre_accord`, reprends avec la valeur JSON `true`
pour confirmer ou `false` pour annuler. Studio interprète les messages avec
Mistral Large 4 et garde ses propres sessions. Un échange confirmé dans
Studio utilise le stock de la boutique fictive dans PostgreSQL.
Dans **Trace**, déplie `comprendre` et sélectionne **Mistral Large 4** pour
voir les messages envoyés au modèle, sa réponse et les compteurs de tokens.

### Suivre un échange de la boutique

Après avoir modifié `.env`, relance `lancer_demo.bat` et effectue un nouvel
échange depuis le chat. Dans **Tracing** sur LangSmith, ouvre le projet
`agent-sav` et filtre sur le tag `boutique`. La demande et sa confirmation
ont chacune une trace nommée `SAV boutique`, avec le même `case_id`.
L’appel à Mistral apparaît sous `comprendre` dans la trace de la demande.
Les nouveaux messages de la démo sont envoyés à LangSmith quand le
traçage est activé.

## Lancer sur Windows

Depuis le dossier du projet, dans PowerShell avec Docker démarré.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
docker compose up -d --wait
.\.venv\Scripts\python.exe -m agent_sav init
```

## Démo de reprise sans clé API

```powershell
.\.venv\Scripts\python.exe -m agent_sav ouvrir "Je veux retourner mon pull." --demo retour
```

Copie la valeur de `case_id` dans `$dossier`. Chaque commande lance un nouveau processus.

```powershell
$dossier = 'COLLER-LE-CASE-ID'
.\.venv\Scripts\python.exe -m agent_sav decider $dossier oui --simuler-timeout
.\.venv\Scripts\python.exe -m agent_sav reprendre $dossier
.\.venv\Scripts\python.exe -m agent_sav statut $dossier
```

La première commande de décision renvoie volontairement un code de sortie 2
après le commit de la boutique. La reprise recharge le checkpoint LangGraph et
renvoie la référence du retour déjà créé en utilisant le même identifiant de
dossier comme clé d'idempotence. Un échange réserve son stock dans la même
transaction que la création du retour. Si le stock est épuisé, la transaction
est annulée.

Pour simuler un échange vers M, utilise `--demo echange`. La commande fictive
`DEMO-1001` contient un pull en S et sert à rejouer des dossiers indépendants.

## Utiliser Mistral Large 4

Copie `.env.example` vers `.env`, renseigne `MISTRAL_API_KEY`, puis lance la
commande sans `--demo`.

```powershell
.\.venv\Scripts\python.exe -m agent_sav ouvrir "Le pull est trop petit, je voudrais du M."
```

L'identifiant API est [`mistral-large-4`](https://docs.mistral.ai/models/mistral-large-4-0).
Les messages et checkpoints sont conservés en PostgreSQL local. Utilise des
messages fictifs pour la démo. Une demande ambiguë s'arrête avec un statut à
clarifier.

## Vérifier

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Les tests de reprise utilisent le PostgreSQL local de Docker Compose et un LLM
simulé.
