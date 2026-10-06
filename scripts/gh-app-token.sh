#!/usr/bin/env bash
# Print a 1-hour GitHub installation token for one AI agent's GitHub App.
# Keys stay outside the repo in ~/.config/github-apps/<key>.pem and <key>.json.
# Usage: export GH_TOKEN=$(scripts/gh-app-token.sh claude|openai|grok)
set -euo pipefail

key="${1:-}"
case "$key" in
  claude|openai|grok) ;;
  *) echo "usage: $0 <claude|openai|grok>" >&2; exit 2 ;;
esac

store="${GITHUB_APPS_DIR:-$HOME/.config/github-apps}"
repo="${GITHUB_APP_REPO:-ihormudryy/mmr}"
pem="$store/$key.pem"
meta="$store/$key.json"
for file in "$pem" "$meta"; do
  [[ -r "$file" ]] || { echo "missing $file: ask the owner for this App's key" >&2; exit 1; }
done

app_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$meta")

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }
now=$(date +%s)
header=$(printf '{"alg":"RS256","typ":"JWT"}' | b64url)
payload=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' "$((now - 60))" "$((now + 540))" "$app_id" | b64url)
signature=$(printf '%s.%s' "$header" "$payload" | openssl dgst -sha256 -sign "$pem" -binary | b64url)
jwt="$header.$payload.$signature"

api() { curl -fsS -H "Authorization: Bearer $jwt" -H "Accept: application/vnd.github+json" "$@"; }
installation_id=$(api "https://api.github.com/repos/$repo/installation" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
api -X POST "https://api.github.com/app/installations/$installation_id/access_tokens" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])'
