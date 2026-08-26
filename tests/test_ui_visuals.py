import pandas as pd

from app.ui.visuals import (
    AVAILABILITY_COLOURS,
    DEPARTMENT_TINTS,
    HEALTH_COLOURS,
    availability_label,
    availability_style,
    project_colour,
    style_planning_table,
)


def test_health_and_unplanned_styles_use_shared_semantics():
    frame = pd.DataFrame({
        "Health": list(HEALTH_COLOURS),
        "Unplanned Hours": [10, 5, 0, 0],
    })
    styles = style_planning_table(frame)._compute().ctx
    assert all((row, 0) in styles for row in range(4))
    assert (0, 1) in styles and (1, 1) in styles
    assert (2, 1) not in styles and (3, 1) not in styles


def test_project_colours_are_stable_muted_and_project_specific():
    assert project_colour("ABC") == project_colour("ABC")
    assert project_colour("ABC") != project_colour("XYZ")
    assert project_colour("ABC").startswith("#")


def test_availability_full_partial_zero_and_reassignment_states():
    full = availability_style("RS", 40, {"RS": 40})
    partial = availability_style("RS", 40, {"RS": 24})
    zero = availability_style("RS", 40, {})
    moved = availability_style("PLS", 40, {"RS": 40})
    split = availability_label("PLS", {"PLS": 20, "RS": 20})

    assert DEPARTMENT_TINTS["RS"] in full
    assert AVAILABILITY_COLOURS["partial"][0] in partial
    assert AVAILABILITY_COLOURS["unavailable"][0] in zero
    assert AVAILABILITY_COLOURS["temporary"][0] in moved
    assert split == "20 PLS / 20 RS · PLS → RS"


def test_holiday_reduction_is_presented_as_partial_not_alert():
    holiday_reduced = availability_style("RS", 40, {"RS": 32})
    assert AVAILABILITY_COLOURS["partial"][0] in holiday_reduced
    assert "#FDE8E7" not in holiday_reduced
