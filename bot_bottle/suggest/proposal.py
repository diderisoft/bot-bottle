"""The Proposal type: a typed bottle suggestion (PRD 0117).

A proposal is always produced as structured data, never free YAML, so the
manifest loader can validate it and the pretty view can preview it.

Allowed option values (preference order, stopping at first that fits):
  reuse           an existing bottle as is
  stack           existing bottles stacked
  stack+additions stacked bottles plus new routes/grants/env names
  new-extending   a new bottle extending the closest existing one
  new             a new bottle from scratch (extends basic or nothing)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

OPTION_REUSE = "reuse"
OPTION_STACK = "stack"
OPTION_STACK_ADDITIONS = "stack+additions"
OPTION_NEW_EXTENDING = "new-extending"
OPTION_NEW = "new"

OPTION_VALUES = (
    OPTION_REUSE,
    OPTION_STACK,
    OPTION_STACK_ADDITIONS,
    OPTION_NEW_EXTENDING,
    OPTION_NEW,
)


@dataclass
class ProposalEgress:
    """One proposed egress route addition."""

    host: str
    methods: list[str] = field(default_factory=list)
    why: str = ""


@dataclass
class ProposalRepo:
    """One proposed repo grant addition, e.g. `gitea.example.com/owner/repo:write`."""

    grant: str
    why: str = ""


@dataclass
class ProposalEnv:
    """One proposed env var name (value comes from the secrets file at launch)."""

    name: str
    why: str = ""


@dataclass
class ProposalCredential:
    """One proposed credential: a secret name injected on a specific route."""

    name: str   # env var name in the host secrets file
    route: str  # the egress host it's injected on
    why: str = ""


@dataclass
class ProposalAdditions:
    """Elements to add on top of the stacked bottles."""

    egress: list[ProposalEgress] = field(default_factory=list)
    repos: list[ProposalRepo] = field(default_factory=list)
    env: list[ProposalEnv] = field(default_factory=list)
    credentials: list[ProposalCredential] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.egress or self.repos or self.env or self.credentials)


@dataclass
class ProposalNewBottle:
    """Metadata for the new bottle to create (option new-extending or new)."""

    name: str
    extends: str = ""
    about: str = ""


@dataclass
class ProposalEdit:
    """A change to a bottle other than this run's (PRD 0118): `bottle` is a
    host bottle's name, a project bottle (`./main`) or `new:<name>` (a new
    host bottle, `extends` its parent). `add` holds what it gains, typed as
    additions are; `forges` describes the forges its repos are on (ADR 0019).
    Written only once the user accepts it (`suggest.edits.accept`)."""

    bottle: str
    add: ProposalAdditions
    why: str = ""
    extends: str = ""
    forges: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        add = _additions_dict(self.add)
        if self.forges:
            add["forges"] = {h: dict(d) for h, d in self.forges.items()}
        out: dict[str, Any] = {"bottle": self.bottle, "add": add, "why": self.why}
        if self.extends:
            out["extends"] = self.extends
        return out


@dataclass
class ProposalSimilar:
    """An existing bottle that is close to what the proposal needs."""

    bottle: str
    covers: float = 0.0  # fraction of proposal elements it already provides


@dataclass
class Proposal:
    """A typed bottle suggestion for a task.

    `bottles` names bottles that already exist on this host; the proposal asks
    the user to stack them.  `new_bottle` carries a name/extends/about for
    options new-extending and new — the caller writes the file.  `additions`
    are elements beyond what the stacked bottles already provide.  `similar`
    are existing bottles that nearly fit and might be used instead.  `notes`
    are free text for caveats the typed fields can't express. `source` is
    the path that produced it, `architect` or `rules` (set by
    `suggest.flow`, never by the model). `blocked`, when set, says why it
    can't run on this host (a secret it needs isn't set; `runnable`).
    `summary` is the architect's one plain sentence on what it recommends
    ("" when it gave none; `summary_line` falls back to one made here).
    `forges` describes each forge a repo grant is on that this host hasn't
    connected yet (set by validation, never by the model; `suggest.forges`):
    the review offers to connect it, and a saved bottle carries it.
    """

    option: str
    bottles: list[str]
    new_bottle: ProposalNewBottle | None
    additions: ProposalAdditions
    similar: list[ProposalSimilar]
    notes: list[str]
    source: str = ""
    blocked: str = ""
    summary: str = ""
    forges: dict[str, dict[str, Any]] = field(default_factory=dict)
    edits: list[ProposalEdit] = field(default_factory=list)

    # --- serialisation --------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """A JSON-compatible dict, suitable for `json.dumps`."""
        out: dict[str, Any] = {
            "option": self.option,
            "bottles": list(self.bottles),
            "new_bottle": (
                {"name": self.new_bottle.name, "extends": self.new_bottle.extends,
                 "about": self.new_bottle.about}
                if self.new_bottle else None
            ),
            "additions": _additions_dict(self.additions),
            "similar": [{"bottle": s.bottle, "covers": s.covers}
                        for s in self.similar],
            "notes": list(self.notes),
        }
        if self.source:
            out["source"] = self.source
        if self.blocked:
            out["blocked"] = self.blocked
        if self.summary:
            out["summary"] = self.summary
        if self.forges:
            out["forges"] = {h: dict(d) for h, d in self.forges.items()}
        if self.edits:
            out["edits"] = [e.to_dict() for e in self.edits]
        return out

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    # --- parsing --------------------------------------------------------

    @classmethod
    def from_dict(cls, d: object) -> "Proposal":
        """Parse and validate a proposal from a dict.  Raises ValueError on
        invalid input."""
        if not isinstance(d, dict):
            raise ValueError(f"proposal must be an object (was {type(d).__name__})")
        option = d.get("option", "")
        if not isinstance(option, str) or option not in OPTION_VALUES:
            raise ValueError(
                f"proposal.option must be one of "
                f"{', '.join(OPTION_VALUES)} (was {option!r})"
            )
        bottles = d.get("bottles", [])
        if not isinstance(bottles, list):
            raise ValueError("proposal.bottles must be an array")
        bottles = [str(b) for b in bottles]

        new_bottle_raw = d.get("new_bottle")
        new_bottle: ProposalNewBottle | None = None
        if new_bottle_raw is not None:
            if not isinstance(new_bottle_raw, dict):
                raise ValueError("proposal.new_bottle must be an object or null")
            name = str(new_bottle_raw.get("name", ""))
            if not name:
                raise ValueError("proposal.new_bottle.name must be a non-empty string")
            new_bottle = ProposalNewBottle(
                name=name,
                extends=str(new_bottle_raw.get("extends", "")),
                about=str(new_bottle_raw.get("about", "")),
            )
        if option in (OPTION_NEW_EXTENDING, OPTION_NEW) and new_bottle is None:
            raise ValueError(
                f"proposal.new_bottle is required when option is {option!r}"
            )

        raw_add = d.get("additions", {})
        if not isinstance(raw_add, dict):
            raise ValueError("proposal.additions must be an object")
        additions = _parse_additions(raw_add)

        raw_similar = d.get("similar", [])
        if not isinstance(raw_similar, list):
            raise ValueError("proposal.similar must be an array")
        similar = [
            ProposalSimilar(
                bottle=str(s.get("bottle", "")) if isinstance(s, dict) else str(s),
                covers=float(s.get("covers", 0.0)) if isinstance(s, dict) else 0.0,
            )
            for s in raw_similar
        ]

        raw_notes = d.get("notes", [])
        if not isinstance(raw_notes, list):
            raise ValueError("proposal.notes must be an array")
        notes = [str(n) for n in raw_notes]

        summary = d.get("summary", "")
        raw_forges = d.get("forges", {})
        forges = ({str(h): dict(v) for h, v in raw_forges.items() if isinstance(v, dict)}
                  if isinstance(raw_forges, dict) else {})
        return cls(option=option, bottles=bottles, new_bottle=new_bottle,
                   additions=additions, similar=similar, notes=notes,
                   summary=" ".join(summary.split()) if isinstance(summary, str) else "",
                   forges=forges, edits=_parse_edit_list(d.get("edits", [])))

    @classmethod
    def from_json(cls, text: str) -> "Proposal":
        """Parse from a JSON string, raising ValueError on failure."""
        try:
            d = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"invalid JSON: {e}") from e
        return cls.from_dict(d)


# --- helpers ----------------------------------------------------------------


def _additions_dict(add: ProposalAdditions) -> dict[str, Any]:
    return {
        "egress": [{"host": e.host, "methods": list(e.methods), "why": e.why}
                   for e in add.egress],
        "repos": [{"grant": r.grant, "why": r.why} for r in add.repos],
        "env": [{"name": v.name, "why": v.why} for v in add.env],
        "credentials": [{"name": c.name, "route": c.route, "why": c.why}
                        for c in add.credentials],
    }


def _parse_additions(raw: dict[str, Any]) -> ProposalAdditions:
    return ProposalAdditions(
        egress=_parse_egress_list(raw.get("egress", [])),
        repos=_parse_repo_list(raw.get("repos", [])),
        env=_parse_env_list(raw.get("env", [])),
        credentials=_parse_credential_list(raw.get("credentials", [])),
    )


def _parse_edit_list(raw: object) -> list[ProposalEdit]:
    """`edits`, skipping any that don't name a bottle or add nothing."""
    if not isinstance(raw, list):
        raise ValueError("proposal.edits must be an array")
    out = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("bottle"), str):
            continue
        add = item.get("add")
        add = add if isinstance(add, dict) else {}
        forges = add.get("forges")
        edit = ProposalEdit(
            bottle=item["bottle"].strip(), add=_parse_additions(add),
            why=str(item.get("why", "")), extends=str(item.get("extends", "") or ""),
            forges=({str(h): dict(v) for h, v in forges.items() if isinstance(v, dict)}
                    if isinstance(forges, dict) else {}))
        if edit.bottle and not edit.add.is_empty():
            out.append(edit)
    return out


def _parse_egress_list(raw: object) -> list[ProposalEgress]:
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        host = str(item.get("host", ""))
        if not host:
            continue
        methods = [str(m) for m in item.get("methods", []) if isinstance(item.get("methods"), list)]
        result.append(ProposalEgress(host=host, methods=methods, why=str(item.get("why", ""))))
    return result


def _parse_repo_list(raw: object) -> list[ProposalRepo]:
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        grant = str(item.get("grant", ""))
        if not grant:
            continue
        result.append(ProposalRepo(grant=grant, why=str(item.get("why", ""))))
    return result


def _parse_env_list(raw: object) -> list[ProposalEnv]:
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", ""))
        if not name:
            continue
        result.append(ProposalEnv(name=name, why=str(item.get("why", ""))))
    return result


def _parse_credential_list(raw: object) -> list[ProposalCredential]:
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", ""))
        route = str(item.get("route", ""))
        if not name or not route:
            continue
        result.append(ProposalCredential(name=name, route=route,
                                         why=str(item.get("why", ""))))
    return result


__all__ = [
    "OPTION_NEW",
    "OPTION_NEW_EXTENDING",
    "OPTION_REUSE",
    "OPTION_STACK",
    "OPTION_STACK_ADDITIONS",
    "OPTION_VALUES",
    "Proposal",
    "ProposalAdditions",
    "ProposalCredential",
    "ProposalEdit",
    "ProposalEgress",
    "ProposalEnv",
    "ProposalNewBottle",
    "ProposalRepo",
    "ProposalSimilar",
]
