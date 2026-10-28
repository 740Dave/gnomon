"""Owner ruling 2026-10-04 (port item 4): the restart_service allowlist is DEPLOY data, never the shipped file.

The shipped ``verb_policy.toml`` stays neutral (``always = []``); a deployment layer with the same block shape that the
live tree ships makes its named role allowlisted for every service, and nobody else. The real deployment file is staged
for the cut-over under ~/gnomon-port-work/reroot/deploy/ (checked by reroot/check_deploy_verb_policy.py).
"""
from gnomon.harness import verb_policy as VP

BLOCK = '''schema = 1

[verbs.restart_service]
always = ["alpha-architect"]
others = "confirm-twice"
warning = "You're not on the allowlist for {what}; coordinate and call again."
confirm_window_seconds = 600
'''


def test_shipped_default_allowlists_nobody():
    pol = VP.load_policy(deploy_path="")
    assert pol.verbs["restart_service"].rule.always == ()


def test_a_deployment_layer_allowlists_its_role_and_only_that_role(tmp_path):
    f = tmp_path / "verb-policy.toml"
    f.write_text(BLOCK, encoding="utf-8")
    pol = VP.load_policy(deploy_path=str(f))
    assert pol.verbs["restart_service"].rule.always == ("alpha-architect",)
    assert pol.verb_sources["restart_service"] == str(f)
    ctx = VP.Ctx(policy=pol)
    yes = VP.check("restart_service", "alpha-architect", target="svc", ctx=ctx)
    no = VP.check("restart_service", "gamma-assistant", target="svc", ctx=ctx)
    assert yes.outcome == VP.ALLOW and yes.reason == "always", yes
    assert no.outcome == VP.CONFIRM and no.needs_ack, no     # control: the allowlist is not everyone
