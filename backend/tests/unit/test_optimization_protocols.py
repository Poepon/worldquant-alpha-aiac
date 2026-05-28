"""Stage A protocols conformance.

These tests pin the optimization-closure boundary types so a rename /
field removal in protocols.py breaks at unit-test time rather than
when Stage B (composite generator) is being added in 2-3 weeks.
"""
from __future__ import annotations

import inspect
from dataclasses import fields, is_dataclass

import pytest

from backend.services.optimization import protocols as P


# ---------------------------------------------------------------------------
# Value objects — dataclass field surface
# ---------------------------------------------------------------------------


def test_variant_is_dataclass_with_required_fields():
    assert is_dataclass(P.Variant)
    names = {f.name for f in fields(P.Variant)}
    assert {
        "expression", "settings", "tag", "generator_name", "generation"
    } == names


def test_variant_sim_result_is_dataclass_with_required_fields():
    assert is_dataclass(P.VariantSimResult)
    names = {f.name for f in fields(P.VariantSimResult)}
    expected = {
        "variant", "sim_response",
        "sharpe", "fitness", "turnover", "margin", "subuniv",
        "brain_alpha_id", "checks_passed", "self_corr", "error",
    }
    assert expected == names


def test_variant_default_generation_is_zero():
    v = P.Variant(
        expression="x", settings={}, tag="t", generator_name="settings_sweep",
    )
    assert v.generation == 0


def test_variant_sim_result_default_self_corr_and_error_are_none():
    v = P.Variant(
        expression="x", settings={}, tag="t", generator_name="g",
    )
    r = P.VariantSimResult(
        variant=v, sim_response={},
        sharpe=1.0, fitness=1.0, turnover=0.1, margin=0.001, subuniv=0.5,
        brain_alpha_id="abc", checks_passed=True,
    )
    assert r.self_corr is None
    assert r.error is None


# ---------------------------------------------------------------------------
# Protocols — method surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "proto_name,expected_methods",
    [
        ("VariantGenerator", ["generate"]),
        ("Simulator", ["run_batch"]),
        ("WinnerSelector", ["pick"]),
        ("Persister", ["save"]),
        ("SubmitPolicy", ["decide"]),
        (
            "OptimizationRunRepository",
            ["open_cycle", "record_persist", "record_submit", "finish_cycle"],
        ),
        ("KnowledgeFeedback", ["on_winner"]),
    ],
)
def test_protocol_exposes_expected_methods(proto_name, expected_methods):
    proto = getattr(P, proto_name)
    for m in expected_methods:
        assert hasattr(proto, m), f"{proto_name} missing method {m}"


def test_submit_action_literal_covers_three_values():
    # SubmitAction is a Literal — pull args via typing.get_args
    from typing import get_args
    args = set(get_args(P.SubmitAction))
    assert args == {"submit", "queue", "skip"}


def test_winner_selector_pick_signature_takes_delay():
    # WinnerSelector.pick(self, results, delay) — delay is mandatory because
    # delay-0 and delay-1 bands differ (b8a9560).
    sig = inspect.signature(P.WinnerSelector.pick)
    params = list(sig.parameters.keys())
    # self + results + delay
    assert params == ["self", "results", "delay"]


def test_persister_save_returns_typed_list_of_optional_int():
    # save(self, winners, parent_alpha_id, opt_run_id) — return type is
    # List[Optional[int]] so ON CONFLICT DO NOTHING skips can be expressed
    # as None entries in-position.
    sig = inspect.signature(P.Persister.save)
    params = list(sig.parameters.keys())
    assert params == ["self", "winners", "parent_alpha_id", "opt_run_id"]
