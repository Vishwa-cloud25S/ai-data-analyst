from app.semantic.layer import as_dict


def test_semantic_layer_loads(semantic_layer):
    assert {"fct_orders", "dim_products", "dim_customers"} <= set(semantic_layer.entities)
    assert "total_revenue" in semantic_layer.metrics
    assert semantic_layer.metrics["total_revenue"].filters


def test_hidden_table_is_not_in_allow_list(semantic_layer):
    assert "employee_salaries" not in semantic_layer.allowed_tables
    assert "salary_usd" not in semantic_layer.allowed_columns


def test_dbt_metadata_is_loaded(semantic_layer):
    assert "fct_orders" in semantic_layer.dbt_docs
    assert "fct_orders.net_revenue" in semantic_layer.dbt_docs
    assert "revenue" in semantic_layer.dbt_docs["fct_orders"].lower()


def test_retrieval_finds_revenue_and_products(retriever):
    ctx = retriever.retrieve_context("What were our highest revenue products last quarter?")
    assert "fct_orders" in ctx["entities"]
    assert "dim_products" in ctx["entities"]
    assert "total_revenue" in ctx["metrics"]


def test_retrieval_finds_margin(retriever):
    ctx = retriever.retrieve_context("which region has the best profit margin")
    assert "gross_margin" in ctx["metrics"]


def test_retrieval_prunes_prompt(retriever):
    ctx = retriever.retrieve_context("revenue by channel")
    prompt = retriever.render_schema_prompt(ctx)
    assert "employee_salaries" not in prompt
    assert "net_revenue" in prompt
    # only the retrieved slice, not the whole warehouse
    assert prompt.count("### ") <= 3


def test_as_dict_shape(semantic_layer):
    d = as_dict(semantic_layer)
    assert d["entities"] and d["metrics"] and d["joins"]
