"""
Atomic key generation against JobBOSS's internal counter table.

JobBOSS doesn't use SQL Server IDENTITY for Quote/Quote_Qty/Quote_Req/
Quote_Req_Qty — it maintains its own shared counter table, tblJB_Keys,
one row per table name. The JobBOSS client increments the relevant row
and uses the new value as the next key, the same pattern implemented
here.

Must always be called inside an existing transaction (a connection with
autocommit off) — this function does not commit. Uses UPDATE ... OUTPUT
to increment and read the new value in one atomic round-trip, avoiding
a separate SELECT-then-UPDATE race window between two processes pulling
the same key at the same time.
"""


def next_key(cursor, table_name: str) -> int:
    """
    Atomically increments tblJB_Keys.K_KeyValue for `table_name` and
    returns the new value. Caller's connection must have an open
    transaction; caller is responsible for commit/rollback.
    """
    cursor.execute(
        """
        UPDATE tblJB_Keys
        SET K_KeyValue = K_KeyValue + 1
        OUTPUT INSERTED.K_KeyValue
        WHERE K_Table = ?
        """,
        table_name,
    )
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError(
            f"No tblJB_Keys row found for K_Table = '{table_name}' — "
            "cannot generate a key. Check the table name matches exactly."
        )
    return row[0]