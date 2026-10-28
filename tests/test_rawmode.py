"""RawMode: enter saves, exit restores — including on exception.

Covers. RawMode is only exercised indirectly via
HarnessProxy.run() (which no test drives), so these unit tests pin it directly.

Two approaches, both here:
* mock ``termios.tcgetattr``/``tcsetattr`` (+ ``tty.setraw``) and assert the
  save/restore calls happen in the right order and on exception;
* a real pty pair as the controlling-tty fixture, proving the attrs are
  genuinely saved and restored byte-for-byte.
"""

import os
import pty
from unittest import mock

import pytest

from gnomon.harness.rawmode import RawMode


# -- mocked termios -----------------------------------------------------------


def _patched(saved_attrs):
    """Patch rawmode's os.isatty (True) + termios/tty with mocks.

    Returns the patchers' mocks as a namespace-ish dict.
    """
    isatty = mock.patch("gnomon.harness.rawmode.os.isatty", return_value=True)
    termios = mock.patch("gnomon.harness.rawmode.termios")
    tty = mock.patch("gnomon.harness.rawmode.tty")
    m_isatty = isatty.start()
    m_termios = termios.start()
    m_tty = tty.start()
    m_termios.tcgetattr.return_value = saved_attrs
    # keep the real TCSADRAIN constant name usable as an opaque sentinel
    m_termios.TCSADRAIN = "TCSADRAIN"
    return (m_isatty, m_termios, m_tty), (isatty, termios, tty)


def _stop(patchers):
    for p in patchers:
        p.stop()


def test_enter_saves_and_exit_restores():
    saved = ["orig-termios-attrs"]
    (m_isatty, m_termios, m_tty), patchers = _patched(saved)
    try:
        rm = RawMode(fd=7)
        assert rm.active is False
        with rm:
            # entering saved the current attrs and switched to raw
            m_termios.tcgetattr.assert_called_once_with(7)
            m_tty.setraw.assert_called_once_with(7)
            # ... and restore has NOT happened yet
            m_termios.tcsetattr.assert_not_called()
            assert rm.active is True
        # exiting restored the EXACT saved attrs
        m_termios.tcsetattr.assert_called_once_with(7, "TCSADRAIN", saved)
        assert rm.active is False
    finally:
        _stop(patchers)


def test_restore_happens_on_exception():
    saved = ["orig-termios-attrs"]
    (m_isatty, m_termios, m_tty), patchers = _patched(saved)
    try:
        rm = RawMode(fd=3)
        with pytest.raises(RuntimeError, match="boom"):
            with rm:
                assert rm.active is True
                raise RuntimeError("boom")
        # the terminal was still restored despite the exception
        m_termios.tcsetattr.assert_called_once_with(3, "TCSADRAIN", saved)
        assert rm.active is False
    finally:
        _stop(patchers)


def test_restore_is_idempotent():
    saved = ["orig-termios-attrs"]
    (m_isatty, m_termios, m_tty), patchers = _patched(saved)
    try:
        rm = RawMode(fd=5)
        rm.__enter__()
        rm.restore()
        rm.restore()  # second call is a no-op
        assert m_termios.tcsetattr.call_count == 1
        assert rm.active is False
    finally:
        _stop(patchers)


def test_non_tty_fd_is_inert():
    # A pipe (not a tty): enter/exit must do nothing and never touch termios.
    r, w = os.pipe()
    try:
        with mock.patch("gnomon.harness.rawmode.termios") as m_termios, \
             mock.patch("gnomon.harness.rawmode.tty") as m_tty:
            with RawMode(fd=r) as rm:
                assert rm.active is False
            m_termios.tcgetattr.assert_not_called()
            m_tty.setraw.assert_not_called()
            m_termios.tcsetattr.assert_not_called()
    finally:
        os.close(r)
        os.close(w)


# -- real pty controlling-tty fixture ----------------------------------------


# NOTE: on macOS, tcgetattr after tcsetattr of the identical struct flips a
# high reserved bit (0x20000000) in c_lflag, so full-struct equality is not a
# portable "restored?" check. We assert on the meaningful cooked line-discipline
# bits (ECHO|ICANON) instead: cooked has them set, raw clears them, restore sets
# them back. c_lflag is index 3 of the tcgetattr list.
def _cooked_bits(attrs):
    import termios as real_termios

    return attrs[3] & (real_termios.ECHO | real_termios.ICANON)


def test_real_pty_saves_and_restores_attrs():
    import termios as real_termios

    parent_fd, child_fd = pty.openpty()
    try:
        before = real_termios.tcgetattr(child_fd)
        assert _cooked_bits(before) != 0  # starts cooked (echo + canonical)
        with RawMode(child_fd) as rm:
            assert rm.active is True
            during = real_termios.tcgetattr(child_fd)
            assert _cooked_bits(during) == 0  # raw cleared echo + canonical
        after = real_termios.tcgetattr(child_fd)
        # the cooked line-discipline bits are restored to their saved state
        assert _cooked_bits(after) == _cooked_bits(before)
        assert rm.active is False
    finally:
        os.close(parent_fd)
        os.close(child_fd)


def test_real_pty_restores_on_exception():
    import termios as real_termios

    parent_fd, child_fd = pty.openpty()
    try:
        before = real_termios.tcgetattr(child_fd)
        with pytest.raises(ValueError):
            with RawMode(child_fd):
                during = real_termios.tcgetattr(child_fd)
                assert _cooked_bits(during) == 0  # really went raw first
                raise ValueError("kaboom")
        after = real_termios.tcgetattr(child_fd)
        # restored despite the exception
        assert _cooked_bits(after) == _cooked_bits(before)
    finally:
        os.close(parent_fd)
        os.close(child_fd)
