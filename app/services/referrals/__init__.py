"""Finding a referral in the user's own network (F10, ADR-042).

Two modules, and a hard boundary round both: **nothing here fetches LinkedIn.** Hard rule 4 says
`linkedin_url` is stored and displayed, never fetched, and ADR-038 added that LinkedIn data
enters only through a file the user uploads. `parsing` reads that file; `finder` matches it
against a company and builds a note the user sends themselves.

`tests/unit/test_referrals_never_fetch.py` asserts the boundary over the whole tree rather than
leaving it to care.
"""
