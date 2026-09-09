"""Public entry must register fresh captures without an external personal daemon."""
import os
import time
from fragment_loop.cognitive_server import scan_public_vault_intents
from tests.test_raw_subscription_intake import Env


def test_public_scanner_registers_then_proposes_and_is_idempotent(tmp_path):
    env = Env(tmp_path, "请评估此公开工具的适配限制。", intake=False)
    os.utime(env.raw, (time.time() - 5, time.time() - 5))
    first = scan_public_vault_intents(vault_root=env.vault,
        continuation_bridge=env.bridge, intent_service=env.service)
    assert first["proposed"] == 1
    alignment = env.service.list_alignments()[0]
    assert alignment["source_origin"] == "raw_capture"
    second = scan_public_vault_intents(vault_root=env.vault,
        continuation_bridge=env.bridge, intent_service=env.service)
    assert second["proposed"] == 0
    assert len(env.service.list_alignments()) == 1
