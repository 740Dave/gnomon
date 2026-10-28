"""Private JSON state under the mail state dir (token, config): 0700 dir, 0600 files, atomic replace.

``load`` REFUSES a file that is a symlink, not a regular file, not owned by this uid, or readable by group
or other (a loud ``TokenPermissionError``, never a silent read). Boundary, stated: this is the same OS user
as the agents, so 0600 does not hide the file from an agent running as this user (g29(b): hygiene, not a
security boundary); it keeps it out of other users, backups with loose perms and accidental ``ls``.
"""

from __future__ import annotations

import json
import os
import stat
from typing import Any, Dict, Optional

from .errors import MailError, TokenPermissionError

MAX_BYTES = 256 * 1024


def ensure_private_dir(path: str) -> None:
    """Create ``path`` (and any missing ancestor UNDER an existing one) with mode 0700, and REFUSE (never fix up) a
    directory that is not private. No chmod is ever called: the modes are set at creation (os.mkdir with an explicit
    mode, which a umask can only narrow), so a loose directory is a loud error, not something silently repaired."""
    path = os.path.abspath(path)
    missing = []
    probe = path
    while not os.path.lexists(probe):
        missing.append(probe)
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    for d in reversed(missing):
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            pass
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise MailError(f"mail state dir {path!r} is not a real directory")
    if st.st_uid != os.getuid():
        raise MailError(f"mail state dir {path!r} is not owned by this user")
    if st.st_mode & 0o077:
        raise MailError(f"mail state dir {path!r} has mode {stat.S_IMODE(st.st_mode):04o} (group/other access): "
                        "refusing to use it; create it 0700 (it is never chmod-ed for you)")


def write_private_json(path: str, obj: Dict[str, Any]) -> None:
    d = os.path.dirname(path)
    ensure_private_dir(d)
    data = json.dumps(obj, indent=2, sort_keys=True).encode("utf-8")
    tmp = os.path.join(d, ".tmp-" + os.urandom(8).hex())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)  # born 0600
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_private_json(path: str) -> Optional[Dict[str, Any]]:
    """None when the file does not exist; the parsed object otherwise; raises on a non-private file."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise TokenPermissionError(f"{path!r} is not a regular file (symlink or special file refused)")
    if st.st_uid != os.getuid():
        raise TokenPermissionError(f"{path!r} is not owned by this user")
    if st.st_mode & 0o077:
        raise TokenPermissionError(f"{path!r} is readable by group/other (mode {stat.S_IMODE(st.st_mode):04o}); "
                                   "chmod 600 it or disconnect and reconnect")
    if st.st_size > MAX_BYTES:
        raise MailError(f"{path!r} is unreasonably large")
    with open(path, "rb") as fh:
        try:
            obj = json.loads(fh.read().decode("utf-8"))
        except ValueError as exc:
            raise MailError(f"{path!r} is not valid JSON: {exc}") from None
    if not isinstance(obj, dict):
        raise MailError(f"{path!r} must hold a JSON object")
    return obj


class TokenStore:
    FILENAME = "token.json"

    def __init__(self, directory: str) -> None:
        self.dir = directory
        self.path = os.path.join(directory, self.FILENAME)

    def save(self, token: Dict[str, Any]) -> None:
        write_private_json(self.path, token)

    def load(self) -> Optional[Dict[str, Any]]:
        return read_private_json(self.path)

    def delete(self) -> bool:
        try:
            os.unlink(self.path)
            return True
        except FileNotFoundError:
            return False
