"""Bounded, credential-free public repository trials in the native macOS sandbox."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any

from fragment_loop.research_fetch import (
    _resolve_public,
    _verified_connect,
    default_connection_factory,
    default_resolver,
    is_public_ip,
)

MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_EXTRACTED_BYTES = 128 * 1024 * 1024
MAX_OUTPUT_BYTES = 65536
MAX_WORK_BYTES = 256 * 1024 * 1024
MAX_WORK_ENTRIES = 20000
MAX_DEPENDENCIES = 64
DEPENDENCY_TIMEOUT = 150

# Native pip resolves versions; this guard runs before every HTTP send, including
# redirects and transitive direct references. No wheel code runs during download.
_PIP_GUARD = """
import sys
import socket
import ipaddress
from urllib.parse import urlsplit
from pip._internal.network.session import PipSession
from pip._internal.cli.main import main
original = PipSession.send
def official_only(self, request, **kwargs):
    url = urlsplit(request.url)
    if (url.scheme != "https" or url.hostname not in ("pypi.org", "files.pythonhosted.org")
        or url.username or url.password or url.port not in (None, 443)):
        raise RuntimeError("repository_dependency_source_forbidden")
    ips = [ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(url.hostname, 443)]
    if not ips or any(not ip.is_global for ip in ips):
        raise RuntimeError("repository_dependency_dns_invalid")
    # requests can discover macOS proxies without proxy environment variables.
    # The public preparation path never uses that implicit credentialed route.
    kwargs["proxies"] = {}
    return original(self, request, **kwargs)
PipSession.send = official_only
raise SystemExit(main(sys.argv[1:]))
"""


def _download(
    host: str, path: str, limit: int, *, timeout_seconds: float = 30,
    download_attempts: list[dict[str, Any]] | None = None,
) -> bytes:
    deadline = time.monotonic() + timeout_seconds
    if host in ("codeload.github.com", "raw.githubusercontent.com"):
        # One receive-error retry shares the original deadline and byte budget.
        # Each connection remains pinned to the same verified public DNS set.
        addresses = _resolve_public(host, default_resolver, "repository_network_unavailable")
        pinned = ",".join(f"[{ip}]" if ":" in ip else ip for ip in sorted(addresses))
        source = f"https://{host}{path}"
        attempts = download_attempts if download_attempts is not None else []
        downloaded = 0
        try:
            with tempfile.TemporaryDirectory(prefix="loop-public-download-") as directory:
                target = Path(directory) / "archive.zip"
                for attempt in (1, 2):
                    started = time.monotonic()
                    remaining = deadline - started
                    if remaining <= 0:
                        raise ValueError("repository_download_timeout")
                    if downloaded >= limit:
                        raise ValueError("repository_download_limit")
                    target.unlink(missing_ok=True)
                    receipt: dict[str, Any] = {
                        "attempt": attempt, "source_url": source,
                        "curl_exit_code": None, "http_status": None, "peer_ip": None,
                    }
                    attempts.append(receipt)
                    try:
                        result = subprocess.run(
                            [
                                "/usr/bin/curl", "-q", "--noproxy", "*", "--proto", "=https",
                                "--max-redirs", "0", "--resolve", f"{host}:443:{pinned}",
                                "--connect-timeout", "15", "--max-time", str(remaining),
                                "--max-filesize", str(limit - downloaded), "--fail",
                                "--silent", "--show-error", "--output", str(target),
                                "--write-out", "%{http_code}\t%{remote_ip}\t%{size_download}",
                                source,
                            ],
                            capture_output=True, text=True, timeout=remaining,
                            env={"PATH": "/usr/bin:/bin", "TMPDIR": directory},
                        )
                    except subprocess.TimeoutExpired as error:
                        size = target.stat().st_size if target.exists() else 0
                        receipt.update(
                            received_bytes=size, cumulative_bytes=downloaded + size,
                            elapsed_seconds=round(time.monotonic() - started, 6),
                            error="repository_download_timeout",
                        )
                        raise ValueError("repository_download_timeout") from error
                    status, peer, received = (result.stdout.rstrip("\r\n").split("\t")
                                              + ["", "", ""])[:3]
                    file_bytes = target.stat().st_size if target.exists() else 0
                    receipt.update(
                        curl_exit_code=result.returncode, http_status=status, peer_ip=peer,
                        received_bytes=file_bytes, cumulative_bytes=downloaded + file_bytes,
                        elapsed_seconds=round(time.monotonic() - started, 6),
                    )
                    if not received.isdecimal():
                        raise ValueError("repository_download_telemetry_invalid")
                    size = max(file_bytes, int(received))
                    downloaded += size
                    receipt.update(received_bytes=size, cumulative_bytes=downloaded)
                    if downloaded > limit:
                        raise ValueError("repository_download_limit")
                    if peer and peer not in addresses:
                        raise ValueError("repository_peer_cross_check_failed")
                    if time.monotonic() >= deadline:
                        raise ValueError("repository_download_timeout")
                    if result.returncode:
                        reason = {28: "timeout", 63: "limit"}.get(
                            result.returncode, f"failed_{result.returncode}"
                        )
                        receipt["error"] = f"repository_download_{reason}"
                        if result.returncode == 56 and attempt == 1:
                            continue
                        raise ValueError(receipt["error"])
                    if status != "200":
                        raise ValueError(f"repository_http_{status}")
                    if peer not in addresses:
                        raise ValueError("repository_peer_cross_check_failed")
                    return target.read_bytes()
        except (ValueError, OSError) as error:
            error.add_note(json.dumps({
                "archive_source_url": source, "archive_download_attempts": attempts,
            }))
            raise
    connection, _ = _verified_connect(
        host,
        resolver=default_resolver,
        connection_factory=default_connection_factory,
        timeout=15,
        code="repository_network_unavailable",
    )
    try:
        connection.request(
            "GET",
            path,
            headers={
                "User-Agent": "LoopGraph-public-trial/1",
                "Accept": "application/vnd.github+json",
            },
        )
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(f"repository_http_{response.status}")
        data = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("repository_download_timeout")
            if connection.sock is not None:
                connection.sock.settimeout(min(30, remaining))
            block = response.read1(min(65536, limit + 1 - len(data)))
            if not block:
                return bytes(data)
            data.extend(block)
            if len(data) > limit:
                raise ValueError("repository_download_limit")
    finally:
        connection.close()


def extract_archive(data: bytes, destination: Path) -> Path:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 10000 or sum(x.file_size for x in entries) > MAX_EXTRACTED_BYTES:
            raise ValueError("repository_archive_limit")
        roots: set[str] = set()
        for entry in entries:
            name = Path(entry.filename)
            if name.is_absolute() or ".." in name.parts or "\\" in entry.filename or not name.parts:
                raise ValueError("repository_archive_path")
            if ((entry.external_attr >> 16) & 0o170000) == 0o120000:
                raise ValueError("repository_archive_symlink")
            roots.add(name.parts[0])
        if len(roots) != 1:
            raise ValueError("repository_archive_root")
        archive.extractall(destination)
        root = destination / next(iter(roots))
        if not root.is_dir():
            raise ValueError("repository_archive_root")
        return root


def _validate_command_shape(argv: object) -> list[str]:
    if (
        not isinstance(argv, list)
        or not 2 <= len(argv) <= 24
        or any(not isinstance(x, str) or "\x00" in x for x in argv)
    ):
        raise ValueError("repository_command_invalid")
    if argv[0] not in ("node", "python3"):
        raise ValueError("repository_runtime_unsupported")
    inline = (argv[0], argv[1]) in (("node", "-e"), ("python3", "-c"))
    if inline:
        if len(argv) != 3 or not argv[2].strip() or len(argv[2]) > 4000:
            raise ValueError("repository_fixture_invalid")
    elif (any(len(x) > 500 for x in argv) or argv[1].startswith("-")
          or Path(argv[1]).is_absolute() or ".." in Path(argv[1]).parts):
        raise ValueError("repository_command_path")
    return argv


def validate_command(argv: object, root: Path) -> list[str]:
    args = _validate_command_shape(argv)
    if (args[0], args[1]) in (("node", "-e"), ("python3", "-c")):
        # A tiny generated fixture is an ordinary file inside the same denied-by-
        # default sandbox. It gains no shell, network, credentials or child processes.
        suffix = ".cjs" if args[0] == "node" else ".py"
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=".loop-trial-", suffix=suffix,
            dir=root, delete=False,
        ) as fixture:
            fixture.write(args[2])
        args = [args[0], Path(fixture.name).name]
    target = (root / args[1]).resolve()
    if not target.is_relative_to(root.resolve()) or not target.is_file():
        raise ValueError("repository_command_path")
    # Homebrew's maintained Python is independent of the application's venv and
    # has native pip available. Do not borrow the application's dependencies.
    preferred = Path("/opt/homebrew/bin/python3") if args[0] == "python3" else None
    binary = str(preferred) if preferred and preferred.is_file() else shutil.which(args[0])
    if not binary:
        raise ValueError("repository_runtime_unavailable")
    return [str(Path(binary).resolve()), *(["-S"] if args[0] == "python3" else []), *args[1:]]


def _work_size(work: Path) -> int:
    total = count = 0
    for directory, dirs, files in os.walk(work, followlinks=False):
        count += len(dirs) + len(files)
        if count > MAX_WORK_ENTRIES:
            raise ValueError("repository_disk_limit")
        for name in files:
            try:
                info = (Path(directory) / name).lstat()
            except FileNotFoundError:
                continue
            total += info.st_size
            if total > MAX_WORK_BYTES:
                raise ValueError("repository_disk_limit")
    return total


def _execute(
    argv: list[str],
    root: Path,
    work: Path,
    timeout: float = 60,
    *,
    network_hosts: tuple[str, ...] = (),
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    sandbox = shutil.which("sandbox-exec")
    if not sandbox:
        raise ValueError("repository_isolation_unavailable")
    # deny-default filesystem and network rules are inherited by all descendants.
    readable = [
        "/System",
        "/usr",
        "/bin",
        "/sbin",
        "/Library/Apple",
        "/Library/Frameworks/Python.framework",
        "/opt/homebrew/bin",
        "/opt/homebrew/lib",
        "/opt/homebrew/Cellar",
        "/opt/homebrew/opt",
        "/opt/homebrew/share",
        "/private/preboot/Cryptexes",
        "/private/var/db/dyld",
        str(Path(argv[0]).parent),
        str(work),
    ]
    profile = ('(version 1)(deny default)(allow process*)(allow sysctl-read)'
               '(allow mach-lookup)(allow file-read-metadata)(allow file-read* (literal "/"))')
    profile += (
        "(allow file-read* " + " ".join("(subpath " + json.dumps(x) + ")" for x in readable) + ")"
    )
    profile += (
        '(allow file-read* (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random"))'
    )
    profile += "(allow file-write* (subpath " + json.dumps(str(work)) + ') (literal "/dev/null"))'
    # No detached descendants can outlive cleanup. These trials deliberately
    # support in-process scripts; build systems and subprocess suites stop here.
    profile += "(deny process-fork)"
    if network_hosts:
        if set(network_hosts) != {"pypi.org", "files.pythonhosted.org"}:
            raise ValueError("repository_network_scope_invalid")
        addresses = set().union(*(default_resolver(host) for host in network_hosts))
        if not addresses or not all(is_public_ip(ip) for ip in addresses):
            raise ValueError("repository_dependency_dns_invalid")
        # This macOS version accepts only '*' and 'localhost' as addresses.
        # Host and public-DNS checks live in the trusted pip guard; the native
        # boundary permits TLS only during preparation, never during trials.
        profile += '(allow network-outbound (remote tcp "*:443"))'
        profile += '(allow network-outbound (literal "/private/var/run/mDNSResponder"))'
    home = work / "home"
    home.mkdir(exist_ok=True)
    environment = {
        "HOME": str(home),
        "TMPDIR": str(work),
        "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
        "LANG": "en_US.UTF-8",
        "ARCHIFY_UPDATE_CHECK_DISABLED": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "PYTHONNOUSERSITE": "1",
        "PIP_CONFIG_FILE": "/dev/null",
        "OPENSSL_CONF": "/dev/null",
    }
    environment.update(extra_env or {})
    _work_size(work)
    launcher = work / "trial_resource_limits.py"
    launcher.write_text(
        "import os, resource, sys\n"
        "resource.setrlimit(resource.RLIMIT_FSIZE, (33554432, 33554432))\n"
        "resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))\n"
        f"resource.setrlimit(resource.RLIMIT_CPU, ({int(timeout) + 1}, {int(timeout) + 2}))\n"
        "os.execv(sys.argv[1], sys.argv[1:])\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-I", str(launcher), sandbox, "-p", profile, *argv],
        cwd=root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    output = bytearray()
    reason = "completed"
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while selector.get_map() or process.poll() is None:
            try:
                _work_size(work)
            except ValueError:
                reason = "disk_limit"
                break
            if time.monotonic() >= deadline:
                reason = "timeout"
                break
            for key, _ in selector.select(min(0.2, max(0, deadline - time.monotonic()))):
                block = os.read(key.fd, 8192)
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(block)
                if len(output) > MAX_OUTPUT_BYTES:
                    reason = "output_limit"
                    break
            if reason != "completed":
                break
    finally:
        selector.close()
        if reason == "completed" and process.poll() is None:
            try:
                process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                reason = "timeout"
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        process.stdout.close()
    if reason == "completed":
        if process.returncode == -signal.SIGXFSZ:
            reason = "file_limit"
        elif process.returncode == -signal.SIGXCPU:
            reason = "cpu_limit"
        else:
            try:
                _work_size(work)
            except ValueError:
                reason = "disk_limit"
    return {
        "exit_code": process.returncode,
        "status": reason,
        "output": bytes(output[:MAX_OUTPUT_BYTES]).decode("utf-8", errors="replace"),
        "output_digest": hashlib.sha256(bytes(output)).hexdigest(),
        "network_enabled": bool(network_hosts),
        "isolation": "macos_sandbox_deny_default",
        "disk_limit_bytes": MAX_WORK_BYTES,
        "disk_limit_enforcement": "monitored_total",
        "file_limit_bytes": 33554432,
        "child_processes_allowed": False,
    }


def _python_dependencies(
    root: Path, work: Path, binary: str, *, distribution: Path | None = None,
) -> dict[str, Any]:
    project_file = root / "pyproject.toml"
    if not project_file.is_file():
        return {"status": "not_declared", "manager": "pip"}
    project = tomllib.loads(project_file.read_text()).get("project", {})
    if not isinstance(project, dict) or "dependencies" in project.get("dynamic", []):
        raise ValueError("repository_dynamic_dependencies_unsupported")
    dependencies = project.get("dependencies", [])
    if not isinstance(dependencies, list) or len(dependencies) > MAX_DEPENDENCIES:
        raise ValueError("repository_dependencies_limit")
    for requirement in dependencies:
        if (
            not isinstance(requirement, str)
            or len(requirement) > 500
            or not re.match(r"^[A-Za-z0-9]", requirement)
            or any(x in requirement for x in ("@", ":", "/", "\\", "\n", "\r"))
        ):
            raise ValueError("repository_dependency_source_forbidden")
    if not dependencies and distribution is None:
        return {"status": "not_needed", "manager": "pip", "packages": []}
    prep = work / "python-preparation"
    prep.mkdir()
    guard = prep / "pip_official_only.py"
    guard.write_text(_PIP_GUARD)
    wheels, site = prep / "wheels", work / "python-dependencies"
    wheels.mkdir()
    result = _execute(
        [
            binary,
            "-I",
            str(guard),
            "download",
            "--only-binary=:all:",
            "--no-input",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--progress-bar",
            "off",
            "--retries",
            "0",
            "--timeout",
            "15",
            "--index-url",
            "https://pypi.org/simple",
            "--dest",
            str(wheels),
            *dependencies,
            *([str(distribution)] if distribution is not None else []),
        ],
        prep,
        work,
        DEPENDENCY_TIMEOUT,
        network_hosts=("pypi.org", "files.pythonhosted.org"),
    )
    if result["status"] != "completed" or result["exit_code"] != 0:
        return {"status": "unavailable", "manager": "pip", "phase": "download", "receipt": result}
    archives = sorted(wheels.iterdir())
    if (
        not archives
        or len(archives) > MAX_DEPENDENCIES
        or any(x.suffix != ".whl" for x in archives)
    ):
        raise ValueError("repository_wheels_invalid")
    expanded = 0
    for wheel in archives:
        with zipfile.ZipFile(wheel) as archive:
            expanded += sum(entry.file_size for entry in archive.infolist())
    if _work_size(work) + expanded > MAX_WORK_BYTES:
        raise ValueError("repository_disk_limit")
    result = _execute(
        [
            binary,
            "-I",
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--no-input",
            "--disable-pip-version-check",
            "--no-compile",
            "--target",
            str(site),
            *map(str, archives),
        ],
        prep,
        work,
        60,
    )
    return {
        "status": "ready"
        if result["exit_code"] == 0 and result["status"] == "completed"
        else "unavailable",
        "manager": "pip",
        "phase": "offline_install",
        "packages": [
            {"archive": x.name, "sha256": hashlib.sha256(x.read_bytes()).hexdigest()}
            for x in archives
        ],
        "receipt": result,
    }


def _node_dependencies(root: Path, work: Path) -> dict[str, Any]:
    manifest_path = root / "package.json"
    if not manifest_path.is_file():
        return {"status": "not_declared", "manager": "npm"}
    manifest = json.loads(manifest_path.read_text())
    if not isinstance(manifest, dict):
        raise ValueError("repository_manifest_invalid")
    dependencies = manifest.get("dependencies", {})
    if not dependencies:
        return {"status": "not_needed", "manager": "npm", "packages": []}
    if not isinstance(dependencies, dict) or len(dependencies) > MAX_DEPENDENCIES:
        raise ValueError("repository_dependencies_limit")
    lock_path = root / "package-lock.json"
    if not lock_path.is_file():
        raise ValueError("repository_npm_lock_required")
    lock = json.loads(lock_path.read_text())
    if (
        not isinstance(lock, dict)
        or lock.get("lockfileVersion") not in (2, 3)
        or not isinstance(lock.get("packages"), dict)
        or any(not isinstance(value, dict) for value in lock["packages"].values())
    ):
        raise ValueError("repository_npm_lock_unsupported")
    packages = lock["packages"]
    selected = {
        key: value for key, value in packages.items() if key and not value.get("dev", False)
    }
    if len(selected) > MAX_DEPENDENCIES:
        raise ValueError("repository_dependencies_limit")
    prep, archives = work / "node-preparation", work / "npm-archives"
    prep.mkdir()
    archives.mkdir()
    receipts = []
    expanded = entries = 0
    deadline = time.monotonic() + DEPENDENCY_TIMEOUT
    for key, package in selected.items():
        url = urllib.parse.urlsplit(package.get("resolved", ""))
        if (
            package.get("link")
            or url.scheme != "https"
            or url.hostname != "registry.npmjs.org"
            or url.username
            or url.password
            or url.port not in (None, 443)
            or url.query
            or url.fragment
        ):
            raise ValueError("repository_dependency_source_forbidden")
        if time.monotonic() >= deadline:
            raise ValueError("repository_dependency_timeout")
        data = _download("registry.npmjs.org", url.path, MAX_ARCHIVE_BYTES)
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
        if package.get("integrity") != integrity:
            raise ValueError("repository_dependency_integrity")
        current_bytes = _work_size(work)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as package_archive:
            for entry in package_archive:
                path = Path(entry.name)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in entry.name
                    or not (entry.isfile() or entry.isdir())
                ):
                    raise ValueError("repository_dependency_archive_path")
                expanded += entry.size
                entries += 1
                if expanded + current_bytes > MAX_WORK_BYTES or entries > MAX_WORK_ENTRIES:
                    raise ValueError("repository_disk_limit")
        archive = archives / (hashlib.sha256(data).hexdigest() + ".tgz")
        archive.write_bytes(data)
        package["resolved"] = archive.as_uri()
        receipts.append({"package": key, "integrity": integrity, "source": url.geturl()})
        _work_size(work)
    # Preserve the locked tree; omit dev tools and every repository configuration
    # and script. Offline npm cannot follow any residual dependency URL.
    clean = {"name": "loop-public-trial", "version": "0.0.0", "dependencies": dependencies}
    lock["name"], lock["version"] = clean["name"], clean["version"]
    lock["packages"][""] = clean
    (prep / "package.json").write_text(json.dumps(clean))
    (prep / "package-lock.json").write_text(json.dumps(lock))
    empty = prep / "empty-npmrc"
    empty.write_text("")
    empty_global = prep / "empty-global-npmrc"
    empty_global.write_text("")
    npm = shutil.which("npm")
    node = shutil.which("node")
    if not npm or not node:
        raise ValueError("repository_runtime_unavailable")
    result = _execute(
        [
            str(Path(node).resolve()),
            str(Path(npm).resolve()),
            "ci",
            "--offline",
            "--ignore-scripts",
            "--omit=dev",
            "--no-audit",
            "--no-fund",
            "--cache",
            str(prep / "cache"),
            "--userconfig",
            str(empty),
            "--globalconfig",
            str(empty_global),
        ],
        prep,
        work,
        max(1, min(60, deadline - time.monotonic())),
    )
    if result["status"] == "completed" and result["exit_code"] == 0:
        target = root / "node_modules"
        if target.exists():
            raise ValueError("repository_existing_node_modules")
        shutil.move(str(prep / "node_modules"), target)
        status = "ready"
    else:
        status = "unavailable"
    return {"status": status, "manager": "npm", "packages": receipts, "receipt": result}


def run_repository_trial(url: str, argv: list[str]) -> dict[str, Any]:
    _validate_command_shape(argv)  # reject malformed actions before any network I/O
    release: dict[str, Any] = {}
    release_match = re.fullmatch(
        r"https://pypi\.org/project/([A-Za-z0-9][A-Za-z0-9._-]*)/"
        r"([0-9]+(?:\.[0-9]+)+(?:[A-Za-z0-9.+_-]*))/?", url,
    )
    if release_match:
        if argv[:2] != ["python3", "-c"]:
            raise ValueError("repository_release_fixture_required")
        name, version = release_match.groups()
        normalized = re.sub(r"[-_.]+", "-", name).lower()
        if len(f"pypi:{normalized}@{version}") > 200:
            raise ValueError("repository_locator_invalid")
        metadata_path = f"/pypi/{normalized}/{version}/json"
        metadata = json.loads(_download("pypi.org", metadata_path, 2 * 1024 * 1024))
        info = metadata.get("info") if isinstance(metadata, dict) else None
        if (not isinstance(info, dict) or not isinstance(info.get("name"), str)
            or re.sub(r"[-_.]+", "-", info["name"]).lower() != normalized
            or info.get("version") != version or info.get("yanked")):
            raise ValueError("repository_release_metadata_invalid")
        links = info.get("project_urls")
        repositories = {
            link.rstrip("/") for link in (links.values() if isinstance(links, dict) else [])
            if isinstance(link, str) and re.fullmatch(
                r"https://github\.com/[A-Za-z0-9_-][A-Za-z0-9_.-]*/"
                r"[A-Za-z0-9_-][A-Za-z0-9_.-]*/?", link,
            )
        }
        if len(repositories) != 1:
            raise ValueError("repository_release_repository_invalid")
        repository = repositories.pop()
        # ponytail: universal wheels cover pure-Python release trials without
        # implementing wheel tag selection or running an sdist build backend.
        filename = f"{normalized.replace('-', '_')}-{version}-py3-none-any.whl"
        files = metadata.get("urls")
        candidates = [
            item for item in (files if isinstance(files, list) else [])
            if isinstance(item, dict) and item.get("filename") == filename
            and item.get("packagetype") == "bdist_wheel" and not item.get("yanked")
        ]
        if len(candidates) != 1:
            raise ValueError("repository_release_wheel_unavailable")
        selected = candidates[0]
        source = selected.get("url")
        digest = selected.get("digests")
        expected_hash = digest.get("sha256") if isinstance(digest, dict) else None
        expected_size = selected.get("size")
        if (not isinstance(source, str) or not isinstance(expected_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
            or type(expected_size) is not int or not 0 < expected_size <= MAX_ARCHIVE_BYTES):
            raise ValueError("repository_release_metadata_invalid")
        locator = urllib.parse.urlsplit(source)
        if (locator.scheme != "https" or locator.netloc != "files.pythonhosted.org"
            or locator.query or locator.fragment or not locator.path.startswith("/packages/")
            or locator.path.rsplit("/", 1)[-1] != filename
            or any(part in ("", ".", "..") for part in locator.path[1:].split("/"))
            or not re.fullmatch(r"/[A-Za-z0-9_./+-]+", locator.path)):
            raise ValueError("repository_release_source_forbidden")
        public_repository = json.loads(_download(
            "api.github.com", "/repos/" + repository.removeprefix("https://github.com/"),
            2 * 1024 * 1024,
        ))
        if (not isinstance(public_repository, dict) or public_repository.get("private") is not False
            or str(public_repository.get("html_url", "")).casefold() != repository.casefold()):
            raise ValueError("repository_release_repository_invalid")
        host, path = "files.pythonhosted.org", locator.path
        archive_scope, revision = "official_release", f"pypi:{normalized}@{version}"
        release = {
            "distribution_name": normalized, "release_version": version,
            "release_metadata_url": f"https://pypi.org{metadata_path}",
            "repository_link_source": "pypi_project_urls",
        }
    else:
        match = re.fullmatch(
            r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)"
            r"(?:/?|/blob/([0-9a-f]{40})/([A-Za-z0-9_./-]+\.zip))", url,
        )
        if not match or any(part in (".", "..") for part in match.groups()[:2]):
            raise ValueError("repository_locator_invalid")
        owner, repo, revision, archive_path = match.groups()
        repository = f"https://github.com/{owner}/{repo}"
        if archive_path is not None:
            if any(part in ("", ".", "..") for part in archive_path.split("/")):
                raise ValueError("repository_locator_invalid")
            host, path = "raw.githubusercontent.com", f"/{owner}/{repo}/{revision}/{archive_path}"
            archive_scope = "repository_file"
        else:
            metadata = json.loads(
                _download("api.github.com", f"/repos/{owner}/{repo}/commits/HEAD", 2 * 1024 * 1024)
            )
            revision = metadata.get("sha", "")
            if not re.fullmatch(r"[0-9a-f]{40}", revision):
                raise ValueError("repository_revision_invalid")
            host, path = "codeload.github.com", f"/{owner}/{repo}/zip/{revision}"
            archive_scope = "repository"
    download_attempts: list[dict[str, Any]] = []
    archive = _download(
        host, path, MAX_ARCHIVE_BYTES, timeout_seconds=120, download_attempts=download_attempts,
    )
    if release:
        if len(archive) != expected_size or hashlib.sha256(archive).hexdigest() != expected_hash:
            raise ValueError("repository_release_integrity")
        with zipfile.ZipFile(io.BytesIO(archive)) as wheel:
            entries = wheel.infolist()
            if len(entries) > 10000 or sum(x.file_size for x in entries) > MAX_EXTRACTED_BYTES:
                raise ValueError("repository_archive_limit")
            for entry in entries:
                member = Path(entry.filename)
                if (member.is_absolute() or ".." in member.parts or not member.parts
                    or "\\" in entry.filename):
                    raise ValueError("repository_archive_path")
                if ((entry.external_attr >> 16) & 0o170000) == 0o120000:
                    raise ValueError("repository_archive_symlink")
    with tempfile.TemporaryDirectory(prefix="loop-public-trial-") as directory:
        work = Path(directory).resolve()
        distribution = None
        if release:
            root = work / "release-fixture"
            root.mkdir()
            (root / "pyproject.toml").write_text("[project]\ndependencies=[]\n")
            distribution = work / filename
            distribution.write_bytes(archive)
        else:
            root = extract_archive(archive, work)
        command = validate_command(argv, root)
        try:
            dependencies = (
                _python_dependencies(root, work, command[0], distribution=distribution)
                if distribution is not None else _python_dependencies(root, work, command[0])
                if argv[0] == "python3" else _node_dependencies(root, work)
            )
        except (ValueError, OSError) as error:
            dependencies = {"status": "unavailable", "reason": str(error)}
        if dependencies["status"] == "unavailable":
            outcome = {
                "exit_code": None,
                "status": "dependency_unavailable",
                "output": "",
                "network_enabled": False,
                "script_executed": False,
            }
        else:
            outcome = _execute(
                command,
                root,
                work,
                extra_env={
                    "PYTHONPATH": os.pathsep.join(
                        map(str, [root, root / "src", work / "python-dependencies"])
                    )
                },
            )
            outcome["script_executed"] = True
        return {
            **outcome,
            "repository": repository,
            "revision": revision,
            "archive_source_url": f"https://{host}{path}",
            "archive_scope": archive_scope,
            "archive_transport": "verified_https" if release else "system_curl",
            "archive_download_attempts": download_attempts,
            "command": argv,
            "archive_sha256": hashlib.sha256(archive).hexdigest(),
            "archive_bytes": len(archive),
            "dependencies": dependencies,
            **release,
        }
