from app.pipeline.executor import QueryResult
from app.pipeline.result_validator import validate_result


def _qr(columns, rows):
    return QueryResult(columns=columns, rows=rows, row_count=len(rows), duration_ms=1.0)


def test_healthy_result(semantic_layer):
    rv = validate_result(_qr(["product_name", "total_revenue"],
                             [["A", 100.0], ["B", 50.0]]), semantic_layer,
                         metric="total_revenue")
    assert rv.ok and rv.confidence == 1.0 and not rv.issues


def test_empty_result_lowers_confidence(semantic_layer):
    rv = validate_result(_qr(["product_name", "total_revenue"], []), semantic_layer)
    assert rv.confidence < 0.5
    assert any("zero rows" in i for i in rv.issues)


def test_fanout_blowup_is_flagged(semantic_layer):
    rv = validate_result(_qr(["region", "total_revenue"], [["NA", 9.9e14]]),
                         semantic_layer, metric="total_revenue")
    assert not rv.ok
    assert any("implausibly large" in i for i in rv.issues)


def test_negative_revenue_flagged(semantic_layer):
    rv = validate_result(_qr(["region", "total_revenue"], [["NA", -5.0]]),
                         semantic_layer, metric="total_revenue")
    assert any("negative" in i for i in rv.issues)
    assert rv.confidence < 1.0


def test_negative_margin_allowed(semantic_layer):
    rv = validate_result(_qr(["region", "gross_margin"], [["NA", -5.0], ["EMEA", 3.0]]),
                         semantic_layer, metric="gross_margin")
    assert not any("negative" in i for i in rv.issues)


def test_duplicate_group_keys_flagged(semantic_layer):
    rv = validate_result(_qr(["region", "total_revenue"], [["NA", 1.0], ["NA", 2.0]]),
                         semantic_layer, metric="total_revenue")
    assert not rv.ok
    assert any("GROUP BY" in i for i in rv.issues)


def test_mostly_null_column_flagged(semantic_layer):
    rv = validate_result(_qr(["region", "total_revenue"],
                             [["NA", None], ["EMEA", None], ["APAC", 1.0]]),
                         semantic_layer)
    assert any("NULL" in i for i in rv.issues)
