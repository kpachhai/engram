"""engram team-vault server-side hook bundle.

The ``pre-receive`` hook in this directory is a stdlib-only Python 3.10+
script. It is COPIED to a team-vault remote's ``hooks/`` directory by
the operator running ``engram team-vault setup``; engram does not
import it as Python code (the script is the artifact, not a module).

The hook is the canonical enforcement point for push-time policies per
Phase 4 pinned invariant 4: ``.indexes/`` paths refused, block
portability refused, committer/captured_by mismatch refused, disallowed
prefix refused, force-push refused, steward-only mutation of policy +
members enforced.
"""

from __future__ import annotations
