"""Per-dataset anomaly check functions.

Each file in this directory corresponds to a dataset_id in the registry.
The alert system discovers them by convention: checks/{dataset_id}.py

Each module must export:
    run_checks(conn, dataset_id) -> list[CheckResult]

CheckResult is imported from the dataset's own module (each defines it locally
or imports from a shared location).
"""

from dataclasses import dataclass


@dataclass
class CheckResult:
    """Result of a single anomaly check."""
    check_name: str
    passed: bool
    message: str
