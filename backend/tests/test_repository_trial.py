from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import sys
import tarfile
import types
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from fragment_loop import repository_trial as trial
from fragment_loop.knowledge_library import KnowledgeLibraryError
from fragment_loop.repository_trial import _execute, extract_archive, validate_command
from fragment_loop.subscription_research import SubscriptionResearch

from .test_knowledge_library import library, save
from .test_knowledge_library import result as research_result


def test_archive_path_escape_and_symlink_rejected(tmp_path: Path) -> None:
    for name in ("../outside", "/absolute", "root/../../outside", "root\\outside"):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr(name, "bad")
        with pytest.raises(ValueError, match="archive_path"):
            extract_archive(data.getvalue(), tmp_path)
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        link = zipfile.ZipInfo("root/link")
        link.external_attr = 0o120777 << 16
        archive.writestr(link, "/etc/passwd")
    with pytest.raises(ValueError, match="symlink"):
        extract_archive(data.getvalue(), tmp_path)


def test_outside_script_and_unbounded_fixture_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="fixture_invalid"):
        validate_command(["node", "-e", "x" * 4001], tmp_path)
    with pytest.raises(ValueError, match="command_path"):
        validate_command(["node", "../outside.js"], tmp_path)


@pytest.mark.skipif(
    not shutil.which("sandbox-exec") or not shutil.which("node"),
    reason="native sandbox/node unavailable",
)
def test_native_trial_can_write_own_workspace_but_cannot_read_outside(tmp_path: Path) -> None:
    work = (tmp_path / "isolated").resolve()
    work.mkdir()
    sentinel = tmp_path / "private-sentinel.txt"
    sentinel.write_text("private")
    script = work / "trial.js"
    script.write_text(
        "const fs=require('fs');fs.writeFileSync('own.txt','ok');"
        "try{fs.readFileSync(" + repr(str(sentinel)) + ");process.exit(9)}"
        "catch(e){console.log('outside denied')}"
    )
    command = validate_command(["node", "trial.js"], work)
    result = _execute(command, work, work, timeout=5)
    assert result["exit_code"] == 0, result["output"]
    assert "outside denied" in result["output"]
    assert (work / "own.txt").read_text() == "ok"
    assert result["network_enabled"] is False


def test_python_dynamic_and_direct_url_dependencies_rejected(tmp_path: Path) -> None:
    for content in (
        '[project]\ndynamic=["dependencies"]',
        '[project]\ndependencies=["bad @ https://evil.example/pkg.whl"]',
    ):
        (tmp_path / "pyproject.toml").write_text(content)
        with pytest.raises(ValueError, match="unsupported|forbidden"):
            trial._python_dependencies(tmp_path, tmp_path, "/python")


def test_pip_send_guard_rejects_redirect_or_transitive_foreign_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = []

    class Session:
        def send(self, request: Any, **kwargs: Any) -> str:
            assert kwargs.get("proxies") == {}
            sent.append(request.url)
            return "ok"

    session_module = types.ModuleType("pip._internal.network.session")
    session_module.PipSession = Session  # type: ignore[attr-defined]
    main_module = types.ModuleType("pip._internal.cli.main")
    main_module.main = lambda argv: 0  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, session_module.__name__, session_module)
    monkeypatch.setitem(sys.modules, main_module.__name__, main_module)
    monkeypatch.setattr("socket.getaddrinfo", lambda *args: [(0, 0, 0, "", ("1.1.1.1", 443))])
    namespace: dict[str, Any] = {}
    with pytest.raises(SystemExit):
        exec(trial._PIP_GUARD, namespace)
    for url in (
        "https://evil.example/package.whl",
        "file:///etc/passwd",
        "https://user:password@pypi.org/pkg",
        "http://pypi.org/pkg",
    ):
        with pytest.raises(RuntimeError, match="source_forbidden"):
            Session().send(types.SimpleNamespace(url=url))
    assert sent == []
    assert (
        Session().send(types.SimpleNamespace(url="https://files.pythonhosted.org/pkg.whl")) == "ok"
    )


def _npm_fixture(
    work: Path, *, source: str = "https://registry.npmjs.org/leaf/-/leaf-1.0.0.tgz"
) -> tuple[Path, bytes]:
    root = work / "repository"
    root.mkdir()
    package = {
        "name": "leaf",
        "version": "1.0.0",
        "main": "index.js",
        "scripts": {"postinstall": "node -e \"require('fs').writeFileSync('executed', 'bad')\""},
    }
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        for path, text in {
            "package/package.json": json.dumps(package),
            "package/index.js": "module.exports='leaf works'",
        }.items():
            info = tarfile.TarInfo(path)
            payload = text.encode()
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    content = data.getvalue()
    manifest = {"name": "fixture", "version": "1.0.0", "dependencies": {"leaf": "1.0.0"}}
    (root / "package.json").write_text(json.dumps(manifest))
    (root / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "fixture",
                "version": "1.0.0",
                "lockfileVersion": 3,
                "packages": {
                    "": manifest,
                    "node_modules/leaf": {
                        "version": "1.0.0",
                        "resolved": source,
                        "integrity": "sha512-"
                        + base64.b64encode(hashlib.sha512(content).digest()).decode(),
                    },
                },
            }
        )
    )
    return root, content


def test_npm_foreign_source_rejected_before_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _npm_fixture(tmp_path, source="https://evil.example/leaf.tgz")
    calls = []
    monkeypatch.setattr(trial, "_download", lambda *args: calls.append(args))
    with pytest.raises(ValueError, match="source_forbidden"):
        trial._node_dependencies(root, tmp_path)
    assert calls == []


@pytest.mark.skipif(
    not shutil.which("sandbox-exec") or not shutil.which("npm"), reason="native npm unavailable"
)
def test_offline_npm_install_ignores_scripts_and_runs_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, content = _npm_fixture(tmp_path)
    monkeypatch.setattr(trial, "_download", lambda *args: content)
    prepared = trial._node_dependencies(root, tmp_path.resolve())
    assert prepared["status"] == "ready", prepared
    assert not list(tmp_path.rglob("executed"))
    script = root / "probe.js"
    script.write_text("console.log(require('leaf'))")
    result = _execute(validate_command(["node", "probe.js"], root), root, tmp_path.resolve(), 5)
    assert result["exit_code"] == 0 and "leaf works" in result["output"]


@pytest.mark.skipif(
    not shutil.which("sandbox-exec") or not shutil.which("node"), reason="native node unavailable"
)
def test_trial_denies_network_fork_and_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-sentinel")
    script = tmp_path / "probe.js"
    script.write_text(
        "const cp=require('child_process');"
        "if(process.env.DEEPSEEK_API_KEY)process.exit(9);"
        "if(!cp.spawnSync('/usr/bin/true').error)process.exit(8);"
        "const s=require('net').connect(443,'1.1.1.1');"
        "s.on('connect',()=>process.exit(7));s.on('error',()=>console.log('denied'));"
    )
    result = _execute(
        validate_command(["node", "probe.js"], tmp_path), tmp_path, tmp_path.resolve(), 5
    )
    assert result["exit_code"] == 0 and "denied" in result["output"]


@pytest.mark.skipif(
    not shutil.which("sandbox-exec") or not shutil.which("node"), reason="native node unavailable"
)
def test_closed_output_does_not_escape_deadline(tmp_path: Path) -> None:
    script = tmp_path / "probe.js"
    script.write_text(
        "const fs=require('fs');fs.closeSync(1);fs.closeSync(2);setInterval(()=>{},1000)"
    )
    result = _execute(
        validate_command(["node", "probe.js"], tmp_path), tmp_path, tmp_path.resolve(), 0.5
    )
    assert result["status"] == "timeout"


@pytest.mark.skipif(
    not shutil.which("sandbox-exec") or not shutil.which("node"), reason="native node unavailable"
)
def test_output_limit_stops_process(tmp_path: Path) -> None:
    script = tmp_path / "probe.js"
    script.write_text("process.stdout.write('x'.repeat(1000000));setInterval(()=>{},1000)")
    result = _execute(
        validate_command(["node", "probe.js"], tmp_path), tmp_path, tmp_path.resolve(), 5
    )
    assert result["status"] == "output_limit" and len(result["output"]) == trial.MAX_OUTPUT_BYTES


def test_work_size_limit_counts_many_small_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(trial, "MAX_WORK_BYTES", 20)
    for index in range(3):
        (tmp_path / str(index)).write_bytes(b"x" * 8)
    with pytest.raises(ValueError, match="disk_limit"):
        trial._work_size(tmp_path)


@pytest.mark.parametrize("timeout_seconds, read_seconds", [(30, 16), (120, 61)])
def test_slow_download_has_total_deadline(
    monkeypatch: pytest.MonkeyPatch, timeout_seconds: int, read_seconds: int
) -> None:
    tick = [0.0]
    reads = []

    class Response:
        status = 200

        def read1(self, size: int) -> bytes:
            tick[0] += read_seconds
            reads.append(size)
            return b"x"

    timeouts = []

    class Socket:
        def settimeout(self, timeout: float) -> None:
            timeouts.append(timeout)

    class Connection:
        sock = Socket()
        closed = False

        def request(self, *args: Any, **kwargs: Any) -> None:
            pass

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(trial, "_verified_connect", lambda *args, **kwargs: (connection, set()))
    monkeypatch.setattr("fragment_loop.repository_trial.time.monotonic", lambda: tick[0])
    with pytest.raises(ValueError, match="download_timeout"):
        trial._download("api.github.com", "/test", 100, timeout_seconds=timeout_seconds)
    assert len(reads) == 2 and connection.closed
    assert timeouts == [min(30, timeout_seconds), min(30, timeout_seconds - read_seconds)]


def test_dependency_download_failure_never_installs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies=["leaf==1.0"]')
    calls = []

    def execute(command: list[str], *args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append((command, kwargs))
        return {"status": "completed", "exit_code": 1, "output": "no wheel available"}

    monkeypatch.setattr(trial, "_execute", execute)
    result = trial._python_dependencies(tmp_path, tmp_path, "/python")
    assert result["status"] == "unavailable" and len(calls) == 1
    assert "--only-binary=:all:" in calls[0][0] and "install" not in calls[0][0]
    assert calls[0][1]["network_hosts"] == ("pypi.org", "files.pythonhosted.org")


def test_invalid_command_rejected_before_network(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(trial, "_download", lambda *args: calls.append(args))
    for argv in (["sh", "x"], ["node", "--require", "x"], ["node", "../x"],
                 ["python3", "-c", "print(1)", "extra"], ["node", "-e", ""]):
        with pytest.raises(ValueError, match="repository_"):
            trial.run_repository_trial("https://github.com/example/repository", argv)
    assert calls == []


@pytest.mark.skipif(
    not shutil.which("sandbox-exec") or not shutil.which("node"), reason="native node unavailable"
)
def test_generated_fixture_has_same_denied_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "trial"
    work.mkdir()
    sentinel = tmp_path / "private.txt"
    sentinel.write_text("private")
    monkeypatch.setenv("SUBSCRIPTION_SECRET_SENTINEL", "private")
    code = (
        "const fs=require('fs'),cp=require('child_process');"
        "if(process.env.SUBSCRIPTION_SECRET_SENTINEL)process.exit(9);"
        "try{fs.readFileSync(" + repr(str(sentinel)) + ");process.exit(8)}catch(e){};"
        "if(!cp.spawnSync('/usr/bin/true').error)process.exit(7);"
        "const s=require('net').connect(443,'1.1.1.1');"
        "s.on('connect',()=>process.exit(6));s.on('error',()=>console.log('isolated fixture'));"
    )
    command = validate_command(["node", "-e", code], work)
    assert command[1].startswith(".loop-trial-") and command[1].endswith(".cjs")
    result = _execute(command, work, work.resolve(), 5)
    assert result["exit_code"] == 0 and "isolated fixture" in result["output"]
    assert result["network_enabled"] is False


@pytest.mark.parametrize("repository_file", [False, True])
def test_repository_and_pinned_zip_keep_source_and_execution_scope(
    monkeypatch: pytest.MonkeyPatch, repository_file: bool
) -> None:
    revision = "a" * 40
    repository = "https://github.com/example/repository"
    target = f"{repository}/blob/{revision}/bundles/tool.zip" if repository_file else repository
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("pack/probe.js", "console.log('public fixture')")
    calls = []

    def download(
        host: str, path: str, limit: int, *, timeout_seconds: float = 30,
        download_attempts: list[dict[str, Any]] | None = None,
    ) -> bytes:
        calls.append((host, path, limit, timeout_seconds))
        if download_attempts is not None:
            download_attempts.append({"attempt": 1, "source_url": f"https://{host}{path}"})
        return (json.dumps({"sha": revision}).encode()
                if host == "api.github.com" else data.getvalue())

    def execute(command: list[str], root: Path, work: Path, **kwargs: Any) -> dict[str, Any]:
        assert root.parent == work and root.name == "pack"
        assert (root / "probe.js").read_text() == "console.log('public fixture')"
        return {"status": "completed", "exit_code": 0, "output": "public fixture"}

    monkeypatch.setattr(trial, "_download", download)
    monkeypatch.setattr(trial.shutil, "which", lambda name: "/usr/bin/node")
    monkeypatch.setattr(trial, "_execute", execute)
    result = trial.run_repository_trial(target, ["node", "probe.js"])
    host = "raw.githubusercontent.com" if repository_file else "codeload.github.com"
    archive_path = (
        f"/example/repository/{revision}/bundles/tool.zip"
        if repository_file else f"/example/repository/zip/{revision}"
    )
    assert calls[-1] == (host, archive_path, trial.MAX_ARCHIVE_BYTES, 120)
    assert len(calls) == (1 if repository_file else 2)
    assert result["repository"] == repository and result["revision"] == revision
    assert result["archive_source_url"] == f"https://{host}{archive_path}"
    assert result["archive_scope"] == ("repository_file" if repository_file else "repository")
    assert result["archive_sha256"] == hashlib.sha256(data.getvalue()).hexdigest()
    assert result["archive_download_attempts"] == [
        {"attempt": 1, "source_url": f"https://{host}{archive_path}"}
    ]
    assert result["script_executed"] is True and result["exit_code"] == 0


@pytest.mark.parametrize("suffix", [
    "blob/main/tool.zip", "blob/" + "a" * 39 + "/tool.zip",
    "blob/" + "a" * 41 + "/tool.zip", "blob/" + "g" * 40 + "/tool.zip",
    "blob/" + "a" * 40 + "/../tool.zip", "blob/" + "a" * 40 + "/a/./tool.zip",
    "blob/" + "a" * 40 + "/a//tool.zip", "blob/" + "a" * 40 + "//tool.zip",
    "blob/" + "a" * 40 + "/%2e%2e/tool.zip", "blob/" + "a" * 40 + "/tool.tar.gz",
    "blob/" + "a" * 40 + "/tool.zip?download=1", "blob/" + "a" * 40 + "/tool.zip#main",
])
def test_unpinned_or_unsafe_repository_file_rejected_before_download(
    monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    monkeypatch.setattr(trial, "_download", lambda *args: pytest.fail("must not download"))
    with pytest.raises(ValueError, match="repository_locator_invalid"):
        trial.run_repository_trial(
            f"https://github.com/example/repository/{suffix}", ["node", "x.js"]
        )


@pytest.mark.parametrize("target", [
    "file:///tmp/tool.zip",
    "https://evil.example/example/repository/blob/" + "a" * 40 + "/tool.zip",
    "https://raw.githubusercontent.com/example/repository/" + "a" * 40 + "/tool.zip",
    "https://user:secret@github.com/example/repository/blob/" + "a" * 40 + "/tool.zip",
])
def test_repository_file_cannot_select_host_or_local_path(
    monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    monkeypatch.setattr(trial, "_download", lambda *args: pytest.fail("must not download"))
    with pytest.raises(ValueError, match="repository_locator_invalid"):
        trial.run_repository_trial(target, ["node", "x.js"])


def test_unreachable_repository_file_never_executes(monkeypatch: pytest.MonkeyPatch) -> None:
    def download(*args: Any, **kwargs: Any) -> bytes:
        raise OSError("public repository unavailable")

    monkeypatch.setattr(trial, "_download", download)
    monkeypatch.setattr(trial, "_execute", lambda *args, **kwargs: pytest.fail("must not execute"))
    with pytest.raises(OSError, match="unavailable"):
        trial.run_repository_trial(
            "https://github.com/example/repository/blob/" + "a" * 40 + "/tool.zip", ["node", "x.js"]
        )


@pytest.mark.parametrize("bad_archive", ["path_escape", "expanded_limit", "not_zip"])
def test_repository_file_preserves_archive_guards(
    monkeypatch: pytest.MonkeyPatch, bad_archive: str
) -> None:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(
            "../escape.js" if bad_archive == "path_escape" else "pack/probe.js", "public"
        )
    if bad_archive == "expanded_limit":
        monkeypatch.setattr(trial, "MAX_EXTRACTED_BYTES", 1)
    monkeypatch.setattr(
        trial, "_download",
        lambda *args, **kwargs: b"not a zip" if bad_archive == "not_zip" else data.getvalue(),
    )
    monkeypatch.setattr(trial, "_execute", lambda *args, **kwargs: pytest.fail("must not execute"))
    error = zipfile.BadZipFile if bad_archive == "not_zip" else ValueError
    message = "zip file" if bad_archive == "not_zip" else "archive_path|archive_limit"
    with pytest.raises(error, match=message):
        trial.run_repository_trial(
            "https://github.com/example/repository/blob/" + "a" * 40 + "/tool.zip",
            ["node", "probe.js"],
        )


def test_archive_curl_pins_public_ips_and_has_no_ambient_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://private.invalid")
    monkeypatch.setenv("GITHUB_TOKEN", "private-sentinel")
    monkeypatch.setattr(
        trial, "_resolve_public", lambda *args: frozenset({"1.1.1.1", "2606:4700::1111"})
    )

    def run(argv: list[str], **kwargs: Any) -> Any:
        assert argv[:2] == ["/usr/bin/curl", "-q"]
        assert (argv[argv.index("--resolve") + 1]
                == "raw.githubusercontent.com:443:1.1.1.1,[2606:4700::1111]")
        assert argv[argv.index("--noproxy") + 1] == "*"
        assert argv[argv.index("--proto") + 1] == "=https"
        assert argv[argv.index("--max-redirs") + 1] == "0"
        assert argv[argv.index("--max-filesize") + 1] == "10"
        assert 0 < float(argv[argv.index("--max-time") + 1]) <= 120
        assert 0 < kwargs["timeout"] <= 120
        assert set(kwargs["env"]) == {"PATH", "TMPDIR"}
        assert not {"-k", "--insecure", "-L", "--location", "--retry"}.intersection(argv)
        Path(argv[argv.index("--output") + 1]).write_bytes(b"archive")
        return types.SimpleNamespace(returncode=0, stdout="200\t1.1.1.1\t7", stderr="")

    monkeypatch.setattr(trial.subprocess, "run", run)
    assert trial._download(
        "raw.githubusercontent.com", "/public.zip", 10, timeout_seconds=120
    ) == b"archive"


@pytest.mark.parametrize("failure", [
    "redirect", "peer", "bytes", "curl_timeout", "curl_limit", "curl_receive", "deadline",
    "subprocess_timeout",
])
def test_archive_curl_fails_closed(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    tick = [0.0]
    monkeypatch.setattr(trial.time, "monotonic", lambda: tick[0])
    monkeypatch.setattr(trial, "_resolve_public", lambda *args: frozenset({"1.1.1.1"}))

    def run(argv: list[str], **kwargs: Any) -> Any:
        if failure == "subprocess_timeout":
            raise trial.subprocess.TimeoutExpired(argv, kwargs["timeout"])
        Path(argv[argv.index("--output") + 1]).write_bytes(b"x" * (11 if failure == "bytes" else 1))
        if failure == "deadline":
            tick[0] = 121
        return types.SimpleNamespace(
            returncode={"curl_timeout": 28, "curl_limit": 63, "curl_receive": 56}.get(failure, 0),
            stdout=("302\t1.1.1.1\t1" if failure == "redirect" else
                    "200\t127.0.0.1\t1" if failure == "peer" else "200\t1.1.1.1\t1"),
            stderr="",
        )

    monkeypatch.setattr(trial.subprocess, "run", run)
    with pytest.raises(ValueError, match="repository_"):
        trial._download("raw.githubusercontent.com", "/public.zip", 10, timeout_seconds=120)


def test_archive_curl_private_dns_never_starts_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trial, "default_resolver", lambda host: ["127.0.0.1"])
    monkeypatch.setattr(
        trial.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not connect")
    )
    with pytest.raises(ValueError):
        trial._download("raw.githubusercontent.com", "/public.zip", 10, timeout_seconds=120)


def test_receive_error_retry_shares_deadline_bytes_source_and_records_both_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tick = [0.0]
    calls = []
    receipts: list[dict[str, Any]] = []
    monkeypatch.setattr(trial.time, "monotonic", lambda: tick[0])
    monkeypatch.setattr(trial, "_resolve_public", lambda *args: frozenset({"1.1.1.1"}))

    def run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(argv)
        index = len(calls)
        assert float(argv[argv.index("--max-time") + 1]) == (120 if index == 1 else 50)
        assert kwargs["timeout"] == (120 if index == 1 else 50)
        assert argv[argv.index("--max-filesize") + 1] == ("10" if index == 1 else "7")
        assert argv[-1] == "https://raw.githubusercontent.com/public.zip"
        assert set(kwargs["env"]) == {"PATH", "TMPDIR"}
        assert not {"--retry", "-L", "-k"}.intersection(argv)
        target = Path(argv[argv.index("--output") + 1])
        assert not target.exists(), "failed partial bytes must not contaminate the next ZIP"
        target.write_bytes(b"bad" if index == 1 else b"archive")
        tick[0] += 70 if index == 1 else 5
        return types.SimpleNamespace(
            returncode=56 if index == 1 else 0,
            stdout="200\t1.1.1.1\t" + ("3" if index == 1 else "7"), stderr="",
        )

    monkeypatch.setattr(trial.subprocess, "run", run)
    result = trial._download(
        "raw.githubusercontent.com", "/public.zip", 10,
        timeout_seconds=120, download_attempts=receipts,
    )
    assert result == b"archive" and len(calls) == 2
    assert [r["curl_exit_code"] for r in receipts] == [56, 0]
    assert [r["received_bytes"] for r in receipts] == [3, 7]
    assert [r["cumulative_bytes"] for r in receipts] == [3, 10]
    assert [r["elapsed_seconds"] for r in receipts] == [70, 5]
    assert receipts[0]["error"] == "repository_download_failed_56"
    assert all(r["source_url"] == calls[0][-1] and r["peer_ip"] == "1.1.1.1" for r in receipts)


@pytest.mark.parametrize("code", [22, 28, 56, 60, 63])
def test_curl_retry_is_once_and_only_for_receive_error(
    monkeypatch: pytest.MonkeyPatch, code: int,
) -> None:
    calls = []
    monkeypatch.setattr(trial, "_resolve_public", lambda *args: frozenset({"1.1.1.1"}))

    def run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(argv)
        Path(argv[argv.index("--output") + 1]).write_bytes(b"x")
        return types.SimpleNamespace(returncode=code, stdout="200\t1.1.1.1\t1", stderr="")

    monkeypatch.setattr(trial.subprocess, "run", run)
    reason = {28: "timeout", 63: "limit"}.get(code, f"failed_{code}")
    with pytest.raises(ValueError, match=f"repository_download_{reason}") as caught:
        trial._download("raw.githubusercontent.com", "/public.zip", 10, timeout_seconds=120)
    assert len(calls) == (2 if code == 56 else 1)
    note = json.loads(caught.value.__notes__[0])
    assert note["archive_source_url"] == "https://raw.githubusercontent.com/public.zip"
    assert len(note["archive_download_attempts"]) == len(calls)
    assert all(r["curl_exit_code"] == code for r in note["archive_download_attempts"])


@pytest.mark.parametrize("boundary", ["deadline", "exhausted_bytes", "cumulative_bytes"])
def test_receive_error_retry_cannot_reset_deadline_or_byte_budget(
    monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    tick = [0.0]
    calls = []
    receipts: list[dict[str, Any]] = []
    monkeypatch.setattr(trial.time, "monotonic", lambda: tick[0])
    monkeypatch.setattr(trial, "_resolve_public", lambda *args: frozenset({"1.1.1.1"}))

    def run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(argv)
        size = 10 if boundary == "exhausted_bytes" else 7 if len(calls) == 1 else 4
        Path(argv[argv.index("--output") + 1]).write_bytes(b"x" * size)
        if boundary == "deadline":
            tick[0] = 120
        if len(calls) == 2:
            assert argv[argv.index("--max-filesize") + 1] == "3"
        return types.SimpleNamespace(
            returncode=56 if len(calls) == 1 else 0,
            stdout=f"200\t1.1.1.1\t{size}", stderr="",
        )

    monkeypatch.setattr(trial.subprocess, "run", run)
    with pytest.raises(
        ValueError, match="download_timeout" if boundary == "deadline" else "download_limit"
    ):
        trial._download(
            "raw.githubusercontent.com", "/public.zip", 10,
            timeout_seconds=120, download_attempts=receipts,
        )
    assert len(calls) == (2 if boundary == "cumulative_bytes" else 1)
    assert receipts[0]["curl_exit_code"] == 56
    assert sum(r["received_bytes"] for r in receipts) == receipts[-1]["cumulative_bytes"]


def _release_fixture(member: str = "leafpkg/__init__.py") -> tuple[bytes, dict[str, Any]]:
    """An installable pure-Python wheel; no build scripts or external dependencies."""
    data = io.BytesIO()
    files = {
        member: "value = 'official release fixture'\n",
        "leafpkg-1.0.0.dist-info/METADATA":
            "Metadata-Version: 2.1\nName: leafpkg\nVersion: 1.0.0\n\n",
        "leafpkg-1.0.0.dist-info/WHEEL":
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n",
    }
    files["leafpkg-1.0.0.dist-info/RECORD"] = "".join(f"{name},,\n" for name in files)
    with zipfile.ZipFile(data, "w") as wheel:
        for name, text in files.items():
            wheel.writestr(name, text)
    content = data.getvalue()
    return content, {
        "info": {
            "name": "leafpkg", "version": "1.0.0",
            "project_urls": {"Source": "https://github.com/example/leafpkg"},
        },
        "urls": [{
            "filename": "leafpkg-1.0.0-py3-none-any.whl", "packagetype": "bdist_wheel",
            "url": "https://files.pythonhosted.org/packages/aa/bb/hash/"
                   "leafpkg-1.0.0-py3-none-any.whl",
            "size": len(content), "digests": {"sha256": hashlib.sha256(content).hexdigest()},
        }],
    }


def _release_response(host: str, wheel: bytes, metadata: dict[str, Any]) -> bytes:
    if host == "pypi.org":
        return json.dumps(metadata).encode()
    if host == "api.github.com":
        return json.dumps({
            "private": False, "html_url": "https://github.com/example/leafpkg",
        }).encode()
    assert host == "files.pythonhosted.org"
    return wheel


def test_official_release_uses_verified_wheel_and_existing_dependency_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel, metadata = _release_fixture()
    downloads = []
    prepared = []

    def download(host: str, path: str, limit: int, **kwargs: Any) -> bytes:
        downloads.append((host, path, limit, kwargs))
        return _release_response(host, wheel, metadata)

    def dependencies(root: Path, work: Path, binary: str, *, distribution: Path) -> dict[str, Any]:
        prepared.append(distribution.name)
        assert distribution.parent == work and distribution.read_bytes() == wheel
        assert (root / "pyproject.toml").read_text() == "[project]\ndependencies=[]\n"
        assert not (root / "leafpkg").exists(), "wheel code must first pass the guarded installer"
        return {"status": "ready", "manager": "pip", "packages": []}

    monkeypatch.setattr(trial, "_download", download)
    monkeypatch.setattr(trial, "_python_dependencies", dependencies)
    monkeypatch.setattr(trial, "_execute", lambda *args, **kwargs: {
        "status": "completed", "exit_code": 0, "output": "release fixture",
        "network_enabled": False,
    })
    result = trial.run_repository_trial(
        "https://pypi.org/project/leafpkg/1.0.0/", ["python3", "-c", "import leafpkg"],
    )
    assert [item[0] for item in downloads] == [
        "pypi.org", "api.github.com", "files.pythonhosted.org",
    ]
    assert downloads[0][1] == "/pypi/leafpkg/1.0.0/json"
    assert downloads[-1][2] == trial.MAX_ARCHIVE_BYTES
    assert downloads[-1][3]["timeout_seconds"] == 120
    assert prepared == ["leafpkg-1.0.0-py3-none-any.whl"]
    assert result["archive_scope"] == "official_release"
    assert result["revision"] == "pypi:leafpkg@1.0.0"
    assert result["distribution_name"] == "leafpkg" and result["release_version"] == "1.0.0"
    assert result["archive_sha256"] == hashlib.sha256(wheel).hexdigest()
    assert result["archive_bytes"] == len(wheel)
    assert result["archive_source_url"] == metadata["urls"][0]["url"]
    assert result["archive_transport"] == "verified_https"
    assert result["repository"] == "https://github.com/example/leafpkg"
    assert result["repository_link_source"] == "pypi_project_urls"
    assert result["script_executed"] and result["exit_code"] == 0


@pytest.mark.parametrize("target", [
    "https://pypi.org/project/leafpkg/", "https://pypi.org/project/leafpkg/latest/",
    "https://pypi.org/project/leafpkg/1.0.0/?latest=true",
    "https://pypi.org/project/leafpkg/1.0.0/#fragment",
    "https://pypi.org/project/../1.0.0/", "https://pypi.org/project/leafpkg/../",
    "https://pypi.org/project/leafpkg/%31.0.0/",
    "https://user:secret@pypi.org/project/leafpkg/1.0.0/",
    "https://pypi.example/project/leafpkg/1.0.0/",
    "https://pypi.org/project/" + "x" * 195 + "/1.0.0/",
    "https://pypi.org/project/leafpkg/1.0." + "1" * 200 + "/",
])
def test_release_locator_must_pin_official_project_and_version_before_network(
    monkeypatch: pytest.MonkeyPatch, target: str,
) -> None:
    monkeypatch.setattr(
        trial, "_download", lambda *args, **kwargs: pytest.fail("must not download")
    )
    with pytest.raises(ValueError, match="repository_locator_invalid"):
        trial.run_repository_trial(target, ["python3", "-c", "import leafpkg"])


@pytest.mark.parametrize("argv", [["node", "-e", "console.log(1)"], ["python3", "probe.py"]])
def test_release_requires_in_process_python_fixture_before_network(
    monkeypatch: pytest.MonkeyPatch, argv: list[str],
) -> None:
    monkeypatch.setattr(
        trial, "_download", lambda *args, **kwargs: pytest.fail("must not download")
    )
    with pytest.raises(ValueError, match="repository_release_fixture_required"):
        trial.run_repository_trial("https://pypi.org/project/leafpkg/1.0.0/", argv)


@pytest.mark.parametrize("failure", [
    "name", "version", "repository_missing", "repository_private", "repository_ambiguous",
    "yanked", "wheel_missing", "wheel_duplicate", "wheel_platform", "sdist", "hash_shape",
    "foreign_host", "credentials", "redirect_query", "path_escape", "oversized", "size_string",
])
def test_release_metadata_failure_never_downloads_wheel_or_executes(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    _, metadata = _release_fixture()
    item = metadata["urls"][0]
    if failure in ("name", "version"):
        metadata["info"][failure] = "unexpected"
    elif failure == "repository_missing":
        metadata["info"]["project_urls"] = {}
    elif failure == "repository_private":
        metadata["info"]["project_urls"]["Source"] = "https://127.0.0.1/example/leafpkg"
    elif failure == "repository_ambiguous":
        metadata["info"]["project_urls"]["Other"] = "https://github.com/another/repository"
    elif failure == "yanked":
        item["yanked"] = True
    elif failure == "wheel_missing":
        metadata["urls"] = []
    elif failure == "wheel_duplicate":
        metadata["urls"].append(dict(item))
    elif failure == "wheel_platform":
        item["filename"] = "leafpkg-1.0.0-cp311-cp311-macosx_11_0_arm64.whl"
    elif failure == "sdist":
        item["packagetype"] = "sdist"
    elif failure == "hash_shape":
        item["digests"]["sha256"] = "not a hash"
    elif failure == "foreign_host":
        item["url"] = item["url"].replace("files.pythonhosted.org", "evil.example")
    elif failure == "credentials":
        item["url"] = item["url"].replace("https://", "https://user:secret@")
    elif failure == "redirect_query":
        item["url"] += "?source=elsewhere"
    elif failure == "path_escape":
        item["url"] = item["url"].replace("/aa/bb/", "/../")
    elif failure == "oversized":
        item["size"] = trial.MAX_ARCHIVE_BYTES + 1
    else:
        item["size"] = str(item["size"])
    calls = []

    def download(host: str, *args: Any, **kwargs: Any) -> bytes:
        calls.append(host)
        assert host == "pypi.org"
        return json.dumps(metadata).encode()

    monkeypatch.setattr(trial, "_download", download)
    monkeypatch.setattr(trial, "_execute", lambda *args, **kwargs: pytest.fail("must not execute"))
    with pytest.raises(ValueError, match="repository_release_"):
        trial.run_repository_trial(
            "https://pypi.org/project/leafpkg/1.0.0/", ["python3", "-c", "import leafpkg"],
        )
    assert calls == ["pypi.org"]


@pytest.mark.parametrize("failure", ["hash", "size", "path", "symlink", "expanded", "not_zip"])
def test_release_wheel_failure_never_prepares_or_executes(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    wheel, metadata = _release_fixture(
        "../escape.py" if failure == "path" else "leafpkg/__init__.py"
    )
    if failure == "hash":
        metadata["urls"][0]["digests"]["sha256"] = "0" * 64
    elif failure == "size":
        metadata["urls"][0]["size"] += 1
    elif failure == "expanded":
        monkeypatch.setattr(trial, "MAX_EXTRACTED_BYTES", 1)
    elif failure in ("symlink", "not_zip"):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            link = zipfile.ZipInfo("leafpkg/link")
            link.external_attr = 0o120777 << 16
            archive.writestr(link, "/etc/passwd")
        wheel = data.getvalue() if failure == "symlink" else b"not a zip"
        metadata["urls"][0]["size"] = len(wheel)
        metadata["urls"][0]["digests"]["sha256"] = hashlib.sha256(wheel).hexdigest()
    monkeypatch.setattr(trial, "_download", lambda host, *args, **kwargs:
                        _release_response(host, wheel, metadata))
    monkeypatch.setattr(trial, "_python_dependencies", lambda *args, **kwargs:
                        pytest.fail("must not prepare dependencies"))
    monkeypatch.setattr(trial, "_execute", lambda *args, **kwargs: pytest.fail("must not execute"))
    with pytest.raises((ValueError, zipfile.BadZipFile)):
        trial.run_repository_trial(
            "https://pypi.org/project/leafpkg/1.0.0/", ["python3", "-c", "import leafpkg"],
        )


@pytest.mark.skipif(
    not shutil.which("sandbox-exec") or not Path("/opt/homebrew/bin/python3").is_file(),
    reason="native sandbox/standalone Python unavailable",
)
def test_release_local_wheel_uses_real_offline_pip_and_isolated_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel, metadata = _release_fixture()
    monkeypatch.setattr(trial, "_download", lambda host, *args, **kwargs:
                        _release_response(host, wheel, metadata))
    monkeypatch.setattr(trial, "default_resolver", lambda host: ["1.1.1.1"])
    result = trial.run_repository_trial(
        "https://pypi.org/project/leafpkg/1.0.0/",
        ["python3", "-c", "import leafpkg; print(leafpkg.value)"],
    )
    assert result["dependencies"]["status"] == "ready", result
    assert result["exit_code"] == 0, result
    assert "official release fixture" in result["output"]
    assert result["network_enabled"] is False and result["child_processes_allowed"] is False
    assert result["dependencies"]["packages"] == [{
        "archive": "leafpkg-1.0.0-py3-none-any.whl", "sha256": hashlib.sha256(wheel).hexdigest(),
    }]


@pytest.mark.parametrize("repository", [
    {"private": True, "html_url": "https://github.com/example/leafpkg"},
    {"private": False, "html_url": "https://github.com/other/repository"},
    {"message": "Not Found"},
])
def test_release_github_backlink_must_resolve_to_same_public_repository(
    monkeypatch: pytest.MonkeyPatch, repository: dict[str, Any],
) -> None:
    _, metadata = _release_fixture()
    calls = []

    def download(host: str, *args: Any, **kwargs: Any) -> bytes:
        calls.append(host)
        assert host != "files.pythonhosted.org", "must not download an unverified wheel"
        return json.dumps(metadata if host == "pypi.org" else repository).encode()

    monkeypatch.setattr(trial, "_download", download)
    monkeypatch.setattr(trial, "_execute", lambda *args, **kwargs: pytest.fail("must not execute"))
    with pytest.raises(ValueError, match="repository_release_repository_invalid"):
        trial.run_repository_trial(
            "https://pypi.org/project/leafpkg/1.0.0/", ["python3", "-c", "import leafpkg"],
        )
    assert calls == ["pypi.org", "api.github.com"]


@pytest.mark.parametrize("missing_revision", [False, True])
def test_official_release_action_survives_library_save_and_null_revision_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_revision: bool,
) -> None:
    wheel, metadata = _release_fixture()
    monkeypatch.setattr(trial, "_download", lambda host, *args, **kwargs:
                        _release_response(host, wheel, metadata))
    monkeypatch.setattr(trial, "_python_dependencies", lambda *args, **kwargs: {
        "status": "ready", "manager": "pip", "packages": [],
    })
    output = "synthetic release action/save fixture"
    monkeypatch.setattr(trial, "_execute", lambda *args, **kwargs: {
        "status": "completed", "exit_code": 0, "output": output,
        "output_digest": hashlib.sha256(output.encode()).hexdigest(), "network_enabled": False,
    })
    engine = SubscriptionResearch(
        object(), run_ids=frozenset({"test"}), trial=trial.run_repository_trial,
    )
    request = {
        "kind": "repository_trial", "target": "https://pypi.org/project/leafpkg/1.0.0/",
        "argv": ["python3", "-c", "import leafpkg"], "reason": "synthetic integration fixture",
    }
    runner = types.SimpleNamespace(
        _clock=lambda: datetime.fromisoformat("2026-09-01T00:00:00+00:00")
    )
    record = engine._action(runner, request)["record"]
    assert record["revision"] == "pypi:leafpkg@1.0.0"
    service = library(tmp_path)
    value = json.loads(json.dumps(research_result()).replace("ev-example", record["evidence_id"]))
    if missing_revision:
        record["revision"] = None
        with pytest.raises(KnowledgeLibraryError, match="trial_revision"):
            save(service, result=value, evidence=[record])
    else:
        saved = save(service, result=value, evidence=[record])
        loaded = service.read(saved["knowledge_id"])["evidence"][0]
        assert loaded["revision"] == "pypi:leafpkg@1.0.0"
        assert loaded["argv"] == request["argv"]
        assert loaded["output_digest"] == hashlib.sha256(output.encode()).hexdigest()
        receipt = json.loads(loaded["excerpts"][0])
        assert receipt["archive_scope"] == "official_release"
        assert receipt["release_version"] == "1.0.0"
        assert receipt["archive_sha256"] == hashlib.sha256(wheel).hexdigest()
