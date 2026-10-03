from app.integrations.ptm_legacy import _values_to_records


def test_values_to_records_ignores_blank_and_duplicate_headers():
    values = [
        ["Name", "Department", "", "", "Department"],
        ["Alice", "GIS", "x", "y", "SHOULD_NOT_OVERRIDE"],
        ["", "", "", "", ""],
    ]
    records = _values_to_records(values)
    assert records == [{"Name": "Alice", "Department": "GIS"}]
