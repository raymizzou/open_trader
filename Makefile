.PHONY: acceptance test

test:
	.venv/bin/python -m pytest -q

acceptance: test
	PYTHONPATH=src .venv/bin/python -m open_trader.dashboard_acceptance \
		--expected-root "$(CURDIR)" \
		--wait-seconds "$${WAIT_SECONDS:-125}"
