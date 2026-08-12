#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
benchmark_root="$repo_root/benchmarks/prediction_solver"
env_root="$repo_root/.benchmark-envs"
adapter_protocol="open_trader.prediction_solver.protocol.v1"
python_bin="${PYTHON_BIN:-python3}"
target="${1:-all}"

case "$target" in highs|scip|cp_sat|all) ;; *) echo "unknown environment: $target" >&2; exit 2 ;; esac

"$python_bin" - "$benchmark_root/licenses.json" <<'PY'
import json
import sys

licenses = json.load(open(sys.argv[1]))
for name in ("highspy", "pyscipopt", "scip", "ortools", "vipr"):
    entry = licenses.get(name, {})
    if not all(entry.get(field) for field in ("version", "license", "project_url", "evidence_path")):
        raise SystemExit(f"license evidence missing or ambiguous for {name}")
    if entry.get("commercial_key_required") is not False:
        raise SystemExit(f"commercial-key policy missing for {name}")
vipr = licenses["vipr"]
if not vipr.get("evidence_sha256"):
    raise SystemExit("license evidence missing or ambiguous for vipr")
PY

build_key() {
  local requirements="$1" dockerfile="$2"
  {
    "$python_bin" -c 'import sys; print(f"python={sys.version_info.major}.{sys.version_info.minor}")'
    printf 'os=%s\narchitecture=%s\nprotocol=%s\n' "$(uname -s)" "$(uname -m)" "$adapter_protocol"
    cat "$requirements"
    cat "$dockerfile"
  } | shasum -a 256 | awk '{print $1}'
}

smoke_check() {
  local environment="$1" venv="$2"
  case "$environment" in
    highs) "$venv/bin/python" -c 'import highspy; assert highspy.Highs().version() == "1.15.1"' ;;
    scip) "$venv/bin/python" -c 'import pyscipopt; assert pyscipopt.__version__ == "6.2.1"' ;;
    cp_sat) "$venv/bin/python" -c 'import ortools; assert ortools.__version__ == "9.15.6755"' ;;
  esac
}

build_venv() {
  local environment="$1" requirements="$2" dockerfile="$3" venv="$env_root/$environment" key
  key="$(build_key "$requirements" "$dockerfile")"
  if [[ -f "$venv/.build-key" && "$(<"$venv/.build-key")" == "$key" ]]; then
    smoke_check "$environment" "$venv"
    printf '%s VENV REUSED key=%s\n' "$environment" "$key"
    return
  fi
  [[ -e "$venv" ]] && printf '%s VENV REBUILD_REQUIRED key=%s\n' "$environment" "$key"
  "$python_bin" -m venv --clear "$venv"
  "$venv/bin/python" -m pip install --disable-pip-version-check --report "$venv/install-report.json" -r "$requirements"
  smoke_check "$environment" "$venv"
  printf '%s\n' "$key" > "$venv/.build-key"
  printf '%s VENV BUILT key=%s\n' "$environment" "$key"
}

image_smoke_check() {
  local environment="$1" image="$2"
  case "$environment" in
    highs) docker run --rm "$image" python -c 'import highspy; assert highspy.Highs().version() == "1.15.1"' ;;
    scip) docker run --rm "$image" sh -c 'python -c "import pyscipopt; assert pyscipopt.__version__ == \"6.2.1\"" && command -v viprchk && command -v viprcomp' ;;
    cp_sat) docker run --rm "$image" python -c 'import ortools; assert ortools.__version__ == "9.15.6755"' ;;
  esac
}

build_image() {
  local environment="$1" requirements="$2" dockerfile="$3" key image
  key="$(build_key "$requirements" "$dockerfile")"
  image="open-trader-prediction-solver-$environment:$key"
  if docker image inspect "$image" >/dev/null 2>&1; then
    image_smoke_check "$environment" "$image"
    printf '%s IMAGE REUSED key=%s id=%s\n' "$environment" "$key" "$(docker image inspect --format '{{.Id}}' "$image")"
    return
  fi
  [[ -n "$(docker image ls -q "open-trader-prediction-solver-$environment" 2>/dev/null)" ]] && printf '%s IMAGE REBUILD_REQUIRED key=%s\n' "$environment" "$key"
  if [[ "$environment" == scip ]]; then
    DOCKER_BUILDKIT=0 docker build --tag "$image" --file "$dockerfile" "$benchmark_root"
  else
    DOCKER_BUILDKIT=0 docker build --build-arg "REQUIREMENTS=$(basename "$requirements")" --tag "$image" --file "$dockerfile" "$benchmark_root"
  fi
  image_smoke_check "$environment" "$image"
  printf '%s IMAGE BUILT key=%s id=%s\n' "$environment" "$key" "$(docker image inspect --format '{{.Id}}' "$image")"
}

build_one() {
  local environment="$1" requirements="$benchmark_root/requirements/$1.txt" dockerfile
  case "$environment" in
    highs|cp_sat) dockerfile="$benchmark_root/Dockerfile.python" ;;
    scip) dockerfile="$benchmark_root/Dockerfile.scip" ;;
  esac
  build_venv "$environment" "$requirements" "$dockerfile"
  build_image "$environment" "$requirements" "$dockerfile"
}

if [[ "$target" == all ]]; then
  for environment in highs scip cp_sat; do build_one "$environment"; done
else
  build_one "$target"
fi
