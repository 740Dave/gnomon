"""a real dated instance id survives as an actor while a credential-shaped
actor is redacted. The spec's fixtures use deployment lineage ids; this test uses neutral ids of the
SAME SHAPE (name-YYYYMMDD-HHMMSS), which is the property under test (whether fixtures count as code under g3 is an
open architect ruling)."""

from fu_index_support import ev, index, rows, write_events
from fu_support import INSTANCE_ID, OTHER_INSTANCE
from gnomon import usage_metrics as UM

GH = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
SK = "sk-" + "proj1234567890abcdefghijklmnop"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sig"


def test_instance_id_kept_secret_redacted(tmp_path):
    root, db = tmp_path / "ev", tmp_path / "u.sqlite"
    write_events(root, "20260930", "hooks", [
        ev(actor=INSTANCE_ID), ev(actor=OTHER_INSTANCE), ev(actor=GH), ev(actor=SK), ev(actor=JWT),
        ev(actor="Not A Valid Actor!")])
    index(db, root)
    a = [r["actor"] for r in rows(str(db))]
    assert a[0] == INSTANCE_ID and a[1] == OTHER_INSTANCE                       # stored VERBATIM
    assert a[2] == a[3] == a[4] == "<redacted>"
    assert a[5] == "<non-category>"                                             # fails the shape regex


def test_negative_control_the_x7_token_rule_destroys_the_same_id():
    """the generic entropy rule (usage_metrics._tok) DOES redact the dated instance id: so the exemption, and
    nothing else, is what changed the outcome in the test above"""
    assert UM.is_credential_shaped(INSTANCE_ID) is True
    assert UM._tok(INSTANCE_ID) == "<redacted>"
    assert UM._tok(OTHER_INSTANCE) == OTHER_INSTANCE          # the short one passes both rules: not a discriminator
