.PHONY: acceptance test prediction-solver-envs prediction-solver-quick prediction-solver-full-macos prediction-solver-full-linux prediction-solver-report prediction-solver-verify-report

WORKTREE_ROOT := $(CURDIR)
REPOSITORY_ROOT := $(shell git rev-parse --path-format=absolute --git-common-dir)/..
PYTHON_BIN ?= $(if $(OPEN_TRADER_PYTHON),$(OPEN_TRADER_PYTHON),$(REPOSITORY_ROOT)/.venv/bin/python)

DASHBOARD_URL ?= http://127.0.0.1:8766
DASHBOARD_LOG ?= $(WORKTREE_ROOT)/logs/frontend_gateway/launchd.out.log
LEGACY_DASHBOARD_URL ?= http://127.0.0.1:8767
LEGACY_DASHBOARD_LOG ?= $(WORKTREE_ROOT)/logs/legacy_dashboard/launchd.out.log
ACCOUNT_API_URL ?= http://127.0.0.1:8768
ACCOUNT_API_LOG ?= $(WORKTREE_ROOT)/logs/account_api/launchd.out.log
test:
	"$(PYTHON_BIN)" -m pytest -q

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

acceptance:
	cd "$(REPOSITORY_ROOT)" && \
		PYTHONSAFEPATH=1 PYTHONPATH="$(WORKTREE_ROOT):$(WORKTREE_ROOT)/src" \
		"$(PYTHON_BIN)" -m pytest "$(WORKTREE_ROOT)/tests" -q
	@test "$$(git -C "$(WORKTREE_ROOT)" branch --show-current)" = main
	@test -z "$$(git -C "$(WORKTREE_ROOT)" status --porcelain)"
	@cd "$(WORKTREE_ROOT)" && scripts/install_account_release.sh --dry-run --repo-root "$(WORKTREE_ROOT)" --python "$(PYTHON_BIN)"
	@cd "$(WORKTREE_ROOT)" && scripts/install_dashboard_launchd.sh --dry-run --repo-root "$(WORKTREE_ROOT)"
	@cd "$(WORKTREE_ROOT)" && scripts/install_daily_premarket_launchd.sh --dry-run --config "$(WORKTREE_ROOT)/config/daily_premarket.env" --trend-only --market all
	@cd "$(WORKTREE_ROOT)" && scripts/install_account_release.sh --repo-root "$(WORKTREE_ROOT)" --python "$(PYTHON_BIN)" --evidence-out "$(WORKTREE_ROOT)/logs/account_release/acceptance.json"
	@cd "$(WORKTREE_ROOT)" && scripts/install_dashboard_launchd.sh --repo-root "$(WORKTREE_ROOT)"
	@cd "$(WORKTREE_ROOT)" && scripts/install_daily_premarket_launchd.sh --config "$(WORKTREE_ROOT)/config/daily_premarket.env" --trend-only --market all
	@cd "$(WORKTREE_ROOT)" && \
	OPEN_TRADER_PYTHON="$(PYTHON_BIN)" \
		npm exec playwright test tests/e2e/prediction-market.spec.ts \
		--project=chromium
	@status=0; \
	cd "$(WORKTREE_ROOT)" && \
	PYTHONPATH=src "$(PYTHON_BIN)" -m open_trader trend-drawdown-preflight \
		--config "$(REPOSITORY_ROOT)/config/daily_premarket.env" \
		--repo "$(WORKTREE_ROOT)" --actor acceptance || status=$$?; \
	if [ $$status -eq 2 ]; then echo BLOCKED; exit 2; fi; \
	if [ $$status -ne 0 ]; then echo FAIL; exit $$status; fi
	cd "$(WORKTREE_ROOT)" && \
		PYTHONPATH=src "$(PYTHON_BIN)" -m open_trader.dashboard_acceptance \
		--url "$(DASHBOARD_URL)" \
		--log "$(DASHBOARD_LOG)" \
		--legacy-url "$(LEGACY_DASHBOARD_URL)" \
		--legacy-log "$(LEGACY_DASHBOARD_LOG)" \
		--account-url "$(ACCOUNT_API_URL)" \
		--account-log "$(ACCOUNT_API_LOG)" \
		--expected-root "$(CURDIR)"
