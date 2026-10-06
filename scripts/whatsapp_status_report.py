import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.db import init_db, connection
init_db()
with connection() as conn:
    rows = conn.execute(
        "SELECT COALESCE(NULLIF(TRIM(whatsapp_status), ''), 'unchecked') AS st, COUNT(*) AS n "
        "FROM customers WHERE phone IS NOT NULL AND TRIM(phone) != '' GROUP BY st"
    ).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM customers WHERE phone IS NOT NULL AND TRIM(phone) != ''"
    ).fetchone()["n"]
print("total_with_phone", total)
for r in rows:
    print(r["st"], r["n"])
