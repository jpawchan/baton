"""Conservative Pi session accounting; no transcript text enters the public ledger."""

import hashlib
import json
from pathlib import Path

FIELDS = ("input", "output", "cacheRead", "cacheWrite", "totalTokens")


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            value.update(block)
    return value.hexdigest()


def session_usage(path):
    calls, issues = [], []
    session_id, model, provider, effort = None, None, None, None
    assistant_count = 0
    seen = set()
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return {"session_id": None, "calls": [], "issues": ["unreadable_session"], "assistant_count": 0}
    for index, line in enumerate(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            issues.append("malformed_session_line")
            continue
        if not isinstance(entry, dict):
            issues.append("malformed_session_entry")
            continue
        kind = entry.get("type")
        if kind == "session":
            if session_id is not None:
                issues.append("duplicate_session_header")
            session_id = entry.get("id") if isinstance(entry.get("id"), str) else None
            continue
        identity = entry.get("id")
        if not isinstance(identity, str):
            issues.append("missing_entry_id")
        elif identity in seen:
            issues.append("duplicate_entry")
            continue
        else:
            seen.add(identity)
        if kind == "model_change":
            model, provider = entry.get("modelId"), entry.get("provider")
        if kind == "thinking_level_change":
            effort = entry.get("thinkingLevel")
        message = entry.get("message", {})
        if not isinstance(message, dict):
            issues.append("malformed_message")
            continue
        if kind == "message" and message.get("role") == "toolResult" and message.get("usage"):
            issues.append("unattributed_nested_tool_usage")
        if kind == "message" and message.get("role") == "assistant":
            assistant_count += 1
            usage = message.get("usage")
            model, provider = message.get("model"), message.get("provider")
            stop = message.get("stopReason")
            if stop in ("error", "aborted", "pending") or message.get("errorMessage"):
                issues.append("provider_error_or_incomplete_response")
            if stop not in ("stop", "toolUse", "length", "error", "aborted", "pending"):
                issues.append("unknown_stop_reason")
        elif kind in ("compaction", "branch_summary"):
            # Retained message copies are NOT new calls; summary usage is counted once.
            usage, stop = entry.get("usage"), kind
        else:
            continue
        if not isinstance(usage, dict) or any(
            not isinstance(usage.get(key), int) or isinstance(usage.get(key), bool)
            or usage[key] < 0 for key in FIELDS
        ):
            issues.append("missing_or_invalid_usage")
            continue
        if sum(usage[key] for key in FIELDS[:-1]) != usage["totalTokens"]:
            issues.append("inconsistent_token_partition")
            continue
        if not isinstance(model, str) or not isinstance(provider, str) or not isinstance(effort, str):
            issues.append("missing_model_or_effort")
        calls.append({
            "entry": index + 1, "kind": kind, "model": model,
            "provider": provider, "effort": effort, "stop": stop,
            **{key: usage[key] for key in FIELDS},
        })
    if not session_id or assistant_count == 0:
        issues.append("empty_or_unidentified_session")
    return {"session_id": session_id, "calls": calls, "issues": sorted(set(issues)),
            "assistant_count": assistant_count}


def stream_summary(path):
    identity, ends, assistant_count = None, 0, 0
    issues = []
    try:
        with Path(path).open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    if line.strip():
                        issues.append("non_json_stream_line")
                    continue
                if not isinstance(event, dict):
                    issues.append("malformed_stream_event")
                    continue
                if event.get("type") == "session":
                    if identity is not None:
                        issues.append("duplicate_stream_header")
                    identity = event.get("id") if isinstance(event.get("id"), str) else None
                if event.get("type") == "agent_end":
                    ends += 1
                if (event.get("type") == "message_end"
                        and isinstance(event.get("message"), dict)
                        and event["message"].get("role") == "assistant"):
                    assistant_count += 1
    except (OSError, UnicodeError):
        issues.append("unreadable_stream")
    if not identity or not ends:
        issues.append("unfinished_or_unidentified_stream")
    return {"session_id": identity, "assistant_count": assistant_count,
            "issues": sorted(set(issues))}


def collect_usage(session_dir, logs):
    """Reconcile one fresh session per CLI stream, including all worker attempts."""
    ledger, issues = [], []
    sessions = {}
    evidence = []
    for path in sorted(Path(session_dir).glob("*.jsonl")):
        result = session_usage(path)
        identity = result["session_id"]
        if identity in sessions:
            issues.append("duplicate_session_identity")
        sessions[identity] = result
        issues.extend(result["issues"])
        evidence.append({"kind": "session", "sha256": digest(path)})
    matched = set()
    for label, path in logs:
        result = stream_summary(path)
        issues.extend(result["issues"])
        identity = result["session_id"]
        if identity in matched:
            issues.append("duplicate_stream_identity")
        matched.add(identity)
        session = sessions.get(identity)
        if session is None:
            issues.append("missing_session_for_stream")
            continue
        if result["assistant_count"] != session["assistant_count"]:
            issues.append("stream_session_call_count_mismatch")
        for call in session["calls"]:
            ledger.append({"stream": label, **call})
        evidence.append({"kind": label, "sha256": digest(path)})
    if set(sessions) != matched:
        issues.append("unmatched_session_or_stream")
    # Count unmatched sessions too: never hide observed extra-agent consumption.
    for identity in set(sessions) - matched:
        ledger.extend({"stream": "unexpected_session", **call}
                      for call in sessions[identity]["calls"])
    totals = {key: sum(call[key] for call in ledger) for key in FIELDS}
    return {
        "complete": bool(ledger) and not issues,
        "issues": sorted(set(issues)), "calls": len(ledger),
        "logical_input_tokens": totals["input"] + totals["cacheRead"] + totals["cacheWrite"],
        **totals, "ledger": ledger, "evidence": evidence,
    }
