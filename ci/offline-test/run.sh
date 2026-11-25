#!/usr/bin/env bash
# Proves the image works with no internet: runs the offline stack twice,
# once with a Bedrock API key and once with an IAM access key.
# Usage: ci/offline-test/run.sh   (IMAGE=... to test another tag)
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# Signs of LiteLLM trying to reach something it should not.
OUTBOUND='ConnectError|ConnectTimeout|Name or service not known|Temporary failure in name resolution|getaddrinfo|raw\.githubusercontent|Failed to fetch'

run_pass() { # run_pass <name> <expect-auth> <creds...>
  local name="$1" expect="$2"; shift 2
  local creds="$work/$name.env" logs="$work/$name.log" status=0
  printf '%s\n' "$@" > "$creds"
  local compose=(docker compose -f "$here/compose.yaml" -p "airgaped-litellm-$name"
                 --env-file "$root/versions.env")
  export CREDS_FILE="$creds" EXPECT_AUTH="$expect"

  echo "=== pass: $name ($expect)"
  if ! "${compose[@]}" up -d --quiet-pull litellm mock; then
    status=1
  elif ! "${compose[@]}" run --rm tester; then
    status=1
  fi
  "${compose[@]}" logs --no-color litellm migrate > "$logs" 2>&1 || true
  if grep -Ei "$OUTBOUND" "$logs" > "$work/$name.outbound"; then
    echo "FAIL outbound_attempts: LiteLLM tried to reach the network:"
    head -20 "$work/$name.outbound"
    status=1
  else
    echo "PASS no_outbound_attempts"
  fi
  if [ "$status" -ne 0 ]; then
    echo "--- last 80 log lines ($name)"; tail -80 "$logs"
  fi
  "${compose[@]}" --profile test down -v --remove-orphans > /dev/null 2>&1 || true
  return "$status"
}

overall=0
run_pass bearer bearer "AWS_BEARER_TOKEN_BEDROCK=ABSK-test-only" || overall=1
run_pass sigv4 sigv4 "AWS_ACCESS_KEY_ID=AKIATESTONLY" "AWS_SECRET_ACCESS_KEY=test-only" || overall=1

if [ "$overall" -eq 0 ]; then echo "OFFLINE TEST: PASS"; else echo "OFFLINE TEST: FAIL"; fi
exit "$overall"
