"""Raw-console route checks over a real HTTP client (design §3.14).

    uv run --extra web python tests/test_web_raw.py

Proves the tiers end to end: a read command is sent and returns lines; a deny
command is refused and **nothing reaches the adapter**; a confirm command needs a
token plus a typed acknowledgement, is sent once, and is audited; the token cannot
be reused; and in read-only mode a confirm command is refused while a read still
works. The adapter is a recording fake, so "nothing reached the adapter" is an
assertion, not a hope.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import httpx  # noqa: F401
    from httpx import ASGITransport, AsyncClient
except ImportError:  # pragma: no cover
    print("the [web] extra is required: uv run --extra web python tests/test_web_raw.py")
    raise SystemExit(2)

from pacifica_obd.web.app import create_app  # noqa: E402
from pacifica_obd.web.settings import Settings  # noqa: E402

BASE = "http://127.0.0.1:8765"
HEADERS = {"X-Pacifica-Request": "1"}


def make_app(**overrides):
    os.environ["PACIFICA_OBD_HOME"] = tempfile.mkdtemp()
    return create_app(Settings(demo=True, read_only=False, **overrides))


def read_only_app():
    os.environ["PACIFICA_OBD_HOME"] = tempfile.mkdtemp()
    return create_app(Settings(demo=True))  # read_only defaults to True


def run_async(app, fn):
    async def wrapper():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=BASE) as client:
            try:
                return await fn(client)
            finally:
                app.state.worker.stop()

    return asyncio.run(wrapper())


def adapter(app):
    return app.state.worker.backend._adapter


def test_a_read_command_is_sent_and_returns_lines():
    app = make_app()

    async def body(client):
        response = await client.post("/api/raw", json={"command": "ATI"}, headers=HEADERS)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["tier"] == "read" and result["sent"] is True
        assert result["command"] == "ATI"
        assert any("STN" in line for line in result["lines"]), result["lines"]
        assert "ATI" in adapter(app).commands, "the read command did not reach the adapter"

        # A framed UDS read is parsed for the response area.
        await client.post("/api/raw", json={"command": "ATSH 7E0"}, headers=HEADERS)
        framed = (await client.post("/api/raw", json={"command": "22 F1 90"},
                                    headers=HEADERS)).json()
        assert framed["response_id"] == "7E8"
        assert framed["uds"].startswith("62F190")
        assert framed["frames"] and framed["frames"][0]["response_id"] == "7E8"

    run_async(app, body)


def test_a_deny_command_is_refused_and_nothing_reaches_the_adapter():
    app = make_app()

    async def body(client):
        for command in ("04", "2E F1 90 01", "27 11", "31 01 02 03", "ATZ", "ATSP0",
                        "ATFCSM1", "ATWM112233", "ATSW96"):
            before = list(adapter(app).commands)
            response = await client.post("/api/raw", json={"command": command},
                                         headers=HEADERS)
            assert response.status_code == 403, (command, response.status_code, response.text)
            error = response.json()["error"]
            assert error["kind"] == "refused" and error["status"] == "refused"
            assert error["detail"]["tier"] == "deny"
            assert error["detail"]["command"]
            assert adapter(app).commands == before, (command, "reached the adapter")

        # A malformed command is a 400, and still never reaches the adapter.
        before = list(adapter(app).commands)
        for command in ("ATI\r\n2E F1 90", "22F190;2E", "ATI 22F190", "", "2E" * 40):
            response = await client.post("/api/raw", json={"command": command},
                                         headers=HEADERS)
            assert response.status_code == 400, (command, response.status_code)
            assert response.json()["error"]["kind"] == "validation"
        assert adapter(app).commands == before

    run_async(app, body)


def test_a_confirm_command_needs_a_token_and_a_typed_acknowledgement():
    app = make_app()

    async def body(client):
        # No token: refused, nothing sent.
        missing = await client.post("/api/raw/confirm",
                                    json={"confirm_token": "x" * 24,
                                          "acknowledgement": "ATCFC0"}, headers=HEADERS)
        assert missing.status_code == 409, missing.text
        assert missing.json()["error"]["kind"] == "confirm"
        assert adapter(app).commands == []

        # The send endpoint previews the exact bytes and returns a token.
        preview = (await client.post("/api/raw", json={"command": "atcfc0"},
                                     headers=HEADERS)).json()
        assert preview["tier"] == "confirm" and preview["sent"] is False
        assert preview["request"] == "ATCFC0"
        assert preview["acknowledgement"] == "ATCFC0"
        assert preview["confirm_token"]
        assert adapter(app).commands == [], "a preview must send nothing"

        # The wrong acknowledgement does not consume the token and sends nothing.
        wrong = await client.post("/api/raw/confirm",
                                  json={"confirm_token": preview["confirm_token"],
                                        "acknowledgement": "ATCAF0"}, headers=HEADERS)
        assert wrong.status_code == 409
        assert wrong.json()["error"]["status"] == "acknowledgement"
        assert adapter(app).commands == []

        # An acknowledgement containing control characters is refused too.
        control = await client.post("/api/raw/confirm",
                                    json={"confirm_token": preview["confirm_token"],
                                          "acknowledgement": "AT\r\nCFC0"}, headers=HEADERS)
        assert control.status_code == 409
        assert adapter(app).commands == []

        # The typed acknowledgement names the exact bytes; the token is single-use.
        confirmed = (await client.post("/api/raw/confirm",
                                       json={"confirm_token": preview["confirm_token"],
                                             "acknowledgement": "at cfc0"}, headers=HEADERS)).json()
        assert confirmed["sent"] is True and confirmed["tier"] == "confirm"
        assert confirmed["audit"]["stored"] is True
        assert adapter(app).commands.count("ATCFC0") == 1

        reuse = await client.post("/api/raw/confirm",
                                  json={"confirm_token": preview["confirm_token"],
                                        "acknowledgement": "ATCFC0"}, headers=HEADERS)
        assert reuse.status_code == 409
        assert reuse.json()["error"]["status"] == "consumed"
        assert adapter(app).commands.count("ATCFC0") == 1, "the token was reused"

    run_async(app, body)


def test_a_token_binds_the_exact_bytes():
    app = make_app()

    async def body(client):
        preview = (await client.post("/api/raw", json={"command": "ATCFC0"},
                                     headers=HEADERS)).json()
        # A tampered body field is ignored: only the token names the bytes.
        result = (await client.post("/api/raw/confirm",
                                    json={"confirm_token": preview["confirm_token"],
                                          "acknowledgement": "ATCFC0",
                                          "command": "2E F1 90 01", "tier": "read"},
                                    headers=HEADERS)).json()
        assert result["command"] == "ATCFC0"
        assert "2EF19001" not in adapter(app).commands
        assert adapter(app).commands.count("ATCFC0") == 1

    run_async(app, body)


def test_a_confirm_is_audited_with_the_exact_bytes_and_outcome():
    app = make_app()

    async def body(client):
        preview = (await client.post("/api/raw", json={"command": "ATCFC0"},
                                     headers=HEADERS)).json()
        await client.post("/api/raw/confirm",
                          json={"confirm_token": preview["confirm_token"],
                                "acknowledgement": "ATCFC0"}, headers=HEADERS)
        rows = (await client.get("/api/db/events")).json()["rows"]
        sends = [row for row in rows if row["kind"] == "raw-send"]
        assert sends, "the raw send was not audited"
        assert any(json.loads(row["detail"])["detail"]["command"] == "ATCFC0"
                   and json.loads(row["detail"])["detail"]["sent"] is True
                   for row in sends)

    run_async(app, body)


def test_a_token_is_invalidated_by_an_adapter_generation_change():
    app = make_app()

    async def body(client):
        preview = (await client.post("/api/raw", json={"command": "ATCFC0"},
                                     headers=HEADERS)).json()
        app.state.worker.backend.generation += 1  # a disconnect/reconnect
        response = await client.post("/api/raw/confirm",
                                     json={"confirm_token": preview["confirm_token"],
                                           "acknowledgement": "ATCFC0"}, headers=HEADERS)
        assert response.status_code == 409
        assert response.json()["error"]["status"] == "generation_mismatch"
        assert "ATCFC0" not in adapter(app).commands

    run_async(app, body)


def test_the_read_tier_works_in_read_only_mode_but_confirm_does_not():
    app = read_only_app()

    async def body(client):
        reading = await client.post("/api/raw", json={"command": "ATI"}, headers=HEADERS)
        assert reading.status_code == 200, reading.text
        assert reading.json()["tier"] == "read" and reading.json()["sent"] is True
        assert "ATI" in adapter(app).commands

        before = list(adapter(app).commands)
        refused = await client.post("/api/raw", json={"command": "ATCFC0"},
                                    headers=HEADERS)
        assert refused.status_code == 403, refused.text
        assert refused.json()["error"]["kind"] == "readonly"
        assert adapter(app).commands == before

        # A deny is still a deny even in read-only mode.
        assert (await client.post("/api/raw", json={"command": "04"},
                                  headers=HEADERS)).status_code == 403

    run_async(app, body)


def test_the_allowlist_endpoint_is_the_shared_source_of_truth():
    app = make_app()

    async def body(client):
        payload = (await client.get("/api/raw/allowlist")).json()
        assert set(payload) >= {"read", "confirm", "deny", "max_length", "reject"}
        assert payload["read_only"] is False
        deny_codes = {row["code"] for row in payload["deny"]["uds_services"]}
        assert {"27", "2E", "31"} <= deny_codes
        assert {row["code"] for row in payload["read"]["uds_services"]} >= {"10", "19", "22", "3E"}

        # With ?command= it classifies, which is how the badge stays in step.
        classified = (await client.get("/api/raw/allowlist?command=22F190")).json()
        assert classified["decision"]["tier"] == "read"
        denied = (await client.get("/api/raw/allowlist",
                                   params={"command": "27 11"})).json()
        assert denied["decision"]["tier"] == "deny"
        assert denied["decision"]["pointer"] == "security"

    run_async(app, body)


def test_the_raw_routes_require_the_csrf_header():
    app = make_app()

    async def body(client):
        for path, payload in (("/api/raw", {"command": "ATI"}),
                              ("/api/raw/confirm", {"confirm_token": "x" * 24,
                                                    "acknowledgement": "ATI"})):
            response = await client.post(path, json=payload)
            assert response.status_code == 403, (path, response.status_code)
            assert response.json()["error"]["code"] == "missing_request_header"
        assert adapter(app).commands == []
        # The allowlist read needs no header (it is a GET).
        assert (await client.get("/api/raw/allowlist")).status_code == 200

    run_async(app, body)


def test_the_caf0_framing_bypass_is_closed():
    """ATCAF0 is refused, and framing is still restored before every hex send.

    With auto-formatting off the adapter sends the hex bytes literally, so the
    first data byte becomes the ISO-TP PCI and ``0104000000000000`` would deliver
    service ``04`` (clear DTCs) while classifying as a mode-01 read. Two
    independent defences: ATCAF0 is deny-tier, and every hex request re-asserts
    ``ATCAF1`` first (in case framing was left off by something else).
    """
    app = make_app()

    async def body(client):
        refused = await client.post("/api/raw", json={"command": "ATCAF0"},
                                    headers=HEADERS)
        assert refused.status_code == 403, refused.status_code
        assert refused.json()["error"]["code"] == "raw_refused"
        assert adapter(app).commands == [], "a deny must not reach the adapter"

        before = len(adapter(app).commands)
        result = (await client.post("/api/raw", json={"command": "0104000000000000"},
                                    headers=HEADERS)).json()
        assert result["tier"] == "read"
        sent = adapter(app).commands[before:]
        assert sent, "nothing was sent"
        assert sent[0] == "ATCAF1", "framing was not restored before a hex send"
        assert "0104000000000000" in sent
        assert sent.index("ATCAF1") < sent.index("0104000000000000")

    run_async(app, body)


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"ok  {name}")
    print("all raw-console checks passed")
