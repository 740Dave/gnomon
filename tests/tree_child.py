"""Start a child Python interpreter that imports the TREE UNDER TEST, never the installed ``gnomon``.

Why this exists: the framework Python carries an EDITABLE install of
the live tree, and the live tree names its package ``gnomon`` exactly like a scratch clone. ``python -I`` and ``python -E`` ignore
PYTHONPATH, ``-I`` and ``-P`` also drop the cwd / script directory from ``sys.path``, and an ``env=`` dict that omits PYTHONPATH
loses the pin ``tests/conftest.py`` puts on the environment. A child started any of those ways imports the LIVE tree, so the test
passes or fails on the wrong code. The cure is an explicit ``sys.path`` entry in the child itself: it survives every flag above.

Use ``child_argv`` / ``run_child`` / ``child_env`` for any child that imports ``gnomon`` and is started with an isolating flag or a
stripped environment. ``tests/test_child_python_tree.py`` guards this module.
"""
import os
import subprocess
import sys

TREE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Flags that make the interpreter ignore PYTHONPATH and/or the cwd, so only an in-child sys.path entry is reliable.
ISOLATING_FLAGS = ("-I", "-E", "-P")


def tree_prelude(root=None):
    """Source text, to run first in the child, that puts the tree under test at the front of ``sys.path``."""
    return "import sys; sys.path.insert(0, %r); " % (root or TREE_ROOT)


def child_argv(code, *args, flags=(), root=None, with_tree_entry=True):
    """argv for ``python <flags> -c <prelude + code> <args>``.

    ``with_tree_entry=False`` omits the sys.path entry; it exists ONLY so the guard test can show that the entry is what makes
    the difference (its positive control). Do not use it elsewhere.
    """
    prelude = tree_prelude(root) if with_tree_entry else ""
    return [sys.executable, *flags, "-c", prelude + code, *args]


def child_env(extra=None, root=None, drop=()):
    """A minimal environment for a child: PATH and HOME from this process, PYTHONPATH pinned to the tree, plus ``extra``.

    PYTHONPATH is also set because it covers a ``-m`` or a script child that the in-child prelude does not (a ``-c`` prelude only
    reaches code that runs after it).
    """
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR") and k not in drop}
    env["PYTHONPATH"] = root or TREE_ROOT
    env.update(extra or {})
    return env


def run_child(code, *args, flags=(), env=None, cwd=None, root=None, with_tree_entry=True, **kw):
    """``subprocess.run`` of ``child_argv(...)``; ``env`` defaults to ``child_env()``."""
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("timeout", 60)
    return subprocess.run(child_argv(code, *args, flags=flags, root=root, with_tree_entry=with_tree_entry),
                          env=child_env(root=root) if env is None else env, cwd=cwd, **kw)
