#!/usr/bin/env bash
# Zasifruje stav a ulozi ho na vetev `state` (jediny commit, force push -> repo neroste).
set -u
python statebox.py pack || exit 1
tmp=$(mktemp -d)
cp state.enc "$tmp/state.enc"
cd "$tmp"
git init -q -b state
git config user.name "parfemy-bot"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add state.enc
git commit -q -m "state $(date -u +%FT%TZ) [skip ci]"
for i in 1 2 3 4 5; do
  if git push -q --force "$REPO_URL" state:state; then
    echo "stav ulozen ($(date +%T))"
    rm -rf "$tmp"
    exit 0
  fi
  echo "push selhal, pokus $i"
  sleep $((i * 5))
done
rm -rf "$tmp"
exit 1
