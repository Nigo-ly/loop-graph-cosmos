"""Fragment Governed Research v1 包 A：安全发现与抓取 transport。

- HTTPS 安全抓取：仅 https、拒 IP 字面量/localhost/私网（离线 URL 校验复用
  ``cognitive_retrieval.is_public_https_locator``），DNS 解析集全部公网复查 +
  TLS peer 与解析集交叉核验（标准参照 ``graph_runtime/agent_live_pilot.py``
  的价格页抓取器），禁代理（``http.client`` 不读代理环境变量）、禁重定向、
  1 MiB 响应上限、connect 10s / total 15s 超时、内容类型白名单、UTF-8 严格
  解码。transport 自报事实全部由 ``validate_fetch_result`` 复核，任何漂移
  fail-closed。
- DDG Source Discovery Adapter：只发现候选 URL，不判断来源真实性；查询计划按
  claim_type 动态生成来源目标（发布/许可/硬件 → 官方；可运行性/性能 →
  GitHub/Hugging Face/模型社区等可复现材料；风险/失败模式 → 独立社区 /
  issue / discussion）。DDG 不可用返回 ``capability_unavailable``，找不到材料
  返回 ``not_found``，绝不伪造来源。
- ``--research-live`` 默认关闭：``live_enabled=False`` 时生产 transport 在
  任何 DNS/连接动作之前失败关闭，零网络。离线测试注入 fake connection /
  fake transport；任何真实 socket 都必须经由显式注入点。
"""

from __future__ import annotations

import hashlib
import html
import http.client
import ipaddress
import re
import socket
import ssl
import time
import urllib.parse
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, NoReturn, Protocol

from fragment_loop.cognitive_retrieval import is_public_https_locator
from fragment_loop.public_source_fetch import extract_visible_text

RESEARCH_FETCH_PROFILE_VERSION = "fragment-research-fetch-v1"
CONNECT_TIMEOUT_SECONDS = 10
TOTAL_TIMEOUT_SECONDS = 15
MAX_BODY_BYTES = 1_048_576  # 1 MiB
ALLOWED_CONTENT_TYPES = ("text/html", "text/plain")
FETCH_USER_AGENT = "MyLifeLoop/1.0 governed-research-fetch"

SEARCH_HOST = "html.duckduckgo.com"
SEARCH_PATH = "/html/"
SEARCH_TIMEOUT_SECONDS = 20
SEARCH_MAX_BYTES = 2 * 1024 * 1024
MAX_RESULTS_PER_QUERY = 5

# Bing CN 搜索出口（rev14/15）：固定 host/path、零代理、DNS+TLS peer 交叉
# 核验、大小/超时有界；与 DDG 同安全框架，解析只产 title/url 候选。
BING_CN_HOST = "cn.bing.com"
BING_CN_PATH = "/search"
# h2 可带属性、h2 与 a 之间可有空白（常见 Bing 形态），不只匹配精确
# <h2><a；每查询最多 MAX_RESULTS_PER_QUERY 条候选，稳定去重。
_BING_CN_RESULT_LINK = re.compile(
    r"<h2[^>]*>\s*<a\s+[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.S
)

# 公开搜索 provider 闭集与 capability fingerprint（rev14）：名称 + 契约
# 版本形成稳定 fingerprint 并持久化；fingerprint 变化才允许历史
# capability_unavailable run 恢复一次，同一 fingerprint 不重试。
SEARCH_PROVIDER_CONTRACT_VERSION = "public-search-v1"
SEARCH_PROVIDERS = frozenset({"ddg", "bing-cn"})
DDG_SEARCH_FINGERPRINT = f"ddg:{SEARCH_PROVIDER_CONTRACT_VERSION}"
BING_CN_SEARCH_FINGERPRINT = f"bing-cn:{SEARCH_PROVIDER_CONTRACT_VERSION}"


def search_provider_fingerprint(provider: str) -> str:
    """闭集 provider → 稳定 capability fingerprint；未知 provider 失败关闭。"""
    if provider == "ddg":
        return DDG_SEARCH_FINGERPRINT
    if provider == "bing-cn":
        return BING_CN_SEARCH_FINGERPRINT
    _fail("search_provider_unknown")


def public_https_seed_url(text: str) -> str | None:
    """从入口元数据文本提取第一个公开 HTTPS 链接作为 source-bound seed
    candidate（rev14）：去查询参数与 fragment，必须通过既有 public
    HTTPS/SSRF 校验；只作为待核验候选，不信任来源身份、绝不把私人正文
    当查询或外传。无合法链接返回 None。"""
    for raw in re.findall(r"https://[^\s<>\"'，。；：！？、]+", text):
        url = raw.rstrip(".,;:!?)]}，。；：！？）】")
        parsed = urllib.parse.urlsplit(url)
        if not isinstance(parsed.hostname, str) or not parsed.hostname:
            continue
        # 去查询参数与 fragment：seed 只绑定 scheme/host/path。
        seed: str = urllib.parse.urlunsplit(
            (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, "", "")
        )
        if is_public_https_locator(seed):
            return seed
    return None
MAX_QUERY_PLAN_QUERIES = 3
MAX_RESEARCH_ROUNDS = 2

# 来源目标闭集：查询计划只为每个目标生成查询，不写任何固定域名逻辑。
SOURCE_TARGETS = ("official", "community", "independent")
# claim_type → 来源目标（DESIGN §8 产品收口冻结）。
CLAIM_TYPE_SOURCE_TARGET = {
    "release": "official",
    "license": "official",
    "hardware": "official",
    "runnability": "community",
    "compatibility": "community",
    "performance": "community",
    "risk": "independent",
    "failure_modes": "independent",
}
CLAIM_TYPES = frozenset(CLAIM_TYPE_SOURCE_TARGET)

FETCH_REQUEST_KEYS = {
    "locator",
    "connect_timeout_seconds",
    "total_timeout_seconds",
    "max_body_bytes",
    "allow_redirects",
    "allow_proxy",
}
FETCH_RESULT_KEYS = {
    "http_status",
    "content_type",
    "body_bytes",
    "final_locator",
    "peer_ip",
    "resolved_ips",
}

FetchTransport = Callable[[Mapping[str, object]], Mapping[str, object]]
"""一次注入式页面抓取；每个 locator 至多调用一次，零重试、零重定向。"""

SearchTransport = Callable[[str], bytes]
"""一次注入式 DDG 查询；输入查询文本，输出响应字节。"""

Resolver = Callable[[str], frozenset[str]]


class ResearchFetchError(ValueError):
    """发现/抓取失败关闭；code 是稳定的机器可读类别。"""

    def __init__(self, code: str, *, redirect_candidate: str | None = None):
        super().__init__(code)
        self.code = code
        self.redirect_candidate = redirect_candidate


def _fail(code: str) -> NoReturn:
    raise ResearchFetchError(code)


def is_public_ip(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def default_resolver(host: str) -> frozenset[str]:
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as error:
        raise ResearchFetchError("dns_resolution_failed") from error
    return frozenset(str(info[4][0]) for info in infos)


class _ConnectionLike(Protocol):
    sock: Any

    def connect(self, /) -> None: ...

    def request(
        self, method: str, url: str, body: Any = None, headers: Mapping[str, str] = {}
    ) -> None: ...

    def getresponse(self, /) -> Any: ...

    def close(self, /) -> None: ...


ConnectionFactory = Callable[[str, int, float, ssl.SSLContext], _ConnectionLike]


def default_connection_factory(
    host: str, port: int, timeout: float, context: ssl.SSLContext
) -> _ConnectionLike:
    return http.client.HTTPSConnection(host, port, timeout=timeout, context=context)


def _resolve_public(host: str, resolver: Resolver, code: str) -> frozenset[str]:
    """DNS 解析集必须非空且全部公网；任一私网/保留地址即失败关闭。"""
    try:
        addresses = resolver(host)
    except ResearchFetchError:
        raise
    except Exception as error:
        raise ResearchFetchError(code) from error
    if not addresses or any(not is_public_ip(item) for item in addresses):
        _fail(code)
    return addresses


def _verified_connect(
    host: str,
    *,
    resolver: Resolver,
    connection_factory: ConnectionFactory,
    timeout: float,
    code: str,
) -> tuple[_ConnectionLike, frozenset[str]]:
    """连接 + TLS peer 交叉核验：peer 必须属于全公网解析集。"""
    addresses = _resolve_public(host, resolver, code)
    context = ssl.create_default_context()
    connection = connection_factory(host, 443, float(timeout), context)
    try:
        connection.connect()
    except (ssl.SSLError, OSError, http.client.HTTPException) as error:
        connection.close()
        raise ResearchFetchError(code) from error
    sock = connection.sock
    peer = sock.getpeername()[0] if sock is not None else None
    if not isinstance(peer, str) or peer not in addresses:
        connection.close()
        _fail("peer_cross_check_failed")
    return connection, addresses


# ---------------------------------------------------------------------------
# 页面抓取（fetch）
# ---------------------------------------------------------------------------


def _explicit_port(locator: str) -> int | None:
    """显式端口（A4）：URL 写出端口时返回端口号，未写返回 None。"""
    netloc = urllib.parse.urlsplit(locator).netloc
    if netloc.startswith("["):
        # IPv6 字面量本来就被 locator 校验拒绝；这里只做端口形态识别。
        return int(netloc.rsplit(":", 1)[1]) if "]:" in netloc else None
    if ":" not in netloc:
        return None
    try:
        return int(netloc.rsplit(":", 1)[1])
    except ValueError:
        return None


def build_fetch_request(locator: str) -> dict[str, object]:
    """冻结每个 fetch transport 被调用时收到的精确描述符。"""
    if not is_public_https_locator(locator):
        _fail("research_fetch_locator_invalid")
    # A4：显式非 443 端口一律拒绝（transport 固定连接 443，绝不静默漂移）；
    # 显式 :443 与缺省同义，允许。
    port = _explicit_port(locator)
    if port is not None and port != 443:
        _fail("research_fetch_locator_invalid")
    return {
        "locator": locator,
        "connect_timeout_seconds": CONNECT_TIMEOUT_SECONDS,
        "total_timeout_seconds": TOTAL_TIMEOUT_SECONDS,
        "max_body_bytes": MAX_BODY_BYTES,
        "allow_redirects": False,
        "allow_proxy": False,
    }


def _parse_content_type(value: object) -> str:
    if not isinstance(value, str):
        _fail("research_fetch_content_type_invalid")
    parts = value.split(";")
    media_type = parts[0].strip().lower()
    if media_type not in ALLOWED_CONTENT_TYPES:
        _fail("research_fetch_content_type_invalid")
    for parameter in parts[1:]:
        name, separator, raw = parameter.strip().partition("=")
        if separator and name.strip().lower() == "charset":
            if raw.strip().strip('"').lower() != "utf-8":
                _fail("research_fetch_charset_invalid")
    return media_type


def validate_fetch_result(
    locator: str,
    raw: object,
    clock: Callable[[], datetime],
) -> dict[str, object]:
    """复核 transport 自报的每一项事实；任何漂移 fail-closed。"""
    if not isinstance(raw, Mapping) or set(raw) != FETCH_RESULT_KEYS:
        _fail("research_fetch_result_invalid")
    http_status = raw.get("http_status")
    if not isinstance(http_status, int) or isinstance(http_status, bool):
        _fail("research_fetch_result_invalid")
    if http_status != 200:
        _fail("research_fetch_http_status_not_200")
    if raw.get("final_locator") != locator:
        _fail("research_fetch_redirect_forbidden")
    media_type = _parse_content_type(raw.get("content_type"))
    body = raw.get("body_bytes")
    if not isinstance(body, (bytes, bytearray)):
        _fail("research_fetch_body_invalid")
    body_bytes = bytes(body)
    if len(body_bytes) > MAX_BODY_BYTES:
        _fail("research_fetch_body_too_large")
    try:
        decoded = body_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _fail("research_fetch_decode_failed")
    peer_ip = raw.get("peer_ip")
    resolved_ips = raw.get("resolved_ips")
    if (
        not is_public_ip(peer_ip)
        or not isinstance(resolved_ips, list)
        or not resolved_ips
        or any(not is_public_ip(item) for item in resolved_ips)
        or str(peer_ip) not in {str(item) for item in resolved_ips}
    ):
        _fail("research_fetch_ip_check_failed")
    fetched_at = clock()
    if (
        not isinstance(fetched_at, datetime)
        or fetched_at.tzinfo is None
        or fetched_at.tzinfo.utcoffset(fetched_at) is None
    ):
        _fail("research_fetch_clock_invalid")
    canonical_text = extract_visible_text(media_type, decoded)
    if not canonical_text.strip():
        _fail("research_fetch_no_visible_text")
    return {
        "canonical_text": canonical_text,
        "transport_facts": {
            "http_status": http_status,
            "content_type": media_type,
            "final_locator": locator,
            "peer_ip": str(peer_ip),
            "resolved_ips": [str(item) for item in resolved_ips],
            "fetched_at": fetched_at.isoformat(),
            "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
        },
    }


class ResearchFetchTransport:
    """生产 HTTPS 抓取 transport：一次 GET、零重试、禁代理、禁重定向。

    ``live_enabled=False``（默认，``--research-live`` 关闭）时在任何 DNS 或
    连接动作之前失败关闭，零网络。离线测试注入 fake resolver /
    connection_factory；真实 socket 只允许经由这两个显式注入点。
    """

    def __init__(
        self,
        *,
        live_enabled: bool = False,
        resolver: Resolver = default_resolver,
        connection_factory: ConnectionFactory = default_connection_factory,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._live_enabled = live_enabled
        self._resolver = resolver
        self._connection_factory = connection_factory
        self._monotonic = monotonic

    def __call__(self, request: Mapping[str, object]) -> Mapping[str, object]:
        if not self._live_enabled:
            _fail("research_live_disabled")
        if not isinstance(request, Mapping) or set(request) != FETCH_REQUEST_KEYS:
            _fail("research_fetch_request_invalid")
        if request.get("allow_redirects") is not False or request.get("allow_proxy") is not False:
            _fail("research_fetch_request_invalid")
        locator = request.get("locator")
        if not isinstance(locator, str) or not is_public_https_locator(locator):
            _fail("research_fetch_locator_invalid")
        port = _explicit_port(locator)
        if port is not None and port != 443:
            _fail("research_fetch_locator_invalid")
        if (
            request.get("connect_timeout_seconds") != CONNECT_TIMEOUT_SECONDS
            or request.get("total_timeout_seconds") != TOTAL_TIMEOUT_SECONDS
            or request.get("max_body_bytes") != MAX_BODY_BYTES
        ):
            _fail("research_fetch_request_invalid")
        host = urllib.parse.urlsplit(locator).hostname
        if not host:
            _fail("research_fetch_locator_invalid")
        path = urllib.parse.urlsplit(locator).path or "/"
        if urllib.parse.urlsplit(locator).query:
            path = f"{path}?{urllib.parse.urlsplit(locator).query}"
        started = self._monotonic()

        def remaining() -> float:
            return float(TOTAL_TIMEOUT_SECONDS) - (self._monotonic() - started)

        connection, addresses = _verified_connect(
            host,
            resolver=self._resolver,
            connection_factory=self._connection_factory,
            timeout=float(CONNECT_TIMEOUT_SECONDS),
            code="research_fetch_unavailable",
        )
        try:
            sock = connection.sock
            if remaining() <= 0:
                _fail("research_fetch_timeout")
            if sock is not None:
                sock.settimeout(remaining())
            try:
                connection.request(
                    "GET",
                    path,
                    body=b"",
                    headers={
                        "Host": host,
                        "User-Agent": FETCH_USER_AGENT,
                        "Accept": "text/html, text/plain",
                    },
                )
                response = connection.getresponse()
            except (OSError, http.client.HTTPException) as error:
                raise ResearchFetchError("research_fetch_unavailable") from error
            status = int(response.status)
            if status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                candidate = None
                if isinstance(location, str) and len(location) <= 4096:
                    target = urllib.parse.urljoin(locator, location)
                    try:
                        build_fetch_request(target)
                    except (ValueError, TypeError):
                        pass
                    else:
                        candidate = target
                # Expose only a safe candidate. Never follow it here: a later
                # explicit read must repeat the full DNS/TLS/public-address checks.
                raise ResearchFetchError(
                    "research_fetch_redirect_forbidden", redirect_candidate=candidate
                )
            try:
                raw = response.read(MAX_BODY_BYTES + 1)
            except (OSError, http.client.HTTPException) as error:
                raise ResearchFetchError("research_fetch_unavailable") from error
            if len(raw) > MAX_BODY_BYTES:
                _fail("research_fetch_body_too_large")
            if self._monotonic() - started > float(TOTAL_TIMEOUT_SECONDS):
                _fail("research_fetch_timeout")
            sock = connection.sock
            peer = sock.getpeername()[0] if sock is not None else None
            content_type = response.getheader("Content-Type")
            return {
                "http_status": status,
                "content_type": "" if content_type is None else str(content_type),
                "body_bytes": bytes(raw),
                "final_locator": locator,
                "peer_ip": "" if peer is None else str(peer),
                "resolved_ips": sorted(addresses),
            }
        finally:
            connection.close()


# ---------------------------------------------------------------------------
# DDG Source Discovery Adapter（只发现候选 URL，不判真实性）
# ---------------------------------------------------------------------------

_RESULT_LINK = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    flags=re.I | re.S,
)
_TAG = re.compile(r"<[^>]+>")


def clean_text(value: str, limit: int) -> str:
    text = " ".join(html.unescape(value).split())
    return "".join(character for character in text if ord(character) >= 0x20)[:limit]


def parse_ddg_candidates(payload: bytes) -> list[dict[str, str]]:
    """解析 DDG HTML 结果页；URL 必须过 research 标准离线校验（拒 IP 字面量）。

    返回的每条候选只有 title/url 两个字段；它证明的只是「存在这样一个候选
    来源」，不构成任何真实性/官方性判断。
    """
    try:
        page = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ResearchFetchError("search_invalid_encoding") from error
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_url, raw_title in _RESULT_LINK.findall(page):
        url = html.unescape(raw_url)
        parsed = urllib.parse.urlsplit(url)
        if parsed.hostname == "duckduckgo.com":
            target = urllib.parse.parse_qs(parsed.query).get("uddg", [])
            if target:
                url = target[0]
        if not is_public_https_locator(url) or url in seen:
            continue
        title = clean_text(_TAG.sub(" ", raw_title), 160)
        if not title:
            continue
        seen.add(url)
        results.append({"title": title, "url": url})
        if len(results) == MAX_RESULTS_PER_QUERY:
            break
    return results


def explicit_https_candidates(
    research_goal: str, source_target: str
) -> list[dict[str, str]]:
    """Treat user-supplied HTTPS links as discovery candidates, not evidence.

    The page still passes the same DNS/TLS peer checks, fetch limits and
    provenance classification as a DDG-discovered URL.  This is the frozen
    honest recovery path when discovery is unavailable and the user supplies
    an explicit official/repository link; it is not a second search adapter.
    """
    if source_target not in SOURCE_TARGETS:
        _fail("source_target_unknown")
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in re.findall(r"https://[^\s<>\"'，。；：！？、]+", research_goal):
        url = raw.rstrip(".,;:!?)]}，。；：！？）】")
        if url in seen or not is_public_https_locator(url):
            continue
        host = urllib.parse.urlsplit(url).hostname
        if not isinstance(host, str) or not host:
            continue
        seen.add(url)
        candidates.append(
            {"title": clean_text(host, 160), "url": url, "source_target": source_target}
        )
        if len(candidates) == MAX_RESULTS_PER_QUERY:
            break
    return candidates


def build_query_plan(
    research_goal: str,
    claim_types: tuple[str, ...] | list[str],
) -> list[dict[str, str]]:
    """按 claim_type 动态生成来源目标（DESIGN §8 冻结映射，无固定域名逻辑）。

    - 发布/许可/硬件 → 官方文档、官方仓库、官方模型页；
    - 可运行性/兼容性/实际性能 → GitHub、Hugging Face、模型社区与可复现材料；
    - 风险与失败模式 → 独立社区、issue、discussion、复现实验。
    """
    goal = clean_text(research_goal, 160)
    if not goal:
        _fail("research_goal_missing")
    if not claim_types:
        _fail("claim_types_missing")
    targets: list[str] = []
    for claim_type in claim_types:
        target = CLAIM_TYPE_SOURCE_TARGET.get(str(claim_type))
        if target is None:
            _fail("claim_type_unknown")
        if target not in targets:
            targets.append(target)
    query_suffix = {
        "official": "官方 发布 许可证",
        "community": "GitHub Hugging Face 模型社区 可复现",
        "independent": "问题 issue discussion 失败",
    }
    return [
        {"query": f"{goal} {query_suffix[target]}", "source_target": target}
        for target in targets[:MAX_QUERY_PLAN_QUERIES]
    ]


def build_replan_queries(
    research_goal: str,
    gap_claim_types: tuple[str, ...] | list[str],
    *,
    round_no: int,
) -> list[dict[str, str]]:
    """缺口反馈环的确定性改述查询（第二轮起；与首轮后缀不同——系统自主
    换关键词，不是用户任务）。gap_claim_types 必须非空（缺口驱动）。"""
    goal = clean_text(research_goal, 160)
    if not goal:
        _fail("research_goal_missing")
    if not gap_claim_types:
        _fail("claim_types_missing")
    if round_no < 2 or round_no > MAX_RESEARCH_ROUNDS:
        _fail("research_round_invalid")
    suffix_by_target = {
        "official": "文档 说明 指南 下载",
        "community": "教程 示例 使用 经验",
        "independent": "评测 复现 对比 记录",
    }
    return [
        {
            "query": f"{goal} {suffix_by_target[CLAIM_TYPE_SOURCE_TARGET[str(claim_type)]]}",
            "source_target": CLAIM_TYPE_SOURCE_TARGET[str(claim_type)],
        }
        for claim_type in gap_claim_types
    ]


def build_counterexample_queries(
    research_goal: str,
    conflict_claim_types: tuple[str, ...] | list[str],
) -> list[dict[str, str]]:
    """冲突补反证的确定性查询：只针对冲突 claim type，目标反例/问题材料。"""
    goal = clean_text(research_goal, 160)
    if not goal:
        _fail("research_goal_missing")
    if not conflict_claim_types:
        _fail("claim_types_missing")
    plan: list[dict[str, str]] = []
    for claim_type in conflict_claim_types:
        target = CLAIM_TYPE_SOURCE_TARGET.get(str(claim_type))
        if target is None:
            _fail("claim_type_unknown")
        plan.append(
            {"query": f"{goal} 问题 失败 争议 风险", "source_target": target}
        )
    return plan


class DDGSearchTransport:
    """生产 DDG 搜索出口：固定 host/path、TLS、DNS+peer 交叉核验、零代理。

    与 ``ResearchFetchTransport`` 对齐（M5）：``live_enabled=False``（默认，
    ``--research-live`` 关闭）时在任何 DNS 或连接动作之前失败关闭，零网络；
    离线测试注入 fake resolver/connection_factory 或直接注入 fake transport。
    """

    def __init__(
        self,
        *,
        live_enabled: bool = False,
        resolver: Resolver = default_resolver,
        connection_factory: ConnectionFactory = default_connection_factory,
    ) -> None:
        self._live_enabled = live_enabled
        self._resolver = resolver
        self._connection_factory = connection_factory

    def __call__(self, query: str) -> bytes:
        if not self._live_enabled:
            _fail("research_live_disabled")
        body = urllib.parse.urlencode({"q": query, "kl": "cn-zh"}).encode("utf-8")
        connection, addresses = _verified_connect(
            SEARCH_HOST,
            resolver=self._resolver,
            connection_factory=self._connection_factory,
            timeout=float(SEARCH_TIMEOUT_SECONDS),
            code="search_unavailable",
        )
        try:
            sock = connection.sock
            if sock is not None:
                sock.settimeout(float(SEARCH_TIMEOUT_SECONDS))
            try:
                connection.request(
                    "POST",
                    SEARCH_PATH,
                    body=body,
                    headers={
                        "Host": SEARCH_HOST,
                        "Accept": "text/html",
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Content-Length": str(len(body)),
                        "User-Agent": "MyLifeLoop/1.0 public-source-discovery",
                    },
                )
                response = connection.getresponse()
            except (OSError, http.client.HTTPException) as error:
                raise ResearchFetchError("search_unavailable") from error
            status = int(response.status)
            if status in (301, 302, 303, 307, 308):
                _fail("search_redirect_rejected")
            if status != 200:
                _fail("search_unavailable")
            try:
                payload = response.read(SEARCH_MAX_BYTES + 1)
            except (OSError, http.client.HTTPException) as error:
                raise ResearchFetchError("search_unavailable") from error
            if len(payload) > SEARCH_MAX_BYTES:
                _fail("search_response_too_large")
            sock = connection.sock
            peer = sock.getpeername()[0] if sock is not None else None
            if not isinstance(peer, str) or peer not in addresses:
                _fail("peer_cross_check_failed")
            return bytes(payload)
        finally:
            connection.close()


def discover_candidates(
    plan: list[dict[str, str]],
    transport: SearchTransport,
    *,
    parser: Callable[[bytes], list[dict[str, str]]] = parse_ddg_candidates,
) -> dict[str, object]:
    """执行查询计划并汇总候选 URL；失败/无材料时诚实停止。

    返回 ``status ∈ {"ok", "capability_unavailable", "not_found"}``；任何单条
    查询失败都记录进 ``errors``，全部失败即 ``capability_unavailable``，成功
    但零候选即 ``not_found``；绝不伪造来源。``parser`` 是 provider 配对的
    解析器（rev14：DDG 默认兼容，Bing CN 由装配显式配对），单次查询只在
    单一 provider 内执行，绝不隐式多 provider 扩散。
    """
    candidates: list[dict[str, str]] = []
    errors: list[str] = []
    for item in plan:
        try:
            found = parser(transport(item["query"]))
        except Exception as error:
            errors.append(clean_text(str(error), 120) or "公开检索失败")
            continue
        for candidate in found:
            candidates.append({**candidate, "source_target": item["source_target"]})
    unique: dict[str, dict[str, str]] = {}
    for candidate in candidates:
        unique.setdefault(candidate["url"], candidate)
    ordered = [unique[key] for key in sorted(unique)]
    if not ordered and errors:
        return {"status": "capability_unavailable", "candidates": [], "errors": errors}
    if not ordered:
        return {"status": "not_found", "candidates": [], "errors": errors}
    return {"status": "ok", "candidates": ordered, "errors": errors}


def parse_bing_cn_candidates(payload: bytes) -> list[dict[str, str]]:
    """解析 Bing CN HTML 结果页（rev14/15）；只产 title/url 候选，所有
    URL 必须过既有公共 HTTPS 校验（私网候选拒绝），畸形编码失败关闭；
    稳定去重，每查询最多 MAX_RESULTS_PER_QUERY 条。

    候选证明的只是「存在这样一个候选来源」，不构成任何真实性/官方性判断。
    """
    try:
        page = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ResearchFetchError("search_invalid_encoding") from error
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_url, raw_title in _BING_CN_RESULT_LINK.findall(page):
        url = html.unescape(raw_url)
        if not is_public_https_locator(url) or url in seen:
            continue
        title = clean_text(_TAG.sub(" ", raw_title), 160)
        if not title:
            continue
        seen.add(url)
        results.append({"title": title, "url": url})
        if len(results) >= MAX_RESULTS_PER_QUERY:
            break
    return results


class BingCNSearchTransport:
    """生产 Bing CN 搜索出口（rev14）：固定 host/path、TLS、DNS+peer 交叉
    核验、零代理、大小/超时有界。

    与 ``DDGSearchTransport`` 对齐：``live_enabled=False``（默认）时在任何
    DNS 或连接动作之前失败关闭，零网络；redirect 一律拒绝，非 200、超限
    响应、超时均失败关闭且绝不伪造证据。
    """

    def __init__(
        self,
        *,
        live_enabled: bool = False,
        resolver: Resolver = default_resolver,
        connection_factory: ConnectionFactory = default_connection_factory,
    ) -> None:
        self._live_enabled = live_enabled
        self._resolver = resolver
        self._connection_factory = connection_factory

    def __call__(self, query: str) -> bytes:
        if not self._live_enabled:
            _fail("research_live_disabled")
        path = f"{BING_CN_PATH}?{urllib.parse.urlencode({'q': query})}"
        connection, addresses = _verified_connect(
            BING_CN_HOST,
            resolver=self._resolver,
            connection_factory=self._connection_factory,
            timeout=float(SEARCH_TIMEOUT_SECONDS),
            code="search_unavailable",
        )
        try:
            sock = connection.sock
            if sock is not None:
                sock.settimeout(float(SEARCH_TIMEOUT_SECONDS))
            try:
                connection.request(
                    "GET",
                    path,
                    headers={
                        "Host": BING_CN_HOST,
                        "Accept": "text/html",
                        "User-Agent": "MyLifeLoop/1.0 public-source-discovery",
                    },
                )
                response = connection.getresponse()
            except (OSError, http.client.HTTPException) as error:
                raise ResearchFetchError("search_unavailable") from error
            status = int(response.status)
            if status in (301, 302, 303, 307, 308):
                _fail("search_redirect_rejected")
            if status != 200:
                _fail("search_unavailable")
            try:
                payload = response.read(SEARCH_MAX_BYTES + 1)
            except (OSError, http.client.HTTPException) as error:
                raise ResearchFetchError("search_unavailable") from error
            if len(payload) > SEARCH_MAX_BYTES:
                _fail("search_response_too_large")
            # rev15：响应后复核 peer 仍属于初始公网解析集（与 DDG 安全
            # 语义一致，防解析后连接被劫持到其它地址）。
            sock = connection.sock
            peer = sock.getpeername()[0] if sock is not None else None
            if not isinstance(peer, str) or peer not in addresses:
                _fail("peer_cross_check_failed")
            return bytes(payload)
        finally:
            connection.close()


__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "BING_CN_HOST",
    "BING_CN_PATH",
    "BING_CN_SEARCH_FINGERPRINT",
    "BingCNSearchTransport",
    "CLAIM_TYPES",
    "CLAIM_TYPE_SOURCE_TARGET",
    "CONNECT_TIMEOUT_SECONDS",
    "DDGSearchTransport",
    "DDG_SEARCH_FINGERPRINT",
    "FETCH_REQUEST_KEYS",
    "FETCH_RESULT_KEYS",
    "MAX_BODY_BYTES",
    "MAX_RESEARCH_ROUNDS",
    "RESEARCH_FETCH_PROFILE_VERSION",
    "SEARCH_HOST",
    "SEARCH_PROVIDERS",
    "SEARCH_PROVIDER_CONTRACT_VERSION",
    "SOURCE_TARGETS",
    "TOTAL_TIMEOUT_SECONDS",
    "FetchTransport",
    "ResearchFetchError",
    "ResearchFetchTransport",
    "SearchTransport",
    "build_counterexample_queries",
    "build_fetch_request",
    "build_query_plan",
    "build_replan_queries",
    "clean_text",
    "discover_candidates",
    "explicit_https_candidates",
    "is_public_ip",
    "parse_bing_cn_candidates",
    "parse_ddg_candidates",
    "public_https_seed_url",
    "search_provider_fingerprint",
    "validate_fetch_result",
]
