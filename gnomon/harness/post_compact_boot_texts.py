"""The post-compact boot texts: the NEUTRAL package defaults — DATA ONLY (X13 S5-3).

No logic lives here. These are the package-shipped defaults of the boot line the
harness delivers after a compaction. The package default is ONE generic sentence for
every role and ships NO per-role text: a role's custom text is deployment data
(``<deploy_root>/defaults/post-compact-boot.json``) or config; see
:mod:`gnomon.harness.post_compact_boot` for the shape and the precedence.

Kept as a separate data module (not inlined in the resolver, not a JSON file: the
package has no package-data declaration, so a non-editable install would lose a
JSON file) so a text change touches no logic.
"""

from __future__ import annotations

from typing import Dict

#: The text for any role without a custom one.
DEFAULT_TEXT: str = (
    "Please complete the bootstrap process as specified in your agent definition."
)

#: Custom per-role texts, keyed by the role (the instance id / ``${ROLE}``). EMPTY by design: per-role text is
#: deployment data, never a package literal.
ROLE_TEXTS: Dict[str, str] = {}
