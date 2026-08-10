-- ==============================================================================
-- BOM_Staging schema — for the bompush_service watcher.
-- Changes need to be made by IT (I (Luke) have no DDL rights on TESTPROD).
--
-- Schema: Integration, per IT's recommendation to keep custom objects
-- separate from standard JobBOSS tables.
--
-- Two tables:
--   Integration.BOM_Staging_Header — one row per quote submission.
--     QuoteNumber is the PRIMARY KEY, which doubles as the cross-process
--     lock: two service instances (or two runs) racing to claim the
--     same quote number will have one INSERT succeed and one fail with
--     a duplicate-key error, atomically — no separate lock table needed.
--   Integration.BOM_Staging_Detail — one row per material line, FK back
--     to the header.
--
-- Status values on the header: PENDING (claimed, not yet processed),
-- IMPORTED (successfully written to JobBOSS), ERROR (failed, see
-- RejectReason).
--
-- HOW RECORDS ARE CREATED / UPDATED / REMOVED (see staging.py):
--   Created  — INSERT, when a quote number is submitted for the first
--              time. The INSERT succeeding IS the lock acquisition.
--   Updated  — UPDATE only, in two cases: (1) the service marks a
--              PENDING row IMPORTED or ERROR once processing finishes;
--              (2) a previously-ERROR row can be reclaimed by a fresh
--              submission of the same quote number via a conditional
--              UPDATE (WHERE Status = 'ERROR') — this lets a failed
--              submission be retried after the underlying issue is
--              fixed, without ever needing to delete anything.
--   Removed  — NEVER by the service. No DELETE statements anywhere in
--              this design. See "Cleanup / retention" below.
--
-- STALE / STUCK LOCKS: a row can only be left in PENDING forever if the
-- service crashes mid-processing. Because the actual JobBOSS write and
-- the staging detail rows are committed together in one transaction,
-- a crash before that commit means NOTHING was written to JobBOSS's
-- real tables — the transaction is simply abandoned and rolled back by
-- SQL Server when the connection closes. So a stuck PENDING row is
-- always safe to resolve: a person with UPDATE rights (which this
-- service already has) can manually run
--     UPDATE Integration.BOM_Staging_Header
--     SET Status = 'ERROR', RejectReason = 'Manually reset — stale PENDING'
--     WHERE QuoteNumber = '<...>' AND Status = 'PENDING';
-- after which the quote number becomes retryable automatically the next
-- time that file is resubmitted. No automatic timeout/expiry is built
-- in yet — flagged as a reasonable future addition (a periodic sweep
-- that auto-resets PENDING rows older than some threshold), not
-- included now to avoid the service ever taking that decision away
-- from a human without being asked to.
--
-- CLEANUP / RETENTION: rows are never deleted; each is a small, cheap
-- audit record (no blobs, capped varchars) and the intent is to keep a
-- permanent history of every submission. Given TESTPROD is periodically
-- overwritten wholesale from a PRODUCTION refresh, these tables (and
-- all their data) will be wiped on every refresh regardless, since they
-- don't exist in PRODUCTION at all — so long-term retention is a
-- non-issue until/unless this moves to a dedicated integration database
-- or PRODUCTION itself, at which point retention policy should be
-- revisited. This script will need to be re-run after every TESTPROD
-- refresh to recreate these tables.
--
-- PERMISSIONS NEEDED: SELECT, INSERT, UPDATE only, on these two tables
-- specifically. No DELETE. No stored procedures — every operation above
-- is a plain INSERT or UPDATE issued directly by the Python service.
--
-- SERVICE ACCOUNT: currently run manually under Luke's own Windows
-- account (SMALLEY\lstrain) during development/testing. If/when this
-- moves to unattended operation on a server, it should run under a
-- dedicated service account rather than a personal login — flagging
-- this as an open item to settle with IT before that happens, not
-- something this script assumes.
--
-- SAFETY GUARD: TESTPROD and PRODUCTION share this SQL Server instance
-- (JBSERVER\SQLEXPRESS). This script refuses to run unless the current
-- database context is genuinely TESTPROD, so it can't accidentally
-- create these tables in PRODUCTION if a query window happens to be
-- pointed at the wrong database when it's run.
-- ==============================================================================

IF DB_NAME() <> 'TESTPROD'
BEGIN
    THROW 51000, 'Refusing to run: current database is not TESTPROD. Switch the query window to TESTPROD and re-run.', 1;
END

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'Integration')
BEGIN
    EXEC('CREATE SCHEMA Integration');
END

CREATE TABLE Integration.BOM_Staging_Header (
    QuoteNumber        VARCHAR(50)   NOT NULL PRIMARY KEY,
    SourceFile         VARCHAR(255),
    SubmittedBy        VARCHAR(50),
    ImportedAt         DATETIME      NOT NULL DEFAULT GETDATE(),
    Status             VARCHAR(20)   NOT NULL DEFAULT 'PENDING',
    RejectReason       VARCHAR(500),
    QuoteGuid          VARCHAR(50),  -- the JobBOSS Quote.Quote GUID, once written
    ProcessedAt        DATETIME
);

-- Supports quick "find everything currently PENDING / ERROR" queries —
-- the kind of lookup a manual review of stuck locks or failures would
-- run. Not required for correctness, just for that query's performance.
CREATE INDEX IX_BOM_Staging_Header_Status
    ON Integration.BOM_Staging_Header (Status);

CREATE TABLE Integration.BOM_Staging_Detail (
    StagingDetailID    INT IDENTITY(1,1) PRIMARY KEY,
    QuoteNumber        VARCHAR(50)   NOT NULL
        FOREIGN KEY REFERENCES Integration.BOM_Staging_Header(QuoteNumber),
    PartNumber         VARCHAR(100),
    Description        VARCHAR(255),
    Quantity           FLOAT,
    Material           VARCHAR(255),
    Category           VARCHAR(50),
    CutLengthIn        FLOAT,
    JobBossMaterial    VARCHAR(50),
    MatchStatus        VARCHAR(50),
    TravelerState      VARCHAR(50),
    ConflictNotes      VARCHAR(500),
    SourcePartNumbers  VARCHAR(1000),
    CutList            VARCHAR(1000),
    TotalStockLengthIn FLOAT,
    MaterialUsedIn     FLOAT
);

-- SQL Server does not automatically index foreign-key columns — added
-- explicitly for lookup/join performance (e.g. "show me every detail
-- row for this quote number").
CREATE INDEX IX_BOM_Staging_Detail_QuoteNumber
    ON Integration.BOM_Staging_Detail (QuoteNumber);