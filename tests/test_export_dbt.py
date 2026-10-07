"""`export_dbt.py`: a workflow's validated `build.sql` converted to a small dbt project (objective-first design D9,
§4.10).

Three layers:

* the canned builds of the samples (wf_0001, 0002, 0003, 0004, 0007): each exports, and the models carry what the
  conversion table says -- a temporary table is an ephemeral model, an overwrite a `table`, an append an
  incremental `append`, an update_insert an incremental `merge` on the objective's keys, PreSQL/PostSQL the model's
  hooks; every table reference is a `ref()`, `source()` or `this`, and every model starts with the step markers it
  sits under;
* small synthetic builds (`tests/build_fixtures.py`'s `wf_0009`) for each row of the table and each place a body is
  more than a cut of the build's text;
* statements the table has no row for (exit 1, naming the line), the skips (`program.dbt: false`, a Python
  workflow: nothing written, any old `dbt/` removed), usage errors (exit 2) and determinism.

That a project PASSes every golden set is `validate_dbt.py`'s, in `test_validate_dbt.py`. Nothing here has run on
Snowflake: a model is dbt text, parsed by dbt-core (`dbt parse`) in the last tests and never executed.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import export_dbt
from lib import dbt_project
from lib.io import write_yaml
from lib.paths import Repo
from tests import build_fixtures as bf

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
SQL_SAMPLES = ["wf_0001", "wf_0002", "wf_0003", "wf_0004", "wf_0007"]

#: wf_id -> {model: (materialized, strategy)} for what each canned build converts to.
EXPECTED_MODELS = {
    "wf_0001": {"clean_orders": ("ephemeral", None), "sales_summary": ("table", None),
                "excluded_orders": ("table", None)},
    "wf_0002": {"customers_clean": ("ephemeral", None), "orders_typed": ("ephemeral", None),
                "customer_order_fact": ("incremental", "append")},
    "wf_0003": {"latest_entries": ("ephemeral", None), "gl_summary": ("incremental", "merge")},
    "wf_0004": {"stock_clean": ("ephemeral", None), "inventory_by_wh": ("table", None),
                "inventory_long": ("table", None)},
    "wf_0007": {"attainment": ("ephemeral", None), "region_attainment": ("table", None),
                "attainment_history": ("incremental", "merge")},
}


def stage_sample(root: Path, wf_id: str) -> Repo:
    """A workflow directory holding the sample's canned objective and canned build, nothing else."""
    repo = Repo(root)
    directory = repo.wf(wf_id)
    directory.mkdir(parents=True, exist_ok=True)
    canned = SAMPLES / wf_id / "canned"
    shutil.copy(canned / "objective.json", directory / "objective.json")
    shutil.copy(canned / "build.sql", directory / "build.sql")
    return repo


def stage(root: Path, build: str | None = None, obj: dict | None = None, *, name: str = "build.sql") -> Repo:
    """`bf.make`'s synthetic `wf_0009` (ITEMS in, ITEMS_OUT out) with `build` instead of the smallest build."""
    return bf.make(root, build, obj, name=name)


def set_dbt(repo: Repo, value) -> None:
    write_yaml(repo.global_mappings, {"program": {"dbt": value}})


def dbt_dir(repo: Repo, wf_id: str = bf.WF) -> Path:
    return repo.wf(wf_id, "dbt")


def files_of(repo: Repo, wf_id: str = bf.WF) -> dict[str, bytes]:
    base = dbt_dir(repo, wf_id)
    return {path.relative_to(base).as_posix(): path.read_bytes() for path in sorted(base.rglob("*")) if path.is_file()}


def text_of(repo: Repo, relative: str, wf_id: str = bf.WF) -> str:
    return dbt_dir(repo, wf_id).joinpath(*relative.split("/")).read_text(encoding="utf-8")


def models_of(repo: Repo, wf_id: str = bf.WF) -> dict[str, export_dbt.Model]:
    return {model.name: model for model in export_dbt.export(repo, wf_id, write=False).models}


def objective_with(**changes) -> dict:
    """The synthetic objective with `bf.objective`'s keyword arguments."""
    return bf.objective(**changes)


def build(*statements: str, steps: str = "s1, s2") -> str:
    """A synthetic build file: the header, then each statement under a step marker (the first under `steps`)."""
    parts = ["-- wf_0009 · Items", "-- Written from objective.md. {{src}} = source schema, {{tgt}} = target schema.", ""]
    for index, statement in enumerate(statements):
        parts += [f"-- step {steps if index == 0 else 's2'}", statement.rstrip(), ""]
    return "\n".join(parts)


KEPT = """CREATE OR REPLACE TEMPORARY TABLE kept AS
SELECT ID, NOTE FROM {{src}}.ITEMS WHERE NOTE IS NOT NULL;"""
OVERWRITE = """CREATE OR REPLACE TABLE {{tgt}}.ITEMS_OUT AS
SELECT ID, NOTE FROM kept;"""


# --- every canned SQL build exports ---------------------------------------------------------------------------------


@pytest.mark.parametrize("wf_id", SQL_SAMPLES)
def test_every_canned_sql_build_exports_the_layout_of_the_design(tmp_path, wf_id, capsys):
    repo = stage_sample(tmp_path, wf_id)
    assert export_dbt.main([wf_id, "--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"{wf_id}: wrote workflows/{wf_id}/dbt (")
    expected = {"dbt_project.yml", "profiles.yml", "README.md", "models/sources.yml", "models/schema.yml",
                *(f"models/{name}.sql" for name in EXPECTED_MODELS[wf_id])}
    assert set(files_of(repo, wf_id)) == expected


@pytest.mark.parametrize("wf_id", SQL_SAMPLES)
def test_each_canned_build_converts_to_the_models_the_table_names(tmp_path, wf_id):
    repo = stage_sample(tmp_path, wf_id)
    models = models_of(repo, wf_id)
    assert {name: (m.materialized, m.strategy) for name, m in models.items()} == EXPECTED_MODELS[wf_id]


@pytest.mark.parametrize("wf_id", SQL_SAMPLES)
def test_a_model_names_every_table_through_ref_source_or_this(tmp_path, wf_id):
    repo = stage_sample(tmp_path, wf_id)
    export_dbt.export(repo, wf_id)
    for relative in files_of(repo, wf_id):
        if not relative.startswith("models/") or not relative.endswith(".sql"):
            continue
        text = text_of(repo, relative, wf_id)
        assert "{{src}}" not in text and "{{tgt}}" not in text and "__PH" not in text, relative
        assert set(re.findall(r"\{\{\s*(\w+)", text)) <= {"config", "ref", "source", "this"}, relative
        for name in re.findall(r"\{\{\s*source\('([^']+)'", text):
            assert name == wf_id


@pytest.mark.parametrize("wf_id", SQL_SAMPLES)
def test_a_model_starts_with_the_step_markers_it_sits_under(tmp_path, wf_id):
    repo = stage_sample(tmp_path, wf_id)
    result = export_dbt.export(repo, wf_id, write=False)
    known = {step["id"] for step in json.loads(repo.wf(wf_id, "objective.json").read_text(encoding="utf-8"))["steps"]}
    for model in result.models:
        text = result.files[f"models/{model.name}.sql"]
        lines = text.splitlines()
        after_config = 1 if lines[0].endswith("}}") else lines.index(") }}") + 1
        marker = lines[after_config]
        assert marker.startswith("-- step "), (model.name, marker)
        steps = [step.strip() for step in marker[len("-- step "):].split(",")]
        assert steps and set(steps) <= known and steps == model.steps
        assert steps == sorted(steps, key=lambda step: int(step[1:]))


def test_wf_0001_models_keep_the_builds_own_comments_and_text(tmp_path):
    repo = stage_sample(tmp_path, "wf_0001")
    result = export_dbt.export(repo, "wf_0001", write=False)
    clean = result.files["models/clean_orders.sql"]
    assert clean.splitlines()[:2] == ["{{ config(materialized='ephemeral') }}", "-- step s1, s3"]
    assert "-- Both outputs read the cleaned orders: CUSTOMER cut to 10 characters" in clean
    assert "FROM {{ source('wf_0001', 'ORDERS') }}" in clean
    assert "TRY_TO_DOUBLE(AMOUNT_TXT) AS AMOUNT" in clean                    # the build's own text, not regenerated
    summary = result.files["models/sales_summary.sql"]
    assert summary.startswith("{{ config(materialized='table', alias='SALES_SUMMARY') }}\n-- step s2, s3, s4\n")
    assert "    FROM {{ ref('clean_orders') }}" in summary
    assert "ORDER BY TOTAL_NET DESC NULLS LAST, REGION ASC NULLS FIRST" in summary
    assert summary.endswith("NULLS FIRST\n")


# --- update_insert, PreSQL and PostSQL (wf_0003) ---------------------------------------------------------------------


def test_the_update_insert_sample_is_an_incremental_merge_on_the_objectives_keys(tmp_path):
    repo = stage_sample(tmp_path, "wf_0003")
    model = models_of(repo, "wf_0003")["gl_summary"]
    objective = json.loads(repo.wf("wf_0003", "objective.json").read_text(encoding="utf-8"))
    assert model.unique_key == objective["outputs"][0]["keys"] == ["ACCT", "PERIOD"]
    assert (model.materialized, model.strategy, model.alias) == ("incremental", "merge", "GL_SUMMARY")
    # only the columns the MERGE's UPDATE SET names are updated on a match: LOADED_FLAG is left alone
    assert model.update_columns == ["TOTAL", "CLOSING_BAL", "ENTRIES", "HAS_PERIOD_END"]


def test_the_merge_keys_come_from_the_objective_not_from_the_text_alone(tmp_path):
    repo = stage_sample(tmp_path, "wf_0007")
    objective = json.loads(repo.wf("wf_0007", "objective.json").read_text(encoding="utf-8"))
    assert models_of(repo, "wf_0007")["attainment_history"].unique_key == ["REGION", "PERIOD"]
    objective["outputs"][1]["keys"] = ["PERIOD", "REGION"]                    # the objective's order is what is written
    repo.wf("wf_0007", "objective.json").write_text(json.dumps(objective), encoding="utf-8")
    assert models_of(repo, "wf_0007")["attainment_history"].unique_key == ["PERIOD", "REGION"]


def test_pre_and_post_sql_become_the_models_hooks(tmp_path):
    repo = stage_sample(tmp_path, "wf_0003")
    export_dbt.export(repo, "wf_0003")
    text = text_of(repo, "models/gl_summary.sql", "wf_0003")
    assert text.startswith("{{ config(\n    materialized='incremental',\n    incremental_strategy='merge',\n"
                           "    full_refresh=false,\n"
                           "    unique_key=['ACCT', 'PERIOD'],\n"
                           "    merge_update_columns=['TOTAL', 'CLOSING_BAL', 'ENTRIES', 'HAS_PERIOD_END'],\n"
                           "    alias='GL_SUMMARY',\n")
    assert """    pre_hook="DELETE FROM {{ this }} WHERE PERIOD < '2020-01'",\n""" in text
    assert """    post_hook="UPDATE {{ this }} SET LOADED_FLAG = 'Y' WHERE LOADED_FLAG IS NULL"\n) }}""" in text
    model = models_of(repo, "wf_0003")["gl_summary"]
    assert model.pre_hooks == ["DELETE FROM {{ this }} WHERE PERIOD < '2020-01'"]
    assert model.post_hooks == ["UPDATE {{ this }} SET LOADED_FLAG = 'Y' WHERE LOADED_FLAG IS NULL"]
    assert not re.search(r"^(DELETE|UPDATE)\b", text.split(") }}", 1)[1], re.MULTILINE)    # hooks, not body


def test_an_incremental_model_carries_full_refresh_false_and_no_other_model_does(tmp_path):
    """`dbt build --full-refresh` must not rebuild a table whose history the build relies on (append, merge)."""
    for wf_id in SQL_SAMPLES:
        repo = stage_sample(tmp_path / wf_id, wf_id)
        result = export_dbt.export(repo, wf_id, write=False)
        for model in result.models:
            text = result.files[f"models/{model.name}.sql"]
            assert ("full_refresh=false" in text) == (model.materialized == "incremental"), (wf_id, model.name)
    text = export_dbt.export(stage_sample(tmp_path / "two", "wf_0002"), "wf_0002", write=False).files[
        "models/customer_order_fact.sql"]
    assert text.startswith("{{ config(\n    materialized='incremental',\n    incremental_strategy='append',\n"
                           "    full_refresh=false,\n    alias='CUSTOMER_ORDER_FACT'\n) }}\n")


def test_a_merge_projects_the_column_its_insert_fills_with_a_constant(tmp_path):
    """dbt's merge inserts every column of the table; the MERGE's INSERT gives LOADED_FLAG a NULL of its own, so the
    model has to project it (it is not a column of the USING query)."""
    repo = stage_sample(tmp_path, "wf_0003")
    body = models_of(repo, "wf_0003")["gl_summary"].body
    assert body.startswith("SELECT S.*, NULL AS LOADED_FLAG\nFROM (\nSELECT ACCT, PERIOD,\n")
    assert body.endswith("\n) AS S")
    assert "FROM {{ ref('latest_entries') }}" in body


def test_a_merge_whose_source_is_a_table_reads_it_through_ref(tmp_path):
    repo = stage_sample(tmp_path, "wf_0007")
    model = models_of(repo, "wf_0007")["attainment_history"]
    assert model.body == "SELECT * FROM {{ ref('attainment') }}"
    assert model.update_columns == ["TARGET_TOTAL", "ACTUAL_TOTAL", "LINES"]
    assert model.pre_hooks == [] and model.post_hooks == []


# --- append, with the INSERT's positional column list (wf_0002) -----------------------------------------------------


def test_an_append_is_an_incremental_model_that_names_the_inserts_columns(tmp_path):
    repo = stage_sample(tmp_path, "wf_0002")
    model = models_of(repo, "wf_0002")["customer_order_fact"]
    assert (model.materialized, model.strategy, model.alias) == ("incremental", "append", "CUSTOMER_ORDER_FACT")
    # the first SELECT's last column is the constant 'MATCHED', so the INSERT's positional list is what names it
    assert model.body.startswith("WITH q (CUST_ID, NAME, CITY, TIER, ORDER_ID, AMOUNT, ORDER_DATE, MATCH_FLAG) AS (\n"
                                 "SELECT c.CUST_ID, c.NAME")
    assert model.body.endswith("\n)\nSELECT * FROM q")


def test_an_insert_whose_query_already_names_its_columns_is_left_as_written(tmp_path):
    insert = "INSERT INTO {{tgt}}.ITEMS_OUT (ID, NOTE)\nSELECT ID, NOTE FROM kept;"
    repo = stage(tmp_path, build(KEPT, insert), objective_with(write_mode="append"))
    model = models_of(repo)["items_out"]
    assert (model.materialized, model.strategy) == ("incremental", "append")
    assert model.body == "SELECT ID, NOTE FROM {{ ref('kept') }}"


def test_an_insert_with_an_unnamed_expression_is_wrapped_by_name(tmp_path):
    insert = "INSERT INTO {{tgt}}.ITEMS_OUT (ID, NOTE)\nSELECT ID, UPPER(NOTE) FROM kept;"
    repo = stage(tmp_path, build(KEPT, insert), objective_with(write_mode="append"))
    assert models_of(repo)["items_out"].body == (
        "WITH q (ID, NOTE) AS (\nSELECT ID, UPPER(NOTE) FROM {{ ref('kept') }}\n)\nSELECT * FROM q")


def test_a_truncate_then_insert_is_a_table_replaced_on_every_run(tmp_path):
    truncate = "TRUNCATE TABLE {{tgt}}.ITEMS_OUT;"
    insert = "INSERT INTO {{tgt}}.ITEMS_OUT (ID, NOTE)\nSELECT ID, NOTE FROM kept;"
    repo = stage(tmp_path, build(KEPT, truncate, insert), objective_with(write_mode="truncate_append"))
    model = models_of(repo)["items_out"]
    assert (model.materialized, model.strategy) == ("table", None)
    assert model.pre_hooks == [] and model.post_hooks == []                       # the TRUNCATE is the table's replacement


def test_a_delete_with_no_where_clears_a_truncate_append_too(tmp_path):
    delete = "DELETE FROM {{tgt}}.ITEMS_OUT;"
    insert = "INSERT INTO {{tgt}}.ITEMS_OUT (ID, NOTE)\nSELECT ID, NOTE FROM kept;"
    repo = stage(tmp_path, build(KEPT, delete, insert), objective_with(write_mode="truncate_append"))
    model = models_of(repo)["items_out"]
    assert model.materialized == "table" and model.pre_hooks == []


def test_the_same_delete_before_an_append_is_the_pre_sql_hook(tmp_path):
    delete = "DELETE FROM {{tgt}}.ITEMS_OUT;"
    insert = "INSERT INTO {{tgt}}.ITEMS_OUT (ID, NOTE)\nSELECT ID, NOTE FROM kept;"
    repo = stage(tmp_path, build(KEPT, delete, insert), objective_with(write_mode="append", pre_sql="DELETE FROM X"))
    model = models_of(repo)["items_out"]
    assert (model.materialized, model.strategy) == ("incremental", "append")
    assert model.pre_hooks == ["DELETE FROM {{ this }}"]


def test_pre_and_post_sql_around_an_overwrite_are_hooks_in_file_order(tmp_path):
    pre1, pre2 = "DELETE FROM {{tgt}}.ITEMS_OUT WHERE ID < 0;", "UPDATE {{tgt}}.ITEMS_OUT SET NOTE = 'x';"
    post = "UPDATE {{tgt}}.ITEMS_OUT SET NOTE = \"it's\" WHERE NOTE IS NULL;"
    repo = stage(tmp_path, build(KEPT, pre1, pre2, OVERWRITE, post),
                 objective_with(pre_sql="DELETE FROM X", post_sql="UPDATE X SET Y = 1"))
    export_dbt.export(repo, bf.WF)
    model = models_of(repo)["items_out"]
    assert model.pre_hooks == ["DELETE FROM {{ this }} WHERE ID < 0", "UPDATE {{ this }} SET NOTE = 'x'"]
    assert model.post_hooks == ["""UPDATE {{ this }} SET NOTE = "it's" WHERE NOTE IS NULL"""]
    text = text_of(repo, "models/items_out.sql")
    assert "    pre_hook=['DELETE FROM {{ this }} WHERE ID < 0', \"UPDATE {{ this }} SET NOTE = 'x'\"],\n" in text
    # a string with both kinds of quote is escaped, not cut
    assert """    post_hook="UPDATE {{ this }} SET NOTE = \\"it's\\" WHERE NOTE IS NULL"\n""" in text


def test_a_merge_whose_insert_overrides_a_column_the_query_projects_excludes_it(tmp_path):
    merge = ("MERGE INTO {{tgt}}.ITEMS_OUT AS T\nUSING (SELECT ID, NOTE FROM kept) AS S\nON T.ID = S.ID\n"
             "WHEN MATCHED THEN UPDATE SET NOTE = S.NOTE\n"
             "WHEN NOT MATCHED THEN INSERT (ID, NOTE) VALUES (S.ID, 'new');")
    repo = stage(tmp_path, build(KEPT, merge), objective_with(write_mode="update_insert", keys=("ID",)))
    model = models_of(repo)["items_out"]
    assert model.unique_key == ["ID"] and model.update_columns == ["NOTE"]
    assert model.body == "SELECT S.* EXCLUDE (NOTE), 'new' AS NOTE\nFROM (\nSELECT ID, NOTE FROM {{ ref('kept') }}\n) AS S"


def test_a_merge_that_copies_every_column_has_the_using_query_as_its_body(tmp_path):
    merge = ("MERGE INTO {{tgt}}.ITEMS_OUT AS T\nUSING (SELECT ID, NOTE FROM kept) AS S\nON T.ID = S.ID\n"
             "WHEN MATCHED THEN UPDATE SET NOTE = S.NOTE\n"
             "WHEN NOT MATCHED THEN INSERT (ID, NOTE) VALUES (S.ID, S.NOTE);")
    repo = stage(tmp_path, build(KEPT, merge), objective_with(write_mode="update_insert", keys=("ID",)))
    assert models_of(repo)["items_out"].body == "SELECT ID, NOTE FROM {{ ref('kept') }}"


# --- temporary tables, sources, targets read back --------------------------------------------------------------------


def test_a_temporary_table_is_an_ephemeral_model_and_the_models_read_it_through_ref(tmp_path):
    repo = stage(tmp_path)
    models = models_of(repo)
    assert list(models) == ["kept", "items_out"]
    kept, out = models["kept"], models["items_out"]
    assert (kept.materialized, kept.alias, kept.logical) == ("ephemeral", None, None)
    assert kept.body == "SELECT ID, NOTE FROM {{ source('wf_0009', 'ITEMS') }} WHERE NOTE IS NOT NULL"
    assert (out.materialized, out.alias, out.logical) == ("table", "ITEMS_OUT", "ITEMS_OUT")
    assert out.body == "SELECT ID, NOTE FROM {{ ref('kept') }}"


def test_a_source_name_is_spelled_as_the_objective_spells_it(tmp_path):
    repo = stage(tmp_path, bf.GOOD_SQL.replace("{{src}}.ITEMS", "{{ src }} . items"))
    assert models_of(repo)["kept"].body == "SELECT ID, NOTE FROM {{ source('wf_0009', 'ITEMS') }} WHERE NOTE IS NOT NULL"


def test_a_target_read_after_it_is_written_is_a_ref_and_its_own_table_is_this(tmp_path):
    second = "CREATE OR REPLACE TABLE {{tgt}}.SECOND AS\nSELECT ID FROM {{tgt}}.ITEMS_OUT;"
    obj = objective_with()
    extra = copy.deepcopy(obj["outputs"][0])
    extra.update(tool_id="8", logical="SECOND", columns=[obj["outputs"][0]["columns"][0]])
    obj["outputs"].append(extra)
    repo = stage(tmp_path, build(KEPT, OVERWRITE, second), obj)
    models = models_of(repo)
    assert models["second"].body == "SELECT ID FROM {{ ref('items_out') }}"
    own = "INSERT INTO {{tgt}}.ITEMS_OUT (ID, NOTE)\nSELECT ID, NOTE FROM {{tgt}}.ITEMS_OUT;"
    repo = stage(tmp_path / "own", build(own), objective_with(write_mode="append"))
    assert models_of(repo)["items_out"].body == "SELECT ID, NOTE FROM {{ this }}"


def test_a_name_that_is_a_cte_of_the_statement_is_left_alone(tmp_path):
    overwrite = ("CREATE OR REPLACE TABLE {{tgt}}.ITEMS_OUT AS\nWITH kept AS (SELECT ID, NOTE FROM {{src}}.ITEMS)\n"
                 "SELECT ID, NOTE FROM kept;")
    repo = stage(tmp_path, build(KEPT, overwrite))
    assert models_of(repo)["items_out"].body == (
        "WITH kept AS (SELECT ID, NOTE FROM {{ source('wf_0009', 'ITEMS') }})\nSELECT ID, NOTE FROM kept")


def test_a_comment_or_string_that_jinja_would_read_as_a_tag_is_made_safe(tmp_path):
    kept = ("-- reads {{src}}.ITEMS, then {% if x %} and {# y #}\nCREATE OR REPLACE TEMPORARY TABLE kept AS\n"
            "SELECT ID, 'a{#b' AS NOTE FROM {{src}}.ITEMS;")
    repo = stage(tmp_path, build(kept, OVERWRITE))
    export_dbt.export(repo, bf.WF)
    text = text_of(repo, "models/kept.sql")
    assert "-- reads <src>.ITEMS, then { % if x %} and { # y #}" in text
    assert "SELECT ID, 'a{{ '{' }}#b' AS NOTE" in text


# --- the project's other files -----------------------------------------------------------------------------------------


def test_sources_lists_one_table_per_objective_input_with_the_source_schema_var(tmp_path):
    repo = stage_sample(tmp_path, "wf_0002")
    export_dbt.export(repo, "wf_0002")
    assert text_of(repo, "models/sources.yml", "wf_0002") == """\
version: 2
sources:
  - name: wf_0002
    schema: "{{ var('src_schema') }}"
    tables:
      - name: CUSTOMERS
        columns:
          - name: CUST_ID
          - name: NAME
          - name: CITY
          - name: TIER
      - name: ORDERS_EXPORT
        columns:
          - name: ORDER_ID
          - name: CUST_ID
          - name: AMOUNT
          - name: ORDER_DATE
"""


def test_schema_has_one_entry_per_output_with_meanings_and_not_null_for_not_null_columns(tmp_path):
    repo = stage_sample(tmp_path, "wf_0001")
    export_dbt.export(repo, "wf_0001")
    text = text_of(repo, "models/schema.yml", "wf_0001")
    assert re.findall(r"^  - name: (\w+)$", text, re.MULTILINE) == ["sales_summary", "excluded_orders"]   # no temp model
    assert 'description: "one row per region and size band"' in text
    summary = text.split("  - name: excluded_orders")[0]
    objective = json.loads(repo.wf("wf_0001", "objective.json").read_text(encoding="utf-8"))
    for column in objective["outputs"][0]["columns"]:
        assert f"      - name: {column['name']}\n        description: {json.dumps(column['meaning'])}" in summary
    not_null = re.findall(r"- name: (\w+)\n(?:        description: .*\n)?        data_tests:\n          - not_null", summary)
    assert not_null == ["REGION", "SIZE_BAND"]                                 # the objective's two NOT NULL columns
    assert "- unique" not in text                                              # both keys are two-column keys


def test_a_one_column_key_gets_a_unique_test(tmp_path):
    obj = objective_with(keys=("ID",))
    obj["outputs"][0]["columns"][0]["nullable"] = False
    repo = stage(tmp_path, obj=obj)
    export_dbt.export(repo, bf.WF)
    text = text_of(repo, "models/schema.yml")
    assert ("      - name: ID\n        data_tests:\n"
            "          - not_null:\n              config:\n                severity: warn\n"
            "          - unique:\n              config:\n                severity: warn\n") in text
    assert text.count("- unique") == 1


def test_every_generated_test_is_a_warning_never_a_failure(tmp_path):
    """compare.py treats a golden NULL in a NOT NULL column and a repeating key as advisories, so dbt must too."""
    for name in ("wf_0001", "wf_0007"):
        repo = stage_sample(tmp_path / name, name)
        export_dbt.export(repo, name)
        text = text_of(repo, "models/schema.yml", name)
        tests = re.findall(r"^          - (not_null|unique):$", text, re.MULTILINE)
        assert tests, name
        assert len(re.findall(r"^                severity: warn$", text, re.MULTILINE)) == len(tests)
        assert len(re.findall(r"severity:", text.split("version: 2")[1])) == len(tests)
        assert re.findall(r"^          - (?:not_null|unique)$", text, re.MULTILINE) == [], "no test without its severity"
        assert "advisories (severity: warn)" in text.splitlines()[0]


def test_dbt_project_names_the_project_and_runs_the_alter_session_before_every_model_on_snowflake_only(tmp_path):
    repo = stage_sample(tmp_path, "wf_0001")
    export_dbt.export(repo, "wf_0001")
    assert text_of(repo, "dbt_project.yml", "wf_0001") == """\
name: mig_wf_0001
version: "1.0.0"
config-version: 2
profile: mig_wf_0001
model-paths: [models]
vars:
  src_schema: null
  tgt_schema: null
models:
  +transient: false           # dbt-snowflake makes tables transient unless told otherwise; build.sql's CREATE TABLE is permanent
  # the build's ALTER SESSION, on each model's own connection before its SQL (and before a PreSQL pre_hook)
  +pre-hook:
    - "{% if target.type == 'snowflake' %}ALTER SESSION SET TIMEZONE = 'America/New_York', WEEK_START = 1{% endif %}"
"""


def test_a_build_without_alter_session_has_no_session_hook(tmp_path):
    repo = stage(tmp_path, bf.GOOD_SQL.replace("ALTER SESSION SET TIMEZONE = 'America/New_York', WEEK_START = 1;\n", ""))
    export_dbt.export(repo, bf.WF)
    text = text_of(repo, "dbt_project.yml")
    assert "pre-hook" not in text and "ALTER" not in text and "on-run-start" not in text
    assert text.startswith("name: mig_wf_0009\n") and "+transient: false" in text


@pytest.mark.parametrize("wf_id", SQL_SAMPLES)
def test_no_generated_file_uses_on_run_start_or_claims_a_thread_count_applies_the_session(tmp_path, wf_id):
    """dbt runs `on-run-start` on its master connection and each model on a worker-thread connection of its own, so
    an ALTER SESSION there never reaches a model, with one thread or many: the session setting is a per-model
    pre-hook, and nothing the exporter writes says otherwise."""
    repo = stage_sample(tmp_path, wf_id)
    export_dbt.export(repo, wf_id)
    for relative, raw in files_of(repo, wf_id).items():
        text = raw.decode("utf-8")
        if relative == "README.md":                     # says once why it is not an on-run-start
            text = text.replace("(dbt's `on-run-start` would run on a different connection", "(")
        assert "on-run-start" not in text and "applies to every model" not in text, relative
    assert "session" not in files_of(repo, wf_id)["profiles.yml"].decode("utf-8").lower()   # no thread count claim


def test_profiles_has_a_local_duckdb_target_and_only_a_commented_snowflake_template(tmp_path):
    repo = stage_sample(tmp_path, "wf_0001")
    export_dbt.export(repo, "wf_0001")
    text = text_of(repo, "profiles.yml", "wf_0001")
    live = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    assert live[0] == "mig_wf_0001:" and "type: duckdb" in " ".join(live) and "snowflake" not in " ".join(live)
    assert "# snowflake:" in text and "#   account: <account identifier>" in text
    assert "on-run-start" not in text and "ALTER SESSION" not in text           # the template makes no claim about sessions
    for secret in ("password", "private_key", "token", "passcode"):
        assert not any(secret in line.lower() and not line.lstrip().startswith("#") for line in text.splitlines())
    assert not re.search(r"[A-Za-z]:[\\/]", text)


def test_readme_says_what_each_model_is_and_that_nothing_ran_on_snowflake(tmp_path):
    repo = stage_sample(tmp_path, "wf_0003")
    export_dbt.export(repo, "wf_0003")
    text = text_of(repo, "README.md", "wf_0003")
    assert text.startswith("# wf_0003 — GL period close, as a dbt project\n")
    assert "Nothing in this project has run on a real Snowflake account" in text
    assert "| `models/gl_summary.sql` | output `GL_SUMMARY` | incremental, merge on ACCT, PERIOD; 1 pre_hook, 1 post_hook |" in text
    assert "| `models/latest_entries.sql` | temporary table `latest_entries` | ephemeral" in text
    assert "dbt build --project-dir workflows/wf_0003/dbt --profiles-dir <your profiles dir> --target <name>" in text
    assert '"src_schema"' in text and '"tgt_schema"' in text
    # the session setting: a project-level +pre-hook on each model's connection, before its own PreSQL hook
    assert "is a project-level `+pre-hook` of every model in `dbt_project.yml`, guarded for Snowflake" in text
    assert "before the model's SQL and before the model's own PreSQL `pre_hook`" in text
    assert "It needs dbt-core 1.8 or later (`data_tests:` in `models/schema.yml`)." in text
    assert "`dbt build --full-refresh` leaves it alone" in text


@pytest.mark.parametrize("wf_id", SQL_SAMPLES)
def test_no_file_carries_a_machine_path_a_crlf_or_a_missing_final_newline(tmp_path, wf_id):
    repo = stage_sample(tmp_path, wf_id)
    export_dbt.export(repo, wf_id)
    for relative, raw in files_of(repo, wf_id).items():
        assert b"\r" not in raw and raw.endswith(b"\n"), relative
        text = raw.decode("utf-8")
        assert not re.search(r"[A-Za-z]:[\\/]|/home/|/Users/", text), relative


# --- determinism and replacing an old export -------------------------------------------------------------------------


@pytest.mark.parametrize("wf_id", SQL_SAMPLES)
def test_a_re_export_is_byte_identical(tmp_path, wf_id):
    repo = stage_sample(tmp_path, wf_id)
    export_dbt.export(repo, wf_id)
    first = files_of(repo, wf_id)
    export_dbt.export(repo, wf_id)
    assert files_of(repo, wf_id) == first
    other = stage_sample(tmp_path / "elsewhere", wf_id)                       # and another root gives the same bytes
    export_dbt.export(other, wf_id)
    assert files_of(other, wf_id) == first


def test_an_export_replaces_the_old_project_and_everything_validation_left_in_it(tmp_path):
    repo = stage_sample(tmp_path, "wf_0001")
    old = dbt_dir(repo, "wf_0001")
    (old / "models").mkdir(parents=True)
    (old / "models" / "stale.sql").write_text("select 1\n", encoding="utf-8")
    (old / "validation.json").write_text("{}\n", encoding="utf-8")
    (old / "logs").mkdir()
    sandbox = repo.wf("wf_0001", "dbt_sandbox_normal.duckdb")
    sandbox.write_text("x", encoding="utf-8")
    export_dbt.export(repo, "wf_0001")
    assert not (old / "models" / "stale.sql").exists() and not (old / "validation.json").exists()
    assert not (old / "logs").exists() and not sandbox.exists()
    assert (old / "models" / "sales_summary.sql").is_file()


def test_export_without_write_changes_nothing_on_disk(tmp_path):
    repo = stage_sample(tmp_path, "wf_0001")
    result = export_dbt.export(repo, "wf_0001", write=False)
    assert not dbt_dir(repo, "wf_0001").exists()
    assert "models/sales_summary.sql" in result.files and result.skipped is None


# --- statements the table has no row for: exit 1, naming the line ----------------------------------------------------

MERGE_HEAD = "MERGE INTO {{tgt}}.ITEMS_OUT AS T\nUSING (SELECT ID, NOTE FROM kept) AS S\nON T.ID = S.ID\n"
UNSUPPORTED = [
    ("a merge update with a condition",
     MERGE_HEAD + "WHEN MATCHED AND S.NOTE IS NULL THEN UPDATE SET NOTE = S.NOTE\n"
                  "WHEN NOT MATCHED THEN INSERT (ID, NOTE) VALUES (S.ID, S.NOTE);",
     dict(write_mode="update_insert", keys=("ID",)), "this MERGE's WHEN clauses"),
    ("a merge update that is not a copy of the source",
     MERGE_HEAD + "WHEN MATCHED THEN UPDATE SET NOTE = UPPER(S.NOTE)\n"
                  "WHEN NOT MATCHED THEN INSERT (ID, NOTE) VALUES (S.ID, S.NOTE);",
     dict(write_mode="update_insert", keys=("ID",)), "in this MERGE's UPDATE SET"),
    ("a merge on other keys than the objective's",
     MERGE_HEAD.replace("T.ID = S.ID", "T.NOTE = S.NOTE") + "WHEN MATCHED THEN UPDATE SET NOTE = S.NOTE\n"
                  "WHEN NOT MATCHED THEN INSERT (ID, NOTE) VALUES (S.ID, S.NOTE);",
     dict(write_mode="update_insert", keys=("ID",)), "this MERGE's ON clause"),
    ("a merge that deletes", MERGE_HEAD + "WHEN MATCHED THEN DELETE\n"
                                          "WHEN NOT MATCHED THEN INSERT (ID, NOTE) VALUES (S.ID, S.NOTE);",
     dict(write_mode="update_insert", keys=("ID",)), "this MERGE's WHEN clauses"),
    ("an insert of values", "INSERT INTO {{tgt}}.ITEMS_OUT (ID, NOTE) VALUES (1, 'a');", dict(write_mode="append"),
     "INSERT takes a query"),
    ("an insert into a temporary table", "INSERT INTO kept (ID, NOTE) SELECT ID, NOTE FROM {{src}}.ITEMS;", {},
     "INSERT writes {{tgt}}.<LOGICAL>, not a temporary table"),
    ("a plain table that is not temporary", "CREATE TABLE scratch AS SELECT ID FROM {{src}}.ITEMS;", {},
     "a table is created as {{tgt}}.<LOGICAL> or as a TEMPORARY table"),
    ("a view", "CREATE VIEW v AS SELECT ID FROM {{src}}.ITEMS;", {}, "expected CREATE [OR REPLACE] [TEMPORARY] TABLE"),
    ("a delete that is not on a target", "DELETE FROM kept WHERE ID = 1;", {}, "allowed only on one {{tgt}}.<LOGICAL>"),
    ("a procedure call", "CALL cleanup();", {}, "`CALL cleanup()`, which sqlglot's Snowflake parser does not understand"),
    ("a table named without a placeholder", "CREATE OR REPLACE TABLE {{tgt}}.ITEMS_OUT AS SELECT ID, NOTE FROM RAW.PUBLIC.ITEMS;",
     {}, "RAW.PUBLIC.ITEMS, a table named without {{src}} or {{tgt}}"),
    ("a table named through IDENTIFIER", "CREATE OR REPLACE TABLE {{tgt}}.ITEMS_OUT AS SELECT ID, NOTE FROM IDENTIFIER('ITEMS');",
     {}, "a table named through IDENTIFIER(...)"),
    ("a source that is not an input", "CREATE OR REPLACE TABLE {{tgt}}.ITEMS_OUT AS SELECT ID, NOTE FROM {{src}}.OTHER;", {},
     "{{src}}.OTHER, which is not an input of objective.json"),
    ("a temporary table that nothing created", "CREATE OR REPLACE TABLE {{tgt}}.ITEMS_OUT AS SELECT ID, NOTE FROM missing;",
     {}, "missing, read before any CREATE TEMPORARY TABLE missing makes it"),
    ("a target that is not an output", "CREATE OR REPLACE TABLE {{tgt}}.ITEMS_OUT AS SELECT ID, NOTE FROM {{tgt}}.SECOND;",
     {}, "{{tgt}}.SECOND, which is not an output of objective.json"),
    ("a placeholder that is not a table", "CREATE OR REPLACE TABLE {{tgt}}.ITEMS_OUT AS SELECT '{{src}}' AS NOTE, ID FROM kept;",
     {}, "a {{src}} or {{tgt}} placeholder that is not the start of a table name"),
    ("a statement sqlglot only reads as a command", "CREATE OR REPLACE TABLE {{tgt}}.ITEMS_OUT AS SELEKT ID FROM kept;", {},
     ", which sqlglot's Snowflake parser does not understand"),
    ("a statement that does not parse", "CREATE OR REPLACE TABLE {{tgt}}.ITEMS_OUT AS SELECT ID, ( FROM kept;", {},
     ", which does not parse as Snowflake SQL"),
]


@pytest.mark.parametrize("label, statement, changes, expect", UNSUPPORTED, ids=[row[0] for row in UNSUPPORTED])
def test_a_statement_with_no_dbt_form_is_exit_1_naming_its_line(tmp_path, capsys, label, statement, changes, expect):
    text = build(KEPT, statement)
    # a write that is not the output's own: the output still needs its write for the objective to be whole
    if "ITEMS_OUT" not in statement:
        text = build(KEPT, statement, OVERWRITE)
    repo = stage(tmp_path, text, objective_with(**changes) if changes else None)
    stale = dbt_dir(repo)
    (stale / "models").mkdir(parents=True)
    (stale / "models" / "old.sql").write_text("select 1\n", encoding="utf-8")
    line = text.splitlines().index(statement.splitlines()[0]) + 1
    assert export_dbt.main([bf.WF, "--root", str(tmp_path)]) == 1
    first = capsys.readouterr().err.splitlines()[0]
    assert first.startswith(f"build.sql line {line}: no dbt form for "), first
    assert expect in first, first
    assert not stale.exists(), "a project that does not match the build is never left behind"


def test_the_error_names_the_exact_line_of_the_statement(tmp_path):
    text = build(KEPT, "CALL cleanup();", OVERWRITE)
    repo = stage(tmp_path, text)
    with pytest.raises(export_dbt.NoDbtForm) as raised:
        export_dbt.export(repo, bf.WF)
    assert raised.value.line == text.splitlines().index("CALL cleanup();") + 1
    assert str(raised.value).startswith(f"build.sql line {raised.value.line}: no dbt form for ")


def test_a_target_read_before_the_statement_that_writes_it_has_no_form(tmp_path):
    obj = objective_with()
    extra = copy.deepcopy(obj["outputs"][0])
    extra.update(tool_id="8", logical="SECOND", columns=[obj["outputs"][0]["columns"][0]])
    obj["outputs"].append(extra)
    reads = "CREATE OR REPLACE TABLE {{tgt}}.ITEMS_OUT AS\nSELECT ID, NOTE FROM {{tgt}}.SECOND;"
    writes = "CREATE OR REPLACE TABLE {{tgt}}.SECOND AS\nSELECT ID FROM kept;"
    text = build(KEPT, reads, writes)
    repo = stage(tmp_path, text, obj)
    with pytest.raises(export_dbt.NoDbtForm, match=r"\{\{tgt\}\}\.SECOND, read before the statement that writes it") as raised:
        export_dbt.export(repo, bf.WF)
    assert raised.value.line == text.splitlines().index("SELECT ID, NOTE FROM {{tgt}}.SECOND;") + 1


@pytest.mark.parametrize("where, statement, objective", [
    ("PreSQL", "DELETE FROM {{tgt}}.ITEMS_OUT WHERE ID IN (SELECT ID FROM kept);", dict(pre_sql="DELETE FROM X")),
    ("PostSQL", "UPDATE {{tgt}}.ITEMS_OUT SET NOTE = (SELECT MAX(NOTE) FROM kept) WHERE NOTE IS NULL;",
     dict(post_sql="UPDATE X SET Y = 1")),
])
def test_a_pre_or_post_sql_that_reads_a_temporary_table_has_no_form(tmp_path, capsys, where, statement, objective):
    """A hook gets no CTE for an ephemeral model, so `{{ ref('kept') }}` in it would name nothing at run time."""
    text = build(KEPT, statement, OVERWRITE) if where == "PreSQL" else build(KEPT, OVERWRITE, statement)
    repo = stage(tmp_path, text, objective_with(**objective))
    stale = dbt_dir(repo)
    (stale / "models").mkdir(parents=True)
    line = text.splitlines().index(statement) + 1
    assert export_dbt.main([bf.WF, "--root", str(tmp_path)]) == 1
    first = capsys.readouterr().err.splitlines()[0]
    assert first == (f"build.sql line {line}: no dbt form for kept, a temporary table read by a PreSQL/PostSQL statement "
                     f"(it becomes a hook, and dbt gives a hook no CTE for an ephemeral model)")
    assert not stale.exists()


def test_a_hook_may_read_a_source_and_a_target_that_was_written_before_it(tmp_path):
    pre = "DELETE FROM {{tgt}}.ITEMS_OUT WHERE ID NOT IN (SELECT ID FROM {{src}}.ITEMS);"
    repo = stage(tmp_path, build(KEPT, pre, OVERWRITE), objective_with(pre_sql="DELETE FROM X"))
    assert models_of(repo)["items_out"].pre_hooks == [
        "DELETE FROM {{ this }} WHERE ID NOT IN (SELECT ID FROM {{ source('wf_0009', 'ITEMS') }})"]


def test_a_usage_error_leaves_an_old_project_where_it_is(tmp_path, capsys):
    """Exit 2 learned nothing about the build, so an old `dbt/` is neither validated nor removed (a skip and exit 1 remove it)."""
    repo = stage_sample(tmp_path, "wf_0001")
    export_dbt.export(repo, "wf_0001")
    before = files_of(repo, "wf_0001")
    (repo.wf("wf_0001") / "build.py").write_text("def main(session, src, tgt): return 'OK'\n", encoding="utf-8")
    assert export_dbt.main(["wf_0001", "--root", str(tmp_path)]) == 2
    assert "both build.sql and build.py exist" in capsys.readouterr().err
    assert files_of(repo, "wf_0001") == before


def test_a_second_create_of_a_temporary_table_has_no_form(tmp_path):
    repo = stage(tmp_path, build(KEPT, KEPT, OVERWRITE))
    with pytest.raises(export_dbt.NoDbtForm, match="the temporary table kept created a second time"):
        export_dbt.export(repo, bf.WF)


def test_an_alter_session_after_the_first_statement_has_no_form(tmp_path):
    repo = stage(tmp_path, build(KEPT, "ALTER SESSION SET TIMEZONE = 'UTC';", OVERWRITE))
    with pytest.raises(export_dbt.NoDbtForm, match="an ALTER SESSION after the first statement"):
        export_dbt.export(repo, bf.WF)


def test_an_output_the_build_never_writes_has_no_form(tmp_path):
    repo = stage(tmp_path, build(KEPT))
    with pytest.raises(export_dbt.NoDbtForm, match=r"\{\{tgt\}\}\.ITEMS_OUT, which the build never writes"):
        export_dbt.export(repo, bf.WF)


def test_a_write_in_another_form_than_the_objectives_mode_has_no_form(tmp_path):
    repo = stage(tmp_path, build(KEPT, OVERWRITE), objective_with(write_mode="append"))
    with pytest.raises(export_dbt.NoDbtForm, match="objective.json says append"):
        export_dbt.export(repo, bf.WF)


def test_a_truncate_append_whose_truncate_is_not_immediately_before_the_insert_has_no_form(tmp_path):
    insert = "INSERT INTO {{tgt}}.ITEMS_OUT (ID, NOTE)\nSELECT ID, NOTE FROM kept;"
    repo = stage(tmp_path, build(KEPT, "TRUNCATE TABLE {{tgt}}.ITEMS_OUT;", KEPT.replace("kept", "other"), insert),
                 objective_with(write_mode="truncate_append"))
    with pytest.raises(export_dbt.NoDbtForm, match="immediately before the INSERT"):
        export_dbt.export(repo, bf.WF)


# --- the skips: nothing written, any old dbt/ removed ------------------------------------------------------------------


def test_program_dbt_false_skips_the_export_and_removes_an_old_project(tmp_path, capsys):
    repo = stage(tmp_path)
    export_dbt.export(repo, bf.WF)
    assert dbt_dir(repo).is_dir()
    set_dbt(repo, False)
    assert export_dbt.main([bf.WF, "--root", str(tmp_path)]) == 0
    assert "dbt export skipped: program.dbt is false in mappings/global.yaml" in capsys.readouterr().out
    assert not dbt_dir(repo).exists()


def test_a_python_workflow_is_skipped_and_an_old_project_removed(tmp_path, capsys):
    obj = objective_with(language="python")
    repo = stage(tmp_path, "def main(session, src, tgt):\n    return 'OK'\n", obj, name="build.py")
    (dbt_dir(repo) / "models").mkdir(parents=True)
    (dbt_dir(repo) / "dbt_project.yml").write_text("name: old\n", encoding="utf-8")
    assert export_dbt.main([bf.WF, "--root", str(tmp_path)]) == 0
    assert "dbt export skipped: the objective's language is python" in capsys.readouterr().out
    assert not dbt_dir(repo).exists()


def test_a_skip_writes_nothing_even_where_nothing_was_there(tmp_path):
    repo = stage(tmp_path)
    set_dbt(repo, False)
    export_dbt.main([bf.WF, "--root", str(tmp_path)])
    assert sorted(path.name for path in repo.wf(bf.WF).iterdir()) == ["build.sql", "objective.json"]


def test_the_export_is_on_by_default_and_when_program_dbt_is_true(tmp_path):
    repo = stage(tmp_path)
    assert not repo.global_mappings.exists()
    assert export_dbt.export(repo, bf.WF, write=False).skipped is None
    set_dbt(repo, True)
    assert export_dbt.export(repo, bf.WF, write=False).skipped is None
    write_yaml(repo.global_mappings, {"program": {"warehouse": "W"}})
    assert export_dbt.export(repo, bf.WF, write=False).skipped is None


# --- usage errors: exit 2 ----------------------------------------------------------------------------------------------


def test_usage_errors_are_exit_2_and_say_what_is_missing(tmp_path, capsys):
    assert export_dbt.main(["wf_9999", "--root", str(tmp_path)]) == 2
    assert "wf_9999 has no objective.json" in capsys.readouterr().err

    repo = stage(tmp_path, name=None)                                         # an objective and no build file
    assert export_dbt.main([bf.WF, "--root", str(tmp_path)]) == 2
    assert "has no build file to export" in capsys.readouterr().err

    (repo.wf(bf.WF) / "build.sql").write_text(bf.GOOD_SQL, encoding="utf-8")
    (repo.wf(bf.WF) / "build.py").write_text("def main(session, src, tgt): return 'OK'\n", encoding="utf-8")
    assert export_dbt.main([bf.WF, "--root", str(tmp_path)]) == 2
    assert "both build.sql and build.py exist" in capsys.readouterr().err


def test_a_build_py_for_a_sql_objective_is_a_usage_error(tmp_path, capsys):
    stage(tmp_path, "def main(session, src, tgt): return 'OK'\n", name="build.py")
    assert export_dbt.main([bf.WF, "--root", str(tmp_path)]) == 2
    assert "is a python build but objective.json says language 'sql'" in capsys.readouterr().err


def test_a_tier_t3_objective_is_skipped_like_a_python_one(tmp_path, capsys):
    obj = objective_with()
    obj["tier"] = "T3"
    repo = stage(tmp_path, None, obj, name=None)
    (dbt_dir(repo) / "models").mkdir(parents=True)
    assert export_dbt.main([bf.WF, "--root", str(tmp_path)]) == 0
    assert "dbt export skipped: the objective says tier T3: the workflow is not translated" in capsys.readouterr().out
    assert not dbt_dir(repo).exists()


def test_an_objective_that_cannot_say_what_the_export_needs_is_exit_2(tmp_path, capsys):
    obj = objective_with()
    obj["outputs"][0]["logical"] = None
    stage(tmp_path, None, obj)
    assert export_dbt.main([bf.WF, "--root", str(tmp_path)]) == 2
    assert "has no logical name" in capsys.readouterr().err

    obj = objective_with(write_mode="update_insert", keys=())
    stage(tmp_path / "keys", None, obj)
    assert export_dbt.main([bf.WF, "--root", str(tmp_path / "keys")]) == 2
    assert "names no keys" in capsys.readouterr().err


def test_program_dbt_must_be_true_or_false(tmp_path, capsys):
    repo = stage(tmp_path)
    set_dbt(repo, "yes")
    assert export_dbt.main([bf.WF, "--root", str(tmp_path)]) == 2
    assert "program.dbt is 'yes'; expected true or false" in capsys.readouterr().err


def test_an_unknown_argument_is_a_usage_error():
    with pytest.raises(SystemExit) as raised:
        export_dbt.main(["wf_0001", "--bogus"])
    assert raised.value.code == 2


def test_an_invalid_workflow_id_is_a_usage_error_and_touches_nothing(tmp_path, capsys):
    assert export_dbt.main(["../escape", "--root", str(tmp_path)]) == 2
    assert "invalid workflow id" in capsys.readouterr().err


# --- the script as a process, and dbt reading what it wrote -----------------------------------------------------------


def test_the_script_runs_as_a_process(tmp_path):
    stage_sample(tmp_path, "wf_0001")
    done = subprocess.run([sys.executable, str(Path(export_dbt.__file__)), "wf_0001", "--root", str(tmp_path)],
                          capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "wf_0001: wrote workflows/wf_0001/dbt (3 models: clean_orders, sales_summary, excluded_orders)"
    assert (tmp_path / "workflows" / "wf_0001" / "dbt" / "dbt_project.yml").is_file()


@pytest.mark.parametrize("wf_id", ["wf_0003", "wf_0007"])
def test_dbt_parses_the_exported_project(tmp_path, wf_id):
    """`dbt parse` (dbt-core's own reader of the Jinja, the config, the sources and schema YAML and every ref) accepts
    what the exporter wrote; it connects to nothing and runs nothing."""
    repo = stage_sample(tmp_path, wf_id)
    export_dbt.export(repo, wf_id)
    result = dbt_project.run_dbt("parse", dbt_dir(repo, wf_id), vars=dbt_project.local_vars("MIG_GOLDEN_X"),
                                 duckdb_path=tmp_path / "parse.duckdb")
    assert result.ok, result.output
    assert result.manifest is not None
    nodes = {node["name"]: node for node in result.manifest["nodes"].values() if node["resource_type"] == "model"}
    assert set(nodes) == set(EXPECTED_MODELS[wf_id])
    for name, (materialized, strategy) in EXPECTED_MODELS[wf_id].items():
        config = nodes[name]["config"]
        assert config["materialized"] == materialized and config.get("incremental_strategy") == strategy
        assert config["full_refresh"] is (False if materialized == "incremental" else None)
        # every model's first pre-hook is the build's session setting, the project's; a PreSQL comes after it
        session = ("{% if target.type == 'snowflake' %}ALTER SESSION SET TIMEZONE = 'America/New_York', "
                   "WEEK_START = 1{% endif %}")
        assert [hook["sql"] for hook in config["pre-hook"]][:1] == [session]
    if wf_id == "wf_0003":
        assert [hook["sql"] for hook in nodes["gl_summary"]["config"]["pre-hook"]][1:] == [
            "DELETE FROM {{ this }} WHERE PERIOD < '2020-01'"]


def test_dbt_parses_a_model_whose_comment_and_string_hold_jinja_lookalikes(tmp_path):
    kept = ("-- reads {{src}}.ITEMS, then {% if x %} and {# y #}\nCREATE OR REPLACE TEMPORARY TABLE kept AS\n"
            "SELECT ID, 'a{#b' AS NOTE FROM {{src}}.ITEMS;")
    repo = stage(tmp_path, build(kept, OVERWRITE))
    export_dbt.export(repo, bf.WF)
    result = dbt_project.run_dbt("parse", dbt_dir(repo), vars=dbt_project.local_vars("MIG_GOLDEN_X"),
                                 duckdb_path=tmp_path / "parse.duckdb")
    assert result.ok, result.output
    compiled = next(node for node in result.manifest["nodes"].values() if node["name"] == "kept")
    assert "'a{#b'" in compiled["raw_code"].replace("{{ '{' }}", "{")
