"""Tiered model selection (graph-engineering playbook, model selection):
resolution/relation-inference weigh conflicting evidence and should use a
stronger sibling model when the user picked a fast/cheap variant for
extraction — with zero new user-facing setting, reading only what's already
configured in ModelConfig.models.
"""
from app.tasks.extraction import _select_reasoning_model


def test_upgrades_from_fast_variant_to_stronger_sibling():
    assert _select_reasoning_model(
        "deepseek-flash", ["deepseek-v4-pro", "deepseek-flash"]
    ) == "deepseek-v4-pro"


def test_recognizes_common_fast_naming_conventions():
    assert _select_reasoning_model("gpt-4o-mini", ["gpt-4o", "gpt-4o-mini"]) == "gpt-4o"
    assert _select_reasoning_model("claude-haiku", ["claude-sonnet", "claude-haiku"]) == "claude-sonnet"


def test_falls_back_to_same_model_when_not_a_fast_variant():
    """The user already picked the strong model — never downgrade it."""
    assert _select_reasoning_model("deepseek-v4-pro", ["deepseek-v4-pro", "deepseek-flash"]) == "deepseek-v4-pro"


def test_falls_back_to_same_model_when_no_stronger_sibling_configured():
    assert _select_reasoning_model("deepseek-flash", ["deepseek-flash"]) == "deepseek-flash"
    assert _select_reasoning_model("deepseek-flash", []) == "deepseek-flash"
    assert _select_reasoning_model("deepseek-flash", None) == "deepseek-flash"


def test_does_not_pick_another_fast_variant_as_the_upgrade():
    assert _select_reasoning_model(
        "gpt-4o-mini", ["gpt-4o-mini", "claude-haiku"]
    ) == "gpt-4o-mini"
