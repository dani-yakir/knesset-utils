"""Foreign-key relationships for the mirrored tables.

The OData v4 $metadata for this API declares NavigationProperty relationships
but no ReferentialConstraint elements, so there's no machine-readable binding
from a scalar FK-shaped column (e.g. "PersonID") to the property it targets.
This module reconstructs that mapping two ways:

1. Automatic, by naming convention: a non-key column ending in "Id"/"ID" whose
   name (minus the suffix) exactly matches another entity set's name (e.g.
   "CommitteeID" -> KNS_Committee) is treated as a real FK. High confidence.

2. MANUAL_FK_OVERRIDES below: a small, deliberately conservative set of
   columns that don't match by naming convention but are real FKs based on
   direct inspection of sample data (e.g. KNS_PlenumVoteResult.MkId ->
   KNS_Person -- "Mk" = Member of Knesset, confirmed by the row carrying
   denormalized LastName/FirstName alongside it).

Deliberately NOT included, even though they end in "Id"/"ID":
- Columns with an inline "*Desc" sibling in the same table (e.g. Bill.TypeID
  + Bill.TypeDesc) -- these are self-contained enum/code values, not
  cross-table foreign keys. Verified by direct inspection, not just absence
  of a name match.
- Columns that look polymorphic: KNS_CmtSessionItem.ItemID,
  KNS_PlmSessionItem.ItemID, KNS_PlenumVote.ItemID, KNS_PlenumVoteResult.ItemID
  -- these tables also carry an ItemTypeID, suggesting ItemID's target table
  depends on a type discriminator rather than being a single fixed table.
  Guessing a single target here would risk reporting false orphans against
  the wrong table, so they're left unvalidated rather than guessed.
- Everything else with no naming match and no confident domain reading (law/
  law-type cross-references, site-code external mappings, a few others) --
  left unvalidated. See docs/fk_integrity.md for the full accounting.
"""
from __future__ import annotations

from knesset_utils.schema.metadata import EntityDef

MANUAL_FK_OVERRIDES: dict[tuple[str, str], str] = {
    ("KNS_PlenumVoteResult", "MkId"): "KNS_Person",
    ("KNS_PlenumVoteResult", "VoteID"): "KNS_PlenumVote",
    ("KNS_PlenumVoteResult", "SessionID"): "KNS_PlenumSession",
    ("KNS_PlenumVote", "SessionID"): "KNS_PlenumSession",
    ("KNS_Agenda", "InitiatorPersonID"): "KNS_Person",
    ("KNS_Agenda", "MinisterPersonID"): "KNS_Person",
    ("KNS_Agenda", "RecommendCommitteeID"): "KNS_Committee",
    ("KNS_Agenda", "LeadingAgendaID"): "KNS_Agenda",
    ("KNS_Committee", "ParentCommitteeID"): "KNS_Committee",
    ("KNS_BillSplit", "MainBillID"): "KNS_Bill",
    ("KNS_BillSplit", "SplitBillID"): "KNS_Bill",
    ("KNS_BillUnion", "MainBillID"): "KNS_Bill",
    ("KNS_BillUnion", "UnionBillID"): "KNS_Bill",
    ("KNS_JointCommittee", "ParticipantCommitteeID"): "KNS_Committee",
    ("KNS_IsraelLawBinding", "IsraelLawReplacedID"): "KNS_IsraelLaw",
    ("KNS_SecLawAuthorizingLaw", "AuthorizingLawID"): "KNS_IsraelLaw",
    ("KNS_SecondaryLaw", "MajorAuthorizingLawID"): "KNS_IsraelLaw",
    ("KNS_SecToSecBinding", "SecChildID"): "KNS_SecondaryLaw",
    ("KNS_SecToSecBinding", "SecParentID"): "KNS_SecondaryLaw",
    ("KNS_SecToSecBinding", "SecMainID"): "KNS_SecondaryLaw",
}


def find_foreign_keys(entities: dict[str, EntityDef]) -> list[tuple[str, str, str]]:
    """Returns (source_table, fk_column, target_table) triples: naming-convention
    matches plus the manual overrides above. Both source and target must be
    present in `entities` (i.e. actually mirrored, not a broken/excluded entity set).
    """
    table_names = set(entities.keys())
    stems = {name[4:] if name.startswith("KNS_") else name: name for name in table_names}

    fks: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()

    for tname, edef in entities.items():
        for col in edef.columns:
            if col.name == edef.key:
                continue
            if not (col.name.endswith("Id") or col.name.endswith("ID")):
                continue
            stem = col.name[:-2]
            target = stems.get(stem)
            if target and target in table_names:
                fks.append((tname, col.name, target))
                seen.add((tname, col.name))

    for (tname, cname), target in MANUAL_FK_OVERRIDES.items():
        if (tname, cname) in seen:
            continue
        if tname in entities and target in entities:
            fks.append((tname, cname, target))

    return fks
