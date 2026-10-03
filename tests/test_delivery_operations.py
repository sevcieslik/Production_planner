from app.integrations.delivery_operations import (
    parse_flow_values,
    parse_rates_front_page,
    parse_rates_project_values,
)


def test_parse_flow_values_reads_three_visible_blocks():
    values = [
        ["Production Flow"],
        ["Current production queues"],
        ["Latest source snapshot: 01/10/2026 08:00"],
        ["Current Production State"],
        [],
        ["Project", "TPC Outstanding", "GIS Ready", "PLS WIP"],
        ["HONI", "12.5 km", "7.0 km", "3.2 km"],
        [],
        ["Movement Since Tuesday 15:00"],
        ["29/09/2026 15:00 → 01/10/2026 08:00"],
        [],
        ["Project", "TPC Started", "GIS Processed"],
        ["HONI", "4.0 km", "8.5 km"],
        [],
        ["Production Backlog"],
        [],
        ["As Of", "Team", "Queue km", "Historical Capacity km/week", "Backlog Weeks", "Status"],
        ["24/09/2026", "GIS", "120.5", "40.0", "3.0", "Watch"],
    ]

    parsed = parse_flow_values(values)

    assert parsed["snapshot"] == "01/10/2026 08:00"
    assert parsed["current_state"][0]["TPC Outstanding"] == 12.5
    assert parsed["current_state"][0]["PLS WIP"] == 3.2
    assert parsed["movement_period"].startswith("29/09/2026 15:00")
    assert parsed["movement"][0]["GIS Processed"] == 8.5
    assert parsed["backlog"][0]["Queue km"] == 120.5
    assert parsed["backlog"][0]["Status"] == "Watch"


def test_parse_rates_front_page_keeps_project_estimates_and_flags():
    values = [
        ["Project", "Name", "RS Hours", "GIS Hours", "PLS Hours", "Dev needed?", "Total", "With Richard?"],
        ["NMIP26055", "SoCo GPC 2026", "120", "240.5", "N/A", "NO", "360.5", "TRUE"],
        ["", "", "", "", "", "", "", "FALSE"],
        ["Rates Calculator", "Subcon tracker"],
    ]

    rows = parse_rates_front_page(values)

    assert rows == [
        {
            "Project": "NMIP26055",
            "Name": "SoCo GPC 2026",
            "RS Hours": 120.0,
            "GIS Hours": 240.5,
            "PLS Hours": None,
            "Dev needed?": "NO",
            "Total": 360.5,
            "With Richard?": True,
        }
    ]


def test_parse_rates_project_values_reads_assumptions_and_project_metadata():
    values = [
        ["Front page", "RS Hrs", "GIS Hrs", "PLS Hrs", "Dev needed"],
        ["NMXX"],
        [],
        ["Item", "Rate km/day", "Rate Per km", "Team", "Hrs"],
        ["Build", "40", "", "RS", "8"],
        ["Class QC", "35", "", "GIS", "12.5"],
        [],
        [None, "Project Name", "Seminole MSA", None, "Project Code", "NMIP26100"],
    ]

    parsed = parse_rates_project_values(values)

    assert parsed["project_name"] == "Seminole MSA"
    assert parsed["project_code"] == "NMIP26100"
    assert parsed["assumptions"][0]["Item"] == "Build"
    assert parsed["assumptions"][0]["Rate km/day"] == 40.0
    assert parsed["assumptions"][1]["Hrs"] == 12.5
