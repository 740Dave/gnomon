"""Single write-site: concurrency/race proof.

test_single_write_site.py already proves keystrokes and injects converge on
one low-level write call and that injects are logged. This file adds the
property that test doesn't: that concurrent callers (a real human-keystroke
thread racing a real inject) cannot INTERLEAVE mid-payload on the wire — the
lock in ``SingleWriteSite._write`` must hold across an entire (possibly
multi-chunk) payload, not just per low-level write() call.

The writer is deliberately made SLOW and CHUNKED (one byte per low-level
write, with a real thread-yielding sleep in between) so that if the lock did
not span the whole payload, the scheduler would have ample opportunity to
interleave two threads' bytes mid-payload. This is a real ``threading.Thread``
test, not a single-threaded simulation — it exercises the actual lock object
under actual contention.

⚠ Sensitivity check (recorded, not asserted in-suite): a hand run of this exact
methodology against a DELIBERATELY UNLOCKED write path (a bespoke copy with the
``with self._lock`` removed, same slow/chunked/sleeping writer body) produced
broken (non-multiple-of-37) runs in 10/10 trials (~1,600-1,700 broken runs per
trial out of 1,850 bytes) — confirming this test can fail, not merely pass
vacuously. A first version of this probe WITHOUT the ``time.sleep`` below
showed 0/10 trials breaking even unlocked — the sleep is load-bearing for
actually exercising the race, not decorative. See the bringup report for the
throwaway script.
"""

import threading
import time

from gnomon.harness import SingleWriteSite


def test_concurrent_inject_and_keystrokes_never_interleave_mid_payload():
    output = []
    out_lock = threading.Lock()  # protects the TEST's observation list only

    def slow_chunked_writer(_fd, data):
        # Always claims to have written exactly ONE byte, forcing
        # SingleWriteSite._write's internal loop to call this many times per
        # payload. The sleep is load-bearing: it yields control long enough for
        # the OS scheduler to actually switch threads (a plain tight loop is too
        # fast to reliably trigger a GIL handoff — measured: 0/10 trials showed
        # interleaving without it, 10/10 did with it, against an unlocked copy of
        # this same writer; see the bringup report for the throwaway probe).
        b = bytes(data[:1])
        with out_lock:
            output.append(b)
        time.sleep(0.0002)
        return 1

    writer = SingleWriteSite(master_fd=-1, writer=slow_chunked_writer)

    PAYLOAD_A = b"A" * 37  # odd, distinctive length
    PAYLOAD_B = b"B" * 37
    ITERATIONS = 25

    def hammer_inject():
        for _ in range(ITERATIONS):
            writer.inject(PAYLOAD_A)

    def hammer_keys():
        for _ in range(ITERATIONS):
            writer.write_keys(PAYLOAD_B)

    t_a = threading.Thread(target=hammer_inject)
    t_b = threading.Thread(target=hammer_keys)
    t_a.start()
    t_b.start()
    t_a.join(timeout=10)
    t_b.join(timeout=10)
    assert not t_a.is_alive() and not t_b.is_alive()

    stream = b"".join(output)
    assert len(stream) == len(PAYLOAD_A) * ITERATIONS + len(PAYLOAD_B) * ITERATIONS

    # Walk the byte stream: every maximal same-symbol run must be an EXACT
    # multiple of the payload length. A broken (non-multiple) run means the
    # other thread's bytes landed inside what should have been one atomic
    # payload — the single-write-site invariant would have been violated.
    i = 0
    while i < len(stream):
        symbol = stream[i:i + 1]
        j = i
        while j < len(stream) and stream[j:j + 1] == symbol:
            j += 1
        run_len = j - i
        assert run_len % 37 == 0, (
            f"interleaved write detected: run of {run_len} bytes of "
            f"{symbol!r} at offset {i} is not a multiple of the 37-byte "
            f"payload — the single write-site invariant was violated"
        )
        i = j

    # Also confirm the injection log recorded exactly the inject-side calls
    # (not the keystroke side), independent of the race probe above.
    assert len(writer.injection_log) == ITERATIONS
    assert writer.injected_bytes == PAYLOAD_A * ITERATIONS


def test_inject_log_ordering_is_consistent_under_concurrent_keystrokes():
    # A second, independent assertion: even under concurrent keystroke traffic,
    # the injection log (which only records injects) is exactly the sequence
    # of inject() calls, length-consistent with no duplication/loss from the
    # race — a duplication/loss bug would show up as a wrong count here even
    # if the byte-level interleave check above somehow missed it.
    calls = {"n": 0}

    def counting_writer(_fd, data):
        calls["n"] += 1
        return len(data)

    writer = SingleWriteSite(master_fd=-1, writer=counting_writer)

    def hammer_inject():
        for i in range(50):
            writer.inject(f"cmd-{i}".encode())

    def hammer_keys():
        for _ in range(50):
            writer.write_keys(b"keystroke")

    t_a = threading.Thread(target=hammer_inject)
    t_b = threading.Thread(target=hammer_keys)
    t_a.start()
    t_b.start()
    t_a.join(timeout=10)
    t_b.join(timeout=10)

    assert len(writer.injection_log) == 50
    assert [r.data for r in writer.injection_log] == [
        f"cmd-{i}".encode() for i in range(50)
    ]
