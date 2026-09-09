"""Loop V1 Package A: public fetch + source verification adapter tests."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from fragment_loop.cognitive_retrieval import run_public_retrieval
from fragment_loop.cognitive_source_verification import (
    CognitiveSourceVerificationError,
    run_public_source_verification,
    validate_public_source_verification_result,
)
from fragment_loop.public_source_fetch import (
    MAX_BODY_BYTES,
    FetchTransport,
    PublicFetchError,
    SourceClassifier,
    build_fetch_request,
    extract_visible_text,
    make_public_fetch_verifier,
)

CLOCK = datetime(2026, 8, 1, 10, 30, tzinfo=timezone(timedelta(hours=8)))
PEER_IP = "93.184.216.34"
LOCATOR_1 = "https://example.com/research/coffee-roasting"
LOCATOR_2 = "https://example.org/industry/general-tool-fit"
PAGE_1_SENTENCE = "来源页面原文：某虚构团队确实使用通用工具研究咖啡烘焙行业。"
PAGE_2_SENTENCE = "来源页面原文：另一份合成材料讨论通用工具的行业适配。"
PAGE_1_HTML = (
    "<html><head><title>合成页面标题一</title>"
    "<script>var secret = 'must not appear';</script>"
    "<style>.x { color: red; }</style></head>"
    f"<body><h1>页面主标题</h1><p>{PAGE_1_SENTENCE}</p>"
    "<p>窗口外的补充段落。</p></body></html>"
)
PAGE_2_HTML = f"<html><body><p>{PAGE_2_SENTENCE}</p></body></html>"
REQUEST: dict[str, object] = {
    "request_id": "public-fetch-adapter-test-1",
    "queries": [{"query_id": "Q1", "question": "公开检索咖啡烘焙行业研究工具？"}],
    "search_dimensions": ["公开行业实践"],
}


def retrieval_result(locators: list[str] | None = None) -> dict[str, object]:
    targets = locators or [LOCATOR_1, LOCATOR_2]
    materials = []
    for index, locator in enumerate(targets):
        excerpt = f"搜索摘要：公开材料 {index + 1} 的发现片段。"
        materials.append(
            {
                "retrieval_id": f"R{index + 1}",
                "query_refs": ["Q1"],
                "title": f"公开材料 {index + 1}",
                "locator": locator,
                "published_at_claim": "2026-07",
                "source_type_hint": "unknown",
                "acquisition_method": "public_search_adapter",
                "excerpt": excerpt,
                "excerpt_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
            }
        )
    return run_public_retrieval(
        REQUEST,
        lambda _request: {
            "status": "complete",
            "error_category": "none",
            "materials": materials,
        },
    )


def page_for(locator: str) -> str:
    return {LOCATOR_1: PAGE_1_HTML, LOCATOR_2: PAGE_2_HTML}[locator]


def make_fetch(
    calls: list[Mapping[str, object]],
    **overrides: object,
) -> FetchTransport:
    def fetch(request: Mapping[str, object]) -> Mapping[str, object]:
        calls.append(request)
        locator = str(request["locator"])
        result: dict[str, object] = {
            "http_status": 200,
            "content_type": "text/html; charset=utf-8",
            "body_bytes": page_for(locator).encode("utf-8"),
            "final_locator": locator,
            "peer_ip": PEER_IP,
            "resolved_ips": [PEER_IP],
        }
        result.update(overrides)
        return result

    return fetch


def selection_for(retrieval_id: str, locator: str) -> dict[str, object]:
    sentence = {LOCATOR_1: PAGE_1_SENTENCE, LOCATOR_2: PAGE_2_SENTENCE}[locator]
    return {
        "retrieval_id": retrieval_id,
        "origin_id": f"public-origin-{retrieval_id.lower()}",
        "source_tier": "secondary",
        "version": "2026-07 公开版",
        "relevance": "direct",
        "independence": "non_independent",
        "source_type": "industry_practice",
        "published_at": "2026-07",
        "verification_basis": "对冻结 locator 单次 200 获取并保留可复算证据窗口",
        "scope": "仅证明当次获取返回 200，不证明发布者身份或主张真实",
        "evidence_context": sentence,
        "evidence_excerpt": sentence.split("：", 1)[1],
    }


def make_classifier(
    calls: list[Mapping[str, object]],
    overrides: Mapping[str, Mapping[str, object]] | None = None,
) -> SourceClassifier:
    def classifier(request: Mapping[str, object]) -> Mapping[str, object]:
        calls.append(request)
        materials = request["materials"]
        assert isinstance(materials, list)
        selections = []
        for material in materials:
            assert isinstance(material, dict)
            locator = str(material["locator"])
            retrieval_id = str(material["retrieval_id"])
            selection = selection_for(retrieval_id, locator)
            if overrides and retrieval_id in overrides:
                selection.update(overrides[retrieval_id])
            selections.append(selection)
        return {"selections": selections}

    return classifier


def run_batch(
    fetch: FetchTransport,
    classifier: SourceClassifier,
    retrieval: dict[str, object] | None = None,
) -> dict[str, object]:
    verifier = make_public_fetch_verifier(fetch, classifier, clock=lambda: CLOCK)
    result = run_public_source_verification(retrieval or retrieval_result(), verifier)
    assert isinstance(result, dict)
    return result


def test_happy_path_fetches_each_locator_once_and_recomputes_every_fact() -> None:
    fetch_calls: list[Mapping[str, object]] = []
    classifier_calls: list[Mapping[str, object]] = []

    result = run_batch(make_fetch(fetch_calls), make_classifier(classifier_calls))

    assert result["contract_status"] == "validated"
    assert result["verification_status"] == "complete"
    assert len(fetch_calls) == 2
    assert [str(call["locator"]) for call in fetch_calls] == [LOCATOR_1, LOCATOR_2]
    for call in fetch_calls:
        assert call == build_fetch_request(str(call["locator"]))
        assert call["connect_timeout_seconds"] == 10
        assert call["total_timeout_seconds"] == 30
        assert call["max_body_bytes"] == MAX_BODY_BYTES
        assert call["allow_redirects"] is False
        assert call["allow_proxy"] is False
    assert len(classifier_calls) == 1
    materials_input = classifier_calls[0]["materials"]
    assert isinstance(materials_input, list)
    first_input = materials_input[0]
    assert isinstance(first_input, dict)
    assert first_input["locator"] == LOCATOR_1
    assert str(first_input["snippet"]).startswith("搜索摘要：")
    assert PAGE_1_SENTENCE in str(first_input["canonical_text"])
    assert "must not appear" not in str(first_input["canonical_text"])
    facts = first_input["transport_facts"]
    assert isinstance(facts, dict)
    assert facts["peer_ip"] == PEER_IP
    assert facts["body_sha256"] == hashlib.sha256(
        PAGE_1_HTML.encode("utf-8")
    ).hexdigest()

    assessments = result["assessments"]
    assert isinstance(assessments, list)
    assert len(assessments) == 2
    first = assessments[0]
    assert isinstance(first, dict)
    assert first["authenticity"] == "unverified"
    assert first["final_locator"] == LOCATOR_1
    assert first["http_status"] == 200
    assert first["fetched_at"] == CLOCK.isoformat()
    assert first["source_content_sha256"] == hashlib.sha256(
        PAGE_1_HTML.encode("utf-8")
    ).hexdigest()
    context = first["evidence_context"]
    excerpt = first["evidence_excerpt"]
    assert isinstance(context, str) and isinstance(excerpt, str)
    assert excerpt in context
    assert context in extract_visible_text("text/html", PAGE_1_HTML)
    assert first["evidence_context_sha256"] == hashlib.sha256(
        context.encode()
    ).hexdigest()
    assert first["evidence_excerpt_sha256"] == hashlib.sha256(
        excerpt.encode()
    ).hexdigest()
    cards = result["source_cards"]
    assert isinstance(cards, list)
    assert cards[0]["evidence_excerpt"] == excerpt
    assert cards[0]["source_type"] == "industry_practice"
    assert cards[0]["authenticity"] == "unverified"
    retrieval = result["retrieval_result"]
    assert isinstance(retrieval, dict)
    retrieval_materials = retrieval["materials"]
    assert isinstance(retrieval_materials, list)
    snippet = retrieval_materials[0]["excerpt"]
    assert cards[0]["evidence_excerpt"] != snippet
    assert validate_public_source_verification_result(result) == ()


def test_unknown_source_type_hint_never_reaches_a_source_card() -> None:
    result = run_batch(make_fetch([]), make_classifier([]))
    retrieval = result["retrieval_result"]
    assert isinstance(retrieval, dict)
    materials = retrieval["materials"]
    assert isinstance(materials, list)
    assert all(material["source_type_hint"] == "unknown" for material in materials)
    cards = result["source_cards"]
    assert isinstance(cards, list)
    assert all(card["source_type"] != "unknown" for card in cards)


def test_fetch_transport_exception_fails_the_whole_batch_before_classifier() -> None:
    fetch_calls: list[Mapping[str, object]] = []
    classifier_calls: list[Mapping[str, object]] = []

    def failing_fetch(request: Mapping[str, object]) -> Mapping[str, object]:
        fetch_calls.append(request)
        raise RuntimeError("secret fetch diagnostic")

    result = run_batch(failing_fetch, make_classifier(classifier_calls))

    assert len(fetch_calls) == 1
    assert classifier_calls == []
    assert result["contract_status"] == "rejected"
    assert result["error_category"] == "source_verifier_failed"
    assert result["assessments"] == []
    assert result["source_cards"] == []
    assert "secret" not in str(result)


def test_second_material_is_never_fetched_after_the_first_fails() -> None:
    fetch_calls: list[Mapping[str, object]] = []

    result = run_batch(
        make_fetch(fetch_calls, http_status=500),
        make_classifier([]),
    )

    assert len(fetch_calls) == 1
    assert result["contract_status"] == "rejected"
    assert result["error_category"] == "source_verifier_failed"


def test_non_200_redirect_and_final_locator_drift_fail_closed() -> None:
    overrides: list[dict[str, object]] = [
        {"http_status": 301},
        {"http_status": 404},
        {"http_status": True},
        {"final_locator": "https://example.com/elsewhere"},
    ]
    for override in overrides:
        result = run_batch(make_fetch([], **override), make_classifier([]))
        assert result["contract_status"] == "rejected", override
        assert result["error_category"] == "source_verifier_failed", override


def test_content_type_charset_size_and_decode_fail_closed() -> None:
    cases: list[dict[str, object]] = [
        {"content_type": "application/json"},
        {"content_type": "text/xml"},
        {"content_type": "text/html; charset=gb2312"},
        {"content_type": 200},
        {"body_bytes": b"x" * (MAX_BODY_BYTES + 1)},
        {"body_bytes": b"\xff\xfe invalid utf-8"},
        {"body_bytes": "not bytes"},
    ]
    for override in cases:
        result = run_batch(make_fetch([], **override), make_classifier([]))
        assert result["contract_status"] == "rejected", override
        assert result["assessments"] == [], override


def test_empty_visible_text_fails_closed() -> None:
    result = run_batch(
        make_fetch([], body_bytes=b"<html><body><script>x()</script></body></html>"),
        make_classifier([]),
    )
    assert result["contract_status"] == "rejected"
    assert result["error_category"] == "source_verifier_failed"


def test_dns_and_peer_ip_double_check_fails_closed() -> None:
    bad_ips = [
        "127.0.0.1",
        "10.0.0.8",
        "192.168.1.1",
        "169.254.1.1",
        "224.0.0.1",
        "240.0.0.1",
        "0.0.0.0",
        "::1",
        "not-an-ip",
    ]
    for ip in bad_ips:
        overrides: list[dict[str, object]] = [
            {"peer_ip": ip},
            {"resolved_ips": [ip]},
            {"peer_ip": PEER_IP, "resolved_ips": [ip]},
        ]
        for override in overrides:
            result = run_batch(make_fetch([], **override), make_classifier([]))
            assert result["contract_status"] == "rejected", override
    result = run_batch(
        make_fetch([], peer_ip=PEER_IP, resolved_ips=["8.8.8.8"]),
        make_classifier([]),
    )
    assert result["contract_status"] == "rejected"
    result = run_batch(make_fetch([], resolved_ips=[]), make_classifier([]))
    assert result["contract_status"] == "rejected"


def test_malformed_fetch_result_shape_fails_closed() -> None:
    malformed_results: list[Mapping[str, object]] = [
        {"http_status": 200},
        {
            "http_status": 200,
            "content_type": "text/html",
            "body_bytes": b"x",
            "final_locator": LOCATOR_1,
            "peer_ip": PEER_IP,
            "resolved_ips": [PEER_IP],
            "extra": 1,
        },
    ]
    for override_result in malformed_results:
        result = run_batch(lambda _request: override_result, make_classifier([]))
        assert result["contract_status"] == "rejected"


def test_fetch_request_rejects_non_public_locators_before_transport() -> None:
    for locator in [
        "http://example.com/insecure",
        "https://127.0.0.1/loopback",
        "https://user@example.com/userinfo",
        "https://example.com/page#fragment",
    ]:
        with pytest.raises(PublicFetchError, match="locator_invalid"):
            build_fetch_request(locator)


def test_text_extraction_is_deterministic_and_skips_script_style() -> None:
    first = extract_visible_text("text/html", PAGE_1_HTML)
    second = extract_visible_text("text/html", PAGE_1_HTML)
    assert first == second
    assert PAGE_1_SENTENCE in first
    assert "must not appear" not in first
    assert "color: red" not in first
    assert extract_visible_text("text/plain", "纯文本正文。\n第二行。") == "纯文本正文。\n第二行。"


def test_classifier_context_and_excerpt_must_come_from_the_fetched_text() -> None:
    cases = [
        {"evidence_context": "正文里根本不存在的一句话。"},
        {"evidence_context": ""},
        {"evidence_context": PAGE_1_HTML},  # raw HTML is not the canonical text
        {"evidence_excerpt": "上下文之外的片段。"},
        {"evidence_excerpt": ""},
        {"evidence_context": "x" * 16_385},
    ]
    for override in cases:
        result = run_batch(
            make_fetch([]),
            make_classifier([], overrides={"R1": override}),
        )
        assert result["contract_status"] == "rejected", override
        assert result["error_category"] == "source_verifier_failed", override
        assert result["assessments"] == [], override


def test_classifier_overprivileged_or_missing_fields_fail_closed() -> None:
    extra_field_selection = selection_for("R1", LOCATOR_1)
    extra_field_selection["authenticity"] = "verified"
    cases: list[Mapping[str, object]] = [
        {"selections": [extra_field_selection]},
        {"selections": []},
        {"selections": [selection_for("R1", LOCATOR_1)], "extra": 1},
        {"unexpected": []},
    ]
    for response in cases:
        result = run_batch(make_fetch([]), lambda _request: response)
        assert result["contract_status"] == "rejected", response

    def missing_key_classifier(_request: Mapping[str, object]) -> Mapping[str, object]:
        selection = selection_for("R1", LOCATOR_1)
        del selection["source_type"]
        return {"selections": [selection, selection_for("R2", LOCATOR_2)]}

    result = run_batch(make_fetch([]), missing_key_classifier)
    assert result["contract_status"] == "rejected"


def test_classifier_exception_fails_closed() -> None:
    def failing_classifier(_request: Mapping[str, object]) -> Mapping[str, object]:
        raise RuntimeError("secret classifier diagnostic")

    result = run_batch(make_fetch([]), failing_classifier)

    assert result["contract_status"] == "rejected"
    assert result["error_category"] == "source_verifier_failed"
    assert "secret" not in str(result)


def test_classifier_cannot_mutate_the_adapter_facts() -> None:
    def mutating_classifier(request: Mapping[str, object]) -> Mapping[str, object]:
        materials = request["materials"]
        assert isinstance(materials, list)
        for material in materials:
            assert isinstance(material, dict)
            material["canonical_text"] = "被分类器篡改的正文"
            facts = material["transport_facts"]
            assert isinstance(facts, dict)
            facts["http_status"] = 500
        selections = [
            selection_for(str(material["retrieval_id"]), str(material["locator"]))
            for material in materials
            if isinstance(material, dict)
        ]
        return {"selections": selections}

    result = run_batch(make_fetch([]), mutating_classifier)

    assert result["contract_status"] == "validated"
    assessments = result["assessments"]
    assert isinstance(assessments, list)
    assert assessments[0]["http_status"] == 200
    assert validate_public_source_verification_result(result) == ()


def test_adapter_ignores_classifier_time_and_recomputes_it() -> None:
    result = run_batch(make_fetch([]), make_classifier([]))
    assessments = result["assessments"]
    assert isinstance(assessments, list)
    assert all(item["fetched_at"] == CLOCK.isoformat() for item in assessments)


def test_not_found_retrieval_never_reaches_the_fetch_verifier() -> None:
    retrieval = run_public_retrieval(
        REQUEST,
        lambda _request: {"status": "not_found", "error_category": "none", "materials": []},
    )
    fetch_calls: list[Mapping[str, object]] = []
    verifier = make_public_fetch_verifier(
        make_fetch(fetch_calls), make_classifier([]), clock=lambda: CLOCK
    )
    with pytest.raises(CognitiveSourceVerificationError, match="not_verifiable"):
        run_public_source_verification(retrieval, verifier)
    assert fetch_calls == []


def test_persisted_drift_is_detected_by_offline_revalidation() -> None:
    result = run_batch(make_fetch([]), make_classifier([]))
    tampered = deepcopy(result)
    assessments = tampered["assessments"]
    assert isinstance(assessments, list)
    first = assessments[0]
    assert isinstance(first, dict)
    first["evidence_context"] = "漂移后的证据窗口"
    assert validate_public_source_verification_result(tampered)

    tampered_card = deepcopy(result)
    cards = tampered_card["source_cards"]
    assert isinstance(cards, list)
    card = cards[0]
    assert isinstance(card, dict)
    card["authenticity"] = "verified"
    assert validate_public_source_verification_result(tampered_card)
