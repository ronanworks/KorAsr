"""Local-only bootstrap and account maintenance CLI.

Examples:
    python -m backend.admin create-user --username admin --admin
    python -m backend.admin create-user --username student01
"""

import argparse
import getpass

from sqlmodel import Session as DBSession, select

from backend.accounts import AccountConflict, change_user, create_user
from backend.storage.database import engine, init_db
from backend.storage.models import User


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage local korAsr accounts")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-user")
    create.add_argument("--username", required=True)
    create.add_argument("--admin", action="store_true")
    disable = commands.add_parser("disable-user")
    disable.add_argument("--username", required=True)
    reset = commands.add_parser("reset-password")
    reset.add_argument("--username", required=True)
    args = parser.parse_args()

    init_db()
    with DBSession(engine) as db:
        try:
            if args.command == "create-user":
                first = getpass.getpass("Password (12+ characters): ")
                second = getpass.getpass("Repeat password: ")
                if first != second:
                    parser.error("Passwords do not match")
                user = create_user(db, args.username, first, args.admin)
                print(f"Created {'administrator' if user.is_admin else 'user'}: {user.username}")
            else:
                user = db.exec(select(User).where(User.username == args.username.strip().lower())).first()
                if user is None:
                    parser.error("User not found")
                if args.command == "disable-user":
                    change_user(db, user, is_active=False)
                    print(f"Disabled: {user.username}")
                else:
                    first = getpass.getpass("New password (12+ characters): ")
                    second = getpass.getpass("Repeat new password: ")
                    if first != second:
                        parser.error("Passwords do not match")
                    change_user(db, user, password=first)
                    print(f"Reset password: {user.username}")
        except (ValueError, AccountConflict) as exc:
            parser.error(str(exc))


if __name__ == "__main__":
    main()
