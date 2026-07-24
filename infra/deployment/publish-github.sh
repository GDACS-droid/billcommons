#!/usr/bin/env bash
# Publish Bill Commons to GitHub. Run once a PAT with `repo` scope is available.
#
#   Provide the token by EITHER:
#     export GITHUB_TOKEN=ghp_xxx        (preferred; not written to disk)
#   or drop it in ~/.config/billcommons/github-token (chmod 600).
#
# Creates GDACS-droid/billcommons (public) if missing, then pushes all history.
set -euo pipefail

OWNER="${GH_OWNER:-GDACS-droid}"
REPO="${GH_REPO:-billcommons}"
DESC="Free, open-source legislative search for all 50 U.S. states + DC (billcommons.org)"

TOKEN="${GITHUB_TOKEN:-}"
if [[ -z "$TOKEN" && -f "$HOME/.config/billcommons/github-token" ]]; then
  TOKEN="$(<"$HOME/.config/billcommons/github-token")"
fi
if [[ -z "$TOKEN" ]]; then
  echo "No token. Set GITHUB_TOKEN or write ~/.config/billcommons/github-token" >&2
  exit 1
fi

cd "$(dirname "$0")/../.."   # repo root

# Create the repo if it doesn't exist (idempotent: 422 if it already does).
api() { curl -fsS -H "Authorization: Bearer $TOKEN" \
             -H "Accept: application/vnd.github+json" "$@"; }
if ! api "https://api.github.com/repos/$OWNER/$REPO" >/dev/null 2>&1; then
  # try org endpoint first, fall back to user endpoint
  api -X POST "https://api.github.com/orgs/$OWNER/repos" \
      -d "{\"name\":\"$REPO\",\"description\":\"$DESC\",\"private\":false}" >/dev/null 2>&1 \
  || api -X POST "https://api.github.com/user/repos" \
      -d "{\"name\":\"$REPO\",\"description\":\"$DESC\",\"private\":false}" >/dev/null
  echo "Created $OWNER/$REPO"
else
  echo "$OWNER/$REPO already exists — pushing"
fi

git remote remove origin 2>/dev/null || true
git remote add origin "git@github.com:$OWNER/$REPO.git"
git push -u origin main --tags
echo "Pushed to https://github.com/$OWNER/$REPO"
