#!/usr/bin/env bash
# Deployt das memrack-Plugin als COPY in JEDE Profil-Home (R2/Präfix: hermes -p scannt NUR das eigene Home).
# Nutzung: deploy_profiles.sh <profil1> [profil2 ...] [--live]   (ohne --live = dry-run)
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIVE=0; for a in "$@"; do [ "$a" = "--live" ] && LIVE=1; done
for a in "$@"; do
  [ "$a" = "--live" ] && continue
  dst="$HOME/.hermes/profiles/$a/plugins/memrack"
  echo "DRY: rsync $SRC -> $dst"   # Copy statt Symlink: Profil-Isolation (R2-A-8)
  if [ "$LIVE" = 1 ]; then
    mkdir -p "$HOME/.hermes/profiles/$a/plugins"
    rsync -a --exclude .git --exclude tests --exclude docs --exclude .pytest_cache \
      --exclude __pycache__ "$SRC/" "$dst/"
    echo "DEPLOYED: $dst"
  fi
done
