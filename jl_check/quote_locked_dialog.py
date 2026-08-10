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
    revision came in) and this quote needs to be replaced. For that,
    "Unlock and Rebuild" DELETES the old JobBOSS quote (the Quote,
    Quote_Qty, Quote_Req, and Quote_Req_Qty rows tied to that specific
    GUID — not the RFQ row, which other quotes may share) before
    resetting the lock, so the rebuild REPLACES the old quote instead
    of leaving it sitting alongside a new one. This is a real delete of
    JobBOSS data, so it requires an explicit, serious confirmation —
    distinct from the lighter PENDING confirmation below.

PENDING — likely a stale claim left behind by a crashed run. "Unlock"
here just resets the staging lock (no JobBOSS data exists yet for a
PENDING row, so there's nothing to delete).

Both paths end the same way: a second, distinct button
("Continue with Finalize") is the explicit decision to proceed with
writing — deliberately never the same click as the reset/delete
action itself. Either step can be abandoned via Close with no side
effects beyond whatever was already confirmed and applied.
"""

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QMessageBox, QPushButton, QVBoxLayout,
)


def _delete_jobboss_quote(cursor, quote_guid: str) -> None:
    """Deletes one Quote record and everything hanging off it — the
    same cleanup this project has run by hand via scratch scripts many
    times over, now attached to the Unlock-and-Rebuild action itself.
    Does NOT touch the RFQ row (other quotes may legitimately share it)
    or the Integration staging tables (handled separately by the caller)."""
    cursor.execute("DELETE FROM Quote_Req_Qty WHERE Quote = ?", quote_guid)
    cursor.execute("DELETE FROM Quote_Req WHERE Quote = ?", quote_guid)
    cursor.execute("DELETE FROM Quote_Qty WHERE Quote = ?", quote_guid)
    cursor.execute(
        "DELETE FROM Bill_Of_Quotes WHERE Parent_Quote = ? OR Component_Quote = ?",
        quote_guid, quote_guid,
    )
    cursor.execute("DELETE FROM Quote WHERE Quote = ?", quote_guid)
    cursor.connection.commit()


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
                f"unlocking will DELETE the existing JobBOSS quote and let "
                f"you push a fresh one. If you just clicked Finalize again "
                f"by accident, close this instead — nothing has changed."
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
            f"This will PERMANENTLY DELETE the existing JobBOSS quote for "
            f"'{self._quote_number}' (GUID {self._status_row.QuoteGuid}) — "
            f"the quote itself and all its material lines.\n\n"
            f"This cannot be undone. Only proceed if this quote genuinely "
            f"needs to be rebuilt.\n\nDelete the old quote and continue?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        _delete_jobboss_quote(self._cursor, self._status_row.QuoteGuid)

        self._cursor.execute(
            """
            UPDATE Integration.BOM_Staging_Header
            SET Status = 'ERROR',
                RejectReason = 'Unlocked and rebuilt from JL Check — old quote deleted'
            WHERE QuoteNumber = ? AND Status = 'IMPORTED'
            """,
            self._quote_number,
        )
        self._cursor.connection.commit()

        self._show_continue_state(
            "Old quote deleted. Click below to push a fresh one."
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

        self._show_continue_state(
            "Unlocked. Click below to proceed with finalizing this BOM."
        )

    def _show_continue_state(self, message: str) -> None:
        # Replace whatever action button was showing with the single,
        # distinct "go ahead" step — never the same click as the
        # reset/delete action itself.
        self.message_label.setText(message)
        self.action_btn.setText("Continue with Finalize")
        self.action_btn.clicked.disconnect()
        self.action_btn.clicked.connect(self._on_continue_clicked)

    def _on_continue_clicked(self) -> None:
        self.proceed = True
        self.accept()