#!/usr/bin/env bash
# Met en ligne un commit de main, sur le serveur. Lancé par GitHub Actions (.github/workflows/deploy.yml) avec une clé
# SSH qui ne peut rien faire d'autre : commande imposée dans authorized_keys, le commit est le seul argument.
# Si la nouvelle version ne se construit pas ou ne répond pas, l'ancienne est remise.
set -euo pipefail

compose() {
  docker compose -f deploy/compose.yaml --env-file deploy/.env "$@"
}

answers() {
  local port
  port=$(sed -n 's/^SAV_PORT=//p' deploy/.env)
  for _ in $(seq 1 30); do
    curl -fsS -m 5 -o /dev/null "http://127.0.0.1:${port:-8000}/" 2>/dev/null && return 0
    sleep 2
  done
  return 1
}

start() {
  compose build --quiet && compose up -d --no-build && answers
}

# Dans une fonction : bash l'a lue en entier quand git remplace ce fichier.
main() {
  local wanted=${SSH_ORIGINAL_COMMAND:-${1:-}} before
  [[ $wanted =~ ^[0-9a-f]{40}$ ]] || { echo "Commit attendu." >&2; exit 2; }
  cd "$(dirname "${BASH_SOURCE[0]}")/.."

  exec 9>.git/deploy.lock
  flock -w 900 9 || { echo "Un autre déploiement est en cours." >&2; exit 1; }

  git fetch -q origin main
  git merge-base --is-ancestor "$wanted" origin/main || { echo "Ce commit n'est pas sur main." >&2; exit 1; }
  git diff --quiet HEAD || { echo "Des fichiers ont été modifiés sur le serveur." >&2; exit 1; }
  before=$(git rev-parse HEAD)
  if [[ $wanted != "$before" ]] && git merge-base --is-ancestor "$wanted" "$before"; then
    echo "Une version plus récente est déjà en ligne."
    exit 0
  fi

  git merge -q --ff-only "$wanted"
  if start; then
    docker image prune -f --filter label=com.docker.compose.project=agent-sav-demo >/dev/null
    echo "En ligne : $(git log --oneline -1)"
    exit 0
  fi

  echo "La nouvelle version ne démarre pas, retour à ${before:0:7}." >&2
  compose logs --no-color --tail 5 web 2>/dev/null | grep -oE "[A-Za-z]+(Error|Exception)" | tail -3 >&2 || true
  git reset -q --keep "$before"
  start || echo "L'ancienne version ne répond pas non plus." >&2
  exit 1
}

main "$@"
