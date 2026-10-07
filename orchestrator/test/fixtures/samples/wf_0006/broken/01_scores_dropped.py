"""wf_0006 · Scored leads -- BROKEN BUILD (test fixture): the scores are dropped."""


def main(session, src: str, tgt: str) -> str:
    # step s1
    leads = session.table(f"{src}.LEADS").drop("SCORE")
    leads.write.save_as_table(f"{tgt}.SCORED_LEADS", mode="overwrite")
    return "OK"
