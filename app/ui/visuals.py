"""Shared, presentation-only visual language for the planner UI."""
from __future__ import annotations

import colorsys
import hashlib

import pandas as pd


DEPARTMENT_COLOURS = {"RS": "#3978A8", "GIS": "#4F8A5B", "PLS": "#A67C2D"}
DEPARTMENT_TINTS = {"RS": "#E8F1F8", "GIS": "#EAF4EC", "PLS": "#F7F0DF"}
HEALTH_COLOURS = {
    "Unplanned": ("#FDE8E7", "#A52622"),
    "Under-resourced": ("#FFF0D5", "#8A5200"),
    "Well-resourced": ("#E5F4E8", "#226B35"),
    "Over-resourced": ("#EEE9FA", "#59409A"),
}
AVAILABILITY_COLOURS = {
    "partial": ("#FFF3D6", "#755000"),
    "unavailable": ("#ECEFF1", "#59636B"),
    "temporary": ("#F1EAFB", "#62429B"),
}
CAPACITY_COLOURS = {"within": "#536471", "shortage": "#C43D3D"}
CAPACITY_STATUS_COLOURS = {
    "Within capacity": ("#EDF2F4", "#42515A"),
    "Capacity risk": ("#FFF0D5", "#8A5200"),
    "Over capacity": ("#FDE8E7", "#A52622"),
}
INTERNAL_ACTIVITY_COLOUR = "#8B9298"


def project_colour(project_code: str) -> str:
    """Return a stable muted colour without borrowing discipline semantics."""
    digest = int(hashlib.sha256(str(project_code).encode("utf-8")).hexdigest()[:8], 16)
    hue = (digest % 360) / 360
    saturation = 0.28 + ((digest >> 9) % 9) / 100
    lightness = 0.52 + ((digest >> 17) % 9) / 100
    red, green, blue = colorsys.hls_to_rgb(hue, lightness, saturation)
    return f"#{round(red * 255):02X}{round(green * 255):02X}{round(blue * 255):02X}"


def style_planning_table(frame: pd.DataFrame) -> pd.io.formats.style.Styler:
    """Emphasise semantic exceptions while leaving ordinary numeric cells neutral."""
    def cell(value: object, column: str) -> str:
        if column in {"Health", "Health status"} and str(value) in HEALTH_COLOURS:
            background, foreground = HEALTH_COLOURS[str(value)]
            return f"background-color:{background};color:{foreground};font-weight:600"
        if column == "Capacity Status" and str(value) in CAPACITY_STATUS_COLOURS:
            background, foreground = CAPACITY_STATUS_COLOURS[str(value)]
            return f"background-color:{background};color:{foreground};font-weight:600"
        if column == "Unplanned Hours" and pd.notna(value) and float(value) > 0:
            return "background-color:#FDE8E7;color:#A52622;font-weight:700"
        return ""

    return frame.style.apply(
        lambda column: [cell(value, str(column.name)) for value in column], axis=0
    ).format(precision=1, na_rep="—")


def availability_label(home: str, contributions: dict[str, float]) -> str:
    parts = [f"{hours:g} {department}" for department, hours in contributions.items() if hours > .005]
    if not parts:
        return "0 · Unavailable"
    moved = any(department != home and hours > .005 for department, hours in contributions.items())
    label = " / ".join(parts)
    destinations = [department for department, hours in contributions.items() if department != home and hours > .005]
    return f"{label} · {home} → {' + '.join(destinations)}" if moved else label


def availability_style(home: str, weekly_hours: float, contributions: dict[str, float]) -> str:
    total = sum(float(value) for value in contributions.values())
    if total <= .005:
        background, foreground = AVAILABILITY_COLOURS["unavailable"]
    elif any(department != home and hours > .005 for department, hours in contributions.items()):
        background, foreground = AVAILABILITY_COLOURS["temporary"]
    elif total < float(weekly_hours) - .005:
        background, foreground = AVAILABILITY_COLOURS["partial"]
    else:
        background, foreground = DEPARTMENT_TINTS.get(home, "#F5F5F5"), DEPARTMENT_COLOURS.get(home, "#333333")
    return f"background-color:{background};color:{foreground};font-weight:600"
