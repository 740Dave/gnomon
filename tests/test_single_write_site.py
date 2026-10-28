"""Single write-site: keystrokes and injects share one path; injects logged.

Covers the single write site.
"""

from gnomon.harness import Injector, SingleWriteSite


def _capturing_writer():
    """A SingleWriteSite whose one os.write is captured, with a call counter."""
    calls = []  # each entry: bytes passed to the low-level writer

    def fake_write(_fd, data):
        calls.append(bytes(data))
        return len(data)

    writer = SingleWriteSite(master_fd=-1, writer=fake_write)
    return writer, calls


def test_keystrokes_and_injects_use_the_same_low_level_write():
    writer, calls = _capturing_writer()
    writer.write_keys(b"abc")
    writer.inject(b"XYZ")
    # Both reached the single low-level write call site, in order.
    assert calls == [b"abc", b"XYZ"]


def test_injects_are_logged_keystrokes_are_not():
    writer, _calls = _capturing_writer()
    writer.write_keys(b"typed by human")
    writer.inject(b"typed by harness")
    writer.write_keys(b"more human")

    logged = writer.injected_bytes
    assert logged == b"typed by harness"
    assert b"typed by human" not in logged
    assert b"more human" not in logged
    assert len(writer.injection_log) == 1
    assert writer.injection_log[0].data == b"typed by harness"


def test_child_cannot_distinguish_inject_from_keystroke_on_the_wire():
    # The low-level writer sees identical bytes whether they came from a
    # keystroke or an inject — the ONLY difference is the injection log.
    # r2c: a raw depth-0 inject now screens the union (a CR would be refused), so the payload is
    # control-free; the wire-identity property is unchanged.
    writer, calls = _capturing_writer()
    writer.write_keys(b"/clear")
    baseline = list(calls)

    writer2, calls2 = _capturing_writer()
    writer2.inject(b"/clear")
    assert calls2 == baseline  # same bytes on the wire


def test_injector_writes_only_through_inject_path():
    writer, _calls = _capturing_writer()
    Injector(writer, sleep=lambda _s: None).inject_line("/clear", clear=True)  # PTY-hooks WS-D: the clear is opt-in now
    # Every phase of the sequence was logged as an injection (none as keystroke).
    assert writer.injected_bytes == b"\x1b\x1b/clear\r"
    assert len(writer.injection_log) == 3  # ESC-ESC, text, CR


def test_partial_writes_are_completed():
    # A low-level write that only accepts one byte at a time must still deliver
    # the whole payload through the single site.
    chunks = []

    def one_byte_writer(_fd, data):
        chunks.append(bytes(data[:1]))
        return 1

    writer = SingleWriteSite(master_fd=-1, writer=one_byte_writer)
    writer.write_keys(b"hello")
    assert b"".join(chunks) == b"hello"
