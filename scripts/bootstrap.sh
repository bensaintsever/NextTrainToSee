#!/usr/bin/env bash
#
# Amorçage complet : environnement virtuel, dépendances, horaires SNCF,
# puis résolution de la géométrie des voies autour du point d'observation.
#
# Usage :
#   ./scripts/bootstrap.sh                       # tout, avec la config par défaut
#   ./scripts/bootstrap.sh config/autre.toml     # avec une autre config
#
set -euo pipefail

CONFIG="${1:-config/toulouse-guilhemery.toml}"
GTFS_URL="https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip"
GTFS_PATH="data/sncf-gtfs.zip"
VENV=".venv"
# iCloud synchronise ~/Documents et ~/Desktop quand « Bureau et Documents » est
# activé. Un environnement virtuel qui s'y trouve est réécrit dans le dos de
# pip : fichiers dupliqués en « nom 2.ext », marqués « hidden », parfois évincés
# du disque. macOS exclut de la synchronisation tout dossier suffixé .nosync :
# on y place le venv et on laisse un lien symbolique .venv, de sorte que les
# commandes habituelles continuent de fonctionner.
NOSYNC_VENV=".venv.nosync"

cd "$(dirname "$0")/.."

say()  { printf '\n\033[1m▸ %s\033[0m\n' "$1"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$1"; }

is_icloud_synced() {
    [ "$(uname)" = "Darwin" ] || return 1
    [ -d "$HOME/Library/Mobile Documents/com~apple~CloudDocs" ] || return 1
    case "$(pwd -P)/" in
        "$HOME/Documents/"*|"$HOME/Desktop/"*) return 0 ;;
        *) return 1 ;;
    esac
}

# Supprime les copies de conflit iCloud (« fichier 2.ext ») d'un dossier.
purge_icloud_duplicates() {
    [ "$(uname)" = "Darwin" ] || return 0
    find "$1" \( -name "* [0-9]" -o -name "* [0-9].*" \) -delete 2>/dev/null || true
}

# --- 1. Python ---------------------------------------------------------------

say "Vérification de Python"
if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 est introuvable. Installez Python 3.11 ou plus récent." >&2
    exit 1
fi
python3 - <<'PY' || { echo "Python 3.11+ requis." >&2; exit 1; }
import sys
if sys.version_info < (3, 11):
    print(f"Python {sys.version.split()[0]} détecté, 3.11 minimum requis.", file=sys.stderr)
    raise SystemExit(1)
print(f"  Python {sys.version.split()[0]}")
PY

# --- 2. Environnement virtuel ------------------------------------------------

if [ ! -e "$VENV" ]; then
    if is_icloud_synced; then
        say "Création de l'environnement virtuel ($NOSYNC_VENV, hors iCloud)"
        warn "ce dossier est synchronisé par iCloud : le venv est placé à côté,"
        warn "dans $NOSYNC_VENV, que macOS exclut de la synchronisation."
        python3 -m venv "$NOSYNC_VENV"
        ln -s "$NOSYNC_VENV" "$VENV"
    else
        say "Création de l'environnement virtuel ($VENV)"
        python3 -m venv "$VENV"
    fi
elif [ -L "$VENV" ]; then
    say "Environnement virtuel déjà présent ($(readlink "$VENV"), hors iCloud)"
else
    say "Environnement virtuel déjà présent ($VENV)"
    if is_icloud_synced; then
        warn "ce venv est dans un dossier synchronisé par iCloud, qui va le corrompre"
        warn "à répétition. Pour le déplacer hors synchronisation, une seule fois :"
        warn "  mv $VENV $NOSYNC_VENV && ln -s $NOSYNC_VENV $VENV"
    fi
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install --quiet --upgrade pip

# --- 3. Dépendances ----------------------------------------------------------

say "Installation du paquet"
# Le capteur audio dépend de PortAudio, absent de certains systèmes : on
# n'échoue pas pour autant, le reste de l'outil n'en a pas besoin.
if python -m pip install --quiet -e ".[realtime,sensor]" 2>/dev/null; then
    echo "  temps réel et capteur installés"
else
    warn "extras capteur indisponibles (PortAudio manquant ?), repli sur le temps réel seul"
    python -m pip install --quiet -e ".[realtime]"
fi

# --- 3 bis. Vérification que l'installation éditable fonctionne --------------

# Le fichier .pth de l'installation éditable se retrouve parfois marqué
# « hidden » par macOS, et Python 3.13+ ignore délibérément les .pth cachés :
# l'installation ne fait alors strictement rien, sans le moindre message, et la
# commande reste introuvable alors que pip a réussi.
if ! python -c "import nexttraintosee" >/dev/null 2>&1; then
    if [ "$(uname)" = "Darwin" ]; then
        warn "installation éditable inopérante ; nettoyage des interférences macOS"
        purge_icloud_duplicates "$VENV"/lib/python*/site-packages
        chflags nohidden "$VENV"/lib/python*/site-packages/*.pth 2>/dev/null || true
    fi
    if ! python -c "import nexttraintosee" >/dev/null 2>&1; then
        echo "Le paquet reste introuvable après installation." >&2
        echo "Contournement : PYTHONPATH=src python -m nexttraintosee.cli --help" >&2
        exit 1
    fi
    echo "  installation réparée"
fi

# --- 4. Horaires théoriques --------------------------------------------------

mkdir -p data
if [ -s "$GTFS_PATH" ]; then
    say "Horaires SNCF déjà présents ($GTFS_PATH)"
else
    say "Téléchargement des horaires SNCF (~100 Mo)"
    if ! curl -fL --progress-bar -o "$GTFS_PATH.part" "$GTFS_URL"; then
        rm -f "$GTFS_PATH.part"
        warn "téléchargement impossible. Reprenez plus tard avec :"
        warn "  curl -L -o $GTFS_PATH $GTFS_URL"
    else
        mv "$GTFS_PATH.part" "$GTFS_PATH"
    fi
fi

# --- 5. Vérifications et géométrie des voies ---------------------------------

say "État de l'environnement"
nexttraintosee -c "$CONFIG" doctor

say "Résolution des voies autour du point (Overpass)"
if nexttraintosee -c "$CONFIG" tracks; then
    cat <<'MSG'

▸ À faire maintenant : reportez dans votre configuration
    - track_distance_m  → la valeur « distance par la voie » affichée ci-dessus
    - passes_observer   → false pour tout corridor qui ne passe pas devant chez vous

  Puis lancez :  nexttraintosee -c CONFIG next
MSG
else
    warn "Overpass n'a pas répondu. Réessayez plus tard : la géométrie est mise en cache,"
    warn "l'appel n'est nécessaire qu'une fois."
fi
