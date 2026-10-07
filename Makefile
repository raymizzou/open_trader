.PHONY: acceptance candidate-acceptance deployment-preflight test test-trend-curve test-pressure host-readiness browser-test production-smoke prediction-solver-envs prediction-solver-quick prediction-solver-full-macos prediction-solver-full-linux prediction-solver-report prediction-solver-verify-report

WORKTREE_ROOT := $(CURDIR)
REPOSITORY_ROOT := $(shell git rev-parse --path-format=absolute --git-common-dir)/..
PYTHON_BIN ?= $(if $(OPEN_TRADER_PYTHON),$(OPEN_TRADER_PYTHON),$(REPOSITORY_ROOT)/.venv/bin/python)
PLAYWRIGHT_NODE_PATH ?= $(REPOSITORY_ROOT)/node_modules

DOCKER ?= docker
DOCKERFILE ?= Dockerfile.dev
WORKTREE_HASH := $(shell printf '%s' "$(WORKTREE_ROOT)" | shasum -a 256 | cut -c1-12)
DOCKER_IMAGE ?= open-trader-dev:$(WORKTREE_HASH)
BUILDX_CONFIG ?= /tmp/open-trader-buildx-$(WORKTREE_HASH)
SOURCE_SHA := $(shell git rev-parse HEAD)
SOURCE_STATE := $(if $(shell git status --porcelain --untracked-files=all),dirty,clean)
DOCKER_BUILD = BUILDX_CONFIG="$(BUILDX_CONFIG)" $(DOCKER) build --build-arg SOURCE_SHA="$(SOURCE_SHA)" --build-arg SOURCE_STATE="$(SOURCE_STATE)" --target dev --file "$(WORKTREE_ROOT)/$(DOCKERFILE)" --tag "$(DOCKER_IMAGE)" "$(WORKTREE_ROOT)"
DOCKER_RUN = $(DOCKER) run --rm --init --network none --cap-drop ALL --security-opt no-new-privileges "$(DOCKER_IMAGE)"
# CI retains a named, offline container only long enough to copy test records.
CI_TEST_ARTIFACTS ?= 0
CI_TEST_CONTAINER ?= open-trader-test-records
ifeq ($(CI_TEST_ARTIFACTS),1)
DOCKER_RUN = $(DOCKER) run --name "$(CI_TEST_CONTAINER)" --init --network none --cap-drop ALL --security-opt no-new-privileges "$(DOCKER_IMAGE)"
CI_PYTEST_RECORDS = -p scripts.ci_test_metrics --junitxml=/tmp/open-trader-ci-evidence/junit.xml
CI_PYTEST_ENV = CI_TEST_METRICS=/tmp/open-trader-ci-evidence/metrics.json CI_TEST_SOURCE_SHA=$(SOURCE_SHA) CI_TEST_WORKERS=$(TEST_WORKERS)
endif
BACKEND_PYTEST = env $(CI_PYTEST_ENV) PYTHONSAFEPATH=1 PYTHONPATH=/workspace:/workspace/src PYTHONDONTWRITEBYTECODE= PYTHONPYCACHEPREFIX=/tmp/open-trader-bytecache pytest -q $(CI_PYTEST_RECORDS) -m "not pressure and not browser" -o cache_dir=/tmp/open-trader-pytest-cache --basetemp=/tmp/open-trader-pytest
TEST_WORKERS ?= $(if $(filter prediction,$(SERVICE)),6,1)
TEST_N_LEG ?= 0
# The reviewed manifest is the sole retirement selection source.
N_LEG_MANIFEST ?= scripts/ci_nleg_retired.json
CI_SELECTION = python3 scripts/ci_evidence.py select --manifest "$(N_LEG_MANIFEST)" --nleg "$(TEST_N_LEG)"

DASHBOARD_URL ?= http://127.0.0.1:8766
DASHBOARD_LOG ?= $(WORKTREE_ROOT)/logs/frontend_gateway/launchd.out.log
LEGACY_DASHBOARD_URL ?= http://127.0.0.1:8767
LEGACY_DASHBOARD_LOG ?= $(WORKTREE_ROOT)/logs/legacy_dashboard/launchd.out.log
ACCOUNT_API_URL ?= http://127.0.0.1:8768
ACCOUNT_API_LOG ?= $(WORKTREE_ROOT)/logs/account_api/launchd.out.log
N_LEG_PAUSED ?= 0

PREDICTION_CONFIG ?= $(REPOSITORY_ROOT)/config/prediction_arbitrage.json
DAILY_CONFIG ?= $(REPOSITORY_ROOT)/config/daily_premarket.env
RELEASE_SERVICES ?= gateway legacy account prediction
FIRST_DEPLOY ?= 0

test:
	$(if $(strip $(SERVICE)$(TEST)),,$(error Specify SERVICE or TEST))
	$(if $(filter-out gateway legacy account prediction,$(SERVICE)),$(error Unknown SERVICE: $(SERVICE)))
	$(if $(and $(strip $(SERVICE)),$(strip $(TEST))),$(error Use SERVICE or TEST, not both))
	$(if $(and $(filter 1,$(words $(TEST_N_LEG))),$(filter 0 1,$(TEST_N_LEG))),,$(error TEST_N_LEG must be 0 or 1))
	$(eval SELECTED_TESTS := $(shell $(CI_SELECTION) $(if $(SERVICE),--services $(SERVICE)) || echo __INVALID_SELECTION__))
	$(if $(filter __INVALID_SELECTION__,$(SELECTED_TESTS)),$(error Invalid retirement selection))
	$(if $(and $(filter prediction,$(SERVICE)),$(filter 0,$(TEST_N_LEG))),@echo "N-leg permanently retired (29 files); use TEST_N_LEG=1 for manual diagnostics.")
	$(DOCKER_BUILD)
	$(DOCKER_RUN) $(BACKEND_PYTEST) $(if $(strip $(TEST)),$(TEST),$(SELECTED_TESTS)) $(if $(filter 1,$(TEST_WORKERS)),,-n $(TEST_WORKERS) --dist=loadgroup)

# CI metadata and collection proof use the identical file partition as make test.
.PHONY: ci-test-files test-ci-portable
ci-test-files:
	@$(CI_SELECTION)

# Preserve one serial session for the portable scenarios; LIVE alone is excluded.
test-ci-portable:
	$(DOCKER_BUILD)
	$(DOCKER_RUN) $(BACKEND_PYTEST) acceptance/test_prediction_arbitrage_scenarios.py -k "not LIVE"

test-trend-curve:
	$(MAKE) test TEST='$(if $(TEST),$(TEST),tests/test_trend_curve_research.py tests/test_trend_curve_backtest.py tests/test_trend_curve_cli.py)'

# Compatibility names now reuse trusted CI; neither builds nor reruns pytest.
candidate-acceptance: deployment-preflight

deployment-preflight:
	"$(PYTHON_BIN)" -B "$(WORKTREE_ROOT)/scripts/deployment_preflight.py" --expected-sha "$(EXPECTED_SHA)" --release-root "$(WORKTREE_ROOT)" --python "$(PYTHON_BIN)" $(foreach extra,$(RELEASE_EXTRAS),--extra "$(extra)")

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
	services='$(strip $(RELEASE_SERVICES))'; \
	first_deploy='$(FIRST_DEPLOY)'; \
	case "$$first_deploy" in 0|1) ;; *) echo "FIRST_DEPLOY must be 0 or 1" >&2; echo BLOCKED; exit 2 ;; esac; \
	set -f; \
	if [ -z "$$services" ]; then echo "RELEASE_SERVICES must name one or more of: gateway legacy account prediction" >&2; echo BLOCKED; exit 2; fi; \
	set -- $$services; \
	gateway_selected=0; legacy_selected=0; account_selected=0; prediction_selected=0; \
	for service in "$$@"; do case "$$service" in gateway) gateway_selected=1 ;; legacy) legacy_selected=1 ;; account) account_selected=1 ;; prediction) prediction_selected=1 ;; *) echo "unknown RELEASE_SERVICES entry: $$service" >&2; echo BLOCKED; exit 2 ;; esac; done; \
	status=0; \
	check() { label="$$1"; shift; if "$$@" >/dev/null 2>&1; then echo "$$label: PASS"; else echo "$$label: BLOCKED"; status=1; fi; }; \
	if [ "$$first_deploy" = 1 ]; then \
		check "fresh host ownership" sh -c 'command -v launchctl >/dev/null || exit 1; listed="$$(launchctl list 2>/dev/null)" || exit 1; case "$$listed" in *com.open-trader.*) exit 1 ;; esac; for plist in "$$HOME"/Library/LaunchAgents/com.open-trader.*.plist; do [ ! -e "$$plist" ] && [ ! -L "$$plist" ] || exit 1; done'; \
	fi; \
	if [ $$gateway_selected -eq 1 ]; then \
		if [ $$legacy_selected -eq 1 ]; then \
			check "dashboard launchd dry-run" "$(WORKTREE_ROOT)/scripts/install_dashboard_launchd.sh" --dry-run --mode stack --repo-root "$(WORKTREE_ROOT)" --runtime-root "$(REPOSITORY_ROOT)"; \
		else \
			check "gateway launchd dry-run" "$(WORKTREE_ROOT)/scripts/install_dashboard_launchd.sh" --dry-run --mode gateway --repo-root "$(WORKTREE_ROOT)" --runtime-root "$(REPOSITORY_ROOT)"; \
		fi; \
	elif [ $$legacy_selected -eq 1 ]; then \
		check "legacy launchd dry-run" "$(WORKTREE_ROOT)/scripts/install_dashboard_launchd.sh" --dry-run --mode legacy --repo-root "$(WORKTREE_ROOT)" --runtime-root "$(REPOSITORY_ROOT)"; \
	fi; \
	if [ $$account_selected -eq 1 ]; then \
		check "account launchd dry-run" "$(WORKTREE_ROOT)/scripts/install_account_release.sh" --dry-run --repo-root "$(WORKTREE_ROOT)" --runtime-root "$(REPOSITORY_ROOT)" --python "$(PYTHON_BIN)"; \
	fi; \
	if [ $$legacy_selected -eq 1 ]; then \
		check "trend launchd dry-run" "$(WORKTREE_ROOT)/scripts/install_daily_premarket_launchd.sh" --dry-run --trend-only --market all --config "$(DAILY_CONFIG)"; \
	fi; \
	nleg_replay_passes() { \
		"$(PYTHON_BIN)" -m open_trader prediction-arb nleg-validate \
			--replay "$(REPOSITORY_ROOT)/tests/fixtures/prediction_n_leg_validation_frozen_n3.json" \
			--live-catalog /dev/null 2>/dev/null \
			| "$(PYTHON_BIN)" -c 'import json,sys; p=json.load(sys.stdin); ok=(p.get("replay") or {}).get("status")=="PASS" and (p.get("live") or {}).get("reason")=="LIVE_CATALOG_UNAVAILABLE"; raise SystemExit(0 if ok else 1)'; \
	}; \
	if [ $$account_selected -eq 1 ] && [ "$$first_deploy" = 0 ]; then check "account status" "$(PYTHON_BIN)" -m open_trader account-sync-status --account-url "$(ACCOUNT_API_URL)" --json; fi; \
	if [ $$prediction_selected -eq 1 ]; then \
		check "prediction wallet" "$(PYTHON_BIN)" -m open_trader prediction-arb wallet status --config "$(PREDICTION_CONFIG)"; \
		if nleg_replay_passes >/dev/null 2>&1; then echo "prediction n-leg replay validation: PASS"; else echo "prediction n-leg replay validation: BLOCKED"; status=1; fi; \
	fi; \
	check "Python Playwright Chrome" "$(PYTHON_BIN)" -c 'from playwright.sync_api import sync_playwright; p = sync_playwright().start(); browser = p.chromium.launch(channel="chrome", headless=True); browser.close(); p.stop()'; \
	if [ -x "$(REPOSITORY_ROOT)/node_modules/.bin/playwright" ] && (cd "$(WORKTREE_ROOT)" && NODE_PATH="$(PLAYWRIGHT_NODE_PATH)" node -e 'const {chromium}=require("playwright"); (async()=>{const browser=await chromium.launch({headless:true}); await browser.close();})().catch(()=>process.exit(1));' && NODE_PATH="$(PLAYWRIGHT_NODE_PATH)" OPEN_TRADER_SMOKE_URL="$(DASHBOARD_URL)" "$(REPOSITORY_ROOT)/node_modules/.bin/playwright" test tests/e2e/production-smoke.spec.ts --config=playwright.config.ts --project=chromium --list) >/dev/null 2>&1; then echo "Playwright Chromium: PASS"; else echo "Playwright Chromium: BLOCKED"; status=1; fi; \
	listener_ports=""; \
	if [ $$gateway_selected -eq 1 ]; then listener_ports="$$listener_ports 8766"; fi; \
	if [ $$legacy_selected -eq 1 ]; then listener_ports="$$listener_ports 8767"; fi; \
	if [ $$account_selected -eq 1 ]; then listener_ports="$$listener_ports 8768"; fi; \
	if [ $$prediction_selected -eq 1 ]; then listener_ports="$$listener_ports 8769"; fi; \
	if [ "$$first_deploy" = 1 ]; then \
		check "loopback listeners absent" sh -c 'command -v lsof >/dev/null || exit 1; for port do if lsof -nP -iTCP:"$$port" -sTCP:LISTEN >/dev/null 2>&1; then exit 1; fi; done' sh $$listener_ports; \
	else \
		check "loopback listeners" sh -c 'command -v lsof >/dev/null && for port do lsof -nP -iTCP:"$$port" -sTCP:LISTEN >/dev/null || exit 1; done' sh $$listener_ports; \
	fi; \
	check "storage" df -P "$(REPOSITORY_ROOT)"; \
	if [ $$legacy_selected -eq 1 ]; then check "Futu connectivity" "$(PYTHON_BIN)" -c 'import socket; s = socket.create_connection(("127.0.0.1", 11111), 2); s.close()'; fi; \
	if [ $$status -eq 0 ]; then echo READY; else echo BLOCKED; exit 2; fi

# This target asserts the POST-#60-cutover world (reader fence 2): it is the
# gate for the cutover release SHA. Legacy-era probes (preflight --no-submit,
# monitor-once, cross-auto status) are retired together with the legacy
# mutation set; state assertions below pin the N_LEG contract generation.
production-smoke:
	@set -u; \
	services='$(strip $(RELEASE_SERVICES))'; \
	set -f; \
	if [ -z "$$services" ]; then echo "RELEASE_SERVICES must name one or more of: gateway legacy account prediction" >&2; echo ROLLBACK; exit 2; fi; \
	set -- $$services; \
	gateway_selected=0; legacy_selected=0; account_selected=0; prediction_selected=0; \
	for service in "$$@"; do case "$$service" in gateway) gateway_selected=1 ;; legacy) legacy_selected=1 ;; account) account_selected=1 ;; prediction) prediction_selected=1 ;; *) echo "unknown RELEASE_SERVICES entry: $$service" >&2; echo ROLLBACK; exit 2 ;; esac; done; \
	status=0; \
	prediction_n_leg_status=""; prediction_n_leg_code=""; \
	expected_sha='$(EXPECTED_SHA)'; expected_root='$(EXPECTED_ROOT)'; expected_runtime_root='$(EXPECTED_RUNTIME_ROOT)'; expected_n_leg_paused='$(N_LEG_PAUSED)'; \
	if [ "$$expected_n_leg_paused" != 0 ] && [ "$$expected_n_leg_paused" != 1 ]; then echo "N_LEG_PAUSED must be 0 or 1"; echo ROLLBACK; exit 2; fi; \
	if ! printf '%s' "$$expected_sha" | grep -Eq '^[0-9a-fA-F]{40}$$'; then echo "EXPECTED_SHA must be a 40-hex Git SHA"; echo ROLLBACK; exit 2; fi; \
	case "$$expected_root" in /*) ;; *) echo "EXPECTED_ROOT must be an absolute immutable checkout"; echo ROLLBACK; exit 2;; esac; \
	if [ ! -d "$$expected_root" ]; then echo "EXPECTED_ROOT does not exist"; echo ROLLBACK; exit 2; fi; \
	expected_root="$$(cd "$$expected_root" && pwd -P)"; \
	case "$$expected_runtime_root" in /*) ;; *) echo "EXPECTED_RUNTIME_ROOT must be an absolute shared runtime root"; echo ROLLBACK; exit 2;; esac; \
	if [ ! -d "$$expected_runtime_root" ]; then echo "EXPECTED_RUNTIME_ROOT does not exist"; echo ROLLBACK; exit 2; fi; \
	expected_runtime_root="$$(cd "$$expected_runtime_root" && pwd -P)"; \
	if [ "$$(git -C "$$expected_root" rev-parse HEAD 2>/dev/null || true)" != "$$expected_sha" ] || [ -n "$$(git -C "$$expected_root" symbolic-ref --quiet --short HEAD 2>/dev/null || true)" ] || [ -n "$$(git -C "$$expected_root" status --porcelain --untracked-files=all 2>/dev/null || true)" ]; then echo "immutable checkout identity or cleanliness mismatch"; status=1; else echo "checkout: PASS"; fi; \
	check_health() { name="$$1"; kind="$$2"; url="$$3"; payload="$$(curl -fsS --max-time 5 "$$url/healthz" 2>/dev/null || true)"; if [ -z "$$payload" ]; then echo "$$name: BLOCKED"; status=1; health_pid=""; return; fi; if [ "$$kind" = prediction ]; then prediction_n_leg_status="$$(printf '%s' "$$payload" | "$(PYTHON_BIN)" -c 'import json,sys; p=json.load(sys.stdin); n=p.get("n_leg") or {}; print(n.get("status", ""), end="")' 2>/dev/null || true)"; prediction_n_leg_code="$$(printf '%s' "$$payload" | "$(PYTHON_BIN)" -c 'import json,sys; p=json.load(sys.stdin); n=p.get("n_leg") or {}; print(n.get("code", ""), end="")' 2>/dev/null || true)"; fi; health_pid="$$(printf '%s' "$$payload" | "$(PYTHON_BIN)" -c 'import json,sys; p=json.load(sys.stdin); kind,sha,root=sys.argv[1:]; under_root=(lambda value: isinstance(value,str) and (value==root or value.startswith(root+"/"))); common=((kind=="account" and p.get("api_git_sha")==sha and p.get("worker_git_sha")==sha and under_root(p.get("code_root")) and under_root(p.get("worker_code_root"))) or (kind!="account" and p.get("cwd")==root and p.get("source_state")=="clean" and p.get("git_sha")==sha and under_root(p.get("code_root")))); ok=(common and ((kind=="gateway" and p.get("schema_version")=="open_trader.frontend_gateway.health.v1" and p.get("module")=="frontend_gateway" and p.get("legacy_upstream_status")=="ok" and p.get("account_upstream_status")=="ok" and p.get("prediction_upstream_status")=="ok" and p.get("prediction_route_mode")=="service") or (kind=="legacy" and p.get("schema_version")=="open_trader.legacy_dashboard.health.v1" and p.get("module")=="legacy_dashboard") or (kind=="prediction" and p.get("schema_version")=="open_trader.prediction_service.health.v1" and p.get("module")=="prediction_service" and p.get("status")=="running" and p.get("mode")=="production" and p.get("production_owner") is True and p.get("mutations")=="enabled") or (kind=="account" and p.get("schema_version")=="open_trader.account_api.health.v1" and p.get("module")=="account_api" and p.get("status")=="ok" and p.get("mode")=="production" and p.get("release_match") is True))); print(p.get("pid", ""), end="") if ok else None; raise SystemExit(0 if ok else 1)' "$$kind" "$$expected_sha" "$$expected_root" 2>/dev/null || true)"; if [ -n "$$health_pid" ]; then echo "$$name: PASS pid=$$health_pid"; else echo "$$name: BLOCKED"; status=1; fi; }; \
	if [ $$status -eq 0 ]; then \
		if (cd "$$expected_root" && PYTHONSAFEPATH=1 PYTHONPATH="$$expected_root:$$expected_root/src" "$(PYTHON_BIN)" -m pytest -q -m browser); then \
			echo "Python browser prerequisite: PASS"; \
		else \
			echo "Python browser prerequisite: BLOCKED"; echo ROLLBACK; exit 1; \
		fi; \
	fi; \
	gateway_pid=""; legacy_pid=""; account_pid=""; prediction_pid=""; \
	if [ $$gateway_selected -eq 1 ]; then check_health "gateway health" gateway "$(DASHBOARD_URL)"; gateway_pid="$$health_pid"; fi; \
	if [ $$legacy_selected -eq 1 ]; then check_health "legacy health" legacy "$(LEGACY_DASHBOARD_URL)"; legacy_pid="$$health_pid"; fi; \
	if [ $$account_selected -eq 1 ]; then check_health "account health" account "$(ACCOUNT_API_URL)"; account_pid="$$health_pid"; fi; \
	if [ $$prediction_selected -eq 1 ]; then check_health "prediction health" prediction "http://127.0.0.1:8769"; prediction_pid="$$health_pid"; fi; \
	check_process() { name="$$1"; port="$$2"; pid="$$3"; listener="$$(lsof -nP -tiTCP:"$$port" -sTCP:LISTEN 2>/dev/null | awk 'NF {print; count++} END {if (count != 1) exit 1}')" || listener=""; cwd="$$(lsof -a -p "$$pid" -d cwd -Fn 2>/dev/null | awk '/^n/ {print substr($$0,2); exit}')"; if [ -n "$$pid" ] && [ "$$listener" = "$$pid" ] && [ "$$cwd" = "$$expected_root" ] && ps -p "$$pid" -o pid=,lstart=,command= >/dev/null 2>&1; then echo "$$name: PASS pid=$$pid"; else echo "$$name: BLOCKED"; status=1; fi; }; \
	if [ $$gateway_selected -eq 1 ]; then check_process "gateway process/listener" 8766 "$$gateway_pid"; fi; \
	if [ $$legacy_selected -eq 1 ]; then check_process "legacy process/listener" 8767 "$$legacy_pid"; fi; \
	if [ $$account_selected -eq 1 ]; then check_process "account process/listener" 8768 "$$account_pid"; fi; \
	if [ $$prediction_selected -eq 1 ]; then check_process "prediction process/listener" 8769 "$$prediction_pid"; fi; \
	if [ $$prediction_selected -eq 1 ]; then \
		if [ "$$expected_n_leg_paused" = 1 ]; then \
			if [ "$$prediction_n_leg_status" = paused ] && [ "$$prediction_n_leg_code" = N_LEG_PAUSED ]; then echo "n-leg state: PAUSED"; else echo "n-leg state: BLOCKED"; status=1; fi; \
			lp_payload="$$(curl -fsS --max-time 10 "http://127.0.0.1:8769/api/prediction-arbitrage/lp/dashboard" 2>/dev/null || true)"; \
			if printf '%s' "$$lp_payload" | "$(PYTHON_BIN)" -c 'import json,sys; p=json.load(sys.stdin); ok=(isinstance(p,dict) and p.get("state")=="ready" and isinstance(p.get("orders"),list) and isinstance(p.get("positions"),list) and isinstance(p.get("recommendations"),list)); raise SystemExit(0 if ok else 1)' >/dev/null 2>&1; then echo "lp dashboard: PASS"; else echo "lp dashboard: BLOCKED"; status=1; fi; \
		else \
			if [ "$$prediction_n_leg_status" = running ] && [ "$$prediction_n_leg_code" = N_LEG_RUNNING ]; then echo "n-leg state: RUNNING"; else echo "n-leg state: BLOCKED"; status=1; fi; \
			state_payload="$$(curl -fsS --max-time 10 "http://127.0.0.1:8769/api/prediction-arbitrage/state" 2>/dev/null || true)"; \
			if printf '%s' "$$state_payload" | "$(PYTHON_BIN)" -c 'import json,sys; p=json.load(sys.stdin); n_leg=p.get("n_leg") or {}; scopes=n_leg.get("execution_scopes") or {}; scope=scopes.get("SAME_EVENT_SAME_VENUE") or {}; rows=p.get("opportunities") or []; ok=(n_leg.get("contract_generation")==2 and n_leg.get("mode")=="MANUAL" and scope.get("capability")=="OBSERVE_ONLY" and all(row.get("engine_owner")=="N_LEG" for row in rows if isinstance(row,dict))); raise SystemExit(0 if ok else 1)' >/dev/null 2>&1; then echo "n-leg state: PASS"; else echo "n-leg state: BLOCKED"; status=1; fi; \
		fi; \
	fi; \
	check_log() { log="$$1"; if ! command -v rg >/dev/null 2>&1 || ! rg --version >/dev/null 2>&1; then echo "log checker unavailable"; status=1; elif "$(PYTHON_BIN)" "$$expected_root/scripts/check_production_log.py" "$$log"; then echo "log clean: $$log"; else echo "log check blocked: $$log"; status=1; fi; }; \
	if [ $$gateway_selected -eq 1 ]; then check_log "$$expected_root/logs/frontend_gateway/launchd.err.log"; fi; \
	if [ $$legacy_selected -eq 1 ]; then check_log "$$expected_root/logs/legacy_dashboard/launchd.err.log"; fi; \
	if [ $$account_selected -eq 1 ]; then check_log "$$expected_root/logs/account_api/launchd.err.log"; fi; \
	if [ $$prediction_selected -eq 1 ]; then check_log "$$expected_runtime_root/logs/prediction_service/launchd.err.log"; fi; \
	if [ $$status -eq 0 ]; then \
		if (cd "$$expected_root" && NODE_PATH="$(PLAYWRIGHT_NODE_PATH)" OPEN_TRADER_SMOKE_URL="$(DASHBOARD_URL)" "$(REPOSITORY_ROOT)/node_modules/.bin/playwright" test tests/e2e/production-smoke.spec.ts --config=playwright.config.ts --project=chromium); then echo "browser smoke: PASS"; else echo "browser smoke: BLOCKED"; status=1; fi; \
	fi; \
	if [ $$status -eq 0 ]; then echo HEALTHY; else echo ROLLBACK; exit 1; fi

.PHONY: prediction-cloud-host-readiness prediction-cloud-smoke
prediction-cloud-host-readiness:
	"$(PYTHON_BIN)" scripts/prediction-cloud-gate.py readiness --client-config "$(CLOUD_CLIENT_CONFIG)" --service-config "$(CLOUD_SERVICE_CONFIG)" --remote-config "$(CLOUD_REMOTE_CONFIG)" --operator-evidence "$(CLOUD_OPERATOR_EVIDENCE)" --browser-runtime "$(REPOSITORY_ROOT)"

prediction-cloud-smoke:
	"$(PYTHON_BIN)" scripts/prediction-cloud-gate.py smoke --client-config "$(CLOUD_CLIENT_CONFIG)" --service-config "$(CLOUD_SERVICE_CONFIG)" --remote-config "$(CLOUD_REMOTE_CONFIG)" --operator-evidence "$(CLOUD_OPERATOR_EVIDENCE)" --browser-runtime "$(REPOSITORY_ROOT)"
