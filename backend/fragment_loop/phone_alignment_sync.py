"""Signed transport bridge between the phone capture Worker and 5684.

Worker KV is only an eventually-consistent display/command queue.  Alignment
and decision authority remains the existing Checkpoint store owned by 5684.
No fragment body crosses this connector.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import ssl
import subprocess
import threading
import time
from collections.abc import Callable, Mapping

from fragment_loop.continuation_bridge import (
    ContinuationBridgeError,
    FragmentContinuationBridge,
)
from fragment_loop.intent_service import FragmentIntentError, FragmentIntentService

PHONE_SYNC_VERSION = "fragment-phone-alignment-sync-v1"
PHONE_SYNC_HOST = "phone-sync.example.invalid"
PHONE_SYNC_PATH = "/api/alignment-sync"
PHONE_SYNC_KEYCHAIN_SERVICE = "fragment-intent-phone-sync-v1"
PHONE_SYNC_HEADER = "X-Fragment-Alignment-Signature"
PHONE_SYNC_TIMESTAMP_HEADER = "X-Fragment-Alignment-Timestamp"
PHONE_SYNC_INTERVAL_SECONDS = 15
PHONE_SYNC_MAX_BYTES = 256 * 1024

# 手机签名 continuation 条目（Worker 排队契约）：判别字段 + 绑定五元组。
CONTINUATION_REQUEST_KIND = "continuation_request"
CONTINUATION_ENTRY_KEYS = (
    "parent_episode_id",
    "parent_run_id",
    "source_result_digest",
    "goal",
    "continuation_id",
    "requester",
)

CredentialReader = Callable[[], str]
SyncTransport = Callable[[bytes, str, str], Mapping[str, object]]


def read_phone_sync_credential() -> str:
    completed = subprocess.run(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-s",
            PHONE_SYNC_KEYCHAIN_SERVICE,
            "-w",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _signature(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(
        secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256
    ).hexdigest()


def https_phone_sync_transport(
    body: bytes, timestamp: str, signature: str
) -> Mapping[str, object]:
    connection = http.client.HTTPSConnection(
        PHONE_SYNC_HOST, 443, timeout=20, context=ssl.create_default_context()
    )
    try:
        connection.request(
            "POST",
            PHONE_SYNC_PATH,
            body=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                PHONE_SYNC_TIMESTAMP_HEADER: timestamp,
                PHONE_SYNC_HEADER: signature,
            },
        )
        response = connection.getresponse()
        raw = response.read(PHONE_SYNC_MAX_BYTES + 1)
        if response.status != 200 or len(raw) > PHONE_SYNC_MAX_BYTES:
            raise RuntimeError(f"phone_sync_http_{response.status}")
        response_timestamp = response.getheader(PHONE_SYNC_TIMESTAMP_HEADER, "")
        response_signature = response.getheader(PHONE_SYNC_HEADER, "")
        return {
            "body": raw,
            "timestamp": response_timestamp,
            "signature": response_signature,
        }
    finally:
        connection.close()


def _phone_projection(item: Mapping[str, object]) -> dict[str, object]:
    allowed = (
        "alignment_id",
        "fragment_id",
        "title",
        "status",
        "sequence",
        "revision",
        "input_digest",
        "case_id",
        "episode_id",
        "suggested_intents",
        "dynamic_intents",
        "reasoning",
        "plan",
        "expected_result",
        "recommended_route",
        "execution_scope",
        "decision",
        "route",
        "execution",
        "updated_at",
    )
    return {key: item[key] for key in allowed if key in item}


class PhoneAlignmentConnector:
    def __init__(
        self,
        intent_service: FragmentIntentService,
        continuation_bridge: FragmentContinuationBridge,
        *,
        credential_reader: CredentialReader = read_phone_sync_credential,
        transport: SyncTransport = https_phone_sync_transport,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.intent_service = intent_service
        self.continuation_bridge = continuation_bridge
        self.credential_reader = credential_reader
        self.transport = transport
        self.clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._phone_fragments: set[str] = set()

    def sync_once(self) -> dict[str, int | str]:
        secret = self.credential_reader()
        if not secret:
            return {"status": "dormant", "proposed": 0, "decided": 0, "continued": 0}
        alignments = [
            _phone_projection(item)
            for item in self.intent_service.list_alignments()
            if item.get("fragment_id") in self._phone_fragments
        ]
        body = json.dumps(
            {
                "contract_version": "1",
                "sync_version": PHONE_SYNC_VERSION,
                "alignments": alignments,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        timestamp = str(int(self.clock()))
        raw = self.transport(body, timestamp, _signature(secret, timestamp, body))
        response_body = raw.get("body")
        response_timestamp = raw.get("timestamp")
        response_signature = raw.get("signature")
        if (
            not isinstance(response_body, bytes)
            or not isinstance(response_timestamp, str)
            or not isinstance(response_signature, str)
            or abs(int(response_timestamp) - int(self.clock())) > 60
            or not hmac.compare_digest(
                response_signature,
                _signature(secret, response_timestamp, response_body),
            )
        ):
            raise RuntimeError("phone_sync_response_invalid")
        payload = json.loads(response_body)
        if not isinstance(payload, dict) or set(payload) != {"fragments", "actions"}:
            raise RuntimeError("phone_sync_response_invalid")
        fragments = payload["fragments"]
        actions = payload["actions"]
        if not isinstance(fragments, list) or not isinstance(actions, list):
            raise RuntimeError("phone_sync_response_invalid")
        proposed = 0
        self._phone_fragments = {
            str(item["fragment_id"])
            for item in fragments[:100]
            if isinstance(item, dict)
            and isinstance(item.get("fragment_id"), str)
        }
        known = {str(item["fragment_id"]) for item in alignments}
        for item in fragments[:100]:
            if (
                not isinstance(item, dict)
                or set(item) != {"fragment_id", "nigo_loop"}
                or item.get("nigo_loop") is not True
                or not isinstance(item.get("fragment_id"), str)
                or item["fragment_id"] in known
            ):
                continue
            try:
                source = self.continuation_bridge.discover_intent_source(item["fragment_id"])
                self.intent_service.propose(
                    {
                        "fragment_id": item["fragment_id"],
                        "input_digest": source["input_digest"],
                        "requester": "nigo",
                    }
                )
                proposed += 1
            except (ContinuationBridgeError, FragmentIntentError):
                continue
        decided = 0
        continued = 0
        for action in actions[:100]:
            if not isinstance(action, dict):
                continue
            if action.get("kind") == CONTINUATION_REQUEST_KIND:
                if self._consume_continuation(action):
                    continued += 1
                continue
            alignment_id = action.get("alignment_id")
            if not isinstance(alignment_id, str):
                continue
            try:
                self.intent_service.decide(alignment_id, action)
                decided += 1
            except FragmentIntentError:
                continue
        return {
            "status": "ok",
            "proposed": proposed,
            "decided": decided,
            "continued": continued,
        }

    def _consume_continuation(self, action: Mapping[str, object]) -> bool:
        """消费手机签名 continuation 条目：桌面同款 continue_episode 能力。

        绑定逐项核验（episode/run/result_digest 同谱系、continuation_id 逐字
        复算）全部在 ``FragmentIntentService.continue_episode`` 内失败关闭；
        相同请求幂等零新增、不同请求冲突不覆盖。任何漂移/异常只跳过该条目，
        不影响其他条目与同步循环。返回是否创建了新的子 episode（幂等重放
        返回 False）。授权/Receipt/预算/人工决定/幂等键/副作用许可零继承
        由子 episode 的独立 Run/Checkpoint 保证（DESIGN §6）。
        """
        parent_episode_id = action.get("parent_episode_id")
        if not isinstance(parent_episode_id, str) or not parent_episode_id:
            return False
        body = {key: action.get(key) for key in CONTINUATION_ENTRY_KEYS}
        try:
            status, _projection = self.intent_service.continue_episode(
                parent_episode_id, body
            )
        except Exception:
            # 失败关闭：漂移/冲突/绑定缺失一律跳过，绝不中断同步循环。
            return False
        return status == 201

    def start(self) -> bool:
        if not self.credential_reader() or self._thread is not None:
            return False

        def run() -> None:
            while not self._stop.is_set():
                try:
                    self.sync_once()
                except Exception:
                    pass
                self._stop.wait(PHONE_SYNC_INTERVAL_SECONDS)

        self._thread = threading.Thread(
            target=run, name="fragment-phone-alignment-sync", daemon=True
        )
        self._thread.start()
        return True

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


__all__ = [
    "PHONE_SYNC_HEADER",
    "PHONE_SYNC_TIMESTAMP_HEADER",
    "PhoneAlignmentConnector",
    "https_phone_sync_transport",
    "read_phone_sync_credential",
]
