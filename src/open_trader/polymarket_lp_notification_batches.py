"""Immutable notification envelopes and per-channel result identities.

Callers plan and persist these records under their existing storage fences before
transport I/O. Business state and channel eligibility remain caller-owned.
"""
from __future__ import annotations

from copy import deepcopy
from collections.abc import Mapping
import uuid


class ChannelStatuses(dict):
    """Keep batch identities beside an otherwise compatible channel mapping."""
    def __init__(self, statuses=(), *, batch_ids=None):
        super().__init__(statuses)
        self.batch_ids = dict(batch_ids or {})


class ChannelDeliveryResult(tuple):
    """Keep the existing (attempted, successful) result interface."""
    def __new__(cls, attempted, delivered, *, batch_ids=None):
        result = super().__new__(cls, (attempted, delivered))
        result.batch_ids = dict(batch_ids or {})
        return result


def matching_batch_channels(result, stored):
    """Return channels still owned by a cached result, or None for legacy data."""
    batch_ids = getattr(result, "batch_ids", None)
    if batch_ids is None:
        return None
    return {channel for channel, identity in batch_ids.items()
            if isinstance(stored.get(channel), Mapping)
            and stored[channel].get("id") == identity}


def plan_notification_batches(*, rows, pending, stored, member_for, member_state, render):
    """Resolve channel retries without changing an existing envelope.

    member_state returns valid, acknowledged, defer (temporary evidence loss), or retire
    (archive/new episode/changed identity). Only retirement changes membership.
    Results include metadata assignments for original members outside today's
    claimed subset, so a replacement identity is shared by every survivor.
    """
    deliveries = {}
    assignments = {}
    resolved = {}
    replacements = {}
    new_channels = {}

    def freeze(members):
        title, message, voice = render(members)
        return {"version": 1, "id": uuid.uuid4().hex,
                "title": title, "message": message, "voice": voice,
                "members": deepcopy(members)}

    def schedule(batch, channel, recipient):
        delivery = deliveries.setdefault(batch["id"], {
            "batch": batch, "channels": set(), "recipients": {},
        })
        delivery["channels"].add(channel)
        delivery["recipients"].setdefault(channel, []).append(recipient)
        for member in batch["members"]:
            assignments.setdefault(member["id"], {})[channel] = batch

    for identity, row in rows.items():
        member = member_for(row)
        for channel in sorted(pending.get(identity, ())):
            previous = (stored.get(identity) or {}).get(channel)
            if (not isinstance(previous, Mapping) or previous.get("version") != 1
                    or not previous.get("id") or not isinstance(previous.get("members"), list)
                    or not all(isinstance(item, Mapping) and isinstance(item.get("id"), str)
                               and isinstance(item.get("episode"), str) for item in previous["members"])
                    or not all(isinstance(previous.get(key), str) for key in ("title", "message", "voice"))
                    or not any(item.get("id") == identity and item.get("episode") == member["episode"]
                               for item in previous["members"])):
                new_channels.setdefault(channel, []).append(identity)
                continue
            key = (previous["id"], channel)
            if key not in resolved:
                states = [(item, member_state(item, previous, channel)) for item in previous["members"]]
                retired = any(state == "retire" for _, state in states)
                survivors = [item for item, state in states
                             if state != "retire" and not (retired and state == "acknowledged")]
                if not survivors or any(state == "defer" for _, state in states):
                    resolved[key] = None
                elif len(survivors) == len(previous["members"]):
                    resolved[key] = previous
                else:
                    replacement_key = (previous["id"], tuple((item["id"], item["episode"]) for item in survivors))
                    if replacement_key not in replacements:
                        replacements[replacement_key] = freeze(survivors)
                    resolved[key] = replacements[replacement_key]
            batch = resolved[key]
            if batch is not None and any(item["id"] == identity for item in batch["members"]):
                schedule(batch, channel, identity)

    fresh = {}
    for channel, identities in new_channels.items():
        membership = tuple(sorted(identities))
        if membership not in fresh:
            fresh[membership] = freeze([member_for(rows[identity]) for identity in membership])
        for identity in identities:
            schedule(fresh[membership], channel, identity)
    for delivery in deliveries.values():
        delivery["recipients"] = {channel: tuple(identities)
                                  for channel, identities in delivery["recipients"].items()}
    return list(deliveries.values()), assignments
