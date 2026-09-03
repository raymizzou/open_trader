.PHONY: acceptance candidate-acceptance test test-trend-curve test-pressure host-readiness browser-test production-smoke prediction-solver-envs prediction-solver-quick prediction-solver-full-macos prediction-solver-full-linux prediction-solver-report prediction-solver-verify-report

WORKTREE_ROOT := $(CURDIR)
REPOSITORY_ROOT := $(shell git rev-parse --path-format=absolute --git-common-dir)/..
PYTHON_BIN ?= $(if $(OPEN_TRADER_PYTHON),$(OPEN_TRADER_PYTHON),$(REPOSITORY_ROOT)/.venv/bin/python)
PLAYWRIGHT_NODE_PATH ?= $(REPOSITORY_ROOT)/node_modules

DOCKER ?= docker
DOCKERFILE ?= Dockerfile.dev
WORKTREE_HASH := $(shell printf '%s' "$(WORKTREE_ROOT)" | shasum -a 256 | cut -c1-12)
DOCKER_IMAGE ?= open-trader-dev:$(WORKTREE_HASH)
BUILDX_CONFIG ?= /tmp/open-trader-buildx-$(WORKTREE_HASH)
DOCKER_BUILD = BUILDX_CONFIG="$(BUILDX_CONFIG)" $(DOCKER) build --target dev --file "$(WORKTREE_ROOT)/$(DOCKERFILE)" --tag "$(DOCKER_IMAGE)" "$(WORKTREE_ROOT)"
DOCKER_RUN = $(DOCKER) run --rm --init --network none --cap-drop ALL --security-opt no-new-privileges "$(DOCKER_IMAGE)"
BACKEND_PYTEST := env PYTHONSAFEPATH=1 PYTHONPATH=/workspace:/workspace/src pytest -q -m "not pressure and not browser" -o cache_dir=/tmp/open-trader-pytest-cache --basetemp=/tmp/open-trader-pytest

DASHBOARD_URL ?= http://127.0.0.1:8766
DASHBOARD_LOG ?= $(WORKTREE_ROOT)/logs/frontend_gateway/launchd.out.log
LEGACY_DASHBOARD_URL ?= http://127.0.0.1:8767
LEGACY_DASHBOARD_LOG ?= $(WORKTREE_ROOT)/logs/legacy_dashboard/launchd.out.log
ACCOUNT_API_URL ?= http://127.0.0.1:8768
ACCOUNT_API_LOG ?= $(WORKTREE_ROOT)/logs/account_api/launchd.out.log

PREDICTION_CONFIG ?= $(REPOSITORY_ROOT)/config/prediction_arbitrage.json
DAILY_CONFIG ?= $(REPOSITORY_ROOT)/config/daily_premarket.env
PRE_DEPLOY_SUBMISSION_BASELINE ?=

test:
	$(DOCKER_BUILD)
	$(DOCKER_RUN) $(BACKEND_PYTEST) $(TEST)

test-trend-curve:
	$(MAKE) test TEST='$(if $(TEST),$(TEST),tests/test_trend_curve_research.py tests/test_trend_curve_backtest.py tests/test_trend_curve_cli.py)'

candidate-acceptance:
	@status=0; \
	if $(DOCKER_BUILD); then \
		if $(DOCKER_RUN) sh -c '$(BACKEND_PYTEST) && $(BACKEND_PYTEST) acceptance/test_prediction_arbitrage_scenarios.py -k "not LIVE"'; then \
			:; \
		else \
			status=$$?; \
		fi; \
	else \
		status=$$?; \
	fi; \
	if [ $$status -eq 0 ]; then echo 'Candidate Acceptance: PASS'; else echo 'Candidate Acceptance: FAIL'; exit 1; fi

test-pressure:
	"$(PYTHON_BIN)" -m pytest -q -m pressure

browser-test:
	PYTHONSAFEPATH=1 PYTHONPATH="$(WORKTREE_ROOT):$(WORKTREE_ROOT)/src" "$(PYTHON_BIN)" -m pytest -q -m browser
	NODE_PATH="$(PLAYWRIGHT_NODE_PATH)" "$(REPOSITORY_ROOT)/node_modules/.bin/playwright" test tests/e2e/dashboard-warm-ledger.spec.ts tests/e2e/kelly-lab.spec.ts tests/e2e/prediction-market.spec.ts --config=playwright.config.ts --project=chromium

prediction-solver-envs:
	PYTHON_BIN="$(PYTHON_BIN)" ./scripts/build_prediction_solver_envs.sh

prediction-solver-quick:
	PYTHONSAFEPATH=1 PYTHONPATH="$(WORKTREE_ROOT)/src" "$(PYTHON_BIN)" -m open_trader prediction-solver-benchmark quick

prediction-solver-full-macos:
	PYTHONSAFEPATH=1 PYTHONPATH="$(WORKTREE_ROOT)/src" "$(PYTHON_BIN)" -m open_trader prediction-solver-benchmark full --environment macos

prediction-solver-full-linux:
	PYTHONSAFEPATH=1 PYTHONPATH="$(WORKTREE_ROOT)/src" "$(PYTHON_BIN)" -m open_trader prediction-solver-benchmark full --environment linux

prediction-solver-report:
	PYTHONSAFEPATH=1 PYTHONPATH="$(WORKTREE_ROOT)/src" "$(PYTHON_BIN)" -m open_trader prediction-solver-benchmark report

prediction-solver-verify-report:
	PYTHONSAFEPATH=1 PYTHONPATH="$(WORKTREE_ROOT)/src" "$(PYTHON_BIN)" -m open_trader prediction-solver-benchmark verify-report

acceptance: candidate-acceptance

host-readiness:
	@set -u; \
	status=0; \
	check() { label="$$1"; shift; if "$$@" >/dev/null 2>&1; then echo "$$label: PASS"; else echo "$$label: BLOCKED"; status=1; fi; }; \
	check "account launchd dry-run" "$(WORKTREE_ROOT)/scripts/install_account_release.sh" --dry-run --repo-root "$(WORKTREE_ROOT)" --runtime-root "$(REPOSITORY_ROOT)" --python "$(PYTHON_BIN)"; \
	check "dashboard launchd dry-run" "$(WORKTREE_ROOT)/scripts/install_dashboard_launchd.sh" --dry-run --mode stack --repo-root "$(WORKTREE_ROOT)" --runtime-root "$(REPOSITORY_ROOT)"; \
	check "trend launchd dry-run" "$(WORKTREE_ROOT)/scripts/install_daily_premarket_launchd.sh" --dry-run --trend-only --market all --config "$(DAILY_CONFIG)"; \
	nleg_replay_passes() { \
		"$(PYTHON_BIN)" -m open_trader prediction-arb nleg-validate \
			--replay "$(REPOSITORY_ROOT)/tests/fixtures/prediction_n_leg_validation_frozen_n3.json" \
			--live-catalog /dev/null 2>/dev/null \
			| "$(PYTHON_BIN)" -c 'import json,sys; p=json.load(sys.stdin); ok=(p.get("replay") or {}).get("status")=="PASS" and (p.get("live") or {}).get("reason")=="LIVE_CATALOG_UNAVAILABLE"; raise SystemExit(0 if ok else 1)'; \
	}; \
	check "account status" "$(PYTHON_BIN)" -m open_trader account-sync-status --account-url "$(ACCOUNT_API_URL)" --json; \
	check "prediction status" "$(PYTHON_BIN)" -m open_trader prediction-arb status --url "$(DASHBOARD_URL)"; \
	check "prediction wallet" "$(PYTHON_BIN)" -m open_trader prediction-arb wallet status --config "$(PREDICTION_CONFIG)"; \
	if nleg_replay_passes >/dev/null 2>&1; then echo "prediction n-leg replay validation: PASS"; else echo "prediction n-leg replay validation: BLOCKED"; status=1; fi; \
	check "Python Playwright Chrome" "$(PYTHON_BIN)" -c 'from playwright.sync_api import sync_playwright; p = sync_playwright().start(); browser = p.chromium.launch(channel="chrome", headless=True); browser.close(); p.stop()'; \
	if [ -x "$(REPOSITORY_ROOT)/node_modules/.bin/playwright" ] && (cd "$(WORKTREE_ROOT)" && NODE_PATH="$(PLAYWRIGHT_NODE_PATH)" node -e 'const {chromium}=require("playwright"); (async()=>{const browser=await chromium.launch({headless:true}); await browser.close();})().catch(()=>process.exit(1));' && NODE_PATH="$(PLAYWRIGHT_NODE_PATH)" OPEN_TRADER_SMOKE_URL="$(DASHBOARD_URL)" "$(REPOSITORY_ROOT)/node_modules/.bin/playwright" test tests/e2e/production-smoke.spec.ts --config=playwright.config.ts --project=chromium --list) >/dev/null 2>&1; then echo "Playwright Chromium: PASS"; else echo "Playwright Chromium: BLOCKED"; status=1; fi; \
	check "loopback listeners" sh -c 'command -v lsof >/dev/null && for port in 8766 8767 8768 8769; do lsof -nP -iTCP:$$port -sTCP:LISTEN >/dev/null; done'; \
	check "storage" df -P "$(REPOSITORY_ROOT)"; \
	check "Futu connectivity" "$(PYTHON_BIN)" -c 'import socket; s = socket.create_connection(("127.0.0.1", 11111), 2); s.close()'; \
	if [ $$status -eq 0 ]; then echo READY; else echo BLOCKED; exit 2; fi

# This target asserts the POST-#60-cutover world (reader fence 2): it is the
# gate for the cutover release SHA. Legacy-era probes (preflight --no-submit,
# monitor-once, cross-auto status) are retired together with the legacy
# mutation set; state assertions below pin the N_LEG contract generation.
production-smoke:
	@set -u; \
	status=0; \
	expected_sha='$(EXPECTED_SHA)'; expected_root='$(EXPECTED_ROOT)'; expected_runtime_root='$(EXPECTED_RUNTIME_ROOT)'; baseline='$(PRE_DEPLOY_SUBMISSION_BASELINE)'; \
	if ! printf '%s' "$$expected_sha" | grep -Eq '^[0-9a-fA-F]{40}$$'; then echo "EXPECTED_SHA must be a 40-hex Git SHA"; echo ROLLBACK; exit 2; fi; \
	case "$$expected_root" in /*) ;; *) echo "EXPECTED_ROOT must be an absolute immutable checkout"; echo ROLLBACK; exit 2;; esac; \
	if [ ! -d "$$expected_root" ]; then echo "EXPECTED_ROOT does not exist"; echo ROLLBACK; exit 2; fi; \
	expected_root="$$(cd "$$expected_root" && pwd -P)"; \
	case "$$expected_runtime_root" in /*) ;; *) echo "EXPECTED_RUNTIME_ROOT must be an absolute shared runtime root"; echo ROLLBACK; exit 2;; esac; \
	if [ ! -d "$$expected_runtime_root" ]; then echo "EXPECTED_RUNTIME_ROOT does not exist"; echo ROLLBACK; exit 2; fi; \
	expected_runtime_root="$$(cd "$$expected_runtime_root" && pwd -P)"; \
	if [ -z "$$baseline" ] || [ ! -s "$$baseline" ]; then echo "PRE_DEPLOY_SUBMISSION_BASELINE is required and must name a captured JSON file"; echo ROLLBACK; exit 2; fi; \
	if [ "$$(git -C "$$expected_root" rev-parse HEAD 2>/dev/null || true)" != "$$expected_sha" ] || [ -n "$$(git -C "$$expected_root" symbolic-ref --quiet --short HEAD 2>/dev/null || true)" ] || [ -n "$$(git -C "$$expected_root" status --porcelain --untracked-files=all 2>/dev/null || true)" ]; then echo "immutable checkout identity or cleanliness mismatch"; status=1; else echo "checkout: PASS"; fi; \
	submission_baseline_matches() { printf '%s' "$$1" | "$(PYTHON_BIN)" -c 'import json,pathlib,sys; baseline=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")); current=json.load(sys.stdin); keys=("current_execution", "last_execution"); marker=lambda value: None if not isinstance(value,dict) else {key:value.get(key) for key in ("execution_id", "id", "status", "state", "result", "order_ids", "legs") if key in value}; raise SystemExit(0 if isinstance(baseline,dict) and all(marker(baseline.get(key)) == marker(current.get(key)) for key in keys) else 1)' "$$baseline" >/dev/null 2>&1; }; \
	check_health() { name="$$1"; kind="$$2"; url="$$3"; payload="$$(curl -fsS --max-time 5 "$$url/healthz" 2>/dev/null || true)"; if [ -z "$$payload" ]; then echo "$$name: BLOCKED"; status=1; health_pid=""; return; fi; health_pid="$$(printf '%s' "$$payload" | "$(PYTHON_BIN)" -c 'import json,sys; p=json.load(sys.stdin); kind,sha,root=sys.argv[1:]; under_root=(lambda value: isinstance(value,str) and (value==root or value.startswith(root+"/"))); common=((kind=="account" and p.get("api_git_sha")==sha and p.get("worker_git_sha")==sha and under_root(p.get("code_root")) and under_root(p.get("worker_code_root"))) or (kind!="account" and p.get("cwd")==root and p.get("source_state")=="clean" and p.get("git_sha")==sha and under_root(p.get("code_root")))); ok=(common and ((kind=="gateway" and p.get("schema_version")=="open_trader.frontend_gateway.health.v1" and p.get("module")=="frontend_gateway" and p.get("legacy_upstream_status")=="ok" and p.get("account_upstream_status")=="ok" and p.get("prediction_upstream_status")=="ok" and p.get("prediction_route_mode")=="service") or (kind=="legacy" and p.get("schema_version")=="open_trader.legacy_dashboard.health.v1" and p.get("module")=="legacy_dashboard") or (kind=="prediction" and p.get("schema_version")=="open_trader.prediction_service.health.v1" and p.get("module")=="prediction_service" and p.get("status")=="running" and p.get("mode")=="production" and p.get("production_owner") is True and p.get("mutations")=="enabled") or (kind=="account" and p.get("schema_version")=="open_trader.account_api.health.v1" and p.get("module")=="account_api" and p.get("status")=="ok" and p.get("mode")=="production" and p.get("release_match") is True))); print(p.get("pid", ""), end="") if ok else None; raise SystemExit(0 if ok else 1)' "$$kind" "$$expected_sha" "$$expected_root" 2>/dev/null || true)"; if [ -n "$$health_pid" ]; then echo "$$name: PASS pid=$$health_pid"; else echo "$$name: BLOCKED"; status=1; fi; }; \
	if [ $$status -eq 0 ]; then \
		if (cd "$$expected_root" && PYTHONSAFEPATH=1 PYTHONPATH="$$expected_root:$$expected_root/src" "$(PYTHON_BIN)" -m pytest -q -m browser); then \
			echo "Python browser prerequisite: PASS"; \
		else \
			echo "Python browser prerequisite: BLOCKED"; echo ROLLBACK; exit 1; \
		fi; \
	fi; \
	check_health "gateway health" gateway "$(DASHBOARD_URL)"; gateway_pid="$$health_pid"; \
	check_health "legacy health" legacy "$(LEGACY_DASHBOARD_URL)"; legacy_pid="$$health_pid"; \
	check_health "account health" account "$(ACCOUNT_API_URL)"; account_pid="$$health_pid"; \
	check_health "prediction health" prediction "http://127.0.0.1:8769"; prediction_pid="$$health_pid"; \
	check_process() { name="$$1"; port="$$2"; pid="$$3"; listener="$$(lsof -nP -tiTCP:"$$port" -sTCP:LISTEN 2>/dev/null | awk 'NF {print; count++} END {if (count != 1) exit 1}')" || listener=""; cwd="$$(lsof -a -p "$$pid" -d cwd -Fn 2>/dev/null | awk '/^n/ {print substr($$0,2); exit}')"; if [ -n "$$pid" ] && [ "$$listener" = "$$pid" ] && [ "$$cwd" = "$$expected_root" ] && ps -p "$$pid" -o pid=,lstart=,command= >/dev/null 2>&1; then echo "$$name: PASS pid=$$pid"; else echo "$$name: BLOCKED"; status=1; fi; }; \
	check_process "gateway process/listener" 8766 "$$gateway_pid"; \
	check_process "legacy process/listener" 8767 "$$legacy_pid"; \
	check_process "account process/listener" 8768 "$$account_pid"; \
	check_process "prediction process/listener" 8769 "$$prediction_pid"; \
	state_payload="$$(curl -fsS --max-time 10 "http://127.0.0.1:8769/api/prediction-arbitrage/state" 2>/dev/null || true)"; \
	if [ -z "$$state_payload" ]; then echo "current execution: BLOCKED"; status=1; else if printf '%s' "$$state_payload" | "$(PYTHON_BIN)" -c 'import json,sys; p=json.load(sys.stdin); raise SystemExit(0 if str(p.get("status", "")).lower() not in {"", "unavailable", "error"} and "current_execution" in p else 1)' >/dev/null 2>&1; then echo "current execution: PASS"; else echo "current execution: BLOCKED"; status=1; fi; fi; \
	if submission_baseline_matches "$$state_payload"; then echo "submission baseline: PASS"; else echo "submission baseline: BLOCKED"; status=1; fi; \
	if printf '%s' "$$state_payload" | "$(PYTHON_BIN)" -c 'import json,sys; p=json.load(sys.stdin); n_leg=p.get("n_leg") or {}; scopes=n_leg.get("execution_scopes") or {}; scope=scopes.get("SAME_EVENT_SAME_VENUE") or {}; rows=p.get("opportunities") or []; ok=(n_leg.get("contract_generation")==2 and n_leg.get("mode")=="MANUAL" and scope.get("capability")=="OBSERVE_ONLY" and all(row.get("engine_owner")=="N_LEG" for row in rows if isinstance(row,dict))); raise SystemExit(0 if ok else 1)' >/dev/null 2>&1; then echo "n-leg state: PASS"; else echo "n-leg state: BLOCKED"; status=1; fi; \
	for log in "$$expected_root/logs/frontend_gateway/launchd.err.log" "$$expected_root/logs/legacy_dashboard/launchd.err.log" "$$expected_root/logs/account_api/launchd.err.log" "$$expected_runtime_root/logs/prediction_service/launchd.err.log"; do if [ ! -f "$$log" ]; then echo "log missing: $$log"; status=1; elif [ ! "$$log" -nt "$$baseline" ]; then echo "log stale: $$log"; status=1; elif tail -n 200 "$$log" | rg -qi 'traceback|fatal|exception|error'; then echo "log error: $$log"; status=1; else echo "log clean: $$log"; fi; done; \
	if [ $$status -eq 0 ]; then \
		if (cd "$$expected_root" && NODE_PATH="$(PLAYWRIGHT_NODE_PATH)" OPEN_TRADER_SMOKE_URL="$(DASHBOARD_URL)" "$(REPOSITORY_ROOT)/node_modules/.bin/playwright" test tests/e2e/production-smoke.spec.ts --config=playwright.config.ts --project=chromium); then echo "browser smoke: PASS"; else echo "browser smoke: BLOCKED"; status=1; fi; \
	fi; \
	post_browser_state="$$(curl -fsS --max-time 10 "http://127.0.0.1:8769/api/prediction-arbitrage/state" 2>/dev/null || true)"; \
	if submission_baseline_matches "$$post_browser_state"; then echo "submission baseline: PASS"; else echo "submission baseline: BLOCKED"; status=1; fi; \
	if [ $$status -eq 0 ]; then echo HEALTHY; else echo ROLLBACK; exit 1; fi
