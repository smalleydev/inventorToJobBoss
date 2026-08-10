from db import get_connection

QUOTE_NUMBER = "TEST_ASSEMA"

conn = get_connection()
cursor = conn.cursor()

print("=" * 70)
print("Integration.BOM_Staging_Header")
print("=" * 70)
cursor.execute(
    "SELECT * FROM Integration.BOM_Staging_Header WHERE QuoteNumber = ?",
    QUOTE_NUMBER,
)
columns = [d[0] for d in cursor.description]
rows = cursor.fetchall()
print(f"{len(rows)} row(s)")
for row in rows:
    print(" ", dict(zip(columns, row)))

print("\n" + "=" * 70)
print("Integration.BOM_Staging_Detail")
print("=" * 70)
cursor.execute(
    "SELECT * FROM Integration.BOM_Staging_Detail WHERE QuoteNumber = ? ORDER BY StagingDetailID",
    QUOTE_NUMBER,
)
columns = [d[0] for d in cursor.description]
rows = cursor.fetchall()
print(f"{len(rows)} row(s)")
for row in rows:
    print(" ", dict(zip(columns, row)))

print("\n" + "=" * 70)
print("dbo.RFQ")
print("=" * 70)
cursor.execute("SELECT * FROM RFQ WHERE RFQ = ?", QUOTE_NUMBER)
columns = [d[0] for d in cursor.description]
rows = cursor.fetchall()
print(f"{len(rows)} row(s)")
for row in rows:
    print(" ", dict(zip(columns, row)))

print("\n" + "=" * 70)
print("dbo.Quote (every record for this RFQ)")
print("=" * 70)
cursor.execute("SELECT * FROM Quote WHERE RFQ = ?", QUOTE_NUMBER)
columns = [d[0] for d in cursor.description]
quote_rows = cursor.fetchall()
print(f"{len(quote_rows)} row(s)")
for row in quote_rows:
    print(" ", dict(zip(columns, row)))

print("\n" + "=" * 70)
print("dbo.Quote_Qty / Quote_Req / Quote_Req_Qty (per Quote GUID above)")
print("=" * 70)
for qrow in quote_rows:
    guid = qrow.Quote
    print(f"\n--- GUID {guid} ---")

    cursor.execute("SELECT * FROM Quote_Qty WHERE Quote = ?", guid)
    cols = [d[0] for d in cursor.description]
    qty_rows = cursor.fetchall()
    print(f"  Quote_Qty: {len(qty_rows)} row(s)")
    for r in qty_rows:
        print("   ", dict(zip(cols, r)))

    cursor.execute("SELECT * FROM Quote_Req WHERE Quote = ?", guid)
    cols = [d[0] for d in cursor.description]
    req_rows = cursor.fetchall()
    print(f"  Quote_Req: {len(req_rows)} row(s)")
    for r in req_rows:
        print("   ", dict(zip(cols, r)))

    cursor.execute("SELECT * FROM Quote_Req_Qty WHERE Quote = ?", guid)
    cols = [d[0] for d in cursor.description]
    reqqty_rows = cursor.fetchall()
    print(f"  Quote_Req_Qty: {len(reqqty_rows)} row(s)")
    for r in reqqty_rows:
        print("   ", dict(zip(cols, r)))

conn.close()