.PHONY: acceptance test

WORKTREE_ROOT := $(CURDIR)
REPOSITORY_ROOT := $(shell git rev-parse --path-format=absolute --git-common-dir)/..
PYTHON_BIN ?= $(if $(OPEN_TRADER_PYTHON),$(OPEN_TRADER_PYTHON),$(WORKTREE_ROOT)/.venv/bin/python)

DASHBOARD_URL ?= http://127.0.0.1:8766
DASHBOARD_LOG ?= $(WORKTREE_ROOT)/logs/frontend_gateway/launchd.out.log
LEGACY_DASHBOARD_URL ?= http://127.0.0.1:8767
LEGACY_DASHBOARD_LOG ?= $(WORKTREE_ROOT)/logs/legacy_dashboard/launchd.out.log
ACCOUNT_API_URL ?= http://127.0.0.1:8768
ACCOUNT_API_LOG ?= $(WORKTREE_ROOT)/logs/account_api/launchd.out.log
SKIP_POLYMARKET_LIVE ?= 0
ACCEPTANCE_DIR := $(WORKTREE_ROOT)/logs/acceptance
ACCEPTANCE_HANDOFF := $(ACCEPTANCE_DIR)/prediction-market-browser-handoff.json
ACCEPTANCE_NONCE := $(ACCEPTANCE_DIR)/prediction-market-browser-nonce
test:
	"$(PYTHON_BIN)" -m pytest -q

acceptance:
	cd "$(REPOSITORY_ROOT)" && \
		PYTHONSAFEPATH=1 PYTHONPATH="$(WORKTREE_ROOT):$(WORKTREE_ROOT)/src" \
		"$(PYTHON_BIN)" -m pytest "$(WORKTREE_ROOT)/tests" -q
	@status=0; \
	cd "$(WORKTREE_ROOT)" && \
	umask 077; \
	mkdir -p "$(ACCEPTANCE_DIR)"; \
	run_nonce="$$($(PYTHON_BIN) -c 'import secrets; print(secrets.token_urlsafe(32))')"; \
	printf '%s' "$$run_nonce" > "$(ACCEPTANCE_NONCE)"; \
	PREDICTION_ACCEPTANCE_BROWSER_HANDOFF="$(WORKTREE_ROOT)/logs/acceptance/prediction-market-browser-handoff.json" \
	PREDICTION_ACCEPTANCE_BROWSER_NONCE="$$run_nonce" \
	PREDICTION_ACCEPTANCE_REVIEW_URL="$(DASHBOARD_URL)" \
	OPEN_TRADER_PYTHON="$(PYTHON_BIN)" \
		npm exec playwright test tests/e2e/prediction-market.spec.ts \
		--project=chromium || status=$$?; \
	if [ $$status -ne 0 ]; then echo FAIL; exit $$status; fi
ifeq ($(SKIP_POLYMARKET_LIVE),1)
	@echo "SKIPPED: Polymarket live acceptance by operator override"
else
	@status=0; \
	cd "$(WORKTREE_ROOT)" && \
	PREDICTION_ACCEPTANCE_BROWSER_NONCE_FILE="$(ACCEPTANCE_NONCE)" \
	PYTHONPATH=src "$(PYTHON_BIN)" -m open_trader.prediction_arbitrage_acceptance \
		--url "$(DASHBOARD_URL)" \
		--expected-root "$(WORKTREE_ROOT)" \
		--config "$(WORKTREE_ROOT)/config/prediction_arbitrage.json" \
		--browser-handoff "$(ACCEPTANCE_HANDOFF)" || status=$$?; \
	if [ $$status -eq 2 ]; then echo BLOCKED; exit 2; fi; \
	if [ $$status -ne 0 ]; then echo FAIL; exit $$status; fi
endif
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
