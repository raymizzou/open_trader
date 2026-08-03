.PHONY: acceptance test

WORKTREE_ROOT := $(CURDIR)
REPOSITORY_ROOT := $(shell git rev-parse --path-format=absolute --git-common-dir)/..

DASHBOARD_URL ?= http://127.0.0.1:8766
DASHBOARD_LOG ?= $(WORKTREE_ROOT)/logs/frontend_gateway/launchd.out.log
LEGACY_DASHBOARD_URL ?= http://127.0.0.1:8767
LEGACY_DASHBOARD_LOG ?= $(WORKTREE_ROOT)/logs/legacy_dashboard/launchd.out.log
SKIP_POLYMARKET_LIVE ?= 0
test:
	.venv/bin/python -m pytest -q

acceptance:
	cd "$(REPOSITORY_ROOT)" && \
		PYTHONSAFEPATH=1 PYTHONPATH="$(WORKTREE_ROOT):$(WORKTREE_ROOT)/src" \
		"$(WORKTREE_ROOT)/.venv/bin/python" -m pytest "$(WORKTREE_ROOT)/tests" -q
	@status=0; \
	cd "$(WORKTREE_ROOT)" && \
	OPEN_TRADER_PYTHON="$(WORKTREE_ROOT)/.venv/bin/python" \
		npm exec playwright test tests/e2e/prediction-market.spec.ts \
		--project=chromium || status=$$?; \
	if [ $$status -ne 0 ]; then echo FAIL; exit $$status; fi
ifeq ($(SKIP_POLYMARKET_LIVE),1)
	@echo "SKIPPED: Polymarket live acceptance by operator override"
else
	@status=0; \
	BROWSER_READY_ARG=--browser-ready; \
	cd "$(WORKTREE_ROOT)" && \
	PYTHONPATH=src .venv/bin/python -m open_trader.prediction_arbitrage_acceptance \
		--url "$(DASHBOARD_URL)" \
		--expected-root "$(WORKTREE_ROOT)" \
		--config "$(WORKTREE_ROOT)/config/prediction_arbitrage.json" \
		$$BROWSER_READY_ARG || status=$$?; \
	if [ $$status -eq 2 ]; then echo BLOCKED; exit 2; fi; \
	if [ $$status -ne 0 ]; then echo FAIL; exit $$status; fi
endif
	@status=0; \
	cd "$(WORKTREE_ROOT)" && \
	PYTHONPATH=src .venv/bin/python -m open_trader trend-drawdown-preflight \
		--config "$(REPOSITORY_ROOT)/config/daily_premarket.env" \
		--repo "$(WORKTREE_ROOT)" --actor acceptance || status=$$?; \
	if [ $$status -eq 2 ]; then echo BLOCKED; exit 2; fi; \
	if [ $$status -ne 0 ]; then echo FAIL; exit $$status; fi
	cd "$(WORKTREE_ROOT)" && \
		PYTHONPATH=src .venv/bin/python -m open_trader.dashboard_acceptance \
		--url "$(DASHBOARD_URL)" \
		--log "$(DASHBOARD_LOG)" \
		--legacy-url "$(LEGACY_DASHBOARD_URL)" \
		--legacy-log "$(LEGACY_DASHBOARD_LOG)" \
		--expected-root "$(CURDIR)"
