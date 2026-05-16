"""
SQL Query Normalizer
--------------------
Turns raw SQL queries into canonical "fingerprints" by replacing all literal
values (numbers, strings, booleans) with a placeholder token.

Two queries with the same fingerprint are considered structurally identical —
only their filter values differ.
"""

import re
import hashlib

try:
    import sqlglot
    from sqlglot import exp as sqlexp
    SQLGLOT_AVAILABLE = True
except ImportError:
    SQLGLOT_AVAILABLE = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def normalize(sql: str) -> str:
    """
    Return a canonical fingerprint for *sql*.

    Steps:
      1. Strip comments
      2. Try sqlglot parse → re-emit canonical SQL (handles keyword casing,
         whitespace, operator spacing, etc.)
      3. Replace all remaining string and numeric literals with '?'
      4. Collapse whitespace
    """
    if not isinstance(sql, str):
        return ""
    sql = sql.strip()
    if not sql:
        return ""

    # Step 1: strip comments
    sql = _strip_comments(sql)

    # Step 2: structural normalisation via sqlglot
    if SQLGLOT_AVAILABLE:
        try:
            tree = sqlglot.parse_one(sql)
            # Replace every Literal / Boolean node in-place
            tree = tree.transform(_replace_literal_node)
            canonical = tree.sql()          # re-emit with sqlglot defaults
        except Exception:
            canonical = sql
    else:
        canonical = sql

    canonical = canonical.lower()

    # Step 3: catch any remaining quoted strings or bare numbers that
    #         sqlglot left behind (e.g. parse failures, exotic dialects)
    canonical = re.sub(r"'(?:[^'\\]|\\.)*'", "?", canonical)   # 'string'
    canonical = re.sub(r"(?<![.\w])\d+(?:\.\d+)?(?![.\w])", "?", canonical)

    # Step 4: collapse whitespace
    canonical = " ".join(canonical.split())
    return canonical


def fingerprint_id(normalized_sql: str) -> str:
    """Short 8-char hex hash of the normalised query — useful as a group key."""
    return hashlib.md5(normalized_sql.encode()).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _strip_comments(sql: str) -> str:
    """Remove -- line comments and /* block comments */."""
    # block comments
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    # line comments
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def _replace_literal_node(node):
    """sqlglot transform callback: swap every Literal/Boolean for '?'."""
    if isinstance(node, sqlexp.Literal):
        # Return a raw anonymous token so sqlglot emits exactly: ?
        return sqlexp.Anonymous(this="?", expressions=[])
    if isinstance(node, sqlexp.Boolean):
        return sqlexp.Anonymous(this="?", expressions=[])
    return node
