"""Entity OWNERSHIP (I1): every entity has ONE owner-scope reached through its owner chain and ONE storage
location, and deletion is allowed only at the owner.

``OWNERSHIP`` is the code form of the ENT-* rows of the spec (owner-scope, parent, substrate). It is an EXTENSION
POINT with a guard (g34 (b)): ``tests/test_x13s2_invariants_i1_i5.py`` parses the spec's ENT nodes (a g25 reader with
a floor) and fails when an ENT node has no row here, when a row's owner-scope disagrees with the spec text, or when a
row names an ENT that is gone. A new entity therefore cannot be added to the model without saying who owns it.

The OWNER SCOPE of a record is either ``machine`` or ``workspace:<ws>``. A row's ``parent`` is the entity whose owner
the record inherits (an item sits in a corpus, a session-run in a conversation in a role, ...); ``owner`` is the scope
the chain ends in. ``machine|workspace`` (the corpus) is decided per record (a corpus is owned by exactly one).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .scopebase import ScopeRefusal


@dataclass(frozen=True)
class Own:
    #: machine | workspace | machine|workspace | layered | not-an-entity
    owner: str
    #: the entity the owner is reached through (None: the row names its owner scope directly)
    parent: Optional[str]
    #: the ONE storage location (the spec's `substrate`, abbreviated)
    storage: str


OWNERSHIP: Dict[str, Own] = {
    "WORKSPACE": Own("machine", None, "DEPLOY workspaces/<ws>/workspace.json"),
    "PROJECT": Own("workspace", None, "DEPLOY projects.json"),
    "BACKLOG": Own("workspace", None, "DEPLOY workspace.json backlogs[]"),
    "ITEM": Own("workspace", "CORPUS", "lts property project: <ws>/<slug>"),
    "TOPIC": Own("workspace", None, "DATA workspaces/<ws>/topics/TOP-nnn.json"),
    "ROLE-DEFINITION": Own("workspace", None, "DEPLOY workspaces/<home>/roles/<id>.md"),
    "ROLE-DATA": Own("machine", None, "DATA intelligences/<id>/"),
    "AGENT-LAUNCH-CONFIG": Own("machine", "ROLE-DATA", "DATA intelligences/<id>/launch-config.json"),
    "CONVERSATION": Own("machine", "ROLE-DATA", "Claude session UUID"),
    "SESSION-RUN": Own("machine", "CONVERSATION", "DATA sessions/<dir-id>/"),
    "HANDOVER": Own("machine", "ROLE-DATA", "DATA intelligences/<id>/handovers/"),
    "DETACHED-PROCESS": Own("machine", "SESSION-RUN", "inside its handover"),
    "CORPUS": Own("machine|workspace", None, "corpus-config entry"),
    "SHIPPED-ASSETS": Own("machine", None, "installed with the package"),
    "DSD-RECORD": Own("machine", "ROLE-DATA", "DATA, beside its answers"),
    "DSD-DECISION": Own("machine", None, "one machine answers corpus"),
    "SCHEDULED-JOB": Own("machine", None, "Scheduler store"),
    "JOB-RUN": Own("machine", "SCHEDULED-JOB", "Scheduler store"),
    "BOOTSTRAP-JOB": Own("machine", None, "launchd plist + a machine.json section"),
    "ACCOUNT-USAGE": Own("machine", None, "statusline feed"),
    "ALERT-EVENT": Own("machine", None, "append-only event log"),
    "TIMELINE-ENTRY": Own("machine", None, "timeline corpus"),
    "ATTENTION-STATE": Own("machine", None, "derived read model"),
    "VIEWER": Own("machine", None, "a viewer entity"),
    "SETTINGS": Own("layered", None, "each key declares its default and scope-floor"),
    "PREFIX-REGISTRY": Own("machine", None, "DATA"),
    "SNAPSHOTS-HUB-BREAKGLASS": Own("machine", None, "as today"),
    "NON-HARNESSED-SESSION": Own("not-an-entity", None, "broadcast target only"),
}


def owner_chain(kind: str) -> List[str]:
    """The owner chain of an entity kind, ending in the scope kind: ``['SESSION-RUN', 'CONVERSATION', 'ROLE-DATA',
    'machine']``. Raises if the kind is unknown or the chain loops (I1)."""
    if kind not in OWNERSHIP:
        raise ScopeRefusal("entity-unknown", f"entity kind {kind!r} has no ownership row")
    chain: List[str] = []
    cur = kind
    while True:
        if cur in chain:
            raise ScopeRefusal("owner-chain-loop", f"owner chain of {kind!r} loops through {cur!r}")
        chain.append(cur)
        row = OWNERSHIP[cur]
        if row.parent is None:
            chain.append(row.owner)
            return chain
        cur = row.parent


def owner_scope_of(kind: str, *, workspace: Optional[str] = None, corpus_owner: Optional[str] = None) -> str:
    """The ONE owner scope of a record of ``kind``: ``'machine'`` or ``'workspace:<ws>'``.

    ``workspace`` is the record's workspace for a workspace-owned kind (derived from its project, never stored: I9);
    ``corpus_owner`` is the owning corpus's own owner for a record that sits in a corpus (an item). An item in a
    machine-owned (shared) corpus is refused (ENT-ITEM resolved-question-117: backlogs use workspace-owned corpora)."""
    chain = owner_chain(kind)
    end = chain[-1]
    if kind == "CORPUS":
        if corpus_owner != "machine" and not (isinstance(corpus_owner, str) and corpus_owner.startswith("workspace:")):
            raise ScopeRefusal("owner-unresolved", f"a corpus is owned by exactly one of machine / workspace:<ws>; got {corpus_owner!r}")
        return corpus_owner
    if kind == "ITEM" or "CORPUS" in chain[:-1]:
        if corpus_owner is None or not corpus_owner.startswith("workspace:"):
            raise ScopeRefusal("item-corpus-not-workspace-owned",
                               f"{kind} sits in a corpus whose owner is {corpus_owner!r}; items need a workspace-owned corpus")
        return corpus_owner
    if end == "workspace":
        if not workspace:
            raise ScopeRefusal("owner-unresolved", f"{kind} is workspace-owned but no workspace was given")
        return f"workspace:{workspace}"
    if end == "machine":
        return "machine"
    raise ScopeRefusal("owner-not-resolvable", f"{kind} has owner {end!r}: it does not resolve to one owner scope")


def authorize_delete(kind: str, *, acting: str, workspace: Optional[str] = None, corpus_owner: Optional[str] = None) -> None:
    """Deletion is allowed ONLY at the owner (I1). ``acting`` is ``'machine'`` or ``'workspace:<ws>'``. Raises
    :class:`ScopeRefusal` (code ``not-the-owner``) otherwise, and always for a workspace entity (I5: a workspace is
    archived, never deleted: use the writer's archive)."""
    if kind == "WORKSPACE":
        raise ScopeRefusal("only-archive-offered", "a workspace is never deleted, only archived (I5)",
                           "archive the workspace instead")
    owner = owner_scope_of(kind, workspace=workspace, corpus_owner=corpus_owner)
    if acting != owner:
        raise ScopeRefusal("not-the-owner", f"{kind} is owned by {owner!r}; a delete from {acting!r} is refused (I1)",
                           f"delete it from {owner}")
