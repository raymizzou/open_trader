"""Issue #96 relation auto-confirm: config-driven Tier 1 governance.

The whitelist is deliberately tiny: a tier pairs a ``discovery_source`` with
``relation_type`` values, and only ``VENUE_METADATA`` mechanical relations
(``NATIVE_COMPLEMENT`` / ``EXACTLY_ONE``) ship enabled in production — modes
are exactly ``disabled | active``; there is no dry-run and no pilot ladder.
Deterministic-rule and LLM-sourced relations stay manual by absence.

Three layers live here:

* :func:`load_auto_confirm_policy` parses and validates the policy document,
  failing closed per tier (an invalid tier becomes disabled and surfaces its
  configuration error instead of poisoning the other tiers);
* :func:`select_auto_confirm_items` is the pure whitelist/order/cap selection;
* :class:`RelationAutoConfirmRunner` runs one approval round per enabled tier
  through the catalog's single-transaction ``approve_many``, carrying an
  error-rate circuit breaker and notifying on halts and all-blocked rounds;
* :func:`run_relation_lifecycle` coordinates one post-scan pass (expiry
  rotation first, then an auto-confirm round), exception-isolated so monitor
  scan loops can never break because a lifecycle step failed.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable


MODES = frozenset({"disabled", "active"})
DEFAULT_MAX_PER_ROUND = 100
#: A round whose unexpected-error share exceeds this rate halts its tier.
ERROR_HALT_RATE = 0.05


@dataclass(frozen=True)
class AutoConfirmMatch:
    discovery_source: str
    relation_type: str


@dataclass(frozen=True)
class AutoConfirmTier:
    name: str
    mode: str = "disabled"
    max_per_round: int = DEFAULT_MAX_PER_ROUND
    matches: tuple[AutoConfirmMatch, ...] = ()
    config_errors: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return self.mode == "active" and not self.config_errors and bool(self.matches)


@dataclass(frozen=True)
class AutoConfirmPolicy:
    tiers: tuple[AutoConfirmTier, ...] = ()

    def configuration_errors(self) -> tuple[str, ...]:
        messages: list[str] = []
        for tier in self.tiers:
            messages.extend(f"tier {tier.name}: {error}" for error in tier.config_errors)
        return tuple(messages)


def _match_error(match_value: object, index: int) -> AutoConfirmMatch | str:
    """One validated match entry or the reason it is malformed."""
    if not isinstance(match_value, Mapping):
        return f"match[{index}] must be an object"
    if set(map(str, match_value)) != {"discovery_source", "relation_type"}:
        return f"match[{index}] must contain exactly discovery_source and relation_type"
    source = match_value.get("discovery_source")
    relation_type = match_value.get("relation_type")
    if not isinstance(source, str) or not source.strip():
        return f"match[{index}].discovery_source must be a non-empty string"
    if not isinstance(relation_type, str) or not relation_type.strip():
        return f"match[{index}].relation_type must be a non-empty string"
    return AutoConfirmMatch(source.strip(), relation_type.strip())


def _parse_tier(raw_name: object, raw_tier: object) -> AutoConfirmTier:
    """One validated tier or its fail-closed disabled form with errors."""
    name = str(raw_name) if isinstance(raw_name, str) and raw_name.strip() else "<unnamed>"
    if not isinstance(raw_tier, Mapping):
        return AutoConfirmTier(name=name, config_errors=("tier entry must be an object",))
    errors: list[str] = []
    mode_raw = raw_tier.get("mode", "disabled")
    if mode_raw not in MODES:
        errors.append(f"unknown mode {mode_raw!r}; must be one of {sorted(MODES)}")
    cap_raw = raw_tier.get("max_per_round", DEFAULT_MAX_PER_ROUND)
    if type(cap_raw) is not int or cap_raw < 1:
        errors.append("max_per_round must be a positive integer")
        cap = DEFAULT_MAX_PER_ROUND
    else:
        cap = cap_raw
    matches: list[AutoConfirmMatch] = []
    match_list = raw_tier.get("match")
    if not isinstance(match_list, list) or not match_list:
        errors.append("match must be a non-empty array")
    else:
        for index, entry in enumerate(match_list):
            outcome = _match_error(entry, index)
            if isinstance(outcome, AutoConfirmMatch):
                matches.append(outcome)
            else:
                errors.append(outcome)
    return AutoConfirmTier(
        name=name.strip(),
        mode=str(mode_raw),
        max_per_round=cap,
        matches=tuple(matches),
        config_errors=tuple(errors),
    )


def load_auto_confirm_policy(document: object) -> AutoConfirmPolicy:
    """Parse one policy document; fail closed per tier with surfaced errors.

    An invalid tier never rolls into an accidental run: it lands ``disabled``
    with explanatory ``config_errors``, while structurally valid siblings are
    honored exactly as configured.
    """
    if document is None:
        return AutoConfirmPolicy()
    if not isinstance(document, Mapping):
        return AutoConfirmPolicy(tiers=(
            AutoConfirmTier(name="<document>", config_errors=("policy document must be an object",)),
        ))
    tiers_document = document.get("tiers", [])
    if not isinstance(tiers_document, list):
        return AutoConfirmPolicy(tiers=(
            AutoConfirmTier(name="<tiers>", config_errors=("tiers must be an array",)),
        ))
    tiers = tuple(
        _parse_tier(entry.get("name"), entry) if isinstance(entry, Mapping)
        else _parse_tier(None, entry)
        for entry in tiers_document
    )
    return AutoConfirmPolicy(tiers=tiers)


def load_auto_confirm_policy_file(path: Path) -> AutoConfirmPolicy:
    """Load the policy from a JSON file; a missing file means nothing is enabled.

    A present-but-unreadable file fails closed like invalid JSON: a missing
    file switches the feature off, while any other read or decode problem
    (binary corruption raises ``UnicodeDecodeError``, a ``ValueError``) loads
    a disabled tier carrying the configuration error instead of taking down
    runtime startup.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return AutoConfirmPolicy()
    except UnicodeDecodeError as exc:
        return AutoConfirmPolicy(tiers=(
            AutoConfirmTier(name=str(path), config_errors=(f"invalid UTF-8: {exc.reason}",)),
        ))
    try:
        document = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as exc:
        return AutoConfirmPolicy(tiers=(
            AutoConfirmTier(name=str(path), config_errors=(f"invalid JSON: {exc.msg}",)),
        ))
    return load_auto_confirm_policy(document)


def select_auto_confirm_items(rows: Sequence[Mapping[str, object]], tier: AutoConfirmTier) -> list[str]:
    """Whitelist matches of ``rows``, oldest ``discovered_at`` first, capped."""
    if not tier.enabled:
        return []
    pair_set = {(match.discovery_source, match.relation_type) for match in tier.matches}
    matched = [
        row
        for row in rows
        if (str(row.get("discovery_source")), str(row.get("relation_type"))) in pair_set
    ]
    matched.sort(key=lambda row: (str(row.get("discovered_at")), str(row.get("version_id"))))
    return [str(row["version_id"]) for row in matched[: tier.max_per_round]]


def _outcome_counts(results: Sequence[Mapping[str, object]]) -> dict[str, int]:
    return {
        "active": sum(1 for result in results if result.get("activation") == "ACTIVE"),
        "blocked": sum(
            1
            for result in results
            if "error" not in result
            and result.get("activation") is not None
            and result.get("activation") != "ACTIVE"
        ),
        "error": sum(1 for result in results if "error" in result),
    }


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def run_relation_lifecycle(
    catalog: object,
    runner: RelationAutoConfirmRunner | None,
    *,
    clock: Callable[[], str] | None = None,
    actor_expire: str = "lifecycle:expire",
    git_sha: str = "",
) -> dict[str, object]:
    """One post-scan governance pass: expire first, then auto-confirm (#96).

    The expiry rotation reopens the shared timeline before any approval is
    attempted, so a Tier-1 candidate whose only blocker was a stale deadline
    can activate in the very same pass. Every step is exception-isolated and
    failures are reported as ``<step>_error`` entries instead of propagating:
    the monitor scan loop calls this after each full scan and must never
    break because a lifecycle step failed. ``clock`` returns the rotation
    ``now`` timestamp and is injectable for tests; production callers get
    wall-clock UTC.
    """
    steps: dict[str, object] = {}
    now = (clock or utc_now_iso)()
    try:
        steps["expiry"] = catalog.expire_stale_members(
            now=now, actor=actor_expire, git_sha=git_sha
        )
    except Exception as exc:
        steps["expiry_error"] = f"{type(exc).__name__}: {exc}"
    if runner is None:
        return steps
    try:
        steps["auto_confirm"] = runner.run_round(git_sha=git_sha)
    except Exception as exc:
        steps["auto_confirm_error"] = f"{type(exc).__name__}: {exc}"
    return steps


class RelationAutoConfirmRunner:
    """Runs auto-confirm rounds per tier with a persistent error-rate breaker."""

    def __init__(
        self,
        catalog: object,
        *,
        policy: AutoConfirmPolicy,
        notifier: object | None = None,
        halt_rate: float = ERROR_HALT_RATE,
    ) -> None:
        self._catalog = catalog
        self._policy = policy
        self._notifier = notifier
        self._halt_rate = halt_rate
        self._halted: set[str] = set()
        # Manual ``auto-confirm-round`` and the monitor observer's round share
        # this runner; rounds serialize so the loser sees a drained selection
        # instead of double-approving into halt-triggering errors.
        self._round_lock = threading.Lock()

    @property
    def halted_tiers(self) -> frozenset[str]:
        return frozenset(self._halted)

    def _notify(self, title: str, message: str) -> None:
        notify = getattr(self._notifier, "notify", None)
        if callable(notify):
            try:
                notify(title, message)
            except Exception:
                pass

    def run_round(self, *, git_sha: str = "") -> dict[str, object]:
        """One lifecycle pass over every runnable tier, one at a time.

        Concurrent callers (manual endpoint vs monitor observer) serialize on
        a per-runner lock: the second caller waits and then re-selects against
        the drained queue, so an overlap yields a normal empty report instead
        of "no longer pending" errors that would trip the breaker. Disabled
        tiers never run; a previously halted tier is frozen to a zero-selection
        no-op for this runner instance. Each executed tier is one
        ``approve_many`` transaction under the ``auto-confirm:<tier>`` actor.
        An unexpected-error share above ``ERROR_HALT_RATE`` trips the tier's
        circuit breaker for this runner instance and notifies; an all-blocked
        round with zero unexpected errors alerts without halting (alert-only).
        Configuration errors ride along on every report so an operator can see
        why a tier was skipped without hunting logs.
        """
        with self._round_lock:
            return self._run_round_locked(git_sha=git_sha)

    def _run_round_locked(self, *, git_sha: str) -> dict[str, object]:
        tier_reports: list[dict[str, object]] = []
        totals = {"selected": 0, "active": 0, "blocked": 0, "error": 0}
        for tier in self._policy.tiers:
            if not tier.enabled:
                continue
            if tier.name in self._halted:
                # A frozen tier stays visible in every later report as an
                # explicit zero-op, never silently absent.
                tier_reports.append({
                    "tier": tier.name,
                    "actor": f"auto-confirm:{tier.name}",
                    "selected": 0,
                    "active": 0,
                    "blocked": 0,
                    "error": 0,
                    "halted": True,
                })
                continue
            rows = list(self._catalog.list("pending_approval"))
            items = select_auto_confirm_items(rows, tier)
            actor = f"auto-confirm:{tier.name}"
            counts = {"active": 0, "blocked": 0, "error": 0}
            if items:
                batch = self._catalog.approve_many(
                    [{"version_id": version_id} for version_id in items],
                    actor=actor,
                    git_sha=git_sha,
                )
                counts = _outcome_counts(batch["results"])
            entry: dict[str, object] = {
                "tier": tier.name,
                "actor": actor,
                "selected": len(items),
                "halted": False,
                **counts,
            }
            unexpected_error_rate = (
                int(entry["error"]) / len(items) if items else 0.0
            )
            if unexpected_error_rate > self._halt_rate:
                entry["halted"] = True
                self._halted.add(tier.name)
                self._notify(
                    f"关系自动确认熔断：{tier.name}",
                    (
                        f"tier {tier.name} halted at error rate "
                        f"{unexpected_error_rate:.2%} "
                        f"(selected={entry['selected']}, error={entry['error']}); "
                        "subsequent rounds are frozen until restart"
                    ),
                )
            elif int(entry["blocked"]) > 0:
                self._notify(
                    f"关系自动确认受阻告警：{tier.name}",
                    (
                        f"tier {tier.name} round finished with blocked approvals "
                        f"(blocked={entry['blocked']}, error={entry['error']}); "
                        "no errors, tier keeps running"
                    ),
                )
            tier_reports.append(entry)
            for name in totals:
                totals[name] += int(entry[name])
        return {
            **totals,
            "halted": any(bool(entry["halted"]) for entry in tier_reports)
            or bool(self._halted),
            "tiers": tier_reports,
            "configuration_errors": [
                {"tier": tier.name, "errors": list(tier.config_errors)}
                for tier in self._policy.tiers
                if tier.config_errors
            ],
        }
