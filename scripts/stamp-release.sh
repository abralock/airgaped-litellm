#!/usr/bin/env bash
# Writes a release copy of deploy/ with images pinned to the release tag and digests,
# and the migration Job renamed per release (a Job's pod template cannot change).
# Usage: scripts/stamp-release.sh <tag> <litellm-digest> <postgres-digest> <out-dir>
# Exit codes: 0 ok, 2 invalid input (nothing written).
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
if [ "$#" -ne 4 ]; then
  echo "usage: $0 <tag> <litellm-digest> <postgres-digest> <out-dir>" >&2; exit 2
fi
tag="$1" litellm_digest="$2" postgres_digest="$3" out="$4"

if ! [[ "$tag" =~ ^(v[0-9]+\.[0-9]+\.[0-9]+)-(r|rc)[0-9]+$ ]]; then
  echo "tag '$tag' must look like v<litellm-version>-r<N> or v<litellm-version>-rc<N>" >&2; exit 2
fi
tag_version="${BASH_REMATCH[1]}"
image_version="$(sed -n 's/^ARG LITELLM_VERSION=//p' "$root/Dockerfile")"
if [ "$tag_version" != "$image_version" ]; then
  echo "tag '$tag' is for LiteLLM $tag_version but the Dockerfile builds $image_version" >&2; exit 2
fi
for digest in "$litellm_digest" "$postgres_digest"; do
  if ! [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "'$digest' is not a sha256 digest" >&2; exit 2
  fi
done

mkdir -p "$out"
rm -rf "$out/deploy"
cp -R "$root/deploy" "$out/deploy"

python3 - "$out/deploy" "$tag" "$litellm_digest" "$postgres_digest" <<'PY'
import pathlib
import re
import sys

deploy, tag, litellm_digest, postgres_digest = sys.argv[1:]
deploy = pathlib.Path(deploy)
digests = {
    "ghcr.io/abralock/airgaped-litellm": litellm_digest,
    "ghcr.io/abralock/airgaped-litellm-postgres": postgres_digest,
}

for kustomization in (deploy / "base/kustomization.yaml", deploy / "base/migrate/kustomization.yaml"):
    lines, current = [], None
    for line in kustomization.read_text().splitlines():
        name = re.match(r"\s*- name: (\S+)", line)
        if name:
            current = name.group(1)
        if line.strip() == "newTag: unreleased" and current in digests:
            indent = line[: len(line) - len(line.lstrip())]
            lines += [f"{indent}newTag: {tag}", f"{indent}digest: {digests[current]}"]
            continue
        lines.append(line)
    text = "\n".join(lines) + "\n"
    if "unreleased" in text:
        sys.exit(f"{kustomization}: an image was left unstamped")
    kustomization.write_text(text)

job = deploy / "base/migrate/job.yaml"
text, count = re.subn(r"(?m)^  name: litellm-migrate\b.*$", f"  name: litellm-migrate-{tag}", job.read_text())
if count != 1:
    sys.exit(f"{job}: expected one Job name to rename, found {count}")
job.write_text(text)
PY
echo "stamped $out/deploy for $tag"
