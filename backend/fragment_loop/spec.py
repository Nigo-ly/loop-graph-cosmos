"""Frozen phone-fragment-link-v1 LoopSpec."""

from common.supervisor import LoopSpec

PHONE_FRAGMENT_LINK_V1 = LoopSpec(
    loop_id="phone-fragment-link-v1",
    version="1.0.0",
    goal="把经 nigo 批准的手机链接碎片转化为可核验、可行动、可追溯的结果",
    first_node="intake",
    nodes=(
        "intake",
        "content_acquisition",
        "source_verification",
        "understanding",
        "value_routing",
        "action_design",
        "publication_eval",
        "feedback",
    ),
    max_iterations=8,
    max_seconds=30 * 60,
    token_limit=50_000,
    tool_call_limit=20,
    worker_version="n8n-deepseek-v4+codex-artifact-adapter-v1",
    evaluator_version="codex-independent-source-verifier-v1",
)

AGENTS_CLI_SKILL_MAPPING_V1 = LoopSpec(
    loop_id="agents-cli-skill-mapping-v1",
    version="1.0.0",
    goal="把 Google Agents CLI 七项官方 Skills 映射为可直接复用、需适配和 Google 专属能力",
    first_node="source_lock",
    nodes=(
        "source_lock",
        "skill_inventory",
        "capability_mapping",
        "independent_eval",
        "feedback",
    ),
    max_iterations=5,
    max_seconds=20 * 60,
    token_limit=20_000,
    tool_call_limit=10,
    worker_version="codex-skill-mapper-v1",
    evaluator_version="deterministic-capability-eval-v1",
)

PUMA_STOP_POLICY_EVAL_V1 = LoopSpec(
    loop_id="puma-stop-policy-eval-v1",
    version="1.0.0",
    goal="验证 PUMA 的推理收敛思想能否安全迁移为 Micro Loop Engine 的停止策略",
    first_node="source_lock",
    nodes=(
        "source_lock",
        "claim_matrix",
        "stop_policy_design",
        "independent_eval",
        "feedback",
    ),
    max_iterations=5,
    max_seconds=20 * 60,
    token_limit=20_000,
    tool_call_limit=10,
    worker_version="codex-stop-policy-designer-v1",
    evaluator_version="deterministic-puma-transfer-eval-v1",
)

INTERACTIVE_KNOWLEDGE_PATTERN_V1 = LoopSpec(
    loop_id="interactive-knowledge-pattern-v1",
    version="1.0.0",
    goal="把 AI Cosmos 的产品能力核验并转化为可复用的交互知识系统设计模式",
    first_node="source_lock",
    nodes=(
        "source_lock",
        "capability_model",
        "relevance_mapping",
        "independent_eval",
        "feedback",
    ),
    max_iterations=5,
    max_seconds=20 * 60,
    token_limit=20_000,
    tool_call_limit=10,
    worker_version="codex-product-pattern-analyst-v1",
    evaluator_version="deterministic-interactive-knowledge-eval-v1",
)

HERMES_K27_FRAGMENT_RESEARCH_V1 = LoopSpec(
    loop_id="hermes-k27-fragment-research-v1",
    version="1.0.4",
    goal="验证 Kimi K3 能否把 approved 手机碎片委派给 Kimi K2.7，并由独立模型复核结果",
    first_node="source_lock",
    nodes=(
        "source_lock",
        "k27_worker",
        "independent_eval",
        "feedback",
    ),
    max_iterations=4,
    max_seconds=15 * 60,
    token_limit=150_000,
    tool_call_limit=15,
    worker_version="hermes-k3-k27-leaf-worker-v5-active-token-budget",
    evaluator_version="deepseek-v4-pro-independent-v1",
)

# 手机碎片最小价值链（第一步完全离线合成候选）：独立于 PHONE_FRAGMENT_LINK_V1，
# 只处理用户主动选择的纯文本碎片，两个节点均由离线确定性逻辑与注入的
# 合成模型替身驱动；不修改任何既有 LoopSpec。
PHONE_FRAGMENT_MINIMUM_VALUE_V1 = LoopSpec(
    loop_id="phone-fragment-minimum-value-v1",
    version="1.0.0",
    goal=(
        "把用户主动选择的一条纯文本手机碎片，经本地整理、逐条同意、单次合成草稿"
        "与人工决策，形成可撤销的用户确认整理结果"
    ),
    first_node="local_organize",
    nodes=(
        "local_organize",
        "draft_generation",
    ),
    max_iterations=4,
    max_seconds=10 * 60,
    token_limit=5_000,
    tool_call_limit=2,
    worker_version="synthetic-draft-fixture-v1",
    evaluator_version="deterministic-draft-validator-v1",
)


# 碎片认知链 R1-B：只把 R1-A 已冻结的内容契约接入既有 Supervisor / Checkpoint。
# 本 LoopSpec 只接受注入的合成结果，不含模型、检索、文件发布或私人数据入口。
FRAGMENT_COGNITIVE_SYNTHETIC_R1B = LoopSpec(
    loop_id="fragment-cognitive-synthetic-r1b",
    version="1.0.0",
    goal="离线验证研究路线与直接路线的认知契约、持久恢复和人工草稿决策",
    first_node="cognitive_contract",
    nodes=("cognitive_contract", "human_decision"),
    max_iterations=2,
    max_seconds=60,
    token_limit=1,
    tool_call_limit=1,
    worker_version="synthetic-cognitive-fixture-v1",
    evaluator_version="fragment-cognitive-contract-r1a",
)


# 碎片认知链 R1-N：与合成链共用两个通用节点，但 loop ID、version、worker 与
# evaluator 身份完全独立；只接受调用方显式传入的本地 fragment，不读取文件、
# 环境变量、网络、凭据或生产数据，也不复用合成链的历史记录。
FRAGMENT_COGNITIVE_LOCAL_R1N = LoopSpec(
    loop_id="fragment-cognitive-local-r1n",
    version="1.0.0",
    goal="离线验证本地用户输入碎片的认知契约、持久恢复和人工草稿决策",
    first_node="cognitive_contract",
    nodes=("cognitive_contract", "human_decision"),
    max_iterations=2,
    max_seconds=60,
    token_limit=1,
    tool_call_limit=1,
    worker_version="local-cognitive-fixture-r1n-v1",
    evaluator_version="fragment-cognitive-contract-r1a-local-r1n",
)


# Loop V1 的单一真实本地认知入口。新 nigo-loop 碎片先进入认知域，路线在
# 语义展开后再绑定；入口不得根据“是否含链接”提前猜 research/direct。
# R1-N 和 phone-fragment-link-v1 均保持历史兼容，不迁移、不改写。
FRAGMENT_COGNITIVE_LOCAL_V1 = LoopSpec(
    loop_id="fragment-cognitive-local-v1",
    version="1.0.0",
    goal="把 nigo-loop 碎片直接转化为可核验、可阅读、可人工决定的认知草稿",
    first_node="cognitive_contract",
    nodes=("cognitive_contract", "human_decision"),
    max_iterations=2,
    max_seconds=60,
    token_limit=1,
    tool_call_limit=1,
    worker_version="cognitive-product-v1-route-pending",
    evaluator_version="fragment-cognitive-contract-r1a-local-v1",
)
