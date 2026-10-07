# wf_0005 — Vendor account dedupe

## Purpose

Reads vendor accounts, keeps one row per account with the third-party Acme Dedupe tool, notifies the vendor team with a Run Command and writes the cleaned accounts. It cannot be migrated automatically: the vendor tool's behaviour and the notification script are not something a translation can reproduce.

## Inputs

- **ACCOUNTS** (tool 1): ACCT VARCHAR(20), UPDATED DATE, BALANCE FLOAT.

## Outputs

### ACCOUNTS_CLEAN (tool 4)

- Grain: one row per vendor account, if the vendor tool does what its configuration suggests (unverified).
- Written: overwrite.
- Key: none declared (compared as a set of rows).
- Shape: filter rows; no row order to keep (Alteryx's order is not known to be deterministic).
- Columns:
  - `ACCT` VARCHAR(20): Vendor account number
  - `UPDATED` DATE: Date the account was last updated; NULL when the vendor has none
  - `BALANCE` FLOAT: Account balance

## Steps

- **s1 · Read vendor accounts** (tools 1): Reads ACCOUNTS: ACCT, UPDATED and BALANCE. Feeds s2.
- **s2 · Dedupe the accounts with the vendor tool** (tools 2): The third-party Acme Dedupe tool is configured with key ACCT, keep MaxDate and date field UPDATED, which reads as one row per ACCT chosen by UPDATED. Its configuration does not say what happens to a NULL UPDATED, to a tie on UPDATED, or whether it compares the date or a full timestamp. Feeds s3.
- **s3 · Notify the vendor team** (tools 3): Runs notify.bat on the Alteryx host with a Run Command; the rows pass through. Feeds s4.
- **s4 · Write the clean accounts** (tools 4): Writes ACCOUNTS_CLEAN, replacing it. Feeds output 4.

## Business rules

- The workflow is not translated: tool 2 is a third-party plugin the parser does not know and tool 3 starts a process on a host, which Snowflake cannot do, so the tier is T3 and the translate stage ends at MANUAL.
- ACCOUNTS_CLEAN is replaced on every run.

## Alteryx behaviour to keep

- Tool 2, UNKNOWN: What the Acme Dedupe tool does with a NULL UPDATED, with a tie on UPDATED, and whether it compares the date or a full timestamp: its XML does not say, and each choice would show up as a data difference rather than an error. A person who knows the vendor tool has to decide before anyone writes the translation.
- Tool 3, UNKNOWN: What replaces notify.bat -- a Snowflake task, an external function, a step in whatever orchestrates the migrated build, or nothing: Snowflake cannot start a process on a host, so the notification has to be rebuilt outside the warehouse.

## Questions

None open. The two decisions that keep this workflow from being migrated automatically are recorded above, under Alteryx behaviour to keep.
