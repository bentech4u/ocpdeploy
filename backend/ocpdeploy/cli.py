"""Command line entry points used by systemd timers (no web server needed).

    python -m ocpdeploy.cli backup <cluster>
"""
import sys

from .store import get_store
from . import jobs


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) < 2 or argv[0] not in ("backup",):
        print(__doc__)
        return 2
    store = get_store(argv[1])
    spec = store.load()
    if argv[0] == "backup":
        from .services import backup
        return jobs.run_sync(store, "etcd-backup", lambda ctx: backup.job_backup(ctx, store, spec))
    return 2


if __name__ == "__main__":
    sys.exit(main())
