"""Fragment Governed Research v1 包 A 反例：安全发现与抓取 transport（全离线）。

覆盖 DESIGN §11 矩阵中属于包 A 的项（rev1–rev3 采集/transport 反例；编号见
设计历史基线）与 §8 来源发现冻结项；所有网络件都是 fake connection / fake
resolver，任何真实 socket 经由爆炸桩证明零触达。
"""
# mypy: disable-error-code="no-untyped-def,untyped-decorator"

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from fragment_loop.research_fetch import (
    ResearchFetchError,
    ResearchFetchTransport,
    build_fetch_request,
    build_query_plan,
    discover_candidates,
    explicit_https_candidates,
    parse_ddg_candidates,
    validate_fetch_result,
)

NOW = datetime(2026, 8, 9, 0, 0, 0, tzinfo=UTC)
PAGE = b"<html><body><h1>Example Release</h1><p>published 2026-08-01</p></body></html>"


def clock() -> datetime:
    return NOW


class FakeSock:
    def __init__(self, peer: str):
        self.peer = peer
        self.timeouts: list[float] = []

    def getpeername(self) -> tuple[str, int]:
        return (self.peer, 443)

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)


class FakeResponse:
    def __init__(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8"):
        self.status = status
        self._body = body
        self._content_type = content_type

    def read(self, limit: int) -> bytes:
        return self._body[:limit]

    def getheader(self, name: str) -> str | None:
        return self._content_type if name.lower() == "content-type" else None


class FakeConnection:
    """离线假连接：记录全部交互，可按脚本失败；绝不触达真实 socket。"""

    def __init__(
        self,
        *,
        peer: str = "93.184.216.34",
        status: int = 200,
        body: bytes = PAGE,
        connect_error: Exception | None = None,
    ):
        self.sock = FakeSock(peer)
        self.status = status
        self.body = body
        self.connect_error = connect_error
        self.requests: list[tuple[str, str]] = []
        self.closed = False
        self.connected = False

    def connect(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    def request(self, method: str, url: str, body: Any = None, headers: Any = None) -> None:
        self.requests.append((method, url))

    def getresponse(self) -> FakeResponse:
        return FakeResponse(self.status, self.body)

    def close(self) -> None:
        self.closed = True


class ExplodingResolver:
    def __call__(self, host: str) -> frozenset[str]:
        raise AssertionError(f"DNS must never happen on this path: {host}")


def public_resolver(host: str) -> frozenset[str]:
    return frozenset({"93.184.216.34", "93.184.216.35"})


def make_transport(connection: FakeConnection, **overrides: Any) -> ResearchFetchTransport:
    options: dict[str, Any] = {
        "live_enabled": True,
        "resolver": public_resolver,
        "connection_factory": lambda host, port, timeout, context: connection,
    }
    options.update(overrides)
    return ResearchFetchTransport(**options)


def fetch_result(**overrides: Any) -> dict[str, Any]:
    result = {
        "http_status": 200,
        "content_type": "text/html; charset=utf-8",
        "body_bytes": PAGE,
        "final_locator": "https://example.com/release",
        "peer_ip": "93.184.216.34",
        "resolved_ips": ["93.184.216.34"],
    }
    result.update(overrides)
    return result


# -- 离线 URL 校验（rev1–rev3 采集反例：仅 https、拒 IP 字面量/localhost/私网）


def test_fetch_request_rejects_non_https_and_ip_literals() -> None:
    """包 A（rev1–rev3 采集反例）：非 https / IP 字面量 / localhost 一律拒绝。"""
    for bad in (
        "http://example.com/x",
        "https://127.0.0.1/x",
        "https://192.168.1.1/x",
        "https://localhost/x",
        "https://example.local/x",
        "https://user:pw@example.com/x",
        "https://example.com/x#frag",
        "https://0x7f000001/x",
    ):
        with pytest.raises(ResearchFetchError) as caught:
            build_fetch_request(bad)
        assert caught.value.code == "research_fetch_locator_invalid"


def test_fetch_request_descriptor_frozen() -> None:
    """包 A：transport 描述符逐字冻结（10s/15s、1 MiB、禁重定向、禁代理）。"""
    request = build_fetch_request("https://example.com/release")
    assert request == {
        "locator": "https://example.com/release",
        "connect_timeout_seconds": 10,
        "total_timeout_seconds": 15,
        "max_body_bytes": 1_048_576,
        "allow_redirects": False,
        "allow_proxy": False,
    }


# -- transport 自报事实复核（任何漂移 fail-closed）


def test_validate_fetch_result_happy_path() -> None:
    checked = validate_fetch_result("https://example.com/release", fetch_result(), clock)
    assert "Example Release" in str(checked["canonical_text"])
    facts = checked["transport_facts"]
    assert isinstance(facts, dict)
    assert facts["peer_ip"] == "93.184.216.34"


def test_validate_fetch_result_rejects_redirect_and_drift() -> None:
    """包 A：重定向/状态/类型/字符集/大小/编码/peer 交叉核验全部失败关闭。"""
    with pytest.raises(ResearchFetchError) as caught:
        validate_fetch_result(
            "https://example.com/release",
            fetch_result(final_locator="https://example.com/other"),
            clock,
        )
    assert caught.value.code == "research_fetch_redirect_forbidden"
    with pytest.raises(ResearchFetchError):
        validate_fetch_result(
            "https://example.com/release", fetch_result(http_status=404), clock
        )
    with pytest.raises(ResearchFetchError):
        validate_fetch_result(
            "https://example.com/release",
            fetch_result(content_type="application/json"),
            clock,
        )
    with pytest.raises(ResearchFetchError):
        validate_fetch_result(
            "https://example.com/release",
            fetch_result(content_type="text/html; charset=latin-1"),
            clock,
        )
    with pytest.raises(ResearchFetchError):
        validate_fetch_result(
            "https://example.com/release",
            fetch_result(body_bytes=b"x" * (1_048_576 + 1)),
            clock,
        )
    with pytest.raises(ResearchFetchError):
        validate_fetch_result(
            "https://example.com/release",
            fetch_result(body_bytes=b"\xff\xfe invalid"),
            clock,
        )


def test_validate_fetch_result_peer_cross_check() -> None:
    """包 A（peer 交叉核验）：peer 私网、解析集含私网、peer 不在解析集全拒。"""
    with pytest.raises(ResearchFetchError) as caught:
        validate_fetch_result(
            "https://example.com/release", fetch_result(peer_ip="10.0.0.1"), clock
        )
    assert caught.value.code == "research_fetch_ip_check_failed"
    with pytest.raises(ResearchFetchError):
        validate_fetch_result(
            "https://example.com/release",
            fetch_result(resolved_ips=["93.184.216.34", "127.0.0.1"]),
            clock,
        )
    with pytest.raises(ResearchFetchError):
        validate_fetch_result(
            "https://example.com/release",
            fetch_result(resolved_ips=["93.184.216.35"]),
            clock,
        )


# -- 生产 transport（fake connection；live 关闭零网络）


def test_live_disabled_fails_closed_with_zero_network() -> None:
    """恒 0 边界（rev5 #83 同族）：--research-live=false 时零 DNS 零连接。"""
    connection = FakeConnection()
    transport = ResearchFetchTransport(
        live_enabled=False,
        resolver=ExplodingResolver(),
        connection_factory=lambda *args: connection,
    )
    with pytest.raises(ResearchFetchError) as caught:
        transport(build_fetch_request("https://example.com/release"))
    assert caught.value.code == "research_live_disabled"
    assert connection.connected is False


def test_transport_fetch_happy_path_via_fake_connection() -> None:
    connection = FakeConnection()
    transport = make_transport(connection)
    result = transport(build_fetch_request("https://example.com/release"))
    assert result["http_status"] == 200
    assert result["peer_ip"] == "93.184.216.34"
    assert connection.requests == [("GET", "/release")]
    assert connection.closed is True
    checked = validate_fetch_result("https://example.com/release", result, clock)
    assert "Example Release" in str(checked["canonical_text"])


def test_transport_rejects_peer_outside_resolved_set() -> None:
    connection = FakeConnection(peer="203.0.113.9")
    transport = make_transport(connection)
    with pytest.raises(ResearchFetchError) as caught:
        transport(build_fetch_request("https://example.com/release"))
    assert caught.value.code == "peer_cross_check_failed"
    assert connection.requests == []


def test_transport_rejects_private_dns_resolution() -> None:
    connection = FakeConnection()
    transport = make_transport(
        connection, resolver=lambda host: frozenset({"10.0.0.1"})
    )
    with pytest.raises(ResearchFetchError) as caught:
        transport(build_fetch_request("https://example.com/release"))
    assert caught.value.code == "research_fetch_unavailable"
    assert connection.connected is False


def test_transport_rejects_redirect_status() -> None:
    connection = FakeConnection(status=302)
    transport = make_transport(connection)
    with pytest.raises(ResearchFetchError) as caught:
        transport(build_fetch_request("https://example.com/release"))
    assert caught.value.code == "research_fetch_redirect_forbidden"


def test_transport_rejects_oversized_body() -> None:
    connection = FakeConnection(body=b"x" * (1_048_576 + 1))
    transport = make_transport(connection)
    with pytest.raises(ResearchFetchError) as caught:
        transport(build_fetch_request("https://example.com/release"))
    assert caught.value.code == "research_fetch_body_too_large"


# -- 查询计划与 DDG 发现（§8 来源发现冻结）


def test_query_plan_targets_follow_claim_type() -> None:
    """§8：发布/许可/硬件→官方；可运行性/性能→社区；风险/失败→独立社区。"""
    official = build_query_plan("某产品", ["release", "license", "hardware"])
    assert [step["source_target"] for step in official] == ["official"]
    community = build_query_plan("某产品", ["runnability", "performance"])
    assert [step["source_target"] for step in community] == ["community"]
    independent = build_query_plan("某产品", ["risk", "failure_modes"])
    assert [step["source_target"] for step in independent] == ["independent"]
    mixed = build_query_plan("某产品", ["release", "risk"])
    assert [step["source_target"] for step in mixed] == ["official", "independent"]
    with pytest.raises(ResearchFetchError):
        build_query_plan("某产品", ["not_a_claim_type"])


DDG_PAGE = b"""
<a class="result__a" href="https://example.com/release">Example Release</a>
<a class="result__a" href="https://github.com/example/project">GitHub project</a>
<a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fdeep">Deep</a>
<a class="result__a" href="http://insecure.example/x">insecure</a>
<a class="result__a" href="https://93.184.216.34/x">ip literal</a>
"""


def test_parse_ddg_candidates_filters_and_unwraps() -> None:
    """包 A：解析只产候选；http/IP 字面量拒绝；DDG 跳转解包；不判真实性。"""
    candidates = parse_ddg_candidates(DDG_PAGE)
    urls = [candidate["url"] for candidate in candidates]
    assert "https://example.com/release" in urls
    assert "https://example.org/deep" in urls
    assert all(url.startswith("https://") for url in urls)
    assert not any("93.184.216.34" in url for url in urls)
    assert all(set(candidate) == {"title", "url"} for candidate in candidates)


def test_ddg_unavailable_is_honest_capability_unavailable() -> None:
    """§8 / 产品验收 #5 对应工程项：DDG 不可用 → capability_unavailable，不伪造。"""

    def exploding(query: str) -> bytes:
        raise AssertionError("network must not be reached when DDG is down")

    def failing(query: str) -> bytes:
        raise ResearchFetchError("search_unavailable")

    plan = build_query_plan("某产品", ["release"])
    outcome = discover_candidates(plan, failing)
    assert outcome["status"] == "capability_unavailable"
    assert outcome["candidates"] == []
    assert outcome["errors"]


def test_ddg_empty_result_is_honest_not_found() -> None:
    """§8：找不到材料 → not_found，诚实停止。"""

    def empty(query: str) -> bytes:
        return b"<html><body>no results</body></html>"

    outcome = discover_candidates(build_query_plan("某产品", ["release"]), empty)
    assert outcome["status"] == "not_found"
    assert outcome["candidates"] == []


def test_explicit_https_link_is_only_a_candidate_and_rejects_unsafe_locators() -> None:
    candidates = explicit_https_candidates(
        "继续核验 https://platform.example.com/docs/local-deploy，忽略 http://127.0.0.1/x",
        "community",
    )
    assert candidates == [
        {
            "title": "platform.example.com",
            "url": "https://platform.example.com/docs/local-deploy",
            "source_target": "community",
        }
    ]
    assert "identity_status" not in candidates[0]


def test_platform_url_not_auto_trusted() -> None:
    """§8 / 产品验收 #4 对应工程项：平台 URL 命中不自动可信/官方。

    DDG 只发现候选 URL；候选本身不携带任何身份/真实性结论，官方性只能来自
    后续 fetch + provenance chain（包 B classify_identity 单测在包 B 测试）。
    """
    outcome = discover_candidates(
        build_query_plan("某产品", ["runnability"]), lambda query: DDG_PAGE
    )
    assert outcome["status"] == "ok"
    candidates = outcome["candidates"]
    assert isinstance(candidates, list)
    github = [c for c in candidates if "github.com" in c["url"]]
    assert github and github[0]["source_target"] == "community"
    assert "identity_status" not in github[0]
    assert "trusted" not in github[0]


@pytest.mark.parametrize("location, expected", [
    ("/canonical", "https://example.com/canonical"),
    ("https://public.example/new", "https://public.example/new"),
    ("https://127.0.0.1/private", None),
    ("http://example.com/plain", None),
    ("https://user:secret@example.com/", None),
    ("https://example.com:8443/", None),
])
def test_redirect_hint_is_candidate_only_without_following(location, expected):
    connection = FakeConnection(status=301)
    response = FakeResponse(301, b"")
    response.getheader = lambda name: location if name == "Location" else None
    connection.getresponse = lambda: response
    hosts = []

    def resolver(host):
        hosts.append(host)
        return public_resolver(host)

    transport = make_transport(connection, resolver=resolver)
    with pytest.raises(ResearchFetchError) as caught:
        transport(build_fetch_request("https://example.com/old"))
    assert caught.value.code == "research_fetch_redirect_forbidden"
    assert caught.value.redirect_candidate == expected
    assert hosts == ["example.com"]
    assert connection.requests == [("GET", "/old")]
    assert connection.closed
