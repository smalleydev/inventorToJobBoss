"""
One-off, read-only schema check — run this BEFORE trusting the
custom-line branch added to quote_writer.py.

Answers two questions the custom-line write path depends on:

  1. Does Quote_Req have an Ext_Description column? If yes, the custom
     line's extended description can be written straight into it. If
     no, it needs to go somewhere else (Note_Text/Comment if those
     exist, or dropped with a warning).

  2. Is Quote_Req.Material actually FK-constrained back to Material?
     The ValueError you hit was a Python-side check in _lookup_material
     — not a database error — so we don't yet know whether the DB
     itself would accept an arbitrary string in that column. If there
     IS a real FK, writing a custom line's made-up ID there will throw
     a hard SQL error at insert time (which is fine — fails loudly,
     doesn't corrupt anything — but means the custom-line write needs a
     different target, not just a skipped Python check).

Run: python check_quote_req_schema.py
"""

from db import get_connection

conn = get_connection()
cursor = conn.cursor()

print("=" * 70)
print("Quote_Req columns")
print("=" * 70)
cursor.execute(
    """
    SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_NAME = 'Quote_Req'
    ORDER BY ORDINAL_POSITION
    """
)
columns = [d[0] for d in cursor.description]
rows = cursor.fetchall()
has_ext_description = False
for row in rows:
    print(" ", dict(zip(columns, row)))
    if row.COLUMN_NAME == "Ext_Description":
        has_ext_description = True

print(f"\n{'FOUND' if has_ext_description else 'NOT FOUND'}: Ext_Description column on Quote_Req")

print("\n" + "=" * 70)
print("Foreign keys FROM Quote_Req (checking for Material -> Material FK)")
print("=" * 70)
cursor.execute(
    """
    SELECT
        fk.name AS FK_Name,
        c.name AS Column_Name,
        OBJECT_NAME(fk.referenced_object_id) AS Referenced_Table
    FROM sys.foreign_keys fk
    JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
    JOIN sys.columns c ON c.object_id = fkc.parent_object_id AND c.column_id = fkc.parent_column_id
    WHERE fk.parent_object_id = OBJECT_ID('Quote_Req')
    """
)
columns = [d[0] for d in cursor.description]
fk_rows = cursor.fetchall()
if not fk_rows:
    print("  No foreign keys found on Quote_Req at all.")
else:
    for row in fk_rows:
        print(" ", dict(zip(columns, row)))

material_fk = any(r.Column_Name == "Material" for r in fk_rows)
print(f"\n{'FOUND' if material_fk else 'NOT FOUND'}: enforced FK on Quote_Req.Material")

print(
    "\nIf 'NOT FOUND' on the FK: Quote_Req.Material is just a free-text "
    "column as far as the database is concerned — a custom line's made-up "
    "ID can be written there safely, no schema risk. If 'FOUND': the "
    "custom-line write needs to target something other than Material, or "
    "this will hard-fail at insert time (better to know now than at 2pm "
    "with an engineer waiting)."
)

conn.close()