"""
Shown when finalize_and_export's pre-check finds the quote number
already locked — IMPORTED or PENDING in Integration.BOM_Staging_Header
— before any new write is attempted. A one-time check against a single
snapshot of status, not a live poll (that's PushOutcomeDialog's job,
shown separately after an actual write attempt).

IMPORTED — a quote was already successfully written. Two real reasons
an engineer might be here on purpose:
  - Accidentally clicked Finalize again on the same, still-correct BOM
    (should NOT create a duplicate quote — just informational, Close).
  - The BOM genuinely needs to be rebuilt (a mistake was found, a
    revision came in) and this quote needs to be replaced.

REBUILD FLOW (per Shane's Integration.usp_ResetQuoteForRerun design —
IT owns the actual delete of JobBOSS quote data now, not JL Check):
  1. Engineer deletes the existing quote themselves in the JobBOSS
     client (Quote/Quote_Req/Quote_Qty/Quote_Req_Qty/Bill_Of_Quotes).
     JL Check has no DELETE rights on any of those tables — engineers
     only ever get EXECUTE on the reset proc, nothing broader.
  2. "Unlock and Rebuild" here re-checks `Quote WHERE RFQ = ?` (the
     exact guard write_quote() itself uses before every push) to
     confirm the delete actually happened before doing anything else.
     If a Quote row is still there, this refuses to proceed — the
     rebuild can't safely continue until JobBOSS is actually clear.
  3. Once confirmed clear, calls
     EXEC Integration.usp_ResetQuoteForRerun @QuoteNumber, @QuoteGuid
     which clears the stale Integration.BOM_Staging_Detail rows and
     flips the staging header from IMPORTED to ERROR.
  4. Normal watchdog flow reclaims ERROR -> PENDING and rebuilds.

PENDING — likely a stale claim left behind by a crashed run. "Unlock"
here just resets the staging lock (no JobBOSS data exists yet for a
PENDING row, so there's nothing to delete or verify).

Both paths end the same way: a second, distinct button
("Continue with Finalize") is the explicit decision to proceed with
writing — deliberately never the same click as the reset action
itself. Either step can be abandoned via Close with no side effects
beyond whatever was already confirmed and applied.

Error handling: every DB step below runs inside a try/except that
rolls back the connection and shows a QMessageBox.critical with the
real exception text. Without this, a failure partway through would
throw inside a Qt slot and, in a --windowed PyInstaller build,
disappear with no console to print to — the confirm box just closes
and the dialog silently reverts to its pre-click state, which is
exactly the "pressing Yes does nothing" symptom this was built to fix.
"""

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QMessageBox, QPushButton, QVBoxLayout,
)


def _jobboss_quote_still_exists(cursor, quote_number: str) -> bool:
    """Same guard write_quote() runs before every push — reused here so
    the rebuild path and the normal push path agree on what "clear"
    means. True means the engineer has NOT actually deleted the quote
    in JobBOSS yet (or the delete didn't fully take)."""
    cursor.execute("SELECT Quote FROM Quote WHERE RFQ = ?", quote_number)
    return cursor.fetchone() is not None


class QuoteLockedDialog(QDialog):
    """
    After exec(), check `dialog.proceed` — True means the caller should
    go ahead and write the export now; False (or dialog closed/
    cancelled) means stop, nothing should be written.
    """

    def __init__(self, quote_number: str, status_row, cursor, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Quote Locked — {quote_number}")
        self.resize(480, 240)

        self._quote_number = quote_number
        self._status_row = status_row
        self._cursor = cursor
        self.proceed = False

        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        self.message_label = QLabel()
        self.message_label.setWordWrap(True)
        layout.addWidget(self.message_label)

        self.action_btn = QPushButton()
        self.action_btn.setVisible(False)
        layout.addWidget(self.action_btn)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if self._status_row.Status == "IMPORTED":
            self.message_label.setText(
                f"This quote number was already written to JobBOSS "
                f"(GUID {self._status_row.QuoteGuid}, at "
                f"{self._status_row.ProcessedAt}).\n\n"
                f"If this BOM needs to be rebuilt (a mistake or revision), "
                f"first delete the existing quote yourself in JobBOSS, then "
                f"click below to reset the lock and push a fresh one. If "
                f"you just clicked Finalize again by accident, close this "
                f"instead — nothing has changed."
            )
            self.action_btn.setText("Unlock and Rebuild")
            self.action_btn.setVisible(True)
            self.action_btn.clicked.connect(self._on_rebuild_clicked)

        else:  # PENDING
            self.message_label.setText(
                f"This quote number is currently locked — claimed at "
                f"{self._status_row.ImportedAt} and not yet finished.\n\n"
                f"If you're confident this is a stale lock from a crashed "
                f"run (not an active push happening right now), you can "
                f"unlock it below."
            )
            self.action_btn.setText("Unlock")
            self.action_btn.setVisible(True)
            self.action_btn.clicked.connect(self._on_unlock_pending_clicked)

    def _on_rebuild_clicked(self) -> None:
        confirm = QMessageBox.warning(
            self, "Confirm rebuild",
            f"Before continuing, you must have ALREADY deleted the "
            f"existing quote for '{self._quote_number}' (GUID "
            f"{self._status_row.QuoteGuid}) yourself in the JobBOSS "
            f"client. JL Check will verify this and refuse to continue "
            f"if it's still there.\n\n"
            f"Have you deleted it in JobBOSS and want to reset the lock "
            f"now?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        if _jobboss_quote_still_exists(self._cursor, self._quote_number):
            QMessageBox.warning(
                self, "Quote still exists",
                f"'{self._quote_number}' still has a Quote record in "
                f"JobBOSS. Delete it in the JobBOSS client first, then "
                f"try again — nothing has changed here.",
            )
            return

        try:
            self._cursor.execute(
                "EXEC Integration.usp_ResetQuoteForRerun "
                "@QuoteNumber = ?, @QuoteGuid = ?",
                self._quote_number, self._status_row.QuoteGuid,
            )
            self._cursor.connection.commit()
        except Exception as exc:
            self._cursor.connection.rollback()
            QMessageBox.critical(
                self, "Rebuild failed",
                f"Nothing was changed — rolled back.\n\nError:\n{exc}",
            )
            return

        self._show_continue_state(
            "Lock reset. Click below to push a fresh quote."
        )

    def _on_unlock_pending_clicked(self) -> None:
        confirm = QMessageBox.warning(
            self, "Confirm unlock",
            f"This will mark '{self._quote_number}' as failed in the "
            f"staging log, allowing a fresh submission to claim it.\n\n"
            f"Only do this if you're confident no other process is "
            f"actively working on it right now.\n\nProceed?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        try:
            self._cursor.execute(
                """
                UPDATE Integration.BOM_Staging_Header
                SET Status = 'ERROR',
                    RejectReason = 'Manually unlocked from JL Check'
                WHERE QuoteNumber = ? AND Status = 'PENDING'
                """,
                self._quote_number,
            )
            self._cursor.connection.commit()
        except Exception as exc:
            self._cursor.connection.rollback()
            QMessageBox.critical(
                self, "Unlock failed",
                f"Nothing was changed — rolled back.\n\nError:\n{exc}",
            )
            return

        self._show_continue_state(
            "Unlocked. Click below to proceed with finalizing this BOM."
        )

    def _show_continue_state(self, message: str) -> None:
        # Replace whatever action button was showing with the single,
        # distinct "go ahead" step — never the same click as the
        # reset action itself.
        self.message_label.setText(message)
        self.action_btn.setText("Continue with Finalize")
        self.action_btn.clicked.disconnect()
        self.action_btn.clicked.connect(self._on_continue_clicked)

    def _on_continue_clicked(self) -> None:
        self.proceed = True
        self.accept()