"""The raw-command classifier: the full tier table and the bypass attempts.

    uv run python tests/test_raw_guard.py

`raw_guard` is the only place the raw console's tiers are defined, and it is
reused by the web route and by the ``/api/raw/allowlist`` endpoint. These checks
pin the table *and* the parser, because the parser is where a raw endpoint is
bypassed: a command that can smuggle a second one is the whole threat.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pacifica_obd import raw_guard as g  # noqa: E402


# Every command that must be sent with no confirmation.
READ_HEX = ["01", "010C", "01 0C", "03", "07", "09 02", "0A",
            "10 01", "1003", "19", "1902FF", "22F190", "22 F1 90",
            "23", "24", "3E", "3E00"]
READ_AT = ["ATI", "ATRV", "ATDP", "ATDPN", "ATAR", "ATS1", "ATS0", "ATH1",
           "ATH0", "ATE0", "ATE1", "ATL1", "ATL0", "ATCAF1", "ATD",
           "ATSH 7E0", "ATSH7E0", "ATCRA 7E8", "ATCRA7E8", "ATTP", "ATTP6"]

# Every command that must be refused, in any mode, forever.
DENY_HEX = ["04", "2E F1 90 01", "2EF19001", "27", "2711", "2702AABB", "31",
            "31010203", "2F", "3D", "34", "35", "36", "37",
            "11", "14", "28", "85",
            # 0x02 is the programming session; reads only need 0x01/0x03.
            "1002"]
DENY_AT = ["ATFCSM1", "ATFCSH 7E0", "ATFCSH7E0", "ATFCSD", "ATFCSD00",
           "ATZ", "ATWS", "ATRST", "ATMA", "ATWM", "ATSW", "ATPPS",
           "ATSP0", "ATSP6",
           # Argument-bearing ELM forms must not slip to the confirm tier.
           "ATWM112233", "ATSW96", "ATPPS01", "ATMA0",
           # Framing off turns the first data byte into the ISO-TP PCI, so a hex
           # request classified as a read could deliver a write service; CAN
           # extended addressing shifts the service byte into the address
           # position, which ATCAF1 does not undo.
           "ATCAF0", "ATCEA", "ATCEA01", "ATCEA 01"]

# Anything else: allowed only with a typed acknowledgement + a confirm token.
CONFIRM_HEX = ["02", "05", "06", "08", "0B", "2101", "2B0F", "4154",
               # 0x2A schedules periodic transmission: a state change, so confirm.
               "2A", "2A01F1",
               # session control with an unusual subfunction
               "1005"]
CONFIRM_AT = ["ATSTFF", "ATCFC0", "ATAL", "ATPC", "ATNL", "AT"]


def test_read_tier():
    for command in READ_HEX + READ_AT:
        decision = g.classify(command)
        assert decision.tier == g.READ, (command, decision)
        assert decision.sendable_now is True, command
        assert decision.valid is True and decision.command, command


def test_deny_tier():
    for command in DENY_HEX + DENY_AT:
        decision = g.classify(command)
        assert decision.tier == g.DENY, (command, decision)
        assert decision.sendable_now is False, command
        assert decision.reason, command


def test_confirm_tier():
    for command in CONFIRM_HEX + CONFIRM_AT:
        decision = g.classify(command)
        assert decision.tier == g.CONFIRM, (command, decision)
        assert decision.sendable_now is False and decision.valid is True, command


def test_deny_reasons_and_pointers():
    security = g.classify("2711")
    assert "0x27" in security.reason and security.pointer == "security"
    assert "seed" in (security.note or "").lower()

    write = g.classify("2EF19001")
    assert write.pointer == "writes" and write.note
    routine = g.classify("31010203")
    assert routine.pointer == "writes"

    for command in ("2F", "3D", "34", "35", "36", "37"):
        decision = g.classify(command)
        assert decision.tier == g.DENY
        assert "not exposed in the browser" in decision.reason, command

    flow = g.classify("ATFCSM1")
    assert flow.tier == g.DENY and "flow control" in flow.reason
    eeprom = g.classify("ATSP0")
    assert eeprom.tier == g.DENY and "EEPROM" in eeprom.reason


def test_04_is_not_read_and_10_03_is():
    assert g.classify("04").tier == g.DENY, "clearing DTCs must never be a read"
    assert g.classify("10 03").tier == g.READ


def test_case_and_whitespace_are_normalised():
    assert g.classify("22f190").command == "22F190"
    assert g.classify("  22 F1 90  ").command == "22F190"
    assert g.classify("22\tf1\n".replace("\n", "") + "90").tier == g.READ
    assert g.classify("\tati ").command == "ATI"
    assert g.normalise("  atsh 7e0 ") == "ATSH7E0"
    # A non-breaking space is whitespace too, so it separates hex bytes.
    assert g.classify("22\u00a0F190").command == "22F190"


def test_bypass_attempts_are_refused():
    bypasses = [
        "ATI\r\n2E F1 90",        # CRLF smuggling the original bypass
        "ATI\r22F190",
        "22F190;2E",              # statement separator
        "ATI;ATZ",
        "22F190\n",               # trailing newline
        "22F190\n2E",
        "ATI\n22F190",            # embedded newline, second command
        "ATI`ATZ",                # backtick
        "ATI 22F190",             # two commands on one line
        "ATI ATRV",
        "22F190 ATI",             # mixed hex then AT
        "ATSH 7E0 22F190",        # AT with two arguments
        "2E" * 40,                # overlong
        "", "   ", "\t",
        "ZZZZ", "ATSH ZZ", "123",    # not hex / odd length
        "\u2028".join(["ATI", "22F190"]),  # unicode line separator
        "\uff12\uff12F190",       # fullwidth digits are not hex
        "22F190\x00",
    ]
    for command in bypasses:
        decision = g.classify(command)
        assert decision.tier == g.DENY, (command, decision)
        assert decision.kind == "invalid", (command, decision)
        assert decision.command == "", (command, decision)
        assert decision.reason, command


def test_empty_and_overlong_messages():
    assert "empty" in g.classify("").reason
    assert "too long" in g.classify("2E" * 40).reason
    assert g.classify("22" * g.MAX_COMMAND_LENGTH).tier == g.DENY


def test_normalise_is_the_exact_bytes_to_send():
    # A spaced hex request and its compact form are the same bytes on the wire.
    assert g.normalise("22 F1 90") == "22F190" == g.normalise("22F190")
    # `ATSH 7E0` is one command; the argument is joined for transmission.
    assert g.normalise("atsh 7e0") == "ATSH7E0"


def test_the_allowlist_is_data_and_matches_the_tables():
    payload = g.allowlist()
    assert set(payload) >= {"max_length", "reject", "read", "confirm", "deny"}
    assert payload["max_length"] == g.MAX_COMMAND_LENGTH

    read_codes = {row["code"] for row in payload["read"]["obd_modes"]}
    assert read_codes == set(g.OBD_READ_MODES)
    read_services = {row["code"] for row in payload["read"]["uds_services"]}
    assert read_services == set(g.UDS_READ_SERVICES)
    assert set(payload["read"]["at_exact"]) == set(g.AT_READ_EXACT)
    assert list(payload["read"]["at_argument"]) == list(g.AT_READ_ARG)

    deny_codes = {row["code"] for row in payload["deny"]["uds_services"]}
    assert deny_codes == set(g.UDS_DENY_SERVICES)
    assert deny_codes >= {"27", "2E", "31", "11", "14", "28", "85"}
    at_prefixes = {row["prefix"] for row in payload["deny"]["at_prefixes"]}
    assert at_prefixes >= {"ATSP", "ATFCSM", "ATFCSH", "ATFCSD"}

    # Every table entry classifies to its own tier (the table and the classifier
    # cannot drift apart). Session control (0x10) is a read only for the sessions
    # a read needs, so it is probed with a valid subfunction.
    for code in read_codes | read_services:
        probe = "1003" if code == "10" else code
        assert g.classify(probe).tier == g.READ, (code, probe)
    for command in g.AT_READ_EXACT:
        assert g.classify(command).tier == g.READ, command
    for code in deny_codes | set(g.OBD_DENY_MODES):
        assert g.classify(code).tier == g.DENY, code
    for command in g.AT_DENY_EXACT:
        assert g.classify(command).tier == g.DENY, command


def test_decision_is_pure_and_never_raises():
    for weird in [None, 123, b"22F190", object(), "AT" * 90]:
        decision = g.classify(weird)
        assert decision.tier in (g.READ, g.CONFIRM, g.DENY)
        assert isinstance(decision.reason, str) and decision.reason


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"ok  {name}")
    print("all raw-guard checks passed")
