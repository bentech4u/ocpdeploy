"""ocpdeploy command line (also used by systemd timers; no web server needed).

  ocpdeployctl user list                       list console accounts (names only)
  ocpdeployctl user set <name>                 create an account or set its password
  ocpdeployctl user reset <name>               change the password of an existing account
  ocpdeployctl user delete <name>              remove an account
      --password-stdin                         read the password from stdin instead of prompting
  ocpdeployctl backup <cluster>                take an etcd backup now
"""
import getpass
import sys


def _password(argv, username: str) -> str:
    from . import auth
    if "--password-stdin" in argv:
        pw = sys.stdin.readline().rstrip("\n")
    else:
        print(f"Password rules: at least 12 characters, three of lowercase/uppercase/digit/symbol, not containing the username.")
        pw = getpass.getpass(f"New password for {username}: ")
        if getpass.getpass("Repeat password: ") != pw:
            raise ValueError("passwords do not match")
    probs = auth.password_problems(username, pw)
    if probs:
        raise ValueError("password too weak: " + "; ".join(probs))
    return pw


def user_cmd(argv) -> int:
    from . import auth
    args = [a for a in argv if not a.startswith("--")]
    if not args or args[0] not in ("list", "set", "reset", "delete") or (args[0] != "list" and len(args) < 2):
        print(__doc__)
        return 2
    action = args[0]
    try:
        if action == "list":
            for u in auth.list_users():
                print(u)
            if not auth.list_users():
                print("(no accounts; the web UI asks for one on first visit)", file=sys.stderr)
            return 0
        name = args[1]
        if action == "delete":
            auth.delete_user(name)
            print(f"deleted {name}; their sessions end immediately")
            return 0
        if action == "reset" and name not in auth.list_users():
            raise KeyError(name)
        if not auth.valid_username(name):
            raise ValueError("username: 2-32 characters, letters, digits, dot, dash, underscore")
        auth.set_password(name, _password(argv, name), must_exist=True if action == "reset" else None)
        print(f"password {'changed' if action == 'reset' else 'set'} for {name}; existing sessions of {name} are logged out")
        return 0
    except KeyError as ex:
        print(f"no such account: {ex.args[0]}", file=sys.stderr)
        return 1
    except ValueError as ex:
        print(str(ex), file=sys.stderr)
        return 1


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if argv and argv[0] == "user":
        return user_cmd(argv[1:])
    if len(argv) < 2 or argv[0] not in ("backup",):
        print(__doc__)
        return 2
    from .store import get_store
    from . import jobs
    store = get_store(argv[1])
    spec = store.load()
    if argv[0] == "backup":
        from .services import backup
        return jobs.run_sync(store, "etcd-backup", lambda ctx: backup.job_backup(ctx, store, spec))
    return 2


if __name__ == "__main__":
    sys.exit(main())
