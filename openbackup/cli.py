"""Command line: setup, user administration and service entry points."""

from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import func, select


def _prompt_password(username: str) -> str:
    from .auth.passwords import PasswordPolicyError, check_policy

    while True:
        pw = getpass.getpass(f"Password for {username}: ")
        try:
            check_policy(pw, username)
        except PasswordPolicyError as e:
            print(e, file=sys.stderr)
            continue
        if getpass.getpass("Repeat password: ") != pw:
            print("Passwords do not match", file=sys.stderr)
            continue
        return pw


def cmd_init(_args: argparse.Namespace) -> int:
    from .auth.secrets import ensure_key_file
    from .config import get_settings
    from .db import init_db

    s = get_settings()
    ensure_key_file(s.secret_key_file)
    s.data_dir.mkdir(parents=True, exist_ok=True)
    init_db()
    print(f"Secret key: {s.secret_key_file}")
    print(f"Database:   {s.db_url}")
    return 0


def cmd_db_upgrade(_args: argparse.Namespace) -> int:
    from .db import init_db

    init_db()
    print("Database schema is up to date")
    return 0


def cmd_user_create(args: argparse.Namespace) -> int:
    from .auth.passwords import hash_password
    from .db import init_db, session_scope
    from .db.models import Role, User

    init_db()
    password = _prompt_password(args.username)
    with session_scope() as db:
        if db.scalar(select(User).where(func.lower(User.username) == args.username.lower())):
            print(f"User {args.username} already exists", file=sys.stderr)
            return 1
        db.add(
            User(
                username=args.username,
                password_hash=hash_password(password),
                role=Role.admin if args.admin else Role(args.role),
                must_change_password=not args.no_change,
            )
        )
    print(f"Created {args.username}")
    return 0


def cmd_user_reset(args: argparse.Namespace) -> int:
    from .auth.passwords import hash_password
    from .auth.service import revoke_user_sessions
    from .db import session_scope
    from .db.models import User

    password = _prompt_password(args.username)
    with session_scope() as db:
        u = db.scalar(select(User).where(func.lower(User.username) == args.username.lower()))
        if u is None:
            print(f"No such user {args.username}", file=sys.stderr)
            return 1
        u.password_hash = hash_password(password)
        u.must_change_password = True
        u.locked_until = None
        u.failed_logins = 0
        u.is_active = True
        revoke_user_sessions(db, u.id)
    print(f"Password reset for {args.username}; they must change it at next login")
    return 0


def cmd_user_list(_args: argparse.Namespace) -> int:
    from .db import session_scope
    from .db.models import User

    with session_scope() as db:
        for u in db.scalars(select(User).order_by(User.username)):
            state = "active" if u.is_active else "disabled"
            print(f"{u.username:24} {u.role.value:9} {state}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    kwargs = {}
    if args.certfile:
        kwargs = {"ssl_certfile": args.certfile, "ssl_keyfile": args.keyfile}
    uvicorn.run(
        "openbackup.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        proxy_headers=False,
        server_header=False,
        **kwargs,
    )
    return 0


def cmd_worker(_args: argparse.Namespace) -> int:
    from .worker.main import run

    run()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="openbackup")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the secret key and database").set_defaults(func=cmd_init)
    dbp = sub.add_parser("db", help="database maintenance").add_subparsers(dest="dcmd",
                                                                           required=True)
    dbp.add_parser("upgrade", help="apply schema migrations").set_defaults(func=cmd_db_upgrade)

    user = sub.add_parser("user", help="manage web users").add_subparsers(dest="ucmd",
                                                                          required=True)
    c = user.add_parser("create")
    c.add_argument("username")
    c.add_argument("--role", choices=["viewer", "operator", "admin"], default="viewer")
    c.add_argument("--admin", action="store_true")
    c.add_argument("--no-change", action="store_true",
                   help="do not force a password change at first login")
    c.set_defaults(func=cmd_user_create)
    r = user.add_parser("reset-password")
    r.add_argument("username")
    r.set_defaults(func=cmd_user_reset)
    user.add_parser("list").set_defaults(func=cmd_user_list)

    s = sub.add_parser("serve", help="run the web server")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8443)
    s.add_argument("--certfile")
    s.add_argument("--keyfile")
    s.set_defaults(func=cmd_serve)

    sub.add_parser("worker", help="run the job worker").set_defaults(func=cmd_worker)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
