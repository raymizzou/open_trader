"""Loopback-only LP Auto controls for the running Prediction service."""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
from http.client import HTTPException
from http.cookiejar import CookieJar
import ipaddress
import json
import math
import time
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, ProxyHandler, Request, build_opener


ROOT = '/api/prediction-arbitrage/lp/auto/'
_STATUS_ADVICE = 'Run open-trader prediction-arb lp-auto status with the same --url to verify the service state.'
_TIMING_DEFAULTS = dict(round_interval_seconds=60, api_retry_interval_seconds=60, order_check_interval_seconds=10)


def _finite_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('LP service response contains a nonfinite number')
    return number


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _Client:
    def __init__(self, url: str, timeout: float):
        parsed = urlparse(url)
        if (
            parsed.scheme != 'http' or parsed.username is not None or parsed.password is not None
            or parsed.path not in {'', '/'} or parsed.query or parsed.fragment
            or not ipaddress.ip_address(parsed.hostname or '').is_loopback
            or not math.isfinite(timeout) or timeout <= 0
        ):
            raise ValueError('LP control requires an HTTP loopback service URL and finite timeout')
        parsed.port  # Reject invalid or out-of-range ports before opening a connection.
        self.base = url.rstrip('/')
        self.cookies = CookieJar()
        self.csrf = ''
        self.write_attempted = False
        self.rejected = False
        self.cookie_values = ()
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(self.cookies), _NoRedirects())
        self.deadline = time.monotonic() + timeout

    def request(self, path: str, payload: dict[str, object] | None = None, *, csrf: str = '') -> dict[str, object]:
        headers = {}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode('utf-8')
            headers = {'Content-Type': 'application/json', 'Origin': self.base, 'X-CSRF-Token': csrf}
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('LP control confirmation timed out')
        request = Request(self.base + path, data=data, headers=headers, method='POST' if payload is not None else 'GET')
        try:
            with self.opener.open(request, timeout=remaining) as response:
                result = json.load(response, parse_float=_finite_json_float, parse_constant=_finite_json_float)
        except HTTPError as exc:
            if payload is not None and exc.code in (400, 401, 403, 404, 405, 409, 422, 429):
                self.rejected = True
            try:
                error = json.load(exc, parse_float=_finite_json_float, parse_constant=_finite_json_float)
            except (OSError, ValueError):
                error = {}
            finally:
                exc.close()
            reason = error.get('error', error.get('reason')) if isinstance(error, dict) else None
            raise ValueError(f'LP service rejected request (HTTP {exc.code}): {reason or "UNKNOWN"}') from exc
        if not isinstance(result, dict):
            raise ValueError('LP service response must be an object')
        return result

    def write(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        bootstrap = self.request('/api/prediction-arbitrage/venues')
        self.cookie_values = tuple(cookie.value for cookie in self.cookies)
        csrf = bootstrap.get('csrf_token')
        if not isinstance(csrf, str) or not csrf:
            raise ValueError('LP service authentication unavailable')
        self.csrf = csrf
        self.write_attempted = True
        return self.request(ROOT + action, payload, csrf=csrf)

    def redact(self, value: object) -> object:
        secrets = [self.csrf, *self.cookie_values, *(cookie.value for cookie in self.cookies)]

        def scrub(item):
            if isinstance(item, str):
                for secret in secrets:
                    if secret:
                        item = item.replace(secret, '[REDACTED]')
                return item
            if isinstance(item, dict):
                return {scrub(key): '[REDACTED]' if key.lower() in
                        {'csrf_token', 'cookie', 'session_token', 'authorization'} else scrub(val)
                        for key, val in item.items()}
            if isinstance(item, list):
                return [scrub(val) for val in item]
            return item

        return scrub(value)


def _short_text(value: object) -> str:
    return ' '.join(str(value).split())[:240]


def _human_summary(result: str, state: dict[str, object] | None,
                   reason: str | None, definite_failure: bool) -> None:
    if result == 'UNKNOWN':
        print('result: FAILED' if definite_failure else 'result: UNKNOWN (UNCONFIRMED)')
        detail = reason or 'The service outcome could not be confirmed.'
        if ': ' in detail and detail.split(': ', 1)[0].endswith(('Error', 'Exception')):
            detail = detail.split(': ', 1)[1]
        if detail.startswith('<urlopen error'):
            detail = 'Cannot reach the service; check its endpoint and availability.'
        if detail in ('RemoteDisconnected', 'BadStatusLine', 'IncompleteRead'):
            detail = 'The service connection closed before confirmation; the write may have been accepted.'
        print('reason: ' + _short_text(detail))
        print('Next: ' + (_STATUS_ADVICE if not definite_failure else
              'Check the reason; use lp-auto --help for arguments or status --verbose for service details.'))
        return
    labels = {'STATUS': 'status read; trading readiness is separate',
              'CONFIGURED': 'configuration saved', 'ON': 'enable confirmed',
              'PAUSED': 'pause confirmed'}
    print(f'result: {result}; {labels[result]}')
    if state is None:
        return
    runtime = state.get('runtime_state')
    reasons = state.get('block_reasons') or state.get('admission_block_reasons') or state.get('reason')
    if isinstance(reasons, list):
        reasons = ', '.join(_short_text(item) for item in reasons[:3])
    if isinstance(reasons, dict):
        reasons = reasons.get('reason')
    if state.get('fatal') is not None:
        runtime, reasons = 'blocked', 'control fault; read status --verbose'
    elif state.get('control_save_state') in ('pending', 'saving', 'unknown'):
        runtime = 'unconfirmed'
    elif not isinstance(runtime, str):
        effective = state.get('effective_running')
        phase = state.get('current_phase')
        runtime = ('blocked' if reasons else phase if phase in ('starting', 'running', 'stopping', 'stopped')
                   else 'on') if effective is True else 'paused' if effective is False else 'unknown'
    line = 'state: ' + _short_text(runtime).upper()
    if reasons:
        line += '; reason: ' + _short_text(reasons)
    elif isinstance(state.get('current_phase'), str):
        line += '; phase: ' + _short_text(state['current_phase'])
    if result == 'PAUSED':
        line += '; new automatic BUYs paused; existing orders and protection remain'
    print(line)
    keys = (('trial_budget_usd', 'income_budget_usd') if 'trial_budget_usd' in state
            or 'income_budget_usd' in state else ('budget_usd', 'target_buy_count', 'buy_price_level'))
    values = [f'{key}: {_short_text(state[key])}' for key in keys if state.get(key) is not None]
    if values:
        print('; '.join(values))


def _emit(result: str, state: dict[str, object] | None, reason: str | None, json_output: bool,
          *, verbose: bool = False, definite_failure: bool = False) -> None:
    if json_output:
        print(json.dumps({'result': result, 'state': state, 'reason': reason,
                          'next_action': _STATUS_ADVICE if result == 'UNKNOWN' else None}, ensure_ascii=False))
        return
    if not verbose:
        _human_summary(result, state, reason, definite_failure)
        return
    print(f'result: {result}')
    if reason:
        print(f'reason: {reason}')
    if result == 'UNKNOWN':
        print(_STATUS_ADVICE)
    if state is None:
        return
    desired = state.get('desired_running')
    print('desired_running: ' + ('ON' if desired is True else 'OFF' if desired is False else 'UNKNOWN'))
    effective = state.get('effective_running')
    print('effective_running: ' + ('ON' if effective is True else 'OFF' if effective is False else 'UNKNOWN'))
    for key in ('control_save_state', 'requested_control_version', 'saved_control_version',
                'control_native_inflight', 'settings_pending', 'settings_native_inflight', 'fatal'):
        value = state.get(key)
        print(f'{key}: {value if value is not None else "NONE" if key in state else "UNKNOWN"}')
    for key in ('current_phase', 'round', 'effective_settings', 'pending_settings',
                'settings_error', 'control_error', 'control_request_status', 'control_request_reason',
                'current_exposure_usd', 'new_capacity_usd', 'plan_wait', 'diagnostics', 'observed_pools'):
        if key in state:
            print(f'{key}: {state[key] if state[key] is not None else "NONE"}')
    for key in ('runtime_state', 'budget_usd', 'target_buy_count', 'buy_price_level', 'config_version',
            'cycle_mode', 'api_retry_interval_seconds', 'order_check_interval_seconds'):
        value = state.get(key)
        if key == 'runtime_state' and isinstance(value, str) and result != 'UNKNOWN':
            value = value.upper()
        print(f'{key}: {value if value is not None else "UNKNOWN"}')
    slots = state.get('slots')
    active = (slots.get('active') if isinstance(slots, dict) else
              len(state['account_buys']) if isinstance(state.get('account_buys'), list) else None)
    target = state.get('target_buy_count')
    print(f'buy_orders: {active if active is not None else "UNKNOWN"}/'
          f'{target if target is not None else "UNKNOWN"}')
    for group, keys in (
        ('slots', ('active', 'pending', 'canceling', 'occupied')),
        ('funds', ('inventory_cost_usd', 'buy_reserved_usd', 'pending_reserved_usd', 'available_usd', 'spendable_usd')),
    ):
        values = state.get(group)
        top_level = not isinstance(values, dict)
        values = values if isinstance(values, dict) else {key: state[key] for key in keys if key in state}
        for key in keys:
            value = values.get(key)
            print(f'{key}: {value if value is not None else "NONE" if top_level and key in values else "UNKNOWN"}')
    for key in ('block_reasons', 'admission_block_reasons', 'reason', 'last_check_at', 'last_check_error'):
        value = state.get(key)
        if value is None and key in state and key in ('reason', 'last_check_error'):
            value = 'NONE'
        print(f'{key}: {value if value is not None else "UNKNOWN"}')
    for key in ('allocation_mode', 'allocation_weights', 'allocation_shadow_enabled',
            'trial_budget_usd','income_budget_usd','trial_target_buy_count','income_target_buy_count','income_weights'):
        if key in state:
            print(f'{key}: {state[key]}')
    checking = state.get('check_in_progress')
    print('check_in_progress: ' + ('true' if checking is True else 'false' if checking is False else 'UNKNOWN'))
    last_round = state.get('last_round')
    last_round = last_round if isinstance(last_round, dict) else {}
    for key in ('checked_at', 'reason'):
        value = last_round.get(key)
        if key == 'reason' and key in last_round and value is None:
            value = 'NONE'
        print(f'last_round_{key}: {value if value is not None else "UNKNOWN"}')



class LPArgumentParser(argparse.ArgumentParser):
    """Keep command argument failures inside the LP result output contract."""

    def parse_known_args(self, args=None, namespace=None):
        self._json_output = '--json' in (args or [])
        parsed, unknown = super().parse_known_args(args, namespace)
        if unknown:
            self.error('unrecognized arguments')
        return parsed, unknown

    def error(self, message):
        _emit('UNKNOWN', None, f'Invalid LP Auto arguments; use {self.prog} --help.',
              getattr(self, '_json_output', False), definite_failure=True)
        raise SystemExit(2)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    lp_auto_commands = parser.add_subparsers(
        dest="lp_auto_command", required=True, parser_class=LPArgumentParser
    )
    for action, description in (
        ("status", "Read actual LP Auto service state"),
        ("config", "Save configuration without enabling Auto"),
        ("on", "Enable Auto with saved configuration; report actual runtime state"),
        ("off", "Pause new automatic BUYs; existing orders and protection remain"),
        ("pause", "Persist pause and wait for service confirmation; existing orders remain"),
    ):
        command = lp_auto_commands.add_parser(action, help=description)
        command.add_argument("--url", default="http://127.0.0.1:8769")
        command.add_argument("--timeout", default=15.0)
        command.add_argument("--json", action="store_true")
        command.add_argument("--verbose", action="store_true", help="Show detailed LP diagnostics")
        if action == "config":
            command.add_argument("--budget", help="Legacy alias for trial budget")
            command.add_argument("--trial-budget")
            command.add_argument("--income-budget")
            command.add_argument("--target-buys")
            command.add_argument("--bid-level")
            command.add_argument("--allocation-mode", choices=("minimum", "rank_weights", "observed_pools"))
            command.add_argument("--allocation-weights", help="Comma-separated Decimal strings; no normalization")
            command.add_argument("--allocation-shadow-enabled", choices=("true", "false"))
            for option in ('round-interval-seconds', 'api-retry-interval-seconds', 'order-check-interval-seconds'):
                command.add_argument('--' + option, help=(
                    'Retired in continuous mode; setting this option returns an error'
                    if option == 'round-interval-seconds' else 'Dependency retry/check seconds'))


def run_namespace(args: argparse.Namespace) -> int:
    return run(args.lp_auto_command, args.url, args.timeout,
               json_output=getattr(args, "json", False),
               verbose=getattr(args, "verbose", False),
               budget=getattr(args, "budget", None),
               trial_budget=getattr(args, "trial_budget", None),
               income_budget=getattr(args, "income_budget", None),
               target_buys=getattr(args, "target_buys", None),
               bid_level=getattr(args, "bid_level", None),
               allocation_mode=getattr(args, "allocation_mode", None),
               allocation_weights=getattr(args, "allocation_weights", None),
               allocation_shadow_enabled=getattr(args, "allocation_shadow_enabled", None),
               round_interval_seconds=getattr(args, 'round_interval_seconds', None),
               api_retry_interval_seconds=getattr(args, 'api_retry_interval_seconds', None),
               order_check_interval_seconds=getattr(args, 'order_check_interval_seconds', None))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='open-trader prediction-arb lp-auto')
    add_arguments(parser)
    return run_namespace(parser.parse_args(argv))


def _config_payload(budget: str | None, target_buys: str | None, bid_level: str | None) -> dict[str, object]:
    try:
        amount = Decimal(budget)
        target = int(target_buys)
        level = int(bid_level) if bid_level is not None else None
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError('budget must be a finite nonnegative Decimal; target BUYs a nonnegative integer; bid level 1 or 2') from exc
    if not amount.is_finite() or amount < 0 or target < 0 or level is not None and level not in (1, 2):
        raise ValueError('budget must be a finite nonnegative Decimal; target BUYs a nonnegative integer; bid level 1 or 2')
    payload = {'budget_usd': str(amount), 'target_buy_count': target}
    if level is not None:
        payload['buy_price_level'] = level
    return payload


def _control_saved(state: dict[str, object], running: bool) -> bool:
    if not any(key in state for key in ('effective_running', 'control_save_state',
            'requested_control_version', 'saved_control_version', 'fatal')):
        return True  # Existing legacy HTTP contract has no D2 version fields.
    requested, saved = state.get('requested_control_version'), state.get('saved_control_version')
    return (type(requested) is int and requested >= 0 and type(saved) is int and saved == requested
        and state.get('control_save_state') == 'saved'
        and state.get('control_native_inflight') is False
        and 'fatal' in state and state['fatal'] is None
        and state.get('effective_running') is running)


def _pause_confirmed(state: dict[str, object]) -> bool:
    return (state.get('desired_running') is False and state.get('pause_confirmed') is True
            and state.get('runtime_state') in (None, 'paused') and _control_saved(state, False))


def _control_confirmed(state: dict[str, object], running: bool) -> bool:
    d2 = 'requested_control_version' in state or 'effective_running' in state
    settings_clear = all(state.get(key, None if d2 else False) is False
                         for key in ('settings_pending', 'settings_native_inflight'))
    if not settings_clear:
        return False
    if not running:
        return _pause_confirmed(state)
    return (state.get('desired_running') is True
            and state.get('pause_confirmed', None if d2 else False) is False
            and state.get('runtime_state') != 'paused' and _control_saved(state, True))


def _control_pending(state: dict[str, object], running: bool, version: object) -> bool:
    # Only a known accepted D2 intent can be followed. Missing safety facts,
    # failed saves and legacy unconfirmed receipts must stay fail-closed.
    return (type(version) is int and version >= 0
            and type(state.get('requested_control_version')) is int
            and state['requested_control_version'] == version
            and state.get('desired_running') is running
            and 'fatal' in state and state['fatal'] is None
            and state.get('control_save_state') in ('pending', 'saving', 'saved')
            and (state.get('control_save_state') in ('pending', 'saving')
                 or state.get('control_native_inflight') is True
                 or state.get('settings_pending') is True
                 or state.get('settings_native_inflight') is True))


def _config_confirmed(state: dict[str, object], payload: dict[str, object], previous: dict[str, object]) -> bool:
    from .polymarket_lp_accounting import ALLOCATION_FIELDS
    for key in ALLOCATION_FIELDS:
        if key in payload or key in previous:
            expected = payload.get(key, previous.get(key))
            if type(state.get(key)) is not type(expected) or state.get(key) != expected:
                return False
    if (state.get('settings_pending', False) is not False
            or state.get('settings_native_inflight', False) is not False
            or state.get('settings_error') is not None or state.get('fatal') is not None):
        return False
    if 'budget_usd' not in payload:
        return (type(state.get('config_version')) is int
            and state['config_version'] == payload['expected_config_version'] + 1
            and type(state.get('desired_running')) is bool
            and state['desired_running'] is previous.get('desired_running')
            and all(type(state.get(key)) is type(previous.get(key)) and state.get(key) == previous.get(key)
                    for key in ('budget_usd', 'target_buy_count', 'buy_price_level', 'pause_confirmed'))
            and all(type(state.get(key)) is int and state[key] == payload.get(key, previous.get(key, default))
                    for key, default in _TIMING_DEFAULTS.items()))
    budget = state.get('budget_usd')
    try:
        amount = Decimal(budget) if isinstance(budget, str) else Decimal('NaN')
    except InvalidOperation:
        return False
    level = payload.get('buy_price_level', previous.get('buy_price_level', 1))
    return (
        _pause_confirmed(state)
        and amount.is_finite() and amount == Decimal(payload['budget_usd'])
        and type(state.get('target_buy_count')) is int
        and state['target_buy_count'] == payload['target_buy_count']
        and type(state.get('buy_price_level')) is int and state['buy_price_level'] == level
        and type(state.get('config_version')) is int
        and state['config_version'] == payload['expected_config_version'] + 1
        and all(type(state.get(key)) is int and state[key] == value
                for key, value in payload.items() if key in _TIMING_DEFAULTS)
    )


def run(action: str, url: str, timeout: float | str, *, json_output: bool = False, verbose: bool = False,
        trial_budget: str | None = None, income_budget: str | None = None,
        budget: str | None = None, target_buys: str | None = None, bid_level: str | None = None,
        round_interval_seconds: str | None = None, api_retry_interval_seconds: str | None = None,
        order_check_interval_seconds: str | None = None,
        allocation_mode: str | None = None, allocation_weights: str | None = None,
        allocation_shadow_enabled: str | None = None) -> int:
    state = None
    client = None
    try:
        try:
            timeout = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError('timeout must be finite and positive') from exc
        payload = None
        if action == 'config':
            if round_interval_seconds is not None:
                raise ValueError('round_interval_seconds_retired_in_continuous_mode')
            dual = trial_budget is not None or income_budget is not None or allocation_mode == 'observed_pools'
            if dual:
                payload = {}
                for key,value in dict(budget_usd=budget,trial_budget_usd=trial_budget,income_budget_usd=income_budget).items():
                    if value is not None:
                        try:
                            amount = Decimal(value)
                        except InvalidOperation as exc:
                            raise ValueError('pool budget must be finite and nonnegative') from exc
                        if not amount.is_finite() or amount < 0:
                            raise ValueError('pool budget must be finite and nonnegative')
                        payload[key] = str(amount)
                if target_buys is not None: payload['target_buy_count'] = int(target_buys)
                if bid_level is not None: payload['buy_price_level'] = int(bid_level)
            else:
                payload = _config_payload(budget, target_buys, bid_level) if any(
                    value is not None for value in (budget, target_buys, bid_level)) else {}
            for key, value in dict(round_interval_seconds=round_interval_seconds,
                    api_retry_interval_seconds=api_retry_interval_seconds,
                    order_check_interval_seconds=order_check_interval_seconds).items():
                if value is None:
                    continue
                try:
                    seconds = int(value)
                except (ValueError, TypeError) as exc:
                    raise ValueError('timing intervals must be positive integer seconds') from exc
                if seconds <= 0:
                    raise ValueError('timing intervals must be positive integer seconds')
                payload[key] = seconds
            from .polymarket_lp_accounting import ALLOCATION_FIELDS, allocation_settings
            if allocation_mode is not None:
                payload['allocation_mode'] = allocation_mode
            if allocation_weights is not None:
                payload['allocation_weights'] = allocation_weights.split(',') if allocation_weights else []
            if allocation_shadow_enabled is not None:
                if allocation_shadow_enabled not in ('true', 'false'):
                    raise ValueError('allocation_shadow_enabled_invalid')
                payload['allocation_shadow_enabled'] = allocation_shadow_enabled == 'true'
            # Validate intrinsic values before transport; validate N after reading saved settings.
            intrinsic = {key: payload[key] for key in ALLOCATION_FIELDS if key in payload}
            if intrinsic and not dual:
                allocation_settings({**intrinsic, 'target_buy_count': payload.get('target_buy_count', len(intrinsic.get('allocation_weights', []))),
                    'allocation_shadow_enabled': False if allocation_weights is None else intrinsic.get('allocation_shadow_enabled', False)})
            if not payload:
                raise ValueError('provide trading configuration, allocation settings or a timing interval')
        client = _Client(url, timeout)
        if action == 'config':
            previous = client.request(ROOT + 'state')
            state = previous
            version = previous.get('config_version')
            if type(version) is not int or version < 0:
                raise ValueError('LP configuration version is unknown; no write was sent')
            if dual:
                from .polymarket_lp_accounting import pool_settings_changes
                payload = pool_settings_changes(previous,payload)
            if any(key in payload for key in ALLOCATION_FIELDS):
                allocation_settings({**previous, **payload})
            payload['expected_config_version'] = version
            state = client.write('config', payload)
            while dual and (state.get('settings_pending') is True or state.get('settings_native_inflight') is True):
                remaining = client.deadline-time.monotonic()
                if remaining <= 0: raise TimeoutError('LP configuration confirmation timed out')
                time.sleep(min(.01,remaining))
                state = client.request(ROOT+'state')
            if not _config_confirmed(state, payload, previous):
                raise ValueError('LP configuration result is unconfirmed')
            result = 'CONFIGURED'
        elif action == 'status':
            state = client.request(ROOT + 'state')
            result = 'STATUS'
        else:
            running = action == 'on'
            state = client.write('enable' if running else 'pause', {'confirm': True})
            version = state.get('requested_control_version')
            # GET exposes shared last-attempt metadata, which can belong to a
            # peer. Only this POST receipt can reject the originating request.
            origin_rejected = state.get('control_request_status') == 'rejected'
            label = 'enable' if running else 'pause'
            while True:
                if time.monotonic() >= client.deadline:
                    raise TimeoutError(f'LP {label} confirmation timed out')
                if origin_rejected:
                    client.rejected = True
                    raise ValueError(f'LP service rejected control: {state.get("control_request_reason") or "UNKNOWN"}')
                if _control_confirmed(state, running):
                    break
                if not _control_pending(state, running, version):
                    raise ValueError(f'LP {label} result is unconfirmed; check saved control and safety state')
                remaining = client.deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f'LP {label} confirmation timed out')
                time.sleep(min(.01, remaining))
                observed = client.request(ROOT + 'state')
                if (type(observed.get('requested_control_version')) is not int
                        or observed['requested_control_version'] != version
                        or observed.get('desired_running') is not running):
                    raise ValueError(f'LP {label} result is unconfirmed; control version or intent changed')
                state = observed
            result = 'ON' if running else 'PAUSED'
    except (OSError, ValueError, OverflowError, HTTPException) as exc:
        reason = type(exc).__name__ if isinstance(exc, HTTPException) else f'{type(exc).__name__}: {exc}'
        if client is not None:
            state = client.redact(state)
            reason = client.redact(reason)
        _emit('UNKNOWN', state, reason, json_output, verbose=verbose,
              definite_failure=client is None or not client.write_attempted or client.rejected)
        return 2
    _emit(result, client.redact(state), None, json_output, verbose=verbose)
    return 0
