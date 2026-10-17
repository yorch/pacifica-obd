"""Pure UDS / ISO-TP protocol logic for pacifica-obd.

This module contains no I/O, no printing, and no CLI concerns: it turns raw
ELM327 response lines into validated UDS payloads, decodes DTCs, and reads data
identifiers through any object exposing ``cmd(command, wait=...) -> list[str]``
(the adapter layer). Everything here is safe to unit-test with synthetic frames.
"""

from __future__ import annotations

import re

# ST/ELM protocol presets: 33 = ISO 15765 11-bit 500k (HS/CAN-C),
# 53 = ISO 15765 11-bit 125k (MS/CAN-IHS).
BUS_PROTOCOL = {"can-c": 33, "can-ihs": 53}

_HEADER_LEN = 3  # 11-bit CAN; our STP presets (33/53) are 11-bit


# --------------------------------------------------------------------------- #
# ISO-TP reassembly
# --------------------------------------------------------------------------- #
def collect_frames(lines: list[str], header_len: int = _HEADER_LEN) -> list[tuple[str, list[bytes]]]:
    """Group response lines by CAN header, preserving arrival order.

    Handles both spaced ("7E8 06 50 03") and compact ("7E8065003") formats.
    """
    frames: dict[str, list[bytes]] = {}
    order: list[str] = []
    for line in lines:
        tokens = line.split()
        if len(tokens) >= 2:
            header, data = tokens[0], "".join(tokens[1:])
        else:
            token = tokens[0] if tokens else ""
            if len(token) <= header_len or not re.fullmatch(r"[0-9A-Fa-f]+", token):
                continue
            header, data = token[:header_len], token[header_len:]
        header = header.upper()
        if not re.fullmatch(r"[0-9A-Fa-f]{3}|[0-9A-Fa-f]{8}", header):
            continue
        if not re.fullmatch(r"[0-9A-Fa-f]+", data) or len(data) % 2:
            continue
        if len(data) > 16:  # classic CAN carries at most 8 bytes per frame
            continue
        if header not in frames:
            frames[header] = []
            order.append(header)
        frames[header].append(bytes.fromhex(data))
    return [(h, frames[h]) for h in order]


def messages_from_frames(frames: list[bytes]) -> list[str]:
    """Split a frame list into one or more complete, validated UDS payloads."""
    messages: list[str] = []
    i = 0
    n = len(frames)
    while i < n:
        first = frames[i]
        i += 1
        if not first or len(first) > 8:  # classic CAN: at most 8 bytes/frame
            continue
        pci = first[0] >> 4
        if pci == 0:  # single frame: length must be 1..7
            length = first[0] & 0x0F
            if not 1 <= length <= 7:
                continue
            data = first[1:1 + length]
            if len(data) == length:
                messages.append(data.hex().upper())
        elif pci == 1:  # first frame: always a full 8-byte classic-CAN frame
            if len(first) != 8:
                continue
            length = ((first[0] & 0x0F) << 8) | first[1]
            if not 8 <= length <= 0xFFF:
                continue
            payload = bytearray(first[2:])
            expected = 1
            while i < n and len(payload) < length:
                cf = frames[i]
                if not cf:
                    i += 1
                    continue
                if len(cf) > 8 or (cf[0] >> 4) != 2 or (cf[0] & 0x0F) != expected:
                    break  # invalid / missing / duplicated / reordered -> abandon
                if len(cf) < 8 and len(payload) + (len(cf) - 1) < length:
                    break  # a non-final consecutive frame must be full
                payload += cf[1:]
                expected = (expected + 1) & 0x0F
                i += 1
            if len(payload) >= length:
                messages.append(bytes(payload[:length]).hex().upper())
        # stray consecutive/flow-control frames are ignored
    return messages


def parse_uds_frame(lines: list[str]) -> tuple[str | None, str | None]:
    """Return (response_header, uds_hex), reassembling ISO-TP if needed.

    Prefers the last non-responsePending (NRC 0x78) message so a pending
    acknowledgement does not mask the real answer.
    """
    for header, frames in collect_frames(lines):
        messages = messages_from_frames(frames)
        if not messages:
            continue
        final = [
            m for m in messages
            if not (m.startswith("7F") and len(m) >= 6 and m[4:6] == "78")
        ]
        return header, (final[-1] if final else messages[-1])
    return None, None


def ascii_payload(uds_hex: str, prefix: str) -> str | None:
    """Decode the printable ASCII payload after `prefix`, or None."""
    if not uds_hex.startswith(prefix):
        return None
    body = uds_hex[len(prefix):]
    if not body or len(body) % 2:
        return None
    try:
        raw = bytes.fromhex(body)
    except ValueError:
        return None
    if any(byte < 0x20 or byte > 0x7E for byte in raw):  # reject non-printable
        return None
    return raw.decode("ascii").strip() or None


def probe_ids(elm, start: int, end: int, probe: str, wait: float):
    """Yield responding module IDs by sending `probe` to each request ID."""
    service = int(probe[:2], 16)
    for rid in range(start, end + 1):
        elm.cmd("ATSH %03X" % rid)
        header, uds = parse_uds_frame(elm.cmd(probe, wait=wait))
        if header is None:
            continue
        if uds.startswith("%02X" % (service + 0x40)):
            kind = "positive"
        elif uds.startswith("7F"):
            kind = "negative"
        else:
            kind = "other"
        yield {
            "request_id": "%03X" % rid,
            "response_id": header,
            "kind": kind,
            "session_response": uds,
        }


# --------------------------------------------------------------------------- #
# DTCs (UDS 0x19 0x02)
# --------------------------------------------------------------------------- #
DTC_STATUS_BITS = [
    (0x01, "testFailed"),
    (0x02, "testFailedThisCycle"),
    (0x04, "pending"),
    (0x08, "confirmed"),
    (0x10, "testNotCompletedSinceClear"),
    (0x20, "testFailedSinceClear"),
    (0x40, "testNotCompletedThisCycle"),
    (0x80, "warningLamp"),
]


def decode_dtc(b0: int, b1: int, b2: int) -> tuple[str, int]:
    """Decode a 3-byte UDS DTC into (e.g. 'P0300', failure_type_byte)."""
    letter = "PCBU"[(b0 >> 6) & 0x03]
    number = ((b0 & 0x3F) << 8) | b1
    return f"{letter}{number:04X}", b2


def classify_dtc_response(uds_hex: str) -> str:
    """Classify a 0x19 0x02 reply: ok | negative | unexpected | malformed."""
    if uds_hex.startswith("7F"):
        return "negative"
    if not uds_hex.startswith("5902"):
        return "unexpected"
    body = uds_hex[4:]
    if len(body) % 2:
        return "malformed"
    nbytes = len(body) // 2
    if nbytes == 0 or (nbytes - 1) % 4 != 0:  # mask byte + N*4-byte records
        return "malformed"
    return "ok"


def parse_dtcs(uds_hex: str) -> list[dict]:
    """Parse a well-formed 0x59 0x02 response: 59 02 <mask> (<dtc3><status>)*."""
    if classify_dtc_response(uds_hex) != "ok":
        return []
    records = bytes.fromhex(uds_hex[4:])[1:]  # skip the status-availability mask
    dtcs: list[dict] = []
    for i in range(0, len(records), 4):
        b0, b1, b2, status = records[i:i + 4]
        code, failure_type = decode_dtc(b0, b1, b2)
        dtcs.append(
            {
                "code": code,
                "failure_type": "%02X" % failure_type,
                "status": "%02X" % status,
                "flags": [name for bit, name in DTC_STATUS_BITS if status & bit],
            }
        )
    return dtcs


def read_dtcs(elm, expect_header: str | None = None, wait: float = 0.5) -> tuple[str | None, str]:
    """Read DTCs (0x19 02 FF) from the currently addressed module."""
    lines = elm.cmd("1902FF", wait=wait)
    header, uds = parse_uds_frame(lines)
    if header is None:
        return None, ""
    if expect_header and header != expect_header:
        return header, uds  # caller decides; mismatch surfaced via responder check
    return header, uds


# --------------------------------------------------------------------------- #
# Data identifiers (UDS 0x22)
# --------------------------------------------------------------------------- #
DID_NAMES = {
    0xF186: "Active diagnostic session",
    0xF187: "Spare part number",
    0xF188: "ECU software number",
    0xF189: "ECU software version",
    0xF18A: "System supplier identifier",
    0xF18C: "ECU serial number",
    0xF190: "VIN",
    0xF191: "ECU hardware number",
}
INFO_DIDS = list(DID_NAMES)

DID_STATUS_TEXT = {
    "no_response": "no response",
    "can_error": "CAN error",
    "negative": "negative response",
    "pending": "pending (no final response)",
    "unexpected": "unexpected response",
    "wrong_responder": "wrong responder",
    "malformed": "malformed data",
}


def enter_session(elm, rid: str | None = None, wait: float = 0.5) -> str | None:
    """Send 0x10 0x03; return the ECU's response header and filter RX to it."""
    lines = elm.cmd("1003", wait=wait)
    header, _ = parse_uds_frame(lines)
    if header:
        elm.cmd("ATCRA " + header)
    return header


def read_did(elm, did: int, wait: float, expect_header: str | None = None) -> dict:
    """Read one DID (0x22) from the currently addressed module."""
    request = "22%04X" % did
    lines = elm.cmd(request, wait=wait)
    header, uds = parse_uds_frame(lines)
    record = {
        "did": "%04X" % did,
        "name": DID_NAMES.get(did),
        "response_id": header,
        "raw": uds,
    }
    if header is None:
        record["status"] = "can_error" if any(
            "CAN ERROR" in line.upper() or "BUFFER FULL" in line.upper() for line in lines
        ) else "no_response"
        record["raw_lines"] = lines
        return record
    if expect_header and header != expect_header:
        record["status"] = "wrong_responder"
        return record
    if uds.startswith("7F"):
        if len(uds) >= 6 and uds[2:4] == "22":
            record["status"] = "pending" if uds[4:6] == "78" else "negative"
        else:
            record["status"] = "malformed"
        return record
    prefix = "62%04X" % did
    if not uds.startswith(prefix):
        record["status"] = "unexpected"
        return record
    data_hex = uds[len(prefix):]
    if not data_hex:
        record["status"] = "malformed"
        return record
    record["status"] = "ok"
    record["hex"] = data_hex
    record["ascii"] = ascii_payload(uds, prefix)
    return record


def did_value(record: dict) -> str:
    if record["status"] != "ok":
        return DID_STATUS_TEXT.get(record["status"], record["status"])
    return record.get("hex", "")


def did_ascii(record: dict) -> str:
    return (record.get("ascii") or "") if record["status"] == "ok" else ""


# Back-compat aliases for the pre-refactor private names.
_BUS_PROTOCOL = BUS_PROTOCOL
_INFO_DIDS = INFO_DIDS
_collect_frames = collect_frames
_messages_from_frames = messages_from_frames
_parse_uds_frame = parse_uds_frame
_ascii_payload = ascii_payload
_probe_ids = probe_ids
_DTC_STATUS_BITS = DTC_STATUS_BITS
_decode_dtc = decode_dtc
_classify_dtc_response = classify_dtc_response
_parse_dtcs = parse_dtcs
_DID_NAMES = DID_NAMES
_DID_STATUS_TEXT = DID_STATUS_TEXT
_read_did = read_did
_did_value = did_value
_did_ascii = did_ascii
_enter_session = enter_session
