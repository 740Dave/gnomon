"""A fake Claude Code input box for the PTY tests (run as a child on a REAL PTY).

It models the TUI behaviour that truncated the 2026-10-02T04:40:19Z ``\\all`` broadcast,
measured from the evidence (not from Claude Code's source, which we do not have):

* raw mode; a line editor with a cursor: printable bytes insert at the cursor,
  ``ESC [ D`` / ``ESC [ C`` move it left / right, ``CR`` submits;
* a bracketed paste (``ESC [200~ ... ESC [201~``) is accumulated across reads and
  inserted whole at the cursor;
* an UNBRACKETED read of >= 41 bytes (HEUR) is a heuristic paste, and a heuristic paste that
  immediately follows another one REPLACES it. A CR inside such a read is absorbed (no
  submit), as measured on the real TUI for a one-write ``line + CR`` (round 3, F15). That is the measured defect: the
  1,107-byte broadcast reached the child as reads of [1022, 85] (macOS PTY), and
  every recipient's transcript held exactly the final 85 characters.

After each read it writes one ACK byte (0x06) so a test can pace "human" keystrokes.
On submit it writes ``SUBMIT:<base64>\\n`` and appends a transcript row to
``$FAKE_TRANSCRIPT`` the way Claude Code records it: a ``user`` row; a prompt that is
one bracketed paste of >= 800 bytes is recorded wrapped in ``<pasted_content id=..>``
with nested tags escaped (the shape measured on the 2026-09-30T16:11:05Z broadcast).
"""

import base64
import json
import os
import sys
import time
import tty

#: Round 4 (r3 Diamond F20 / P3-2): the absorb threshold, pinned to the REAL measurement on
#: Claude Code 2.1.287: a write of line + CR SUBMITTED at <= 40 bytes and was ABSORBED at >= 64.
#: The unmeasured band 41..63 counts as absorbed (conservative), so any read of >= 41 bytes is a
#: heuristic paste: a CR inside it does not submit, and it replaces an immediately preceding one.
HEUR = 41
ACK = b"\x06"
PS, PE = b"\x1b[200~", b"\x1b[201~"


def _iso_now() -> str:
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".%03dZ" % int((t % 1) * 1000)


def main() -> None:
    try:
        tty.setraw(0)
    except Exception:
        pass
    transcript = os.environ.get("FAKE_TRANSCRIPT")
    buf = bytearray()
    cur = 0
    pending = b""
    in_paste = False
    paste = bytearray()
    last_heur = None          # (start, length) of the previous heuristic-paste insertion
    whole_paste_prompt = None  # the buffer is exactly one bracketed paste (for the wrap shape)
    os.write(1, b"READY\n")
    while True:
        try:
            data = os.read(0, 65536)
        except OSError:
            break
        if not data:
            break
        if not in_paste and not pending and b"\x1b" not in data and len(data) >= HEUR:
            # (round 3) a CR inside such a burst does NOT submit: it is absorbed into the
            # heuristic paste. MEASURED on the real 2.1.287 TUI: one 1312-byte write of
            # line + CR left only the tail in the input box, unsubmitted.
            data = data.replace(b"\r", b"")
            if last_heur is not None:
                s, n = last_heur
                del buf[s:s + n]
                cur = s
            buf[cur:cur] = data
            last_heur = (cur, len(data))
            cur += len(data)
            whole_paste_prompt = None
            os.write(1, ACK)
            continue
        last_heur = None
        stream = pending + data
        pending = b""
        i = 0
        while i < len(stream):
            if in_paste:
                j = stream.find(PE, i)
                if j < 0:
                    # keep a possible partial end marker for the next read
                    keep = max(i, len(stream) - (len(PE) - 1))
                    paste += stream[i:keep]
                    pending = stream[keep:]
                    i = len(stream)
                    break
                paste += stream[i:j]
                if not buf:
                    whole_paste_prompt = bytes(paste)
                else:
                    whole_paste_prompt = None
                buf[cur:cur] = paste
                cur += len(paste)
                paste = bytearray()
                in_paste = False
                i = j + len(PE)
                continue
            b = stream[i]
            if b == 0x1B:
                rest = stream[i:]
                if rest.startswith(PS):
                    in_paste = True
                    i += len(PS)
                    continue
                if rest.startswith(b"\x1b[D"):
                    cur = max(0, cur - 1)
                    i += 3
                    continue
                if rest.startswith(b"\x1b[C"):
                    cur = min(len(buf), cur + 1)
                    i += 3
                    continue
                if len(rest) < len(PS) and PS.startswith(rest) or rest in (b"\x1b", b"\x1b["):
                    pending = rest
                    i = len(stream)
                    break
                i += 1  # an unmodelled escape: ignored
                continue
            if b == 0x0D:
                line = bytes(buf)
                os.write(1, b"SUBMIT:" + base64.b64encode(line) + b"\n")
                if transcript:
                    text = line.decode("utf-8", "replace")
                    if whole_paste_prompt is not None and whole_paste_prompt == line and len(line) >= 800:
                        esc = text.replace("</pasted_content", "<\\/pasted_content").replace(
                            "<pasted_content", "<\\pasted_content")
                        text = f'\n\n<pasted_content id="f4ke">\n{esc}\n</pasted_content id="f4ke">\n'
                    with open(transcript, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps({"type": "user", "timestamp": _iso_now(),
                                             "message": {"role": "user", "content": text}}) + "\n")
                buf = bytearray()
                cur = 0
                whole_paste_prompt = None
                i += 1
                continue
            buf[cur:cur] = bytes([b])
            cur += 1
            whole_paste_prompt = None
            i += 1
        os.write(1, ACK)


if __name__ == "__main__":
    sys.exit(main())
