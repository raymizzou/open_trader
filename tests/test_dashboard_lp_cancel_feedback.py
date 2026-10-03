"""Offline LP cancel receipts: visible outcomes, lifecycle, and read ordering.

All network calls use the existing deferred harness. Business expiry uses a
controlled clock; run_dashboard_js retains its independent process watchdog.
"""

from __future__ import annotations

import html
import json
import re

import pytest

from tests.test_dashboard_web import _LP162_INTERACTIVE, run_dashboard_js


_CANCEL_INTERACTIVE = _LP162_INTERACTIVE + r'''
enterLpView();
state.predictionMarket.lpDashboard = buildDashboard({lp_orders_today: [sessionRow]});
state.predictionMarket.payload = {lp_dashboard: state.predictionMarket.lpDashboard};
const cancelMatch = (u, m) => m === "POST" && u === "/api/prediction-arbitrage/lp/orders/cancel";
const openCancel = () => openPredictionModal("lp_cancel", null, {scope: "all", orders: [sessionRow]});
const confirmCancel = () => modalClick({modalAction: "lp-cancel-confirm"});
const panelHtml = () => predictionLpCard({lp_dashboard: state.predictionMarket.lpDashboard});
const renderedHtml = () => nodes["prediction-market-root"].innerHTML;
let cancelClock = Date.now();
let timerId = 0;
const cancelTimers = new Map();
Date.now = () => cancelClock;
window.setTimeout = (callback, delay) => {
  const id = ++timerId;
  cancelTimers.set(id, {callback, due: cancelClock + Number(delay)});
  return id;
};
window.clearTimeout = (id) => cancelTimers.delete(id);
const advanceCancelClock = async (milliseconds) => {
  cancelClock += milliseconds;
  for (const [id, timer] of [...cancelTimers]) {
    if (timer.due <= cancelClock && cancelTimers.has(id)) {
      cancelTimers.delete(id);
      timer.callback();
    }
  }
  await drain();
};
const completeCancel = async (result) => {
  deferResponse(cancelMatch).respond(jsonResponse(result));
  deferResponse(dashboardMatch).respond(jsonResponse(buildDashboard()));
  openCancel();
  await confirmCancel();
  await drain();
};
'''


def _run(script: str) -> dict:
    return json.loads(run_dashboard_js(_CANCEL_INTERACTIVE + script))


def _feedback(markup: str) -> str:
    match = re.search(
        r'<section\b[^>]*class="[^"]*\blp-cancel-feedback\b[^"]*"[^>]*>.*?</section>',
        markup,
        re.DOTALL,
    )
    assert match, "LP cancel outcome must be visible in its own feedback section"
    section = match.group()
    assert re.search(r'role="(?:alert|status)"', section)
    return section


def _text(markup: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", markup)).split())


@pytest.mark.parametrize(
    ("receipt", "title", "summary", "reasons"),
    [
        pytest.param(
            {"requested": 4, "canceled": ["ok-1"], "skipped": [
                {"order_id": "gone-1", "reason": "unknown_order"},
                {"order_id": "filled-1", "reason": "not_active"},
            ], "not_canceled": {"failed-1": "exchange rejected"}},
            "撤单未全部成功", "请求 4 笔 · 成功 1 笔 · 跳过 2 笔 · 失败 1 笔",
            ["gone-1", "订单已不在当前委托中", "filled-1", "订单已无可撤份额", "failed-1", "exchange rejected"],
            id="mixed",
        ),
        pytest.param(
            {"requested": 2, "canceled": ["ok-1", "ok-2"], "skipped": [], "not_canceled": {}},
            "撤单成功", "请求 2 笔 · 成功 2 笔 · 跳过 0 笔 · 失败 0 笔", [],
            id="all-success",
        ),
        pytest.param(
            {"requested": 1, "canceled": [], "skipped": [{"order_id": "gone-1", "reason": "unknown_order"}], "not_canceled": {}},
            "撤单结果 · 含跳过", "请求 1 笔 · 成功 0 笔 · 跳过 1 笔 · 失败 0 笔",
            ["gone-1", "订单已不在当前委托中"], id="all-skipped",
        ),
        pytest.param(
            {"requested": 1, "canceled": [], "skipped": [], "not_canceled": {"failed-1": "venue rejected"}},
            "撤单未全部成功", "请求 1 笔 · 成功 0 笔 · 跳过 0 笔 · 失败 1 笔",
            ["failed-1", "venue rejected"], id="all-failed",
        ),
        pytest.param(
            {"requested": 0, "canceled": [], "skipped": [], "not_canceled": {}},
            "无可撤委托", "请求 0 笔 · 成功 0 笔 · 跳过 0 笔 · 失败 0 笔", [],
            id="zero-request",
        ),
    ],
)
def test_lp_cancel_receipt_outcomes_render_counts_and_reasons(
    receipt: dict, title: str, summary: str, reasons: list[str],
) -> None:
    result = _run("const receipt = " + json.dumps(receipt) + r''';
state.predictionMarket.lpCancelSummary = "已登记 · 会话 registered-session";
await completeCancel(receipt);
console.log(JSON.stringify({
  html: panelHtml(), rendered: renderedHtml(), modalKind: predictionModal.kind,
  registrationSummary: state.predictionMarket.lpCancelSummary,
  posts: postCount(), reads: dashCount(),
}));
''')
    text = _text(_feedback(result["html"]))
    assert title in text
    assert summary in text
    assert all(reason in text for reason in reasons)
    assert title in _text(_feedback(result["rendered"]))
    assert result["modalKind"] == ""
    assert result["registrationSummary"] == "已登记 · 会话 registered-session"
    assert result["posts"] == result["reads"] == 1


def test_lp_cancel_feedback_escapes_order_ids_and_all_reason_text() -> None:
    receipt = {
        "requested": 2, "canceled": [],
        "skipped": [{"order_id": '<img src=x onerror="skip()">', "reason": '<svg onload="skipReason()">& skipped'}],
        "not_canceled": {'<img src=x onerror="failure()">': '<script>failureReason()</script>& failed'},
    }
    result = _run("await completeCancel(" + json.dumps(receipt) + r''');
console.log(JSON.stringify({html: panelHtml()}));
''')
    section = _feedback(result["html"])
    for raw in [*receipt["not_canceled"], *receipt["not_canceled"].values(), *receipt["skipped"][0].values()]:
        assert raw not in section
        assert raw in html.unescape(section)
    assert "<script>" not in section
    assert "<svg " not in section
    assert "<img " not in section


def test_lp_cancel_receipt_is_rendered_before_dashboard_refresh_finishes() -> None:
    result = _run(r'''
const post = deferResponse(cancelMatch);
const read = deferResponse(dashboardMatch);
openCancel();
const operation = confirmCancel();
await drain();
post.respond(jsonResponse({requested: 1, canceled: ["ok-1"], skipped: [], not_canceled: {}}));
await drain();
const beforeRefresh = {
  html: renderedHtml(), kind: predictionModal.kind,
  reading: state.predictionMarket.lpDashboardRequestInFlight,
  reads: dashCount(),
};
read.respond(jsonResponse(buildDashboard()));
await operation;
await drain();
console.log(JSON.stringify({...beforeRefresh, finalHtml: renderedHtml()}));
''')
    assert "撤单成功" in _text(_feedback(result["html"]))
    assert result["kind"] == ""
    assert result["reading"] is True
    assert result["reads"] == 1
    assert 'data-order-id="sys-order-1"' in result["html"]
    assert 'data-order-id="sys-order-1"' not in result["finalHtml"]


def test_lp_cancel_valid_receipt_survives_failed_dashboard_refresh() -> None:
    result = _run(r'''
const post = deferResponse(cancelMatch);
const read = deferResponse(dashboardMatch);
openCancel();
const operation = confirmCancel();
post.respond(jsonResponse({requested: 1, canceled: ["ok-1"], skipped: [], not_canceled: {}}));
await drain();
const beforeRefresh = renderedHtml();
read.respond(new Error("dashboard refresh unavailable"));
await operation;
await drain();
console.log(JSON.stringify({beforeRefresh, afterRefresh: renderedHtml(),
  stale: state.predictionMarket.lpDashboard.stale,
  dashboardError: state.predictionMarket.lpDashboardError,
  oldOrder: state.predictionMarket.lpDashboard.lp_orders_today[0]?.order_id,
  posts: postCount(), reads: dashCount(), modalKind: predictionModal.kind,
}));
''')
    before = _text(_feedback(result["beforeRefresh"]))
    after = _text(_feedback(result["afterRefresh"]))
    assert "撤单成功" in before
    assert "请求 1 笔 · 成功 1 笔 · 跳过 0 笔 · 失败 0 笔" in before
    assert after == before
    assert result["stale"] is True
    assert result["dashboardError"] == "dashboard refresh unavailable"
    assert result["oldOrder"] == "sys-order-1"
    assert result["posts"] == result["reads"] == 1
    assert result["modalKind"] == ""


def test_lp_cancel_pending_receipt_blocks_duplicate_confirm_after_reopen() -> None:
    result = _run(r'''
const post = deferResponse(cancelMatch);
deferResponse(dashboardMatch).respond(jsonResponse(buildDashboard()));
openCancel();
const first = confirmCancel();
await drain();
const pending = {busy: predictionModal.busy, html: renderedHtml()};
await advanceCancelClock(30000);
pending.afterWait = renderedHtml();
await confirmCancel();
await modalClick({modalAction: "cancel"});
openCancel();
const newModal = {epoch: predictionModal.epoch, html: modalRoot.innerHTML};
await confirmCancel();
const whilePending = {posts: postCount(), epoch: predictionModal.epoch, busy: predictionModal.busy};
post.respond(jsonResponse({requested: 1, canceled: ["ok-1"], skipped: [], not_canceled: {}}));
await first;
await drain();
console.log(JSON.stringify({pending, whilePending, newModal, after: {
  posts: postCount(), kind: predictionModal.kind, epoch: predictionModal.epoch,
  html: modalRoot.innerHTML, feedback: panelHtml(), busy: predictionModal.busy,
}}));
''')
    assert result["pending"]["busy"] is True
    assert "正在撤单" in _text(_feedback(result["pending"]["html"]))
    assert "正在撤单" in _text(_feedback(result["pending"]["afterWait"]))
    assert result["whilePending"]["posts"] == result["after"]["posts"] == 1
    assert result["whilePending"]["busy"] is False
    assert result["after"]["kind"] == "lp_cancel"
    assert result["after"]["epoch"] == result["newModal"]["epoch"]
    assert result["after"]["html"] == result["newModal"]["html"]
    assert result["after"]["busy"] is False
    assert "撤单成功" in _text(_feedback(result["after"]["feedback"]))


def test_lp_cancel_modal_buttons_show_pending_and_reenable_reopened_confirmation() -> None:
    result = _run(r'''
// The shared lightweight DOM does not parse innerHTML. Bind button instances
// from the real modal markup so attribute and later DOM mutations are tested.
const bindCancelButtons = () => {
  modalRoot._qs = {};
  for (const action of ["cancel", "lp-cancel-confirm"]) {
    const markup = modalRoot.innerHTML.match(new RegExp('<button([^>]*data-modal-action="' + action + '"[^>]*)>([^<]*)</button>'));
    if (!markup) throw new Error("Missing rendered button " + action);
    const button = new Element();
    button.dataset.modalAction = action;
    button.disabled = /\bdisabled\b/.test(markup[1]);
    button.textContent = markup[2];
    modalRoot._qs["[data-modal-action='" + action + "']"] = button;
  }
};
const buttons = () => {
  const confirm = modalRoot.querySelector("[data-modal-action='lp-cancel-confirm']");
  const cancel = modalRoot.querySelector("[data-modal-action='cancel']");
  return {confirmDisabled: confirm.disabled, confirmText: confirm.textContent,
    cancelDisabled: cancel.disabled, cancelText: cancel.textContent};
};
const post = deferResponse(cancelMatch);
deferResponse(dashboardMatch).respond(jsonResponse(buildDashboard()));
openCancel();
bindCancelButtons();
const initial = buttons();
const operation = confirmCancel();
await drain();
const pending = buttons();
await modalClick({modalAction: "cancel"});
openCancel();
bindCancelButtons();
const reopened = buttons();
await confirmCancel();
post.respond(jsonResponse({requested: 1, canceled: ["ok-1"], skipped: [], not_canceled: {}}));
await operation;
await drain();
console.log(JSON.stringify({initial, pending, reopened, completed: buttons(),
  kind: predictionModal.kind, posts: postCount()}));
''')
    assert result["initial"]["confirmDisabled"] is False
    assert result["pending"]["confirmDisabled"] is True
    assert "正在撤单" in result["pending"]["confirmText"]
    assert result["pending"]["cancelDisabled"] is False
    assert result["pending"]["cancelText"] == "关闭"
    assert result["reopened"]["confirmDisabled"] is True
    assert result["reopened"]["cancelDisabled"] is False
    assert result["completed"]["confirmDisabled"] is False
    assert result["completed"]["cancelDisabled"] is False
    assert result["kind"] == "lp_cancel"
    assert result["posts"] == 1


@pytest.mark.parametrize("late_error", [False, True], ids=["receipt", "network-error"])
def test_lp_cancel_late_outcome_does_not_change_newer_modal(late_error: bool) -> None:
    result = _run("const lateError = " + json.dumps(late_error) + r''';
const post = deferResponse(cancelMatch);
deferResponse(dashboardMatch).respond(jsonResponse(buildDashboard()));
openCancel();
const operation = confirmCancel();
await drain();
await modalClick({modalAction: "cancel"});
openPredictionModal("lp_order", null, lpOrderIntent(candidateRow));
setPredictionModalBusy(true);
const before = {epoch: predictionModal.epoch, html: modalRoot.innerHTML, busy: predictionModal.busy};
post.respond(lateError ? new Error("socket closed") : jsonResponse({
  requested: 1, canceled: [], skipped: [{order_id: "gone-1", reason: "unknown_order"}], not_canceled: {},
}));
await operation;
await drain();
console.log(JSON.stringify({before, after: {
  epoch: predictionModal.epoch, html: modalRoot.innerHTML, busy: predictionModal.busy,
}, kind: predictionModal.kind, html: panelHtml(), reads: dashCount()}));
''')
    assert result["after"] == result["before"]
    assert result["kind"] == "lp_order"
    text = _text(_feedback(result["html"]))
    assert ("撤单结果待核对" if late_error else "订单已不在当前委托中") in text
    assert result["reads"] == 1


def test_lp_cancel_network_error_is_unknown_and_refreshes_without_retry() -> None:
    result = _run(r'''
const post = deferResponse(cancelMatch);
const read = deferResponse(dashboardMatch);
openCancel();
const operation = confirmCancel();
post.respond(new Error("socket closed after send"));
await drain();
const beforeRefresh = {
  html: renderedHtml(), kind: predictionModal.kind, busy: predictionModal.busy,
  posts: postCount(), reads: dashCount(),
};
read.respond(jsonResponse(buildDashboard()));
await operation;
await advanceCancelClock(30000);
console.log(JSON.stringify({beforeRefresh, finalPosts: postCount()}));
''')
    before = result["beforeRefresh"]
    text = _text(_feedback(before["html"]))
    assert "撤单结果待核对" in text
    assert "不能确认成功或失败" in text
    assert "撤单成功" not in text
    assert not re.search(r"成功 \d+ 笔", text)
    assert before["kind"] == ""
    assert before["busy"] is False
    assert before["posts"] == result["finalPosts"] == before["reads"] == 1


@pytest.mark.parametrize(
    "receipt",
    [
        pytest.param(None, id="null-receipt"),
        pytest.param({}, id="empty-receipt"),
        pytest.param({"requested": 1, "canceled": ["ok-1"], "not_canceled": {}}, id="missing-skipped"),
        pytest.param({"requested": 0, "skipped": [], "not_canceled": {}}, id="missing-canceled"),
        pytest.param({"requested": 0, "canceled": [], "skipped": []}, id="missing-not-canceled"),
        pytest.param({"requested": 2, "canceled": ["ok-1"], "skipped": [], "not_canceled": {}}, id="count-mismatch"),
        pytest.param({"canceled": ["ok-1"], "skipped": [], "not_canceled": {}}, id="missing-requested"),
        pytest.param({"requested": 1, "canceled": [None], "skipped": [], "not_canceled": {}}, id="null-canceled-id"),
        pytest.param({"requested": 1, "canceled": [""], "skipped": [], "not_canceled": {}}, id="empty-canceled-id"),
        pytest.param({"requested": 1, "canceled": ["  "], "skipped": [], "not_canceled": {}}, id="blank-canceled-id"),
        pytest.param({"requested": 1, "canceled": [123], "skipped": [], "not_canceled": {}}, id="nonstring-canceled-id"),
        pytest.param({"requested": 1, "canceled": [], "skipped": [None], "not_canceled": {}}, id="null-skipped-row"),
        pytest.param({"requested": 1, "canceled": [], "skipped": [{"reason": "unknown_order"}], "not_canceled": {}}, id="missing-skipped-id"),
        pytest.param({"requested": 1, "canceled": [], "skipped": [{"order_id": "gone-1"}], "not_canceled": {}}, id="missing-skipped-reason"),
        pytest.param({"requested": 1, "canceled": [], "skipped": [{"order_id": "", "reason": "unknown_order"}], "not_canceled": {}}, id="empty-skipped-id"),
        pytest.param({"requested": 1, "canceled": [], "skipped": [{"order_id": "gone-1", "reason": {"code": "unknown_order"}}], "not_canceled": {}}, id="nonstring-skipped-reason"),
        pytest.param({"requested": 1, "canceled": [], "skipped": [], "not_canceled": {"": "venue rejected"}}, id="empty-failure-id"),
        pytest.param({"requested": 1, "canceled": [], "skipped": [], "not_canceled": {"  ": "venue rejected"}}, id="blank-failure-id"),
        pytest.param({"requested": 1, "canceled": [], "skipped": [], "not_canceled": {"failed-1": None}}, id="null-failure-reason"),
        pytest.param({"requested": 1, "canceled": [], "skipped": [], "not_canceled": {"failed-1": 503}}, id="nonstring-failure-reason"),
    ],
)
def test_lp_cancel_incomplete_receipt_cannot_report_success(receipt: dict | None) -> None:
    result = _run("await completeCancel(" + json.dumps(receipt) + r''');
console.log(JSON.stringify({html: panelHtml(), posts: postCount(), reads: dashCount(), kind: predictionModal.kind}));
''')
    text = _text(_feedback(result["html"]))
    assert "撤单结果待核对" in text
    assert "不能确认成功或失败" in text
    assert "撤单成功" not in text
    assert not re.search(r"成功 \d+ 笔", text)
    assert result["posts"] == result["reads"] == 1
    assert result["kind"] == ""


@pytest.mark.parametrize(
    ("receipt", "summary", "order_id"),
    [
        pytest.param(
            {"requested": 1, "canceled": [], "skipped": [{"order_id": "gone-1", "reason": "  "}], "not_canceled": {}},
            "请求 1 笔 · 成功 0 笔 · 跳过 1 笔 · 失败 0 笔", "gone-1", id="blank-skipped-reason",
        ),
        pytest.param(
            {"requested": 1, "canceled": [], "skipped": [], "not_canceled": {"failed-1": ""}},
            "请求 1 笔 · 成功 0 笔 · 跳过 0 笔 · 失败 1 笔", "failed-1", id="empty-failure-reason",
        ),
    ],
)
def test_lp_cancel_blank_reason_preserves_confirmed_outcome_with_explicit_fallback(
    receipt: dict, summary: str, order_id: str,
) -> None:
    result = _run("await completeCancel(" + json.dumps(receipt) + r''');
console.log(JSON.stringify({html: panelHtml(), posts: postCount(), reads: dashCount()}));
''')
    text = _text(_feedback(result["html"]))
    assert summary in text
    assert order_id in text
    assert "服务端未提供原因" in text
    assert "撤单结果待核对" not in text
    assert "撤单成功" not in text
    assert result["posts"] == result["reads"] == 1


@pytest.mark.parametrize(
    ("scope", "expected_body"),
    [
        pytest.param("order", {"confirm": True, "order_ids": ["sys-order-1", "man-order-1"]}, id="order"),
        pytest.param("market", {"confirm": True, "condition_id": "condition-fed"}, id="market"),
        pytest.param("all", {"confirm": True, "scope": "all"}, id="all"),
    ],
)
def test_lp_cancel_preserves_request_selector_contract(scope: str, expected_body: dict) -> None:
    result = _run("const scope = " + json.dumps(scope) + r''';
deferResponse(cancelMatch).respond(jsonResponse({requested: 0, canceled: [], skipped: [], not_canceled: {}}));
deferResponse(dashboardMatch).respond(jsonResponse(buildDashboard()));
openPredictionModal("lp_cancel", null, {scope, conditionId: "condition-fed", orders: [sessionRow, manualRow]});
await confirmCancel();
await drain();
console.log(JSON.stringify({posts: fetchCalls.filter((call) => call.method === "POST")}));
''')
    assert len(result["posts"]) == 1
    assert result["posts"][0]["url"] == "/api/prediction-arbitrage/lp/orders/cancel"
    assert json.loads(result["posts"][0]["body"]) == expected_body


@pytest.mark.parametrize("old_fails", [False, True], ids=["old-success", "old-error"])
def test_lp_cancel_forced_refresh_supersedes_old_read_without_unlocking_new_read(old_fails: bool) -> None:
    result = _run("const oldFails = " + json.dumps(old_fails) + r''';
const oldRead = deferResponse(dashboardMatch);
const oldOperation = fetchPredictionLpDashboard();
await drain();
const post = deferResponse(cancelMatch);
const newRead = deferResponse(dashboardMatch);
openCancel();
const cancelOperation = confirmCancel();
post.respond(jsonResponse({requested: 1, canceled: ["ok-1"], skipped: [], not_canceled: {}}));
await drain();
const afterReceipt = {reads: dashCount(), html: renderedHtml()};
oldRead.respond(oldFails ? new Error("old read unavailable") : jsonResponse(buildDashboard({marker: "old"})));
await oldOperation;
await drain();
const afterOldRead = {
  reading: state.predictionMarket.lpDashboardRequestInFlight,
  marker: state.predictionMarket.lpDashboard.marker || "initial",
  error: state.predictionMarket.lpDashboardError,
};
// A routine read must still be suppressed while the newer forced read owns the lock.
const routineRead = fetchPredictionLpDashboard();
await drain();
const countAfterRoutine = dashCount();
newRead.respond(jsonResponse(buildDashboard({marker: "new"})));
await Promise.all([cancelOperation, routineRead]);
await drain();
console.log(JSON.stringify({afterReceipt, afterOldRead, countAfterRoutine,
  finalMarker: state.predictionMarket.lpDashboard.marker,
  finalReading: state.predictionMarket.lpDashboardRequestInFlight,
}));
''')
    assert result["afterReceipt"]["reads"] == 2
    assert "撤单成功" in _text(_feedback(result["afterReceipt"]["html"]))
    assert result["afterOldRead"] == {"reading": True, "marker": "initial", "error": ""}
    assert result["countAfterRoutine"] == 2
    assert result["finalMarker"] == "new"
    assert result["finalReading"] is False


@pytest.mark.parametrize("old_finishes_first", [False, True], ids=["old-last", "old-first"])
@pytest.mark.parametrize("old_fails", [False, True], ids=["old-success", "old-error"])
def test_lp_cancel_refresh_survives_concurrent_manual_pause(
    old_finishes_first: bool, old_fails: bool,
) -> None:
    result = _run("const oldFinishesFirst = " + json.dumps(old_finishes_first)
        + "; const oldFails = " + json.dumps(old_fails) + r''';
state.predictionMarket.lpDashboard.auto = {desired_running: true, scheduler_running: true};
const post = deferResponse(cancelMatch);
const pause = deferResponse((u, m) => m === "POST" && u.endsWith("/lp/auto/pause"));
const oldRead = deferResponse(dashboardMatch);
const freshRead = deferResponse(dashboardMatch);
openCancel();
const cancelOperation = confirmCancel();
await drain();
await modalClick({modalAction: "cancel"});
const pauseOperation = controlLpAuto("pause");
post.respond(jsonResponse({requested: 1, canceled: ["sys-order-1"], skipped: [], not_canceled: {}}));
await drain();
pause.respond(jsonResponse({desired_running: false, pause_confirmed: true}));
await drain();
const finishOld = () => oldRead.respond(oldFails ? new Error("superseded read failed")
  : jsonResponse(buildDashboard({lp_orders_today: [sessionRow], auto: {desired_running: true}})));
if (oldFinishesFirst) {
  finishOld();
  await cancelOperation;
  // The old request must not release the replacement read's in-flight guard.
  await fetchPredictionLpDashboard();
}
freshRead.respond(jsonResponse(buildDashboard({
  auto: {desired_running: false, pause_confirmed: true, scheduler_running: true},
})));
await pauseOperation;
if (!oldFinishesFirst) finishOld();
await cancelOperation;
await drain();
console.log(JSON.stringify({html: renderedHtml(), reads: dashCount(),
  posts: postCount(), error: state.predictionMarket.lpDashboardError}));
''')
    assert 'data-order-id="sys-order-1"' not in result["html"]
    assert "人工意愿：暂停" in _text(result["html"])
    assert "撤单成功" in _text(_feedback(result["html"]))
    assert result["error"] == ""
    assert result["posts"] == result["reads"] == 2


def test_lp_cancel_older_refresh_and_expiry_cannot_replace_newer_receipt() -> None:
    result = _run(r'''
const postA = deferResponse(cancelMatch);
const readA = deferResponse(dashboardMatch);
openCancel();
const operationA = confirmCancel();
postA.respond(jsonResponse({requested: 1, canceled: ["old-ok"], skipped: [], not_canceled: {}}));
await drain();
const olderExpiry = [...cancelTimers.values()].map((item) => item.callback);
await advanceCancelClock(5000);
const postB = deferResponse(cancelMatch);
const readB = deferResponse(dashboardMatch);
openCancel();
const operationB = confirmCancel();
postB.respond(jsonResponse({requested: 2, canceled: [], skipped: [], not_canceled: {"new-a": "denied A", "new-b": "denied B"}}));
await drain();
readB.respond(jsonResponse(buildDashboard({marker: "new"})));
await drain();
const newest = panelHtml();
// Deliberately invoke a canceled callback: a queued old timer must be fenced too.
for (const callback of olderExpiry) callback();
readA.respond(jsonResponse(buildDashboard({marker: "old"})));
await Promise.all([operationA, operationB]);
await drain();
console.log(JSON.stringify({newest, final: panelHtml(), marker: state.predictionMarket.lpDashboard.marker, posts: postCount(), reads: dashCount()}));
''')
    newest = _text(_feedback(result["newest"]))
    assert "请求 2 笔 · 成功 0 笔 · 跳过 0 笔 · 失败 2 笔" in newest
    assert _text(_feedback(result["final"])) == newest
    assert result["marker"] == "new"
    assert result["posts"] == result["reads"] == 2


def test_lp_cancel_completed_feedback_expires_at_fifteen_seconds() -> None:
    result = _run(r'''
await completeCancel({requested: 1, canceled: ["ok-1"], skipped: [], not_canceled: {}});
await advanceCancelClock(14999);
const before = {html: panelHtml(), rendered: renderedHtml()};
await advanceCancelClock(1);
console.log(JSON.stringify({before, after: {html: panelHtml(), rendered: renderedHtml()}}));
''')
    assert "撤单成功" in _text(_feedback(result["before"]["html"]))
    assert "撤单成功" in _text(_feedback(result["before"]["rendered"]))
    assert "lp-cancel-feedback" not in result["after"]["html"]
    assert "lp-cancel-feedback" not in result["after"]["rendered"]


@pytest.mark.parametrize("reopened", [False, True], ids=["owned-modal", "newer-modal"])
def test_lp_cancel_result_focus_is_visible_and_never_stolen_from_newer_modal(reopened: bool) -> None:
    result = _run("const reopened = " + json.dumps(reopened) + r''';
const root = nodes["prediction-market-root"];
let renderedMarkup = root.innerHTML;
let feedbackNode = null;
let feedbackNodeId = 0;
const focused = [];
const otherInput = {matches(){return false;}, focus(){document.activeElement = this;}};
// Model replacement DOM nodes and focus loss on removal. This observes the
// real render function's focus restoration rather than retaining one fake node.
Object.defineProperty(root, "innerHTML", {
  get(){return renderedMarkup;},
  set(markup){
    if (document.activeElement === feedbackNode) document.activeElement = document.body;
    renderedMarkup = markup;
    feedbackNode = markup.includes("lp-cancel-feedback") ? {
      id: ++feedbackNodeId,
      matches(selector){return selector === ".lp-cancel-feedback";},
      focus(options){focused.push({id: this.id, options: options || null}); document.activeElement = this;},
    } : null;
  },
});
const originalQuery = root.querySelector.bind(root);
root.querySelector = (selector) => selector === ".lp-cancel-feedback" ? feedbackNode : originalQuery(selector);
const post = deferResponse(cancelMatch);
const read = deferResponse(dashboardMatch);
openCancel();
const operation = confirmCancel();
await drain();
if (reopened) {
  await modalClick({modalAction: "cancel"});
  openPredictionModal("lp_order", null, lpOrderIntent(candidateRow));
  otherInput.focus();
}
post.respond(jsonResponse({requested: 1, canceled: ["ok-1"], skipped: [], not_canceled: {}}));
await drain();
const beforeRefresh = {focused: [...focused], receiptFocused: document.activeElement === feedbackNode,
  otherFocused: document.activeElement === otherInput, html: renderedHtml()};
read.respond(jsonResponse(buildDashboard()));
await operation;
await drain();
console.log(JSON.stringify({beforeRefresh, afterRefresh: {
  focused, receiptFocused: document.activeElement === feedbackNode,
  otherFocused: document.activeElement === otherInput,
}}));
''')
    assert 'tabindex="-1"' in _feedback(result["beforeRefresh"]["html"])
    before, after = result["beforeRefresh"], result["afterRefresh"]
    if reopened:
        assert before["focused"] == after["focused"] == []
        assert before["otherFocused"] is after["otherFocused"] is True
        assert before["receiptFocused"] is after["receiptFocused"] is False
    else:
        assert before["receiptFocused"] is after["receiptFocused"] is True
        assert len(before["focused"]) == 1
        assert before["focused"][0]["options"] is None
        assert len(after["focused"]) == 2
        assert after["focused"][1]["options"] == {"preventScroll": True}
        assert after["focused"][1]["id"] != before["focused"][0]["id"]
