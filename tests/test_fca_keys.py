"""Seed->key vectors + selector coverage for the built-in FCA provider.

Runs with no vehicle and no adapter (the whole module is pure). The expected
keys below are the original C# ground truth captured from the operator's own
licensed copy; see ``docs/fca-keys.md`` for how they were produced.

    uv run python tests/test_fca_keys.py
"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from unittest import mock

from pacifica_obd import cli, fca_keys
from pacifica_obd.fca_keys import FcaKeyError

SEED = 0x12345678  # 12 34 56 78

# algorithm -> the key the original C# returns for SEED.
VECTORS: list[tuple[str, int]] = [
    ("CalculateKeyS1_IPC_PN_Sup00C6_Var49_AlgID0257_V01", 0xBA32A3C0),
    ("CalculateKeyS5_IPC_PN_Sup00C6_Var49_AlgID0258_V01", 0xD8618EDE),
    ("CalculateKeyS1_IPC_PN_Sup0087_Var47_AlgID01A5_V01", 0x37A80259),
    ("CalculateKeyS1_RFH_PN_Sup0040_Var45_AlgID0240_V01", 0x5472BAAE),
    ("CalculateKeyS1_RFH_PN_Sup00C6_Var4A_AlgID02A7_V01", 0xCC8A4751),
    ("CalculateKeyS1_RFH_PN_Sup00C6_Var48_AlgID0293_V01", 0x3254D0B9),
    ("CalculateKeyS1_ABS_PN_Sup0027_Var40_V01", 0xF69E7C3A),
    ("CalculateKeyS1_BPCM_PN_Sup00DD_Var40_AlgID017D_V01", 0x5B4EBEF4),
    ("CalculateKeyS1_LBSS_PN_Sup00F1_Var0001_AlgID0329_V02", 0x2E5A13EB),
    ("CalculateKeyS1_ICS_PN_Sup0032_Var40_V01", 0x95A1ADFE),
    ("CalculateKeyS1_ESM_PN_Sup0007_Var41_V02", 0xC76B60FA),
    ("CalculateKeyS1_APM_PN_Sup00D6_Var40_AlgID01B8_V01", 0x4E28C521),
]

# Every ECU that has a shipped selector row, with the levels it covers.
SHIPPED_MODULES: dict[str, tuple[int, ...]] = {
    "ABS_PN": (1,),
    "AMP_PN": (1, 5),
    "ANC_PN": (1, 5),
    "APM_PN": (1, 5),
    "BPCM_PN": (1, 5),
    "CVPM_PN": (1, 5),
    "EPS_PN": (1, 5),
    "ESM_PN": (1, 5),
    "HCP_PN": (1, 5),
    "ICS_PN": (1, 5),
    "IPC_PN": (1, 5, 61, 65),
    "LBSS_PN": (1, 5),
    "OCM_PN": (1, 5),
    "ORC_PN": (1,),
    "RBSS_PN": (1, 5),
    "RFH_PN": (1, 3, 5, 61),
}

# target that must resolve to exactly one algorithm.
RESOLVED: list[tuple[str, int, dict, str]] = [
    ("IPC_PN", 1, {"device_id": 11872},
     "CalculateKeyS1_IPC_PN_Sup00C6_Var49_AlgID0257_V01"),
    ("IPC_PN", 1, {"device_id": 11871},
     "CalculateKeyS1_IPC_PN_Sup0087_Var47_AlgID01A5_V01"),
    ("IPC_PN", 5, {"device_id": 11872},
     "CalculateKeyS5_IPC_PN_Sup00C6_Var49_AlgID0258_V01"),
    ("LBSS_PN", 1, {"device_id": 8467},
     "CalculateKeyS1_LBSS_PN_Sup00F1_Var0001_AlgID0329_V02"),
    ("RBSS_PN", 1, {"device_id": 8468},
     "CalculateKeyS1_LBSS_PN_Sup00F1_Var0001_AlgID0329_V02"),
    ("ABS_PN", 1, {"variant_id": 64}, "CalculateKeyS1_ABS_PN_Sup0027_Var40_V01"),
    ("BPCM_PN", 1, {"variant_id": 64},
     "CalculateKeyS1_BPCM_PN_Sup00DD_Var40_AlgID017D_V01"),
    ("ICS_PN", 1, {"variant_id": 64}, "CalculateKeyS1_ICS_PN_Sup0032_Var40_V01"),
    ("ESM_PN", 1, {"variant_id": 65}, "CalculateKeyS1_ESM_PN_Sup0007_Var41_V02"),
    ("ANC_PN", 1, {"variant_id": 65},
     "CalculateKeyS1_ANC_PN_Sup0062_Var41_AlgID0142_V01"),
    ("CVPM_PN", 1, {"variant_id": 64},
     "CalculateKeyS1_CVPM_PN_Sup0049_Var40_AlgID019F_V01"),
    ("OCM_PN", 1, {"variant_id": 64}, "CalculateKeyS1_OCM_PN_Sup0040_V02"),
    ("APM_PN", 1, {"variant_id": 64},
     "CalculateKeyS1_APM_PN_Sup00D6_Var40_AlgID01B8_V01"),
    ("ORC_PN", 1, {"variant_id": 64}, "CalculateKeyS1_ORC_PN_Sup0003_Var40_V01"),
    # an 8-byte-seed algorithm: the provider must build 8 bytes, not 4.
    ("ORC_PN", 1, {"variant_id": 65}, "CalculateKeyS1_ORC_PN_Sup0079_Var41_V02"),
]


# --------------------------------------------------------------------------- #
# algorithm vectors (the transpiled code against the original C#)
# --------------------------------------------------------------------------- #
def test_algorithm_vectors_match_csharp():
    for name, want in VECTORS:
        assert name in fca_keys.ALGORITHMS, f"missing algorithm {name}"
        got = int.from_bytes(fca_keys.ALGORITHMS[name](fca_keys._seed_bytes(SEED)), "big")
        assert got == want, f"{name}: {got:08X} != {want:08X}"


def test_known_ipc_and_rfh_vectors():
    # The two rows the selector document calls out.
    assert fca_keys.compute_key("IPC_PN", 1, SEED, device_id=11872) == 0xBA32A3C0
    assert fca_keys.compute_key("IPC_PN", 1, SEED, device_id=11871) == 0x37A80259
    # RFH's three rows share one device id, so it must refuse rather than guess.
    try:
        fca_keys.compute_key("RFH_PN", 1, SEED, device_id=14039)
    except FcaKeyError:
        pass
    else:
        raise AssertionError("RFH without a part number must refuse")


# --------------------------------------------------------------------------- #
# selector: coverage of the RU module set
# --------------------------------------------------------------------------- #
def test_shipped_modules_and_levels():
    got = {e[0] for e in fca_keys._SELECTOR}
    assert got == set(SHIPPED_MODULES), sorted(got ^ set(SHIPPED_MODULES))
    for module, levels in SHIPPED_MODULES.items():
        seen = tuple(sorted({lv for (m, lv, *_rest) in fca_keys._SELECTOR if m == module}))
        assert seen == levels, f"{module}: {seen} != {levels}"


def test_every_algorithm_is_reachable_and_vice_versa():
    reachable = {e[4] for e in fca_keys._SELECTOR}
    assert reachable == set(fca_keys.ALGORITHMS)
    for entry in fca_keys._SELECTOR:
        assert entry[2] in {"device", "variant", "supplier"}
        assert entry[1] % 2 == 1, "security levels are odd"


def test_resolved_targets_choose_the_expected_algorithm():
    for module, level, kwargs, want in RESOLVED:
        info = fca_keys.explain(module, level, **kwargs)
        assert info["status"] == "resolved", (module, level, info["status"])
        assert info["algorithm"] == want, (module, want, info["algorithm"])
        provider = fca_keys.provider_for(module, level, **kwargs)
        assert isinstance(provider(SEED), int)


def test_provider_roundtrip_and_ctx_is_ignored():
    provider = fca_keys.provider_for("IPC_PN", 1, device_id=11872)
    assert provider(SEED, ecu="IPC_PN", noise=1) == 0xBA32A3C0
    assert provider(0x00000000) == int("3BD45340", 16)


def test_eight_byte_seed_algorithm_rebuilds_eight_bytes():
    # ORC Var41 reads an 8-byte seed; rebuilding only 4 raises IndexError.
    name = "CalculateKeyS1_ORC_PN_Sup0079_Var41_V02"
    assert fca_keys._SEED_WIDTH[name] == 8
    provider = fca_keys.provider_for("ORC_PN", 1, variant_id=65)
    assert provider(0x12345678) == 0x822E3CEA  # C#: 00000000 12345678
    assert provider(0x12345678, width=8) == 0x822E3CEA
    assert fca_keys._seed_bytes(0x12345678, 8) == bytearray(
        b"\x00\x00\x00\x00\x12\x34\x56\x78")


def test_seed_width_table_is_complete():
    assert set(fca_keys._SEED_WIDTH) == set(fca_keys.ALGORITHMS)
    assert set(fca_keys._SEED_WIDTH.values()) <= {4, 8}
    assert set(fca_keys._KEY_WIDTH) == set(fca_keys.ALGORITHMS)
    assert set(fca_keys._KEY_WIDTH.values()) == {4}


# --------------------------------------------------------------------------- #
# refusals: never pick when several algorithms (or none) fit
# --------------------------------------------------------------------------- #
def test_ambiguous_ipc_without_a_device_id():
    info = fca_keys.explain("IPC_PN", 1)
    assert info["status"] == "ambiguous"
    assert len(info["candidates"]) == 2
    try:
        fca_keys.provider_for("IPC_PN", 1)
    except FcaKeyError:
        pass
    else:
        raise AssertionError("provider_for must refuse an ambiguous target")


def test_rfh_stays_ambiguous_even_with_the_device_id():
    assert "RFH_PN" in fca_keys._PARTIAL
    for level in (1, 3, 5, 61):
        info = fca_keys.explain("RFH_PN", level, device_id=14039)
        assert info["status"] == "ambiguous", (level, info["status"])
    # level 1 has three candidate algorithms; 61 has one but is still refused.
    assert len(fca_keys.explain("RFH_PN", 1, device_id=14039)["candidates"]) == 3
    assert len(fca_keys.explain("RFH_PN", 61, device_id=14039)["candidates"]) == 1


def test_supplier_only_algorithms_refuse_without_a_supplier():
    # EPS/HCP/AMP rows keyed on a supplier id we cannot read stay off.
    assert fca_keys.explain("EPS_PN", 1, variant_id=67)["status"] == "ambiguous"


def test_unmapped_targets():
    assert fca_keys.explain("PENTASTAR", 1)["status"] == "unmapped"
    assert fca_keys.explain("NOT_A_MODULE", 1)["status"] == "unmapped"
    try:
        fca_keys.provider_for("IPC_PN", 7)
    except FcaKeyError:
        pass
    else:
        raise AssertionError("an unmapped level must refuse")


# --------------------------------------------------------------------------- #
# shipped source hygiene
# --------------------------------------------------------------------------- #
def test_shipped_source_has_no_vendor_name():
    text = Path(fca_keys.__file__).read_text(encoding="utf-8").lower()
    for needle in ("alfaobd", "alfa obd"):
        assert needle not in text, f"vendor name leaked into fca_keys.py: {needle}"


def test_import_has_no_side_effects():
    # No adapter, no store, no console: the module only defines names.
    assert callable(fca_keys.provider_for)
    assert isinstance(fca_keys._SELECTOR, tuple)
    assert fca_keys._seed_bytes(0x01020304) == bytearray(b"\x01\x02\x03\x04")
    assert fca_keys._seed_bytes(0x0102030405060708) == bytearray(
        b"\x01\x02\x03\x04\x05\x06\x07\x08")


# --------------------------------------------------------------------------- #
# CLI wiring (offline): built-in provider vs refusal
# --------------------------------------------------------------------------- #
def test_cli_builtin_resolves_with_a_device_id():
    args = cli.build_parser().parse_args(
        ["security", "--module", "742", "--device-id", "11872", "--level", "1"])
    provider, info, _ecu = cli._builtin_key_provider(args)
    assert info["status"] == "resolved"
    assert provider(SEED) == 0xBA32A3C0


def test_cli_builtin_auto_uses_a_single_device_id():
    # LBSS_PN has exactly one device id in the dataset, so no flag is needed.
    args = cli.build_parser().parse_args(["security", "--module", "791", "--level", "1"])
    provider, info, _ecu = cli._builtin_key_provider(args)
    assert info["status"] == "resolved"
    assert info["ids"] == {"device": 8467}
    assert provider is not None


def test_cli_builtin_refuses_ambiguous_without_a_key():
    args = cli.build_parser().parse_args(["security", "--module", "742", "--level", "1"])
    provider, info, _ecu = cli._builtin_key_provider(args)
    assert provider is None
    assert info["status"] == "ambiguous"


class _FakeElm:
    """Replays a multi-frame 8-byte seed and records the send-key command."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def cmd(self, command: str, wait: float | None = None) -> list[str]:
        self.sent.append(command)
        if command == "2701":
            # ISO-TP: first frame (len 0x0A) + one consecutive frame
            return ["7E8 10 0A 67 01 00 00 00 00", "7E8 21 12 34 56 78"]
        if command.startswith("2702"):
            return ["7E8 02 67 02"]
        return ["NO DATA"]


def test_security_access_sends_key_at_the_algorithm_output_width():
    from pacifica_obd import uds

    elm = _FakeElm()
    provider = fca_keys.provider_for("ORC_PN", 1, variant_id=65)
    result = uds.security_access(elm, 1, seedkey=provider)
    assert result["status"] == "unlocked", result
    # 8-byte seed, but the algorithm returns a 4-byte key -> send 4 bytes.
    assert elm.sent[-1] == "2702822E3CEA", elm.sent


def test_cli_json_unlock_refusal_is_structured_stdout():
    # --json must not leak human text to stdout when it refuses to unlock.
    args = cli.build_parser().parse_args(
        ["security", "--module", "742", "--level", "1",
         "--unlock", "--confirm", "--json"])
    buf = io.StringIO()
    code = None
    # The refusal must happen before the adapter is touched.
    with mock.patch.object(cli, "open_elm",
                           side_effect=AssertionError("adapter opened")), \
            contextlib.redirect_stdout(buf):
        try:
            cli.cmd_security(args)
        except SystemExit as exc:
            code = exc.code
    assert code == 2
    payload = json.loads(buf.getvalue())
    assert payload["status"] == "refused"
    assert payload["selector"]["status"] == "ambiguous"


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"ok  {name}")
    print("all fca-keys checks passed")
