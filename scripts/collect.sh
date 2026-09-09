#!/usr/bin/env bash
#
# Collecte de fond : enregistre les prédictions à intervalle régulier pour
# pouvoir mesurer, après coup, la couverture temps réel et la stabilité des
# annonces. Tout est écrit dans la base SQLite du site — aucun journal texte à
# trier ensuite.
#
# Usage :
#   ./scripts/collect.sh                 # toutes les 3 min, sans fin (Ctrl-C pour arrêter)
#   ./scripts/collect.sh 120 240         # toutes les 2 min, pendant 240 itérations
#
set -euo pipefail

INTERVAL="${1:-180}"
ITERATIONS="${2:-0}"          # 0 = sans fin
CONFIG="${CONFIG:-config/toulouse-guilhemery.toml}"

cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source .venv/bin/activate

printf '\033[1m▸ Collecte toutes les %s s' "$INTERVAL"
[ "$ITERATIONS" -gt 0 ] && printf ', %s itérations' "$ITERATIONS"
printf '\033[0m\n  base : %s\n  Ctrl-C pour arrêter.\n\n' "$(grep -m1 database "$CONFIG" || echo 'par défaut')"

count=0
while [ "$ITERATIONS" -eq 0 ] || [ "$count" -lt "$ITERATIONS" ]; do
    if nexttraintosee -c "$CONFIG" next --horizon 120 --limit 300 --record >/dev/null 2>&1; then
        printf '\r  %s · %d relevés' "$(date +%H:%M:%S)" "$((count + 1))"
    else
        printf '\n  %s · échec du relevé (réseau ?), on continue\n' "$(date +%H:%M:%S)"
    fi
    count=$((count + 1))
    [ "$ITERATIONS" -ne 0 ] && [ "$count" -ge "$ITERATIONS" ] && break
    sleep "$INTERVAL"
done

printf '\n\n%d relevés enregistrés. Pour l'"'"'analyse :\n' "$count"
printf '  nexttraintosee -c %s coverage --days 7\n' "$CONFIG"
