import copy
import json
from pathlib import Path
from typing import Any

LIVE_KEY = "sk-live-ZZZZZZZZZZZZZZZZZZZZZZZZ"

_BASE_SLP: dict[str, Any] = {
    "id": "chatcmpl-abc123",
    "trace_id": "trace-0001",
    "litellm_call_id": "11111111-2222-3333-4444-555555555555",
    "call_type": "acompletion",
    "stream": False,
    "status": "success",
    "cache_hit": False,
    "startTime": 1791015615.269,
    "endTime": 1791015640.910,
    "model": "example-model",
    "model_id": "model-id-1",
    "model_group": "router",
    "api_base": "https://llm.example.invalid/v1",
    "custom_llm_provider": "openai",
    "prompt_tokens": 36842,
    "completion_tokens": 997,
    "total_tokens": 37839,
    "response_cost": 0.0123,
    "requester_ip_address": "127.0.0.1",
    "user_agent": "OpenAI/Python 2.24.0",
    "end_user": "",
    "request_tags": [],
    "model_parameters": {"temperature": 0.2, "tools": [{"type": "function", "function": {"name": "terminal"}}]},
    "metadata": {
        "user_api_key_alias": "agent-key",
        "user_api_key_hash": "hash123",
        "user_api_key_user_id": "user-1",
        "user_api_key_team_id": None,
        "user_api_key_end_user_id": None,
        "requester_metadata": {"source": "task-runner"},
        "requester_custom_headers": {
            "x-litellm-session-id": "session-42",
            "x-agent-task": "task-7",
            "x-other": "ignored",
        },
        "spend_logs_metadata": None,
    },
    "messages": [
        {"role": "system", "content": "You are an agent."},
        {"role": "user", "content": "Fetch the file list."},
    ],
    "response": {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "call_1", "function": {"name": "terminal", "arguments": "{\"command\": \"ls\"}"}}
                    ],
                    "reasoning_content": "thinking about it",
                }
            }
        ]
    },
    "error_str": None,
    "error_information": None,
}


def make_slp(**overrides: Any) -> dict[str, Any]:
    slp = copy.deepcopy(_BASE_SLP)
    slp.update(overrides)
    return slp


def make_kwargs(slp: dict[str, Any] | None = None, **litellm_params: Any) -> dict[str, Any]:
    params: dict[str, Any] = {"api_key": LIVE_KEY, "api_base": "https://llm.example.invalid/v1"}
    params.update(litellm_params)
    return {"api_key": LIVE_KEY, "litellm_params": params,
            "standard_logging_object": make_slp() if slp is None else slp}


def rec(call_id: str, ts: str, trace: str, model: str = "router", text: str = "hello", **extra: Any) -> dict:
    r = {
        "v": 1, "ts_start": ts, "ts_end": ts, "status": "success", "call_id": call_id, "id": f"resp-{call_id}",
        "trace_id": trace, "model": "example-model", "model_group": model,
        "caller": {"key_alias": "agent-key"},
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12, "cost": 0.0},
        "correlation": {"headers": {"x-litellm-session-id": trace}},
        "messages": [{"role": "user", "content": text}],
        "response": {"choices": [{"message": {"content": "ok"}}]},
    }
    r.update(extra)
    return r


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def profile_dict(tmp: Path, **sections: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "ledger": {"spool": str(tmp / "spool"), "spool_cap": "1MiB"},
        "store": {"type": "local", "path": str(tmp / "store")},
        "retention": {"max_age_days": 180, "max_bytes": "10GiB"},
    }
    raw.update(sections)
    return raw
