# wf_0006 - Subscription revenue recognition
# Written from objective.md; checked against golden data. Nothing in this file has run on Snowflake or Alteryx.
# Python because the Python tool (tool 3) carries a deferred balance from one row to the next inside a customer:
# min(carried + billed, cap) does not distribute over a running sum, so no window function expresses it.
import pandas as pd
from snowflake.snowpark.functions import col, count, lit, sum as total
from snowflake.snowpark.types import DoubleType, StringType, StructField, StructType


def main(session, src: str, tgt: str) -> str:
    # step s1
    # Filter [BILLED] > 0, strict: a BILLED of exactly 0 goes too, and NaN > 0 is False, so a NULL BILLED is
    # dropped as Alteryx's unwired False anchor does. Done on the pandas frame: the Local Testing Framework
    # cannot filter an empty table.
    pdf = session.table(f"{src}.SUBSCRIPTIONS").to_pandas()
    pdf = pdf[pdf["BILLED"] > 0]

    # step s2
    # The Python tool: per customer, in CUSTOMER and PERIOD order (the script sorts for itself), the deferred
    # balance carries over and resets to 0 on a cancelled period BEFORE that period's own cap applies. Rounding is
    # Python's round, as in the script, not the Alteryx Round. A NULL CANCELLED fails loudly, as the script does
    # (bool(pd.NA) raises, while the framework's None would quietly be False).
    pdf = pdf.sort_values(["CUSTOMER", "PERIOD"]).reset_index(drop=True)
    schedule = []
    for customer, group in pdf.groupby("CUSTOMER", sort=False):
        deferred = 0.0
        for _, row in group.iterrows():
            if pd.isna(row["CANCELLED"]):
                raise ValueError(f"CANCELLED is NULL for {customer} {row['PERIOD']}")
            if bool(row["CANCELLED"]):
                deferred = 0.0
            billed = float(row["BILLED"])
            recognized = min(billed + deferred, float(row["CAP"]))
            deferred = billed + deferred - recognized
            schedule.append([row["PERIOD"], round(recognized, 2), round(deferred, 2)])
    schema = StructType([StructField("PERIOD", StringType(254)), StructField("RECOGNIZED", DoubleType()),
                         StructField("DEFERRED", DoubleType())])

    # step s3
    # Summarize by PERIOD: Sum of a Double is a Double, Count counts rows. No sort: the target is keyed on PERIOD.
    totals = session.create_dataframe(schedule, schema=schema).group_by("PERIOD").agg(
        total(col("RECOGNIZED")).alias("TOTAL_RECOGNIZED"), total(col("DEFERRED")).alias("TOTAL_DEFERRED"),
        count(lit(1)).alias("CUSTOMERS"))
    totals.write.mode("overwrite").save_as_table(f"{tgt}.REVENUE_BY_PERIOD")
    return "OK"
