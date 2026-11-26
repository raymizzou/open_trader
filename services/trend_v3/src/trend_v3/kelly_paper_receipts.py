"""Convert corroborated paper order details into atomic Kelly ledger events."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Context, Decimal, localcontext
from hashlib import sha256
from pathlib import Path

from .kelly_demo import PARTIAL_IDENTITY, REPORT_REF
from .kelly_ledger import (
    Fill,
    RoundIdentity,
    _amount,
    _decimal_text,
    _Event,
    _timestamp,
)
from .kelly_paper_source import authorized_attempt, opening_group
from .paper_authority import private_bytes, strict_json
from .paper_entry import fingerprint
from .paper_evidence import text
from .paper_futu import FutuPaperBroker

RECEIPT_SCHEMA = "trend-v3-kelly-receipts-v1"
DOCUMENT_SCHEMA = "trend-v3-kelly-document-v1"


class DemoLedgerError(ValueError):
    """A formal paper command targeted the known fabricated fixture."""


def require_formal_ledger(db):
    if db.execute(
        """
        SELECT 1 FROM rounds
        WHERE report_ref = ?
           OR (account_key IN (?, 'fabricated-real') AND instrument_key GLOB 'mock-*')
        LIMIT 1
        """,
        (REPORT_REF, PARTIAL_IDENTITY.account_key),
    ).fetchone():
        raise DemoLedgerError("Select a separate official ledger; this is a demo.")


def _id(value):
    if not text(value) or value.strip() != value or len(value) > 200:
        raise ValueError("invalid broker identifier")
    return value


def _quantity(value, *, signed=False):
    if type(value) is not int or abs(value) > 2**53 or (not signed and value < 0):
        raise ValueError("invalid broker quantity")
    return value


@dataclass(frozen=True)
class ReceiptOrder:
    data: dict
    account: str
    id: str
    side: str
    quantity: int
    filled: int
    notional: Decimal
    state: str

    @classmethod
    def parse(cls, row):
        if set(row) - {
            "broker",
            "environment",
            "account_id",
            "account_market",
            "order_id",
            "code",
            "product",
            "currency",
            "side",
            "order_quantity",
            "state",
            "filled_quantity",
            "filled_notional",
            "deals",
            "checkpoints",
            "attempt",
            "opening_order_id",
            "position_before",
            "position_after",
        }:
            raise ValueError("unknown order evidence field")
        account_id = _id(row["account_id"])
        if (
            row["broker"] != "Futu"
            or row["environment"] != "SIMULATE"
            or not account_id.isascii()
            or not account_id.isdecimal()
            or not 0 < int(account_id)
            or str(int(account_id)) != account_id
        ):
            raise ValueError("receipt needs an explicit verified simulation account")
        account = FutuPaperBroker.account_identity(
            {"id": int(account_id), "trade_market": _id(row["account_market"])}
        )
        side = row["side"]
        quantity = _quantity(row["order_quantity"])
        filled = _quantity(row["filled_quantity"])
        notional = _amount(row["filled_notional"])
        state = row["state"]
        if (
            side not in {"buy", "sell"}
            or quantity <= 0
            or filled > quantity
            or (filled == 0 and notional != 0)
            or (filled > 0 and notional <= 0)
            or state not in {"active", "filled", "cancelled"}
            or (state == "filled") != (filled == quantity)
            or ("attempt" not in row and filled == 0)
        ):
            raise ValueError("invalid receipt order totals")
        return cls(
            row, account, _id(row["order_id"]), side, quantity, filled, notional, state
        )

    @property
    def anchor(self):
        return _id(self.data.get("opening_order_id", self.id))

    @property
    def ref(self):
        return "futu-order/" + fingerprint([self.account, self.id])

    def require_position(self, identity, line):
        if (
            self.account != identity.account_key
            or self.data["code"] != line["code"]
            or self.data["product"] != line["product"]
            or self.data["currency"] != identity.currency
        ):
            raise ValueError("receipt position does not match opening identity")

    def deals(self, multiplier, observed_at):
        if not isinstance(self.data["deals"], list):
            raise ValueError("invalid broker deals")
        fills = {}
        for item in self.data["deals"]:
            if set(item) != {"deal_id", "quantity", "price", "executed_at"}:
                raise ValueError("invalid deal evidence fields")
            key = "futu-deal/" + fingerprint([self.account, _id(item["deal_id"])])
            quantity = _quantity(item["quantity"])
            price = _amount(item["price"])
            occurred = _timestamp(item["executed_at"])
            if not quantity or price <= 0 or occurred > observed_at:
                raise ValueError("invalid executed deal")
            fill = Fill(
                key,
                occurred,
                self.side,
                str(quantity),
                _amount(price * quantity * multiplier),
                self.ref,
            )
            if fills.setdefault(key, fill) != fill:
                raise ValueError("conflicting broker deal")
        ordered = sorted(
            fills.values(), key=lambda fill: (fill.occurred_at, fill.event_key)
        )
        self.verify_cumulative(ordered)
        return ordered

    def verify_cumulative(self, fills):
        checkpoints = self.data.get("checkpoints", [])
        if not isinstance(checkpoints, list):
            raise ValueError("invalid order checkpoints")
        prefix = {0: Decimal(0)}
        quantity = 0
        for fill in fills:
            next_quantity = quantity + int(fill.quantity)
            prefix[next_quantity] = prefix[quantity] + fill.notional
            quantity = next_quantity
        previous_qty, previous_total = 0, Decimal(0)
        for check in [
            *checkpoints,
            {"filled_quantity": self.filled, "filled_notional": self.notional},
        ]:
            qty = _quantity(check["filled_quantity"])
            total = _amount(check["filled_notional"])
            if (
                qty > self.filled
                or qty < previous_qty
                or (qty == previous_qty and total != previous_total)
                or (qty > previous_qty and total <= previous_total)
                or (fills and prefix.get(qty) != total)
            ):
                raise ValueError(
                    "deal deltas do not match cumulative order checkpoints"
                )
            previous_qty, previous_total = qty, total
        if fills and quantity != self.filled:
            raise ValueError("executed deal quantity does not match broker order")


def _bind_order(order, snapshot):
    attempt, line, scope, earliest = authorized_attempt(order.data["attempt"], snapshot)
    entry = "buy" if line["direction"] == "long" else "sell"
    if (
        order.id != attempt["order_id"]
        or order.account != scope["identity"]
        or order.data["account_market"] != scope["market"]
        or order.data["currency"] != scope["currency"]
        or order.data["code"] != line["code"]
        or order.data["product"] != line["product"]
        or order.quantity != attempt["quantity"]
        or order.side != entry
        or order.state != attempt["state"]
        or order.filled != attempt["filled"]
        or _amount(attempt["filled_notional"]) != order.notional
    ):
        raise ValueError("receipt does not match the official order")
    return attempt, line, earliest


def _round_events(db, report, snapshot, orders, anchor, observed_at):
    root = orders[anchor]
    attempt, line, _ = _bind_order(root, snapshot)
    earlier = {
        row["number"]: row
        for row in snapshot.attempts
        if row["plan"] == attempt["plan"] and row["item"] == attempt["item"]
    }
    if any(
        earlier[number]["filled"] != 0
        or earlier[number]["state"] not in {"cancelled", "rejected"}
        for number in range(1, attempt["number"])
    ):
        raise ValueError(
            "opening receipt must cover the first filled authorized attempt"
        )
    identity = RoundIdentity(
        root.account,
        "SIMULATE",
        line["code"],
        line["direction"],
        opening_group(report, line),
        report.ref,
        line["currency"],
    )
    entries = []
    pending = False
    for order in orders.values():
        order.require_position(identity, line)
        fills = order.deals(_amount(line["multiplier"]), observed_at)
        if "attempt" in order.data:
            other, other_line, earliest = _bind_order(order, snapshot)
            if (
                other_line != line
                or other["plan"] != attempt["plan"]
                or other["number"] < attempt["number"]
                or any(
                    earlier[number]["filled"] is None
                    or (
                        earlier[number]["filled"] > 0
                        and (
                            earlier[number]["order_id"] not in orders
                            or orders[earlier[number]["order_id"]].data.get("attempt")
                            != earlier[number]["token"]
                        )
                    )
                    for number in range(attempt["number"], other["number"])
                )
            ):
                raise ValueError("additional attempt does not belong to the opening")
            closes = _timestamp(line["closes_at"])
            earliest = earliest.isoformat(timespec="microseconds")
            if fills and any(
                not _timestamp(earliest) <= f.occurred_at <= closes for f in fills
            ):
                raise ValueError("deal is outside the authorized execution window")
        elif (order.account, order.id) in snapshot.orders:
            raise ValueError("official order cannot act as a supplementary document")
        if order.filled and not fills:
            pending = True
        if fills:
            entries.append((fills[0].occurred_at, order, fills))
    if pending:
        return identity, [], True
    entries.sort(key=lambda item: item[0])
    if entries and entries[0][1] != root:
        raise ValueError("first deal does not belong to the opening order")

    position = 0
    previous_time = None
    seen = set()
    for first_time, order, fills in entries:
        if previous_time and (not position or first_time <= previous_time):
            raise ValueError("position sequence is ambiguous or the position is closed")
        previous_time = fills[-1].occurred_at
        after = position + order.filled * (1 if order.side == "buy" else -1)
        if "attempt" not in order.data and (
            _quantity(order.data["position_before"], signed=True) != position
            or _quantity(order.data["position_after"], signed=True) != after
        ):
            raise ValueError("supplementary receipt does not prove position movement")
        if after < 0 if identity.direction == "long" else after > 0:
            raise ValueError("receipt crosses the opening position")
        keys = {fill.event_key for fill in fills}
        previous = {
            row[0]
            for row in db.execute(
                "SELECT event_key FROM trade_events WHERE receipt_ref=? "
                "AND event_type IN ('buy','sell')",
                (order.ref,),
            )
        }
        if keys & seen or not previous <= keys:
            raise ValueError("reused deal or stale order history")
        seen.update(keys)
        position = after
    return identity, entries, False


def _settle(db, ledger, round_id, identity, orders, settlement, anchor, observed_at):
    root = orders[anchor]
    if (
        settlement["currency"] != identity.currency
        or settlement["broker"] != "Futu"
        or settlement["environment"] != "SIMULATE"
        or settlement["account_id"] != root.data["account_id"]
        or settlement["account_market"] != root.data["account_market"]
        or not isinstance(settlement["fees"], dict)
    ):
        raise ValueError("settlement currency or fees are unverified")
    refs = {
        row[0]
        for row in db.execute(
            "SELECT DISTINCT receipt_ref FROM trade_events WHERE round_id=? "
            "AND event_type IN ('buy','sell')",
            (round_id,),
        )
    }
    fee_orders = {orders[id_].ref: orders[id_] for id_ in settlement["fees"]}
    if (
        refs != set(fee_orders)
        or not refs
        or any(order.state == "active" for order in fee_orders.values())
    ):
        raise ValueError("settlement lacks complete fees for the executed orders")
    total = _amount(sum(charge for charge, _ in settlement["fees"].values()))
    at = _timestamp(settlement["confirmed_at"])
    if at > observed_at:
        raise ValueError("settlement confirmation is in the future")
    fee_ref = "futu-fees/" + fingerprint(
        sorted((id_, identity) for id_, (_, identity) in settlement["fees"].items())
    )
    ledger._save_cash_event(
        db,
        round_id,
        _Event(
            "futu-cost/" + round_id,
            at,
            "adjustment",
            None,
            None,
            _decimal_text(-total),
            fee_ref,
        ),
    )
    ledger._save_cash_event(
        db,
        round_id,
        _Event("futu-settle/" + round_id, at, "settle", None, None, None, fee_ref),
    )


def _pinned_bytes(proof, directory, checked):
    if set(proof) != {"file", "sha256"}:
        raise ValueError("receipt needs a pinned private document")
    name = _id(proof["file"])
    path = Path(name)
    if path.name != name or name in {".", ".."}:
        raise ValueError("receipt documents must be adjacent private files")
    if name not in checked:
        raw = private_bytes(directory / path)
        checked[name] = (sha256(raw).hexdigest(), raw)
    digest, raw = checked[name]
    if digest != proof["sha256"] or not raw:
        raise ValueError("receipt document digest does not match")
    return raw


def _document_row(binding, group, key, directory, checked):
    allowed = (
        {"proof", key, "attempt", "opening_order_id"}
        if group == "orders"
        else {"proof", key}
    )
    if set(binding) - allowed:
        raise ValueError("receipt bindings cannot supply broker facts")
    proof = binding["proof"]
    document = strict_json(_pinned_bytes(proof, directory, checked))
    if (
        document["schema"] != DOCUMENT_SCHEMA
        or document["source"] not in {"broker_export", "manual_document"}
        or document["original"]["file"] == proof["file"]
        or not isinstance(document[group], list)
    ):
        raise ValueError("invalid verified document")
    _pinned_bytes(document["original"], directory, checked)
    (row,) = [r for r in document[group] if r[key] == binding[key]]
    return row


def _verified_fee(order_id, fee, settlement, directory, checked):
    if isinstance(fee, str):
        charge = _amount(fee)
        if charge < 0:
            raise ValueError("invalid verified fee")
        return charge, _decimal_text(charge)
    if set(fee) != {"amount", "currency", "fx"}:
        raise ValueError("foreign fee requires documented FX")
    source, target = fee["currency"], settlement["currency"]
    if (
        source == target
        or not isinstance(source, str)
        or len(source) != 3
        or not source.isascii()
        or not source.isalpha()
        or not source.isupper()
    ):
        raise ValueError("foreign fee currency is unverified")
    amount = _amount(fee["amount"])
    conversion = _document_row(
        {"order_id": order_id, "proof": fee["fx"]},
        "fee_conversions",
        "order_id",
        directory,
        checked,
    )
    expected = {
        key: settlement[key]
        for key in (
            "opening_order_id",
            "broker",
            "environment",
            "account_id",
            "account_market",
        )
    } | {"order_id": order_id, "from_currency": source, "to_currency": target}
    if (
        set(conversion) != set(expected) | {"confirmed_at", "fee_amount", "rate"}
        or any(conversion[key] != value for key, value in expected.items())
        or _timestamp(conversion["confirmed_at"])
        != _timestamp(settlement["confirmed_at"])
        or _amount(conversion["fee_amount"]) != amount
    ):
        raise ValueError("FX document does not match the fee settlement")
    rate = _amount(conversion["rate"])
    if rate <= 0:
        raise ValueError("FX rate must be positive")
    with localcontext(Context(prec=80)):
        charge = _amount(_decimal_text(amount * rate))
    return charge, (source, target, _decimal_text(amount), _decimal_text(rate))


def sync_receipts(ledger, report, snapshot, payload, directory):
    if (
        set(payload) != {"schema", "orders", "settlements"}
        or payload["schema"] != RECEIPT_SCHEMA
        or not isinstance(payload["orders"], list)
        or not isinstance(payload["settlements"], list)
    ):
        raise ValueError("invalid receipt package")
    rounds = {}
    settlements = {}
    checked = {}
    for binding in payload["orders"]:
        row = _document_row(binding, "orders", "order_id", directory, checked)
        if set(row) & {"attempt", "opening_order_id"}:
            raise ValueError("broker document cannot supply report bindings")
        order = ReceiptOrder.parse(
            row
            | {k: binding[k] for k in ("attempt", "opening_order_id") if k in binding}
        )
        orders = rounds.setdefault(order.anchor, {})
        if orders.setdefault(order.id, order) != order:
            raise ValueError("conflicting order in receipt package")
    for binding in payload["settlements"]:
        settlement = _document_row(
            binding, "settlements", "opening_order_id", directory, checked
        )
        if set(settlement) != {
            "opening_order_id",
            "broker",
            "environment",
            "account_id",
            "account_market",
            "currency",
            "confirmed_at",
            "fees",
        }:
            raise ValueError("invalid settlement evidence fields")
        anchor = _id(settlement["opening_order_id"])
        if not isinstance(settlement["fees"], dict):
            raise ValueError("invalid settlement fees")
        settlement = settlement | {
            "fees": {
                id_: _verified_fee(id_, fee, settlement, directory, checked)
                for id_, fee in settlement["fees"].items()
            }
        }
        if (
            anchor not in rounds
            or settlements.setdefault(anchor, settlement) != settlement
        ):
            raise ValueError("unbound or conflicting settlement")
    pending = 0
    observed_at = _timestamp(datetime.now(UTC).isoformat())
    with localcontext(Context(prec=64)), ledger._write() as db:
        require_formal_ledger(db)
        prepared = []
        for anchor, orders in rounds.items():
            identity, entries, missing = _round_events(
                db, report, snapshot, orders, anchor, observed_at
            )
            if missing:
                if anchor in settlements:
                    raise ValueError("settlement requires confirmed deals")
                pending += 1
            if entries:
                prepared.append((entries[0][0], anchor, identity, entries))
            elif anchor in settlements:
                raise ValueError("settlement requires a confirmed round")
        prepared.sort(key=lambda item: (item[0], item[1]))
        for _, anchor, identity, entries in prepared:
            round_id = None
            for _, _, fills in entries:
                for fill in fills:
                    actual = ledger._save_fill(db, identity, fill)
                    if round_id is not None and round_id != actual:
                        raise ValueError("deal does not belong to the opening round")
                    round_id = actual
            if {f.event_key for _, _, fills in entries for f in fills} != {
                row[0]
                for row in db.execute(
                    "SELECT event_key FROM trade_events WHERE round_id=? "
                    "AND event_type IN ('buy','sell')",
                    (round_id,),
                )
            }:
                raise ValueError(
                    "package omits previously imported deals for this round"
                )
            if anchor in settlements:
                _settle(
                    db,
                    ledger,
                    round_id,
                    identity,
                    rounds[anchor],
                    settlements[anchor],
                    anchor,
                    observed_at,
                )
    return pending
