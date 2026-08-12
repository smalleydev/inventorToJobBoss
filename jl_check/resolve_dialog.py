"""
Resolution dialog for hitch rows (ambiguous / needs_review_uhmw /
not_found). Opened by double-clicking a red row in the working table.

Layout: tabs (Candidates / Search) on the left, a details pane on the
right that updates whenever a result is selected in either tab — it pulls
the full Material record from JobBOSS, including Ext_Description, which
the compact list rows don't show.

Actions available:
  - Pick a candidate or search result -> resolved_manual
  - Ignore this line item entirely -> manually_ignored (excluded from
    the final export, same as sheet/plate)
  - Add as Custom Line -> custom_line (last resort when nothing in the
    material library or raw stock is right; ID/Description prefilled
    from this row's own Inventor PartNumber/Description, both editable)
  - Cancel -> no change to the row

Usage: construct, exec(), then read resolution() — a dict of row updates
to merge into the working row, or None if cancelled.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPushButton, QTabWidget, QTextEdit,
    QVBoxLayout, QWidget,
)

from custom_line_dialog import CustomLineDialog

# Fields shown in the details pane when a result is selected. Kept short
# on purpose — it's a quick-reference panel, not a full record dump.
DETAIL_FIELDS = [
    "Material", "Description", "Ext_Description", "Class", "Shape",
    "Make_Buy", "Stocked_UofM", "Primary_Vendor", "Vendor_Reference",
]

# Cap on manual search results.
MAX_SEARCH_RESULTS = 50


def parse_search_terms(text: str) -> list[list[str]]:
    """
    Parses a search string into AND-terms, where each term is a list of
    OR-alternatives. Curly braces group alternatives that should be
    treated as "any of these" within one AND position; commas outside
    braces separate the AND positions themselves. Whitespace around
    each piece is stripped, so spacing after commas is optional either
    way. Phrases containing spaces (but no commas) pass through intact,
    since only commas are delimiters.

    "angle, 2X2, {SS, stainless steel}, 304"
    -> [["angle"], ["2X2"], ["SS", "stainless steel"], ["304"]]
    """
    terms: list[list[str]] = []
    current_alternatives: list[str] = []
    current_text = ""
    depth = 0

    for ch in text:
        if ch == "{":
            depth += 1
            if depth == 1:
                current_text = ""  # discard anything stray before the brace
            else:
                current_text += ch
        elif ch == "}":
            if depth == 1:
                if current_text.strip():
                    current_alternatives.append(current_text.strip())
                depth = 0
                current_text = ""
            else:
                depth = max(depth - 1, 0)
                current_text += ch
        elif ch == ",":
            if depth == 0:
                if current_alternatives:
                    terms.append(current_alternatives)
                    current_alternatives = []
                elif current_text.strip():
                    terms.append([current_text.strip()])
                current_text = ""
            else:
                if current_text.strip():
                    current_alternatives.append(current_text.strip())
                current_text = ""
        else:
            current_text += ch

    if current_alternatives:
        terms.append(current_alternatives)
    elif current_text.strip():
        terms.append([current_text.strip()])

    return terms


def _matches_all_terms(text: str, terms: list[list[str]]) -> bool:
    """True if `text` contains at least one alternative from every
    AND-term (case-insensitive substring match). Used to filter the
    already-fetched search results client-side — SQL Server's collation
    handles case-insensitivity for the database search itself, but this
    is plain Python string comparison, so it's folded explicitly here."""
    text_lower = text.lower()
    return all(
        any(alt.lower() in text_lower for alt in alternatives)
        for alternatives in terms
    )


class ResolveDialog(QDialog):
    """Modal dialog that produces a resolution dict for one hitch row."""

    def __init__(self, row: dict, cursor, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Resolve — {row.get('PartNumber', '')}")
        self.resize(950, 550)

        self._row = row
        self._cursor = cursor  # shared app cursor; caller owns the connection
        self._resolution: dict | None = None

        self._build_ui()
        self._populate_candidates_tab()

    # --- UI construction ----------------------------------------------------

    def _build_ui(self) -> None:
        outer_layout = QVBoxLayout(self)

        # Context header: what we're resolving.
        outer_layout.addWidget(QLabel(f"<b>Part Number:</b> {self._row.get('PartNumber', '')}"))
        outer_layout.addWidget(QLabel(f"<b>Description:</b> {self._row.get('Description', '')}"))
        outer_layout.addWidget(QLabel(f"<b>Material:</b> {self._row.get('Material', '')}"))
        outer_layout.addWidget(QLabel(f"<b>Status:</b> {self._row.get('MatchStatus', '')}"))

        # Left/right split: tabs on the left, details pane on the right.
        split_layout = QHBoxLayout()
        outer_layout.addLayout(split_layout, stretch=1)

        left_side = QVBoxLayout()
        split_layout.addLayout(left_side, stretch=2)

        # Candidates first — it's the initial tab because for the common
        # "ambiguous" case the answer is usually already in that list.
        self.tabs = QTabWidget()
        left_side.addWidget(self.tabs)
        self.tabs.addTab(self._build_candidates_tab(), "Candidates")
        self.tabs.addTab(self._build_search_tab(), "Search")

        self.details_pane = QTextEdit()
        self.details_pane.setReadOnly(True)
        self.details_pane.setPlaceholderText("Select a result to see full JobBOSS details...")
        split_layout.addWidget(self.details_pane, stretch=1)

        # Ignore this line item entirely — drops it from the final export.
        ignore_btn = QPushButton("Ignore This Item")
        ignore_btn.clicked.connect(self._mark_as_ignored)
        outer_layout.addWidget(ignore_btn)

        # Last resort: nothing in the material library or raw stock is
        # right. Opens CustomLineDialog prefilled from this row's own
        # Inventor data.
        custom_line_btn = QPushButton("Add as Custom Line")
        custom_line_btn.clicked.connect(self._add_as_custom_line)
        outer_layout.addWidget(custom_line_btn)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        buttons.rejected.connect(self.reject)
        outer_layout.addWidget(buttons)

    def _build_candidates_tab(self) -> QWidget:
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)

        self.candidates_list = QListWidget()
        self.candidates_list.currentItemChanged.connect(self._on_selection_changed)
        self.candidates_list.itemDoubleClicked.connect(
            lambda _item: self._accept_selected(self.candidates_list)
        )
        tab_layout.addWidget(self.candidates_list)

        use_btn = QPushButton("Use Selected Material")
        use_btn.clicked.connect(lambda: self._accept_selected(self.candidates_list))
        tab_layout.addWidget(use_btn)

        return tab

    def _build_search_tab(self) -> QWidget:
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)

        search_row = QHBoxLayout()
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search JobBOSS materials...")
        # Pre-fill with the row's own part number — the most likely
        # starting point for a manual search when the automatic lookup
        # didn't find a confident match.
        self.search_box.setText(self._row.get("PartNumber", ""))
        self.search_box.returnPressed.connect(self._run_search)
        search_row.addWidget(self.search_box)

        search_btn = QPushButton("Search")
        search_btn.clicked.connect(self._run_search)
        search_row.addWidget(search_btn)
        tab_layout.addLayout(search_row)

        self.search_results = QListWidget()
        self.search_results.currentItemChanged.connect(self._on_selection_changed)
        self.search_results.itemDoubleClicked.connect(
            lambda _item: self._accept_selected(self.search_results)
        )
        tab_layout.addWidget(self.search_results)

        # Narrows the results already returned by the search above —
        # entirely client-side (no new database query), updates live as
        # you type. Separate from the base search itself: the search box
        # finds the candidate pool (e.g. by part number), this filters
        # which of those results are actually worth looking at.
        self.filter_box = QLineEdit()
        self.filter_box.setPlaceholderText(
            "Filter these results... angle, 2X2, {SS, stainless steel}, 304"
        )
        self.filter_box.textChanged.connect(self._apply_result_filter)
        tab_layout.addWidget(self.filter_box)

        use_btn = QPushButton("Use Selected Material")
        use_btn.clicked.connect(lambda: self._accept_selected(self.search_results))
        tab_layout.addWidget(use_btn)

        return tab

    # --- Population / interaction -------------------------------------------

    @staticmethod
    def _make_result_item(number: str, description: str = "", is_raw_stock: bool = False) -> QListWidgetItem:
        label = f"{number} — {description}" if description else number
        item = QListWidgetItem(label)
        item.setData(Qt.UserRole, number)
        item.setData(Qt.UserRole + 1, is_raw_stock)
        return item

    def _populate_candidates_tab(self) -> None:
        """Fill the Candidates tab from what the lookup already found.
        `Candidates` on the row is a list of (number, description) pairs
        stashed by the walk for ambiguous results; UHMW rows carry a
        single pre-matched material needing confirmation instead."""

        for candidate in self._row.get("Candidates") or []:
            if isinstance(candidate, (list, tuple)):
                number = candidate[0]
                description = candidate[1] if len(candidate) > 1 else ""
                is_raw_stock = candidate[2] if len(candidate) > 2 else False
            else:
                number, description, is_raw_stock = candidate, "", False
            self.candidates_list.addItem(self._make_result_item(number, description, is_raw_stock))

        if (self._row.get("MatchStatus") == "needs_review_uhmw"
                and self._row.get("JobBossMaterial")):
            self.candidates_list.addItem(self._make_result_item(
                self._row["JobBossMaterial"], "(raw stock match, needs confirmation)"
            ))

        if self.candidates_list.count() == 0:
            placeholder = QListWidgetItem("No candidates found — try the Search tab.")
            placeholder.setFlags(Qt.NoItemFlags)  # not selectable/acceptable
            self.candidates_list.addItem(placeholder)

    def _on_selection_changed(self, current: QListWidgetItem,
                              _previous: QListWidgetItem) -> None:
        """Refresh the details pane whenever a result is highlighted in
        either tab."""
        if current is None or not current.data(Qt.UserRole):
            self.details_pane.clear()
            return
        self._show_material_details(current.data(Qt.UserRole))

    def _show_material_details(self, material_number: str) -> None:
        # DETAIL_FIELDS is a module constant, not user input — safe to
        # interpolate into the column list. The material number itself
        # stays parameterized.
        columns = ", ".join(DETAIL_FIELDS)
        self._cursor.execute(
            f"SELECT {columns} FROM Material WHERE Material = ?",
            material_number,
        )
        row = self._cursor.fetchone()

        if row is None:
            self.details_pane.setPlainText(f"No record found for {material_number}.")
            return

        lines = []
        for field_name in DETAIL_FIELDS:
            value = getattr(row, field_name)
            lines.append(f"<b>{field_name.replace('_', ' ')}:</b> {value if value else '—'}")
        self.details_pane.setHtml("<br>".join(lines))

    def _run_search(self) -> None:
        """Base search against JobBOSS — whatever's in search_box
        (typically a part number). Re-running this replaces the result
        pool entirely and clears any filter text, since a filter from
        the previous search's results doesn't necessarily still make
        sense for a new one."""
        term = self.search_box.text().strip()
        self.search_results.clear()
        self.filter_box.blockSignals(True)
        self.filter_box.clear()
        self.filter_box.blockSignals(False)

        if not term:
            return

        self._cursor.execute(
            f"SELECT TOP {MAX_SEARCH_RESULTS} Material, Description FROM Material "
            "WHERE Material LIKE ? OR Description LIKE ?",
            f"%{term}%", f"%{term}%",
        )

        for row in self._cursor.fetchall():
            self.search_results.addItem(self._make_result_item(row.Material, row.Description))

    def _apply_result_filter(self) -> None:
        """Narrows the already-fetched search_results list to items
        matching the filter box, entirely client-side — hides
        non-matching rows via setHidden rather than re-querying."""
        terms = parse_search_terms(self.filter_box.text())

        for i in range(self.search_results.count()):
            item = self.search_results.item(i)
            item.setHidden(bool(terms) and not _matches_all_terms(item.text(), terms))

    # --- Resolution outcomes -------------------------------------------------

    def _accept_selected(self, list_widget: QListWidget) -> None:
        item = list_widget.currentItem()
        if item is None or not (item.flags() & Qt.ItemIsSelectable):
            return

        is_raw_stock = item.data(Qt.UserRole + 1) or False
        material_number = item.data(Qt.UserRole)

        # Fetch the matched material's real description so the working
        # table can display the correction in-line (see bom_model.py's
        # Material-column override). The original row["Material"] (raw
        # Inventor string) is deliberately left untouched here — it's
        # the only way back if this resolution ever needs to be undone.
        self._cursor.execute(
            "SELECT Description FROM Material WHERE Material = ?",
            material_number,
        )
        material_row = self._cursor.fetchone()
        material_description = material_row.Description if material_row else ""

        self._resolution = {
            "MatchStatus": "resolved_manual_raw_stock" if is_raw_stock else "resolved_manual",
            "JobBossMaterial": material_number,
            "JobBossDescription": material_description,
            "ConflictNotes": None,
            "HasConflict": False,
        }
        self.accept()

    def _mark_as_ignored(self) -> None:
        self._resolution = {
            "MatchStatus": "manually_ignored",
            "JobBossMaterial": None,
            "ConflictNotes": "Manually ignored by engineer — excluded from JobBOSS export",
            "HasConflict": False,
        }
        self.accept()

    def _add_as_custom_line(self) -> None:
        """Opens CustomLineDialog prefilled from this row's own Inventor
        PartNumber/Description — the natural starting point when nothing
        else matched. Nested dialog: cancelling it returns here with
        this ResolveDialog still open and nothing changed."""
        dialog = CustomLineDialog(
            parent=self,
            id_prefill=self._row.get("PartNumber", ""),
            description_prefill=self._row.get("Description", ""),
            ext_description_prefill=self._row.get("Material", ""),
        )
        if dialog.exec() != CustomLineDialog.Accepted:
            return
        result = dialog.result()
        if not result:
            return

        self._resolution = {
            "MatchStatus": "custom_line",
            "JobBossMaterial": result["Id"],
            "Description": result["Description"],
            "ExtDescription": result["ExtDescription"],
            "ConflictNotes": None,
            "HasConflict": False,
        }
        self.accept()

    def resolution(self) -> dict | None:
        """Call after exec() returns Accepted to get the chosen resolution
        (a dict of row updates), or None if the dialog was cancelled."""
        return self._resolution