"""Mapping git author identities to employee ids."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional, Set, Tuple

from .errors import RosterError
from .roster import AuthorMap, Roster

_TOKEN_SPLIT_RE = re.compile(r"[^0-9A-Za-z]+")


@dataclass(frozen=True)
class Resolution:
    employee_id: Optional[str]
    basis: str
    candidates: Tuple[str, ...] = ()


class EmployeeResolver:
    """Resolve git names/emails with explicit aliases taking precedence.

    Supported automatic conventions include an email local part equal to the
    employee id, an id token such as ``1001+ci`` or ``alice.1001``, and a git
    name containing the id.  Ambiguous automatic matches remain unresolved
    instead of being guessed.
    """

    def __init__(self, roster: Roster, author_map: Optional[AuthorMap] = None) -> None:
        self.roster = roster
        self.employee_ids = tuple(sorted(roster.employees))
        self._email_aliases: Dict[str, str] = {}
        self._name_aliases: Dict[str, str] = {}
        self._cache: Dict[Tuple[str, str], Resolution] = {}

        self._merge_aliases(dict(roster.email_aliases), dict(roster.name_aliases))
        if author_map is not None:
            self._merge_aliases(
                dict(author_map.email_aliases), dict(author_map.name_aliases)
            )

    def _merge_aliases(
        self, emails: Dict[str, str], names: Dict[str, str]
    ) -> None:
        for email, employee_id in emails.items():
            existing = self._email_aliases.get(email)
            if existing is not None and existing != employee_id:
                raise RosterError(
                    f"email alias {email!r} maps to both {existing!r} and "
                    f"{employee_id!r}"
                )
            self._email_aliases[email.strip().lower()] = employee_id
        for name, employee_id in names.items():
            key = name.strip().casefold()
            existing = self._name_aliases.get(key)
            if existing is not None and existing != employee_id:
                raise RosterError(
                    f"name alias {name!r} maps to both {existing!r} and "
                    f"{employee_id!r}"
                )
            self._name_aliases[key] = employee_id

    def _inferred_candidates(self, name: str, email: str) -> Set[str]:
        candidates: Set[str] = set()
        local = email.split("@", 1)[0].strip().casefold() if email else ""
        local_tokens = {token for token in _TOKEN_SPLIT_RE.split(local) if token}
        name_tokens = {
            token.casefold()
            for token in _TOKEN_SPLIT_RE.split(name or "")
            if token
        }

        for employee_id in self.employee_ids:
            normalised = employee_id.strip().casefold()
            if not normalised:
                continue
            if local and local == normalised:
                candidates.add(employee_id)
                continue
            if normalised in local_tokens or normalised in name_tokens:
                candidates.add(employee_id)
                continue
            if local.startswith(normalised) and len(local) > len(normalised):
                delimiter = local[len(normalised)]
                if delimiter in "+._-":
                    candidates.add(employee_id)

        return candidates

    def resolve(self, name: str, email: str) -> Resolution:
        normalised_email = (email or "").strip().lower()
        normalised_name = (name or "").strip()
        cache_key = (normalised_name, normalised_email)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        employee_id = self._email_aliases.get(normalised_email)
        if employee_id:
            resolution = Resolution(employee_id, "explicit_email")
        else:
            employee_id = self._name_aliases.get(normalised_name.casefold())
            if employee_id:
                resolution = Resolution(employee_id, "explicit_name")
            else:
                candidates = self._inferred_candidates(normalised_name, normalised_email)
                if len(candidates) == 1:
                    resolution = Resolution(next(iter(candidates)), "inferred")
                elif len(candidates) > 1:
                    resolution = Resolution(
                        None, "ambiguous", tuple(sorted(candidates))
                    )
                else:
                    resolution = Resolution(None, "unresolved")

        self._cache[cache_key] = resolution
        return resolution
