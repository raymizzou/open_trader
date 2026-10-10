"""Manually invoked, one-order Polymarket diagnostic. No automatic trading."""
from __future__ import annotations

import argparse
import json
import fcntl
import stat
import time
import uuid
import os
import re
import signal
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx
from polymarket.errors import RequestRejectedError

from .polymarket_trading import GEOBLOCK_URL, PolymarketTradingClient, load_trading_config


class ProbeError(Exception):
    def __init__(self, reason: str, result: str = 'UNKNOWN'):
        self.reason = reason
        self.result = result


@contextmanager
def deadline(seconds=5.0):
    """Hard wall-clock bound, including paginated SDK reads (standalone POSIX CLI)."""
    def expired(*_):
        raise ProbeError('operation_deadline')
    previous = signal.signal(signal.SIGALRM, expired)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous)


@contextmanager
def backend(args):
    values = {'OPEN_TRADER_CREDENTIAL_BACKEND': args.credential_backend}
    if args.credential_backend == 'file':
        if args.credentials_file is None:
            raise ProbeError('credentials_file_required', 'BLOCKED')
        values['OPEN_TRADER_CREDENTIAL_FILE'] = str(args.credentials_file)
    if args.credential_backend == 'tencent-ssm' and any(
        not os.environ.get('OPEN_TRADER_SSM_' + key)
        for key in ('REGION', 'SECRET', 'VERSION', 'ROLE')
    ):
        raise ProbeError('ssm_references_incomplete', 'BLOCKED')
    old = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def region():
    try:
        with deadline(), httpx.Client(timeout=5) as http:
            payload = http.get(GEOBLOCK_URL).raise_for_status().json()
        blocked = payload.get('blocked')
        if type(blocked) is not bool:
            return {'status': 'unknown'}
        result = {'status': 'blocked' if blocked else 'allowed', 'blocked': blocked}
        for key, pattern in [('country', r'[A-Z]{2}'), ('region', r'[A-Za-z0-9-]{1,16}')]:
            value = payload.get(key)
            if isinstance(value, str) and re.fullmatch(pattern, value):
                result[key] = value
        return result
    except Exception:
        return {'status': 'unknown'}


def facts(client, token):
    started = time.monotonic()
    with deadline():
        account = client._lp_account_facts(include_raw_trades=True)
    complete = all(account.get(key) is True for key in (
        'authenticated', 'balance_complete', 'open_orders_complete', 'positions_complete', 'trades_complete'))
    if not complete or account.get('balance') is None or account.get('allowance') is None:
        raise ProbeError('account_incomplete')
    with deadline():
        book = client._client.get_order_book(token_id=token)
        if str(book.token_id) != token:
            raise ProbeError('book_token_mismatch')
        market = client._client._ctx.clob.get_json('/markets/' + str(book.condition_id))
        server = client._client._ctx.clob.get_json('/time')
    if time.monotonic() - started > 10:
        raise ProbeError('account_or_market_stale')
    return account, book, market, server


def inputs(args):
    try:
        price, quantity = Decimal(args.price), Decimal(args.quantity)
        if not price.is_finite() or not quantity.is_finite() or not 0 < price < 1 or quantity <= 0:
            raise ValueError
    except (InvalidOperation, ValueError):
        raise ProbeError('invalid_input', 'BLOCKED') from None
    if price * quantity > Decimal('1'):
        raise ProbeError('budget_exceeded', 'BLOCKED')
    if args.command == 'run':
        if not args.confirm_live:
            raise ProbeError('live_confirmation_required', 'BLOCKED')
        if not args.confirm_single_writer:
            raise ProbeError('writer_confirmation_required', 'BLOCKED')
    return price, quantity


def validate_market(price, quantity, book, market, server, account):
    if (type(server) is not int or book.timestamp is None or str(book.token_id) == ''
            or not isinstance(market, dict) or market.get('condition_id') != str(book.condition_id)
            or not any(isinstance(t, dict) and t.get('token_id') == str(book.token_id)
                       for t in market.get('tokens', []))):
        raise ProbeError('market_unknown')
    if not 0 <= server - book.timestamp.timestamp() <= 10:
        raise ProbeError('market_stale')
    if market.get('accepting_orders') is False or market.get('closed') is True or market.get('active') is False:
        raise ProbeError('market_not_accepting', 'BLOCKED')
    if (market.get('accepting_orders') is not True or market.get('closed') is not False
            or market.get('active') is not True or not book.asks or not book.bids
            or not book.tick_size.is_finite() or book.tick_size <= 0
            or not book.min_order_size.is_finite() or book.min_order_size <= 0):
        raise ProbeError('market_unknown')
    if price % book.tick_size != 0 or not book.tick_size <= price <= 1 - book.tick_size:
        raise ProbeError('price_off_tick', 'BLOCKED')
    if quantity < book.min_order_size:
        raise ProbeError('quantity_below_minimum', 'BLOCKED')
    if price >= min(level.price for level in book.asks):
        raise ProbeError('price_crosses_ask', 'BLOCKED')
    if account['balance'] < price * quantity:
        raise ProbeError('balance_insufficient', 'BLOCKED')
    if account['allowance'] < price * quantity:
        raise ProbeError('allowance_insufficient', 'BLOCKED')


class Receipt:
    """Private local receipt with atomic durable replacement and same-path exclusion."""
    def __init__(self, path):
        self.path = path
        self.dirfd = None
        self.lockfd = None
        self.data = None

    @staticmethod
    def private_file(info):
        return (stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1)

    def __enter__(self):
        try:
            if not self.path.is_absolute():
                raise ValueError
            for part in (self.path.parent, *self.path.parent.parents):
                if stat.S_ISLNK(part.lstat().st_mode):
                    raise ValueError
            info = self.path.parent.lstat()
            if (info.st_uid != os.geteuid() or not stat.S_ISDIR(info.st_mode)
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise ValueError
            self.dirfd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            opened = os.fstat(self.dirfd)
            if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError
            self.lockfd = os.open(self.path.name + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                                  0o600, dir_fd=self.dirfd)
            if not self.private_file(os.fstat(self.lockfd)):
                raise ValueError
            fcntl.flock(self.lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                fd = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.dirfd)
            except FileNotFoundError:
                return self
            with os.fdopen(fd, 'rb') as file:
                if not self.private_file(os.fstat(file.fileno())):
                    raise ValueError
                raw = file.read(16385)
            if len(raw) > 16384:
                raise ValueError
            self.data = json.loads(raw)
            if not isinstance(self.data, dict):
                raise ValueError
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise ProbeError('unsafe_or_unavailable_record', 'BLOCKED') from None

    def save(self, data):
        name = '.' + self.path.name + '.' + uuid.uuid4().hex
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=self.dirfd)
            with os.fdopen(fd, 'w') as file:
                json.dump(data, file, sort_keys=True)
                file.flush()
                os.fsync(file.fileno())
            os.replace(name, self.path.name, src_dir_fd=self.dirfd, dst_dir_fd=self.dirfd)
            os.fsync(self.dirfd)
            self.data = dict(data)
        except Exception:
            raise ProbeError('record_write_failed') from None
        finally:
            try:
                os.unlink(name, dir_fd=self.dirfd)
            except FileNotFoundError:
                pass

    def __exit__(self, *_):
        if self.lockfd is not None:
            os.close(self.lockfd)
            self.lockfd = None
        if self.dirfd is not None:
            os.close(self.dirfd)
            self.dirfd = None


def own_order(order, data):
    return (order.id == data['order_id'] and str(order.token_id) == data['token']
            and str(order.condition_id) == data['condition']
            and order.maker_address.lower() == data['wallet'].lower()
            and order.side == 'BUY' and order.order_type.upper() == 'GTD'
            and order.price == Decimal(data['price'])
            and order.original_size == Decimal(data['quantity'])
            and order.expires_at is not None
            and int(order.expires_at.timestamp()) == data['expiration'])


def target_position(account, token):
    total = Decimal('0')
    for row in account['positions']:
        if row.get('token_id') != token:
            continue
        size = row.get('size')
        if not isinstance(size, Decimal) or not size.is_finite() or size < 0:
            raise ProbeError('target_position_unknown')
        total += size
    return total


def trade_fills(account, data):
    quantity, notional = Decimal('0'), Decimal('0')
    seen = {}
    for trade in account['raw_trades']:
        payload = trade.model_dump()
        if trade.id in seen:
            if seen[trade.id] != payload:
                raise ProbeError('trade_history_conflict')
            continue
        seen[trade.id] = payload
        for maker in trade.maker_orders:
            if maker.order_id != data['order_id']:
                continue
            if (str(trade.condition_id) != data['condition'] or str(maker.token_id) != data['token']
                    or maker.maker_address.lower() != data['wallet'].lower() or maker.side != 'BUY'
                    or not maker.matched_amount.is_finite() or maker.matched_amount <= 0
                    or not maker.price.is_finite() or not 0 < maker.price <= Decimal(data['price'])):
                raise ProbeError('trade_identity_unknown')
            if trade.status != 'CONFIRMED':
                raise ProbeError('fill_settlement_unknown')
            quantity += maker.matched_amount
            notional += maker.matched_amount * maker.price
    if quantity > Decimal(data['quantity']):
        raise ProbeError('trade_quantity_conflict')
    return quantity, notional


def reconcile(client, data, report, *, cancel=False):
    """Read at most once per phase; cancel only this submission's explicit ID."""
    stop = time.monotonic() + 30
    def bounded(call):
        remaining = stop - time.monotonic()
        if remaining <= 0:
            raise ProbeError('reconciliation_deadline')
        with deadline(min(5, remaining)):
            return call()
    order_id = data['order_id']
    if not order_id:
        raise ProbeError('order_id_unknown')
    observed = None
    read_failed = False
    try:
        observed = bounded(lambda: client._client.get_order(order_id=order_id))
    except Exception:
        read_failed = True
    if observed is not None and not own_order(observed, data):
        raise ProbeError('order_identity_mismatch')
    if observed is not None:
        report['live_observed'] = data.get('live_observed', False) or observed.status.upper() == 'LIVE'
        report['identity_verified'] = True
    if cancel:
        # An unreadable response does not remove the explicit ID returned by our POST.
        # An observed identity mismatch above prevents DELETE.
        try:
            ack = bounded(lambda: client.cancel_orders_detailed((order_id,)))
            report['cancel_acknowledged'] = order_id in ack['canceled']
        except Exception:
            report['cancel_acknowledged'] = False
    else:
        report['cancel_acknowledged'] = data.get('cancel_acknowledged', False)
    terminal = bounded(lambda: client._client.get_order(order_id=order_id))
    if not own_order(terminal, data):
        raise ProbeError('order_identity_mismatch')
    account = bounded(lambda: client._lp_account_facts(include_raw_trades=True))
    complete = all(account.get(key) is True for key in (
        'authenticated', 'balance_complete', 'open_orders_complete', 'positions_complete', 'trades_complete'))
    report['terminal_status'] = terminal.status.upper()
    trade_quantity, trade_notional = trade_fills(account, data)
    matched = terminal.size_matched
    if not matched.is_finite() or not 0 <= matched <= Decimal(data['quantity']):
        raise ProbeError('order_fill_unknown')
    filled = max(matched, trade_quantity, Decimal(data.get('filled_quantity', '0')))
    report['order_matched_quantity'] = str(matched)
    report['trade_filled_quantity'] = str(trade_quantity)
    report['fill_discrepancy'] = matched != trade_quantity
    report['filled_quantity'] = str(filled)
    report['filled_notional'] = str(trade_notional if trade_quantity >= filled else filled * Decimal(data['price']))
    position_before = Decimal(data['position_before'])
    position_after = target_position(account, data['token'])
    report['position_before'] = str(position_before)
    report['position_after'] = str(position_after)
    report['funds_reconciled'] = (complete and position_before == position_after and account['balance'] == Decimal(data['balance_before'])
        and account['allowance'] == Decimal(data['allowance_before'])
        and all(row.get('order_id') != order_id for row in account['open_orders']))
    passed = (not read_failed and report.get('live_observed') is True
              and report['cancel_acknowledged'] is True
              and report['terminal_status'] in ('CANCELED', 'CANCELLED', 'EXPIRED')
              and filled == 0 and report['funds_reconciled'])
    report['result'] = ('FILLED' if trade_quantity == Decimal(data['quantity']) else 'PARTIAL') if trade_quantity > 0 else ('PASS' if passed else 'UNKNOWN')
    if position_before != position_after and trade_quantity == 0:
        report['reason'] = 'target_position_changed'
    report['order_validation'] = 'VERIFIED' if passed else 'UNKNOWN'


def run(client, args, config, account, book, server, price, quantity, report, started):
    with Receipt(args.record) as receipt:
        if receipt.data is not None:
            raise ProbeError('existing_record_requires_status')
        if type(server) is not int or server <= 0:
            raise ProbeError('server_time_unknown')
        expiration = server + 240
        data = dict(version=1, wallet=config.wallet_address, signer=config.signer_address,
                    token=args.token, condition=str(book.condition_id), price=str(price), quantity=str(quantity),
                    expiration=expiration, attempted=False, order_id=None, result='UNKNOWN',
                    balance_before=str(account['balance']), allowance_before=str(account['allowance']),
                    position_before=str(target_position(account, args.token)),
                    credential_backend=args.credential_backend, single_writer='manual_confirmation')
        receipt.save(data)
        with deadline():
            signed = client.lp_create_limit_order(token_id=args.token, price=price, quantity=quantity,
                    side='BUY', post_only=True, expiration=expiration)
        # Integer cross-products retain every requested digit, independent of Decimal precision.
        price_numerator, price_denominator = price.as_integer_ratio()
        quantity_numerator, quantity_denominator = quantity.as_integer_ratio()
        if (signed.maker_amount * price_denominator * quantity_denominator
                != price_numerator * quantity_numerator * 1000000
                or signed.taker_amount * quantity_denominator != quantity_numerator * 1000000
                or str(signed.token_id) != args.token or signed.side != 'BUY'
                or signed.expiration != expiration or signed.order_type != 'GTD' or signed.post_only is not True):
            raise ProbeError('signed_order_mismatch', 'BLOCKED')
        with deadline():
            now = client._client._ctx.clob.get_json('/time')
        if type(now) is not int or expiration - now < 180:
            raise ProbeError('expiration_too_close', 'BLOCKED')
        if not 0 <= now - book.timestamp.timestamp() <= 10:
            raise ProbeError('market_stale')
        if time.monotonic() - started > 10:
            raise ProbeError('account_or_market_stale')
        data['attempted'] = True
        receipt.save(data)  # Durable before POST. Failure prevents submission.
        submit_facts = {}
        def observe(response):
            if response.request.method != 'POST' or response.request.url.path != '/order':
                return
            submit_facts['http_status'] = response.status_code
            try:
                response.read()
                raw = response.json()
                if isinstance(raw, dict):
                    # Keep only classification, never raw error text or headers.
                    submit_facts['explicit_rejection'] = raw.get('success') is False or bool(raw.get('errorMsg'))
            except Exception:
                pass
        hooks = client._client._ctx.secure_clob._client.event_hooks['response']
        hooks.append(observe)
        try:
            try:
                with deadline():
                    response = client.lp_post_order(signed)
            except RequestRejectedError as error:
                report['submit_http_status'] = error.status
                raise ProbeError('venue_rejected' if 400 <= error.status < 500 else 'submit_unknown',
                                 'REJECTED' if 400 <= error.status < 500 else 'UNKNOWN') from None
            order_id = getattr(response, 'order_id', None)
            if submit_facts.get('explicit_rejection'):
                raise ProbeError('venue_rejected', 'REJECTED')
            if not getattr(response, 'ok', False) or not isinstance(order_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', order_id):
                raise ProbeError('submit_unknown')
            data['order_id'] = order_id
            report['order_id'] = order_id
            write_failed = False
            try:
                receipt.save(data)
            except ProbeError:
                # Keep the accepted ID in memory and clean it up even when audit storage fails.
                write_failed = True
            try:
                reconcile(client, data, report, cancel=True)
            finally:
                if write_failed:
                    report.update(result='UNKNOWN', reason='record_write_failed', order_validation='UNKNOWN')
        except ProbeError as error:
            report.update(result=error.result, reason=error.reason)
        except Exception:
            report.update(result='UNKNOWN', order_validation='UNKNOWN', reason='submit_or_reconciliation_unknown')
        finally:
            hooks.remove(observe)
        data.update(report)
        receipt.save(data)


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest='command', required=True)
    for name in ('check', 'run', 'status', 'cancel'):
        command = commands.add_parser(name)
        command.add_argument('--config', type=Path, required=True)
        command.add_argument('--credential-backend', choices=('file', 'keychain', 'tencent-ssm'), required=True)
        command.add_argument('--credentials-file', type=Path)
        if name in ('check', 'run'):
            command.add_argument('--token', required=True)
            command.add_argument('--price', required=True)
            command.add_argument('--quantity', required=True)
        if name != 'check':
            command.add_argument('--record', type=Path, required=True)
        if name in ('run', 'cancel'):
            command.add_argument('--confirm-live', action='store_true')
            command.add_argument('--confirm-single-writer', action='store_true')
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    report = {'result': 'UNKNOWN', 'order_validation': 'UNVERIFIED',
              'credential_backend': args.credential_backend,
              'single_writer': 'manual_confirmation' if getattr(args, 'confirm_single_writer', False) else 'unconfirmed'}
    client = None
    try:
        with backend(args):
            if args.command in ('check', 'run'):
                price, quantity = inputs(args)
            config = load_trading_config(args.config)
            if args.command != 'check':
                with Receipt(args.record) as receipt:
                    if receipt.data is not None:
                        data = receipt.data
                        if (data.get('wallet', '').lower() != config.wallet_address.lower()
                                or data.get('signer', '').lower() != config.signer_address.lower()
                                or data.get('credential_backend') != args.credential_backend):
                            raise ProbeError('record_identity_mismatch', 'BLOCKED')
                        if args.command == 'run':
                            if (data.get('token') != args.token or Decimal(data.get('price', 'NaN')) != price
                                    or Decimal(data.get('quantity', 'NaN')) != quantity):
                                raise ProbeError('record_identity_mismatch', 'BLOCKED')
                            result = 'REJECTED' if data.get('result') == 'REJECTED' else 'UNKNOWN'
                            raise ProbeError('attempted_record_requires_status', result)
                    elif args.command in ('status', 'cancel'):
                        raise ProbeError('record_missing', 'BLOCKED')
            with deadline():
                client = PolymarketTradingClient.from_keychain(config, read_only=True)
            report['region'] = region()
            if args.command in ('check', 'run'):
                started = time.monotonic()
                account, book, market, server = facts(client, args.token)
                validate_market(price, quantity, book, market, server, account)
                report['input_validation'] = 'valid'
                report['notional'] = str(price * quantity)
                report['account'] = {'status': 'ready', 'balance': str(account['balance']), 'allowance': str(account['allowance'])}
                report['market'] = {'tick': str(book.tick_size), 'minimum': str(book.min_order_size)}
                report['result'] = 'CHECKED'
                if args.command == 'run':
                    run(client, args, config, account, book, server, price, quantity, report, started)
            else:
                with Receipt(args.record) as receipt:
                    data = receipt.data
                    if not data.get('order_id'):
                        raise ProbeError('order_id_unknown', 'REJECTED' if data.get('result') == 'REJECTED' else 'UNKNOWN')
                    if args.command == 'cancel' and (not args.confirm_live or not args.confirm_single_writer):
                        raise ProbeError('cancel_confirmation_required', 'BLOCKED')
                    try:
                        reconcile(client, data, report, cancel=args.command == 'cancel')
                    except ProbeError as error:
                        report.update(result=error.result, reason=error.reason)
                    except Exception:
                        report.update(result='UNKNOWN', reason='reconciliation_unknown')
                    data.update(report)
                    receipt.save(data)
    except ProbeError as error:
        report.update(result=error.result, reason=error.reason)
    except Exception:
        report.update(result='UNKNOWN', reason='external_or_local_failure')
    finally:
        if client is not None:
            try:
                with deadline():
                    client.close()
            except Exception:
                pass
    print(json.dumps(report, sort_keys=True))
    return 0 if report['result'] in ('CHECKED', 'PASS') else 2


if __name__ == '__main__':
    raise SystemExit(main())
