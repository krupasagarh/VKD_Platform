"""CSV download helpers for list pages."""
from __future__ import annotations

import csv
import io
from urllib.parse import urlencode

from fastapi.responses import Response

EXPORT_ROW_LIMIT = 100_000


def csv_response(filename: str, headers: list[str], rows: list[list]) -> Response:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerows(rows)
    # BOM helps Excel open UTF-8 names correctly on Windows.
    body = "\ufeff" + buf.getvalue()
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def export_url(path: str, **params) -> str:
    """Same filters as the current list, as a CSV download link."""
    clean = {k: str(v) for k, v in params.items() if v not in (None, "", [])}
    clean["format"] = "csv"
    query = urlencode(clean)
    return f"{path}?{query}" if query else path


def wants_csv(request) -> bool:
    return (request.query_params.get("format") or "").strip().lower() == "csv"
