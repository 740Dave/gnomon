"""Slice 4: the presence line claims no more than it measures (step 1).
Where the line reports observed typing it says CONSOLE typing and ends with the fixed clause; a DECLARED status is byte-identical to before."""

import re

import pytest

from gnomon.worklist import constants as C
from gnomon.worklist.presence import NO_CONSOLE_TYPING, REMOTE_CONTROL_CLAUSE, observed_part, presence_line
from gnomon.worklist.status import Status

T = 1_800_000_000.0                       # 2027-01-15 08:00Z
CLAUSE = " (Remote Control messages are not counted)"
OBS_PEER = {"here": None, "others": [("peer-agent", 240.0)], "any": 240.0}          # readable beacons: typing 4 min ago in another session
OBS_HERE = {"here": 130.0, "others": [], "any": 130.0}                               # readable: typing 2 min ago in this session
OBS_NONE = None                                                                    # beacons unreadable / nothing known
OBS_EMPTY = {"here": None, "others": [], "any": None}                              # readable, but no typing seen anywhere

IN_OFFICE = Status(declared=True, state="in-office", text="", setter_id="operator", setter_name="Sam", source="", set_at=T)
AWAY = Status(declared=True, state="away", text="at the dentist", setter_id="agent-b", setter_name="Agent B", source="graph:K", set_at=T)
UNKNOWN = Status(declared=True, state="unknown", lost=True, error="status file is unreadable")
NEVER = Status()

TOLD_OFFICE = "(as told to Sam at 2027-01-15 08:00Z)"
TOLD_AWAY = "(as told to Agent B at 2027-01-15 08:00Z; source: graph:K)"


def line(st, obs):
    return presence_line(st, obs, "Sam")


# ---- each state, with and without readable beacons: the exact line -------------------------------------------------------------
@pytest.mark.parametrize("st,obs,want", [
    (IN_OFFICE, OBS_PEER, f"[Gnomon] Sam's status: in office {TOLD_OFFICE}; seen typing at a console 4 min ago with peer-agent{CLAUSE}"),
    (IN_OFFICE, OBS_HERE, f"[Gnomon] Sam's status: in office {TOLD_OFFICE}; seen typing at a console 2 min ago here{CLAUSE}"),
    (IN_OFFICE, OBS_NONE, f"[Gnomon] Sam's status: in office {TOLD_OFFICE}; console typing could not be read{CLAUSE}"),
    (IN_OFFICE, OBS_EMPTY, f"[Gnomon] Sam's status: in office {TOLD_OFFICE}"),
    (AWAY, OBS_PEER, f"[Gnomon] Sam's status: away: at the dentist {TOLD_AWAY}; seen typing at a console 4 min ago with peer-agent{CLAUSE}"),
    (AWAY, OBS_NONE, f"[Gnomon] Sam's status: away: at the dentist {TOLD_AWAY}; console typing could not be read{CLAUSE}"),
    (AWAY, OBS_EMPTY, f"[Gnomon] Sam's status: away: at the dentist {TOLD_AWAY}"),
    (UNKNOWN, OBS_PEER, f"[Gnomon] Sam's status: unreadable (status unreadable; treated as not in office); seen typing at a console 4 min ago with peer-agent{CLAUSE}"),
    (UNKNOWN, OBS_NONE, f"[Gnomon] Sam's status: unreadable (status unreadable; treated as not in office); console typing could not be read{CLAUSE}"),
    (UNKNOWN, OBS_EMPTY, "[Gnomon] Sam's status: unreadable (status unreadable; treated as not in office)"),
    (NEVER, OBS_PEER, f"[Gnomon] Sam: seen typing at a console 4 min ago with peer-agent{CLAUSE}"),
    (NEVER, OBS_HERE, f"[Gnomon] Sam: seen typing at a console 2 min ago here{CLAUSE}"),
    (NEVER, OBS_NONE, f"[Gnomon] Sam: console typing could not be read{CLAUSE}"),
    (NEVER, OBS_EMPTY, f"[Gnomon] Sam: no console typing seen{CLAUSE}"),
])
def test_s4_the_exact_line_for_each_state(st, obs, want):
    got = line(st, obs)
    assert got == want
    # the old wording is ABSENT
    assert not re.search(r"seen typing \d", got) and "no typing seen" not in got
    if obs is OBS_NONE:                                   # an instrument that could not be read is never worded as a reading of nothing (g112)
        assert "no console typing seen" not in got and "could not be read" in got


def test_s4_a_declared_status_is_byte_identical_to_before_with_no_observation():
    """The declared part (text, who set it, when, 'as told to') has not moved by one byte: it is the old line, exactly (beacons read, nothing seen)."""
    assert line(IN_OFFICE, OBS_EMPTY) == "[Gnomon] Sam's status: in office (as told to Sam at 2027-01-15 08:00Z)"
    assert line(AWAY, OBS_EMPTY) == "[Gnomon] Sam's status: away: at the dentist (as told to Agent B at 2027-01-15 08:00Z; source: graph:K)"
    assert line(UNKNOWN, OBS_EMPTY) == "[Gnomon] Sam's status: unreadable (status unreadable; treated as not in office)"
    # and with an observation the declared part is a prefix of the new line
    assert line(AWAY, OBS_PEER).startswith("[Gnomon] Sam's status: away: at the dentist (as told to Agent B at 2027-01-15 08:00Z; source: graph:K)")


def test_s4_the_fixed_clause_appears_exactly_when_the_observed_part_does():
    for st in (IN_OFFICE, AWAY, UNKNOWN, NEVER):
        for obs in (OBS_PEER, OBS_HERE, OBS_NONE, OBS_EMPTY):
            got = line(st, obs)
            shown = bool(observed_part(obs)) or st is NEVER                      # never set: the 'no console typing seen' statement IS the observed part
            assert got.count(REMOTE_CONTROL_CLAUSE) == (1 if shown else 0), (st.state, obs, got)
            assert got.endswith(CLAUSE) == shown
    assert CLAUSE == " " + REMOTE_CONTROL_CLAUSE and NO_CONSOLE_TYPING == "no console typing seen"


def test_s4_the_longest_case_is_cut_in_the_ruled_order_and_stays_within_the_bound():
    st = Status(declared=True, state="away", text="t" * 200, setter_id="s", setter_name="n" * 40, source="s" * 200, set_at=T)
    got = presence_line(st, {"here": 120.0, "others": [("o" * 64, 30.0)]}, "u" * 100)
    assert len(got) <= C.PRESENCE_LINE_MAX == 400 and got.endswith(CLAUSE) and "\n" not in got
    assert "; source: …)" in got and "source: sss" not in got                                  # step 1: the source is dropped whole, marked
    assert "seen typing at a console 0 min ago with " + "o" * 64 + " " in got                      # the observed part is whole
    assert got.count(REMOTE_CONTROL_CLAUSE) == 1


def test_s4_the_display_name_still_comes_from_the_caller():
    assert presence_line(NEVER, OBS_EMPTY, "Ada Lovelace") == f"[Gnomon] Ada Lovelace: no console typing seen{CLAUSE}"
    assert presence_line(NEVER, OBS_EMPTY, "") == f"[Gnomon] the operator: no console typing seen{CLAUSE}"
