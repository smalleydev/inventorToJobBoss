"""
JL Check — JobBOSS material review tool.

Loads a BOM JSON exported by the Inventor add-in (BomPushAddIn), walks
every line against JobBOSS's Material table, and presents the results in
a two-panel mirror layout:

  Base (left)     — the original BOM, untouched. Read-only, columns
                    right-aligned with the row-number column against the
                    splitter, so each value faces its counterpart.
  Working (right) — the resolved BOM. Color-coded by Traveler State
                    (see traveler_state.py); red rows open a resolution
                    dialog on double-click; lengths are edited in place.

The two panels stay row-for-row aligned at all times: sorting the working
table (header click, or the automatic post-walk Traveler State sort)
reorders the base table identically, keyed by each row's _orig_index —
not by position — so substitutions never break the cross-reference.

Launch: `python main.py [path\\to\\bom.json]`. The optional path is how
the Inventor add-in opens this tool with the BOM pre-loaded; without it,
use File > Import.
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QHeaderView, QInputDialog, QLabel, QMainWindow,
    QMenu, QMessageBox, QProgressBar, QPushButton, QSplitter, QTableView,
    QVBoxLayout, QWidget,
)

from bom_model import BomTableModel, LengthEditDelegate
from db import get_connection
from jobboss_lookup import classify_secondary_category, lookup_material
from resolve_dialog import ResolveDialog
from quote_locked_dialog import QuoteLockedDialog
from push_outcome_dialog import PushOutcomeDialog
from settings import get_theme, set_theme
from stock_nesting import expand_pieces, nest_pieces
from theme import apply_theme
from traveler_state import SORT_ORDER as TRAVELER_STATE_SORT_ORDER
from traveler_state import compute_traveler_state

# Lookup statuses that land in the working table already resolved, with
# no human interaction required before (at most) a length entry.
AUTO_RESOLVE_STATUSES = frozenset({
    "exact_part", "exact_vendor", "jb_reference", "raw_stock_match",
})

# Statuses that open the resolve dialog on double-click.
NEEDS_RESOLUTION_STATUSES = frozenset({
    "ambiguous", "needs_review_uhmw", "needs_review_lexan", "not_found",
})

# Categories whose length-bearing rows go through stock nesting at
# Finalize time instead of simple material+length grouping. Must match
# traveler_state.py's LENGTH_REQUIRED_CATEGORIES.
NESTED_CATEGORIES = frozenset({"TUBE", "ANGLE", "BAR", "ROUND BAR"})

# Fields included in the finalized export payload — everything else on a
# working row is internal bookkeeping (e.g. _orig_index, Children) or an
# always-null placeholder reserved for future use (MakeBuy, StockNumber,
# Mass, etc.) that the push service has no use for yet.
EXPORT_FIELDS = (
    "PartNumber", "Description", "ExtDescription", "Quantity", "Material",
    "Category", "CutLengthIn", "JobBossMaterial", "MatchStatus",
    "TravelerState", "ConflictNotes", "SourcePartNumbers", "CutList",
    "TotalStockLengthIn", "MaterialUsedIn",
)
# MatchStatus == "custom_line" is how the push service should recognize
# a row to write as a JobBOSS Misc line rather than a material-linked
# one. ExtDescription only ever carries a real value on those rows —
# everything else leaves it unset.
#
# Author is deliberately NOT in this per-row list — every row on a BOM
# carries the same value (the Inventor add-in stamps the top-level
# assembly's Author iProperty onto every line, see BomExtractor), so
# it's pulled once and sent as the payload's top-level QuotedBy instead
# (see finalize_and_export) rather than repeated on every exported row.


# Every length JL Check works with internally (CutLengthIn, piece
# lengths for nesting) is in inches. Material.IS_Length, though, is
# stored in whatever unit that material is actually stocked in
# (Stocked_UofM — "ft", "in", "ea", "pack") — a bar stocked by the foot
# has its stick length recorded in feet, not inches. Convert on the way
# in rather than assuming inches, or nesting silently compares inch
# piece lengths against a foot-denominated stick and over/under-counts
# sticks needed by 12x.
INCHES_PER_FOOT = 12


def _stick_length_to_inches(value: float, uofm: str | None) -> float:
    """Convert a JobBOSS length value to inches based on the material's
    Stocked_UofM. Only "ft" needs conversion — "in" (and anything else,
    which shouldn't reach here; see LENGTH_REQUIRED_UOFM in
    traveler_state.py) is already inches."""
    if (uofm or "").strip().upper() == "FT":
        return value * INCHES_PER_FOOT
    return value


def _get_stick_length(cursor, jobboss_material: str, uofm: str | None) -> float:
    """Look up the standard stick/bar length for a material from
    Material.IS_Length — confirmed against the JobBOSS calculator's own
    'Bar Length' field for several real materials — and convert it to
    inches per `uofm` (the material's own Stocked_UofM, as already
    resolved onto the row / see traveler_state.JobBossUofM) so it can be
    compared against inch-denominated piece lengths."""
    cursor.execute(
        "SELECT IS_Length FROM Material WHERE Material = ?", jobboss_material
    )
    row = cursor.fetchone()
    if row is None or row.IS_Length in (None, 0, 0.0):
        raise ValueError(
            f"Material '{jobboss_material}' has no IS_Length (stick length) "
            f"set in JobBOSS — cannot nest cuts for this material."
        )
    return _stick_length_to_inches(float(row.IS_Length), uofm)


def _combine_rows(rows: list[dict], cursor) -> list[dict]:
    """
    Group approved rows for the Finalize export.

    Length-bearing linear-stock rows (NESTED_CATEGORIES) are grouped by
    JobBossMaterial ONLY (not length) and run through stock nesting —
    every individual piece across every length of that material is
    packed against the material's real stick length, and the result is
    written as ONE combined line: total stock length needed (in the
    material's stocked unit), matching how the shop actually orders
    material (nest into standard sticks, order the total).

    Everything else (hardware, vendor items, sheet/plate that somehow
    reached here) keeps the old behavior: group by
    (JobBossMaterial, CutLengthIn), summing quantity within each exact
    match.

    `cursor` is a live DB cursor, needed to look up each nested
    material's stick length (Material.IS_Length).
    """
    nestable_rows = [r for r in rows if r.get("Category") in NESTED_CATEGORIES
                     and r.get("CutLengthIn") not in (None, 0, 0.0)]
    nestable_ids = {id(r) for r in nestable_rows}
    simple_rows = [r for r in rows if id(r) not in nestable_ids]

    combined = []

    # --- Simple combine: group by (material, exact length) --------------
    groups: dict[tuple, list[dict]] = {}
    for row in simple_rows:
        key_material = row.get("JobBossMaterial") or row.get("PartNumber")
        length = row.get("CutLengthIn")
        key_length = length if length not in (None, 0, 0.0) else None
        key = (key_material, key_length)
        groups.setdefault(key, []).append(row)

    for (material_key, length_key), group in groups.items():
        if len(group) == 1:
            row = dict(group[0])
            row["SourcePartNumbers"] = row.get("PartNumber")
            row["CutList"] = None
            combined.append(row)
            continue

        total_qty = sum(r.get("Quantity", 0) or 0 for r in group)
        part_numbers = [r.get("PartNumber", "") for r in group]
        first = group[0]

        combined.append({
            "PartNumber": material_key,
            "Description": f"Combined ({len(group)} items) — {first.get('Description', '')}",
            "Quantity": total_qty,
            "Material": first.get("Material", ""),
            "Category": first.get("Category", ""),
            "CutLengthIn": length_key,
            "JobBossMaterial": material_key,
            "MatchStatus": first.get("MatchStatus"),
            "TravelerState": first.get("TravelerState"),
            "ConflictNotes": None,
            "SourcePartNumbers": ", ".join(part_numbers),
            "CutList": None,
        })

    # --- Nested combine: one line per material, total stock needed -----
    nest_groups: dict[str, list[dict]] = {}
    for row in nestable_rows:
        key_material = row.get("JobBossMaterial") or row.get("PartNumber")
        nest_groups.setdefault(key_material, []).append(row)

    for material_key, group in nest_groups.items():
        pieces = expand_pieces(group)
        # Use the UofM already resolved onto these rows (walk or resolve
        # dialog) rather than re-querying it — same value the Needs
        # Length gate in traveler_state.py used to get here in the first
        # place, so the two stay consistent.
        uofm = group[0].get("JobBossUofM")
        stick_length = _get_stick_length(cursor, material_key, uofm)
        result = nest_pieces(pieces, stick_length_in=stick_length)

        part_numbers = [r.get("PartNumber", "") for r in group]
        first = group[0]
        cut_list_note = ", ".join(
            f"{r.get('Quantity')}x @ {r.get('CutLengthIn')}\"" for r in group
        )

        combined.append({
            "PartNumber": material_key,
            "Description": f"Nested ({result.total_piece_count} cuts, "
                            f"{result.sticks_needed} sticks) — {first.get('Description', '')}",
            "Quantity": result.sticks_needed,
            "Material": first.get("Material", ""),
            "Category": first.get("Category", ""),
            # No single CutLengthIn applies to a nested combined line —
            # the quantity IS the answer (total stock to order).
            "CutLengthIn": None,
            "JobBossMaterial": material_key,
            "MatchStatus": first.get("MatchStatus"),
            "TravelerState": first.get("TravelerState"),
            "ConflictNotes": None,
            "SourcePartNumbers": ", ".join(part_numbers),
            "CutList": cut_list_note,
            # Total stock length in inches, for quote_writer to convert
            # into the material's stocked UofM (e.g. ft).
            "TotalStockLengthIn": result.total_stock_length_in,
            # True material consumed (sum of piece + kerf), for the
            # Part_Length field — distinct from the rounded-up-to-
            # whole-sticks order quantity above.
            "MaterialUsedIn": result.total_material_used_in,
        })

    return combined


def _to_export_row(row: dict) -> dict:
    """Trim a working row down to just the fields the push service needs."""
    return {key: row.get(key) for key in EXPORT_FIELDS}


class MainWindow(QMainWindow):

    # Directory the push service watches for approved exports — the
    # "Incoming" sibling folder under IT's provisioned share, per the
    # UNC path they explicitly asked us to use over the F: mapped drive.
    # Must stay in sync with watcher_service.py's INBOX_PATH.
    INBOX_PATH = Path(r"\\SYS\sys\BOMIntegration") / "Incoming"

    # Where in-progress session state is saved, keyed by source filename
    # — a separate concept from the JobBOSS inbox above, purely local
    # scratch storage so a BOM can be picked back up later on this same
    # workstation.
    # Where in-progress session state is saved, keyed by source filename
    # — a separate concept from the JobBOSS inbox above, purely local
    # scratch storage so a BOM can be picked back up later.
    #
    # %LOCALAPPDATA% (C:\Users\<username>\AppData\Local) rather than
    # Program Files or a shared C:\TEMP path: every user can write here
    # without admin elevation, and each engineer's sessions stay
    # isolated from everyone else's automatically. Falls back to
    # C:\TEMP if the environment variable is somehow unset (shouldn't
    # happen on a real Windows install, but avoids a hard crash if it
    # ever is).
    SESSIONS_PATH = Path(os.environ.get("LOCALAPPDATA", r"C:\TEMP")) / "JL Check" / "sessions"

    def __init__(self):
        super().__init__()
        self.setWindowTitle("JL Check")
        self.resize(1400, 800)

        # mirror_layout gives the base model right-aligned cells plus the
        # synthetic row-number column on its far right (see bom_model.py).
        self.base_model = BomTableModel(rows=[], editable=False, mirror_layout=True)
        self.working_model = BomTableModel(rows=[], editable=True)

        # One connection for the lifetime of the window — shared by the
        # walk and every resolve dialog. Failing here (server down, wrong
        # environment) fails at startup, before any file is loaded.
        self.conn = get_connection()
        self.cursor = self.conn.cursor()

        # Manual sort state (no proxy models in this app — sorting
        # reorders the actual row lists so both panels can stay aligned).
        self._sort_column: str | None = None
        self._sort_ascending = True

        # Reentrancy guards: setValue()/setColumnWidth() fire the same
        # signals they're responding to, which would otherwise recurse
        # between the two tables.
        self._syncing_scroll = False
        self._loaded_file_path: str | None = None

        self._build_ui()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.conn.close()
        super().closeEvent(event)

    # --- UI construction ----------------------------------------------------

    def _build_ui(self) -> None:
        self._build_menu()

        central = QWidget()
        self.setCentralWidget(central)
        outer_layout = QVBoxLayout(central)

        finalize_btn = QPushButton("Finalize && Push to JobBOSS")
        finalize_btn.clicked.connect(self.finalize_and_export)
        outer_layout.addWidget(finalize_btn)

        # Splitter keeps the divider draggable and defaults to a centered
        # 50/50 split between the two panels.
        splitter = QSplitter(Qt.Horizontal)
        outer_layout.addWidget(splitter)

        base_layout, self.base_view, self.base_progress = self._make_panel(
            "Base (original)", self.base_model
        )
        splitter.addWidget(self._wrap_layout(base_layout))

        working_layout, self.working_view, _ = self._make_panel(
            "Working (approved)", self.working_model
        )
        splitter.addWidget(self._wrap_layout(working_layout))

        self.base_view.verticalScrollBar().valueChanged.connect(
            self._on_base_scrolled
        )
        self.working_view.verticalScrollBar().valueChanged.connect(
            self._on_working_scrolled
        )

        self._lock_content_fit_columns(self.base_view, self.base_model)
        self._lock_content_fit_columns(self.working_view, self.working_model)

        # Free-typing decimal entry for the length column — see
        # LengthEditDelegate for why the default delegate can't handle
        # something like "3.154" in one pass.
        self._length_delegate = LengthEditDelegate(self.working_view)
        self.working_view.setItemDelegateForColumn(
            self.working_model.view_column_for_key("CutLengthIn"),
            self._length_delegate,
        )

        splitter.setSizes([1, 1])

        self.working_view.doubleClicked.connect(self.on_working_row_double_clicked)
        self.working_view.horizontalHeader().sectionClicked.connect(
            self.on_working_header_clicked
        )
        self.working_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.working_view.customContextMenuRequested.connect(
            self.on_working_context_menu
        )

        # Checkpoint autosave: fires only on a REAL change (an inline
        # edit, or a resolve-dialog resolution) — never on the walk's
        # cosmetic sweep highlight. See bom_model.row_edited.
        self.working_model.row_edited.connect(self._save_session)

    @staticmethod
    def _wrap_layout(layout: QVBoxLayout) -> QWidget:
        """QSplitter takes widgets, not layouts — wrap a layout so it can
        be added as a splitter pane."""
        widget = QWidget()
        widget.setLayout(layout)
        return widget

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("File")

        import_action = QAction("Import BOM JSON...", self)
        import_action.triggered.connect(self.import_json)
        file_menu.addAction(import_action)

        view_menu = self.menuBar().addMenu("View")

        # Exclusive group makes Light/Dark behave like radio buttons.
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)

        current_theme = get_theme()
        for theme_name in ("Light", "Dark"):
            action = QAction(theme_name, self, checkable=True)
            action.setChecked(current_theme == theme_name)
            action.triggered.connect(
                lambda _checked=False, name=theme_name: self._set_theme(name)
            )
            theme_group.addAction(action)
            view_menu.addAction(action)

    def _set_theme(self, theme: str) -> None:
        set_theme(theme)
        apply_theme(QApplication.instance(), theme)

    def _make_panel(self, title: str, model: BomTableModel):
        layout = QVBoxLayout()
        layout.addWidget(QLabel(title))

        view = QTableView()
        view.setModel(model)
        view.verticalHeader().setVisible(False)

        header = view.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        header.setMinimumSectionSize(50)

        layout.addWidget(view)

        progress = QProgressBar()
        progress.setTextVisible(True)
        progress.setVisible(False)
        layout.addWidget(progress)

        return layout, view, progress

    def _on_base_scrolled(self, value: int) -> None:
        if self._syncing_scroll:
            return
        self._syncing_scroll = True
        self.working_view.verticalScrollBar().setValue(value)
        self._syncing_scroll = False

    def _on_working_scrolled(self, value: int) -> None:
        if self._syncing_scroll:
            return
        self._syncing_scroll = True
        self.base_view.verticalScrollBar().setValue(value)
        self._syncing_scroll = False

    # --- File loading -------------------------------------------------------

    def import_json(self) -> None:
        """File > Import handler."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Import BOM JSON", "", "JSON Files (*.json)"
        )
        if path:
            self.load_file(path)

    def load_file(self, path: str) -> None:
        """Load a BOM JSON. If a saved session exists for this same
        source filename, asks the engineer right here, at open time,
        which way to go — same moment the old "Resume previous
        session?" prompt used to appear, just with a smarter second
        option than plain resume-verbatim-or-discard:

          Continue Session — merge the freshly re-exported JSON against
            the saved session: any part whose Description or Material
            changed in Inventor since last time is re-walked against
            JobBOSS from scratch; every part that didn't change keeps
            EXACTLY the state (TravelerState, JobBossMaterial,
            CutLengthIn, any manual resolution) it had when this BOM
            was last closed. See _resume_with_merge.

          Start Fresh Session — ignore the saved session entirely and
            walk every row against JobBOSS from scratch, discarding all
            prior resolutions/lengths/states, same as opening this BOM
            for the very first time. The saved session file itself is
            then overwritten with the fresh result (start_walk's own
            checkpoint save).

        No saved session exists -> no prompt, straight to a fresh walk,
        same as always.

        Shared by File > Import and the startup auto-load (Inventor
        add-in launch). Fully replaces both tables. Each row gets a
        stable _orig_index before display so the two panels can always
        be reordered to line up with each other, regardless of sorting
        or substitutions made later.
        """
        previous_file_path = self._loaded_file_path
        self._loaded_file_path = path

        with open(path, "r", encoding="utf-8") as f:
            rows = json.load(f)

        for i, row in enumerate(rows):
            row["_orig_index"] = i

        session = self._load_saved_session(path)
        if session is not None:
            box = QMessageBox(self)
            box.setWindowTitle("Previous session found")
            box.setText(
                f"A previous JL Check session was saved for "
                f"'{Path(path).name}' (last saved {session.get('SavedAt', 'unknown time')}).\n\n"
                f"Continue it — recompute only the parts that changed in "
                f"Inventor since then, keep everything else exactly as you "
                f"left it — or start over from scratch?"
            )
            continue_btn = box.addButton("Continue Session", QMessageBox.AcceptRole)
            fresh_btn = box.addButton("Start Fresh Session", QMessageBox.DestructiveRole)
            box.addButton(QMessageBox.Cancel)
            box.setDefaultButton(continue_btn)
            box.exec()

            clicked = box.clickedButton()
            if clicked == continue_btn:
                self._resume_with_merge(rows, session)
                return
            elif clicked == fresh_btn:
                pass  # fall through to the fresh walk below
            else:
                # Cancel — leave whatever was previously loaded alone,
                # including which file future saves/checkpoints target.
                self._loaded_file_path = previous_file_path
                return

        self.base_model.replace_rows(rows)
        self.working_model.replace_rows([])
        self.start_walk()

    def _session_path(self) -> Path | None:
        """Session file for the currently loaded BOM, keyed by its
        source filename stem — independent of the JobBOSS quote number
        (this is purely a local resume mechanism, unrelated to what
        gets submitted)."""
        if not self._loaded_file_path:
            return None
        stem = Path(self._loaded_file_path).stem
        return self.SESSIONS_PATH / f"{stem}.session.json"

    def _save_session(self) -> None:
        """Checkpoint save — captures both tables' full row state so a
        later run of JL Check on the same source file can offer to
        resume exactly where this one left off. Called only at real
        checkpoints (a resolve, an inline edit, a successful finalize)
        via bom_model.row_edited and the explicit call in
        finalize_and_export — never continuously."""
        session_path = self._session_path()
        if session_path is None:
            return

        self.SESSIONS_PATH.mkdir(parents=True, exist_ok=True)

        payload = {
            "SourceFile": self._loaded_file_path,
            "SavedAt": datetime.now().isoformat(timespec="seconds"),
            "BaseRows": self.base_model._rows,
            "WorkingRows": self.working_model._rows,
        }

        with open(session_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    def _load_saved_session(self, path: str) -> dict | None:
        """Read a previously saved session for `path`'s source filename,
        if one exists. Returns the raw session dict (BaseRows/
        WorkingRows/SavedAt) or None if no session was ever saved for
        this file — the caller then does a normal fresh walk. No
        prompt: merging (_resume_with_merge) is always the right thing
        to do when a session exists, so there's no longer a decision to
        ask the user for. To force a full re-walk from scratch anyway,
        delete the .session.json file under SESSIONS_PATH by hand."""
        stem = Path(path).stem
        session_path = self.SESSIONS_PATH / f"{stem}.session.json"

        if not session_path.exists():
            return None

        with open(session_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _resume_with_merge(self, new_rows: list[dict], session: dict) -> None:
        """Reconcile a freshly re-exported Inventor BOM against a
        previously saved session for the same file.

        The whole point: an engineer can close JL Check mid-review, go
        back into Inventor and correct a part's Description or
        Material, re-run the add-in, and come back to find exactly that
        part re-walked against JobBOSS — while every part that didn't
        change keeps precisely the state it was left in (TravelerState,
        JobBossMaterial, CutLengthIn, any manual resolution, all of it).

        A part counts as "changed" when its Description or Material
        differs from what the LAST saved BASE row had for that same
        PartNumber (base rows are the untouched Inventor snapshot, so
        this is a true diff against what Inventor actually said last
        time — not against the working row, whose Description a custom
        line may have deliberately overwritten). Quantity is refreshed
        from the new export either way (it doesn't drive matching, so
        there's no reason to recompute anything over it, but the
        working table shouldn't show a stale count either). A part
        missing from the new export is dropped; a part new to it is
        walked fresh like any other row.

        Matched by PartNumber — the identifier that survives a BOM
        re-export even when Description/Material/row order change. A
        PartNumber repeated more than once in one BOM is paired up in
        the order both files list it, which is the best that can be
        done without a better identity to key on; in practice an
        Inventor BOM export has one row per distinct part number.
        """
        saved_base_by_part: dict[str, list[dict]] = {}
        for row in session.get("BaseRows") or []:
            saved_base_by_part.setdefault(row.get("PartNumber"), []).append(row)

        saved_working_by_part: dict[str, list[dict]] = {}
        for row in session.get("WorkingRows") or []:
            saved_working_by_part.setdefault(row.get("PartNumber"), []).append(row)

        merged_working: list[dict] = []
        changed_count = 0
        new_count = 0

        for new_row in new_rows:
            part_number = new_row.get("PartNumber")
            old_base_queue = saved_base_by_part.get(part_number) or []
            old_working_queue = saved_working_by_part.get(part_number) or []

            old_base = old_base_queue.pop(0) if old_base_queue else None
            old_working = old_working_queue.pop(0) if old_working_queue else None

            unchanged = (
                old_base is not None
                and old_working is not None
                and old_base.get("Description") == new_row.get("Description")
                and old_base.get("Material") == new_row.get("Material")
            )

            if unchanged:
                # Carry the old working row forward exactly as it was —
                # resolution, TravelerState, CutLengthIn, everything —
                # only re-keying _orig_index to the new BOM's position
                # (so sorting and the base<->working mirror stay
                # aligned) and refreshing Quantity (doesn't affect
                # matching, but a stale count would be a visible bug).
                carried = dict(old_working)
                carried["_orig_index"] = new_row["_orig_index"]
                carried["Quantity"] = new_row.get("Quantity", carried.get("Quantity"))
                merged_working.append(carried)
            else:
                if old_base is None:
                    new_count += 1
                else:
                    changed_count += 1
                merged_working.append(self._resolve_row(new_row))

        # Whatever's left in saved_base_by_part never got popped above —
        # those PartNumbers are no longer in the new export at all.
        removed_count = sum(len(rows) for rows in saved_base_by_part.values())

        self.base_model.replace_rows(new_rows)
        self.working_model.replace_rows(merged_working)
        self._lock_content_fit_columns(self.base_view, self.base_model)
        self._lock_content_fit_columns(self.working_view, self.working_model)

        # Same post-walk sort as a fresh load — a changed/new row can
        # land anywhere in TravelerState (including back in Needs
        # Attention), so it needs to resurface at the top like normal.
        self._sort_column = "TravelerState"
        self._sort_ascending = True
        self._apply_sort("TravelerState")
        self._show_sort_indicator("TravelerState", ascending=True)

        self._save_session()

        if changed_count or new_count or removed_count:
            QMessageBox.information(
                self, "BOM re-synced from Inventor",
                f"Resumed the previous session for this BOM and compared it "
                f"against what Inventor just re-exported.\n\n"
                f"{changed_count} part(s) changed (Description or Material) "
                f"and were re-walked against JobBOSS.\n"
                f"{new_count} new part(s) were added and walked.\n"
                f"{removed_count} part(s) are no longer in the BOM and were "
                f"dropped.\n\n"
                f"Everything else kept exactly the state it had when this "
                f"BOM was last closed.",
            )

    # --- The walk -----------------------------------------------------------

    def _resolve_row(self, source: dict) -> dict:
        """Resolve one Inventor BOM row against JobBOSS: run the lookup,
        fill in any secondary category, and compute its TravelerState.
        Returns a new dict — `source` itself is never mutated, matching
        every other place a row gets resolved (start_walk's per-row
        loop used to inline this; _resume_with_merge needs the exact
        same logic for any row that's new or changed, hence factored
        out here as the one place it's written)."""
        result = lookup_material(
            self.cursor,
            part_number=source.get("PartNumber", ""),
            material_field=source.get("Material", ""),
            description=source.get("Description", ""),
        )

        resolved = dict(source)
        resolved["MatchStatus"] = result.status

        if result.status in AUTO_RESOLVE_STATUSES:
            resolved["JobBossMaterial"] = result.matched_material
            # Stocked_UofM off the matched Material row — traveler_state.py
            # uses this to require a cut length whenever the material is
            # stocked by a linear unit (feet/inches), regardless of Category.
            resolved["JobBossUofM"] = result.matched_uofm
        else:
            resolved["JobBossMaterial"] = None
            resolved["JobBossUofM"] = None
            # Stash candidates for the resolve dialog's Candidates tab.
            resolved["Candidates"] = [
                (c.material_number, c.description, c.is_raw_stock)
                for c in result.candidates
            ]

        # The Inventor extractor only categorizes TUBE/ROUND BAR/BAR;
        # fill in SHEET/PLATE/ANGLE here from the material string.
        if not resolved.get("Category"):
            secondary = classify_secondary_category(resolved.get("Material", ""))
            if secondary:
                resolved["Category"] = secondary

        resolved["TravelerState"] = compute_traveler_state(resolved)
        return resolved

    def start_walk(self) -> None:
        """Walk the base table row by row, resolving each against JobBOSS.

        Clean matches land in the working table auto-resolved; hitches
        land red for the engineer to double-click and fix. A green sweep
        highlight plus a progress bar track position through the base
        table while the walk runs.
        """
        total = self.base_model.rowCount()
        self.base_progress.setVisible(True)
        self.base_progress.setRange(0, total)
        self.base_progress.setValue(0)

        for row in range(total):
            self.base_model.set_walk_progress(row)
            self.base_progress.setValue(row + 1)

            # Yield to Qt's event loop so the sweep/progress bar actually
            # paint — this loop runs on the UI thread and would otherwise
            # finish before a single repaint happens. Pragmatic choice
            # over a worker thread: each lookup is a fast local query.
            QApplication.processEvents()

            source = self.base_model.get_row(row)
            resolved = self._resolve_row(source)
            self.working_model.append_row(resolved)

        self.base_model.set_walk_progress(-1)
        self.base_progress.setVisible(False)

        # Surface the rows needing human attention first.
        self._sort_column = "TravelerState"
        self._sort_ascending = True
        self._apply_sort("TravelerState")
        self._show_sort_indicator("TravelerState", ascending=True)

        self._lock_content_fit_columns(self.base_view, self.base_model)
        self._lock_content_fit_columns(self.working_view, self.working_model)

        # First checkpoint — a session now exists even if nothing gets
        # manually resolved before the app is closed.
        self._save_session()

    # --- Row resolution -----------------------------------------------------

    def on_working_row_double_clicked(self, index) -> None:
        """Open the resolve dialog for red rows. Rows in any other state
        fall through to Qt's built-in inline editing (e.g. typing a
        length into an orange Needs Length row)."""
        row_num = index.row()
        row_data = self.working_model.get_row(row_num)

        if row_data.get("MatchStatus") not in NEEDS_RESOLUTION_STATUSES:
            return

        self._open_resolve_dialog(row_num)

    def _open_resolve_dialog(self, row_num: int) -> None:
        """Open ResolveDialog for `row_num` regardless of its current
        state, and merge back whatever it resolves to. Shared by the
        double-click handler (gated to red rows, so it doesn't steal
        double-click from inline length editing) and the "Resolve..."
        context menu action (available on any row, gate-free — the
        deliberate way to re-open resolution on an already-clean or
        already-attended line, e.g. to fix a bad match after the fact)."""
        row_data = self.working_model.get_row(row_num)

        dialog = ResolveDialog(row_data, self.cursor, parent=self)
        if dialog.exec() == ResolveDialog.Accepted:
            resolution = dialog.resolution()
            if resolution:
                merged = dict(row_data)
                merged.update(resolution)
                merged["TravelerState"] = compute_traveler_state(merged)
                self.working_model.update_row(row_num, merged)

    def on_working_context_menu(self, pos) -> None:
        """Right-click on the working table. Currently offers only
        "Resolve...", which re-opens the resolve dialog for the clicked
        row regardless of its current state — the deliberate way to fix
        a bad match after the fact. TravelerState itself is always
        derived by compute_traveler_state() and can no longer be forced
        by hand (the old "Set State" submenu was removed — see
        traveler_state.py's history for why a manual override was unsafe
        to keep around)."""
        index = self.working_view.indexAt(pos)
        if not index.isValid():
            return
        row_num = index.row()

        menu = QMenu(self)

        resolve_action = QAction("Resolve...", self)
        resolve_action.triggered.connect(
            lambda _checked=False, r=row_num: self._open_resolve_dialog(r)
        )
        menu.addAction(resolve_action)

        menu.exec(self.working_view.viewport().mapToGlobal(pos))

    # --- Sorting (both panels in lockstep) -----------------------------------

    def _sort_value(self, row: dict, key: str):
        """TravelerState sorts by fixed priority (Needs Attention first);
        everything else sorts by its own value."""
        if key == "TravelerState":
            return TRAVELER_STATE_SORT_ORDER.get(row.get(key), 99)
        return row.get(key, "")

    def _apply_sort(self, key: str, reverse: bool = False) -> None:
        """Sort the working table by `key`, then reorder the base table to
        match by _orig_index — keeps every row lined up with its original
        counterpart regardless of substitutions."""
        self.working_model.sort_by(
            lambda r: self._sort_value(r, key), reverse=reverse
        )
        order = [r["_orig_index"] for r in self.working_model._rows]
        self.base_model.reorder_by_original_index(order)

    def on_working_header_clicked(self, column: int) -> None:
        """Header-click sorting on the working table only; the base table
        always mirrors the working table's order. Clicking the same
        column again toggles direction."""
        key, _ = BomTableModel.COLUMNS[column]

        if self._sort_column == key:
            self._sort_ascending = not self._sort_ascending
        else:
            self._sort_column = key
            self._sort_ascending = True

        self._apply_sort(key, reverse=not self._sort_ascending)
        self.working_view.horizontalHeader().setSortIndicator(
            column,
            Qt.AscendingOrder if self._sort_ascending else Qt.DescendingOrder,
        )

    def _show_sort_indicator(self, key: str, ascending: bool) -> None:
        """Point the working table's header sort arrow at `key`'s column."""
        column = next(
            i for i, (col_key, _) in enumerate(BomTableModel.COLUMNS)
            if col_key == key
        )
        self.working_view.horizontalHeader().setSortIndicator(
            column, Qt.AscendingOrder if ascending else Qt.DescendingOrder
        )

    # --- Column widths ----------------------------------------------------

    def _lock_content_fit_columns(self, view: QTableView, model: BomTableModel) -> None:
        """One-time width calculation for short, bounded-content columns
        (Quantity, Category, CutLengthIn, TravelerState): measure their
        ideal width from current content, then lock it in as Fixed.
        Deliberately NOT ResizeToContents — that mode recalculates on
        nearly every repaint (including continuously while dragging the
        splitter), which gets slow with real row counts. A one-time
        measurement + Fixed mode gives the same tight fit with none of
        the ongoing cost."""
        view.resizeColumnsToContents()  # computes ideal width for every column

        header = view.horizontalHeader()
        for key in ("Quantity", "Category", "CutLengthIn", "TravelerState"):
            col = model.view_column_for_key(key)
            width = view.columnWidth(col)
            header.setSectionResizeMode(col, QHeaderView.Fixed)
            view.setColumnWidth(col, width)

    # --- Finalize / export ----------------------------------------------------

    # Top-level Inventor assemblies carry a trailing all-zero item
    # segment (e.g. "28255-01-000") that isn't part of the real JobBOSS
    # serial/quote number — the shop's quote numbers are just
    # job-subassembly ("28255-01"). Only strips a trailing "-0", "-00",
    # "-000", etc. segment; a real non-zero item segment (job-specific
    # part numbers like "28255-01-005") is left alone.
    TOP_LEVEL_SUFFIX = re.compile(r"-0+$")

    def _quote_number_from_loaded_file(self) -> str:
        """Quote number = source filename (no extension), with a
        trailing top-level "-000"-style segment stripped, + 'A',
        e.g. '28255-01-000.json' -> '28255-01A' (not '28255-01-000A').
        JobBOSS quote IDs are uppercase by convention."""
        stem = Path(self._loaded_file_path).stem
        serial = self.TOP_LEVEL_SUFFIX.sub("", stem)
        return f"{serial}A".upper()

    def finalize_and_export(self) -> None:
        if not self._loaded_file_path:
            QMessageBox.warning(self, "Nothing to finalize",
                                 "Import a BOM before finalizing.")
            return

        default_quote_number = self._quote_number_from_loaded_file()
        quote_number, ok = QInputDialog.getText(
            self, "Quote Number",
            "Quote number for this push:",
            text=default_quote_number,
        )
        if not ok:
            return
        quote_number = quote_number.strip().upper()
        if not quote_number:
            QMessageBox.warning(self, "Quote number required",
                                 "Quote number cannot be blank.")
            return

        # --- Pre-check: does JobBOSS already have a Quote under this number? ---
        # Separate from the BOM_Staging_Header check below, which only
        # tracks pushes made through THIS pipeline. This queries JobBOSS's
        # actual Quote table directly — catches a manually-entered quote,
        # or one from any other source, that our staging table has never
        # heard of. No unlock/override path here: the existing quote must
        # be deleted in JobBOSS itself before this tool will write to this
        # quote number.
        self.cursor.execute("SELECT Quote FROM Quote WHERE RFQ = ?", quote_number)
        existing_jobboss_quotes = self.cursor.fetchall()
        if existing_jobboss_quotes:
            QMessageBox.critical(
                self, "Quote already exists in JobBOSS",
                f"Quote number '{quote_number}' already has "
                f"{len(existing_jobboss_quotes)} existing quote record(s) "
                f"in JobBOSS.\n\nThis tool will not write a second quote "
                f"under the same number. Delete the existing quote in "
                f"JobBOSS first, or use a different quote number."
            )
            return

        # --- Pre-check: is this quote number already locked? -----------
        # Catches the case up front, before doing any nesting/export
        # work. IMPORTED is a terminal success — nothing to unlock, no
        # action offered, since JL Check should only ever create one
        # quote per finalized file. PENDING may be a stale lock from a
        # crashed run, so it gets an explicit two-click unlock-then-
        # confirm flow before this method continues.
        self.cursor.execute(
            "SELECT Status, RejectReason, QuoteGuid, ImportedAt, ProcessedAt "
            "FROM Integration.BOM_Staging_Header WHERE QuoteNumber = ?",
            quote_number,
        )
        existing = self.cursor.fetchone()

        if existing is not None and existing.Status in ("PENDING", "IMPORTED"):
            dialog = QuoteLockedDialog(quote_number, existing, self.cursor, parent=self)
            dialog.exec()
            if not dialog.proceed:
                return
            # proceed == True only when status was PENDING and the user
            # explicitly unlocked and confirmed — fall through below to
            # write a fresh submission, same as the normal path.

        rows = [self.working_model.get_row(r) for r in range(self.working_model.rowCount())]

        # Every row carries the same Author — the Inventor add-in reads it
        # once from the top-level assembly document and stamps it onto
        # every BomLineItem (see BomExtractor.TraverseBom), so any row
        # with a non-empty one will do. Assumed to already be the
        # engineer's JobBOSS username (e.g. "LSTRAIN") per Inventor's
        # Author iProperty convention here — written straight through to
        # QuotedBy below, no name-to-username lookup. None (not "") when
        # nothing on the BOM has an Author set, so the watcher/quote_writer
        # fall back to their own default rather than writing a blank.
        quoted_by = next((r.get("Author") for r in rows if r.get("Author")), None)

        # Ignored rows never go to JobBOSS regardless (sheet/plate,
        # manually ignored). Needs Attention / Needs Length rows are
        # skipped rather than blocking finalize — the engineer can push
        # what's ready and resolve the rest separately.
        skip_states = {"Ignored", "Needs Attention", "Needs Length"}
        approved_rows = [r for r in rows if r.get("TravelerState") not in skip_states]
        skipped_rows = [r for r in rows if r.get("TravelerState") in skip_states]

        unresolved_count = sum(
            1 for r in skipped_rows if r.get("TravelerState") in ("Needs Attention", "Needs Length")
        )

        if unresolved_count:
            confirm = QMessageBox.question(
                self, "Unresolved rows will be skipped",
                f"{unresolved_count} row(s) are still red/orange (unresolved) and "
                "will NOT be included in this push.\n\n"
                f"Proceed and export the remaining {len(approved_rows)} row(s)?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return

        if not approved_rows:
            QMessageBox.warning(self, "Nothing to export",
                                 "No rows are ready to push to JobBOSS.")
            return

        try:
            combined_rows = _combine_rows(approved_rows, self.cursor)
        except ValueError as exc:
            QMessageBox.critical(
                self, "Nesting failed",
                f"Could not finalize — a material is missing setup data:\n\n{exc}"
            )
            return

        payload = {
            "QuoteNumber": quote_number,
            "SourceFile": os.path.basename(self._loaded_file_path),
            "QuotedBy": quoted_by,
            "Rows": [_to_export_row(r) for r in combined_rows],
        }

        self.INBOX_PATH.mkdir(parents=True, exist_ok=True)
        output_path = self.INBOX_PATH / f"{quote_number}.json"

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

        self._save_session()

        # One outcome dialog, Close only — no further unlock/retry loop.
        # A genuine failure here means fixing the real issue and clicking
        # Finalize again from the main window, not looping inside a dialog.
        PushOutcomeDialog(quote_number, self.cursor, parent=self).exec()


def main() -> None:
    app = QApplication(sys.argv)

    # Fusion gives a consistent look regardless of how the process was
    # launched (terminal vs. Process.Start from the Inventor add-in).
    app.setStyle("Fusion")
    apply_theme(app, get_theme())

    # Optional argv[1] is a BOM JSON path — how the Inventor add-in opens
    # this tool with the BOM pre-loaded.
    initial_file = sys.argv[1] if len(sys.argv) > 1 else None

    window = MainWindow()
    window.show()

    # Load AFTER show() so the window is visible and paintable while the
    # walk runs — otherwise the sweep/progress bar do their work against
    # an invisible window and the user sees none of it.
    if initial_file:
        window.load_file(initial_file)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()