#!/usr/bin/env bash
# Ticket-specific, non-production validation on a standard Docker-capable host.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
[[ -z "$(git status --porcelain --untracked-files=all)" ]] || { echo 'Use a clean committed checkout' >&2; exit 1; }
sha=$(git rev-parse HEAD)
evidence=${1:?Pass an evidence directory outside the checkout}
mkdir -p "$evidence"
evidence=$(cd "$evidence" && pwd -P)
case "$evidence/" in "$(pwd -P)/"*) echo 'Evidence must be outside checkout' >&2; exit 1;; esac
context=$(mktemp -d)
container=''
cleanup() {
  if [[ -n "$container" ]]; then docker rm -f "$container" >/dev/null || true; fi
  rm -rf "$context"
}
trap cleanup EXIT
git archive HEAD | tar -x -C "$context"
# Only fake sentinels: never locate, read, mount, or forward real host secrets.
printf 'fake-only\n' > "$context/.env"
mkdir -p "$context/.aws" "$context/data" "$context/config"
printf 'fake-only\n' > "$context/.aws/credentials"
printf 'fake-only\n' > "$context/data/dev-private.txt"
printf 'fake-only\n' > "$context/config/prediction_arbitrage.json"
export OPEN_TRADER_DEV_HOST_SENTINEL=fake-only
printf '%s\n' "$sha" > "$evidence/source-sha.txt"
sha256sum uv.lock > "$evidence/lock-before.txt"
for n in 1 2; do
  image="open-trader-dev-acceptance:$n"
  docker build --no-cache --target dev --progress plain -f "$context/Dockerfile.dev" \
    --build-arg SOURCE_SHA="$sha" --build-arg SOURCE_STATE=clean \
    -t "$image" "$context" 2>&1 | tee "$evidence/build-$n.log"
  docker image inspect "$image" > "$evidence/image-$n.json"
  docker run --rm --init --network none --cap-drop ALL \
    --security-opt no-new-privileges "$image" \
    python scripts/dev_dependency_manifest.py > "$evidence/manifest-$n.json"
  docker run --rm --init --network none --cap-drop ALL \
    --security-opt no-new-privileges "$image" \
    uv lock --check --offline --no-python-downloads 2>&1 | tee "$evidence/lock-check-$n.log"
  container=$(docker create --init --network none --cap-drop ALL \
    --security-opt no-new-privileges "$image" python -c '
from pathlib import Path
import importlib.util, os, socket
for path in ["/workspace/.env", "/workspace/.aws/credentials", "/workspace/data/dev-private.txt", "/workspace/config/prediction_arbitrage.json", "/var/run/docker.sock"]:
    assert not Path(path).exists(), path
assert "OPEN_TRADER_DEV_HOST_SENTINEL" not in os.environ
assert importlib.util.find_spec("playwright") is None
s = socket.socket(); s.settimeout(1)
assert s.connect_ex(("1.1.1.1", 443)) != 0
s.close()
print("Fake production-file exclusions, no forwarded environment, no browser, no external network: PASS")')
  docker inspect "$container" > "$evidence/container-$n.json"
  python3 - "$evidence/container-$n.json" <<'PY'
import json, sys
container = json.load(open(sys.argv[1]))[0]
host = container['HostConfig']
assert container['Mounts'] == []
assert host['NetworkMode'] == 'none'
assert host['CapDrop'] == ['ALL']
assert any(s.startswith('no-new-privileges') for s in host['SecurityOpt'])
assert not host['Privileged'] and not host.get('PortBindings')
PY
  docker start -a "$container" 2>&1 | tee "$evidence/isolation-$n.log"
  [[ "$(docker inspect --format '{{.State.ExitCode}}' "$container")" == 0 ]]
  docker rm "$container" >/dev/null
  container=''
done
cmp "$evidence/manifest-1.json" "$evidence/manifest-2.json"
python3 - "$evidence/manifest-1.json" "$sha" <<'PY'
import json, sys
manifest = json.load(open(sys.argv[1]))
assert manifest['source_sha'] == sys.argv[2]
assert manifest['source_state'] == 'clean'
assert manifest['python'] == '3.12.14'
assert not any(name == 'playwright' for name, _ in manifest['dependencies'])
PY
make test TEST='tests/test_dev_dependencies.py tests/test_dependency_workflow.py tests/test_prediction_ssm.py tests/test_dashboard_acceptance.py::test_candidate_acceptance_owns_container_backend_gate tests/test_dashboard_web.py::test_acceptance_gate_is_backend_only_and_production_smoke_owns_playwright' \
  2>&1 | tee "$evidence/focused-docker-tests.log"
sha256sum uv.lock > "$evidence/lock-after.txt"
cmp "$evidence/lock-before.txt" "$evidence/lock-after.txt"
git diff --exit-code -- pyproject.toml uv.lock
printf 'Development dependency acceptance: PASS for %s\n' "$sha" | tee "$evidence/result.txt"
