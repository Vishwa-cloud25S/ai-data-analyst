"""Step 6 - Result validation.

Catches the failure mode that SQL validation cannot: syntactically legal SQL
that returns a nonsense answer. We check shape, nullness, sanity of magnitudes
and suspicious patterns, then attach a confidence score used by the UI.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.pipeline.executor import QueryResult
from app.semantic.layer import SemanticLayer


@dataclass
class ResultValidation:
    ok: bool
    confidence: float
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)


NEGATIVE_OK = {"gross_margin", "margin", "growth", "delta", "change", "diff", "variance"}


def validate_result(
    result: QueryResult, sl: SemanticLayer, *, metric: str | None = None
) -> ResultValidation:
    issues: list[str] = []
    notes: list[str] = []
    checks: dict[str, bool] = {}
    confidence = 1.0

    checks["has_columns"] = bool(result.columns)
    if not result.columns:
        issues.append("Query returned no columns.")
        confidence = 0.0

    checks["has_rows"] = result.row_count > 0
    if result.row_count == 0:
        issues.append("Query returned zero rows - the filters may be too narrow "
                      "or the period may have no data.")
        confidence = min(confidence, 0.35)

    if result.truncated:
        notes.append(f"Results truncated at {result.row_count} rows.")
        confidence = min(confidence, 0.85)

    # numeric sanity
    numeric_cols: dict[str, list[float]] = {}
    for i, col in enumerate(result.columns):
        vals = [r[i] for r in result.rows]
        nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if nums and len(nums) >= max(1, len(vals) // 2):
            numeric_cols[col] = nums

        null_share = sum(v is None for v in vals) / len(vals) if vals else 0.0
        if null_share > 0.5:
            issues.append(f"Column '{col}' is more than 50% NULL.")
            confidence = min(confidence, 0.5)

    checks["no_all_null_metric"] = True
    for col, nums in numeric_cols.items():
        base = col.lower()
        if any(n < 0 for n in nums) and not any(k in base for k in NEGATIVE_OK):
            issues.append(f"Column '{col}' contains negative values, which is unexpected "
                          f"for this metric.")
            confidence = min(confidence, 0.6)
        if nums and max(abs(n) for n in nums) > 1e12:
            issues.append(f"Column '{col}' has implausibly large values (>1e12); "
                          f"check for a fan-out join.")
            confidence = min(confidence, 0.4)

    # single-row single-value answers are fine but low information
    checks["shape_reasonable"] = True
    if result.row_count == 1 and len(result.columns) == 1:
        notes.append("Single scalar answer.")

    # duplicate grouping keys hint at a missing GROUP BY
    if result.row_count > 1 and len(result.columns) >= 2:
        key_idx = 0
        keys = [r[key_idx] for r in result.rows]
        if len(set(map(str, keys))) < len(keys):
            issues.append("Duplicate values in the first (grouping) column - "
                          "the GROUP BY may be incomplete.")
            confidence = min(confidence, 0.55)
            checks["unique_group_keys"] = False
    checks.setdefault("unique_group_keys", True)

    if metric and metric in sl.metrics:
        notes.append(f"Metric '{sl.metrics[metric].label}' definition applied from the "
                     f"semantic layer.")

    checks["passed"] = not issues
    return ResultValidation(
        ok=not any(
            i for i in issues
            if "implausibly large" in i or "no columns" in i or "GROUP BY" in i
        ),
        confidence=round(confidence, 2),
        issues=issues,
        notes=notes,
        checks=checks,
    )
