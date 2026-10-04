import json

import proxy_callback_import as cl
from fixtures import LIVE_KEY, make_kwargs, make_slp


def test_record_never_contains_api_key():
    record = cl.build_record(make_kwargs(), "success")
    assert LIVE_KEY not in json.dumps(record)


def test_record_carries_content_and_ids():
    record = cl.build_record(make_kwargs(), "success")
    assert record["v"] == cl.RECORD_VERSION
    assert record["status"] == "success"
    assert record["call_id"] == "11111111-2222-3333-4444-555555555555"
    assert record["id"] == "chatcmpl-abc123"
    assert record["trace_id"] == "trace-0001"
    assert record["model_group"] == "router"
    assert record["usage"] == {"prompt_tokens": 36842, "completion_tokens": 997, "total_tokens": 37839, "cost": 0.0123}
    assert record["caller"]["key_alias"] == "agent-key"
    assert record["messages"][1]["content"] == "Fetch the file list."
    assert record["response"]["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "terminal"
    assert record["model_parameters"]["tools"][0]["function"]["name"] == "terminal"


def test_record_timestamps_are_iso_utc():
    record = cl.build_record(make_kwargs(), "success")
    assert record["ts_start"] == "2026-10-03T08:20:15.269000+00:00"
    assert record["ts_end"] == "2026-10-03T08:20:40.910000+00:00"


def test_record_flags_client_no_log():
    assert cl.build_record(make_kwargs(**{"no-log": True}), "success")["client_requested_no_log"] is True
    assert cl.build_record(make_kwargs(), "success")["client_requested_no_log"] is False


def test_record_keeps_only_correlation_headers_by_default():
    record = cl.build_record(make_kwargs(), "success")
    assert record["correlation"]["headers"] == {"x-litellm-session-id": "session-42"}
    assert record["correlation"]["requester_metadata"] == {"source": "task-runner"}


def test_record_keeps_configured_header_prefixes():
    record = cl.build_record(make_kwargs(), "success", header_prefixes=("x-agent-",))
    assert record["correlation"]["headers"] == {"x-litellm-session-id": "session-42", "x-agent-task": "task-7"}


def test_failure_record_has_error():
    slp = make_slp(status="failure", response=None, error_str="RateLimitError: slow down",
                   error_information={"error_class": "RateLimitError", "error_code": "429"})
    record = cl.build_record(make_kwargs(slp), "failure")
    assert record["error"]["str"] == "RateLimitError: slow down"
    assert record["error"]["information"]["error_class"] == "RateLimitError"


def test_no_standard_logging_object_gives_none():
    assert cl.build_record({"litellm_params": {}}, "success") is None


def test_record_tolerates_missing_metadata():
    record = cl.build_record(make_kwargs(make_slp(metadata=None, model_parameters=None)), "success")
    assert record["caller"]["key_alias"] is None
    assert record["correlation"]["headers"] == {}
