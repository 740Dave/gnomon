"""The filesystem-dependent half of g73-amendment-4 rule 2 (the pure half is manifest.check_argv_shape).

The pin is a hash of EVERY file under the app root, so the rest of the rule is about what may sit OUTSIDE it and what
the argv may name:

* ROUND 3d (R3C-F1, the ONE strict shape, architect's ruling): every launched argv is exactly
  ``[interpreter, <host hardening flags>, script, <args>...]``. The interpreter is on the ALLOW-LIST by the REALPATH
  basename AND its realpath lies outside the user's home, outside the app root and outside every scratch directory (a
  system, framework or Homebrew interpreter, or a venv symlink that resolves to one); NEVER a file inside the app
  root, never matched by name alone, and no root binary is ever argv[0]. Between argv[0] and the script there are ONLY
  the host's own flags (python: -I -B; zsh: -f; bash: --norc --noprofile) and the manifest supplies NO token there
  (no ``-``, no ``+``, nothing). The script is a literal root-relative manifest token naming a regular file in the
  pinned root; the host emits it to the exec as an ABSOLUTE path, so an interpreter can never read it as an option or
  a subcommand word (``+O``, ``inspect``). {path:} host values, port args and manifest args come ONLY after the script.
* a path the app WRITES must lie outside the root, must not be the root and must not contain it (the whole tree is
  pinned, so data written inside it would invalidate the pin at the next start; and code dropped into a "data" path
  inside the root would otherwise be the R2-F2 H1 bypass);
* every one of these is re-checked at EVERY start and on every verification, because a symlink or a settings value
  can change after confirm;
* ROUND 3c (R3-F2/F3/F11, one defect: guards scoped by location): the rule is enforced ONCE, on the FINAL argv, by
  :func:`final_check`, called only from ``preflight.exec_argv`` (the single choke point every launched argv passes
  through: start, action, presence check) and, structurally, at propose / every verify. No deny-list.

Pure functions over paths: nothing here runs a process.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import pwd
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .manifest import PLACEHOLDER_RE, Manifest


def identity(path: str) -> Optional[Tuple[int, int]]:
    """The file identity ``(st_dev, st_ino)`` of ``path``, or None when it does not exist."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def inside(p: str, root: str) -> bool:
    """True when ``p`` is ``root`` or lies under it, decided by FILE IDENTITY, never by string (R3D-F1, g73-amendment-7).

    ``p`` is resolved with realpath (symlinks), then its ancestor chain (itself first) is walked and each existing entry's
    ``(st_dev, st_ino)`` is compared with the reference directory's. On macOS one directory has many spellings (case
    variants on case-insensitive APFS, the ``/System/Volumes/Data`` firmlink, ``/private`` aliases, Unicode
    normalisations) and they all stat to the same identity, so none of them can pass as 'outside'. A ``p`` that does not
    exist yet is judged by its nearest existing ancestor (a new file under the root is inside the root). The ONE helper
    for every location decision in appshost. If the reference does not exist nothing can be inside it by identity: the
    string compare of the two realpaths is the (stricter-than-nothing) fallback."""
    ref = identity(root)
    cur = os.path.realpath(p)
    if ref is None:
        rr = os.path.realpath(root)
        return cur == rr or cur.startswith(rr.rstrip(os.sep) + os.sep)
    while True:
        if identity(cur) == ref:
            return True
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent


#: first four bytes of a Mach-O executable: thin 32/64-bit, both byte orders, and universal (fat) binaries, both orders
#: and the 64-bit fat variant (R3D-F2)
_MACHO_MAGICS = frozenset(bytes.fromhex(h) for h in (
    "feedface", "feedfacf", "cefaedfe", "cffaedfe", "cafebabe", "bebafeca", "cafebabf", "bfbafeca"))


_THIN = {bytes.fromhex("feedface"): (">", False), bytes.fromhex("feedfacf"): (">", True),
         bytes.fromhex("cefaedfe"): ("<", False), bytes.fromhex("cffaedfe"): ("<", True)}
_FAT = {bytes.fromhex("cafebabe"): (">", False), bytes.fromhex("bebafeca"): ("<", False),
        bytes.fromhex("cafebabf"): (">", True), bytes.fromhex("bfbafeca"): ("<", True)}
#: CPU types of a Mach-O this host could run an interpreter from (x86, x86_64, arm, arm64, arm64_32, ppc kept out)
_CPUS = {7, 0x01000007, 12, 0x0100000C, 0x0200000C}
MH_EXECUTE = 2
_MAX_FAT_ARCHS = 16
_MAX_NCMDS = 4096


def _thin_plausible(head: bytes, size_left: int) -> bool:
    """A thin Mach-O header that could be a real executable: known magic, a CPU we run, ``MH_EXECUTE``, 1..4096 load
    commands whose total size fits both ``ncmds * 8`` (a load command is at least 8 bytes) and the file."""
    fmt = _THIN.get(head[:4])
    if fmt is None:
        return False
    end, is64 = fmt
    hdr = 32 if is64 else 28
    if len(head) < hdr or size_left < hdr:
        return False
    cputype, _sub, filetype, ncmds, sizeofcmds = struct.unpack(end + "iiIII", head[4:24])
    return (cputype in _CPUS and filetype == MH_EXECUTE and 1 <= ncmds <= _MAX_NCMDS
            and ncmds * 8 <= sizeofcmds <= size_left - hdr)


def is_macho(path: str) -> bool:
    """True when the regular file at ``path`` is a plausible Mach-O executable (round 3f: more than the magic).
    A thin file needs a sane header (:func:`_thin_plausible`); a universal file needs 1..16 arch entries each naming a
    CPU we run, a slice inside the file, and a plausible thin header at the slice. So a text file that merely starts with
    ``cafebabe`` (``nfat_arch`` becomes a huge number) or any other magic prefix is refused here, not left to execve.
    Opened non-blocking and checked on the descriptor (fsafe). Never raises."""
    from . import fsafe
    try:
        with fsafe.open_regular(path) as fh:
            size = os.fstat(fh.fileno()).st_size
            head = fh.read(32)
            if head[:4] in _THIN:
                return _thin_plausible(head, size)
            fmt = _FAT.get(head[:4])
            if fmt is None or len(head) < 8:
                return False
            end, is64 = fmt
            n = struct.unpack(end + "I", head[4:8])[0]
            if not 1 <= n <= _MAX_FAT_ARCHS:
                return False
            ent = 32 if is64 else 20
            fh.seek(8)
            raw = fh.read(n * ent)
            if len(raw) != n * ent:
                return False
            for i in range(n):
                e = raw[i * ent:(i + 1) * ent]
                if is64:
                    cpu, _s, off, sz = struct.unpack(end + "iiQQ", e[:24])
                else:
                    cpu, _s, off, sz = struct.unpack(end + "iiII", e[:16])
                if cpu not in _CPUS or off < 8 + n * ent or sz < 28 or off + sz > size:
                    return False
                fh.seek(off)
                if not _thin_plausible(fh.read(32), sz):
                    return False
            return True
    except (OSError, struct.error):
        return False


def user_homes() -> List[str]:
    """The user's home as the environment says and as the account database says (both realpath'd)."""
    out: List[str] = []
    cands = [os.path.expanduser("~")]
    try:
        cands.append(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError:
        pass
    for c in cands:
        if c and c != "/":
            rp = os.path.realpath(c)
            if rp not in out and rp != "/":
                out.append(rp)
    return out


#: ROUND 3c: the ALLOW-LIST of interpreter kinds, classified by the REALPATH basename of argv[0]. Every kind here takes
#: its script as the FIRST operand (argv[1]; for Python argv[3], after the host's ``-I -B``) and treats everything after
#: it as arguments TO the script. A new kind is added HERE and in the checklist (g73-amendment-5 rule 12), never by
#: listing what to refuse. ``python*`` covers python, python3, python3.13, python3-intel64, python3.13-intel64.
_INTERPRETERS = (
    ("python", re.compile(r"python", re.I)),                       # prefix: every python* (R3-F3)
    ("shell", re.compile(r"(?:sh|bash|dash|zsh|ksh)", re.I)),
    ("node", re.compile(r"node(?:js)?", re.I)),
    ("perl", re.compile(r"perl[\d.]*", re.I)),
    ("ruby", re.compile(r"ruby[\d.]*", re.I)),
)

#: directories any local user or process can create files in: never a place a trusted interpreter lives
_SCRATCH_DIRS = ("/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp", "/var/folders", "/private/var/folders",
                 "/Users/Shared", "/Volumes")


class RuleViolation(Exception):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


def physical(cwd: str, tok: str) -> str:
    """The PHYSICAL path the kernel will use for ``tok``: ``os.path.realpath`` on the ORIGINAL string (relative tokens are
    joined to ``cwd`` first). NEVER normalised lexically (R3E-F1, g73-amendment-8): ``normpath`` collapses ``S/..`` as if
    ``S`` were a directory, while the kernel resolves the symlink ``S`` first, so a lexical collapse judges one file and
    the kernel runs another. Every path judged in a security decision comes from here, and the judged string is the one
    that is exec'd."""
    return os.path.realpath(tok if os.path.isabs(tok) else os.path.join(cwd, tok))


def exec_path(argv0: str, cwd: str) -> str:
    """The path string to EXEC for the manifest's ``argv0``: the realpath that was judged (R3E-F1)."""
    return physical(cwd, argv0)


def _plain_absolute(path: str) -> bool:
    """True for an absolute path with no '.' or '..' component and no empty component ('//', trailing '/')."""
    if not path or not path.startswith("/") or "\x00" in path:
        return False
    return all(c not in ("", ".", "..") for c in path[1:].split("/"))


class Judged:
    """What :func:`operand_rule` judged, as file identities, so the wrapper can refuse if either file changed between the
    judgement and the exec (R3E-F1: re-verify ``(st_dev, st_ino)`` of the resolved interpreter and script immediately
    before exec). ``exe`` is the path string to exec; ``script`` the absolute script operand (both realpaths)."""

    def __init__(self, exe: str, script: Optional[str], literal: Optional[str] = None) -> None:
        self.exe = exe
        self.script = script
        self.literal = literal                 # the manifest's argv[0] spelling (R3F-F1: may be exec'd, see exec_target)
        self.exe_id = identity(exe)
        self.script_id = identity(script) if script is not None else None
        self._digest: Optional[str] = None     # set once by :meth:`bind` (R3H-F2); None only for a hand-built Judged in a test

    def _digest_of(self, argv) -> str:
        """sha256 over the argv AND every field of the judgement (R3H-F2): the exact argv that was judged, the paths, the
        manifest literal and the file identities. Anything changed after :meth:`bind` changes this value."""
        body = json.dumps([[str(a) for a in argv], self.exe, self.script, self.literal,
                           list(self.exe_id) if self.exe_id is not None else None,
                           list(self.script_id) if self.script_id is not None else None],
                          ensure_ascii=True, separators=(",", ":"))
        return hashlib.sha256(body.encode("ascii")).hexdigest()

    def bind(self, argv) -> "Judged":
        """Bind the judged argv into this judgement, once (called by :func:`final_check` on the argv it judged). A second
        call is refused: a bound judgement is never re-pointed at another argv."""
        if self._digest is not None:
            raise RuleViolation("exec", "this judgement is already bound to an argv (R3H-F2)")
        self._digest = self._digest_of(argv)
        return self

    def verify(self, argv=None) -> None:
        """Refuse unless the argv is the one that was judged AND the interpreter and script are the judged files.
        A bound judgement (every one :func:`final_check` returns) REQUIRES ``argv`` and re-computes its digest (R3H-F2)."""
        if self._digest is not None:
            if argv is None:
                raise RuleViolation("exec", "verify() needs the argv that is about to run (R3H-F2); refusing")
            if self._digest_of(argv) != self._digest:
                raise RuleViolation("exec", "the argv (or the judgement) changed since it was judged; refusing to run "
                                    "it (R3H-F2, g73-amendment-10)")
        if self.exe_id is None or identity(self.exe) != self.exe_id or os.path.realpath(self.exe) != self.exe:
            raise RuleViolation("exec", f"the interpreter {self.exe} is not the file that was judged (it changed or "
                                "moved since the check); refusing to run it (g73-amendment-8)")
        if self.script is not None and (self.script_id is None or identity(self.script) != self.script_id
                                        or os.path.realpath(self.script) != self.script):
            raise RuleViolation("exec", f"the script {self.script} is not the file that was judged (it changed or moved "
                                "since the check); refusing to run it (g73-amendment-8)")

    def exec_target(self) -> str:
        """The path string to hand to execve (R3F-F1, architect ruling). macOS Python derives sys.prefix and the venv's
        pyvenv.cfg lookup from the path given to execve (the kernel's executable_path), NOT from argv[0], so exec of the
        realpath makes a venv symlink run as its target interpreter without the venv. So the LITERAL manifest path is exec'd
        when ALL hold: (a) it is absolute, (b) it has no '.' or '..' component and no '//', (c) ``os.stat(literal)``
        (following symlinks) has the same (st_dev, st_ino) as the judged realpath, checked now. Otherwise the judged
        realpath. Call it immediately after :meth:`verify`. The literal is then resolved by the kernel through exactly the
        symlinks realpath resolved (no '..' to collapse differently); a swap in between makes (c) fail or is the stated
        same-uid TOCTOU class (R3C-F2/F10), never a different judged file."""
        lit = self.literal
        if lit is not None and _plain_absolute(lit) and self.exe_id is not None and identity(lit) == self.exe_id:
            return lit
        return self.exe


def interpreter_kind(argv0: str, cwd: str) -> Optional[str]:
    """The allow-listed interpreter kind of the program ``argv0`` resolves to, classified by the REALPATH basename (a
    symlink called ``app`` to python3.13-intel64 is a python), or None."""
    base = os.path.basename(physical(cwd, argv0))
    for kind, rx in _INTERPRETERS:
        if rx.match(base) if kind == "python" else rx.fullmatch(base):
            return kind
    return None


def host_flags(argv0: str, cwd: str) -> Tuple[str, ...]:
    """The flags the HOST (never the manifest) inserts between the interpreter and its script, by the REALPATH
    basename: Python -I -B (isolated, no .pyc); zsh -f (zsh otherwise sources ``$HOME/.zshenv`` for a script, R3C-F5);
    bash --norc --noprofile. sh, dash, ksh, node, perl and ruby take none: a non-interactive sh/dash/ksh reads no
    startup file, and the env is closed (no ENV, BASH_ENV, NODE_OPTIONS, PERL5OPT, RUBYOPT; see preflight.build_env)."""
    kind = interpreter_kind(argv0, cwd)
    if kind == "python":
        return ("-I", "-B")
    if kind == "shell":
        base = os.path.basename(physical(cwd, argv0)).lower()
        if base == "zsh":
            return ("-f",)
        if base == "bash":
            return ("--norc", "--noprofile")
    return ()


def python_like(argv0: str, cwd: str) -> bool:
    """True when the host must run this program isolated (-I -B): the file it resolves to starts with ``python``
    (python3-intel64 and python3.13-intel64 included; R3-F3)."""
    return interpreter_kind(argv0, cwd) == "python"


def harden_argv(argv: List[str], cwd: str) -> List[str]:
    """Build the host's argv from the substituted manifest argv: insert the host flags after the interpreter (g73-amendment-4
    rule 3 and R3C-F5) and emit the script operand as an ABSOLUTE path (R3C-F1: an absolute path is never an option or a
    subcommand word to any interpreter). Idempotent. A token that starts ``-``, ``+`` or ``~`` is left as it is for
    :func:`operand_rule` to refuse.

    -I (isolated) ignores PYTHON* environment variables, INCLUDING PYTHONDONTWRITEBYTECODE, so the env var cannot stop
    an -I interpreter writing .pyc; -B (the flag form) does. The env var stays set for a Python started INDIRECTLY."""
    if not argv:
        return []
    flags = list(host_flags(argv[0], cwd))
    out = list(argv)
    if flags and out[1:1 + len(flags)] != flags:
        out = [out[0], *flags, *out[1:]]
    i = 1 + len(flags)
    if len(out) > i and out[i] and not out[i].startswith(("-", "+", "~")) and not PLACEHOLDER_RE.search(out[i]):
        out[i] = physical(cwd, out[i])
    return out


def check_argv(where: str, argv: Tuple[str, ...], cwd: str, root: str, homes: Iterable[str]) -> None:
    """Raise :class:`RuleViolation` when ``argv`` breaks rule 2 against this app root. ``argv`` is the manifest's
    UNSUBSTITUTED argv (argv[0] and argv[1] carry no placeholder: the manifest parse guarantees it)."""
    a0 = argv[0]
    rp0 = physical(cwd, a0)
    if not os.path.exists(rp0):
        raise RuleViolation(f"{where}[0]", f"{a0!r} does not exist")
    if not os.path.isfile(rp0):
        raise RuleViolation(f"{where}[0]", f"{a0!r} is not a file")
    if not os.access(rp0, os.X_OK):
        raise RuleViolation(f"{where}[0]", f"{a0!r} is not executable")
    hard = harden_argv(list(argv), cwd)
    operand_rule(hard, raw=tuple(argv), n_fixed=len(hard), cwd=cwd, root=root, where=where, homes=homes)


def final_check(argv: List[str], *, raw: Tuple[str, ...], n_fixed: int, cwd: str, root: str, where: str) -> "Judged":
    """THE one final check (round 3c): the ENTRY the choke point (``preflight.exec_argv``) calls on the argv about to be
    exec'd. It is the same :func:`operand_rule` the structural checks (propose / confirm / every verify) apply to the
    manifest's own argv, kept as a separate entry so the reachability test can fail exactly the exec paths."""
    return operand_rule(argv, raw=raw, n_fixed=n_fixed, cwd=cwd, root=root, where=where)


def operand_rule(argv: List[str], *, raw: Tuple[str, ...], n_fixed: int, cwd: str, root: str, where: str,
                 homes: Optional[Iterable[str]] = None) -> "Judged":
    """THE strict shape (R3C-F1), on an argv. ``argv`` is the final list; ``raw`` the manifest's UNSUBSTITUTED tokens;
    ``argv[:n_fixed]`` is the hardened substituted manifest argv, everything after it is host-appended.

    Required, in this order, with no exception:
      1. argv[0] is the manifest's literal program token and an allow-listed interpreter by its REALPATH basename,
         whose realpath is a regular executable file OUTSIDE the user's home, OUTSIDE the app root and outside every
         scratch directory. A file inside the root is NEVER argv[0] (a copy of node named ``nc`` is refused);
      2. argv[1:1+k] is exactly the host's own flags (:func:`host_flags`); the manifest supplies nothing there;
      3. argv[1+k] is the manifest's raw[1], a literal token (no placeholder; not ``-``, ``+`` or ``~``-prefixed) that
         names a regular file in the pinned root, emitted as that file's absolute path;
      4. everything after the script (manifest args, port_arg, optional path flags, {path:}/{setting:} values) is an
         argument TO the script. Nothing host-supplied can precede it, so no host value is ever interpreter code;
      5. (R3E-F1, g73-amendment-8) the exec'd path is the judged path: argv[0] is judged by ``physical`` (realpath of
         the original string, no lexical normalisation) and the returned :class:`Judged` carries that realpath to exec
         and the identities to re-verify immediately before exec; the script operand is the realpath, judged and passed.
    """
    if not argv or not raw:
        raise RuleViolation(f"{where}[0]", "empty argv")
    homes = list(homes) if homes is not None else user_homes()
    if argv[0] != raw[0]:
        raise RuleViolation(f"{where}[0]", "the program is the manifest's own literal token")
    rp0 = physical(cwd, argv[0])
    kind = interpreter_kind(argv[0], cwd)
    if kind is None:
        raise RuleViolation(f"{where}[0]", f"{argv[0]!r} is not an allow-listed interpreter "
                            f"({', '.join(k for k, _ in _INTERPRETERS)}): argv[0] must be a system, framework or Homebrew "
                            "interpreter (or a venv symlink that resolves to one); a program that runs another program "
                            "it finds on PATH or in its arguments (env, arch, xcrun, caffeinate, sh -c ...) and any file "
                            "under the app root are refused by the operand rule (g73-amendment-6)")
    if not os.path.isfile(rp0) or not os.access(rp0, os.X_OK):
        raise RuleViolation(f"{where}[0]", f"{argv[0]!r} does not resolve to an executable regular file")
    if not is_macho(rp0):
        raise RuleViolation(f"{where}[0]", f"{argv[0]!r} resolves to {rp0}, which is not a Mach-O executable (a script or "
                            "other text is never an interpreter, whatever it is named; R3D-F2)")
    if inside(rp0, root):
        raise RuleViolation(f"{where}[0]", f"{argv[0]!r} resolves to {rp0}, inside the app root ({root}): a file under the "
                            "root is never the program, whatever it is named (a copied interpreter is code the "
                            "allow-list never saw); name a system or framework interpreter (operand rule, g73-amendment-6)")
    if any(inside(rp0, h) for h in homes):
        raise RuleViolation(f"{where}[0]", f"{argv[0]!r} resolves to {rp0}, under the user's home; name a system or "
                            "framework interpreter (such as /Library/Frameworks/Python.framework/Versions/3.13/bin/python3) "
                            "or a venv whose interpreter resolves to one (g73-amendment-6)")
    if any(inside(rp0, d) for d in _SCRATCH_DIRS if os.path.exists(d)):
        raise RuleViolation(f"{where}[0]", f"{argv[0]!r} resolves to {rp0}, in a world-writable scratch directory; name a "
                            "system or framework interpreter")
    flags = list(host_flags(argv[0], cwd))
    idx = 1 + len(flags)
    if list(argv[1:idx]) != flags:
        raise RuleViolation(f"{where}[0]", f"{argv[0]!r} must run with exactly the host's own flags {flags} between the "
                            "interpreter and the script, and the manifest supplies none")
    if len(raw) < 2 or len(argv) <= idx or n_fixed != len(flags) + len(raw) or n_fixed > len(argv):
        raise RuleViolation(f"{where}[1]", f"{argv[0]!r} is an interpreter and needs a script under the app root as its "
                            "first operand in the manifest's own argv (a host value never supplies it)")
    tok = raw[1]
    if PLACEHOLDER_RE.search(tok) or tok.startswith(("-", "+", "~")):
        raise RuleViolation(f"{where}[1]", f"{tok!r}: the script operand must be a literal file name under the app root "
                            "(no placeholder, no '-' or '+' option token, no ~): the manifest supplies nothing between "
                            "the interpreter and the script")
    script_path = physical(cwd, tok)
    if argv[idx] != script_path:
        raise RuleViolation(f"{where}[1]", f"the script operand must be the absolute path {script_path!r} (the host "
                            "emits it so no interpreter can read it as an option or subcommand)")
    script = script_path                                       # already the realpath: the judged string is the passed string
    if not os.path.isfile(script) or not inside(script, root):
        raise RuleViolation(f"{where}[1]", f"the script {tok!r} is not a file under the app root ({root}): the pin "
                            "covers the root and the script's modules must be inside it (declare "
                            "app_root in the manifest if the code lives above the manifest's directory)")
    return Judged(rp0, script, argv[0]).bind(argv)


def argv_groups(m: Manifest) -> List[Tuple[str, Tuple[str, ...]]]:
    out: List[Tuple[str, Tuple[str, ...]]] = [("start", m.start)]
    if m.hardware is not None:
        out.append(("hardware.presence_check", m.hardware.presence_check))
    for i, a in enumerate(m.actions):
        out.append((f"actions[{i}].argv", a.argv))
    return out


def check_manifest_argvs(m: Manifest, cwd: str, root: str, homes: Optional[Iterable[str]] = None) -> None:
    homes = list(homes) if homes is not None else user_homes()
    for where, argv in argv_groups(m):
        check_argv(where, argv, cwd, root, homes)


def check_writes_outside_root(m: Manifest, path_values: Dict[str, Any], root: str) -> Optional[str]:
    """None, or the reason a ``writes`` path value sits in the pinned tree (inside it, equal to it, or above it)."""
    for p in m.paths:
        v = (path_values or {}).get(p.name)
        if not v or p.access != "writes":
            continue
        rv = os.path.realpath(os.path.expanduser(v))
        if inside(rv, root) or inside(root, rv):
            return (f"the writes path {p.name!r} ({rv}) is inside, equal to or above the app root ({root}); a path "
                    "the app writes must lie OUTSIDE the pinned tree (g73-amendment-4 rule 2)")
    return None


def argv_path_names(m: Manifest) -> List[str]:
    """Every path-parameter name that reaches an argv the host builds: a {path:x} placeholder in start / port_arg /
    presence_check / actions, a path-typed {setting:x}, and every optional path (appended as [flag, value])."""
    import re

    names: List[str] = []
    toks: List[str] = list(m.start) + (list(m.port_arg) if m.port_arg else [])
    if m.hardware is not None:
        toks += list(m.hardware.presence_check)
    for a in m.actions:
        toks += list(a.argv)
    for k, v in m.env:
        toks.append(v)
    for t in toks:
        for inner in re.findall(r"\{([^{}]*)\}", t):
            kind, _, name = inner.partition(":")
            if kind == "path" and name and name not in names:
                names.append(name)
    for p in m.paths:
        if p.optional and p.name not in names:
            names.append(p.name)
    return sorted(names)


def argv_setting_path_names(m: Manifest) -> List[str]:
    """Path-typed user settings referenced by a {setting:x} in an argv/env (their VALUE is a path in an argv)."""
    import re

    toks: List[str] = list(m.start) + (list(m.port_arg) if m.port_arg else [])
    if m.hardware is not None:
        toks += list(m.hardware.presence_check)
    for a in m.actions:
        toks += list(a.argv)
    for k, v in m.env:
        toks.append(v)
    out = []
    for t in toks:
        for inner in re.findall(r"\{([^{}]*)\}", t):
            kind, _, name = inner.partition(":")
            s = m.setting(name) if kind == "setting" else None
            if s is not None and s.type == "path" and name not in out:
                out.append(name)
    return sorted(out)
