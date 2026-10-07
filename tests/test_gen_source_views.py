"""`scripts/gen_source_views.py` under the objective-first design: its views ARE the schema `{{src}}` stands for.

The DDL itself (schema, one view per logical source, ordering, the errors) is covered with the golden loader in
`tests/test_load_golden.py`; this pins what the script says about its own purpose now that a workflow is one
build file reading `{{src}}.<LOGICAL>` rather than stored procedures reading `IDENTIFIER(:..._SRC)`.
"""
from __future__ import annotations

import gen_source_views
from gen_source_views import production_views_sql

MAPPINGS = {"sources": {"sales/orders.yxdb": {"snowflake": "SALES.RAW.ORDERS", "logical": "ORDERS",
                                              "tool_ids": ["1"]}}}


def test_the_ddl_header_says_a_build_file_reads_the_views_as_src():
    sql = production_views_sql("wf_0001", MAPPINGS, {"target_database": "ANALYTICS"})

    assert sql.splitlines()[0].startswith("-- Source views for wf_0001: the logical names its build file reads as "
                                          "{{src}}.<LOGICAL>")
    assert "CREATE OR REPLACE VIEW ANALYTICS.MIG_SRC_WF0001.ORDERS AS SELECT * FROM SALES.RAW.ORDERS;" in sql
    assert "procedure" not in sql.lower()


def test_the_docstring_names_the_src_schema_and_no_retired_form():
    doc = " ".join(gen_source_views.__doc__.split())

    assert "`{{src}}.<LOGICAL>`" in doc and "`<target_database>.MIG_SRC_<WF>`" in doc
    assert "deploy.py --src-schema MIG_SRC_<WF>" in doc and "has not been executed against a Snowflake account" in doc
    for retired in ("LET ", "IDENTIFIER(", "SRC_DB", "procedure"):
        assert retired not in doc, retired
