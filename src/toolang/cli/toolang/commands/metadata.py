"""Lightweight metadata shared by Toolang command entry points."""

QUERY_HELP = """Show collection-query syntax and fields.

QUERY = MATCH ("," MATCH)*
MATCH = IDENTITY-PATTERN? PREDICATE-BLOCK?

An identity pattern and its predicates are intersected. Predicates in one
block are intersected. Comma-separated matches and repeated --query options
form a stable, deduplicated union.

Bare identities are case-sensitive globs; JSON-quoted identities are exact.
Boolean fields accept positive or negated flags inside a predicate block.
Other predicates use =, !=, ~=, !~=, <, <=, >, >=, in, or not in as allowed
by the field type.

Collections: models, tools, psyches, skills, services, prompts.
Run `too query COLLECTION` for its identity and predicate fields.
"""

__all__ = ["QUERY_HELP"]
