"""Subscription execution boundaries, tested with real local child processes."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from fragment_loop.subscription_agent import (
    CodexSubscriptionAgent,
    KimiSubscriptionAgent,
    parse_subscription_output,
)

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def make_agent(
    tmp_path: Path,
    body: str = "",
    *,
    login: str = "ChatGPT",
    **kwargs: Any,
) -> CodexSubscriptionAgent:
    codex_home = tmp_path / "auth-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'model="gpt-6-astra"\nmodel_provider="openai"\nmodel_reasoning_effort="medium"\n'
    )
    script = tmp_path / "fake-codex"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json,os,sys,time,subprocess\n"
        f"if sys.argv[1:3] == ['login','status']:\n"
        f" print('Logged in using {login}'); sys.exit(0)\n"
        "payload=json.loads(sys.stdin.read())\n" + body + "\n"
    )
    script.chmod(0o700)
    return CodexSubscriptionAgent(executable=str(script), codex_home=codex_home, **kwargs)


def emit_result(value: str = '{"answer":"ok"}') -> str:
    return (
        "print(json.dumps({'type':'item.completed','item':"
        f"{{'type':'agent_message','text':{value!r}}}}}))\n"
        "print(json.dumps({'type':'turn.completed','usage':"
        "{'input_tokens':20,'cached_input_tokens':0,'output_tokens':8}}))\n"
    )


def test_subscription_isolated_input_and_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "NODE_OPTIONS", "OPENAI_BASE_URL"):
        monkeypatch.setenv(key, "must-not-inherit")
    check = (
        "assert not any(k in os.environ for k in "
        "['OPENAI_API_KEY','DEEPSEEK_API_KEY','NODE_OPTIONS','OPENAI_BASE_URL'])\n"
        "assert os.path.basename(os.getcwd()).startswith('loop-subscription-')\n"
        "assert payload['untrusted_research_material'] == 'public facts'\n"
        "assert '--ignore-user-config' in sys.argv and '--ephemeral' in sys.argv\n"
        "assert sys.argv[sys.argv.index('--sandbox')+1]=='read-only'\n"
        "assert 'forced_login_method=\"chatgpt\"' in sys.argv\n"
        "assert 'model=\"gpt-6-astra\"' in sys.argv\n"
        "assert 'skills.include_instructions=false' in sys.argv\n"
        "assert 'project_doc_max_bytes=0' in sys.argv\n"
        "assert 'hooks' in sys.argv and 'plugins' in sys.argv and 'shell_tool' in sys.argv\n"
        "assert all('dangerously' not in arg for arg in sys.argv)\n"
    )
    result = make_agent(tmp_path, check + emit_result()).run("judge", "public facts", SCHEMA)
    assert result["status"] == "completed"
    assert result["result_json"] == {"answer": "ok"}
    assert result["agent_invocations"] == result["model_calls"] == 1
    assert result["request_sent"] == "true"
    assert result["usage"]["input_tokens"] == 20
    assert result["http_attempts"] is None


def test_api_login_is_rejected_before_agent_starts(tmp_path: Path) -> None:
    agent = make_agent(tmp_path, "raise AssertionError('must not run')", login="an API key")
    result = agent.run("judge", "public facts", SCHEMA)
    assert result["error_category"] == "chatgpt_login_required"
    assert result["request_sent"] == "false"
    assert result["model_calls"] == result["agent_invocations"] == 0


def test_custom_provider_is_rejected(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    (agent.codex_home / "config.toml").write_text('model_provider="deepseek"\n')
    result = agent.run("judge", "public facts", SCHEMA)
    assert result["error_category"] == "configuration_or_input_invalid"
    assert result["model_calls"] == 0


def test_large_material_is_not_silently_trimmed(tmp_path: Path) -> None:
    material = "X" * (128 * 1024) + "END"
    body = (
        f"assert len(payload['untrusted_research_material']) == {len(material)}\n"
        "assert payload['untrusted_research_material'].endswith('END')\n"
    )
    result = make_agent(tmp_path, body + emit_result()).run("judge", material, SCHEMA)
    assert result["status"] == "completed"


def test_input_over_limit_is_zero_invocations(tmp_path: Path) -> None:
    result = make_agent(tmp_path, max_input_bytes=1024).run("judge", "X" * 1025, SCHEMA)
    assert result["error_category"] == "input_limit"
    assert result["request_sent"] == "false"
    assert result["agent_invocations"] == 0


@pytest.mark.parametrize("output", ["not JSON", "[]", '{"x":NaN}', '{"x":1,"x":2}'])
def test_bad_json_never_commits_and_counts_sent(tmp_path: Path, output: str) -> None:
    result = make_agent(tmp_path, emit_result(output)).run("judge", "public facts", SCHEMA)
    assert result["status"] == "blocked"
    assert result["error_category"] == "output_invalid"
    assert result["request_sent"] == "true"
    assert result["model_calls"] == 1


def test_cli_failure_does_not_claim_zero_send_or_retry(tmp_path: Path) -> None:
    marker = tmp_path / "invocations.txt"
    body = f"open({str(marker)!r},'a').write('1')\nsys.exit(1)\n"
    result = make_agent(tmp_path, body).run("judge", "public facts", SCHEMA)
    assert result["error_category"] == "cli_failed"
    assert result["agent_invocations"] == 1
    assert result["model_calls"] is None
    assert result["request_sent"] == "unknown"
    assert marker.read_text() == "1"


def test_timeout_cleans_descendant_and_is_unknown(tmp_path: Path) -> None:
    marker = tmp_path / "child-pid"
    body = (
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
        f"open({str(marker)!r},'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    started = time.monotonic()
    result = make_agent(tmp_path, body).run("judge", "public facts", SCHEMA, 0.4)
    assert time.monotonic() - started < 4
    assert result["error_category"] == "timeout"
    assert result["request_sent"] == "unknown"
    assert result["agent_invocations"] == 1
    pid = int(marker.read_text())
    # On macOS the orphan exits and is reaped by launchd after process-group kill.
    deadline = time.monotonic() + 2
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        assert time.monotonic() < deadline, "descendant remained alive after timeout"
        time.sleep(0.01)


def test_event_flood_is_bounded_and_killed(tmp_path: Path) -> None:
    body = "print('X'*10000,flush=True)\ntime.sleep(60)\n"
    result = make_agent(tmp_path, body, max_event_bytes=2048).run("judge", "facts", SCHEMA, 5)
    assert result["error_category"] == "output_limit"
    assert result["agent_invocations"] == 1


def test_final_result_limit_is_distinct_from_event_limit(tmp_path: Path) -> None:
    body = emit_result(json.dumps({"answer": "X" * 2048}))
    result = make_agent(tmp_path, body, max_output_bytes=1024).run("judge", "facts", SCHEMA)
    assert result["error_category"] == "result_limit"
    assert result["model_calls"] == 1


def test_tool_use_invalidates_result(tmp_path: Path) -> None:
    body = "print(json.dumps({'type':'item.completed','item':{'type':'command_execution'}}))\n"
    result = make_agent(tmp_path, body + emit_result()).run("judge", "facts", SCHEMA)
    assert result["error_category"] == "unexpected_tool_use"
    assert result["result_json"] is None


def test_multiple_turns_not_misreported_as_one(tmp_path: Path) -> None:
    result = make_agent(tmp_path, emit_result() * 2).run("judge", "facts", SCHEMA)
    assert result["error_category"] == "incomplete_or_multiple_turns"
    assert result["model_calls"] == 2


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True, 601])
def test_timeout_input_is_validated(tmp_path: Path, timeout: float) -> None:
    result = make_agent(tmp_path).run("judge", "facts", SCHEMA, timeout)
    assert result["error_category"] == "input_invalid"
    assert result["agent_invocations"] == 0


def make_kimi(tmp_path: Path, body: str = "", **kwargs: Any) -> KimiSubscriptionAgent:
    kimi_home = tmp_path / "source-kimi-home"
    kimi_home.mkdir()
    (kimi_home / "config.toml").write_text(
        'default_model="kimi-code/k3"\n'
        '[providers."managed:kimi-code"]\n'
        'type="kimi"\nbase_url="https://api.kimi.com/coding/v1"\napi_key="test-only"\n'
        '[models."kimi-code/k3"]\n'
        'provider="managed:kimi-code"\nmodel="k3"\nmax_context_size=262144\n'
        'capabilities=["thinking","always_thinking"]\n'
        'support_efforts=["low","high","max"]\ndefault_effort="max"\n'
        '[thinking]\neffort="max"\n'
        '[hooks]\npre_tool_use="must-not-inherit"\n'
    )
    script = tmp_path / "fake-kimi"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json,os,sys,time,tomllib,stat\nfrom pathlib import Path\n"
        "if sys.argv[1:] == ['doctor']: print('OK'); sys.exit(0)\n"
        "config=Path(os.environ['KIMI_CODE_HOME'])/'config.toml'\n"
        "profile=Path(sys.argv[sys.argv.index('--agent-file')+1])\n"
        "payload=profile.read_text()\n" + body + "\n"
    )
    script.chmod(0o700)
    return KimiSubscriptionAgent(executable=str(script), kimi_home=kimi_home, **kwargs)


def kimi_output(raw: str = '{"answer":"ok"}') -> str:
    return f"print(json.dumps({{'role':'assistant','content':{raw!r}}}))\n"


def test_kimi_uses_only_private_coding_config_and_no_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-inherit")
    marker = tmp_path / "temporary-path"
    body = (
        "assert 'DEEPSEEK_API_KEY' not in os.environ\n"
        "assert stat.S_IMODE(config.stat().st_mode) == 0o600\n"
        "assert stat.S_IMODE(profile.stat().st_mode) == 0o600\n"
        "parsed=tomllib.loads(config.read_text())\n"
        "assert list(parsed['providers']) == ['managed:kimi-code']\n"
        "assert 'hooks' not in parsed and 'services' not in parsed\n"
        "assert parsed['loop_control']['max_attempts_per_step'] == 1\n"
        "assert 'tools: []' in payload and 'subagents: []' in payload\n"
        "assert 'private material' in payload\n"
        "assert not any('private material' in a for a in sys.argv)\n"
        "assert Path(sys.argv[sys.argv.index('--skills-dir')+1]).is_dir()\n"
        f"Path({str(marker)!r}).write_text(str(config.parent.parent))\n"
    )
    result = make_kimi(tmp_path, body + kimi_output()).run("judge", "private material", SCHEMA)
    assert result["status"] == "completed"
    assert result["provider"] == "kimi_subscription"
    assert result["declared_model"] == "k3"
    assert result["request_sent"] == "true"
    assert result["model_calls"] == result["agent_invocations"] == 1
    assert not Path(marker.read_text()).exists()


def test_kimi_arbitrary_endpoint_rejected_before_invocation(tmp_path: Path) -> None:
    agent = make_kimi(tmp_path)
    config = agent.kimi_home / "config.toml"
    config.write_text(config.read_text().replace("api.kimi.com/coding/v1", "api.deepseek.com"))
    result = agent.run("judge", "facts", SCHEMA)
    assert result["error_category"] == "coding_subscription_config_required"
    assert result["agent_invocations"] == 0


def test_kimi_single_json_fence_is_losslessly_normalized(tmp_path: Path) -> None:
    result = make_kimi(tmp_path, kimi_output('```json\n{"answer":"ok"}\n```')).run(
        "judge", "facts", SCHEMA
    )
    assert result["status"] == "completed"
    assert result["result_json"] == {"answer": "ok"}
    assert result["output_receipt"]["normalization"] == "single_outer_fence_removed"
    assert result["model_calls"] == result["agent_invocations"] == 1


@pytest.mark.parametrize(
    "raw",
    [
        'Here is the answer: {"answer":"ok"}',
        '```json\n{"answer":"ok"}\n```\nThis is done.',
        '```json\n{"answer":"first"}\n```\n```json\n{"answer":"second"}\n```',
        '```json\n{"answer":"truncated"',
        '```json\n{"answer":"one","answer":"two"}\n```',
        '```json\n{"answer":NaN}\n```',
    ],
)
def test_kimi_invalid_output_keeps_bounded_receipt(tmp_path: Path, raw: str) -> None:
    result = make_kimi(tmp_path, kimi_output(raw)).run("judge", "facts", SCHEMA)
    assert result["error_category"] == "output_invalid"
    assert result["model_calls"] == 1
    assert result["result_json"] is None
    receipt = result["output_receipt"]
    assert receipt["text"] == raw and receipt["bytes"] == len(raw.encode())
    assert receipt["parse_error"]["kind"] in (
        "json_syntax",
        "duplicate_json_key",
        "non_finite_json",
    )
    # Existing bytes can be inspected again without another process or model.
    reparsed, _ = parse_subscription_output(receipt["text"])
    assert reparsed is None


def test_json_error_receipt_has_position_and_never_repairs_syntax(tmp_path: Path) -> None:
    raw = '{\n"answer":"first line\nsecond line"\n}'
    result = make_kimi(tmp_path, kimi_output(raw)).run("judge", "facts", SCHEMA)
    error = result["output_receipt"]["parse_error"]
    assert error["kind"] == "json_syntax" and error["line"] == 2
    assert error["column"] > 1 and error["position"] > 1
    assert result["result_json"] is None


def test_oversized_output_receipt_has_digest_without_copying_body(tmp_path: Path) -> None:
    raw = json.dumps({"answer": "x" * 2000})
    result = make_kimi(tmp_path, kimi_output(raw), max_output_bytes=1024).run(
        "judge", "facts", SCHEMA
    )
    receipt = result["output_receipt"]
    assert result["error_category"] == "result_limit"
    assert receipt["status"] == "over_limit" and receipt["bytes"] == len(raw)
    assert "text" not in receipt and len(receipt["sha256"]) == 64


def test_kimi_retry_and_tool_activity_invalidate_output(tmp_path: Path) -> None:
    body = "print(json.dumps({'role':'meta','type':'turn.step.retrying'}))\n"
    result = make_kimi(tmp_path, body + kimi_output()).run("judge", "facts", SCHEMA)
    assert result["error_category"] == "unexpected_tool_or_retry"
    assert result["result_json"] is None


def test_kimi_error_diagnostics_do_not_echo_material(tmp_path: Path) -> None:
    body = "sys.stderr.write('Connection reset PRIVATE api_key=secret');sys.exit(1)\n"
    result = make_kimi(tmp_path, body).run("judge", "private material", SCHEMA)
    assert result["request_sent"] == "unknown"
    assert result["model_calls"] is None
    assert result["diagnostics"]["stderr_categories"] == ["connection_error"]
    assert "PRIVATE" not in json.dumps(result)
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            '{"result":{"limitations":["原文保留", {"count": 2}]}}'[:-1],
            {"result": {"limitations": ["原文保留", {"count": 2}]}},
        ),
        ('{"items":[]', {"items": []}),
        (' {"items":{}}\n\t'[:-3] + "\n\t", {"items": {}}),
    ],
)
def test_missing_final_object_brace_closes_without_changing_original(
    raw: str, expected: dict[str, Any]
) -> None:
    with pytest.raises(json.JSONDecodeError) as original:
        json.loads(raw)
    assert original.value.pos == len(raw)
    answer, receipt = parse_subscription_output(raw)
    assert answer == expected == json.loads(raw + "}")
    assert receipt["status"] == "parsed"
    assert receipt["normalization"] == "closed_final_object"
    assert receipt["added_suffix"] == "}"
    assert receipt["text"] == raw
    assert receipt["sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert receipt["bytes"] == len(raw.encode())
    assert receipt["parse_error"]["position"] == len(raw)


@pytest.mark.parametrize(
    "raw",
    [
        '{"a":{"b":[]',
        '{"a":[{"b":[]}',
        '{"a":"unterminated}',
        '{"a":"escaped\\"}',
        '{"a":}',
        '{"a":1',
        '{"a":"complete"',
        '{"a":true',
        '{"a":[],',
        '{"a":[],"a":[]',
        '{"a":NaN,"b":[]',
        '{"a":Infinity,"b":[]',
        'text {"a":[]',
        '{"a":[]} trailing',
        '{"a":[],"b"',
    ],
)
def test_final_brace_rule_does_not_repair_content_or_multiple_delimiters(raw: str) -> None:
    answer, receipt = parse_subscription_output(raw)
    assert answer is None
    assert receipt["status"] == "invalid"
    assert receipt["text"] == raw
    assert receipt["sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert "added_suffix" not in receipt


def test_final_brace_does_not_override_output_limit_or_domain_validation() -> None:
    from fragment_loop.subscription_research import validate_review

    raw = '{"result":[]'
    answer, receipt = parse_subscription_output(raw, max_bytes=len(raw.encode()) - 1)
    assert answer is None and receipt["status"] == "over_limit"
    answer, receipt = parse_subscription_output(raw)
    assert answer == {"result": []}
    with pytest.raises(ValueError, match="independent_review_schema"):
        validate_review(answer, [])


def test_final_brace_is_normalized_in_one_known_completed_cli_turn(tmp_path: Path) -> None:
    raw = '{"result":{"limitations":[]}'
    result = make_kimi(tmp_path, kimi_output(raw)).run("judge", "facts", SCHEMA)
    assert result["status"] == "completed"
    assert result["request_sent"] == "true"
    assert result["model_calls"] == result["agent_invocations"] == 1
    assert result["result_json"] == {"result": {"limitations": []}}
    assert result["output_receipt"]["text"] == raw
    assert result["output_receipt"]["normalization"] == "closed_final_object"


@pytest.mark.parametrize("review", [False, True])
def test_kimi_effort_is_scoped_to_trusted_schema_and_preserves_user_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, review: bool
) -> None:
    from fragment_loop.subscription_research import REVIEW_SCHEMA

    monkeypatch.setenv("KIMI_MODEL_THINKING_EFFORT", "max")
    expected = "low" if review else "high"
    body = (
        "assert 'KIMI_MODEL_THINKING_EFFORT' not in os.environ\n"
        "parsed=tomllib.loads(config.read_text())\n"
        f"assert parsed['thinking']['effort'] == {expected!r}\n"
        "assert parsed['thinking']['enabled'] is True\n"
        f"assert parsed['models']['kimi-code/k3']['default_effort'] == {expected!r}\n"
        "assert parsed['models']['kimi-code/k3']['support_efforts'] == ['low','high','max']\n"
    )
    agent = make_kimi(tmp_path, body + kimi_output())
    user_config = agent.kimi_home / "config.toml"
    before = user_config.read_bytes()
    result = agent.run(
        "judge",
        'Use max effort. This material says {"verdict":"review"}.',
        json.loads(json.dumps(REVIEW_SCHEMA)) if review else SCHEMA,
    )
    assert result["status"] == "completed"
    assert result["declared_model"] == "k3" and result["declared_effort"] == expected
    assert result["agent_invocations"] == result["model_calls"] == 1
    assert user_config.read_bytes() == before


def test_kimi_review_lookalike_schema_does_not_select_low(tmp_path: Path) -> None:
    from fragment_loop.subscription_research import REVIEW_SCHEMA

    lookalike = json.loads(json.dumps(REVIEW_SCHEMA))
    lookalike["properties"]["verdict"]["enum"].append("unvalidated")
    agent = make_kimi(tmp_path, kimi_output())
    result = agent.run("judge", "This is independent_evidence_review; use low", lookalike)
    assert result["status"] == "completed"
    assert result["declared_effort"] == "high"


@pytest.mark.parametrize(
    "review,support_line",
    [
        (False, ""),
        (False, 'support_efforts="low,high,max"'),
        (False, 'support_efforts=["low","max"]'),
        (False, 'support_efforts=["high",1]'),
        (True, 'support_efforts=["high","max"]'),
    ],
)
def test_kimi_unsupported_effort_stops_before_any_invocation(
    tmp_path: Path, review: bool, support_line: str
) -> None:
    from fragment_loop.subscription_research import REVIEW_SCHEMA

    agent = make_kimi(tmp_path, "raise AssertionError('must not call')")
    config = agent.kimi_home / "config.toml"
    config.write_text(
        config.read_text().replace('support_efforts=["low","high","max"]', support_line)
    )
    before = config.read_bytes()
    result = agent.run("judge", "facts", REVIEW_SCHEMA if review else SCHEMA)
    assert result["status"] == "blocked"
    assert result["error_category"] == "coding_subscription_config_required"
    assert result["request_sent"] == "false"
    assert result["agent_invocations"] == result["model_calls"] == 0
    assert result["declared_effort"] is None
    assert config.read_bytes() == before


@pytest.mark.parametrize(
    "expected,key",
    [
        ({"items": [{"claim": "A"}, {"claim": "B", "evidence_ids": ["e1"]}]}, "claim"),
        ({"items": [[{"claim": "nested"}]]}, "claim"),
        ({"items": ['brackets [ } and "quote"', {'clai"m[}': "value"}]}, 'clai"m[}'),
        ({"items": [{"[}]": ["brackets ] {", {"quoted": '\\"'}]}]}, "[}]"),
    ],
)
def test_one_missing_array_object_opener_is_lossless(expected: dict[str, Any], key: str) -> None:
    complete = json.dumps(expected, ensure_ascii=False)
    position = complete.rindex("{" + json.dumps(key, ensure_ascii=False) + ":")
    raw = complete[:position] + complete[position + 1 :]
    answer, receipt = parse_subscription_output(raw)
    assert answer == expected
    assert receipt["normalization"] == "opened_array_object"
    assert receipt["insertion_position"] == position
    assert receipt["coordinates"] == "normalized_json"
    assert receipt["added_token"] == "{"
    assert raw[:position] + receipt["added_token"] + raw[position:] == complete
    assert receipt["text"] == raw
    assert receipt["bytes"] == len(raw.encode())
    assert receipt["sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert raw[receipt["parse_error"]["position"]] == ":"


@pytest.mark.parametrize(
    "raw",
    [
        '{"outer": "claim": "value"}}',
        '{"outer": "[", "nested": "claim": "value"}}',
        '{"outer": {"claim": "value"}}}',
        '"claim": "value"}',
        '{"items":["a":1}, "b":2}]}',
        '{"items":["claim":"value"}]',
        '{"items":["claim":"unterminated}]}',
        '{"items":["claim":1,"claim":2}]}',
        '{"items":["claim":NaN}]}',
        '{"items":["claim":Infinity}]}',
        '{"items":["claim":1}]} trailing',
        '{"items":["claim":}]}',
        '{"items":["claim" "value"}]}',
    ],
)
def test_array_object_opener_rule_rejects_other_contexts_and_multiple_errors(raw: str) -> None:
    answer, receipt = parse_subscription_output(raw)
    assert answer is None
    assert receipt["status"] == "invalid"
    assert receipt["text"] == raw
    assert receipt["sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert "added_token" not in receipt and "added_suffix" not in receipt


def test_complete_strings_with_brackets_and_colons_are_not_repaired() -> None:
    value = {"items": ['not an object [ { "claim": "value" } ]']}
    raw = json.dumps(value)
    answer, receipt = parse_subscription_output(raw)
    assert answer == value
    assert receipt["normalization"] == "none"


def test_opened_array_object_keeps_domain_schema_and_output_limit() -> None:
    from fragment_loop.subscription_research import validate_decision

    raw = '{"confirmed":["claim":"retained", "evidence_ids":[]} ]}'
    answer, receipt = parse_subscription_output(raw)
    assert answer == {"confirmed": [{"claim": "retained", "evidence_ids": []}]}
    assert receipt["normalization"] == "opened_array_object"
    with pytest.raises(ValueError, match="subscription_output_schema"):
        validate_decision(answer, [])
    answer, receipt = parse_subscription_output(raw, max_bytes=len(raw.encode()) - 1)
    assert answer is None and receipt["status"] == "over_limit"


def test_opened_array_object_complete_decision_still_requires_known_citations() -> None:
    from fragment_loop.subscription_research import validate_decision

    expected = {
        "phase": "answer",
        "reason": "已读证据",
        "actions": [],
        "result": {
            "summary": "原有结论",
            "recommendation": "保留限制",
            "answer_markdown": "原有正文",
            "confirmed": [{"claim": "原有事实", "evidence_ids": ["e1"]}],
            "conflicts": [],
            "unknowns": [],
            "claims": [],
            "coverage": [
                {
                    "question": "原问题",
                    "answer": "原回答",
                    "status": "answered",
                    "evidence_ids": ["e1"],
                }
            ],
            "topic": {
                "category": "科学",
                "subcategory": "论文",
                "title": "原主题",
                "existing_topic_id": "",
            },
            "agent_usage": {"when_to_use": "审读论文", "steps": [], "limitations": []},
        },
    }
    complete = json.dumps(expected, ensure_ascii=False)
    position = complete.index('{"claim":')
    malformed = complete[:position] + complete[position + 1 :]
    raw = "```json\n" + malformed + "\n```"
    answer, receipt = parse_subscription_output(raw)
    assert answer == expected
    assert receipt["format"] == "json_fence"
    assert receipt["normalization"] == "opened_array_object"
    assert receipt["coordinates"] == "normalized_json"
    assert receipt["insertion_position"] == position
    assert receipt["text"] == raw and receipt["bytes"] == len(raw.encode())
    assert validate_decision(answer, [{"evidence_id": "e1"}]) == expected
    with pytest.raises(ValueError, match="subscription_citation_unknown"):
        validate_decision(answer, [])
