#!/usr/bin/env bash
# Assemble the Cloud Run build context in out/deploy_ctx from an ALLOWLIST.
#
# Nothing is copied by default: code comes from `git ls-files` (so an untracked
# or gitignored file such as .env can never ride along), the data is named one
# file at a time, and the index is copied without its snapshot text or its
# embedding cache. A final scan refuses to finish if anything secret-shaped
# made it in anyway.
#
#   bash deploy/stage.sh            then: gcloud run deploy --source out/deploy_ctx
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CTX="$ROOT/out/deploy_ctx"
cd "$ROOT"

[ -f rag_index/meta.json ] || { echo "no rag_index: run 'python -m jobbank rag build' first" >&2; exit 1; }

rm -rf "$CTX"
mkdir -p "$CTX/data" "$CTX/config" "$CTX/rag_index"

# Tracked code only.
git ls-files -z -- pyproject.toml src agent ":!:*.egg-info/*" | while IFS= read -r -d '' f; do
  mkdir -p "$CTX/$(dirname "$f")"
  cp "$f" "$CTX/$f"
done

# Data and config, by name. No personal data files, not profiles/.
cp config/config.yaml config/watchlist.yaml "$CTX/config/"
cp data/state.db "$CTX/data/"

# The index the agent searches; not snapshots/ (raw posting text) or the cache.
cp -r rag_index/lance "$CTX/rag_index/"
cp rag_index/meta.json "$CTX/rag_index/"

cp deploy/Dockerfile "$CTX/Dockerfile"
printf '**/__pycache__\n**/*.pyc\n' > "$CTX/.dockerignore"
printf '**/__pycache__\n**/*.pyc\n' > "$CTX/.gcloudignore"

# Belt and braces: fail on anything that looks like a credential.
bad=$(find "$CTX" \( -name '.env*' -o -name 'service_account*.json' -o -name '*.pem' \
        -o -name '*.key' -o -name 'credentials*.json' \) -print)
if [ -n "$bad" ]; then
  echo "refusing: secret-shaped files in the build context:" >&2
  echo "$bad" >&2
  exit 1
fi
if grep -rIlE 'sk-ant-|-----BEGIN [A-Z ]*PRIVATE KEY' "$CTX" --exclude-dir=lance >/dev/null 2>&1; then
  echo "refusing: a file in the build context contains key material:" >&2
  grep -rIlE 'sk-ant-|-----BEGIN [A-Z ]*PRIVATE KEY' "$CTX" --exclude-dir=lance >&2
  exit 1
fi

echo "staged $(find "$CTX" -type f | wc -l) files, $(du -sh "$CTX" | cut -f1) -> $CTX"
