#!/usr/bin/env bash
set -euo pipefail

umask 077

repo_root="$(git rev-parse --show-toplevel)"
config_root="${XDG_CONFIG_HOME:-${HOME}/.config}"
target_file="${BILLCOMMONS_ENV_FILE:-${config_root}/billcommons/.env}"
target_file="$(realpath -m -- "$target_file")"

case "$target_file" in
  "$repo_root"/*)
    relative_target="${target_file#"$repo_root"/}"
    if git -C "$repo_root" ls-files --error-unmatch -- "$relative_target" >/dev/null 2>&1; then
      echo "Refusing to write SOLARI_API_KEY to tracked file: $relative_target" >&2
      exit 1
    fi
    ;;
esac

mkdir -p -- "$(dirname -- "$target_file")"
touch -- "$target_file"
chmod 600 -- "$target_file"

printf 'Solari API key: ' >&2
IFS= read -r -s solari_key
printf '\n' >&2

if [[ -z "$solari_key" ]]; then
  echo "No key entered; configuration was not changed." >&2
  exit 1
fi

temporary_file="$(mktemp "${target_file}.tmp.XXXXXX")"
cleanup() {
  rm -f -- "$temporary_file"
}
trap cleanup EXIT

while IFS= read -r line || [[ -n "$line" ]]; do
  [[ "$line" == SOLARI_API_KEY=* ]] && continue
  printf '%s\n' "$line" >>"$temporary_file"
done <"$target_file"
printf 'SOLARI_API_KEY=%s\n' "$solari_key" >>"$temporary_file"
unset solari_key

chmod 600 -- "$temporary_file"
mv -- "$temporary_file" "$target_file"
trap - EXIT

echo "Solari credentials saved to the Bill Commons local config file (value hidden)."
