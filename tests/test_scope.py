"""Scope-gate tests.

Regression cover for a real flaw: questions that matched no metric keyword fell
through to the default metric, so "what is the weather in Hyderabad" was
answered with total revenue. No data leaked, but a confidently wrong answer to
an unrelated question is its own kind of failure.
"""
import pytest

OUT_OF_SCOPE = [
    "what is the weather in Hyderabad",
    "tell me a joke",
    "who is the CEO",
    "what is the average compensation per employee",
    "how much do we pay our staff",
    "summarise the news",
    "what is 2 + 2",
    "recommend a restaurant",
]

IN_SCOPE = [
    "What were our highest revenue products last quarter?",
    "which categories sell best",
    "how many orders last month",
    "revenue by region",
    "average order value by channel",
    "units sold by brand",
    "return rate by segment",
    "how many customers in APAC",
    "margin trend over time",
    "top 5 skus by sales",
]


@pytest.mark.parametrize("question", OUT_OF_SCOPE)
def test_out_of_scope_is_refused(analyst, question):
    r = analyst.ask(question)
    assert r.status == "refused", f"{question!r} was answered: {r.answer[:80]}"
    assert r.sql is None
    assert r.rows == []
    assert r.confidence == 0.0


@pytest.mark.parametrize("question", OUT_OF_SCOPE)
def test_out_of_scope_never_reaches_the_warehouse(analyst, question):
    r = analyst.ask(question)
    assert "execution" not in [s.name for s in r.trace]


@pytest.mark.parametrize("question", IN_SCOPE)
def test_in_scope_still_answers(analyst, question):
    r = analyst.ask(question)
    assert r.status == "answered", f"{question!r} was wrongly refused: {r.answer[:80]}"
    assert r.row_count > 0


def test_scope_check_reports_matched_terms(retriever):
    ok, terms = retriever.scope_check("revenue by region")
    assert ok and "region" in terms and "revenue" in terms

    ok, terms = retriever.scope_check("tell me a joke")
    assert not ok and terms == []


def test_stopwords_do_not_create_false_matches(retriever):
    """'what is the ...' must not score as domain vocabulary."""
    ok, terms = retriever.scope_check("what is the")
    assert not ok and terms == []


def test_plurals_are_matched(retriever):
    for q in ["categories", "products", "customers", "orders", "channels"]:
        ok, _ = retriever.scope_check(q)
        assert ok, f"{q!r} should be in scope"


def test_scope_refusal_names_what_it_can_do(analyst):
    r = analyst.ask("what is the weather in Hyderabad")
    for term in ("revenue", "margin", "product", "region"):
        assert term in r.answer.lower()
    trace = next(s for s in r.trace if s.name == "schema_retrieval")
    assert trace.status == "blocked"
    assert trace.detail["in_scope"] is False
