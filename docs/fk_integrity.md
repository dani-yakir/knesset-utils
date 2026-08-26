# Foreign key integrity

The OData API declares no formal foreign keys (no `ReferentialConstraint` elements in
`$metadata` — only `NavigationProperty` relationships, which don't bind to a specific scalar
column). This document records how the FK set checked here was derived, and the actual
results of validating it against the mirror on 2026-08-26.

Run `knesset-utils validate-fks` to reproduce.

## How the FK set was derived

`src/knesset_utils/schema/foreign_keys.py` builds the checked FK list two ways:

1. **Naming convention** (44 relationships): a non-key column ending in `Id`/`ID` whose name,
   minus that suffix, exactly matches another table's name (e.g. `CommitteeID` → `KNS_Committee`).
   High confidence.
2. **Manual review** (20 more, 64 total): columns that don't match by naming convention but were
   confirmed as real FKs by inspecting sample data — most notably `KNS_PlenumVoteResult.MkId` →
   `KNS_Person` (`Mk` = Member of Knesset; confirmed because the row also carries denormalized
   `LastName`/`FirstName` matching the referenced person).

**Deliberately left unvalidated** (not guessed):
- 50 columns with an inline `*Desc` sibling in the same table (e.g. `Bill.TypeID` +
  `Bill.TypeDesc`) — these are self-contained enum/code values, not cross-table FKs. Verified
  by direct inspection of several, not assumed from the absence of a name match.
- ~24 columns with no naming match, no `*Desc` sibling, and no confident target: the four
  `ItemID` columns (`KNS_CmtSessionItem`, `KNS_PlmSessionItem`, `KNS_PlenumVote`,
  `KNS_PlenumVoteResult`) look polymorphic — each of those tables also carries an `ItemTypeID`,
  suggesting the actual target table depends on a type discriminator rather than being fixed.
  Guessing a single target would risk reporting false orphans against the wrong table, so these
  are left unchecked rather than guessed. Also unvalidated: site-code cross-references to
  external systems (`KNS_CmtSiteCode`/`KNS_MkSiteCode`'s `KnsID`/`SiteId`), and several
  law/law-type cross-references with no confident reading (`KNS_LawBinding.LawID`/`ParentLawID`,
  `KNS_IsraelLawBinding.LawID`, etc).

## Results: 64 checked, 54 clean, 10 with orphans

All orphan counts below were independently confirmed against the **live** API (not just checked
against our own mirror) for the two largest cases — the missing target rows genuinely don't
exist upstream either. This isn't a sync gap; it's real dangling data in the source.

| Source.Column → Target | Orphan rows / total | % | Distinct missing values | Read |
|---|---:|---:|---:|---|
| `KNS_PlenumVoteResult.MkId` → `KNS_Person` | 631,655 / 1,953,709 | 32.3% | 171 | **Genuine.** See example below. |
| `KNS_DocumentAgenda.AgendaID` → `KNS_Agenda` | 17,253 / 27,535 | 62.7% | 14,612 | **Genuine.** See example below. |
| `KNS_Bill.CommitteeID` → `KNS_Committee` | 19,872 / 34,661 | 57.3% | **1** (`-1`) | Sentinel value, not real corruption — `-1` clearly means "no committee assigned." |
| `KNS_CmtSessionItem.ItemTypeID` → `KNS_ItemType` | 28,012 / 80,678 | 34.7% | **1** (`11`) | Likely an incomplete lookup table (`KNS_ItemType` has only 23 rows) rather than corrupt data — one code, used a lot. |
| `KNS_PlmSessionItem.ItemTypeID` → `KNS_ItemType` | 11,293 / 168,866 | 6.7% | 3 (`9`, `11`, `950`) | Same read as above — a few item-type codes missing from the small lookup table. |
| `KNS_SecLawAuthorizingLaw.AuthorizingLawID` → `KNS_IsraelLaw` | 961 / 68,307 | 1.4% | 112 | Not yet characterized further — plausibly genuine (laws outside `KNS_IsraelLaw`'s coverage), not investigated as deeply as the top two. |
| `KNS_PlenumVoteResult.VoteID` → `KNS_PlenumVote` | 2,764 / 1,953,709 | 0.1% | 61 | Not yet characterized further. |
| `KNS_IsraelLawName.IsraelLawID` → `KNS_IsraelLaw` | 1 / 2,180 | ~0% | 1 | Single dangling reference. |
| `KNS_SecToSecBinding.SecParentID` → `KNS_SecondaryLaw` | 1 / 23,763 | ~0% | 1 | Single dangling reference. |
| `KNS_SecToSecBinding.SecMainID` → `KNS_SecondaryLaw` | 1 / 23,763 | ~0% | 1 | Single dangling reference (same underlying row as `SecParentID`, worth noting). |

### Example: `KNS_PlenumVoteResult.MkId` — genuine, not old/legacy data

The original hypothesis was "old MKs from before digitization, never entered into `KNS_Person`."
**Wrong** — orphaned rows span **2015-09-02 to 2026-07-28** (essentially current), overlapping
almost the entire range of non-orphaned rows (1982-2026). This is an ongoing gap, not a historical
artifact.

Concrete example: `MkId=30216` has no row in `KNS_Person` (confirmed live, not just locally), yet
carries real votes:

```json
{
  "Id": 1050731, "MkId": 30216, "VoteID": 31710, "VoteDate": "2019-05-21T01:30:23+03:00",
  "ResultDesc": "נגד", "LastName": "גבאי", "FirstName": "אבי",
  "SessionID": 2080341, "ItemID": 2080479
}
```

`LastName`/`FirstName` ("Avi Gabbay," a real, identifiable MK) are denormalized directly into the
vote row, so the vote data itself is intact and readable — it's specifically the join back to
`KNS_Person` that's broken. 171 distinct MK ids show this pattern.

### Example: `KNS_DocumentAgenda.AgendaID` — genuine, majority of the table

62.7% of `KNS_DocumentAgenda` rows (17,253 of 27,535) point at an `AgendaID` with no row in
`KNS_Agenda` — 14,612 distinct missing ids, e.g. `9703, 9707, 9726, 9728, 9730`. Confirmed live:
none of these exist in `KNS_Agenda` right now either. Given `KNS_Agenda` only has 22,072 rows
total and these missing ids are low, pre-shared-sequence-looking numbers (compare to the ~2.2M
range seen elsewhere), the likely explanation is that older agenda items were never migrated into
the current `KNS_Agenda` master table while documents still reference them — but this is a read,
not confirmed the way the MkId case was.

## Not yet done

- The 3 mid-tier cases (`AuthorizingLawID`, `VoteID`, `ItemTypeID` cases) weren't characterized
  as deeply as the top two — no live-existence check, no theory beyond "not yet investigated."
- No investigation of *why* the Knesset's own data has these gaps (no access to their systems).
- No handling of this in the sync/schema layer yet — this is a read-only integrity report, not
  a constraint enforced anywhere. Consumers of the mirror (including the stats layer) need to be
  aware that a `JOIN` on `KNS_PlenumVoteResult.MkId` will silently drop ~32% of vote rows if done
  as an inner join.
