"""Pilot 1: one real fragment over frozen public research materials.

This module keeps the first real-content trial deliberately narrow.  It does
not read Obsidian, environment variables, production Loop data, or arbitrary
files.  Public research is frozen below so the same material can be reviewed
independently before any model call.
"""

from __future__ import annotations

import fcntl
import hashlib
import http.client
import json
import os
import ssl
import tempfile
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

PILOT_FRAGMENT = "针对不同行业进行深入研究，定制部署，垂直专业化的推进业务"

PUBLIC_SOURCES: tuple[dict[str, str], ...] = (
    {
        "source_id": "S1",
        "name": "Stanford HAI 2025 AI Index",
        "source_type": "dataset",
        "published_at": "2025",
        "locator": "https://hai.stanford.edu/ai-index/2025-ai-index-report",
        "excerpt": (
            "78% of organizations reported using AI in 2024, up from 55% the year before."
        ),
        "verification": "Stanford HAI 官方报告页面，核对标题、发布机构与页面正文。",
        "scope": "只说明组织采用 AI 的广度上升，不证明垂直定制业务必然成功。",
    },
    {
        "source_id": "S2",
        "name": "Palantir AIP Bootcamp",
        "source_type": "industry_practice",
        "published_at": "current page checked 2026-07-30",
        "locator": "https://www.palantir.com/platforms/aip/bootcamp",
        "excerpt": "From 0 to use case in 5 days.",
        "verification": "Palantir 官方 AIP 产品页面，核对页面标题与原文。",
        "scope": "证明企业以短周期共创发现用例的官方交付主张，不等于独立效果证明。",
    },
    {
        "source_id": "S3",
        "name": "ServiceNow telecom industry AI agents",
        "source_type": "official_document",
        "published_at": "2025-03-03",
        "locator": (
            "https://newsroom.servicenow.com/press-releases/details/2025/"
            "ServiceNow-introduces-AI-agents-built-for-the-telecom-industry-to-drive-"
            "productivity-across-the-entire-service-lifecycle-03-03-2025-traffic/default.aspx"
        ),
        "excerpt": (
            "introducing vertical-specific agentic AI solutions to drive productivity, "
            "improve customer experiences, and simplify operations"
        ),
        "verification": "ServiceNow 官方 newsroom，核对发布日期、产品对象与原文。",
        "scope": "证明通用平台厂商推出电信垂直方案；效果措辞仍是厂商声明。",
    },
    {
        "source_id": "S4",
        "name": "Scale GenAI Platform documentation",
        "source_type": "official_document",
        "published_at": "current docs checked 2026-07-30",
        "locator": "https://docs.gp.scale.com/docs/introduction",
        "excerpt": (
            "rapidly develop, test and deploy Generative AI applications for custom use cases"
        ),
        "verification": "Scale 官方产品文档，核对平台定位、定制用例与部署说明。",
        "scope": "证明平台加专有数据、定制应用的产品路线存在；不证明客户 ROI。",
    },
    {
        "source_id": "S5",
        "name": "Retrieval-Augmented Generation for Large Language Models: A Survey",
        "source_type": "paper",
        "published_at": "2023-12-18",
        "locator": "https://arxiv.org/abs/2312.10997",
        "excerpt": (
            "allows for continuous knowledge updates and integration of domain-specific information"
        ),
        "verification": "arXiv 论文页，核对题名、作者、日期与摘要。",
        "scope": "支持领域知识接入的技术可行性，不直接支持跨行业商业模式。",
    },
    {
        "source_id": "S6",
        "name": "Chinese industrial LLM accuracy and robustness study",
        "source_type": "paper",
        "published_at": "2024-01-27",
        "locator": "https://arxiv.org/abs/2402.01723",
        "excerpt": "all LLMs scoring less than 0.6",
        "verification": "arXiv 论文页，核对 8 个行业、1,200 道题与主要发现。",
        "scope": "对中国工业场景中的准确性形成反例；不能外推到所有行业与新模型。",
    },
    {
        "source_id": "S7",
        "name": "McKinsey OFSE gen AI adoption survey",
        "source_type": "media_report",
        "published_at": "2026",
        "locator": (
            "https://www.mckinsey.com/industries/oil-and-gas/our-insights/"
            "gen-ai-in-the-ofse-industry-progress-lags-behind-intent"
        ),
        "excerpt": (
            "only 1 percent of respondents said that their company had achieved "
            "significant gen AI scale"
        ),
        "verification": "McKinsey 行业调查页面，核对调查年份、比例与障碍说明。",
        "scope": "提供单一行业规模化困难的独立反例，不能代表全部行业。",
    },
)

_PILOT_VERSION = "fragment-cognitive-real-pilot1-v1"
_STAGE_BUDGETS = {
    "primary": ("glm47", 10_000, 2_500),
    "review": ("deepseek_v4_pro", 10_000, 2_000),
    "final": ("glm47", 10_000, 3_500),
}
_PROVIDER_LIMITS = {"glm47": 3, "deepseek_v4_pro": 2}
_TOTAL_INPUT_LIMIT = 30_000
_TOTAL_OUTPUT_LIMIT = 8_000


class PilotBudgetError(ValueError):
    """The one-off real-content pilot cannot safely consume another call."""


class PilotExecutionError(RuntimeError):
    """A planned call failed without exposing provider diagnostics."""


class ModelCallError(PilotExecutionError):
    """Fixed provider failure category plus the best known send fact."""

    def __init__(self, category: str, request_sent: bool | None):
        super().__init__(category)
        self.category = category
        self.request_sent = request_sent


@dataclass(frozen=True)
class ModelReply:
    model_claim: str
    prompt_tokens: int
    completion_tokens: int
    result: dict[str, object]


ModelAdapter = Callable[[str, int], ModelReply]
CredentialReader = Callable[[str], str]
JsonTransport = Callable[[str, bytes, str], Mapping[str, object]]

_PROVIDER_CONFIG = {
    "glm47": {
        "host": "open.bigmodel.cn",
        "path": "/api/paas/v4/chat/completions",
        "model": "glm-4.7",
        "service": "p4a-gate2-r9-provider-glm47",
    },
    "deepseek_v4_pro": {
        "host": "api.deepseek.com",
        "path": "/v1/chat/completions",
        "model": "deepseek-v4-pro",
        "service": "p4a-gate2-r9-provider-deepseek",
    },
}
_MAX_RESPONSE_BYTES = 1_048_576


def _production_credential_reader(service: str) -> str:
    from system_governance.gate2_r9 import SecurityCommandCredentialReader

    credential = SecurityCommandCredentialReader()(service)
    if not isinstance(credential, str):
        raise PilotExecutionError("credential_unavailable")
    return credential


def https_json_once(
    provider: str, body: bytes, credential: str
) -> Mapping[str, object]:
    """One direct HTTPS call, no proxy, redirect, fallback, or retry."""
    config = _PROVIDER_CONFIG.get(provider)
    if config is None:
        raise PilotExecutionError("provider_not_allowed")
    try:
        connection = http.client.HTTPSConnection(
            config["host"],
            443,
            timeout=60,
            context=ssl.create_default_context(),
        )
    except (OSError, ssl.SSLError):
        raise ModelCallError("connection_setup_failed", False) from None
    try:
        try:
            connection.request(
                "POST",
                config["path"],
                body=body,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {credential}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                },
            )
        except (OSError, ssl.SSLError, http.client.HTTPException):
            raise ModelCallError("request_transport_failed", None) from None
        try:
            response = connection.getresponse()
        except (OSError, ssl.SSLError, http.client.HTTPException):
            raise ModelCallError("response_headers_failed", None) from None
        if response.status != 200:
            raise ModelCallError("http_failure", True)
        try:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except (OSError, ssl.SSLError, http.client.HTTPException):
            raise ModelCallError("response_body_failed", True) from None
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ModelCallError("response_too_large", True)
        try:
            loaded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ModelCallError("response_envelope_invalid", True) from None
        if not isinstance(loaded, dict):
            raise ModelCallError("response_envelope_invalid", True)
        return loaded
    finally:
        connection.close()


class OpenAIJsonModelAdapter:
    """Minimal provider adapter for one JSON chat-completions response."""

    def __init__(
        self,
        provider: str,
        *,
        credential_reader: CredentialReader = _production_credential_reader,
        transport: JsonTransport = https_json_once,
    ) -> None:
        if provider not in _PROVIDER_CONFIG:
            raise PilotExecutionError("provider_not_allowed")
        self._provider = provider
        self._credential_reader = credential_reader
        self._transport = transport

    def __call__(self, prompt: str, max_tokens: int) -> ModelReply:
        if not _nonempty_text(prompt) or not isinstance(max_tokens, int):
            raise PilotExecutionError("request_invalid")
        config = _PROVIDER_CONFIG[self._provider]
        request = {
            "model": config["model"],
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是受证据约束的中文研究助手。只输出一个 JSON 对象；"
                        "不得使用未提供的事实、来源或私人上下文。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "thinking": {"type": "disabled"},
            "stream": False,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        body = json.dumps(
            request, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(body) > 120_000:
            raise PilotExecutionError("request_too_large")
        try:
            credential = self._credential_reader(config["service"]).strip()
        except Exception:
            raise ModelCallError("credential_unavailable", False) from None
        if not credential:
            raise ModelCallError("credential_unavailable", False)
        try:
            response = self._transport(self._provider, body, credential)
        finally:
            credential = ""
        try:
            return self._parse(response, max_tokens)
        except PilotExecutionError as error:
            raise ModelCallError(str(error), True) from None

    def _parse(self, response: Mapping[str, object], max_tokens: int) -> ModelReply:
        config = _PROVIDER_CONFIG[self._provider]
        if response.get("model") != config["model"]:
            raise PilotExecutionError("response_model_mismatch")
        choices = response.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise PilotExecutionError("response_choices_invalid")
        choice = choices[0]
        if not isinstance(choice, dict) or choice.get("finish_reason") != "stop":
            raise PilotExecutionError("response_finish_invalid")
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not _nonempty_text(content):
            raise PilotExecutionError("response_content_missing")
        try:
            result = json.loads(str(content))
        except json.JSONDecodeError:
            raise PilotExecutionError("response_content_invalid") from None
        if not isinstance(result, dict):
            raise PilotExecutionError("response_content_invalid")
        usage = response.get("usage")
        if not isinstance(usage, dict):
            raise PilotExecutionError("response_usage_missing")
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        total_tokens = usage.get("total_tokens")
        if (
            not isinstance(prompt_tokens, int)
            or isinstance(prompt_tokens, bool)
            or prompt_tokens < 0
            or not isinstance(completion_tokens, int)
            or isinstance(completion_tokens, bool)
            or completion_tokens < 0
            or not isinstance(total_tokens, int)
            or isinstance(total_tokens, bool)
            or total_tokens != prompt_tokens + completion_tokens
            or prompt_tokens > 10_000
            or completion_tokens > max_tokens
        ):
            raise PilotExecutionError("response_usage_invalid")
        return ModelReply(
            str(response["model"]),
            prompt_tokens,
            completion_tokens,
            result,
        )


class PilotLedger:
    """Small persistent reservation record for the three-call pilot plan."""

    def __init__(self, path: Path):
        self.path = path
        self._lock_path = path.with_name(f".{path.name}.lock")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _load(self) -> dict[str, object]:
        if not self.path.exists():
            return {"version": _PILOT_VERSION, "reservations": []}
        if self.path.is_symlink() or not self.path.is_file():
            raise PilotBudgetError("ledger_path_invalid")
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PilotBudgetError("ledger_invalid") from error
        if (
            not isinstance(loaded, dict)
            or set(loaded) != {"version", "reservations"}
            or loaded.get("version") != _PILOT_VERSION
            or not isinstance(loaded.get("reservations"), list)
        ):
            raise PilotBudgetError("ledger_invalid")
        return loaded

    def _write(self, state: dict[str, object]) -> None:
        payload = json.dumps(
            state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def reserve(
        self,
        stage: str,
        provider: str,
        input_tokens: int,
        output_tokens: int,
    ) -> str:
        expected = _STAGE_BUDGETS.get(stage)
        if expected is None:
            raise PilotBudgetError("stage_not_allowed")
        if expected != (provider, input_tokens, output_tokens):
            raise PilotBudgetError("stage_budget_mismatch")
        with self._locked():
            state = self._load()
            reservations = state["reservations"]
            assert isinstance(reservations, list)
            if any(
                isinstance(item, dict) and item.get("stage") == stage
                for item in reservations
            ):
                raise PilotBudgetError("stage_already_reserved")
            provider_count = sum(
                1
                for item in reservations
                if isinstance(item, dict) and item.get("provider") == provider
            )
            if provider_count >= _PROVIDER_LIMITS[provider]:
                raise PilotBudgetError("provider_call_limit_reached")
            reserved_input = sum(
                int(item.get("reserved_input_tokens", 0))
                for item in reservations
                if isinstance(item, dict)
            )
            reserved_output = sum(
                int(item.get("reserved_output_tokens", 0))
                for item in reservations
                if isinstance(item, dict)
            )
            if (
                reserved_input + input_tokens > _TOTAL_INPUT_LIMIT
                or reserved_output + output_tokens > _TOTAL_OUTPUT_LIMIT
            ):
                raise PilotBudgetError("total_token_limit_reached")
            reservation_id = f"pilot1-{uuid.uuid4().hex}"
            reservations.append(
                {
                    "reservation_id": reservation_id,
                    "stage": stage,
                    "provider": provider,
                    "reserved_input_tokens": input_tokens,
                    "reserved_output_tokens": output_tokens,
                    "status": "reserved",
                }
            )
            self._write(state)
            return reservation_id

    def complete(
        self,
        reservation_id: str,
        *,
        model_claim: str,
        prompt_tokens: int,
        completion_tokens: int,
        result: dict[str, object],
    ) -> None:
        if (
            not _nonempty_text(model_claim)
            or not isinstance(prompt_tokens, int)
            or isinstance(prompt_tokens, bool)
            or prompt_tokens < 0
            or not isinstance(completion_tokens, int)
            or isinstance(completion_tokens, bool)
            or completion_tokens < 0
        ):
            raise PilotBudgetError("completion_evidence_invalid")
        with self._locked():
            state = self._load()
            reservations = state["reservations"]
            assert isinstance(reservations, list)
            matches = [
                item
                for item in reservations
                if isinstance(item, dict)
                and item.get("reservation_id") == reservation_id
            ]
            if len(matches) != 1:
                raise PilotBudgetError("reservation_not_found")
            reservation = matches[0]
            if reservation.get("status") != "reserved":
                raise PilotBudgetError("reservation_already_completed")
            if prompt_tokens > int(reservation["reserved_input_tokens"]) or (
                completion_tokens > int(reservation["reserved_output_tokens"])
            ):
                raise PilotBudgetError("observed_usage_out_of_bounds")
            stage = str(reservation["stage"])
            provider = str(reservation["provider"])
            expected_model = {
                "glm47": "glm-4.7",
                "deepseek_v4_pro": "deepseek-v4-pro",
            }[provider]
            if model_claim != expected_model:
                raise PilotBudgetError("response_model_mismatch")
            prior = {
                str(item.get("stage")): item.get("result")
                for item in reservations
                if isinstance(item, dict) and item.get("status") == "completed"
            }
            if stage == "primary":
                errors = validate_primary_analysis(result)
            elif stage == "review":
                errors = validate_independent_review(result, prior.get("primary"))
            else:
                errors = validate_final_analysis(
                    result,
                    prior.get("primary"),
                    prior.get("review"),
                )
            if errors:
                raise PilotBudgetError(f"stage_result_invalid:{errors[0]}")
            reservation.update(
                {
                    "status": "completed",
                    "model_claim": model_claim,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "result": result,
                }
            )
            self._write(state)

    def fail(
        self,
        reservation_id: str,
        *,
        error_category: str,
        request_sent: bool | None,
    ) -> None:
        if (
            not _nonempty_text(error_category)
            or len(error_category) > 160
            or request_sent not in (True, False, None)
        ):
            raise PilotBudgetError("failure_evidence_invalid")
        with self._locked():
            state = self._load()
            reservations = state["reservations"]
            assert isinstance(reservations, list)
            matches = [
                item
                for item in reservations
                if isinstance(item, dict)
                and item.get("reservation_id") == reservation_id
            ]
            if len(matches) != 1 or matches[0].get("status") != "reserved":
                raise PilotBudgetError("reservation_not_pending")
            matches[0].update(
                {
                    "status": "failed",
                    "error_category": error_category,
                    "request_sent": request_sent,
                }
            )
            self._write(state)

    def stage_result(self, stage: str) -> dict[str, object] | None:
        with self._locked():
            state = self._load()
        reservations = state["reservations"]
        assert isinstance(reservations, list)
        matches = [
            item
            for item in reservations
            if isinstance(item, dict) and item.get("stage") == stage
        ]
        if len(matches) != 1 or matches[0].get("status") != "completed":
            return None
        result = matches[0].get("result")
        if not isinstance(result, dict):
            raise PilotBudgetError("ledger_invalid")
        return result

    def stage_reserved(self, stage: str) -> bool:
        with self._locked():
            state = self._load()
        reservations = state["reservations"]
        assert isinstance(reservations, list)
        return any(
            isinstance(item, dict) and item.get("stage") == stage
            for item in reservations
        )

    def view(self) -> dict[str, object]:
        with self._locked():
            state = self._load()
        reservations = state["reservations"]
        assert isinstance(reservations, list)
        provider_counts = {
            provider: sum(
                1
                for item in reservations
                if isinstance(item, dict) and item.get("provider") == provider
            )
            for provider in _PROVIDER_LIMITS
        }
        return {
            "version": state["version"],
            "reservations": reservations,
            "reserved_input_tokens": sum(
                int(item.get("reserved_input_tokens", 0))
                for item in reservations
                if isinstance(item, dict)
            ),
            "reserved_output_tokens": sum(
                int(item.get("reserved_output_tokens", 0))
                for item in reservations
                if isinstance(item, dict)
            ),
            "provider_counts": provider_counts,
        }


def build_analysis_prompt() -> str:
    """Return the frozen public-only input for the first analyst call."""
    sources = json.dumps(PUBLIC_SOURCES, ensure_ascii=False, separators=(",", ":"))
    return (
        "这是一次真实公开资料研究 Pilot 1。不得使用模型记忆补充事实；每个外部事实"
        "必须引用下列 source_id。\n"
        f"原始碎片：{PILOT_FRAGMENT}\n"
        "已确认背景：用户把碎片信息视为尚未结构化、价值待发现的原始信息；用户希望"
        "Loop 是可扩展底座，不是金融级合规成品；R1-A 至 R1-K 离线合成认知链已通过。\n"
        "隐私边界：本轮不读取 Obsidian，不得声称使用了知识库、私人笔记或未提供的用户事实。\n"
        f"冻结公开来源：{sources}\n"
        "请严格区分：原文字面信息、结构推断、未知项、来源事实、企业自述、独立反例。"
    )


def _primary_prompt() -> str:
    return build_analysis_prompt() + (
        "\n只输出 JSON 对象，精确字段为：literal_facts[{quote,meaning}]、"
        "structural_inferences[{text,source_quote,basis}]、unknowns[string]、"
        "research_questions[string]、claims[{claim_id,text,source_quote,verdict,"
        "source_ids,basis}]、v1_inventory。verdict 只允许 supported、"
        "partially_supported、contradicted、not_covered；除 not_covered 外每条"
        "claim 必须引用来源。source_quote 必须是原始碎片逐字子串。"
    )


def _review_prompt(primary: dict[str, object]) -> str:
    return (
        build_analysis_prompt()
        + "\n以下是 GLM 的第一版分析："
        + json.dumps(primary, ensure_ascii=False, separators=(",", ":"))
        + "\n你是独立审查者。逐条检查是否扩大原文、混淆厂商自述与独立证据、"
        "忽略反例。只输出 JSON 对象，精确字段为：claim_reviews[{claim_id,"
        "conclusion,reason,source_ids}]、overreach[string]、"
        "missing_counterevidence[string]、recommended_changes[string]。"
        "conclusion 只允许 agree、revise、reject，必须恰好覆盖全部 claim_id。"
    )


def _final_prompt(
    primary: dict[str, object], review: dict[str, object]
) -> str:
    return (
        build_analysis_prompt()
        + "\n第一版分析："
        + json.dumps(primary, ensure_ascii=False, separators=(",", ":"))
        + "\n独立审查："
        + json.dumps(review, ensure_ascii=False, separators=(",", ":"))
        + "\n形成更贴近原文的第二版。只输出 JSON 对象，精确字段为："
        "claim_revisions[{claim_id,final_text,change_reason,source_ids}]、"
        "v2_inventory、original_alignment{preserved,corrected_overreach}、"
        "memory_view{analysis,basis,uncertainty}、"
        "project_view{analysis,basis,uncertainty}、"
        "frontier_view{analysis,source_ids,uncertainty}、"
        "synthesis{judgment,opportunities,risks,next_steps,source_ids}。"
        "不得声称读取 Obsidian；memory 和 project 视角只能使用提示中明确提供的背景。"
    )


def _execute_stage(
    ledger: PilotLedger,
    *,
    stage: str,
    provider: str,
    prompt: str,
    input_limit: int,
    output_limit: int,
    adapter: ModelAdapter,
) -> dict[str, object]:
    completed = ledger.stage_result(stage)
    if completed is not None:
        return completed
    if ledger.stage_reserved(stage):
        raise PilotExecutionError(f"{stage}_incomplete_blocks_retry")
    reservation_id = ledger.reserve(stage, provider, input_limit, output_limit)
    try:
        reply = adapter(prompt, output_limit)
    except ModelCallError as error:
        ledger.fail(
            reservation_id,
            error_category=error.category,
            request_sent=error.request_sent,
        )
        raise PilotExecutionError(f"{stage}_call_failed") from None
    except PilotExecutionError as error:
        ledger.fail(
            reservation_id,
            error_category=str(error),
            request_sent=None,
        )
        raise PilotExecutionError(f"{stage}_call_failed") from None
    except Exception:
        ledger.fail(
            reservation_id,
            error_category="unexpected_adapter_failure",
            request_sent=None,
        )
        raise PilotExecutionError(f"{stage}_call_failed") from None
    try:
        ledger.complete(
            reservation_id,
            model_claim=reply.model_claim,
            prompt_tokens=reply.prompt_tokens,
            completion_tokens=reply.completion_tokens,
            result=reply.result,
        )
    except PilotBudgetError as error:
        ledger.fail(
            reservation_id,
            error_category=str(error),
            request_sent=True,
        )
        raise PilotExecutionError(f"{stage}_result_rejected") from None
    return reply.result


def run_real_pilot(
    ledger_path: Path,
    glm_adapter: ModelAdapter,
    deepseek_adapter: ModelAdapter,
) -> dict[str, object]:
    """Run the fixed three-stage real-content plan, resuming completed stages."""
    ledger = PilotLedger(ledger_path)
    primary = _execute_stage(
        ledger,
        stage="primary",
        provider="glm47",
        prompt=_primary_prompt(),
        input_limit=10_000,
        output_limit=2_500,
        adapter=glm_adapter,
    )
    review = _execute_stage(
        ledger,
        stage="review",
        provider="deepseek_v4_pro",
        prompt=_review_prompt(primary),
        input_limit=10_000,
        output_limit=2_000,
        adapter=deepseek_adapter,
    )
    final = _execute_stage(
        ledger,
        stage="final",
        provider="glm47",
        prompt=_final_prompt(primary, review),
        input_limit=10_000,
        output_limit=3_500,
        adapter=glm_adapter,
    )
    return {
        "fragment_text": PILOT_FRAGMENT,
        "source_count": len(PUBLIC_SOURCES),
        "primary": primary,
        "review": review,
        "final": final,
        "markdown": render_pilot_markdown(primary, review, final),
        "ledger": ledger.view(),
    }


def render_pilot_markdown(
    primary: dict[str, object],
    review: dict[str, object],
    final: dict[str, object],
) -> str:
    """Render the validated pilot into a directly reviewable draft."""
    if validate_primary_analysis(primary):
        raise PilotExecutionError("primary_invalid")
    if validate_independent_review(review, primary):
        raise PilotExecutionError("review_invalid")
    if validate_final_analysis(final, primary, review):
        raise PilotExecutionError("final_invalid")
    lines = [
        "# 碎片信息真实研究 Pilot 1",
        "",
        "## 原始碎片",
        "",
        f"> {PILOT_FRAGMENT}",
        "",
        "## 边界",
        "",
        "- 本轮只使用冻结公开来源。",
        "- 本轮未读取 Obsidian、私人笔记或生产数据。",
        "- 模型输出只是受约束草稿，外部事实以来源卡为准。",
        "",
        "## 第一版信息盘点",
        "",
        str(primary["v1_inventory"]),
        "",
        "### 第一版主张",
        "",
    ]
    claims = primary["claims"]
    assert isinstance(claims, list)
    for claim in claims:
        assert isinstance(claim, dict)
        refs = ", ".join(str(item) for item in claim["source_ids"])
        lines.append(
            f"- **{claim['claim_id']} · {claim['verdict']}**：{claim['text']}"
            f"（来源：{refs or '无'}；依据：{claim['basis']}）"
        )
    lines.extend(["", "## 独立审查", ""])
    reviews = review["claim_reviews"]
    assert isinstance(reviews, list)
    for item in reviews:
        assert isinstance(item, dict)
        lines.append(
            f"- **{item['claim_id']} · {item['conclusion']}**：{item['reason']}"
        )
    lines.extend(["", "## 第二版信息盘点", "", str(final["v2_inventory"]), ""])
    revisions = final["claim_revisions"]
    assert isinstance(revisions, list)
    for item in revisions:
        assert isinstance(item, dict)
        refs = ", ".join(str(ref) for ref in item["source_ids"])
        lines.append(
            f"- **{item['claim_id']}**：{item['final_text']}"
            f"（校准：{item['change_reason']}；来源：{refs}）"
        )
    for title, key in (
        ("现有记忆视角", "memory_view"),
        ("知识库与项目进展视角", "project_view"),
        ("前沿行业／技术／科学视角", "frontier_view"),
    ):
        view = final[key]
        assert isinstance(view, dict)
        lines.extend(["", f"## {title}", "", str(view["analysis"])])
        if "source_ids" in view:
            lines.append(f"\n来源：{', '.join(str(ref) for ref in view['source_ids'])}")
        lines.append(f"\n不确定性：{view['uncertainty']}")
    synthesis = final["synthesis"]
    assert isinstance(synthesis, dict)
    lines.extend(["", "## 综合判断", "", str(synthesis["judgment"]), ""])
    for label, key in (
        ("机会", "opportunities"),
        ("风险", "risks"),
        ("下一步", "next_steps"),
    ):
        lines.append(f"### {label}")
        lines.append("")
        lines.extend(f"- {item}" for item in synthesis[key])
        lines.append("")
    lines.extend(["## 公开来源", ""])
    for source in PUBLIC_SOURCES:
        lines.append(
            f"- **{source['source_id']} · {source['name']}**：{source['locator']}"
            f"（范围：{source['scope']}）"
        )
    return "\n".join(lines).rstrip() + "\n"


def publish_pilot(output_dir: Path, artifact: Mapping[str, object]) -> dict[str, object]:
    """Publish only the public draft, validated results, sources, and call facts."""
    primary = artifact.get("primary")
    review = artifact.get("review")
    final = artifact.get("final")
    markdown = artifact.get("markdown")
    if (
        not isinstance(primary, dict)
        or not isinstance(review, dict)
        or not isinstance(final, dict)
        or validate_primary_analysis(primary)
        or validate_independent_review(review, primary)
        or validate_final_analysis(final, primary, review)
        or not _nonempty_text(markdown)
    ):
        raise PilotExecutionError("artifact_invalid")
    ledger = artifact.get("ledger")
    reservations = ledger.get("reservations") if isinstance(ledger, Mapping) else None
    call_evidence: list[dict[str, object]] = []
    if isinstance(reservations, list):
        for item in reservations:
            if not isinstance(item, Mapping):
                continue
            call_evidence.append(
                {
                    key: item[key]
                    for key in (
                        "stage",
                        "provider",
                        "status",
                        "model_claim",
                        "prompt_tokens",
                        "completion_tokens",
                    )
                    if key in item
                }
            )
    summary = {
        "status": "real_public_research_draft_ready",
        "fragment_text": PILOT_FRAGMENT,
        "obsidian_read": False,
        "source_count": len(PUBLIC_SOURCES),
        "model_calls": call_evidence,
        "primary": primary,
        "review": review,
        "final": final,
    }
    payloads = {
        "pilot.md": str(markdown).encode("utf-8"),
        "sources.json": (
            json.dumps(
                PUBLIC_SOURCES,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8"),
        "summary.json": (
            json.dumps(
                summary,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8"),
    }
    hashes = {
        name: hashlib.sha256(payload).hexdigest() for name, payload in payloads.items()
    }
    hashes_text = "".join(f"{digest}  {name}\n" for name, digest in sorted(hashes.items()))
    payloads["hashes.txt"] = hashes_text.encode("utf-8")
    if output_dir.exists():
        if output_dir.is_symlink() or not output_dir.is_dir():
            raise PilotExecutionError("evidence_path_invalid")
        if set(path.name for path in output_dir.iterdir()) != set(payloads):
            raise PilotExecutionError("evidence_conflict")
        for name, payload in payloads.items():
            path = output_dir / name
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise PilotExecutionError("evidence_conflict")
        status = "already_published"
    else:
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix=f".{output_dir.name}.staging-", dir=output_dir.parent)
        )
        try:
            for name, payload in payloads.items():
                path = staging / name
                path.write_bytes(payload)
                path.chmod(0o600)
            os.replace(staging, output_dir)
        except Exception:
            for path in staging.iterdir():
                path.unlink()
            staging.rmdir()
            raise
        status = "published"
    return {
        "status": status,
        "files": sorted(payloads),
        "hashes": hashes,
    }


_PRIMARY_KEYS = {
    "literal_facts",
    "structural_inferences",
    "unknowns",
    "research_questions",
    "claims",
    "v1_inventory",
}
_VERDICTS = {"supported", "partially_supported", "contradicted", "not_covered"}


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _string_list(value: object, *, allow_empty: bool = True) -> list[str] | None:
    if not isinstance(value, list) or any(not _nonempty_text(item) for item in value):
        return None
    if not allow_empty and not value:
        return None
    return value


def validate_primary_analysis(value: object) -> tuple[str, ...]:
    """Validate the first model's proposal without trusting its prose."""
    if not isinstance(value, dict) or set(value) != _PRIMARY_KEYS:
        return ("primary_shape_invalid",)
    errors: list[str] = []
    literal_facts = value.get("literal_facts")
    if not isinstance(literal_facts, list) or not literal_facts:
        errors.append("literal_facts_invalid")
    else:
        for index, item in enumerate(literal_facts):
            if not isinstance(item, dict) or set(item) != {"quote", "meaning"}:
                errors.append(f"literal_fact_invalid:{index}")
                continue
            quote = item.get("quote")
            if not _nonempty_text(quote) or str(quote) not in PILOT_FRAGMENT:
                errors.append(f"literal_quote_unbound:{index}")
            if not _nonempty_text(item.get("meaning")):
                errors.append(f"literal_meaning_invalid:{index}")
    inferences = value.get("structural_inferences")
    if not isinstance(inferences, list):
        errors.append("structural_inferences_invalid")
    else:
        for index, item in enumerate(inferences):
            if not isinstance(item, dict) or set(item) != {
                "text",
                "source_quote",
                "basis",
            }:
                errors.append(f"inference_invalid:{index}")
                continue
            quote = item.get("source_quote")
            if not _nonempty_text(quote) or str(quote) not in PILOT_FRAGMENT:
                errors.append(f"inference_quote_unbound:{index}")
            if not _nonempty_text(item.get("text")) or not _nonempty_text(
                item.get("basis")
            ):
                errors.append(f"inference_text_invalid:{index}")
    for field in ("unknowns", "research_questions"):
        if _string_list(value.get(field), allow_empty=field == "unknowns") is None:
            errors.append(f"{field}_invalid")
    claims = value.get("claims")
    source_ids = {source["source_id"] for source in PUBLIC_SOURCES}
    seen_claims: set[str] = set()
    if not isinstance(claims, list) or not claims:
        errors.append("claims_invalid")
    else:
        for index, claim in enumerate(claims):
            required = {
                "claim_id",
                "text",
                "source_quote",
                "verdict",
                "source_ids",
                "basis",
            }
            if not isinstance(claim, dict) or set(claim) != required:
                errors.append(f"claim_invalid:{index}")
                continue
            claim_id = claim.get("claim_id")
            if not _nonempty_text(claim_id) or str(claim_id) in seen_claims:
                errors.append(f"claim_id_invalid:{index}")
            else:
                seen_claims.add(str(claim_id))
            quote = claim.get("source_quote")
            if not _nonempty_text(quote) or str(quote) not in PILOT_FRAGMENT:
                errors.append(f"claim_quote_unbound:{index}")
            if not _nonempty_text(claim.get("text")) or not _nonempty_text(
                claim.get("basis")
            ):
                errors.append(f"claim_text_invalid:{index}")
            verdict = claim.get("verdict")
            if verdict not in _VERDICTS:
                errors.append(f"claim_verdict_invalid:{index}")
            refs = _string_list(claim.get("source_ids"))
            if refs is None:
                errors.append(f"claim_sources_invalid:{index}")
                continue
            if verdict == "not_covered" and refs:
                errors.append(f"not_covered_has_sources:{index}")
            if verdict != "not_covered" and not refs:
                errors.append(f"claim_sources_missing:{index}")
            for source_id in refs:
                if source_id not in source_ids:
                    errors.append(f"unknown_source:{source_id}")
    if not _nonempty_text(value.get("v1_inventory")):
        errors.append("v1_inventory_invalid")
    return tuple(errors)


def validate_independent_review(
    value: object,
    primary: object,
) -> tuple[str, ...]:
    """Require the evaluator to review exactly the analyst's claim set."""
    expected_keys = {
        "claim_reviews",
        "overreach",
        "missing_counterevidence",
        "recommended_changes",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        return ("review_shape_invalid",)
    if validate_primary_analysis(primary) or not isinstance(primary, dict):
        return ("primary_invalid",)
    errors: list[str] = []
    for field in ("overreach", "missing_counterevidence", "recommended_changes"):
        if _string_list(value.get(field)) is None:
            errors.append(f"{field}_invalid")
    source_ids = {source["source_id"] for source in PUBLIC_SOURCES}
    claims = primary.get("claims")
    assert isinstance(claims, list)
    expected_claims = {
        str(claim["claim_id"])
        for claim in claims
        if isinstance(claim, dict) and _nonempty_text(claim.get("claim_id"))
    }
    reviews = value.get("claim_reviews")
    reviewed_claims: list[str] = []
    if not isinstance(reviews, list) or not reviews:
        errors.append("claim_reviews_invalid")
    else:
        for index, review in enumerate(reviews):
            if not isinstance(review, dict) or set(review) != {
                "claim_id",
                "conclusion",
                "reason",
                "source_ids",
            }:
                errors.append(f"claim_review_invalid:{index}")
                continue
            claim_id = review.get("claim_id")
            if _nonempty_text(claim_id):
                reviewed_claims.append(str(claim_id))
            else:
                errors.append(f"review_claim_id_invalid:{index}")
            if review.get("conclusion") not in {"agree", "revise", "reject"}:
                errors.append(f"review_conclusion_invalid:{index}")
            if not _nonempty_text(review.get("reason")):
                errors.append(f"review_reason_invalid:{index}")
            refs = _string_list(review.get("source_ids"))
            if refs is None:
                errors.append(f"review_sources_invalid:{index}")
                continue
            for source_id in refs:
                if source_id not in source_ids:
                    errors.append(f"unknown_source:{source_id}")
    if set(reviewed_claims) != expected_claims or len(reviewed_claims) != len(
        expected_claims
    ):
        errors.append("review_claim_set_mismatch")
    return tuple(errors)


def validate_final_analysis(
    value: object,
    primary: object,
    review: object,
) -> tuple[str, ...]:
    """Validate the finalizer's user-facing result and provenance limits."""
    expected_keys = {
        "claim_revisions",
        "v2_inventory",
        "original_alignment",
        "memory_view",
        "project_view",
        "frontier_view",
        "synthesis",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        return ("final_shape_invalid",)
    if validate_primary_analysis(primary) or not isinstance(primary, dict):
        return ("primary_invalid",)
    if validate_independent_review(review, primary):
        return ("review_invalid",)
    errors: list[str] = []
    if not _nonempty_text(value.get("v2_inventory")):
        errors.append("v2_inventory_invalid")
    source_ids = {source["source_id"] for source in PUBLIC_SOURCES}
    claims = primary.get("claims")
    assert isinstance(claims, list)
    expected_claims = {
        str(claim["claim_id"])
        for claim in claims
        if isinstance(claim, dict) and _nonempty_text(claim.get("claim_id"))
    }
    revisions = value.get("claim_revisions")
    revised_claims: list[str] = []
    if not isinstance(revisions, list) or not revisions:
        errors.append("claim_revisions_invalid")
    else:
        for index, revision in enumerate(revisions):
            if not isinstance(revision, dict) or set(revision) != {
                "claim_id",
                "final_text",
                "change_reason",
                "source_ids",
            }:
                errors.append(f"claim_revision_invalid:{index}")
                continue
            claim_id = revision.get("claim_id")
            if _nonempty_text(claim_id):
                revised_claims.append(str(claim_id))
            if not _nonempty_text(revision.get("final_text")) or not _nonempty_text(
                revision.get("change_reason")
            ):
                errors.append(f"claim_revision_text_invalid:{index}")
            refs = _string_list(revision.get("source_ids"), allow_empty=False)
            if refs is None:
                errors.append(f"claim_revision_sources_invalid:{index}")
            else:
                errors.extend(
                    f"unknown_source:{source_id}"
                    for source_id in refs
                    if source_id not in source_ids
                )
    if set(revised_claims) != expected_claims or len(revised_claims) != len(
        expected_claims
    ):
        errors.append("final_claim_set_mismatch")
    alignment = value.get("original_alignment")
    if not isinstance(alignment, dict) or set(alignment) != {
        "preserved",
        "corrected_overreach",
    }:
        errors.append("original_alignment_invalid")
    else:
        if _string_list(alignment.get("preserved"), allow_empty=False) is None:
            errors.append("preserved_invalid")
        if _string_list(alignment.get("corrected_overreach")) is None:
            errors.append("corrected_overreach_invalid")
    for field in ("memory_view", "project_view"):
        view = value.get(field)
        if not isinstance(view, dict) or set(view) != {
            "analysis",
            "basis",
            "uncertainty",
        }:
            errors.append(f"{field}_invalid")
            continue
        if any(not _nonempty_text(view.get(key)) for key in view):
            errors.append(f"{field}_text_invalid")
    frontier = value.get("frontier_view")
    if not isinstance(frontier, dict) or set(frontier) != {
        "analysis",
        "source_ids",
        "uncertainty",
    }:
        errors.append("frontier_view_invalid")
    else:
        if not _nonempty_text(frontier.get("analysis")) or not _nonempty_text(
            frontier.get("uncertainty")
        ):
            errors.append("frontier_view_text_invalid")
        refs = _string_list(frontier.get("source_ids"), allow_empty=False)
        if refs is None:
            errors.append("frontier_sources_invalid")
        else:
            errors.extend(
                f"unknown_source:{source_id}"
                for source_id in refs
                if source_id not in source_ids
            )
    synthesis = value.get("synthesis")
    if not isinstance(synthesis, dict) or set(synthesis) != {
        "judgment",
        "opportunities",
        "risks",
        "next_steps",
        "source_ids",
    }:
        errors.append("synthesis_invalid")
    else:
        if not _nonempty_text(synthesis.get("judgment")):
            errors.append("synthesis_judgment_invalid")
        for field in ("opportunities", "risks", "next_steps", "source_ids"):
            values = _string_list(synthesis.get(field), allow_empty=False)
            if values is None:
                errors.append(f"synthesis_{field}_invalid")
            elif field == "source_ids":
                errors.extend(
                    f"unknown_source:{source_id}"
                    for source_id in values
                    if source_id not in source_ids
                )
    serialized = json.dumps(value, ensure_ascii=False)
    forbidden_claims = (
        "已扫描 Obsidian",
        "已读取 Obsidian",
        "来自 Obsidian",
        "根据私人笔记",
    )
    if any(claim in serialized for claim in forbidden_claims):
        errors.append("private_context_claimed")
    return tuple(errors)
