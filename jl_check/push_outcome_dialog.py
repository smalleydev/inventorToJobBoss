"""
Shown once, right after finalize_and_export actually writes an export
file to the inbox — polls Integration.BOM_Staging_Header until the
watcher reaches a terminal state (IMPORTED/ERROR) or the poll limit is
hit, then shows the result.

Close only. Deliberately no Unlock or Retry buttons here: a lock
collision is caught and handled BEFORE a write is ever attempted (see
quote_locked_dialog.py's pre-check), so by the time this dialog exists,
a real write has genuinely just happened. If it failed for a real
reason (e.g. missing material setup), the fix is to resolve the
underlying issue and click Finalize again from the main window — not
to loop inside this dialog.

STALE-RESULT GUARD: the header row's Status/RejectReason/ProcessedAt
can already hold a value from a PREVIOUS attempt (e.g. a manual reset
done before the watcher had a chance to run) at the moment this dialog
opens. Reporting that immediately as "the result of what I just
submitted" is wrong and was a real bug — it caused a dialog to show a
stale failure and stop polling entirely, moments before the watcher
actually claimed and succeeded on the file this dialog was meant to be
tracking. The fix: snapshot ProcessedAt when polling starts, and only
treat IMPORTED/ERROR as a genuine result of THIS submission once
ProcessedAt actually changes from that snapshot — not merely because
the column already holds some value.
"""

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout

POLL_INTERVAL_MS = 750

# Give up waiting after this many polls — the watcher should claim and
# finish a small quote within a few seconds; past this, whatever's
# happening is worth a human looking at directly rather than the dialog
# spinning forever.
MAX_POLLS = 20


class PushOutcomeDialog(QDialog):
    def __init__(self, quote_number: str, cursor, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Push Status — {quote_number}")
        self.resize(480, 200)

        self._quote_number = quote_number
        self._cursor = cursor
        self._poll_count = 0
        self._baseline_processed_at = self._current_processed_at()

        self._build_ui()

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

        self._poll()  # check immediately, don't wait for the first tick

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        self.status_label = QLabel("Waiting for the watcher to pick this up...")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.detail_label = QLabel("")
        self.detail_label.setWordWrap(True)
        layout.addWidget(self.detail_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def _current_processed_at(self):
        """One-off lookup of the row's current ProcessedAt, used as the
        baseline to detect a genuinely NEW result later. Returns None
        if the row doesn't exist yet or has never been processed —
        either way, any real ProcessedAt value showing up later is new."""
        self._cursor.execute(
            "SELECT ProcessedAt FROM Integration.BOM_Staging_Header WHERE QuoteNumber = ?",
            self._quote_number,
        )
        row = self._cursor.fetchone()
        return row.ProcessedAt if row else None

    def _poll(self) -> None:
        self._poll_count += 1

        self._cursor.execute(
            """
            SELECT Status, RejectReason, QuoteGuid, ProcessedAt
            FROM Integration.BOM_Staging_Header
            WHERE QuoteNumber = ?
            """,
            self._quote_number,
        )
        row = self._cursor.fetchone()

        # A stale IMPORTED/ERROR from before this submission looks
        # identical to a fresh one unless we check whether ProcessedAt
        # has actually moved past the baseline captured at open time.
        is_fresh_result = (
            row is not None
            and row.ProcessedAt is not None
            and row.ProcessedAt != self._baseline_processed_at
        )

        if row is None:
            self.status_label.setText("Waiting for the watcher to pick this up...")
            self.detail_label.setText(
                "Make sure watcher_service.py is running." if self._poll_count > 4 else ""
            )
        elif row.Status == "PENDING":
            self.status_label.setText("In progress...")
            self.detail_label.setText("")
        elif row.Status == "IMPORTED" and is_fresh_result:
            self.status_label.setText("Success")
            self.detail_label.setText(
                f"Written to JobBOSS. GUID {row.QuoteGuid}, at {row.ProcessedAt}."
            )
            self._timer.stop()
        elif row.Status == "ERROR" and is_fresh_result:
            self.status_label.setText("Failed")
            self.detail_label.setText(row.RejectReason or "(no reason recorded)")
            self._timer.stop()
        else:
            # Status is IMPORTED/ERROR, but ProcessedAt hasn't moved —
            # this is leftover from before we wrote our own file. The
            # watcher hasn't actually claimed THIS submission yet.
            self.status_label.setText("Waiting for the watcher to claim this...")
            self.detail_label.setText(
                f"(Currently showing an older result from a previous "
                f"attempt: {row.Status}. Still waiting for a new one.)"
            )

        if self._poll_count >= MAX_POLLS and self._timer.isActive():
            self._timer.stop()
            self.status_label.setText("Still waiting")
            self.detail_label.setText(
                "Gave up waiting for a result — check the staging table "
                "or the watcher's own log directly."
            )