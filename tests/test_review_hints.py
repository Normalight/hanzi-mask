"""Review guidance must describe measurements and actions present in the client."""

import pytest

from ui.review_hints import review_hints


def test_missing_or_invalid_measurements_do_not_invent_a_quality_problem():
    assert review_hints({}) == []
    assert review_hints({"foreground_pixels": "nan", "components": float("inf"),
                         "removed_fraction": "unknown", "isolated_regions_removed": True}) == []


@pytest.mark.parametrize("manual,undo,action", [
    (False, False, "强度"), (True, False, "处理当前图片"), (True, True, "撤销清理"),
])
def test_empty_candidates_are_excluded_and_offer_an_available_recovery_action(manual, undo, action):
    hints = review_hints({"foreground_pixels": 0, "manual_correction": manual}, can_undo=undo)
    assert len(hints) == 1
    assert "默认不采用" in hints[0]
    assert action in hints[0]
    assert "--" not in hints[0]


def test_isolated_stroke_hint_uses_the_checkbox_without_claiming_cleanup_can_restore_strokes():
    text = "；".join(review_hints({"foreground_pixels": 90, "isolated_regions_removed": 2}))
    assert "保留独立点画" in text and "处理当前图片" in text
    assert "取消“采用”" in text
    assert "keep-isolated" not in text and "清理选中色块" not in text


def test_manual_cleanup_does_not_report_historical_recovery_as_a_current_addition():
    metrics = {"foreground_pixels": 50, "stroke_recovery_final_surviving_added_pixels": 8,
               "tip_added_pixels": 4}
    assert any("补入" in hint for hint in review_hints(metrics))
    hints = review_hints({**metrics, "manual_correction": True}, can_undo=True)
    assert all("补入" not in hint for hint in hints)
    assert any("撤销清理" in hint for hint in hints)


def test_merged_background_guidance_does_not_offer_component_cleanup_as_a_solution():
    hints = review_hints({"foreground_pixels": 80, "width": 10, "height": 10})
    assert len(hints) == 1
    assert "粘连" in hints[0] and "取消“采用”" in hints[0]


def test_color_and_many_components_offer_actual_cleanup_action():
    hints = review_hints({"foreground_pixels": 100, "foreground_signal": "color", "components": 8})
    assert len(hints) == 2
    assert all("清理选中色块" in hint and "--" not in hint for hint in hints)
