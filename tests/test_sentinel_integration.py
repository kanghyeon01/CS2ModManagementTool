"""The C sentinel and the Python agent client working together (real processes, real sockets).

Builds ``sentinel/`` with the system C compiler (with AddressSanitizer when available), runs it
with a fake curl, changes the "Steam" build, and checks: baseline, settle, job queued, the
Wake-on-LAN packet on the wire, boot decision, claim, heartbeat, completion, de-duplication,
authentication and malformed requests. Skipped when no C compiler is installed.
"""

import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from cs2mmt.agent.client import ControllerRef, Controllers
from cs2mmt.controller.wol import magic_packet

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "integration-test-token-0123456789"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _steam_doc(build: str) -> str:
    return json.dumps({"data": {"949230": {"common": {"name": "Cities: Skylines II"},
                                           "depots": {"branches": {"public": {"buildid": build, "timeupdated": "1761900000"}}}}},
                       "status": "success"})


@pytest.fixture(scope="module")
def sentinel_binary(tmp_path_factory):
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if cc is None:
        pytest.skip("no C compiler")
    out = tmp_path_factory.mktemp("bin") / "cs2mmt-sentinel"
    src = [str(ROOT / "sentinel" / "src" / f) for f in ("sentinel.c", "json.c", "util.c")]
    flags = ["-O1", "-g", "-std=c11", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter"]
    asan = ["-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
    r = subprocess.run([cc] + flags + asan + ["-o", str(out)] + src, capture_output=True, text=True)
    if r.returncode != 0:
        r = subprocess.run([cc] + flags + ["-o", str(out)] + src, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return out


@pytest.fixture
def sentinel(sentinel_binary, tmp_path):
    fake = tmp_path / "curl"
    fake.write_text("#!/bin/sh\nexec %s %s \"$@\"\n" % (sys.executable, ROOT / "tests" / "fixtures" / "fake_curl.py"))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    steam_file = tmp_path / "steam.json"
    steam_file.write_text(_steam_doc("100"))
    discord_log = tmp_path / "discord.log"
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0))
    udp.settimeout(10)
    port = _free_port()
    conf = tmp_path / "sentinel.conf"
    conf.write_text("\n".join([
        "id = test-sentinel",
        "listen_host = 127.0.0.1",
        "listen_port = %d" % port,
        "api_token = %s" % TOKEN,
        "state_file = %s" % (tmp_path / "sentinel.state"),
        "curl = %s" % fake,
        "poll_seconds = 1",
        "settle_seconds = 0",
        "dispatch_seconds = 1",
        "controller_check_seconds = 3600",
        "wol_mac = 01:02:03:04:05:06",
        "wol_broadcast = 127.0.0.1",
        "wol_port = %d" % udp.getsockname()[1],
        "wol_repeat = 1",
        "discord_webhook_url = https://discord.com/api/webhooks/123/TEST-WEBHOOK-TOKEN",
        "notify = always",
        "",
    ]))
    env = dict(os.environ, FAKE_STEAM_FILE=str(steam_file), FAKE_DISCORD_LOG=str(discord_log), ASAN_OPTIONS="detect_leaks=1")
    proc = subprocess.Popen([str(sentinel_binary), "--config", str(conf)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = "http://127.0.0.1:%d" % port
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/api/v1/health", timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    yield {"proc": proc, "url": base, "steam_file": steam_file, "discord_log": discord_log, "udp": udp, "state": tmp_path / "sentinel.state"}
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    udp.close()


def _health(url: str) -> dict:
    req = urllib.request.Request(url + "/api/v1/health", headers={"Authorization": "Bearer " + TOKEN})
    return json.loads(urllib.request.urlopen(req, timeout=5).read())


def _wait(pred, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.2)
    raise AssertionError("condition not reached")


def test_full_cycle_with_python_client(sentinel):
    url = sentinel["url"]
    _wait(lambda: _health(url).get("known_build") == "100")
    sentinel["steam_file"].write_text(_steam_doc("101"))
    _wait(lambda: _health(url).get("job_state") in ("queued", "dispatched"))
    packet, _ = sentinel["udp"].recvfrom(200)
    assert packet == magic_packet("01:02:03:04:05:06")
    _wait(lambda: _health(url).get("job_state") == "dispatched")

    clients = Controllers([ControllerRef("sentinel", url)], TOKEN)
    decision = clients.boot_decision(time.time())
    assert decision["action"] == "windows"
    claimed = clients.claim("main-pc")
    assert claimed is not None
    assert claimed.kind == "game_update"
    assert claimed.payload["new_build"] == "101" and claimed.payload["old_build"] == "100"
    assert claimed.work_key == "game_update:101"
    assert claimed.payload["approval"] == "auto"
    assert clients.claim("main-pc") is not None  # the same host asking again gets its job back (resume)
    assert clients.heartbeat(claimed, "build", "RealisticAviation") is False
    clients.complete(claimed, "succeeded", {"mods": {}}, "모든 모드 정상")
    _wait(lambda: _health(url).get("job_state") == "done")
    assert clients.claim("main-pc") is None
    # approvals are not the sentinel's business: the agent sees zero acceptances
    assert clients.request_approval({"id": "ap-x", "title": "t", "body": "b", "payload": {}}) == 0
    assert clients.approval_state("ap-x") == "unknown"
    lines = [json.loads(l) for l in sentinel["discord_log"].read_text(encoding="utf-8").splitlines()]
    texts = [l["content"] for l in lines]
    assert any("게임 업데이트 감지" in t for t in texts)
    assert any("모든 모드 정상" in t for t in texts)
    assert all(t.startswith("[test-sentinel] ") for t in texts)
    assert all(l["url"].endswith("/123/TEST-WEBHOOK-TOKEN") and l["header"] == "Content-Type: application/json" for l in lines)
    assert all(l["allowed_mentions"] == {"parse": []} for l in lines)


def test_dedupe_from_other_controller_closes_queued_job(sentinel):
    url = sentinel["url"]
    _wait(lambda: _health(url).get("known_build") == "100")
    clients = Controllers([ControllerRef("sentinel", url)], TOKEN)
    # pause dispatching so the job stays queued, then queue one by hand
    req = urllib.request.Request(url + "/api/v1/control", data=b'{"action":"pause"}', method="POST",
                                 headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=5)
    status, data = clients._post(clients.refs[0], "/api/v1/jobs", {"kind": "game_update"})
    assert status == 200 and data["job"]["state"] == "queued"
    clients.broadcast_done("game_update:100", "succeeded", via="nas")
    assert _health(url)["job_state"] == "done"


def test_auth_and_bad_requests(sentinel):
    url = sentinel["url"]
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(urllib.request.Request(url + "/api/v1/status"), timeout=5)
    assert e.value.code == 401
    bad = urllib.request.Request(url + "/api/v1/jobs/claim", data=b"{not json", method="POST",
                                 headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(bad, timeout=5)
    assert e.value.code == 400
    big = urllib.request.Request(url + "/api/v1/notify", data=b"x" * 70000, method="POST",
                                 headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(big, timeout=5)
    assert e.value.code == 413
    wrong = urllib.request.Request(url + "/api/v1/status", headers={"Authorization": "Bearer wrong-token-0123456789"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(wrong, timeout=5)
    assert e.value.code == 401
    # still alive after all that
    assert _health(url)["ok"] is True


def test_state_survives_restart(sentinel, sentinel_binary):
    url = sentinel["url"]
    _wait(lambda: _health(url).get("known_build") == "100")
    sentinel["proc"].send_signal(signal.SIGTERM)
    out, _ = sentinel["proc"].communicate(timeout=10)
    assert sentinel["proc"].returncode == 0, out
    assert "ERROR: AddressSanitizer" not in out and "runtime error" not in out, out
    state = sentinel["state"].read_text()
    assert "known_build = 100" in state


def test_restart_request_exits_cleanly(sentinel):
    url = sentinel["url"]
    _wait(lambda: _health(url).get("known_build") == "100")
    req = urllib.request.Request(url + "/api/v1/control", data=b'{"action":"restart"}', method="POST",
                                 headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    assert json.loads(urllib.request.urlopen(req, timeout=5).read())["restarting"] is True
    sentinel["proc"].wait(timeout=10)
    assert sentinel["proc"].returncode == 0
