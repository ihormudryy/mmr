def number_or_nan(mapping, key) -> float:
    """Extract a number from mapping, handling missing keys and JSON nulls."""
    value = mapping.get(key)
    if value is None:
        return float('nan')
    return float(value)
