"""
bompush_service — watches the shared inbox for JL Check's approved
exports and pushes each one into JobBOSS as a real quote.

Pipeline per file:
  1. Claim it — move into inbox/processing/ (same-process debounce: a
     filesystem write can fire multiple watchdog events for one file).
  2. Claim the quote number in the database — an INSERT into
     BOM_Staging_Header keyed by QuoteNumber. This is the real,
     cross-process lock (see staging.py): if another instance already
     claimed this quote number, the INSERT fails and we back off rather
     than risk a double-write.
  3. Insert staging detail rows + call write_quote(), in one
     transaction. Staging details and the real JobBOSS write succeed or
     fail together — a failed write never leaves staging rows claiming
     a success that didn't happen.
  4. On success: commit, mark the staging header IMPORTED, move the
     file to inbox/processed/.
     On failure: roll back, mark the staging header ERROR with the
     reason, move the file to inbox/error/ for a human to look at.

Concurrency note: within one running instance, filesystem events are
handled one at a time (no worker pool yet) — the database-level lock is
what protects against a SECOND instance or a restart racing on the same
quote number, not against internal parallelism, since there isn't any
yet. If the inbox ever backs up under load, that's the place to add a
small worker pool; the staging lock already makes that safe to do.

Run: python watcher_service.py
Stop: Ctrl+C
"""

import json
import logging
import shutil
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from db import get_connection
from quote_writer import QuoteLine, write_quote
from staging import insert_staging_details, mark_error, mark_imported, try_claim_quote

# Shared location IT provisioned (\\SYS\sys\BOMIntegration). Per their
# explicit instruction, the watcher uses the UNC path, not the F:
# mapped drive — a mapped drive letter is a per-user-session convenience
# and isn't guaranteed to exist/resolve the same way for a service
# account running unattended.
BOM_INTEGRATION_ROOT = Path(r"\\SYS\sys\BOMIntegration")

# These are SIBLING folders directly under the root — matches IT's real
# provisioned structure exactly (NOT nested inside Incoming the way an
# earlier version of this file had them as inbox/processing/processed/
# error subfolders).
INBOX_PATH = BOM_INTEGRATION_ROOT / "Incoming"
PROCESSING_PATH = BOM_INTEGRATION_ROOT / "Processing"
PROCESSED_PATH = BOM_INTEGRATION_ROOT / "Completed"
ERROR_PATH = BOM_INTEGRATION_ROOT / "Error"
LOGS_PATH = BOM_INTEGRATION_ROOT / "Logs"

# How long to wait after seeing a file-creation event before touching the
# file, so a still-in-progress write (JL Check's json.dump) has time to
# finish and flush before we try to read it.
DEBOUNCE_SECONDS = 1.0

log = logging.getLogger("bompush_service")
log.setLevel(logging.INFO)
_log_formatter = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s")

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_log_formatter)
log.addHandler(_console_handler)

# File logging into IT's provisioned Logs folder — best-effort. A
# network hiccup or permissions issue on the share shouldn't take the
# whole service down; console logging alone is still enough to debug a
# live session, so this degrades gracefully rather than crashing at
# startup.
try:
    LOGS_PATH.mkdir(parents=True, exist_ok=True)
    _file_handler = logging.FileHandler(LOGS_PATH / "bompush_service.log", encoding="utf-8")
    _file_handler.setFormatter(_log_formatter)
    log.addHandler(_file_handler)
except OSError:
    log.warning(f"Could not set up file logging in {LOGS_PATH} — continuing with console logging only.")


def _ensure_folders() -> None:
    for path in (INBOX_PATH, PROCESSING_PATH, PROCESSED_PATH, ERROR_PATH):
        path.mkdir(parents=True, exist_ok=True)


def _build_lines(payload: dict) -> list[QuoteLine]:
    lines = []
    for row in payload["Rows"]:
        is_custom = row.get("MatchStatus") == "custom_line"
        lines.append(QuoteLine(
            jobboss_material=row.get("JobBossMaterial", ""),
            quantity=row.get("Quantity", 0) or 0,
            cut_length_in=row.get("CutLengthIn"),
            total_stock_length_in=row.get("TotalStockLengthIn"),
            material_used_in=row.get("MaterialUsedIn"),
            is_custom_line=is_custom,
            description=row.get("Description") if is_custom else None,
            ext_description=row.get("ExtDescription") if is_custom else None,
        ))
    return lines


def _move_to(path: Path, destination_folder: Path) -> None:
    """Moves a file, adding a numeric suffix if the destination already
    has a file with that name (e.g. reprocessing the same quote number
    after a fix) rather than silently overwriting."""
    target = destination_folder / path.name
    counter = 1
    while target.exists():
        target = destination_folder / f"{path.stem}_{counter}{path.suffix}"
        counter += 1
    shutil.move(str(path), str(target))


def process_file(path: Path) -> None:
    """
    Runs one export file through the full claim -> stage -> write
    pipeline. `path` is expected to already be sitting in
    PROCESSING_PATH (moved there by the caller before this runs).
    """
    log.info(f"Processing {path.name}")

    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.error(f"Could not read {path.name}: {exc}")
        _move_to(path, ERROR_PATH)
        return

    if not isinstance(payload, dict):
        log.error(
            f"{path.name} is not a finalized JL Check export (expected a "
            f"JSON object with QuoteNumber/Rows, got a {type(payload).__name__}) "
            f"— moving to error/. This folder should only ever receive "
            f"Finalize output, not raw Inventor exports."
        )
        _move_to(path, ERROR_PATH)
        return

    quote_number = payload.get("QuoteNumber", "").upper()
    if not quote_number:
        log.error(f"{path.name} has no QuoteNumber — moving to error/")
        _move_to(path, ERROR_PATH)
        return

    if "Rows" not in payload or not isinstance(payload["Rows"], list):
        log.error(
            f"{path.name} doesn't look like a finalized JL Check export "
            f"(missing/invalid 'Rows') — moving to error/. This folder "
            f"should only ever receive Finalize output, not raw Inventor "
            f"exports."
        )
        _move_to(path, ERROR_PATH)
        return

    conn = get_connection()
    conn.autocommit = False
    cursor = conn.cursor()

    try:
        # --- Step 1: claim the quote number (own commit, real lock) -----
        claimed = try_claim_quote(
            cursor, quote_number,
            source_file=payload.get("SourceFile", path.name),
        )
        if not claimed:
            log.warning(
                f"Quote '{quote_number}' is already claimed (by this or "
                f"another instance) — {path.name} moved to error/ for review."
            )
            _move_to(path, ERROR_PATH)
            return

        # --- Steps 2-3: staging details + the real JobBOSS write, ------
        # one transaction — succeed or fail together.
        insert_staging_details(cursor, quote_number, payload["Rows"])

        lines = _build_lines(payload)
        quote_guid = write_quote(
            cursor,
            quote_number=quote_number,
            part_number=quote_number,
            description=f"Imported from {payload.get('SourceFile', '')}",
            lines=lines,
        )

        conn.commit()
        mark_imported(cursor, quote_number, quote_guid)

        log.info(f"Wrote quote '{quote_number}' ({len(lines)} lines), GUID {quote_guid}")
        _move_to(path, PROCESSED_PATH)

    except Exception as exc:
        conn.rollback()
        reason = f"{type(exc).__name__}: {exc}"
        log.error(f"Failed to write quote '{quote_number}': {reason}")
        try:
            mark_error(cursor, quote_number, reason)
        except Exception:
            log.exception("Also failed to record the error in staging — check manually.")
        _move_to(path, ERROR_PATH)

    finally:
        conn.close()


class InboxHandler(FileSystemEventHandler):
    """Watches for new .json files landing directly in the inbox root
    (not in its processing/processed/error subfolders)."""

    def on_created(self, event):
        if event.is_directory:
            return
        self._handle(Path(event.src_path))

    def on_moved(self, event):
        # Some editors/writers save via write-then-rename; catch that
        # pattern too.
        if event.is_directory:
            return
        self._handle(Path(event.dest_path))

    def _handle(self, path: Path):
        try:
            self._handle_inner(path)
        except Exception:
            # Never let anything escape this method — an uncaught
            # exception here kills watchdog's dispatch thread silently,
            # which would stop ALL future file processing, not just this
            # one file. Every real failure mode should already be
            # handled inside process_file (which moves the file to
            # error/ and logs), so reaching this is a bug — but even a
            # bug here should degrade to "one file didn't get handled,
            # logged loudly" rather than "the whole service goes quiet."
            log.exception(f"Unexpected error handling {path.name} — service is still running.")

    def _handle_inner(self, path: Path):
        if path.suffix.lower() != ".json":
            return
        if path.parent != INBOX_PATH:
            return  # ignore events inside processing/processed/error

        time.sleep(DEBOUNCE_SECONDS)  # let the write finish

        if not path.exists():
            return  # moved/deleted before we got to it

        claimed_path = PROCESSING_PATH / path.name
        try:
            shutil.move(str(path), str(claimed_path))
        except (OSError, shutil.Error) as exc:
            log.warning(f"Could not claim {path.name} for processing: {exc}")
            return

        process_file(claimed_path)


def process_existing_files() -> None:
    """Startup sweep: handles any .json files already sitting in the
    inbox root when the service starts (watchdog only reports events
    that happen after it starts watching)."""
    existing = sorted(INBOX_PATH.glob("*.json"))
    if not existing:
        return

    log.info(f"Found {len(existing)} file(s) already in the inbox — processing now.")
    for path in existing:
        claimed_path = PROCESSING_PATH / path.name
        try:
            shutil.move(str(path), str(claimed_path))
        except (OSError, shutil.Error) as exc:
            log.warning(f"Could not claim {path.name} for processing: {exc}")
            continue

        try:
            process_file(claimed_path)
        except Exception:
            # Same reasoning as InboxHandler._handle: one bad file in
            # the startup sweep should never prevent the service from
            # reaching observer.start() and watching for new arrivals.
            log.exception(f"Unexpected error processing {claimed_path.name} at startup.")


def main() -> None:
    _ensure_folders()

    log.info(f"Watching {INBOX_PATH} for approved BOM exports...")
    process_existing_files()

    observer = Observer()
    observer.schedule(InboxHandler(), str(INBOX_PATH), recursive=False)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Stopping...")
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()