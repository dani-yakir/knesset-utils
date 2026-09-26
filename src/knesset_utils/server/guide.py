"""Orientation text for LLM clients: server instructions and one-line table descriptions.

Everything here documents the mirror's structure and known data quirks; it is served as
the MCP `instructions` field and merged into `list_tables` output so an agent can plan
queries without first describing every table.
"""
from __future__ import annotations

INSTRUCTIONS = """\
SQLite mirror of the Israeli Knesset's official open-data API (tables KNS_*). All text values
(names, titles, statuses, descriptions) are in HEBREW.

Suggested workflow: list_tables (purpose + row counts) -> describe_table on the few tables you
need, several at once (shows foreign keys, value distributions, and decodes code columns into
their Hebrew labels) -> query_sql. Use find_person to turn a person's name into KNS_Person.Id.

Core model:
- KNS_Person: one row per person. KNS_PersonToPosition is the hub for every role a person held:
  PositionID -> KNS_Position (MK, minister, deputy minister, PM, Speaker, committee chair or
  member, faction member...), with KnessetNum, GovernmentNum, FactionID/FactionName,
  CommitteeID, GovMinistryID, StartDate/FinishDate and IsCurrent.
- Status codes (StatusID) of bills, queries, agenda items, sessions -> KNS_Status.
- Most entity tables carry KnessetNum (the Knesset term, 1..25; 25 is the current one).
  KNS_PlenumVote has none: join SessionID -> KNS_PlenumSession.KnessetNum, or filter on dates.
- Plenum votes: KNS_PlenumVote has one row per vote (a bill reading, a reservation
  "הסתייגות", a motion...). What voting "for" means is in ForOptionDesc (e.g. passing a
  reading, expressing no confidence in the government), which is the reliable way to classify
  votes. IsNoConfidenceInGov is almost always NULL. KNS_PlenumVoteResult has one row per MK per
  vote (ResultDesc: for/against/abstain/present) with the MK's name inline.
- Bills: KNS_Bill (SubTypeDesc = government/private/committee), initiators in
  KNS_BillInitiator (IsInitiator=1 initiator, 0 = joined later).
- Dates are ISO-8601 text with a timezone offset, so compare them as strings
  ('2025-01-01' <= d < '2026-01-01').
- Foreign keys in the source are not fully reliable; prefer LEFT JOIN when counting.
- A law in force (KNS_IsraelLaw) links to the bill that enacted it via KNS_LawBinding
  (IsraelLawID -> LawID = KNS_Bill.Id, BindingTypeDesc 'החוק המקורי'). A bill's plenum votes
  are the KNS_PlenumVote rows with ItemID = the bill's Id.

Citing sources: whenever an answer refers to specific plenum votes, bills or laws, call
get_vote_official_link / get_bill_official_link (they take lists of ids) and include the
official knesset.gov.il URLs in the answer.
"""

TABLE_DESCRIPTIONS: dict[str, str] = {
    "KNS_Agenda": "Motions for the agenda (הצעות לסדר היום) and their status/initiator",
    "KNS_Bill": "Bills (הצעות חוק): Knesset term, government/private/committee subtype, status, publication",
    "KNS_BillHistoryInitiator": "Historical changes to bill initiators",
    "KNS_BillInitiator": "Bill <-> person initiators (IsInitiator=1) and co-signers who joined later",
    "KNS_BillName": "Historical names of bills",
    "KNS_BillSplit": "Bills split into several bills",
    "KNS_BillUnion": "Bills merged together",
    "KNS_BroadcastCommitteSession": "Broadcast URLs of committee sessions",
    "KNS_CmtSessionItem": "Agenda items discussed in committee sessions",
    "KNS_CmtSiteCode": "Committee id -> Knesset website code mapping",
    "KNS_Committee": "Committees per Knesset term (name, type, parent, dates)",
    "KNS_CommitteeSession": "Committee sessions/meetings (committee, date, status incl. cancelled)",
    "KNS_DocumentAgenda": "Documents attached to agenda motions",
    "KNS_DocumentBill": "Documents attached to bills",
    "KNS_DocumentCommitteeSession": "Documents (protocols etc.) of committee sessions",
    "KNS_DocumentIsraelLaw": "Documents of laws (empty)",
    "KNS_DocumentPlenumSession": "Documents (protocols etc.) of plenum sessions",
    "KNS_DocumentSecondaryLaw": "Documents of secondary legislation",
    "KNS_Faction": "Factions (parliamentary groups) per Knesset term",
    "KNS_GovMinistry": "Government ministries",
    "KNS_IsraelLaw": "Laws of Israel in force or repealed (IsBasicLaw, validity status, dates)",
    "KNS_IsraelLawBinding": "Links between laws that replace each other",
    "KNS_IsraelLawClassificiation": "Subject classification of laws",
    "KNS_IsraelLawLawCorrections": "Law <-> law-correction links",
    "KNS_IsraelLawMinistry": "Ministries responsible for laws",
    "KNS_IsraelLawName": "Historical names of laws",
    "KNS_ItemType": "Item type codes (query, bill, agenda, session...) used by *ItemTypeID columns",
    "KNS_JointCommittee": "Joint committees and their participating committees",
    "KNS_KnessetDates": "Knesset terms and their sittings (assembly/plenum start and end dates)",
    "KNS_LawBinding": "Bills/laws amending or binding to laws of Israel",
    "KNS_LawCorrections": "Corrections to laws",
    "KNS_MkSiteCode": "Person id -> Knesset website code mapping",
    "KNS_Person": "People (MKs, ministers...): Hebrew name, gender, IsCurrent",
    "KNS_PersonToPosition": "Every role a person held: MK, minister, PM, Speaker, faction, committee... (the roles hub)",
    "KNS_PlenumSession": "Plenum sessions (date, Knesset term)",
    "KNS_PlenumVote": "Plenum votes: one row per vote; ForOptionDesc says what 'for' means",
    "KNS_PlenumVoteResult": "How each MK voted in each plenum vote (~2M rows)",
    "KNS_PlmSessionItem": "Agenda items of plenum sessions",
    "KNS_Position": "Position codes (MK, minister, Speaker, ...) used by KNS_PersonToPosition.PositionID",
    "KNS_Query": "Parliamentary questions to ministers (שאילתות): asker, ministry, type, status",
    "KNS_SecLawAuthorizingLaw": "Secondary legislation <-> authorizing law",
    "KNS_SecLawRegulator": "Regulators of secondary legislation",
    "KNS_SecToSecBinding": "Links between pieces of secondary legislation",
    "KNS_SecondaryLaw": "Secondary legislation (regulations, orders)",
    "KNS_Status": "Status codes used by StatusID columns (bills, queries, agenda, sessions)",
}
