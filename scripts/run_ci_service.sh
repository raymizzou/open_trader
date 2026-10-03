#!/usr/bin/env bash
# Ordinary development gate only. Builds may download; test containers are offline.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
scope=${1:?scope required}
nleg=${2:?TEST_N_LEG required}
evidence=${3:?evidence directory required}
case "$scope" in gateway|legacy|account|prediction|portable|trend-curve) ;; *) exit 2;; esac
case "$nleg" in 1) ;; *) exit 2;; esac
[[ -z "$(git status --porcelain --untracked-files=all)" ]] || { echo 'Clean checkout required' >&2; exit 1; }
sha=$(git rev-parse HEAD)
[[ "$sha" == "${GITHUB_SHA:?event SHA required}" ]] || { echo 'Candidate SHA mismatch' >&2; exit 1; }
mkdir -p "$evidence"
evidence=$(cd "$evidence" && pwd -P)
case "$evidence/" in "$(pwd -P)/"*) echo 'Evidence must be outside checkout' >&2; exit 1;; esac
printf 'source_sha=%s\nscope=%s\nTEST_N_LEG=%s\n' "$sha" "$scope" "$nleg" | tee "$evidence/identity.txt"
sha256sum uv.lock | tee "$evidence/lock-sha256.txt"
image="open-trader-ci:$scope"
status=0
# Preserve Makefile serial defaults outside Prediction; SDK imports share state.
workers=1
[[ "$scope" != prediction ]] || workers=2
if [[ "$scope" == portable ]]; then
  make test-ci-portable DOCKER_IMAGE="$image" 2>&1 | tee "$evidence/test.log" || status=$?
elif [[ "$scope" == trend-curve ]]; then
  make test-trend-curve DOCKER_IMAGE="$image" TEST_WORKERS="$workers" 2>&1 | tee "$evidence/test.log" || status=$?
else
  make test SERVICE="$scope" TEST_N_LEG="$nleg" TEST_WORKERS="$workers" DOCKER_IMAGE="$image" \
    2>&1 | tee "$evidence/test.log" || status=$?
fi
# Preserve build and lock identities even if pytest failed, without masking failure.
if docker image inspect "$image" > "$evidence/image.json"; then
  docker run --rm --init --network none --cap-drop ALL --security-opt no-new-privileges \
    "$image" python scripts/dev_dependency_manifest.py > "$evidence/dependency-manifest.json" || status=1
else
  status=1
fi
if [[ "$scope" == portable ]]; then
  docker run --rm --init --network none --cap-drop ALL --security-opt no-new-privileges \
    "$image" python scripts/ci_partition.py > "$evidence/partition.json" 2> "$evidence/partition.log" || status=1
fi
sha256sum -c "$evidence/lock-sha256.txt" || status=1
python3 scripts/ci_evidence.py "$scope" "$nleg" "$workers" "$status" "$evidence" || status=1
printf 'scope=%s source_sha=%s exit_status=%s\n' "$scope" "$sha" "$status" | tee "$evidence/result.txt"
if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  cat "$evidence/identity.txt" "$evidence/lock-sha256.txt" "$evidence/result.txt" >> "$GITHUB_STEP_SUMMARY"
fi
exit "$status"
