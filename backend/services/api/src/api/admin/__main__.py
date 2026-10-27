"""Grant or revoke the admin role from the command line.

    python -m api.admin grant-admin someone@example.com
    python -m api.admin revoke-admin someone@example.com

The first admin is created this way, never through the UI. The user must have signed in once.
"""

import sys

from sqlalchemy import func, select

from core.enums import Role
from db.models import User
from db.session import session_scope


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("grant-admin", "revoke-admin"):
        print(__doc__)
        return 2
    action, email = argv
    with session_scope() as tx:
        users = list(tx.execute(select(User).where(func.lower(User.email) == email.lower())).scalars())
        if len(users) != 1:
            print(f"Expected one user with email {email}, found {len(users)}. They must sign in once first.")
            return 1
        if action == "revoke-admin":
            admins = tx.execute(select(User.id).where(User.role == Role.admin.value).with_for_update()).all()
            if len(admins) <= 1 and users[0].role == Role.admin:
                print("Refusing: this is the last admin.")
                return 1
        users[0].role = (Role.admin if action == "grant-admin" else Role.editor).value
        print(f"{email} is now {users[0].role}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
