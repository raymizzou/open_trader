"""Offline collection assessment and run-day control. No network or fee authority."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo


def valid_date(value):
    try:
        return (
            date.fromisoformat(value).isoformat()
            if isinstance(value, str)
            and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value)
            else None
        )
    except ValueError:
        return None


LOOKUP_LABELS = {"miss": "查不到", "pending": "待核"}


def platform_id(asset):
    value = asset.get("instrument_id")
    if value is None or value == "":
        return None
    if type(value) is int and value >= 0:
        return str(value)
    if isinstance(value, str):
        return value.strip() or None
    raise ValueError("Instrument ID must be text or a nonnegative integer")


def instrument_scope(asset):
    # Currency is an additional boundary when supplied, never inferred.
    values = (asset.get("market"), asset.get("instrument_type"))
    if not all(isinstance(value, str) and value.strip() for value in values):
        return None
    currency = asset.get("currency")
    if currency is not None and not isinstance(currency, str):
        raise ValueError("Instrument currency must be text")
    return (*values, currency or None)


def instrument_identity(asset, ids_by_code):
    # A code-only alias can use a single established ID in this exact scope.
    # Multiple contradictory IDs never make the alias identify either one.
    scope = instrument_scope(asset)
    if scope is None:
        return None
    identifier = platform_id(asset)
    code = asset.get("code")
    if identifier is None and isinstance(code, str) and code.strip():
        ids = ids_by_code.get((*scope, code), set())
        if len(ids) == 1:
            identifier = next(iter(ids))
        else:
            return (*scope, "code", code)
    return (*scope, "instrument_id", identifier) if identifier is not None else None


def reconcile_lookup(preferred, aliases):
    # Winner selection uses the original rows. Only the exact-identity match
    # fact can aggregate here; quotes require the separate strength-only opt-in.
    # Events and eligibility never transfer.
    evidence = [
        row
        for row in aliases
        if row.get("platform_match_verified") is True
        or row.get("lookup") in LOOKUP_LABELS
    ]
    if not evidence:
        return preferred
    has_match = any(row.get("platform_match_verified") is True for row in evidence)
    scopes = set()
    for row in evidence:
        scope = row.get("lookup_scope")
        if scope is not None and (not isinstance(scope, str) or not scope.strip()):
            raise ValueError("Lookup scope must be nonempty text")
        scopes.add(scope)
    conflict = len(scopes) > 1 or (
        has_match and any(row.get("lookup") == "miss" for row in evidence)
    )
    if not has_match and not conflict:
        return preferred
    reconciled = dict(preferred)
    reconciled["platform_match_verified"] = has_match and not conflict
    if conflict:
        if "_signal_pending" in reconciled:
            reconciled["_signal_pending"] = True
        reconciled["lookup"] = "pending"
        reconciled["reason"] = "平台匹配证据冲突或适用范围待核。"
    elif preferred.get("lookup") == "pending":
        # Removing a pending lookup must not unmask its old quote/event payload
        # or activate an old watch flag. Identity success proves no market data.
        for field in (
            "lookup",
            "trend",
            "strength",
            "local_strength",
            "signal",
            "strength_reason",
            "local_strength_reason",
        ):
            reconciled.pop(field, None)
        reconciled["signal_events"] = []
        if "_signal_pending" in reconciled:
            reconciled["_signal_pending"] = True
        reconciled["watch_eligible"] = False
        reconciled["reason"] = "平台身份已核实，行情字段待核。"
    return reconciled


def instrument_aliases(group):
    candidates = []
    for field in ("direct", "searchable", "related"):
        assets = group.get(field, [])
        if not isinstance(assets, list):
            raise ValueError("Asset lists must be arrays")
        candidates.extend(assets)
    ids_by_code = {}
    for asset in candidates:
        scope = instrument_scope(asset)
        identifier = platform_id(asset)
        code = asset.get("code")
        if scope is not None and identifier is not None and isinstance(code, str):
            ids_by_code.setdefault((*scope, code), set()).add(identifier)
    aliases_by_identity = {}
    for asset in candidates:
        identity = instrument_identity(asset, ids_by_code)
        if identity is not None:
            aliases_by_identity.setdefault(identity, []).append(asset)
    return candidates, ids_by_code, aliases_by_identity


DATE_FORMAT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
STRENGTH_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")


def strength_value(value):
    """Keep source precision while validating the 0–100 comparison scale."""
    raw = str(value)
    if raw.endswith(" / 100"):
        raw = raw[:-6]
    try:
        if STRENGTH_NUMBER.fullmatch(raw) and Decimal(raw) <= 100:
            return raw
    except InvalidOperation:
        pass
    return None


def strength_inputs(asset):
    """Normalize field provenance before deciding presence or invalidation."""
    fields = {key: asset[key] for key in ("strength", "local_strength") if key in asset}
    legacy = fields.get("strength")
    if isinstance(legacy, str) and legacy.startswith("同品种 "):
        # Peer-only text supplies no global claim, including no withdrawal.
        # Explicit local null still wins over the legacy local value.
        fields.pop("strength")
        fields.setdefault("local_strength", legacy.removeprefix("同品种 "))
    return fields


def strength_fields(asset):
    fields = strength_inputs(asset)
    return (
        strength_value(fields.get("strength")),
        strength_value(fields.get("local_strength")),
    )


def strength_scope(value):
    if not isinstance(value, dict):
        return None
    version = value.get("version")
    if not isinstance(version, str) or not version.strip():
        return None
    try:
        day = report_date(value.get("data_date"))
    except (TypeError, ValueError):
        return None
    return day, version


def strength_candidate_withdrawn(candidate, field, aliases):
    scope = strength_scope(candidate.get("_strength_field_scopes", {}).get(field))
    if scope is None:
        return False
    return any(
        platform_id(row) == platform_id(candidate)
        and instrument_scope(row) == instrument_scope(candidate)
        and row.get("lookup") is None
        and strength_scope(row.get("_strength_withdrawals", {}).get(field)) == scope
        for row in aliases
    )


def reconcile_strengths(preferred, reconciled, aliases):
    """Opt-in, field-only bridge for independently verified split projections."""
    # Explicit, validated current null withdrawals are field-local. They must
    # survive duplicate selection even without the optional split-field opt-in.
    withdrawn = {
        field
        for field in preferred.get("_strength_field_scopes", {})
        if strength_candidate_withdrawn(preferred, field, aliases)
    }
    if withdrawn:
        preferred = {**preferred, **{field: None for field in withdrawn}}
        reconciled = {**reconciled, **{field: None for field in withdrawn}}
    scope = strength_scope(preferred.get("strength_scope"))
    identifier = platform_id(preferred)
    identity_scope = instrument_scope(preferred)
    # Identity resolution alone must never revive pending/hidden market data.
    if (
        scope is None
        or identifier is None
        or identity_scope is None
        or preferred.get("lookup") is not None
        or reconciled.get("lookup") is not None
    ):
        return reconciled
    result = dict(reconciled)
    preferred_inputs = strength_inputs(preferred)
    own_evidence = preferred.get("strength_evidence", {})
    if not isinstance(own_evidence, dict):
        own_evidence = {}
    for field, current in zip(
        ("strength", "local_strength"), strength_fields(preferred), strict=True
    ):
        values = [current] if current is not None else []
        # Omission can be supplemented. An explicit null/invalid value cannot.
        invalidated = field in preferred_inputs and current is None
        if field in own_evidence:
            proof = own_evidence[field]
            invalidated |= (
                strength_scope(proof) != scope or proof.get("state") != "verified"
            )
        for row in aliases:
            if (
                row is preferred
                or row.get("lookup") is not None
                or row.get("platform_match_verified") is not True
                or platform_id(row) != identifier
                or instrument_scope(row) != identity_scope
            ):
                continue
            evidence = row.get("strength_evidence", {})
            proof = evidence.get(field) if isinstance(evidence, dict) else None
            if strength_scope(proof) != scope:
                continue
            row_inputs = strength_inputs(row)
            if proof.get("state") == "withdrawn":
                invalidated = True
            elif proof.get("state") == "verified" and field in row_inputs:
                value = strength_value(row_inputs[field])
                if value is None or strength_candidate_withdrawn(row, field, aliases):
                    invalidated = True
                else:
                    values.append(value)
        if invalidated or len({Decimal(value) for value in values}) > 1:
            result[field] = None
            result[field + "_reason"] = "该强度证据已失效或存在冲突，待核。"
        elif values:
            # Equal values are compatible; preserve preferred precision, otherwise
            # choose the longest source representation independently of row order.
            result[field] = current or max(
                values, key=lambda value: (len(value), value)
            )
            result.pop(field + "_reason", None)
    return result


def report_date(value):
    if not isinstance(value, str) or not DATE_FORMAT.fullmatch(value):
        raise ValueError("Report and board dates must use YYYY-MM-DD")
    return date.fromisoformat(value)


def reconciled_instruments(group):
    candidates, ids_by_code, aliases_by_identity = instrument_aliases(group)
    source_paths = {}
    for section in ("direct", "searchable", "related"):
        for index, asset in enumerate(group.get(section, [])):
            source_paths.setdefault(id(asset), []).append(f"{section}/{index}")
    sections = [[], [], []]
    seen = set()
    # Resolve strongest verified classification before removing duplicates.
    # Keep selected-row facts and eligibility. Only explicitly verified strength
    # fields may cross rows below; never infer membership from list position.
    for priority in range(3):
        for asset in candidates:
            category = (
                0
                if asset.get("direct_verified") is True
                else 1
                if asset.get("platform_match_verified") is True
                else 2
            )
            if category != priority:
                continue
            identity = instrument_identity(asset, ids_by_code)
            if identity is not None:
                if identity in seen:
                    continue
                seen.add(identity)
            aliases = aliases_by_identity.get(identity, [asset])
            reconciled = reconcile_lookup(asset, aliases)
            reconciled = reconcile_strengths(asset, reconciled, aliases)
            reconciled = dict(reconciled)
            reconciled["_source_paths"] = list(
                dict.fromkeys(
                    path for alias in aliases for path in source_paths[id(alias)]
                )
            )
            final_category = (
                0
                if reconciled.get("direct_verified") is True
                else 1
                if reconciled.get("platform_match_verified") is True
                else 2
            )
            sections[final_category].append(reconciled)
    return sections


def evidence_for(evidence, path):
    value = evidence.get(path) if isinstance(evidence, dict) else None
    return value if isinstance(value, dict) else {}


def field_evidence_matches(asset, field, proof, target, states=("verified",)):
    identifier = asset.get("instrument_id")
    known_id = (type(identifier) is int and identifier >= 0) or (
        isinstance(identifier, str) and bool(identifier.strip())
    )
    return (
        known_id
        and bool(target)
        and all(
            isinstance(asset.get(key), str) and asset[key].strip()
            for key in ("market", "instrument_type")
        )
        and proof.get("state") in states
        and valid_date(proof.get("data_date")) == target
        and proof.get("value") == asset.get(field)
        and proof.get("identity") == identifier
        and proof.get("market") == asset.get("market")
        and proof.get("instrument_type") == asset.get("instrument_type")
        and all(
            isinstance(proof.get(key), str) and proof[key].strip()
            for key in ("source", "version")
        )
    )


def field_proof_matches(asset, field, proof, target):
    value = asset.get(field)
    valid_value = value is not None
    if field in ("strength", "local_strength"):
        raw = str(value).removesuffix(" / 100")
        valid_value = bool(
            re.fullmatch(r"[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", raw)
        )
        if valid_value:
            valid_value = Decimal(raw) <= 100
    elif field == "trend":
        valid_value = (
            isinstance(value, str)
            and bool(value.strip())
            and value.casefold() not in ("unknown", "pending", "待核")
        )
    elif field == "signal_events":
        valid_value = isinstance(value, list) and all(
            isinstance(name, str)
            and bool(name.strip())
            and name.casefold() not in ("unknown", "pending", "待核")
            for name in value
        )
    return valid_value and field_evidence_matches(asset, field, proof, target)


def dated_projection(report):
    result = deepcopy(report)
    collection = report.get("collection") or {}
    evidence_bearing = "collection" in report
    targets = collection.get("targets", {})
    pending = []
    for direction in ("short", "long"):
        for gi, group in enumerate(result.get(direction, [])):
            group["_date_note"] = "大类数据日期 " + (
                valid_date(group.get("data_date")) or "待核"
            )
            coverage = group.get("coverage", {})
            coverage_proof = evidence_for(
                collection.get("evidence", {}), f"/{direction}/{gi}/coverage"
            )
            group["_coverage_pending"] = evidence_bearing and not (
                valid_date(targets.get("BOARD"))
                and isinstance(coverage, dict) and coverage.get("state") == "complete"
                and coverage_proof.get("state") == "verified"
                and coverage_proof.get("value") == coverage
                and coverage_proof.get("identity") == group.get("name")
                and valid_date(coverage_proof.get("data_date"))
                == valid_date(targets.get("BOARD"))
                and bool(coverage_proof.get("source"))
                and bool(coverage_proof.get("version"))
            )
            for section in ("direct", "searchable", "related"):
                for ai, asset in enumerate(group.get(section, [])):
                    # These annotations are derived anew from this report's proofs.
                    asset.pop("_strength_field_scopes", None)
                    asset.pop("_strength_withdrawals", None)
                    actual = valid_date(asset.get("data_date"))
                    market = asset.get("market")
                    target = valid_date(targets.get(market))
                    try:
                        validate_targets(
                            report["date"],
                            {
                                "targets": {market: target},
                                "target_evidence": {
                                    market: collection["target_evidence"][market]
                                }
                                if market in collection.get("target_evidence", {})
                                else {},
                            },
                        )
                    except ValueError:
                        target = None
                    card_current = bool(actual and target and actual == target)
                    # Display freshness is separate from publication/watch eligibility.
                    display_target = target
                    try:
                        validate_targets(report["date"], {
                            "targets": {market: target},
                            "target_evidence": collection.get("target_evidence", {}),
                        }, require_proof=(market,))
                    except ValueError:
                        display_target = None
                    asset["_freshness"] = (
                        "fresh" if actual and display_target == actual else "waiting"
                    )
                    asset["_freshness_label"] = (
                        "本期最新" if asset["_freshness"] == "fresh"
                        else f"待更新 · 等 {display_target}"
                        if actual and display_target and actual < display_target
                        else "日期待核"
                    )
                    asset["_date_note"] = "行情截至 " + (actual or "待核")
                    if evidence_bearing and not card_current:
                        asset["reason"] = "行情日期不适用或待核，不作当期比较。"
                    if not evidence_bearing and (
                        (asset.get("data_date") is not None and not actual)
                        or (actual and target and actual > target)
                    ):
                        for field in ("trend", "strength", "local_strength", "signal"):
                            asset[field] = None
                        asset["signal_events"] = []
                        asset["watch_eligible"] = False
                        asset["reason"] = "行情日期无效或晚于目标日，行情待核。"
                    prefix = f"/{direction}/{gi}/{section}/{ai}"
                    proofs = collection.get("evidence", {})
                    lookup_proof = evidence_for(proofs, prefix + "/lookup")
                    if asset.get("lookup") == "miss" and evidence_bearing:
                        if (
                            not field_proof_matches(
                                asset, "lookup", lookup_proof, target
                            )
                            or lookup_proof.get("full_lookup") is not True
                        ):
                            asset["lookup"] = "pending"
                            asset["reason"] = "完整查询证据或适用范围待核。"
                    fields_current = True
                    for field, label in (
                        ("trend", "趋势"),
                        ("strength", "全局强度"),
                        ("local_strength", "同品种强度"),
                        ("signal_events", "特殊信号"),
                    ):
                        proof = evidence_for(proofs, prefix + "/" + field)
                        field_day = valid_date(proof.get("data_date"))
                        current_strength_scope = (
                            field in ("strength", "local_strength")
                            and card_current
                            and field_evidence_matches(
                                asset,
                                field,
                                proof,
                                target,
                                states=("verified", "withdrawn"),
                            )
                        )
                        if evidence_bearing and current_strength_scope:
                            asset.setdefault("_strength_field_scopes", {})[field] = {
                                "data_date": proof["data_date"],
                                "version": proof["version"],
                            }
                            if (
                                proof["state"] == "withdrawn"
                                and asset.get(field) is None
                            ):
                                asset.setdefault("_strength_withdrawals", {})[field] = {
                                    "data_date": proof["data_date"],
                                    "version": proof["version"],
                                }
                        if (
                            evidence_bearing and not card_current
                        ) or not field_proof_matches(asset, field, proof, target):
                            supplied = field in asset or bool(proof)
                            missing_strength = (
                                field in ("strength", "local_strength")
                                and asset.get(field) is None
                                and (
                                    not proof
                                    or field_evidence_matches(
                                        asset,
                                        field,
                                        proof,
                                        target,
                                        states=("verified", "withdrawn"),
                                    )
                                )
                            )
                            if field in ("trend", "signal_events") or (
                                supplied and not missing_strength
                            ):
                                fields_current = False
                            if evidence_bearing:
                                # Filter each source before identity dedup/ranking.
                                # Explicit invalid input remains a withdrawal; mere
                                # omission can still use a verified split-field donor.
                                if (
                                    supplied
                                    or field == "signal_events"
                                    or not card_current
                                ):
                                    asset[field] = (
                                        [] if field == "signal_events" else None
                                    )
                                if field in ("strength", "local_strength"):
                                    split = asset.get("strength_evidence")
                                    if isinstance(split, dict):
                                        split.pop(field, None)
                            pending.append(
                                f"{asset['code']} {field}：目标 {target or '待核'}，"
                                f"实际 {field_day or actual or '待核'}"
                            )
                    if evidence_bearing:
                        asset["_signal_pending"] = not (
                            card_current and field_proof_matches(
                                asset, "signal_events",
                                evidence_for(proofs, prefix + "/signal_events"), target
                            )
                        )
                    else:
                        asset.pop("_signal_pending", None)
                    if not fields_current:
                        asset["watch_eligible"] = False
                    if not actual or not target or actual != target:
                        asset["watch_eligible"] = False
                        if actual and target and actual < target:
                            asset["_date_note"] += "（历史数据）"
                        pending.append(
                            f"{asset['code']}：目标 {target or '待核'}，"
                            f"实际 {actual or '待核'}"
                        )
    return result, pending


def required_facts(report):
    """Enumerate visible obligations; the supplied missing list is never authority."""
    yield "/board_date", "榜图", "BOARD", "board", report.get("board_date")
    yield "/board_version", "榜图版次", "BOARD", "board", report.get("board_version")
    for direction in ("short", "long"):
        for gi, group in enumerate(report.get(direction, [])):
            prefix = f"/{direction}/{gi}"
            for field in ("board_strength", "coverage"):
                yield (
                    prefix + "/" + field,
                    group["name"] + " " + field,
                    "BOARD",
                    group["name"],
                    group.get(field),
                )
            for section in ("direct", "searchable", "related"):
                for ai, asset in enumerate(group.get(section, [])):
                    fields = (
                        ("lookup",)
                        if asset.get("lookup")
                        else ("trend", "strength", "local_strength", "signal_events")
                    )
                    for field in fields:
                        yield (
                            prefix + f"/{section}/{ai}/" + field,
                            asset["code"] + " " + field,
                            asset.get("market"),
                            asset.get("instrument_id"),
                            asset.get(field),
                        )


def confirmation_time(value):
    try:
        parsed = datetime.fromisoformat(value)
        return value if parsed.utcoffset() is not None else None
    except (TypeError, ValueError):
        return None


def verified_final_miss(report, asset, prefix):
    """Only an applicable full negative can exempt final market fields."""
    if asset.get("lookup") != "miss":
        return False
    collection = report.get("collection") or {}
    target = valid_date(collection.get("targets", {}).get(asset.get("market")))
    for source in asset["_source_paths"]:
        proof = evidence_for(collection.get("evidence"), prefix + source + "/lookup")
        if proof.get("full_lookup") is True and field_proof_matches(
            asset, "lookup", proof, target
        ):
            return True
    return False


def pending_final_fields(report):
    """Assess the actual sanitized, deduplicated lookup/strength card result."""
    projected, _ = dated_projection(report)
    result = []
    for direction in ("short", "long"):
        for gi, group in enumerate(projected.get(direction, [])):
            prefix = f"/{direction}/{gi}/"
            for section in reconciled_instruments(group):
                for asset in section:
                    if verified_final_miss(report, asset, prefix):
                        continue
                    if asset.get("lookup") is not None:
                        fields = ("lookup",)
                    else:
                        fields = [
                            field
                            for field, value in zip(
                                ("strength", "local_strength"),
                                strength_fields(asset),
                                strict=True,
                            )
                            if value is None
                        ]
                    for field in fields:
                        for source in asset["_source_paths"]:
                            result.append(
                                {
                                    "path": prefix + source + "/" + field,
                                    "field": field,
                                    "market": asset.get("market"),
                                    "code": asset["code"],
                                }
                            )
    return result


def assess(report):
    """Evidence must bind every obligation to its frozen target and actual value."""
    collection = report.get("collection") or {}
    targets = collection.get("targets") or {}
    evidence = collection.get("evidence") or {}
    worklist = collection.get("worklist") or []
    obligations = {
        item.get("path"): item for item in worklist if isinstance(item, dict)
    }
    pending = []
    try:
        validate_targets(report["date"], collection)
    except ValueError:
        pending.append("目标日历证据待核")
    facts = list(required_facts(report))
    paths = [item.get("path") for item in worklist if isinstance(item, dict)]
    if not worklist or len(paths) != len(worklist) or len(set(paths)) != len(paths):
        pending.append("工作清单缺失或重复")
    known_paths = {fact[0] for fact in facts}
    for path in paths:
        if path not in known_paths:
            pending.append("额外工作项待核")
    for path, label, market, identity, value in facts:
        parts = path.strip("/").split("/")
        subject = report
        for part in parts[:-1]:
            subject = subject[int(part)] if isinstance(subject, list) else subject[part]
        item = obligations.get(path, {})
        proof = evidence_for(evidence, path)
        target = valid_date(targets.get(market))
        evidence_day = valid_date(proof.get("data_date"))
        # A proof's metadata cannot replace the actual date cited in the report.
        # Board edition remains independent of the board's data day.
        actual = valid_date(value) if path == "/board_date" else evidence_day
        if (
            not target
            or not identity
            or value is None
            or item.get("target_date") != target
            or item.get("market") != market
            or proof.get("state") != "verified"
            or evidence_day != target
            or actual != target
            or proof.get("identity") != identity
            or proof.get("value") != value
            or not proof.get("source")
            or not proof.get("version")
        ):
            pending.append(f"{label}：目标 {target or '待核'}，实际 {actual or '待核'}")
        if len(parts) == 5:
            if (
                not field_proof_matches(subject, parts[-1], proof, target)
                or not subject.get("instrument_type")
                or proof.get("market") != market
                or proof.get("instrument_type") != subject.get("instrument_type")
                or valid_date(subject.get("data_date")) != target
            ):
                pending.append(label + "：身份范围或行情日期待核")
        if len(parts) == 3 and valid_date(subject.get("data_date")) != target:
            pending.append(label + "：大类日期待核")
        if parts[-1] in ("strength", "local_strength", "board_strength"):
            raw = str(value).removesuffix(" / 100")
            try:
                usable = (
                    re.fullmatch(r"[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", raw)
                    and Decimal(raw) <= 100
                )
            except InvalidOperation:
                usable = False
            if not usable:
                pending.append(label + "：数值待核")
        if path.endswith("/lookup") and (
            value != "miss"
            or proof.get("full_lookup") is not True
            or subject.get("platform_match_verified") is True
        ):
            pending.append(label + "：完整查询范围待核")
    for direction in ("short", "long"):
        groups = report.get(direction, [])
        if {g.get("seat") for g in groups} != {1, 2}:
            pending.append(direction + "：榜图席位待核")
        for group in groups:
            members = [
                a.get("instrument_id")
                for section in ("direct", "searchable", "related")
                for a in group.get(section, [])
            ]
            coverage = group.get("coverage") or {}
            if (
                coverage.get("state") != "complete"
                or coverage.get("instrument_ids") != members
                or any(not member for member in members)
            ):
                pending.append(group["name"] + "：成分及查询覆盖待核")
    final_pending = pending_final_fields(report)
    lookup_pending = [item for item in final_pending if item["field"] == "lookup"]
    pending.extend(
        item["code"] + " " + item["field"] + "：最终卡片字段待核。"
        for item in final_pending
    )
    verified_at = confirmation_time(collection.get("verified_at"))
    if not verified_at:
        pending.append("完备核实时间待核")
    return {
        "complete": not pending,
        "pending": pending,
        "verified_at": verified_at,
        "lookup_pending": lookup_pending,
        "final_pending": final_pending,
    }


# Persistent controls deliberately do not send requests, sleep, or hold the shared
# billing lock. The scheduled owner uses the existing ledger around each send.
TAIPEI = ZoneInfo("Asia/Taipei")


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def save(path, value):
    """Durably replace control state; failure must propagate to the owner."""
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        parent_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def state_lock(path):
    # NB: never wait holding another lock. This is not a billing/run-lock token.
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def initialize(path, report):
    path = Path(path)
    day = valid_date(report.get("date"))
    if not day:
        raise ValueError("Invalid report day")
    with state_lock(path):
        if path.exists():
            state = json.loads(path.read_text())
            if check_frozen(state, report):
                save(path, state)
            return state
        validate_targets(day, report.get("collection", {}))
        state = {
            "date": day,
            "targets": report.get("collection", {}).get("targets", {}),
            "worklist": report.get("collection", {}).get("worklist", []),
            "target_evidence": report.get("collection", {}).get("target_evidence", {}),
            "last_slot": -1,
            "rounds": [],
            "evidence": {},
            "published": None,
        }
        save(path, state)
        return state


def validate_targets(day, collection, require_proof=()):
    """Legacy initial dates remain supported; first resolution needs day-bound proof."""
    basis = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    proofs = collection.get("target_evidence", {})
    for market, target in collection.get("targets", {}).items():
        if target is None:
            continue
        if valid_date(target) != target or target > basis:
            raise ValueError("Invalid or future target")
        proof = proofs.get(market)
        if proof is None and market not in require_proof:
            continue
        if (
            not isinstance(proof, dict)
            or proof.get("state") != "verified"
            or proof.get("report_date") != day
            or proof.get("basis_date") != basis
            or not isinstance(proof.get("source"), str)
            or not proof["source"].strip()
        ):
            raise ValueError("Verified original-day calendar proof required")
        sessions = proof.get("sessions", [])
        if (
            not isinstance(sessions, list)
            or any(not valid_date(value) or value > basis for value in sessions)
            or target_dates(day, {market: proof}).get(market) != target
        ):
            raise ValueError("Target does not match original-day calendar proof")


def check_frozen(state, report):
    if report.get("date") != state["date"]:
        raise ValueError("Report day is frozen; reconcile original run")
    collection = report.get("collection", {})
    incoming = collection.get("targets", {})
    old = state["targets"]
    if any(
        market not in incoming or (target is not None and incoming[market] != target)
        for market, target in old.items()
    ):
        raise ValueError("Established targets are frozen; reconcile original run")
    resolving = {
        market
        for market, target in incoming.items()
        if target is not None and old.get(market) is None
    }
    validate_targets(state["date"], collection, resolving)
    changed = incoming != old
    if changed:
        # Called only under state_lock; the caller must persist before returning,
        # even when the current hourly slot is not due. No request is authorized.
        state.update(
            targets=deepcopy(incoming),
            worklist=collection.get("worklist", []),
            projection=report,
            assessment=assess(report),
            published=None,
        )
        state["pending_target_changes"] = sorted(
            set(state.get("pending_target_changes", [])) | resolving
        )
        state.setdefault("target_evidence", {}).update(
            {market: collection["target_evidence"][market] for market in resolving}
        )
    return changed


def claim_round(path, report, now):
    path = Path(path)
    instant = datetime.fromisoformat(now)
    if instant.utcoffset() is None:
        raise ValueError("Clock must have a timezone")
    with state_lock(path):
        state = json.loads(path.read_text())
        targets_changed = check_frozen(state, report)
        start = datetime.fromisoformat(state["date"] + "T08:30:00").replace(
            tzinfo=TAIPEI
        )
        slot = int((instant - start).total_seconds() // 3600)
        if slot < 0 or slot <= state["last_slot"]:
            if targets_changed:
                save(path, state)
            return None
        if state.get("published") == fingerprint(report) and assess(report)["complete"]:
            return None
        evidence = report.get("collection", {}).get("evidence", {})
        hashes = {key: fingerprint(value) for key, value in evidence.items()}
        changed = sorted(
            key
            for key in set(hashes) | set(state["evidence"])
            if hashes.get(key) != state["evidence"].get(key)
        )
        target_changes = set(state.get("pending_target_changes", []))
        changed = sorted(
            set(changed)
            | {fact[0] for fact in required_facts(report) if fact[2] in target_changes}
        )
        assessment = assess(report)
        # Re-assess cheap local facts, but request/recompute only affected items.
        # Release observation alone does not satisfy any instrument obligation.
        pending_paths = [item["path"] for item in assessment["final_pending"]]
        markets = {
            item["market"] for item in assessment["final_pending"] if item["market"]
        }
        for fact in required_facts(report):
            path_, label, market, identity, value = fact
            if any(text.startswith(label + "：") for text in assessment["pending"]):
                pending_paths.append(path_)
                if market:
                    markets.add(market)
        if not assessment["complete"] and not pending_paths:
            markets.add("BOARD")
        result = {
            "slot": slot,
            "observed_at": now,
            "next_check": (start + timedelta(hours=slot + 1)).isoformat(),
            "markets": sorted(markets),
            "pending_paths": list(dict.fromkeys(pending_paths)),
            "changed": changed,
            "affected_groups": sorted(
                {
                    "/".join(p.split("/")[:3])
                    for p in changed
                    if p.startswith(("/short/", "/long/"))
                }
            ),
        }
        state.update(
            last_slot=slot,
            evidence=hashes,
            worklist=report.get("collection", {}).get("worklist", []),
            assessment=assessment,
            projection=report,
        )
        state["rounds"].append(result)
        state["pending_target_changes"] = []
        state["published"] = None
        save(path, state)  # claim before any observation; unknown is never replayed
        return result


def render_html(report):
    """Use the official public renderer, including its exact pinned template."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source, output = root / "input.json", root / "report.html"
        source.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("render_report.py")),
                str(source),
                str(output),
            ],
            check=True,
            capture_output=True,
        )
        return output.read_text(encoding="utf-8")


def confirm_publication(path, report, local, delivered):
    """Stop only after current complete HTML is published and read back exactly."""
    path = Path(path)
    if not assess(report)["complete"]:
        raise ValueError("Report evidence is incomplete")
    expected = render_html(report).encode("utf-8")
    if Path(local).read_bytes() != expected or Path(delivered).read_bytes() != expected:
        raise ValueError("Current HTML publication readback mismatch")
    with state_lock(path):
        state = json.loads(path.read_text())
        check_frozen(state, report)
        state.update(
            published=fingerprint(report),
            projection=report,
            assessment=assess(report),
            publication={
                "local": str(local),
                "delivered": str(delivered),
                "html_sha256": hashlib.sha256(expected).hexdigest(),
            },
        )
        save(path, state)


def target_dates(report_day, calendars):
    """Freeze D-1, or the last verified local closing session on/before D-1."""
    previous = date.fromisoformat(report_day) - timedelta(days=1)
    result = {}
    for market, calendar in calendars.items():
        target = None
        if calendar.get("source"):
            if calendar.get("kind") == "continuous":
                target = previous.isoformat()
            elif calendar.get("kind") == "equity":
                through = valid_date(calendar.get("covered_through"))
                sessions = calendar.get("sessions", [])
                if (
                    through
                    and through >= previous.isoformat()
                    and sessions
                    and all(valid_date(value) for value in sessions)
                ):
                    eligible = [
                        value for value in sessions if value <= previous.isoformat()
                    ]
                    target = max(eligible) if eligible else None
        result[market] = target
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("targets", "init", "claim", "assess", "confirm")
    )
    parser.add_argument(
        "input", type=Path, help="Verified projection, or calendar JSON for targets"
    )
    parser.add_argument("--state", type=Path)
    parser.add_argument(
        "--now", help="Timezone-aware observation time; no waiting or requests"
    )
    parser.add_argument("--local", type=Path)
    parser.add_argument("--delivered", type=Path)
    args = parser.parse_args()
    try:
        report = json.loads(args.input.read_text(encoding="utf-8"))
        if args.action == "targets":
            value = target_dates(report["date"], report["calendars"])
        elif args.action == "assess":
            value = assess(report)
        else:
            if args.state is None:
                raise ValueError("--state is required")
            if args.action == "init":
                value = initialize(args.state, report)
            elif args.action == "claim":
                if args.now is None:
                    raise ValueError("--now is required")
                value = claim_round(args.state, report, args.now)
            else:
                if args.local is None or args.delivered is None:
                    raise ValueError("--local and --delivered are required")
                confirm_publication(args.state, report, args.local, args.delivered)
                value = {"publication_verified": True}
        print(json.dumps(value, ensure_ascii=False))
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        subprocess.CalledProcessError,
    ) as error:
        parser.exit(2, f"Collection control failed: {error}\n")


if __name__ == "__main__":
    main()
