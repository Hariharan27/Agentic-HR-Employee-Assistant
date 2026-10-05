#!/usr/bin/env sh
# Start a self-hosted Langfuse next to PeopleDesk (UI at http://localhost:3000).
# Uses the official Langfuse docker-compose.yml plus observability/langfuse.override.yml.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TARGET=${LANGFUSE_DIR:-"$HERE/../../langfuse-selfhost"}
if [ ! -d "$TARGET" ]; then
  git clone --depth 1 https://github.com/langfuse/langfuse.git "$TARGET"
fi
cd "$TARGET"
docker compose -p langfuse -f docker-compose.yml -f "$HERE/langfuse.override.yml" up -d
echo
echo "Langfuse is starting at http://localhost:3000 (first start takes 1-2 minutes)."
echo "Login: langfuse-admin@example.com / PeopleDesk!Trace-2026 · project: PeopleDesk"
