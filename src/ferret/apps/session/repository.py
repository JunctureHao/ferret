"""Session persistence API.

The implementation remains in ``services`` temporarily to preserve existing
imports while callers migrate to the repository-oriented module name.
"""

from ferret.apps.session.services import SessionRepository, normalize_session_name

__all__ = ["SessionRepository", "normalize_session_name"]
