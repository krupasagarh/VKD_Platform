"""Look up blank-plan STBs in Hathway dashboard dump, then live portal."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DASH = Path(r"c:\Users\A1\Downloads\TotalDashboardData_2992026142955.xls")
OUT = Path(r"c:\Users\A1\Desktop\AI agent\vk_digital_hub\vk_platform\scripts\hathway_blank_plan_check.json")

IDS = [
    "N70130838256", "N70130923132", "N70130929063", "N70130929188", "N70130929253",
    "N70130932695", "N70130934063", "N70142403743", "N70142553067", "N70150015744",
    "N70150026733", "N70150029190", "N70150030552", "N70150033382", "N70150034240",
    "N70150034802", "N70150034885", "N70150038688", "N70150049933", "N70152400357",
    "N70152403229", "N70152403567", "N70152404029", "N70152405083", "N70152415827",
    "N70152416080", "N70152416148", "N70152416809", "N70152458496", "N70174920879",
    "N70174970650", "N70175025652", "N70175037137", "N70175040941", "N70940350518",
    "N70940350542", "T403241252721",
]


def dashboard_index() -> dict[str, dict]:
    text = DASH.read_text(encoding="utf-8-sig", errors="replace")
    rows: dict[str, dict] = {}
    for line in text.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        rec = {
            "status": parts[1].strip().upper(),
            "stb": parts[2].strip().lstrip("'").upper(),
            "vc": parts[3].strip().lstrip("'").upper() if len(parts) > 3 else "",
            "phone": re.sub(r"\D", "", parts[4] if len(parts) > 4 else "")[-10:],
            "name": (parts[5].strip().lstrip("'") if len(parts) > 5 else ""),
        }
        if rec["stb"]:
            rows[rec["stb"]] = rec
        if rec["vc"]:
            rows.setdefault(rec["vc"], rec)
    return rows


def main() -> int:
    index = dashboard_index()
    dash = []
    for stb in IDS:
        rec = index.get(stb.upper())
        dash.append(
            {
                "id": stb,
                "dashboard_status": rec["status"] if rec else "NOT IN DASHBOARD",
                "dashboard_name": rec["name"] if rec else "",
                "dashboard_vc": rec["vc"] if rec else "",
            }
        )
        print(f"{stb:16} {(rec['status'] if rec else 'MISSING'):10} {(rec['name'] if rec else '')}")

    payload = {"dashboard": dash}
    live_ids = [row["id"] for row in dash if row["dashboard_status"] == "ACTIVE"]
    print("ACTIVE without master pack:", live_ids, file=sys.stderr)

    candidates = [
        Path(r"c:\Users\A1\Desktop\AI agent\vk_digital_hub\railtel_debugger\vk_agent"),
        Path(r"c:\Users\A1\Desktop\AI agent\vk_digital_hub\vk_agent"),
        Path(__file__).resolve().parents[2] / "railtel_debugger" / "vk_agent",
    ]
    agent = next((p for p in candidates if (p / "hathway_portal.py").is_file()), None)
    if agent is None:
        raise SystemExit("hathway_portal.py not found")
    sys.path.insert(0, str(agent))
    from hathway_portal import check_hathway_portal_batch  # noqa: E402

    # Live-check ACTIVE anomalies plus a couple of INACTIVE samples.
    sample_inactive = [row["id"] for row in dash if row["dashboard_status"] == "INACTIVE"][:3]
    check_ids = live_ids + sample_inactive
    print("portal check", check_ids, file=sys.stderr)
    result = check_hathway_portal_batch(check_ids)
    payload["portal"] = result
    OUT.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print("wrote", OUT)
    if isinstance(result, dict):
        print("portal success", result.get("success"), "error", result.get("error"))
        for row in result.get("results") or []:
            print(
                row.get("search_value"),
                row.get("hathway_tv_status") or row.get("error"),
                row.get("hathway_plan_name"),
                row.get("hathway_valid_upto") or row.get("expiry"),
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
