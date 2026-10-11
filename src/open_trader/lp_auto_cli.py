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


def _emit(result: str, state: dict[str, object] | None, reason: str | None, json_output: bool) -> None:
    if json_output:
        print(json.dumps({'result': result, 'state': state, 'reason': reason,
                          'next_action': _STATUS_ADVICE if result == 'UNKNOWN' else None}, ensure_ascii=False))
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
    for key in ('runtime_state', 'budget_usd', 'target_buy_count', 'buy_price_level', 'config_version', *_TIMING_DEFAULTS):
        value = state.get(key)
        if key == 'runtime_state' and isinstance(value, str) and result != 'UNKNOWN':
            value = value.upper()
        print(f'{key}: {value if value is not None else "UNKNOWN"}')
    slots = state.get('slots')
    active = slots.get('active') if isinstance(slots, dict) else None
    target = state.get('target_buy_count')
    print(f'buy_orders: {active if active is not None else "UNKNOWN"}/'
          f'{target if target is not None else "UNKNOWN"}')
    for group, keys in (
        ('slots', ('active', 'pending', 'canceling', 'occupied')),
        ('funds', ('inventory_cost_usd', 'buy_reserved_usd', 'pending_reserved_usd', 'available_usd', 'spendable_usd')),
    ):
        values = state.get(group)
        values = values if isinstance(values, dict) else {}
        for key in keys:
            value = values.get(key)
            print(f'{key}: {value if value is not None else "UNKNOWN"}')
    for key in ('block_reasons', 'admission_block_reasons', 'reason', 'last_check_at', 'last_check_error'):
        value = state.get(key)
        if value is None and key in state and key in ('reason', 'last_check_error'):
            value = 'NONE'
        print(f'{key}: {value if value is not None else "UNKNOWN"}')
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
              getattr(self, '_json_output', False))
        raise SystemExit(2)


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


def _pause_confirmed(state: dict[str, object]) -> bool:
    return (state.get('desired_running') is False and state.get('pause_confirmed') is True
            and state.get('runtime_state') in (None, 'paused'))


def _config_confirmed(state: dict[str, object], payload: dict[str, object], previous: dict[str, object]) -> bool:
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


def run(action: str, url: str, timeout: float | str, *, json_output: bool = False,
        budget: str | None = None, target_buys: str | None = None, bid_level: str | None = None,
        round_interval_seconds: str | None = None, api_retry_interval_seconds: str | None = None,
        order_check_interval_seconds: str | None = None) -> int:
    state = None
    client = None
    try:
        try:
            timeout = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError('timeout must be finite and positive') from exc
        payload = None
        if action == 'config':
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
            if not payload:
                raise ValueError('provide trading configuration or at least one timing interval')
        client = _Client(url, timeout)
        if action == 'config':
            previous = client.request(ROOT + 'state')
            state = previous
            version = previous.get('config_version')
            if type(version) is not int or version < 0:
                raise ValueError('LP configuration version is unknown; no write was sent')
            payload['expected_config_version'] = version
            state = client.write('config', payload)
            if not _config_confirmed(state, payload, previous):
                raise ValueError('LP configuration result is unconfirmed')
            result = 'CONFIGURED'
        elif action == 'on':
            state = client.write('enable', {'confirm': True})
            if (state.get('desired_running') is not True
                or state.get('pause_confirmed', False) is not False
                or state.get('runtime_state') == 'paused'):
                raise ValueError('LP enable result is unconfirmed')
            result = 'ON'
        elif action == 'status':
            state = client.request(ROOT + 'state')
            result = 'STATUS'
        else:
            state = client.write('pause', {'confirm': True})
            if not _pause_confirmed(state):
                raise ValueError('LP pause result is unconfirmed')
            result = 'PAUSED'
    except (OSError, ValueError, OverflowError, HTTPException) as exc:
        reason = type(exc).__name__ if isinstance(exc, HTTPException) else f'{type(exc).__name__}: {exc}'
        if client is not None:
            state = client.redact(state)
            reason = client.redact(reason)
        _emit('UNKNOWN', state, reason, json_output)
        return 2
    _emit(result, client.redact(state), None, json_output)
    if result == 'PAUSED' and not json_output:
        print('new automatic BUYs paused; existing orders and protection remain')
    return 0
