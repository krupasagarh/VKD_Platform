"""Quick diagnostic for complaint WhatsApp settings."""
from app.config import settings
from app.db import connection, get_setting, init_db

init_db()
with connection() as conn:
    group = get_setting(conn, "complaint_whatsapp_group", "")
    print("DB group:", repr(group))
    print("ENV group:", repr(settings.complaint_whatsapp_group))
    print("WHATSAPP_WEB_AUTO_SEND:", settings.whatsapp_web_auto_send)
    print("notify_group:", settings.complaint_whatsapp_notify_group)
    print("notify_agent:", settings.complaint_whatsapp_notify_agent)
    print("notify_customer:", settings.complaint_whatsapp_notify_customer)
    print()
    print("Agents:")
    for r in conn.execute("SELECT id, name, phone FROM agents ORDER BY id"):
        print(f"  {r['id']}: {r['name']} phone={r['phone']!r}")
    print()
    print("Recent complaints:")
    for r in conn.execute(
        "SELECT id, title, assigned_to, assigned_agent_id, created_at "
        "FROM complaints ORDER BY id DESC LIMIT 5"
    ):
        print(
            f"  #{r['id']} {r['title']!r} -> {r['assigned_to']!r} "
            f"(agent_id={r['assigned_agent_id']}) {r['created_at']}"
        )
    print()
    print("Recent WhatsApp / complaint activity:")
    for r in conn.execute(
        "SELECT at, kind, message FROM activity_log "
        "WHERE kind LIKE '%whatsapp%' OR message LIKE '%Complaint #%' "
        "ORDER BY id DESC LIMIT 20"
    ):
        print(f"  {r['at']} [{r['kind']}] {r['message']}")
