"""Tests for diagram identifier extraction / classification (generic patterns).

Run:  ./.venv/bin/python -m pytest tests/test_diagram_config.py -q
   or: ./.venv/bin/python tests/test_diagram_config.py   (falls back to asserts)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.diagram_index import (
    extract_identifiers, classify_figure_kind, infer_unit, page_from_section,
)


def test_identifiers_positive():
    # real strong (usefully-unique) identifiers must be captured
    for tok in ["7214A2", "7214A2W-E", "7304A1", "4S018-922", "A-712",
                "IU-DRV1-X4P", "CN12", "PS1A"]:
        got = extract_identifiers(f"... {tok} ...")
        assert tok.upper() in got, f"missing {tok}: {got}"


def test_short_nonunique_ids_excluded_by_design():
    # ubiquitous short pins/connectors (PS1, CN1) appear on nearly every page,
    # so they are intentionally NOT indexed (noise, not a locator). 宁漏不误连.
    for tok in ["PS1", "CN1", "A-7"]:
        got = extract_identifiers(f"... {tok} ...")
        assert tok.upper() not in got, f"should be excluded: {tok} -> {got}"


def test_identifiers_no_false_connect():
    # plain words / dates / short noise must NOT be captured (宁漏不误连)
    for bad_ctx, forbidden in [
        ("Illumination Switching Unit", "ILLUMINATION"),
        ("2026-07-24 rev", "2026-07-24"),
        ("the wafer stage", "WAFER"),
        ("page 34", "PAGE"),
    ]:
        got = extract_identifiers(bad_ctx)
        assert forbidden not in got, f"false match {forbidden} in {got}"


def test_figure_kind():
    assert classify_figure_kind("照明部ブロック図 No.2") == "block"
    assert classify_figure_kind("総合配線図") == "general_wiring"
    assert classify_figure_kind("STIFMEMX4A基板回路図 No.1") == "board_circuit"
    assert classify_figure_kind("plain text no kind") is None


def test_unit_infer():
    assert infer_unit("IU No.2 制御ラック") == "IU NO.2"
    assert infer_unit("no unit here") is None


def test_page_from_section():
    assert page_from_section("第34页") == 34
    assert page_from_section("第 5307 页") == 5307
    assert page_from_section("") is None


def _run_all():
    fns = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    ok = 0
    for fn in fns:
        try:
            fn(); ok += 1; print(f"  PASS {fn.__name__}")
        except AssertionError as e:
            print(f"  FAIL {fn.__name__}: {e}")
    print(f"{ok}/{len(fns)} passed")
    return ok == len(fns)


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
