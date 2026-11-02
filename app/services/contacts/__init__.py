"""Hiring-contact lookups on the user's own provider keys (F9, ADR-041).

Three modules, split by what each one is allowed to touch:

- `providers` — the network, and nothing about our database. One function per provider reads its
  response shape, so a shape that changes is one function to correct.
- `keys` — the credential: sealed on the way in, never returned to a route.
- `lookup` — the orchestration, the refusals, the budget and the rows. The only entry point, and
  reachable only from a route a person called.
"""
