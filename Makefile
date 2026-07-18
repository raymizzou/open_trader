.PHONY: acceptance test

WORKTREE_ROOT := $(CURDIR)
REPOSITORY_ROOT := $(shell git rev-parse --path-format=absolute --git-common-dir)/..

DASHBOARD_URL ?= http://127.0.0.1:8766
DASHBOARD_LOG ?= /tmp/open_trader_dashboard_8766.log
EXPECTED_CN ?= 4

test:
	cd "$(REPOSITORY_ROOT)" && \
		PYTHONSAFEPATH=1 PYTHONPATH="$(WORKTREE_ROOT):$(WORKTREE_ROOT)/src" \
		"$(WORKTREE_ROOT)/.venv/bin/python" -m pytest "$(WORKTREE_ROOT)/tests" -q

acceptance: test
	PYTHONPATH=src .venv/bin/python -m open_trader.dashboard_acceptance \
		--url "$(DASHBOARD_URL)" \
		--log "$(DASHBOARD_LOG)" \
		--expected-cn "$(EXPECTED_CN)" \
		--expected-root "$(CURDIR)"
