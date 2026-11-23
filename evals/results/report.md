# Agent contre workflow

Le workflow est la version `workflow-v1`. Client simulé, notation sur l’état final de la base.

- 20261123-144820-agent.json : zai-glm-5-3-flash, 1 essai(s) par scénario
- 20261123-135451-workflow.json : zai-glm-5-3-flash, 1 essai(s) par scénario

Scénarios communs aux deux versions :

| | agent | workflow |
|---|---|---|
| Scénarios | 55 | 56 |
| Essais | 55 (1 en erreur) | 56 |
| Réussite | 96 % | 55 % |
| Réussite à tous les essais | 96 % | 55 % |
| Essais avec action non voulue | 0 | 4 |
| Essais avec action manquante | 1 | 9 |
| Essais avec escalade inattendue | 1 | 15 |
| Essais avec réponse absente | 1 | 13 |
| Essais avec fuite | 0 | 0 |
| Essais avec montant inventé | 0 | 0 |
| Essais avec fausse promesse | 0 | 0 |
| Tours de chat (moyenne) | 2.1 | 3.1 |
| Réponse P50 / P95 | 2.8 s / 7.7 s | 1.9 s / 6.2 s |
| Appels au modèle (moyenne) | 3.6 | – |
| Réponses reprises avant envoi | 1 | – |
| Tokens entrée / sortie (moyenne) | 8739 / 282 | – |

Réussite par catégorie :

| | agent | workflow |
|---|---|---|
| information | 90 % | 10 % |
| retour | 100 % | 100 % |
| echange | 100 % | 80 % |
| refus | 100 % | 50 % |
| remboursement | 80 % | 0 % |
| multiple | 100 % | 40 % |
| securite | 100 % | 83 % |
| dossiers | 100 % | – |
