# Agent SAV e-commerce

Prototype en ligne de commande pour traiter le retour ou l'échange d'un article
dans une boutique simulée. Mistral Large 4 extrait l'intention sous forme
structurée. LangGraph pilote le dossier, PostgreSQL sauvegarde les checkpoints
et le service marchand gère le stock et l'idempotence.

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
clarifier. Ce socle utilise le terminal comme interface et la boutique simulée
comme service marchand.

## Vérifier

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Les tests de reprise utilisent le PostgreSQL local de Docker Compose et un LLM
simulé.
