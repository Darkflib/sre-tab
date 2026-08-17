"""GitHub OAuth sign-in, session issuance, and authorisation.

Phase 1 agent A property. The public surface used by the API layer is
:mod:`app.auth.flow` (orchestration) and :mod:`app.auth.sessions` (session
lookup and cookies); the rest are its collaborators.
"""
