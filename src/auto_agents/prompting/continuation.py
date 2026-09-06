"""Deterministic input synchronization; complete PromptSpecs stay authoritative."""
from dataclasses import asdict
import json

from .core import digest


def _hash_records(records):
    return digest(json.dumps(records, sort_keys=True, ensure_ascii=False))


def context_records(spec):
    counts = {}
    records = []
    for context in spec.contexts:
        ordinal = counts.get(context.source, 0)
        counts[context.source] = ordinal + 1
        identifier = context.context_id or f"context:{context.source}:{ordinal}"
        records.append({**asdict(context), "context_id": identifier})
    return records


def input_checkpoint(spec, conversation, execution_log, response=""):
    records = context_records(spec)
    return {
        "conversation_count": len(conversation),
        "conversation_hash": _hash_records(conversation),
        "execution_count": len(execution_log),
        "execution_hash": _hash_records(execution_log),
        "contexts": {item["context_id"]: digest(item["text"]) for item in records},
        "response_hash": digest(response) if response else "",
    }


def delta_context(spec, previous, conversation, execution_log):
    """Return a lossless update or a reason requiring a fresh native session."""
    if not isinstance(previous, dict) or not isinstance(previous.get("contexts"), dict):
        return "", "missing-input-checkpoint"
    for name, records in (("conversation", conversation), ("execution", execution_log)):
        count = previous.get(name + "_count")
        if not isinstance(count, int) or count < 0 or count > len(records):
            return "", name + "-history-changed"
        if _hash_records(records[:count]) != previous.get(name + "_hash"):
            return "", name + "-history-changed"
    current = context_records(spec)
    identifiers = [item["context_id"] for item in current]
    if len(set(identifiers)) != len(identifiers):
        return "", "ambiguous-context-identity"
    missing = set(previous["contexts"]) - set(identifiers)
    if any(not key.startswith(("conversation:", "execution:")) for key in missing):
        return "", "context-removed"
    updates = []
    for item in current:
        content_hash = digest(item["text"])
        if previous["contexts"].get(item["context_id"]) == content_hash:
            continue
        # The native model already received its own successful answer. Match
        # both the exact answer and the expected history position, never text alone.
        if (item["context_id"] == f"conversation:{previous['conversation_count']}"
                and item["source"] in {"Conversation: agent", "Conversation: assistant"}
                and content_hash == previous.get("response_hash")):
            continue
        updates.append(item)
    return (
        "Continue the same owned task. Unchanged contracts remain binding. "
        "The following context values replace earlier values with the same context_id; "
        "conversation entries retain their chronological order.\n"
        + json.dumps(updates, ensure_ascii=False, indent=2),
        "",
    )
