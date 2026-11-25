"""Human-approved first paper entries from the Trend V3 daily report."""

import hashlib
import json
from datetime import UTC, timedelta
from pathlib import Path

from .paper_evidence import TAIPEI, candidates, decimal, instant, text
from .paper_futu import MissedOpening
from .paper_risk import (
    Portfolio,
    PortfolioUnknown,
    existing_reservations,
    unit_notional,
)
from .paper_store import transaction

OPEN_WAKE_SLACK = timedelta(seconds=2)
OPEN_DISPATCH_SLACK = timedelta(seconds=30)
RETRY_EXPIRY = timedelta(minutes=1)
RETRYABLE = {"rejected", "cancelled"}


def encode(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    )


def fingerprint(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


class PaperEntry:
    def __init__(
        self,
        database,
        broker,
        session,
        *,
        verify_reply=None,
        clock=None,
        check_plan=None,
    ):
        if broker.environment != "SIMULATE":
            raise ValueError("paper entry requires a simulation broker")
        self.database = Path(database)
        self.broker = broker
        self.session = session
        self.verify_reply = verify_reply
        self.clock = clock
        self.check_plan = check_plan

    def plan(self, report, facts, *, now):
        rows, skipped = candidates(report, facts)
        if report.get("date") != self.session["date"] or (
            now.astimezone(TAIPEI).date().isoformat() != self.session["date"]
        ):
            raise ValueError("report date must match the current scheduled report")
        for row in rows:
            if now >= instant(row["opens_at"]):
                skipped.append({"code": row["code"], "reason": "missed_open"})
        rows = [row for row in rows if now < instant(row["opens_at"])]
        with transaction(self.database) as store:
            previous = self.latest_plan(store)
            report_hash = facts.get("daily_evidence_hash") or fingerprint(
                [report, facts]
            )
            revision = facts.get("report_revision")
            if previous is not None:
                self.check_revision(previous, revision, report_hash)
                if previous["report_hash"] == report_hash and any(
                    now >= instant(line["opens_at"]) for line in previous["lines"]
                ):
                    return previous
            lines = self.size_candidates(store, rows, skipped, now)
            content = {
                "date": report.get("date"),
                "report_version": facts.get("report_version"),
                "revision": revision,
                "report_hash": report_hash,
                "lines": lines,
                "skipped": skipped,
                "owner": self.session["owner"],
                "session": self.session["id"],
                "account_scope": getattr(self.broker, "account_scope", dict)(),
            }
            content_hash = fingerprint(content)
            if previous and previous["content_hash"] == content_hash:
                return previous
            plan = {
                "id": fingerprint([content_hash, previous["id"] if previous else None]),
                "content_hash": content_hash,
                **content,
            }
            store.execute(
                "INSERT OR IGNORE INTO plans VALUES (?, ?, ?, ?)",
                (plan["id"], plan["date"], encode(plan), now.isoformat()),
            )
            store.execute(
                "INSERT OR REPLACE INTO current_plan VALUES (?, ?)",
                (plan["date"], plan["id"]),
            )
        return plan

    @staticmethod
    def check_revision(previous, revision, report_hash):
        if previous["revision"] is None:
            return
        if type(revision) is not int or revision < previous["revision"]:
            raise ValueError("an older report cannot replace the current paper plan")
        if revision == previous["revision"] and previous["report_hash"] != report_hash:
            raise ValueError("changed evidence requires a new report revision")

    def latest_plan(self, store):
        row = store.execute(
            "SELECT id FROM current_plan WHERE report_day = ?",
            (self.session["date"],),
        ).fetchone()
        if row is None:
            return None
        plan, _ = self.current(store, row["id"])
        if plan is None:
            raise ValueError(
                "this report date already has a different scheduled owner or session"
            )
        return plan

    def size_candidates(self, store, rows, skipped, now):
        if not rows:
            return []
        try:
            portfolio = self.portfolio(store, now)
        except PortfolioUnknown as error:
            skipped.extend({"code": row["code"], "reason": str(error)} for row in rows)
            return []

        lines = []
        for row in rows:
            row["account_identity"] = portfolio.accounts.get(row["account"], {}).get(
                "identity"
            )
            quantity, reason = portfolio.quantity(row)
            if quantity:
                try:
                    capacity = self.broker.capacity(row)
                except (OSError, RuntimeError):
                    capacity = None
                if type(capacity) is not int or capacity < row["lot"]:
                    reason = (
                        "short_capacity_unverified"
                        if row["direction"] == "short"
                        else "market_capacity_unverified"
                    )
                    quantity = 0
                else:
                    quantity = min(quantity, capacity // row["lot"] * row["lot"])
            if not quantity:
                skipped.append({"code": row["code"], "reason": reason})
                continue
            lines.append(
                {
                    **row,
                    "item": fingerprint(row)[:24],
                    "quantity": quantity,
                    "notional": str(unit_notional(row) * quantity),
                }
            )
            portfolio.reserve(row, quantity)
        return lines

    def account_snapshot(self):
        try:
            return self.broker.snapshot()
        except (OSError, RuntimeError) as error:
            raise PortfolioUnknown("paper_account_unavailable") from error

    def portfolio(self, store, now, accounts=None):
        result = Portfolio(self.account_snapshot() if accounts is None else accounts)
        existing_reservations(store, result, now)
        return result

    def stored_plan(self, store, plan_id):
        row = store.execute(
            "SELECT body, created FROM plans WHERE id = ?",
            (plan_id,),
        ).fetchone()
        if row is None:
            return None, None
        plan = json.loads(row["body"])
        if (plan["date"], plan["owner"], plan["session"]) != (
            self.session["date"],
            self.session["owner"],
            self.session["id"],
        ):
            return None, None
        return plan, instant(row["created"])

    def current(self, store, plan_id):
        if store.execute(
            "SELECT id FROM current_plan WHERE id = ?", (plan_id,)
        ).fetchone():
            return self.stored_plan(store, plan_id)
        return None, None

    def current_plan(self, plan_id):
        with transaction(self.database) as store:
            return self.current(store, plan_id)

    def approve(self, plan_id, reply, *, now):
        result = self.record_reply(plan_id, reply, now=now)
        return bool(result and result["recorded"])

    def record_reply(self, plan_id, reply, *, now):
        if (
            not isinstance(reply, dict)
            or self.verify_reply is None
            or not self.verify_reply(reply)
        ):
            return None
        with transaction(self.database) as store:
            plan, created = self.current(store, plan_id)
            if plan is None or not self.valid_reply(reply, plan, created, now):
                return None
            if self.check_plan is not None and not self.check_plan(plan):
                return None
            items = {line["item"]: line for line in plan["lines"]}
            decision = reply["decision"]
            selected = [item["item"] for item in reply["items"]]
            result = {"decision": decision, "items": selected, "recorded": True}
            if decision == "retry":
                return (
                    result
                    if self.approve_retry(store, plan, items, reply, now)
                    else None
                )
            if not reply["items"] or any(
                not self.valid_selection(
                    selected,
                    items.get(selected.get("item")),
                )
                for selected in reply["items"]
            ):
                return None
            if len(set(selected)) != len(selected):
                return None
            at = instant(reply["at"])
            opened = [
                item for item in selected if at >= instant(items[item]["opens_at"])
            ]
            if decision == "approve" and opened:
                return None
            if decision == "deny":
                settled = {
                    row["item"]
                    for row in store.execute(
                        "SELECT item FROM attempts WHERE plan = ? "
                        "AND reason IS NOT 'awaiting_broker_receipt' "
                        "UNION SELECT item FROM decisions WHERE plan = ?",
                        (plan_id, plan_id),
                    )
                }
                opened = [
                    item for item in selected if item in opened or item in settled
                ]
                selected = [item for item in selected if item not in opened]
                result.update(items=selected, ignored_opened_items=opened)
                if not selected:
                    return result | {"recorded": False, "reason": "no_unopened_items"}
            at = at.astimezone(UTC).isoformat()
            newer = {
                row["item"]
                for row in store.execute(
                    "SELECT item FROM approvals WHERE plan = ? AND "
                    "(at > ? OR (at = ? AND ? = 'approve'))",
                    (plan_id, at, at, decision),
                )
            }
            selected = [item for item in selected if item not in newer]
            if not selected:
                return None
            inserted = store.execute(
                "INSERT OR IGNORE INTO replies VALUES (?, ?, ?)",
                (reply["event_id"], plan_id, encode(reply)),
            )
            if not inserted.rowcount:
                return None
            store.executemany(
                """INSERT INTO approvals VALUES (?, ?, ?, ?)
                   ON CONFLICT(plan, item) DO UPDATE SET
                   quantity=excluded.quantity, at=excluded.at""",
                (
                    (
                        plan_id,
                        item,
                        items[item]["quantity"] if decision == "approve" else 0,
                        at,
                    )
                    for item in selected
                ),
            )
            return result | {"items": selected}

    def valid_reply(self, reply, plan, created, now):
        at = instant(reply.get("at"))
        delivered = instant(reply.get("delivered_at"))
        return (
            isinstance(reply.get("items"), list)
            and all(
                isinstance(item, dict) and text(item.get("item"))
                for item in reply["items"]
            )
            and text(reply.get("event_id"))
            and reply.get("author") == self.session.get("author", "Ray")
            and reply.get("source") == "user_reply"
            and self.session.get("kind") == "scheduled"
            and reply.get("owner") == plan["owner"]
            and reply.get("session") == plan["session"]
            and reply.get("decision") in {"approve", "deny", "retry"}
            and reply.get("plan") == plan["id"]
            and reply.get("report_version") == plan["report_version"]
            and at is not None
            and delivered is not None
            and created <= delivered <= at <= now
        )

    @staticmethod
    def valid_selection(selected, line):
        return (
            line is not None
            and type(selected.get("quantity")) is int
            and selected["quantity"] == line["quantity"]
            and selected.get("open") == line["opens_at"]
        )

    def approve_retry(self, store, plan, items, reply, now):
        if len(reply["items"]) != 1:
            return False
        (selected,) = reply["items"]
        line = items.get(selected.get("item"))
        if line is None:
            return False
        attempt = self.attempt(store, line)
        at = instant(reply["at"])
        if (
            attempt is None
            or attempt["plan"] != plan["id"]
            or attempt["state"] not in RETRYABLE
            or attempt["reconciled"] is None
            or instant(attempt["reconciled"]) >= at
            or selected.get("after_attempt") != attempt["token"]
            or selected.get("open") != line["opens_at"]
            or type(selected.get("quantity")) is not int
            or not 0 < selected["quantity"] == attempt["quantity"] - attempt["filled"]
            or not instant(line["opens_at"]) <= at <= now < instant(line["closes_at"])
            or now > at + RETRY_EXPIRY
        ):
            return False
        inserted = store.execute(
            "INSERT OR IGNORE INTO replies VALUES (?, ?, ?)",
            (reply["event_id"], plan["id"], encode(reply)),
        )
        if not inserted.rowcount:
            return False
        store.execute(
            "INSERT OR REPLACE INTO retries VALUES (?, ?, ?, ?)",
            (attempt["token"], reply["event_id"], selected["quantity"], reply["at"]),
        )
        return True

    def submit(self, plan_id, *, now, retry=False, item=None, opening=None):
        with transaction(self.database) as store:
            plan, _ = self.stored_plan(store, plan_id)
        if plan is None:
            return []
        lines = [
            line
            for line in plan["lines"]
            if (item is None or line["item"] == item)
            and (opening is None or instant(line["opens_at"]) == opening)
        ]
        accounts = None

        def current_portfolio(store):
            nonlocal accounts
            if accounts is None:
                if hasattr(self.broker, "restore_scope"):
                    self.broker.restore_scope(plan.get("account_scope") or {})
                accounts = self.account_snapshot()
            # Use one opening snapshot and overlay newly committed local
            # reservations before each independent market/capacity check.
            return self.portfolio(store, now, accounts)

        return [
            self.submit_line(plan_id, line, now, retry, current_portfolio)
            for line in lines
        ]

    def submit_line(self, plan_id, line, now, retry, current_portfolio):
        with transaction(self.database) as store:
            plan, _ = self.current(store, plan_id)
            if plan is None:
                return self.outcome(line, "skipped", reason="plan_superseded")
            if self.check_plan is not None and not self.check_plan(plan):
                return self.outcome(
                    line, "skipped", reason="report_revised_or_unverified"
                )
            attempt = self.attempt(store, line)
            if attempt:
                pending_retry = self.authorized_retry(
                    store, plan, line, attempt, now, retry
                )
                if pending_retry is None:
                    return self.attempt_outcome(store, line, attempt)
                quantity = pending_retry["quantity"]
                number = attempt["number"] + 1
                deadline = min(
                    instant(pending_retry["at"]) + RETRY_EXPIRY,
                    instant(line["closes_at"]),
                )
            else:
                if retry:
                    return self.outcome(line, "skipped", reason="retry_not_authorized")
                opens = instant(line["opens_at"])
                if now < opens:
                    return self.outcome(line, "pending", reason="waiting_for_open")
                decision = self.decision(store, plan_id, line)
                if decision:
                    return decision
                if now > opens + OPEN_WAKE_SLACK:
                    return self.finalize(store, plan_id, line, "missed_open")
                approved = store.execute(
                    "SELECT quantity FROM approvals WHERE plan = ? AND item = ?",
                    (plan_id, line["item"]),
                ).fetchone()
                if approved is None:
                    return self.finalize(store, plan_id, line, "not_approved")
                quantity = approved["quantity"]
                if quantity == 0:
                    return self.finalize(
                        store, plan_id, line, "ray_denied", state="denied"
                    )
                number = 1
                deadline = min(opens + OPEN_DISPATCH_SLACK, instant(line["closes_at"]))
            expires = instant(self.session.get("expires_at"))
            if expires is not None:
                deadline = min(deadline, expires)
            if self.clock is not None and self.clock() >= deadline:
                if retry:
                    return self.outcome(line, "skipped", reason="retry_deadline")
                return self.finalize(store, plan_id, line, "open_dispatch_deadline")
            previous_fills = self.previous_fills(store, line) if attempt else 0
            reason = self.execution_blocker(
                store,
                line,
                quantity,
                previous_fills,
                current_portfolio,
            )
            if reason:
                if retry:
                    return self.outcome(line, "skipped", reason=reason)
                return self.finalize(store, plan_id, line, reason)
            economic_key = self.economic_key(line)
            token = "TV3" + fingerprint([economic_key, number])[:25]
            store.execute(
                """INSERT INTO attempts
                   (token, economic_key, plan, item, quantity, number, state, reason)
                   VALUES (?, ?, ?, ?, ?, ?, 'unknown', 'awaiting_broker_receipt')""",
                (token, economic_key, plan_id, line["item"], quantity, number),
            )
        return self.send(plan_id, line, token, quantity, deadline, retry=retry)

    def execution_blocker(
        self, store, line, quantity, previous_fills, current_portfolio
    ):
        try:
            portfolio = current_portfolio(store)
            allowed, reason = portfolio.quantity(line, previous_fills=previous_fills)
            if allowed < quantity:
                return reason or "approved_size_unavailable"
            capacity = self.broker.capacity(line)
            if type(capacity) is not int or capacity < quantity:
                return "approved_size_unavailable"
            if not self.broker.market_open(line):
                return "market_not_open"
        except (PortfolioUnknown, OSError, RuntimeError):
            return "execution_conditions_unverified"
        return None

    def decision(self, store, plan_id, line):
        row = store.execute(
            "SELECT state, reason FROM decisions WHERE plan = ? AND item = ?",
            (plan_id, line["item"]),
        ).fetchone()
        return self.outcome(line, row["state"], reason=row["reason"]) if row else None

    def finalize(self, store, plan_id, line, reason, *, state="skipped"):
        store.execute(
            "INSERT INTO decisions VALUES (?, ?, ?, ?)",
            (plan_id, line["item"], state, reason),
        )
        return self.outcome(line, state, reason=reason)

    def authorized_retry(self, store, plan, line, attempt, now, retry):
        if (
            not retry
            or attempt["plan"] != plan["id"]
            or attempt["state"] not in RETRYABLE
        ):
            return None
        approved = store.execute(
            "SELECT * FROM retries WHERE token = ?",
            (attempt["token"],),
        ).fetchone()
        if approved is not None:
            at = instant(approved["at"])
            if at <= now <= at + RETRY_EXPIRY and now < instant(line["closes_at"]):
                return approved
        return None

    def send(self, plan_id, line, token, quantity, deadline, *, retry):
        # The committed reservation survives process death. The second transaction
        # serializes dispatch against plan withdrawal and broker reconciliation.
        with transaction(self.database) as store:
            current, _ = self.current(store, plan_id)
            attempt = self.attempt(store, line)
            if (
                attempt["token"] != token
                or attempt["reason"] != "awaiting_broker_receipt"
            ):
                return self.attempt_outcome(store, line, attempt)
            response = None
            expired = "retry_deadline" if retry else "open_dispatch_deadline"
            approval = None
            if not retry:
                approval = store.execute(
                    "SELECT quantity FROM approvals WHERE plan = ? AND item = ?",
                    (plan_id, line["item"]),
                ).fetchone()
            if current is None:
                result = ("skipped", None, 0, "plan_superseded")
            elif not retry and (approval is None or approval["quantity"] != quantity):
                result = ("denied", None, 0, "ray_denied")
            elif self.check_plan is not None and not self.check_plan(current):
                result = ("skipped", None, 0, "report_revised_or_unverified")
            elif self.clock is not None and self.clock() >= deadline:
                result = ("skipped", None, 0, expired)
            else:
                try:
                    response = self.broker.place_market(
                        line,
                        quantity,
                        token,
                        deadline=deadline if self.clock else None,
                    )
                    result = self.broker_result(response, quantity)
                except MissedOpening:
                    result = ("skipped", None, 0, expired)
                except TimeoutError:
                    result = ("unknown", None, None, "broker_timeout")
                except ConnectionError:
                    result = ("unknown", None, None, "broker_disconnected")
                except Exception:
                    result = ("unknown", None, None, "broker_receipt_unverified")
            store.execute(
                """UPDATE attempts SET state=?, order_id=?, filled=?, reason=?,
                   filled_notional=? WHERE token=?""",
                (*result, self.filled_notional(response, result[2]), token),
            )
            return self.attempt_outcome(store, line, self.attempt(store, line))

    def reconcile(self, plan_id, *, now):
        with transaction(self.database) as store:
            plan, _ = self.stored_plan(store, plan_id)
            if plan is None:
                return []
            results = []
            for line in plan["lines"]:
                attempt = self.attempt(store, line)
                if attempt is None or attempt["plan"] != plan_id:
                    continue
                if attempt["state"] != "skipped":
                    self.reconcile_attempt(store, line, attempt, now)
                    attempt = self.attempt(store, line)
                results.append(self.attempt_outcome(store, line, attempt))
            return results

    def reconcile_attempt(self, store, line, attempt, now):
        try:
            response = self.broker.find_order(
                line,
                attempt["token"],
                attempt["order_id"],
                attempt["quantity"],
            )
        except Exception:
            return
        state, order_id, filled, reason = self.broker_result(
            response, attempt["quantity"]
        )
        explicit_conflict = (
            isinstance(response, dict) and response.get("conflict") is True
        )
        if state == "unknown" and not explicit_conflict:
            return
        conflicting = explicit_conflict or (
            (attempt["order_id"] and attempt["order_id"] != order_id)
            or filled < (attempt["filled"] or 0)
            or (
                attempt["reconciled"] is not None
                and attempt["state"] in RETRYABLE | {"filled"}
                and (attempt["state"], attempt["filled"]) != (state, filled)
            )
        )
        if conflicting:
            store.execute(
                """UPDATE attempts SET state='unknown',
                   reason='broker_receipt_conflict', reconciled=NULL WHERE token=?""",
                (attempt["token"],),
            )
            store.execute("DELETE FROM retries WHERE token=?", (attempt["token"],))
            return
        store.execute(
            """UPDATE attempts SET state=?, order_id=?, filled=?, reason=?,
               filled_notional=COALESCE(?, CASE WHEN filled=? THEN filled_notional END),
               reconciled=COALESCE(reconciled, ?) WHERE token=?""",
            (
                state,
                order_id,
                filled,
                reason,
                self.filled_notional(response, filled),
                filled,
                (self.clock() if self.clock else now).isoformat()
                if state != "active"
                else None,
                attempt["token"],
            ),
        )

    @staticmethod
    def broker_result(response, quantity):
        if isinstance(response, dict):
            state = response.get("state")
            order_id = response.get("id")
            filled = response.get("filled")
            if text(order_id) and type(filled) is int and 0 <= filled <= quantity:
                if state == "filled" and filled == quantity:
                    return state, order_id, filled, None
                if state in {"active", "cancelled", "rejected"} and filled < quantity:
                    return state, order_id, filled, state if state != "active" else None
        return "unknown", None, None, "broker_receipt_unverified"

    @staticmethod
    def filled_notional(response, filled):
        if filled is None:
            return None
        if filled == 0:
            return "0"
        reported = (
            decimal(response.get("filled_notional"))
            if isinstance(response, dict)
            else None
        )
        return str(reported) if reported is not None and reported > 0 else None

    @staticmethod
    def economic_key(line):
        return encode(
            [
                line["market"],
                line["product"],
                line["economic_instrument"],
                line["session_date"],
            ]
        )

    def attempt(self, store, line):
        return store.execute(
            """SELECT * FROM attempts WHERE economic_key = ?
               ORDER BY number DESC LIMIT 1""",
            (self.economic_key(line),),
        ).fetchone()

    def attempt_outcome(self, store, line, attempt):
        previous_fills = self.previous_fills(store, line, before=attempt["number"])
        filled = attempt["filled"]
        outcome = self.outcome(
            line,
            attempt["state"],
            previous_fills + filled if filled is not None else None,
            attempt["reason"],
        )
        outcome["attempt"] = attempt["token"]
        outcome["reconciled"] = attempt["reconciled"] is not None
        outcome["previous_fills"] = previous_fills
        outcome["filled_notional"] = attempt["filled_notional"]
        if filled is not None:
            outcome["remaining"] = attempt["quantity"] - filled
        return outcome

    @staticmethod
    def outcome(line, state, filled=None, reason=None):
        return {
            "item": line["item"],
            "code": line["code"],
            "state": state,
            "filled": filled,
            "remaining": line["quantity"] - filled if filled is not None else None,
            "active": None if state == "unknown" else state == "active",
            "reason": reason,
        }

    def previous_fills(self, store, line, *, before=None):
        return (
            store.execute(
                """SELECT SUM(filled) FROM attempts
               WHERE economic_key = ? AND (? IS NULL OR number < ?)""",
                (self.economic_key(line), before, before),
            ).fetchone()[0]
            or 0
        )

    def openings(self, plan_id):
        with transaction(self.database) as store:
            plan, _ = self.current(store, plan_id)
            if plan is None:
                return []
        return sorted({instant(line["opens_at"]) for line in plan["lines"]})

    def status(self, *, now):
        """Read the current and prior-order summaries for the scheduled report."""
        with transaction(self.database) as store:
            plan = self.latest_plan(store)
            if plan is None:
                return None
            results = {}
            for line in plan["lines"]:
                attempt = self.attempt(store, line)
                if attempt:
                    result = self.attempt_outcome(store, line, attempt)
                else:
                    result = self.decision(store, plan["id"], line)
                    if result is None:
                        result = self.unsubmitted_status(store, plan["id"], line, now)
                results[line["code"]] = result
            history = store.execute(
                """SELECT attempts.*, plans.body FROM attempts
                   JOIN plans ON plans.id = attempts.plan
                   WHERE plans.report_day = ? ORDER BY attempts.number""",
                (self.session["date"],),
            )
            for attempt in history:
                old_plan = json.loads(attempt["body"])
                line = next(
                    line
                    for line in old_plan["lines"]
                    if line["item"] == attempt["item"]
                )
                results[line["code"]] = self.attempt_outcome(store, line, attempt)
            return {
                "schema": "trend-v3-paper-entry.v1",
                "date": plan["date"],
                "report_version": plan["report_version"],
                "results": [self.public_outcome(result) for result in results.values()],
                "skipped": [
                    item for item in plan["skipped"] if item.get("code") not in results
                ],
            }

    def unsubmitted_status(self, store, plan_id, line, now):
        approved = store.execute(
            "SELECT quantity FROM approvals WHERE plan = ? AND item = ?",
            (plan_id, line["item"]),
        ).fetchone()
        if approved and approved["quantity"] == 0:
            return self.outcome(line, "denied", reason="ray_denied")
        if now < instant(line["opens_at"]):
            reason = "waiting_for_open" if approved else "waiting_for_ray"
            return self.outcome(line, "pending", reason=reason)
        reason = "missed_open" if approved else "not_approved"
        return self.outcome(line, "skipped", reason=reason)

    @staticmethod
    def public_outcome(result):
        fields = ("code", "state", "filled", "remaining", "active", "reason")
        return {field: result[field] for field in fields}
