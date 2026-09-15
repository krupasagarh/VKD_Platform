"""Reset the admin login hash.

Changing VK_PLATFORM_PASSWORD in .env does not unlock an existing admin —
the password was copied into the agents table on first start.

Usage, from vk_platform:

    python scripts/reset_admin_password.py
    python scripts/reset_admin_password.py "new-password-here"

With no argument, the new password is VK_PLATFORM_PASSWORD from .env
(or vkdigital if that is unset). Restart the platform after it succeeds.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from app.auth import hash_password
from app.config import settings
from app.db import transaction
from app.money import now_iso


def main() -> int:
    new_password = (sys.argv[1] if len(sys.argv) > 1 else settings.password).strip()
    if not new_password:
        print("Give a non-empty password as the first argument, or set VK_PLATFORM_PASSWORD.")
        return 1

    with transaction() as conn:
        row = conn.execute(
            "SELECT id, username FROM agents WHERE username = 'admin' OR role = 'admin' "
            "ORDER BY CASE username WHEN 'admin' THEN 0 ELSE 1 END, id LIMIT 1"
        ).fetchone()
        if row is None:
            print("No admin agent exists. Start the platform once so it can create one.")
            return 1
        conn.execute(
            "UPDATE agents SET password_hash = ?, updated_at = ? WHERE id = ?",
            (hash_password(new_password), now_iso(), row["id"]),
        )

    print(f"Password reset for username '{row['username']}'. Sign in with that user.")
    print("Restart the platform if it is already running.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
