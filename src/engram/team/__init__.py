"""engram.team - Phase 4 team-vault primitives.

This package houses the team-vault-specific concerns introduced in Phase 4:

* :mod:`engram.team.policy` - Pydantic ``TeamVaultPolicy`` model + the
  capture-time policy refusal gate.
* :mod:`engram.team.members` - ``MembersList`` enrolled-fingerprint mapping.
* :mod:`engram.team.routing` - ``RoutingRule`` (re-exported from
  :mod:`engram.config.models`) + ``resolve_target_vault`` dispatcher.
* :mod:`engram.team.identity` - GPG signing-key discovery + member-enrollment
  assertion.
* :mod:`engram.team.push_queue` - Persistent push queue surviving engram
  restart.
* :mod:`engram.team.capture_gate` - Composes the read-only-refusal +
  member-enrollment + policy-pass + captured_by-stamping at capture time.
* :mod:`engram.team.server_hooks` - Python 3.10+ stdlib-only ``pre-receive``
  hook bundled by ``engram team-vault setup``.

Phase 4 design: see ``docs/PHASE_4_PLAN.md`` and ``docs/adr/007-team-brain.md``.
"""

from __future__ import annotations
