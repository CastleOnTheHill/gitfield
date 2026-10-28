class GitfieldError(Exception):
    """An expected error that should be reported without a traceback."""


class GitCommandError(GitfieldError):
    """A git command failed."""


class RosterError(GitfieldError):
    """The responsibility roster is invalid."""


class DatabaseError(GitfieldError):
    """The local database cannot be used."""
