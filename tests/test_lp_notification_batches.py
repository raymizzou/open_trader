"""Receiver-level contracts for durable immutable LP notification batches."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from open_trader.notifications import CompositeNotifier, FeishuAppNotifier, XiaoaiSSHNotifier
from open_trader.polymarket_lp import PolymarketLPService
from open_trader.polymarket_lp_auto import LPAutoPool
from tests.test_lp_notification_delivery import arm_fault, notice_service
from tests.test_lp_auto_pool import setup


class NotificationBatchCase:
    def __init__(self, tmp_path, automatic, *, accept_failed=False, count=2):
        self.automatic = automatic
        self.accept_failed = accept_failed
        self.fail_feishu = True
        self.calls = []
        self.accepted = {}
        self.voice_calls = []
        titles = ("Market Alpha", "Market Beta", "Market Gamma")[:count]
        if automatic:
            self.engine, self.exchange, self.service, self.store = setup(tmp_path, count)
            self.current = [self.service._now()]
            self.service.clock = lambda: self.current[0]
            self.engine.lp_auto_configure({"budget_usd": "100", "target_buy_count": count})
            self.engine.lp_auto_set_desired_running(True)
            rows = self.engine.lp_auto_run_once()["intents"]
            worker = self.service._attention_thread
            if worker is not None:
                worker.join(timeout=3)
                assert not worker.is_alive()
            self.ids = [row["intent_id"] for row in rows]
            self.sids = [row["session_id"] for row in rows]
            self.pool = self.engine._auto_pool
            self.channel = "feishu_app"
            self.prefix = "attention_recovery"
            for index, title in enumerate(titles):
                self.store.lp_update_session(self.sids[index], patch={"market_title": title})
                self.patch(index, attention_episode=self.ids[index] + ":fault",
                    attention_since=(self.current[0] - timedelta(seconds=301)).isoformat(),
                    attention_due=False, attention_recovery_due=True,
                    attention_delivered_channels=[self.channel, "xiaoai"],
                    attention_recovered_at=(self.current[0] - timedelta(seconds=60)).isoformat(),
                    attention_recovery_ready_since=(self.current[0] - timedelta(seconds=60)).isoformat(),
                    attention_recovery_first_checked_at=(self.current[0] - timedelta(seconds=60)).isoformat(),
                    reconcile_error=None, reconcile_reason=None, financial_status="known",
                    checked_at=self.current[0].isoformat())
        else:
            self.service, self.store, self.current, _ = notice_service(tmp_path)
            self.ids = self.sids = ["one", "two", "three"][:count]
            self.channel = "feishu"
            self.prefix = "needs_attention_recovery"
            for index, title in enumerate(titles):
                sid = self.sids[index]
                arm_fault(self.store, sid, title)
                self.store.lp_update_session(sid, state="entry_open", patch={
                    "reconciliation": None, "position_reconciled": True,
                    "facts_checked_at": self.current[0].isoformat(),
                    "needs_attention_due": False, "needs_attention_recovery_due": True,
                    "needs_attention_recovery_episode": sid + ":fault",
                    "needs_attention_recovery_channels": [self.channel, "xiaoai"],
                    "needs_attention_recovery_channel_status": {},
                    "needs_attention_recovery_ready_since": (self.current[0] - timedelta(seconds=60)).isoformat(),
                    "needs_attention_recovery_first_checked_at": (self.current[0] - timedelta(seconds=60)).isoformat(),
                })
        self.app = FeishuAppNotifier(app_id="fixture", app_secret="fixture", receive_id_type="chat_id",
                                    receive_id="fixture-chat", post_json=self.post)
        case = self
        class Voice(XiaoaiSSHNotifier):
            def __init__(self):
                pass
            def notify(self, title, message):
                case.voice_calls.append((title, message))
        self.voice = Voice()
        self.install_notifier()

    def post(self, url, payload, headers, timeout):
        if url.endswith("/auth/v3/tenant_access_token/internal"):
            return {"code": 0, "tenant_access_token": "fixture-token"}
        self.calls.append(deepcopy(payload))
        if not self.fail_feishu or self.accept_failed:
            self.accepted.setdefault(payload["uuid"], json.loads(payload["content"])["text"])
        return {"code": 1, "msg": "delivery acknowledgement unavailable"} if self.fail_feishu else {"code": 0}

    def install_notifier(self):
        if self.automatic:
            self.engine._notifier = CompositeNotifier([self.app, self.voice])
        else:
            def notify(title, message, voice, *, channels):
                results = {}
                if "feishu" in channels:
                    try:
                        self.app.notify(title, message)
                    except RuntimeError:
                        results["feishu"] = False
                    else:
                        results["feishu"] = True
                if "xiaoai" in channels:
                    self.voice.notify(title, message)
                    results["xiaoai"] = True
                return results
            self.service.set_protection_notifier(notify)

    def read(self, index):
        return self.pool._read()["intents"][self.ids[index]] if self.automatic else self.store.lp_session(self.sids[index])

    def patch(self, index, **patch):
        if self.automatic:
            self.pool._update(lambda document: document["intents"][self.ids[index]].update(patch))
        else:
            self.store.lp_update_session(self.sids[index], patch=patch)

    def refresh(self, *indices, advance=0):
        self.current[0] += timedelta(seconds=advance)
        for index in indices or range(len(self.ids)):
            self.patch(index, **{("checked_at" if self.automatic else "facts_checked_at"): self.current[0].isoformat()})

    def defer(self, index, seconds):
        self.patch(index, **{("attention_send_retry_at" if self.automatic else "needs_attention_recovery_retry_at"):
                           (self.current[0] + timedelta(seconds=seconds)).isoformat()})

    def flush(self, *indices):
        for index in indices or range(len(self.ids)):
            if self.automatic:
                self.pool.flush_attention(self.sids[index])
            else:
                self.service.flush_session_recovery(self.sids[index])

    def restart(self):
        if self.automatic:
            self.pool = LPAutoPool(self.engine)
            self.engine._auto_pool = self.pool
        else:
            self.service = PolymarketLPService(self.store, object(), clock=lambda: self.current[0])
        self.install_notifier()

    def business_image(self):
        sessions = [{key: value for key, value in row.items()
                     if not key.startswith(("needs_attention_", "attention_"))
                     and key not in {"updated_at", "_lp_revision"}} for row in self.store.lp_sessions()]
        if not self.automatic:
            return sessions, self.store.lp_trade_generation()
        document = deepcopy(self.pool._read())
        for row in document["intents"].values():
            for key in list(row):
                if key.startswith("attention_"):
                    row.pop(key)
        return document, self.pool.state(include_intents=False), sessions, self.store.lp_trade_generation()


def make_batch_case(tmp_path, automatic, *, accept_failed=False, count=2):
    return NotificationBatchCase(tmp_path, automatic, accept_failed=accept_failed, count=count)


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("first_delivered", [False, True])
def test_receiver_dedup_restart_subset_preserves_original_body(tmp_path, automatic, first_delivered):
    case = make_batch_case(tmp_path, automatic, accept_failed=first_delivered)
    before = case.business_image()
    case.flush()
    assert len(case.calls) == 1 and len(case.voice_calls) == 1
    assert len(case.accepted) == int(first_delivered)
    assert case.business_image() == before
    original = case.calls[0]
    assert "Market Alpha" in original["content"] and "Market Beta" in original["content"]

    case.refresh(0, 1, advance=60)
    case.defer(1, 60)  # Fresh original member, but its delivery retry is not due.
    case.restart()
    case.fail_feishu = False
    before_retry = case.business_image()
    case.flush(0)
    assert len(case.calls) == 2 and len(case.voice_calls) == 1
    assert case.calls[-1]["uuid"] == original["uuid"]
    assert not case.read(0).get(case.prefix + "_due")
    assert case.read(1)[case.prefix + "_due"] is True
    assert case.business_image() == before_retry

    # Alpha has already been acknowledged. Its aged notification evidence
    # must not prevent Beta's ordinary retry of the same historical event.
    case.refresh(1, advance=61)
    before_final = case.business_image()
    case.flush(1)
    assert len(case.calls) == 3 and len(case.voice_calls) == 1
    assert not case.read(1).get(case.prefix + "_due")
    assert case.business_image() == before_final
    assert all(payload["uuid"] == original["uuid"] for payload in case.calls)
    assert len(case.accepted) == 1
    accepted_text = next(iter(case.accepted.values()))
    assert "Market Alpha" in accepted_text and "Market Beta" in accepted_text
    assert all(payload["content"] == original["content"] for payload in case.calls)


@pytest.mark.parametrize("automatic", [False, True])
def test_legacy_bodyless_pending_channel_gets_new_identity_without_repeating_success(tmp_path, automatic):
    from open_trader.notifications import notification_delivery_episode

    case = make_batch_case(tmp_path, automatic)
    old_group = "legacy-bodyless-group"
    case.fail_feishu = False
    with notification_delivery_episode(("lp-auto:" if automatic else "lp-session:") + old_group):
        case.app.notify("Earlier recovery", "Market Alpha and Market Beta")
    old_uuid = case.calls[-1]["uuid"]
    case.fail_feishu = True
    for index in (0, 1):
        if automatic:
            case.patch(index, attention_recovery_delivery_group=old_group,
                       attention_recovery_attempted_channels=[case.channel, "xiaoai"],
                       attention_recovery_delivered_channels=["xiaoai"])
        else:
            case.patch(index, needs_attention_recovery_delivery_group=old_group,
                       needs_attention_recovery_channel_status={"feishu": False, "xiaoai": True})
    before = case.business_image()
    case.flush()
    assert len(case.calls) == 2 and case.voice_calls == []
    replacement = case.calls[-1]
    assert replacement["uuid"] != old_uuid
    assert all(case.read(index)[case.prefix + "_due"] for index in (0, 1))
    assert case.business_image() == before

    case.refresh(0, 1, advance=60)
    case.restart()
    case.fail_feishu = False
    before_retry = case.business_image()
    case.flush()
    assert len(case.calls) == 3 and case.voice_calls == []
    assert case.calls[-1]["uuid"] == replacement["uuid"]
    assert case.calls[-1]["content"] == replacement["content"]
    assert not any(case.read(index).get(case.prefix + "_due") for index in (0, 1))
    assert len(case.accepted) == 2  # Approved legacy migration can display one duplicate event.
    assert case.business_image() == before_retry


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("restart", [False, True])
def test_immutable_payload_survives_local_ack_failure(tmp_path, monkeypatch, automatic, restart):
    import sqlite3

    case = make_batch_case(tmp_path, automatic)
    case.fail_feishu = False
    target = case.pool if automatic else case.store
    name = "_finish_attention_delivery" if automatic else "lp_finish_attention_notification"
    finish = getattr(target, name)
    pending = [True]

    def fail_once(*args, **kwargs):
        if pending:
            pending.pop()
            raise sqlite3.OperationalError("database is locked")
        return finish(*args, **kwargs)

    monkeypatch.setattr(target, name, fail_once)
    before = case.business_image()
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        case.flush()
    assert len(case.calls) == 1 and len(case.accepted) == 1
    original = case.calls[0]
    assert case.business_image() == before
    case.refresh(0, 1, advance=60)
    if restart:
        case.restart()
    before_retry = case.business_image()
    case.flush()
    assert len(case.calls) == (2 if restart else 1)
    assert len(case.accepted) == 1
    assert all(payload["uuid"] == original["uuid"] and payload["content"] == original["content"]
               for payload in case.calls)
    assert not any(case.read(index).get(case.prefix + "_due") for index in (0, 1))
    assert case.business_image() == before_retry


@pytest.mark.parametrize("automatic", [False, True])
def test_retirement_replacement_excludes_already_acknowledged_original_members(tmp_path, automatic):
    case = make_batch_case(tmp_path, automatic, count=3)
    case.flush()
    assert len(case.calls) == 1 and len(case.voice_calls) == 1
    original = case.calls[0]
    case.refresh(0, 1, 2, advance=60)
    case.defer(1, 60)
    case.defer(2, 60)
    case.fail_feishu = False
    case.flush(0)
    assert case.calls[-1]["uuid"] == original["uuid"]
    assert case.calls[-1]["content"] == original["content"]
    assert not case.read(0).get(case.prefix + "_due")
    acknowledged = deepcopy(case.read(0))

    case.refresh(2, advance=60)
    case.store.lp_update_session(case.sids[1], patch={"account_baseline_archive": {"id": "retired"}})
    if automatic:
        case.patch(1, account_baseline_archive={"id": "retired"})
    archived = deepcopy(case.read(1))
    case.fail_feishu = True
    before = case.business_image()
    case.flush(2)
    assert len(case.calls) == 3 and len(case.voice_calls) == 1
    replacement = case.calls[-1]
    assert replacement["uuid"] != original["uuid"]
    assert "Market Gamma" in replacement["content"]
    assert "Market Alpha" not in replacement["content"] and "Market Beta" not in replacement["content"]
    assert case.read(0) == acknowledged and case.read(1) == archived
    assert case.business_image() == before

    case.refresh(2, advance=60)
    case.restart()
    case.fail_feishu = False
    case.flush(2)
    assert len(case.calls) == 4 and len(case.voice_calls) == 1
    assert case.calls[-1]["uuid"] == replacement["uuid"]
    assert case.calls[-1]["content"] == replacement["content"]
    assert not case.read(2).get(case.prefix + "_due")
