"""
Dialog for adding a custom line item — opened from ResolveDialog as a
last resort when a "Needs Attention" row has no real fix (nothing in
the material library or raw stock matches, and no candidate is right).

Mirrors how this is done manually in JobBOSS today: type an ID into the
quote's material field, it shows no match, tab to Description and
Extended Description and type those by hand too. A custom line never
touches jobboss_lookup — no PartNumber to match, no Material field to
parse, no candidates.

Only ID / Description / Extended Description are captured here.
Quantity isn't part of this dialog at all — the row keeps whatever
Quantity it already had from the Inventor BOM, same as every other line,
and flows into _combine_rows' quantity totals the same way. Length
likewise isn't asked for: MatchStatus "custom_line" always resolves to
the "Custom" TravelerState regardless of Category/CutLengthIn (see
traveler_state.py), so a custom line is never held up waiting on one.
"""

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QTextEdit, QVBoxLayout,
)


class CustomLineDialog(QDialog):
    """Modal dialog that produces a custom-line dict, or None if
    cancelled. Call exec(), then read result().

    id_prefill/description_prefill let the caller start from the
    Inventor BOM row's own PartNumber/Description (the usual case: this
    opens from ResolveDialog as a last resort when nothing else matched,
    so the engineer's own part data is the natural starting point).
    ext_description_prefill starts from the row's raw Material string
    (e.g. "SS SH 12GA X 48 X 120 T316L #4") — the closest existing
    equivalent to a real extended description, since there's no matched
    JobBOSS record to pull one from. All three remain freely editable."""

    def __init__(self, parent=None, id_prefill: str = "", description_prefill: str = "",
                 ext_description_prefill: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Add Custom Line")
        self.resize(480, 320)

        self._result: dict | None = None
        self._build_ui(id_prefill, description_prefill, ext_description_prefill)

    def _build_ui(self, id_prefill: str, description_prefill: str,
                  ext_description_prefill: str) -> None:
        layout = QVBoxLayout(self)

        note = QLabel(
            "Adds a line new to this quote only — not matched against "
            "the JobBOSS material library or raw stock."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        # ID and Description sit side by side — short, single-line
        # fields, same visual weight, no reason to stack them vertically
        # and waste space above the (much taller) extended description.
        inline_row = QHBoxLayout()
        layout.addLayout(inline_row)

        id_column = QVBoxLayout()
        id_column.addWidget(QLabel("ID:"))
        self.id_edit = QLineEdit(id_prefill)
        self.id_edit.setPlaceholderText("Required")
        id_column.addWidget(self.id_edit)
        inline_row.addLayout(id_column, stretch=1)

        description_column = QVBoxLayout()
        description_column.addWidget(QLabel("Description:"))
        self.description_edit = QLineEdit(description_prefill)
        self.description_edit.setPlaceholderText("Required")
        description_column.addWidget(self.description_edit)
        inline_row.addLayout(description_column, stretch=2)

        # Extended description gets its own full-width paragraph box
        # below — unlike ID/Description, this one can genuinely run to
        # a few sentences (raw stock spec, vendor notes, etc.).
        layout.addWidget(QLabel("Extended Description:"))
        self.ext_description_edit = QTextEdit(ext_description_prefill)
        self.ext_description_edit.setPlaceholderText("Optional")
        self.ext_description_edit.setAcceptRichText(False)
        layout.addWidget(self.ext_description_edit, stretch=1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self) -> None:
        item_id = self.id_edit.text().strip()
        description = self.description_edit.text().strip()

        if not item_id or not description:
            QMessageBox.warning(self, "Missing fields",
                                 "ID and Description are both required.")
            (self.id_edit if not item_id else self.description_edit).setFocus()
            return

        self._result = {
            "Id": item_id,
            "Description": description,
            "ExtDescription": self.ext_description_edit.toPlainText().strip() or None,
        }
        self.accept()

    def result(self) -> dict | None:
        return self._result