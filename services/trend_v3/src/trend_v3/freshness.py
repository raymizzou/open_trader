from datetime import date


def is_opening_data_fresh(data_date: date | None, expected_data_date: date) -> bool:
    """Return whether the class data is fresh enough to consider an opening."""
    return data_date is not None and data_date == expected_data_date
