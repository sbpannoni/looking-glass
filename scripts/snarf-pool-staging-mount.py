#!/usr/bin/env python3
"""Mount a per-card rw staging area into the engine container (item C's
sanctioned write path).

RUN THIS ON SNARF:  python3 snarf-pool-staging-mount.py

Patches /ssdpool/coder-engine/pipeline/dispatch_task.py (backing it up first)
and creates /ssdpool/pool-staging. Idempotent: re-running is a no-op.

Why: the primary checkout is mounted READ-ONLY into the container, so a card
that legitimately needs to ADD a reference genome to database/collab_refs/ has
nowhere to put it, and the only remaining route is to block and work by hand
outside the container -- exactly what t_d17fef80 did. This is the replacement.
See docs/PIPELINE-VERIFICATION.md item C in the looking-glass repo.

After this lands, set darkhelix.enforce_block: true on CT112 and restart
looking-glass.service. Not before -- enforcement without this rebuilds the
hard wall.
"""
import ast
import time
from pathlib import Path

TARGET = Path("/ssdpool/coder-engine/pipeline/dispatch_task.py")
STAGING_ROOT = Path("/ssdpool/pool-staging")

CONST_ANCHOR = 'IMAGE = "coder-engine:phase2"'
CONST_ADD = '''

# Per-card rw staging for proposed shared-pool additions. See the mount in
# dispatch() for why this exists and why it lives outside the repo.
POOL_STAGING_ROOT = "/ssdpool/pool-staging"'''

MOUNT_ANCHOR = '''        if test_command:
            docker_cmd += ["-e", f"TEST_COMMAND={test_command}"]
'''
MOUNT_ADD = '''        # SANCTIONED WRITE PATH into the shared reference pool.
        #
        # The primary checkout is mounted READ-ONLY above, which is right --
        # but it leaves a card that legitimately needs to ADD a reference
        # genome to database/collab_refs/ with nowhere to put it. Adding
        # references is a real, recurring operation, and with the pool
        # read-only the only remaining route is to block and then work by hand
        # outside the container -- which is exactly what t_d17fef80 did.
        #
        # So: a per-card rw area OUTSIDE the repo. The worker writes proposed
        # reference files here and says so in its summary; promotion into the
        # pool is a separate reviewed step on the HUD
        # (POST /api/darkhelix/promote-refs), which refuses to overwrite
        # unless told to and records the change in the pool-manifest ledger.
        # The pool itself stays read-only from in here.
        #
        # Outside the repo on purpose: inside the checkout it would show up in
        # `git status`, and inside collab_refs/ it would register in the pool
        # manifest as an added file -- conflating a PROPOSAL with a mutation.
        staging = Path(POOL_STAGING_ROOT) / task_id
        staging.mkdir(parents=True, exist_ok=True)
        docker_cmd += ["-v", f"{staging}:{staging}",
                       "-e", f"POOL_STAGING={staging}"]

'''


def main() -> int:
    src = TARGET.read_text()
    if "POOL_STAGING_ROOT" in src:
        print("already patched; nothing to do")
    else:
        if src.count(CONST_ANCHOR) != 1:
            print(f"ABORT: expected exactly one {CONST_ANCHOR!r}")
            return 1
        if src.count(MOUNT_ANCHOR) != 1:
            print("ABORT: could not find the test_command block to insert before")
            return 1
        backup = TARGET.with_name(
            TARGET.name + ".bak-poolstaging-" + time.strftime("%Y%m%d-%H%M%S"))
        backup.write_text(src)
        out = src.replace(CONST_ANCHOR, CONST_ANCHOR + CONST_ADD, 1)
        out = out.replace(MOUNT_ANCHOR, MOUNT_ADD + MOUNT_ANCHOR, 1)
        ast.parse(out)                      # refuse to write a broken file
        TARGET.write_text(out)
        print(f"patched {TARGET} (backup: {backup.name})")

    STAGING_ROOT.mkdir(parents=True, exist_ok=True)
    print(f"staging root ready: {STAGING_ROOT}")
    print("\nNo engine restart needed -- darkhelix-engine.py shells a fresh")
    print("dispatch_task.py per attempt, so this takes effect on the next dispatch.")
    print("\nNext: set darkhelix.enforce_block: true on CT112 and restart the HUD.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
