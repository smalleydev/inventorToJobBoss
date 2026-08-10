from db import get_connection

QUOTE_GUID = "{a58457bd-31a2-4ebd-9bc4-50872e1abf08}"

conn = get_connection()
cursor = conn.cursor()

cursor.execute("DELETE FROM Quote_Req_Qty WHERE Quote = ?", QUOTE_GUID)
print(f"Quote_Req_Qty: {cursor.rowcount} deleted")

cursor.execute("DELETE FROM Quote_Req WHERE Quote = ?", QUOTE_GUID)
print(f"Quote_Req: {cursor.rowcount} deleted")

cursor.execute("DELETE FROM Quote_Qty WHERE Quote = ?", QUOTE_GUID)
print(f"Quote_Qty: {cursor.rowcount} deleted")

cursor.execute("DELETE FROM Quote WHERE Quote = ?", QUOTE_GUID)
print(f"Quote: {cursor.rowcount} deleted")

conn.commit()
print("Committed.")

conn.close()