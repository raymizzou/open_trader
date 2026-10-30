"""Read-only LP observations. Timers are process-local evidence, never authority."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from collections.abc import Mapping
import hashlib
import math
from threading import RLock
from zoneinfo import ZoneInfo

TOKYO = ZoneInfo('Asia/Tokyo')
SECTIONS = ('orders', 'cancellations', 'pools', 'balance', 'fills')


def moment(value):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.astimezone(UTC) if result.utcoffset() is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def number(value):
    try:
        result = Decimal(str(value)) if not isinstance(value, bool) else None
        return result if result is not None and result.is_finite() and result >= 0 else None
    except InvalidOperation:
        return None


def mapping(value):
    return value if isinstance(value, Mapping) else {}


def section(status='unknown', reason='数据未读全，当前情况未知', **values):
    return dict(status=status, reason=reason, **values)


def read_health(auto, now):
    """Copy only the bounded public cleanup schema; absent evidence is unknown."""
    unknown = section(reason='读取清理状态未知', cleanup_fault_details=None, new_buy_block_reasons=None)
    runtime = mapping(mapping(auto.get('diagnostics')).get('async_reads'))
    details = mapping(runtime.get('cleanup_fault_details'))
    total, omitted, items = (details.get(key) for key in ('total', 'omitted', 'items'))
    reasons = runtime.get('new_buy_block_reasons')
    if (type(total) is not int or type(omitted) is not int or min(total, omitted) < 0
            or not isinstance(items, (list, tuple)) or len(items) > 16 or total != omitted+len(items)
            or type(runtime.get('cleanup_faults')) is not int or runtime['cleanup_faults'] != total
            or not isinstance(reasons, (list, tuple)) or len(reasons) > 3
            or any(type(r) is not str or r not in ('cleanup_fault', 'capacity', 'deferred') for r in reasons)
            or len(set(reasons)) != len(reasons) or type(runtime.get('new_buy_allowed')) is not bool
            or runtime['new_buy_allowed'] != (not reasons)):
        return unknown
    copied = []
    for raw in items:
        row = mapping(raw)
        lane, kind, reason = (row.get(key) for key in ('lane', 'kind', 'reason'))
        since = moment(row.get('since'))
        if (lane not in ('buy', 'financial', 'exact', 'low','reward_share')
                or kind not in dict(buy=('buy','income'), financial=('financial',), exact=('exact',),
                    low=('sell', 'position', 'holding', 'unknown'),reward_share=('reward_share',)).get(lane, ())
                or reason not in ('delayed_read_cleanup', 'port_close_failure')
                or row.get('active') is not True or type(row.get('blocks_new_buy')) is not bool
                or row['blocks_new_buy'] != (lane not in ('buy','reward_share')) or since is None or since > now):
            return unknown
        copied.append(dict(lane=lane, kind=kind, reason=reason, since=since.isoformat(),
            active=True, blocks_new_buy=row['blocks_new_buy']))
    if any(row['blocks_new_buy'] for row in copied) and 'cleanup_fault' not in reasons:
        return unknown
    blocked = 'cleanup_fault' in reasons
    reason = ('读取清理故障，暂停新增 BUY' if blocked else '读取容量已满，暂停新增 BUY' if 'capacity' in reasons
        else '读取需求排队，暂停新增 BUY' if 'deferred' in reasons else '已有 BUY 读取清理故障' if total
        else '读取清理正常')
    return section('abnormal' if blocked else 'attention' if total or reasons else 'normal', reason,
        cleanup_fault_details=dict(total=total, omitted=omitted, items=copied), new_buy_block_reasons=list(reasons))


def financial_timeout_reason(auto, account_id, now):
    """An active runtime event streak, bound by the current live owner.

    Elapsed time is observed by the runtime. Never age a historical snapshot
    into an outage or infer health from an old timeout record alone.
    """
    runtime=mapping(mapping(auto.get('diagnostics')).get('async_reads'))
    streak=mapping(runtime.get('financial_timeout_streak'))
    expected=mapping(runtime.get('financial_identity'))
    identity=streak.get('identity')
    fingerprint=streak.get('account_fingerprint')
    observed=moment(streak.get('observed_at'))
    elapsed=streak.get('elapsed_seconds')
    count=streak.get('timeouts')
    member=mapping(mapping(auto.get('observed_pools')).get('current_membership'))
    if (type(account_id) is not str or not account_id
            or type(identity) is not str or len(identity)!=64 or any(c not in '0123456789abcdef' for c in identity)
            or fingerprint!=hashlib.sha256(account_id.casefold().encode()).hexdigest()
            or identity!=expected.get('identity') or fingerprint!=expected.get('account_fingerprint')
            or member.get('account_id',account_id)!=account_id
            or streak.get('state')!='timed_out' or type(count) is not int or count<1
            or type(elapsed) not in (int,float) or not math.isfinite(elapsed) or elapsed<0
            or observed is None or not 0<=(now-observed).total_seconds()<=60):return None
    if elapsed>60:return f'账户挂单/余额读取卡住 {max(1,int(elapsed//60))} 分钟'
    return None


def unknown_summary(now=None):
    result = dict(as_of=(now or datetime.now(UTC)).isoformat(), timezone='Asia/Tokyo',
                  **{key: section() for key in SECTIONS})
    result['orders'].update(counts=None, items=[])
    result['pools'].update(items={})
    result['balance'].update(balance_usd=None, allowance_usd=None, capacity_usd=None,
                             checked_at=None, inventory_cost_usd=None)
    result['fills'].update(hour=dict(count=None, amount_usd=None),
                          today=dict(count=None, amount_usd=None), items=[], inventory_cost_usd=None)
    result['cancellations'].update(hour=dict.fromkeys(('success','rejected','unknown','blocked')),
        today=dict.fromkeys(('success','rejected','unknown','blocked')), items=[], stuck=[])
    result['holdings'] = section(items=[], position_quantity=None, covered_quantity=None,
        uncovered_quantity=None, reasoned_quantity=None)
    return result


class LPMonitorSummary:
    def __init__(self):
        self.lock = RLock()
        self.since = {}
        self.shortages = {}
        self.account_id = None
        self.last_counts = None

    def duration(self, key, active, now):
        if not active:
            self.since.pop(key, None)
            return 0
        start = self.since.setdefault(key, now)
        if now < start:
            self.since[key] = now
            return 0
        return (now - start).total_seconds()

    def shortage_duration(self, pool, account, known, lacking, stamp, now):
        if not known:
            return None  # one UNKNOWN cannot disprove an observed shortage
        if not lacking:
            self.shortages.pop(pool, None)
            return None
        previous = self.shortages.get(pool)
        if previous is None or previous[0] != account or now < previous[1]:
            self.shortages[pool] = (account, now, stamp, False)
            return None
        owner, start, first_stamp, confirmed = previous
        confirmed = confirmed or stamp > first_stamp
        self.shortages[pool] = (owner, start, first_stamp, confirmed)
        return (now-start).total_seconds() if confirmed else None

    def summarize(self, *, auto, dashboard, trades, audits, account_id, now, holdings=None):
        with self.lock:
            result = unknown_summary(now)
            if isinstance(holdings, Mapping):
                result['holdings'] = dict(holdings)
            auto, dashboard, trades = mapping(auto), mapping(dashboard), mapping(trades)
            membership = mapping(mapping(auto.get('observed_pools')).get('current_membership'))
            stamp = moment(membership.get('observed_at'))
            rows = membership.get('orders')
            known = (membership.get('state') == 'known' and account_id
                and membership.get('account_id') == account_id and stamp is not None
                and 0 <= (now-stamp).total_seconds() <= 60 and isinstance(rows, list)
                and all(isinstance(r, dict) and r.get('account_id') == account_id
                    and all(isinstance(r.get(k),str) and r[k] for k in ('condition_id', 'token_id', 'order_id'))
                    and r.get('side') in ('BUY', 'SELL')
                    and r.get('pool') in ('trial','income','unclassified','promotion') for r in rows)
                and len({r['order_id'] for r in rows}) == len(rows))
            if self.account_id != account_id:
                self.account_id = account_id
                self.last_counts = None
                self.shortages.clear()
            counts = dict(trial=0, income=0, unassigned=0, sell=0) if known else None
            items = []
            if known:
                seen = set()
                for row in rows:
                    identity = tuple(row[k] for k in ('account_id', 'condition_id', 'token_id', 'order_id'))
                    if identity in seen:
                        continue
                    seen.add(identity)
                    pool = row.get('pool') if row.get('pool') in ('trial', 'income') else 'unassigned'
                    counts['sell' if row['side'] == 'SELL' else pool] += 1
                    items.append({k: row.get(k) for k in ('condition_id', 'token_id', 'order_id', 'side', 'pool')})
            if known:
                self.last_counts = (dict(counts), stamp)
            confirmed_at = self.last_counts[1].isoformat() if self.last_counts else None
            display_counts = counts if known else dict(self.last_counts[0]) if self.last_counts else None
            unconfirmed_reason = None if known else (
                'account_identity_mismatch' if membership.get('account_id') != account_id else
                'membership_stale' if stamp is not None and not 0 <= (now-stamp).total_seconds() <= 60 else
                membership.get('reason') or 'membership_unconfirmed')
            pools = {}
            shortage = False
            for pool in ('trial', 'income'):
                target = mapping(auto.get('effective_settings', auto)).get(pool+'_target_buy_count')
                valid_target = type(target) is int and target >= 0
                count = display_counts[pool] if display_counts is not None else None
                lacking = known and valid_target and count < target
                age = self.shortage_duration(pool, account_id, known and valid_target, lacking, stamp, now)
                state = ('abnormal' if age is not None and age >= 300 else 'attention') if lacking else 'normal'
                if not known or not valid_target:
                    state = 'unknown'
                shortage |= lacking
                budget = number(mapping(auto.get('effective_settings')).get(pool+'_budget_usd'))
                members = [r for r in rows if r.get('pool')==pool and r.get('side')=='BUY'] if known else []
                amounts = [number(r.get('price')) * number(r.get('remaining'))
                    if number(r.get('price')) is not None and number(r.get('remaining')) is not None
                    else None for r in members]
                used = sum(amounts, Decimal(0)) if known and all(v is not None for v in amounts) else None
                changed = [moment(r.get('last_change_at')) for r in members]
                changed = [v for v in changed if v is not None and v <= now]
                pools[pool] = dict(count=count, target=target if valid_target else None,
                    status=state, shortage_seconds=age if lacking else None,
                    budget_usd=str(budget) if budget is not None else None,
                    used_usd=str(used) if used is not None else None,
                    last_change_at=max(changed).isoformat() if changed else None,
                    last_change_reason='当前角色的原始变更时间' if changed else '既有来源未保存可归属的补位或换单时间')
            state = 'unknown' if not known or any(p['status']=='unknown' for p in pools.values()) else (
                'abnormal' if any(p['status']=='abnormal' for p in pools.values()) else
                'attention' if shortage else 'normal')
            reason = {'normal':'挂单已达到当前配置目标', 'attention':'池内缺单，正在等待补位',
                      'abnormal':'池内连续五分钟缺单', 'unknown':'当前挂单或池归属未确认'}[state]
            result['orders'] = section(state, reason, counts=display_counts, items=items, observed_at=stamp.isoformat() if stamp else None,
                confirmed_at=confirmed_at, counts_basis='current' if known else 'last_confirmed' if self.last_counts else 'unknown',
                unconfirmed_reason=unconfirmed_reason)
            candidate_count = dashboard.get('candidate_valid_count')
            candidate_stamp = moment(dashboard.get('candidate_last_success_at'))
            candidate_source = (account_id and dashboard.get('account_id') == account_id
                and type(candidate_count) is int and candidate_count >= 0 and candidate_stamp is not None
                and candidate_stamp <= now)
            failed = candidate_source and (dashboard.get('candidate_stale') is True
                or dashboard.get('candidate_state') in ('stale','failed','error'))
            failure_age = self.duration('candidate_failure', failed, now)
            pool_state, pool_reason = state, reason
            health = read_health(auto, now)
            timeout_reason = (financial_timeout_reason(auto, account_id, now)
                if not auto.get('fatal') and auto.get('desired_running') is True
                and auto.get('effective_running') is True else None)
            if timeout_reason:
                result['orders'].update(status='abnormal',reason=timeout_reason)
            if auto.get('fatal'):
                pool_state, pool_reason = 'abnormal', '做市出现致命错误'
            elif auto.get('desired_running') is True and auto.get('effective_running') is False:
                pool_state, pool_reason = 'abnormal', '做市想开但实际没开'
            elif timeout_reason:
                pool_state, pool_reason = 'abnormal', timeout_reason
            elif (known and auto.get('desired_running') is True and auto.get('effective_running') is True
                    and health['status'] == 'abnormal'):
                pool_state, pool_reason = 'abnormal', health['reason']
            elif candidate_source and candidate_count == 0 and auto.get('desired_running') is True:
                pool_state, pool_reason = 'abnormal', '有效候选为零'
            elif failed and failure_age >= 600:
                pool_state, pool_reason = 'abnormal', '候选连续十分钟刷新失败'
            elif pool_state != 'abnormal':
                if not candidate_source or (not failed and (now-candidate_stamp).total_seconds() > 300):
                    pool_state, pool_reason = 'unknown', '候选刷新来源或成功时间未知、过期'
                elif failed:
                    pool_state, pool_reason = 'attention', '候选刷新失败，等待恢复'
                elif not all(type(auto.get(k)) is bool for k in ('desired_running','effective_running')):
                    pool_state, pool_reason = 'unknown', '做市开关状态未知'
            observed = mapping(auto.get('observed_pools'))
            share = mapping(observed.get('reward_share'))
            share_checked = moment(share.get('checked_at'))
            share_age = (now-share_checked).total_seconds() if share_checked is not None and share_checked <= now else None
            reward_share = dict(state='known' if share.get('state')=='known' and share_age is not None
                and 0<=share_age<=60 else 'unknown',checked_at=share.get('checked_at'),age_seconds=share_age)
            raw_gaps = mapping(observed.get('income_gaps'))
            gap_items = raw_gaps.get('items')
            gaps = dict(total=None, omitted=None, items=[])
            if (isinstance(gap_items, list) and len(gap_items) <= 20
                    and type(raw_gaps.get('total')) is int and type(raw_gaps.get('omitted')) is int
                    and 0 <= raw_gaps['omitted'] and raw_gaps['total'] == len(gap_items)+raw_gaps['omitted']):
                gaps = dict(total=raw_gaps['total'], omitted=raw_gaps['omitted'],
                    checked_at=raw_gaps.get('checked_at'), items=[{k: row.get(k) for k in
                        ('condition_id','order_id','token_id','reason','age_seconds','quantity','minimum_quantity')}
                        for row in gap_items if isinstance(row, Mapping)])
            attempts = []
            raw_attempts = mapping(observed.get('measurement_attempts'))
            roles = mapping(observed.get('roles'))
            for cid, attempt in raw_attempts.items():
                if mapping(roles.get(cid)).get('pool') not in ('trial','income'):
                    continue
                raw = mapping(mapping(attempt).get('result'))
                retained = mapping(mapping(mapping(observed.get('results')).get(cid)).get('result'))
                measured = moment(raw.get('measured_at') or retained.get('measured_at'))
                sample_age = (now-measured).total_seconds() if measured is not None and measured <= now else None
                source = mapping(raw.get('source_order') or retained.get('source_order'))
                order_id=mapping(attempt).get('order_id') or source.get('order_id')
                if source.get('order_id')!=order_id:sample_age=None
                expired=raw.get('state')=='known' and (sample_age is None or sample_age>120)
                attempts.append(dict(order_id=order_id,token_id=mapping(attempt).get('token_id') or source.get('token_id'),
                    sample_age_seconds=sample_age,condition_id=str(cid)[:96], checked_at=mapping(attempt).get('checked_at'),
                    state='unknown' if expired else raw.get('state'), reason='sample_expired' if expired else raw.get('reason'),
                    yield_pct_per_hour=None if expired else raw.get('yield_pct_per_hour')))
                if len(attempts) >= 64:
                    break
            result['pools'] = section(pool_state, pool_reason, items=pools,
                income_gaps=gaps, measurement_attempts=attempts, reward_share=reward_share,
                desired_running=auto.get('desired_running'), effective_running=auto.get('effective_running'),
                fatal=bool(auto.get('fatal')), eligible_candidates=candidate_count if candidate_source else None,
                candidate_last_success_at=candidate_stamp.isoformat() if candidate_stamp else None,
                candidate_failure_seconds=failure_age if failed else None, read_health=health)
            checked = moment(dashboard.get('checked_at'))
            balance, allowance = number(dashboard.get('balance')), number(dashboard.get('allowance'))
            capacity = number(auto.get('new_capacity_usd'))
            financial_known = (account_id and dashboard.get('account_id') == account_id
                and dashboard.get('authenticated') is True and dashboard.get('stale') is False
                and dashboard.get('positions_complete') is True and dashboard.get('open_orders_complete') is True
                and checked is not None
                and 0 <= (now-checked).total_seconds() and balance is not None and allowance is not None
                and capacity is not None and known)
            unknown_age = self.duration('capacity_unknown', not financial_known, now)
            if financial_known:
                aged = (now-checked).total_seconds() > 300
                result['balance'] = section('attention' if aged else 'normal',
                    '余额已超过五分钟未更新' if aged else '余额、授权和可用资金已确认',
                    balance_usd=str(balance), allowance_usd=str(allowance), capacity_usd=str(capacity),
                    inventory_cost_usd=auto.get('inventory_cost_usd'), checked_at=checked.isoformat())
            else:
                result['balance'].update(status='abnormal' if unknown_age >= 300 else 'unknown',
                    reason='可用资金未知：持仓或余额没读全' + ('，已持续五分钟' if unknown_age >= 300 else ''))
            result['cancellations'] = self.cancellations(audits, account_id, items, known, now)
            cancels=result['cancellations']
            boot_blocks=mapping(mapping(auto.get('diagnostics')).get('cancel_blocks')).get('total')
            cancels['boot_reference']=dict(blocked=boot_blocks if type(boot_blocks) is int else None)
            cancels['coverage']='仅统计本机持久请求审计；未登记请求前的拦截历史总量未知'
            if type(boot_blocks) is int and boot_blocks>0:
                cancels['hour']['blocked']=None;cancels['today']['blocked']=None
                cancels['coverage']='存在仅保留本次启动内存的早期拦截；小时和今天拦截总量未知'
                if cancels['status']!='abnormal':
                    cancels.update(status='unknown',reason='早期拦截未全部写入持久审计，完整次数未知')
            result['fills'] = self.fills(trades, auto, account_id, membership, now)
            return result

    def cancellations(self, audits, account_id, orders, orders_known, now):
        unknown = unknown_summary(now)['cancellations']
        if (not isinstance(audits, list) or not isinstance(account_id, str)
                or not account_id.strip()):
            return unknown
        canonical_account = account_id.casefold()
        start = now.astimezone(TOKYO).replace(hour=0, minute=0, second=0, microsecond=0)
        hour = dict.fromkeys(('success','rejected','unknown','blocked'), 0)
        today = dict(hour)
        details, stuck = [], []
        pending_unknown = False
        for raw in audits:
            row = mapping(raw)
            at = moment(row.get('requested_at'))
            if (not isinstance(row.get('account_id'), str)
                    or row['account_id'].casefold() != canonical_account or at is None or at > now
                    or row.get('kind') != 'CANCEL' or not row.get('request_id')
                    or not all(row.get(k) for k in ('condition_id','token_id','order_id'))):
                return unknown
            if any(row.get(key) is not None and not isinstance(row[key],Mapping) for key in ('receipt','progress')):
                return unknown
            receipt, progress = mapping(row.get('receipt')), mapping(row.get('progress'))
            if receipt and receipt.get('outcome') not in ('accepted','rejected','unknown','not_sent'):
                return unknown
            if any(evidence and any(evidence.get(k) != row.get(k) for k in
                    ('account_id','process_epoch','request_id','session_id','condition_id','token_id','order_id'))
                    for evidence in (receipt, progress)):
                return unknown
            terminal = progress.get('status') in ('CANCELED','CANCELLED','FILLED','MATCHED','EXPIRED','REJECTED')
            outcome = ('success' if progress.get('status') in ('CANCELED','CANCELLED') else
                'completed_other' if terminal else
                {'rejected':'rejected','unknown':'unknown','not_sent':'blocked'}.get(receipt.get('outcome'), 'pending'))
            detail = dict(request_id=row['request_id'], order_id=row['order_id'],
                condition_id=row['condition_id'], token_id=row['token_id'], outcome=outcome, requested_at=at.isoformat())
            if at >= min(start, now-timedelta(hours=1)) or outcome in ('pending','unknown'):
                details.append(detail)
            if outcome in hour:
                if at >= now-timedelta(hours=1): hour[outcome] += 1
                if at >= start: today[outcome] += 1
            if outcome == 'pending':
                live = any(all(o.get(k)==row.get(k) for k in ('condition_id','token_id','order_id')) for o in orders)
                if live and (now-at).total_seconds() > 120:
                    stuck.append(detail)
                elif not orders_known:
                    pending_unknown = True
        abnormal = bool(stuck or any(r['outcome']=='unknown' for r in details))
        return section('abnormal' if abnormal else 'unknown' if pending_unknown else
            'attention' if any(r['outcome']=='pending' for r in details) else 'normal',
            '存在结果未知或超过两分钟仍挂着的撤单' if abnormal else
            '当前挂单未确认，无法核对撤单进度' if pending_unknown else
            '撤单回执不代表最终撤完，等待账户确认' if any(r['outcome']=='pending' for r in details) else
            '持久撤单审计未见结果未知或卡单', hour=hour, today=today, items=details, stuck=stuck,
            basis='local durable CANCEL requests; accepted receipt is not final cancellation')

    def fills(self, trades, auto, account_id, membership, now):
        import hashlib
        import json
        unknown = unknown_summary(now)['fills']
        checked = moment(trades.get('checked_at'))
        raw_rows = trades.get('items')
        if (not account_id or trades.get('account_id') != account_id
                or trades.get('complete') is not True or trades.get('stale') is not False
                or trades.get('state') != 'ready' or checked is None
                or not 0 <= (now-checked).total_seconds() <= 60
                or not isinstance(raw_rows, (list, tuple))
                or type(trades.get('total')) is not int or trades['total'] != len(raw_rows)):
            return unknown
        start = now.astimezone(TOKYO).replace(hour=0, minute=0, second=0, microsecond=0)
        allocations = {}
        observed=moment(membership.get('observed_at'))
        membership_known=(membership.get('state')=='known' and membership.get('account_id')==account_id
            and observed is not None and 0 <= (now-observed).total_seconds() <= 60)
        current = ([r for r in membership.get('orders',[]) if isinstance(r,Mapping)]
            if membership_known and isinstance(membership.get('orders'),list) else [])
        for raw in raw_rows:
            row = mapping(raw)
            status = row.get('status')
            if status in ('FAILED','REJECTED','CANCELED','CANCELLED'):
                continue
            at = moment(row.get('matched_at'))
            tid, condition = row.get('trade_id') or row.get('id'), row.get('condition_id') or row.get('market')
            if status != 'CONFIRMED' or not tid or not condition or at is None or at > now:
                return unknown
            role = row.get('trader_side')
            if role == 'TAKER':
                parts = [dict(order_id=row.get('taker_order_id'), token_id=row.get('token_id'), side=row.get('side'),
                    price=row.get('price'), matched_amount=row.get('size'))]
            elif role == 'MAKER' and isinstance(row.get('maker_orders'), (list,tuple)) and row['maker_orders']:
                parts = row['maker_orders']  # background adapter has filtered exact own-wallet allocations
            else:
                return unknown
            for raw_part in parts:
                part = mapping(raw_part)
                oid, token, side = part.get('order_id'), part.get('token_id'), part.get('side')
                price, qty = number(part.get('price')), number(part.get('matched_amount'))
                if (not oid or not token or side not in ('BUY','SELL') or price is None or qty is None
                        or not 0 < price <= 1 or qty <= 0):
                    return unknown
                identity = (account_id, str(tid), str(condition), str(oid), str(token), side, role)
                pool = next((o.get('pool') for o in current if o.get('condition_id')==condition
                    and o.get('token_id')==token and o.get('order_id')==oid and o.get('side')==side
                    and o.get('pool') in ('trial','income')), None)
                item = dict(id=hashlib.sha256(json.dumps(identity).encode()).hexdigest(), trade_id=str(tid),
                    market=str(condition), order_id=str(oid), token_id=str(token), side=side, pool=pool,
                    price=str(price), quantity=str(qty), amount_usd=str(price*qty), time=at.isoformat())
                if identity in allocations and allocations[identity] != item:
                    return unknown
                allocations[identity] = item
        rows = sorted(allocations.values(), key=lambda r:(r['time'],r['id']))
        def window(since):
            chosen = [r for r in rows if moment(r['time']) >= since]
            return dict(count=len(chosen),amount_usd=str(sum((Decimal(r['amount_usd']) for r in chosen),Decimal(0))))
        rows = [r for r in rows if moment(r['time']) >= min(start,now-timedelta(hours=1))]
        inventory = number(auto.get('inventory_cost_usd'))
        observed = moment(membership.get('observed_at'))
        if (membership.get('state') != 'known' or membership.get('account_id') != account_id
                or observed is None or not 0 <= (now-observed).total_seconds() <= 60):
            inventory = None
        return section('attention' if rows else 'normal',
            '出现本钱包成交；成交本身不是异常' if rows else '最近一小时及今天未见本钱包成交',
            hour=window(now-timedelta(hours=1)), today=window(start), items=rows,
            inventory_cost_usd=str(inventory) if inventory is not None else None, checked_at=checked.isoformat())
