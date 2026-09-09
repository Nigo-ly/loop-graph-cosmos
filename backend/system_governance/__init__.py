"""P4A model and resource governance primitives."""

from system_governance.core import (
    EndpointPolicy,
    EvidencePublisher,
    HarnessBridge,
    HealthSnapshot,
    IntentLedger,
    MacOSTemporaryKeychain,
    ModelConfig,
    ProcessController,
    ResourceGuard,
    ResponsesCompatibleAdapterContract,
    RuntimeModelRegistry,
    SafeNodePauseRequest,
    ScheduleDedupe,
    SystemControlContract,
    SystemControlHTTPServer,
    TempKeychain,
    TransportFacts,
    probe_model_health,
    select_role_pair,
)
from system_governance.gate2 import (
    FROZEN_PROVIDERS,
    P4A_GATE2_INCREMENTAL_CONFIRMATION,
    P4A_GATE2_R7_STAGE_A_CONFIRMATION,
    Gate2CanaryRunner,
    Gate2Provider,
    ProbeBudget,
)

P4A_GATE1_VERSION = "p4a-gate1-2026-07-23.1"
P4A_GATE2_VERSION = "p4a-gate2-2026-07-24.8"
P4A_GATE2_CODEPROXY_VERSION = "p4a-gate2-codeproxy-r1-offline-candidate-r4"
P4A_GATE2_R9_VERSION = "p4a-gate2-r9-revision-11-stage-b-ledger-compatibility-offline-candidate"

__all__ = [
    "P4A_GATE1_VERSION",
    "P4A_GATE2_VERSION",
    "P4A_GATE2_CODEPROXY_VERSION",
    "P4A_GATE2_R9_VERSION",
    "EndpointPolicy",
    "EvidencePublisher",
    "HealthSnapshot",
    "HarnessBridge",
    "IntentLedger",
    "MacOSTemporaryKeychain",
    "ModelConfig",
    "ProcessController",
    "ResourceGuard",
    "ResponsesCompatibleAdapterContract",
    "RuntimeModelRegistry",
    "SafeNodePauseRequest",
    "ScheduleDedupe",
    "SystemControlHTTPServer",
    "SystemControlContract",
    "TempKeychain",
    "TransportFacts",
    "probe_model_health",
    "select_role_pair",
    "FROZEN_PROVIDERS",
    "Gate2CanaryRunner",
    "Gate2Provider",
    "P4A_GATE2_INCREMENTAL_CONFIRMATION",
    "P4A_GATE2_R7_STAGE_A_CONFIRMATION",
    "ProbeBudget",
]
