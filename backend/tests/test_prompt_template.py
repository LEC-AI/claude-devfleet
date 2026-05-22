"""Tests for prompt_template.build_prompt — owner-dynamic commit attribution.

Critical: the commit prefix is the IP attribution boundary. If a Claude/AI/Anthropic
trailer ever lands in commit output, that's a $1M+ legal liability.
"""

import pytest

import prompt_template


def _base_mission(**over):
    base = {
        "title": "Add /users endpoint",
        "detailed_prompt": "Build the endpoint.",
        "mission_type": "implement",
    }
    base.update(over)
    return base


@pytest.mark.unit
def test_owner_defaults_to_farhan_when_no_creator():
    prompt = prompt_template.build_prompt(_base_mission())
    assert "Farhanfeat(scope):" in prompt
    assert "Farhanfix(scope):" in prompt


@pytest.mark.unit
def test_owner_capitalizes_lowercase_name():
    prompt = prompt_template.build_prompt(_base_mission(created_by_name="hasan"))
    assert "Hasanfeat(scope):" in prompt
    assert "Hasanfix(scope):" in prompt


@pytest.mark.unit
def test_owner_normalizes_uppercase_name():
    prompt = prompt_template.build_prompt(_base_mission(created_by_name="ADIL"))
    # Python's .capitalize() lowercases everything after the first char
    assert "Adilfeat(scope):" in prompt


@pytest.mark.unit
def test_owner_strips_whitespace():
    prompt = prompt_template.build_prompt(_base_mission(created_by_name="  hasan  "))
    assert "Hasanfeat(scope):" in prompt


@pytest.mark.unit
def test_owner_empty_string_falls_back_to_farhan():
    prompt = prompt_template.build_prompt(_base_mission(created_by_name=""))
    assert "Farhanfeat(scope):" in prompt


@pytest.mark.unit
def test_owner_none_falls_back_to_farhan():
    prompt = prompt_template.build_prompt(_base_mission(created_by_name=None))
    assert "Farhanfeat(scope):" in prompt


@pytest.mark.unit
def test_no_ai_attribution_in_output():
    """The prompt template must never instruct agents to add AI attribution."""
    prompt = prompt_template.build_prompt(_base_mission())
    # The forbidden phrases — these must only appear in the "NEVER include" section
    # as instructions, never as actual examples of valid commits
    forbidden_in_examples = [
        "Co-Authored-By: Claude",
        "Generated-By: Claude",
        "Co-Authored-By: Anthropic",
    ]
    # Confirm they appear ONLY in the NEVER section (rejection examples)
    for phrase in forbidden_in_examples:
        if phrase in prompt:
            # Must appear after "NEVER include" marker
            never_idx = prompt.find("NEVER include")
            assert never_idx > 0
            assert prompt.find(phrase) > never_idx


@pytest.mark.unit
def test_all_prefix_types_use_owner():
    """All 7 commit prefixes (feat/fix/update/refactor/test/chore/sync) must be owner-prefixed."""
    prompt = prompt_template.build_prompt(_base_mission(created_by_name="Hasan"))
    for prefix_type in ["feat", "fix", "update", "refactor", "test", "chore"]:
        assert f"Hasan{prefix_type}(scope):" in prompt, f"Missing Hasan{prefix_type}(scope):"
    assert "Hasanchore(sync):" in prompt


@pytest.mark.unit
def test_mission_title_in_prompt():
    prompt = prompt_template.build_prompt(_base_mission(title="My Special Mission"))
    assert "## Mission: My Special Mission" in prompt


@pytest.mark.unit
def test_mission_type_included():
    prompt = prompt_template.build_prompt(_base_mission(mission_type="review"))
    assert "**Type:** review" in prompt


@pytest.mark.unit
def test_detailed_prompt_included():
    prompt = prompt_template.build_prompt(_base_mission(detailed_prompt="Do the thing carefully."))
    assert "Do the thing carefully." in prompt


@pytest.mark.unit
def test_acceptance_criteria_included():
    prompt = prompt_template.build_prompt(
        _base_mission(acceptance_criteria="- Tests pass\n- No regressions")
    )
    assert "## Acceptance Criteria" in prompt
    assert "- Tests pass" in prompt


@pytest.mark.unit
def test_no_acceptance_section_when_empty():
    prompt = prompt_template.build_prompt(_base_mission(acceptance_criteria=""))
    assert "## Acceptance Criteria" not in prompt


@pytest.mark.unit
def test_last_report_resume_context_included():
    last = {
        "what_done": "Built API skeleton",
        "what_open": "Missing /health endpoint",
        "what_tested": "Manual GET /users",
        "what_untested": "Error paths",
        "errors_encountered": "None",
        "next_steps": "Add /health",
    }
    prompt = prompt_template.build_prompt(_base_mission(), last_report=last)
    assert "## Previous Session Context" in prompt
    assert "Built API skeleton" in prompt
    assert "Missing /health endpoint" in prompt
    assert "Continue from where the previous session left off" in prompt


@pytest.mark.unit
def test_no_previous_context_without_last_report():
    prompt = prompt_template.build_prompt(_base_mission())
    assert "## Previous Session Context" not in prompt


@pytest.mark.unit
def test_rebase_discipline_section_present():
    prompt = prompt_template.build_prompt(_base_mission())
    assert "## Git Rebase Discipline (MANDATORY)" in prompt
    assert "git rebase origin/dev" in prompt
    assert "Run this again even if you just did it" in prompt
    assert "git rebase --abort" in prompt


@pytest.mark.unit
def test_validation_discipline_present():
    prompt = prompt_template.build_prompt(_base_mission())
    assert "## Validation Discipline" in prompt
    assert "timeout 300" in prompt
    assert "pnpm --filter" in prompt
    assert "Commit before you validate" in prompt or "Commit your code FIRST" in prompt


@pytest.mark.unit
def test_report_format_section_present():
    prompt = prompt_template.build_prompt(_base_mission())
    assert "---DEVFLEET-REPORT-START---" in prompt
    assert "---DEVFLEET-REPORT-END---" in prompt
    assert "submit_report" in prompt


@pytest.mark.unit
def test_tags_field_included_when_set():
    prompt = prompt_template.build_prompt(_base_mission(tags='["urgent","backend"]'))
    assert '"urgent"' in prompt


@pytest.mark.unit
def test_empty_tags_array_excluded():
    prompt = prompt_template.build_prompt(_base_mission(tags="[]"))
    assert "**Tags:**" not in prompt
