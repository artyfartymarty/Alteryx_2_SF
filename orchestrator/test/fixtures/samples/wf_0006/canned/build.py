"""wf_0006 · Scored leads -- a test fixture for the orchestrator's own tests; it is never run."""


def main(session, src: str, tgt: str) -> str:
    # step s1
    leads = session.table(f"{src}.LEADS")
    leads.write.save_as_table(f"{tgt}.SCORED_LEADS", mode="overwrite")
    return "OK"
