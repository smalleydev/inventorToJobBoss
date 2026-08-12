"""
Table model shared by both JL Check panels.

Each row is a plain dict matching the JSON produced by the Inventor
add-in (BomExtractor.ToJson) plus keys added during the walk:
MatchStatus, JobBossMaterial, Candidates, TravelerState, _orig_index.

Two instances exist with different construction flags:

  working_model  — editable=True. The right panel. Cells can be edited
                   in place (notably CutLengthIn), rows carry status
                   colors, and edits recompute TravelerState live.
  base_model     — mirror_layout=True. The left panel: read-only, cells
                   right-aligned, columns shown in reverse order so the
                   column nearest the splitter lines up with its
                   working-table counterpart.
"""

import math

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QColor, QDoubleValidator, QFont
from PySide6.QtWidgets import QLineEdit, QStyledItemDelegate
from traveler_state import compute_traveler_state

# Cut lengths always get rounded UP to the nearest 16th inch on entry —
# you never want to under-order material off a typed-in length. Applied
# in setData() below, the single choke point every CutLengthIn edit goes
# through regardless of entry path (delegate, programmatic, etc.).
LENGTH_INCREMENT_IN = 1 / 16  # 0.0625"

# Same float-noise concern as stock_nesting.py's FLOAT_TOLERANCE_IN — a
# value that's already an exact multiple of 1/16" (e.g. 3.125 arriving
# as 3.1249999999999996) shouldn't get bumped up to the next increment.
_ROUND_TOLERANCE = 1e-6


def round_up_to_sixteenth(value: float) -> float:
    """Round `value` UP to the nearest 1/16" (0.0625")."""
    steps = math.ceil(value / LENGTH_INCREMENT_IN - _ROUND_TOLERANCE)
    return round(steps * LENGTH_INCREMENT_IN, 4)


class LengthEditDelegate(QStyledItemDelegate):
    """Free-typing decimal entry for the CutLengthIn column.

    Qt's default item delegate picks its editor widget based on the
    Python type of the cell's current EditRole value — an int (e.g. the
    0 a row starts with before any length is entered) gets a plain
    QSpinBox with no decimal support at all, and even once a value has
    been saved as a float, the default QDoubleSpinBox editor only
    allows 2 decimal places and select-alls its text on focus. Both
    make it impossible to type something like "3.154" in one pass —
    hence the old type-3/click-out/click-back-in/type-154 workaround.

    This delegate always hands back a plain QLineEdit with a
    QDoubleValidator, so typing a length behaves like any normal text
    field no matter what the cell currently holds."""

    def createEditor(self, parent, option, index):
        editor = QLineEdit(parent)
        validator = QDoubleValidator(0.0, 999999.0, 4, editor)
        validator.setNotation(QDoubleValidator.StandardNotation)
        editor.setValidator(validator)
        return editor

    def setEditorData(self, editor, index):
        value = index.model().data(index, Qt.EditRole)
        editor.setText("" if value in (None, "", 0, 0.0) else str(value))
        editor.selectAll()

    def setModelData(self, editor, model, index):
        text = editor.text().strip()
        if text:
            model.setData(index, text, Qt.EditRole)


class BomTableModel(QAbstractTableModel):

    # Emitted only for a REAL data change (an inline edit via setData, or
    # a resolve-dialog resolution via update_row) — never for the walk's
    # cosmetic sweep highlight (set_walk_progress), which repaints but
    # changes no actual row data. MainWindow uses this specifically to
    # trigger session autosave at real checkpoints, without saving
    # hundreds of times during a single walk's highlight sweep.
    row_edited = Signal()

    # Display columns, in order: (row-dict key, header label).
    # NOTE: main.py's sorting and width-sync code indexes into this list —
    # if columns change, check MainWindow's header handling too.
    COLUMNS = [
        ("PartNumber", "Part Number"),
        ("Description", "Description"),
        ("Quantity", "Qty"),
        ("Material", "Material"),
        ("Category", "Category"),
        ("CutLengthIn", "Length (in)"),
        ("TravelerState", "Traveler State"),
    ]

    # Row colors keyed by TravelerState: (background, foreground).
    # "Clean" and "Attended" intentionally absent — they render with the
    # theme's default colors. Values are hardcoded RGB chosen to read
    # acceptably against both the light and dark palettes.
    STATUS_COLORS = {
        "Ignored": (QColor(240, 220, 130), QColor(20, 20, 20)),          # yellow
        "Needs Attention": (QColor(230, 130, 130), QColor(20, 20, 20)),  # red
        "Needs Length": (QColor(235, 155, 70), QColor(20, 20, 20)),      # orange
    }

    # Transient green highlight for the row currently being processed by
    # the walk sweep.
    WALK_HIGHLIGHT = (QColor(120, 200, 120), QColor(20, 20, 20))

    def __init__(self, rows: list[dict], editable: bool = False,
                 mirror_layout: bool = False, parent=None):
        super().__init__(parent)
        self._rows = rows
        self._editable = editable
        self._mirror_layout = mirror_layout
        self._walk_progress_row = -1  # -1 = no active walk indicator

        # Mirrored table shows COLUMNS in reverse order, so the column
        # nearest the splitter on each side lines up conceptually (e.g.
        # PartNumber ends up adjacent to PartNumber). This is a manual
        # index remap, NOT Qt's RightToLeft layout direction — RTL also
        # invokes Unicode bidi text reordering, which visibly corrupts
        # plain LTR content like "028-294-08-01-30-SS" into
        # "SS-028-294-08-01-30". Verified independently with the same
        # bidi algorithm Qt uses; this remap avoids invoking it at all.
        self._column_order = list(range(len(self.COLUMNS)))
        if mirror_layout:
            self._column_order.reverse()

    # --- Required QAbstractTableModel overrides ----------------------------

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self.COLUMNS)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None

        actual_col = self._column_order[index.column()]
        key, _ = self.COLUMNS[actual_col]
        row_data = self._rows[index.row()]

        if role in (Qt.DisplayRole, Qt.EditRole):
            # Manually-resolved rows display the CORRECTED JobBOSS match
            # instead of the original raw Inventor data — the engineer
            # picked a specific material via the resolve dialog, and the
            # table should reflect that choice at a glance:
            #   Part Number -> the resolved JobBOSS material number
            #   Description -> that material's real JobBOSS description
            #   Material    -> literal "---" (no longer meaningful once
            #                  resolved; the raw string lives on in
            #                  row_data["Material"] itself, untouched,
            #                  for revert purposes)
            # None of row_data's underlying keys are overwritten here —
            # this is display-only, so reverting a resolution is just a
            # matter of changing MatchStatus/JobBossMaterial back.
            is_resolved_manual = row_data.get("MatchStatus") in (
                "resolved_manual", "resolved_manual_raw_stock"
            ) and row_data.get("JobBossMaterial")

            if is_resolved_manual:
                if key == "PartNumber":
                    return row_data["JobBossMaterial"]
                if key == "Description":
                    return row_data.get("JobBossDescription") or row_data.get("Description", "")
                if key == "Material":
                    return "---"

            return row_data.get(key, "")
        
        if role == Qt.ToolTipRole:
            return str(row_data.get(key, ""))
        
        if role == Qt.FontRole and index.row() % 2 == 1:
            font = QFont()
            font.setBold(True)
            return font

        # Mirrored table right-aligns everything so values sit against
        # the splitter, directly facing their working-table counterparts.
        if role == Qt.TextAlignmentRole and self._mirror_layout:
            return Qt.AlignRight | Qt.AlignVCenter

        # Walk sweep highlight takes precedence over status colors.
        if index.row() == self._walk_progress_row:
            if role == Qt.BackgroundRole:
                return self.WALK_HIGHLIGHT[0]
            if role == Qt.ForegroundRole:
                return self.WALK_HIGHLIGHT[1]

        colors = self.STATUS_COLORS.get(row_data.get("TravelerState"))
        if colors:
            if role == Qt.BackgroundRole:
                return colors[0]
            if role == Qt.ForegroundRole:
                return colors[1]

        return None

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            actual_col = self._column_order[section]
            return self.COLUMNS[actual_col][1]
        # Vertical headers are hidden in the views; return the row number
        # anyway for correctness if a view ever shows them.
        return section + 1

    def flags(self, index: QModelIndex):
        base_flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if self._editable:
            return base_flags | Qt.ItemIsEditable
        return base_flags

    def setData(self, index: QModelIndex, value, role: int = Qt.EditRole) -> bool:
        if not self._editable or role != Qt.EditRole:
            return False

        actual_col = self._column_order[index.column()]
        key, _ = self.COLUMNS[actual_col]
        row_data = self._rows[index.row()]

        # Length edits must be numeric; anything else is rejected and the
        # cell keeps its previous value. Whatever's typed is always
        # rounded UP to the nearest 1/16" — never leave a length that
        # would under-order material.
        if key == "CutLengthIn":
            try:
                value = float(value)
            except (TypeError, ValueError):
                return False
            value = round_up_to_sixteenth(value)

        row_data[key] = value

        # Any edit can change the row's state (e.g. entering a length
        # clears "Needs Length") — recompute rather than patching.
        row_data["TravelerState"] = compute_traveler_state(row_data)

        # Emit for the whole row: a state change also changes row color,
        # not just the edited cell.
        self._emit_row_changed(index.row())
        self.row_edited.emit()
        return True

    # --- Helpers for our own code (not part of the Qt interface) -----------

    def _emit_row_changed(self, row: int) -> None:
        """Signal the view that every cell in `row` may have changed."""
        top_left = self.index(row, 0)
        bottom_right = self.index(row, self.columnCount() - 1)
        self.dataChanged.emit(top_left, bottom_right)

    def get_row(self, row: int) -> dict:
        """Direct access to a row's backing dict (shared, not a copy)."""
        return self._rows[row]

    def replace_rows(self, rows: list[dict]) -> None:
        """Swap in an entirely new dataset (e.g. after loading a file)."""
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def append_row(self, row: dict) -> None:
        """Add a single new row and notify the view."""
        insert_at = len(self._rows)
        self.beginInsertRows(QModelIndex(), insert_at, insert_at)
        self._rows.append(row)
        self.endInsertRows()

    def update_row(self, row: int, updates: dict) -> None:
        """Merge `updates` into an existing row's dict and repaint it."""
        self._rows[row].update(updates)
        self._emit_row_changed(row)
        self.row_edited.emit()

    def set_manual_state(self, row: int, state: str | None) -> None:
        """Force TravelerState to `state` (from traveler_state.ALL_STATES),
        overriding whatever compute_traveler_state would otherwise derive
        — used by the working table's right-click "Set State" menu. Pass
        None to clear the override and go back to normal derivation.

        Sets ManualTravelerState on the row (compute_traveler_state checks
        it first) and recomputes TravelerState through the same function
        everything else uses, so this stays consistent with every other
        code path instead of hand-setting the display value directly."""
        row_data = self._rows[row]
        if state is None:
            row_data.pop("ManualTravelerState", None)
        else:
            row_data["ManualTravelerState"] = state
        row_data["TravelerState"] = compute_traveler_state(row_data)
        self._emit_row_changed(row)
        self.row_edited.emit()

    def sort_by(self, key_func, reverse: bool = False) -> None:
        """Sort rows in place by an arbitrary key function. This reorders
        the actual data (full model reset), not just a view — there is no
        proxy layer in this app, so this is the one true order."""
        self.beginResetModel()
        self._rows.sort(key=key_func, reverse=reverse)
        self.endResetModel()

    def reorder_by_original_index(self, order: list) -> None:
        """Reorder rows to follow a sequence of _orig_index values — used
        to make this model mirror another model's current order so both
        panels stay row-for-row aligned regardless of sorting or
        substitutions."""
        index_map = {r["_orig_index"]: r for r in self._rows}
        self.beginResetModel()
        self._rows = [index_map[i] for i in order if i in index_map]
        self.endResetModel()

    def set_walk_progress(self, row: int) -> None:
        """Move the green walk-sweep highlight to `row`, clearing it from
        the previous position. Pass -1 to clear entirely."""
        old_row = self._walk_progress_row
        self._walk_progress_row = row

        if old_row >= 0:
            self._emit_row_changed(old_row)
        if row >= 0:
            self._emit_row_changed(row)

    def view_column_for_key(self, key: str) -> int:
        """Return the view column index currently displaying `key`,
        accounting for this instance's column order (reversed for the
        mirrored/base table, normal otherwise)."""
        target = next(i for i, (k, _) in enumerate(self.COLUMNS) if k == key)
        return self._column_order.index(target)