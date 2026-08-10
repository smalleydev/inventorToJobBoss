"""
Interface to the BOM_Staging tables (see staging_schema.sql).

The core safety mechanism lives in try_claim_quote(): it attempts to
INSERT a header row keyed by QuoteNumber. SQL Server's own PRIMARY KEY
constraint makes this atomic — if two service instances (or two runs of
this one) race to claim the same quote number, exactly one INSERT
succeeds and the other gets a duplicate-key error, with no window for
both to believe they own it. This is the same pattern JobBOSS's own
p_GetNextKey/tblJB_Keys mechanism relies on, just via a UNIQUE
constraint instead of an atomic UPDATE...OUTPUT.
"""

import pyodbc

# SQL Server's error code for a primary-key/unique-constraint violation.
DUPLICATE_KEY_SQLSTATE = "23000"


def try_claim_quote(cursor, quote_number: str, source_file: str,
                    submitted_by: str = None) -> bool:
    """
    Attempts to claim `quote_number` for processing by this instance.

    Two paths to a successful claim, both atomic (no race window):
      1. INSERT a brand-new header row — normal case, first time this
         quote number has ever been submitted.
      2. If the INSERT fails because the row already exists, try an
         UPDATE that only succeeds if the existing row's Status is
         'ERROR' — reclaiming a previously-failed submission for retry
         (e.g. the engineer fixed the underlying data issue and
         re-exported). SQL Server serializes UPDATEs on the same row,
         so if two processes somehow raced on this, only one UPDATE
         actually matches and succeeds.

    A row sitting in PENDING (currently being worked, or a crashed/
    abandoned claim) is NEVER auto-reclaimed here — that's deliberate.
    A genuinely stuck PENDING row needs a human to confirm it's dead
    and manually UPDATE its Status to 'ERROR' before it becomes
    retryable through the normal path above. This avoids ever needing
    DELETE rights or a stored procedure — every state transition is a
    plain INSERT or UPDATE.

    Returns True if the claim succeeded (this instance now owns the
    quote — proceed with staging details + the JobBOSS write). Returns
    False if the quote is currently PENDING or already IMPORTED
    elsewhere — the caller should back off, not retry immediately.

    Commits immediately either way: the claim needs to be visible to
    other instances right away, independent of whatever transaction
    the actual JobBOSS write happens in afterward.
    """
    try:
        cursor.execute(
            """
            INSERT INTO Integration.BOM_Staging_Header
                (QuoteNumber, SourceFile, SubmittedBy, Status)
            VALUES (?, ?, ?, 'PENDING')
            """,
            quote_number, source_file, submitted_by,
        )
        cursor.connection.commit()
        return True
    except pyodbc.IntegrityError as exc:
        if exc.args[0] != DUPLICATE_KEY_SQLSTATE:
            raise  # a different integrity error is a real problem, not a lock miss
        cursor.connection.rollback()

    # Row already exists — only reclaim it if it previously failed.
    cursor.execute(
        """
        UPDATE Integration.BOM_Staging_Header
        SET Status = 'PENDING', SourceFile = ?, SubmittedBy = ?,
            ImportedAt = GETDATE(), RejectReason = NULL,
            QuoteGuid = NULL, ProcessedAt = NULL
        WHERE QuoteNumber = ? AND Status = 'ERROR'
        """,
        source_file, submitted_by, quote_number,
    )
    reclaimed = cursor.rowcount == 1
    cursor.connection.commit() if reclaimed else cursor.connection.rollback()
    return reclaimed


def insert_staging_details(cursor, quote_number: str, rows: list[dict]) -> None:
    """Inserts one detail row per material line for an already-claimed
    quote. Caller manages the transaction (commit/rollback alongside
    the actual JobBOSS write, so staging and the real quote stay in
    sync — a rolled-back write shouldn't leave orphaned staging rows
    claiming success)."""
    for row in rows:
        cursor.execute(
            """
            INSERT INTO Integration.BOM_Staging_Detail
                (QuoteNumber, PartNumber, Description, Quantity, Material,
                 Category, CutLengthIn, JobBossMaterial, MatchStatus,
                 TravelerState, ConflictNotes, SourcePartNumbers, CutList,
                 TotalStockLengthIn, MaterialUsedIn)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            quote_number,
            row.get("PartNumber"), row.get("Description"), row.get("Quantity"),
            row.get("Material"), row.get("Category"), row.get("CutLengthIn"),
            row.get("JobBossMaterial"), row.get("MatchStatus"), row.get("TravelerState"),
            row.get("ConflictNotes"), row.get("SourcePartNumbers"), row.get("CutList"),
            row.get("TotalStockLengthIn"), row.get("MaterialUsedIn"),
        )


def mark_imported(cursor, quote_number: str, quote_guid: str) -> None:
    """Marks a claimed quote as successfully written to JobBOSS. Own
    commit — called after the write_quote transaction has already
    committed, so this just records the outcome."""
    cursor.execute(
        """
        UPDATE Integration.BOM_Staging_Header
        SET Status = 'IMPORTED', QuoteGuid = ?, ProcessedAt = GETDATE()
        WHERE QuoteNumber = ?
        """,
        quote_guid, quote_number,
    )
    cursor.connection.commit()


def mark_error(cursor, quote_number: str, reason: str) -> None:
    """Marks a claimed quote as failed. Own commit, same reasoning as
    mark_imported — this records the outcome after the failed write's
    own transaction has already been rolled back."""
    cursor.execute(
        """
        UPDATE Integration.BOM_Staging_Header
        SET Status = 'ERROR', RejectReason = ?, ProcessedAt = GETDATE()
        WHERE QuoteNumber = ?
        """,
        reason[:500], quote_number,  # column is VARCHAR(500)
    )
    cursor.connection.commit()