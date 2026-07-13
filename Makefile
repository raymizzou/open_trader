.PHONY: acceptance test

DASHBOARD_URL ?= http://127.0.0.1:8766
DASHBOARD_LOG ?= /tmp/open_trader_dashboard_8766.log

test:
	.venv/bin/python -m pytest -q

acceptance: test
	PYTHONPATH=src .venv/bin/python -m open_trader.dashboard_acceptance \
		--url "$(DASHBOARD_URL)" \
		--log "$(DASHBOARD_LOG)" \
		--expected-root "$(CURDIR)" \
		--wait-seconds "$${WAIT_SECONDS:-125}"
