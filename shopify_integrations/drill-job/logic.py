"""Pure logic, testable without a runtime."""


def next_count(current):
    """The next run number. Idempotent input, monotonic output."""
    return int(current or 0) + 1
