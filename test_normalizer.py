"""
Tests for normalizer.py
Run after setup:  source venv/bin/activate && python test_normalizer.py
Or with pytest:   pytest test_normalizer.py -v
"""

import hashlib
import pandas as pd
import pytest
from normalizer import normalize, fingerprint_id


# ── Helpers ──────────────────────────────────────────────────────────────────

def same_pattern(*queries):
    """Assert all queries normalise to the same fingerprint."""
    norms = [normalize(q) for q in queries]
    assert len(set(norms)) == 1, (
        f"Expected same pattern but got {len(set(norms))} distinct:\n"
        + "\n".join(f"  {q!r} → {n!r}" for q, n in zip(queries, norms))
    )

def diff_pattern(*queries):
    """Assert all queries produce distinct fingerprints."""
    norms = [normalize(q) for q in queries]
    assert len(set(norms)) == len(queries), (
        f"Expected {len(queries)} distinct patterns but got {len(set(norms))}:\n"
        + "\n".join(f"  {q!r} → {n!r}" for q, n in zip(queries, norms))
    )


# ── Unit tests ────────────────────────────────────────────────────────────────

class TestNumericLiterals:
    def test_integer(self):
        assert normalize("select * from t where a = 10") == "select * from t where a = ?"

    def test_different_integers_same_pattern(self):
        same_pattern(
            "select * from t where a = 10",
            "select * from t where a = 99999",
        )

    def test_float(self):
        assert normalize("select * from t where price > 9.99") == "select * from t where price > ?"

    def test_negative_number(self):
        n = normalize("select * from t where temp < -5")
        assert "?" in n

    def test_multiple_numbers(self):
        assert normalize("select * from t where a = 1 and b = 2") == \
               "select * from t where a = ? and b = ?"


class TestStringLiterals:
    def test_single_quoted(self):
        assert normalize("select * from t where country = 'us'") == \
               "select * from t where country = ?"

    def test_different_strings_same_pattern(self):
        same_pattern(
            "select * from t where country = 'us'",
            "select * from t where country = 'india'",
            "select * from t where country = 'australia'",
        )

    def test_string_with_space(self):
        same_pattern(
            "select * from t where name = 'john doe'",
            "select * from t where name = 'jane smith'",
        )

    def test_escaped_quote_in_string(self):
        n = normalize("select * from t where name = 'o\\'brien'")
        assert "?" in n


class TestStructuralDifferences:
    def test_different_tables_are_different_patterns(self):
        diff_pattern(
            "select * from table1 where a = 10",
            "select * from table2 where a = 10",
        )

    def test_different_columns_are_different_patterns(self):
        diff_pattern(
            "select * from t where a = 10",
            "select * from t where b = 10",
        )

    def test_extra_filter_is_different_pattern(self):
        diff_pattern(
            "select * from t where a = 10",
            "select * from t where a = 10 and b = 'x'",
        )

    def test_different_select_columns(self):
        diff_pattern(
            "select id from t where a = 1",
            "select name from t where a = 1",
        )


class TestInList:
    def test_in_list_numbers(self):
        same_pattern(
            "select * from t where id in (1, 2, 3)",
            "select * from t where id in (4, 5, 6)",
        )

    def test_in_list_strings(self):
        same_pattern(
            "select * from t where code in ('a', 'b')",
            "select * from t where code in ('x', 'y')",
        )


class TestComments:
    def test_line_comment_stripped(self):
        same_pattern(
            "select * from t where x = 5 -- this is a filter",
            "select * from t where x = 99",
        )

    def test_block_comment_stripped(self):
        same_pattern(
            "/* get all */ select * from t where x = 5",
            "select * from t where x = 42",
        )


class TestWhitespaceAndCase:
    def test_extra_spaces(self):
        same_pattern(
            "select * from t where x = 1",
            "SELECT   *   FROM   t   WHERE   x = 2",
        )

    def test_case_insensitive(self):
        same_pattern(
            "SELECT * FROM t WHERE x = 1",
            "select * from t where x = 2",
        )

    def test_newlines(self):
        same_pattern(
            "select *\nfrom t\nwhere x = 1",
            "select * from t where x = 99",
        )


class TestSubqueries:
    def test_subquery_literal(self):
        same_pattern(
            "select * from t where id in (select id from u where val = 100)",
            "select * from t where id in (select id from u where val = 999)",
        )


class TestEdgeCases:
    def test_empty_string(self):
        assert normalize("") == ""

    def test_none(self):
        assert normalize(None) == ""

    def test_whitespace_only(self):
        assert normalize("   ") == ""

    def test_fingerprint_id_stable(self):
        n = normalize("select * from t where a = 1")
        assert fingerprint_id(n) == fingerprint_id(n)

    def test_fingerprint_id_differs_by_pattern(self):
        n1 = normalize("select * from t1 where a = 1")
        n2 = normalize("select * from t2 where a = 1")
        assert fingerprint_id(n1) != fingerprint_id(n2)


# ── Integration test: end-to-end grouping ────────────────────────────────────

class TestEndToEnd:
    def test_five_queries_collapse_to_three(self):
        queries = [
            "select * from table1 where a = 10",
            "select * from table1 where a = 20",
            "select * from table1 where a = 10 and b = 'india'",
            "select * from table1 where a = 10 and b = 'aus'",
            "select * from table2 where a = 10 and b = 'aus'",
        ]
        df = pd.DataFrame({"query": queries})
        df["_pattern"] = df["query"].map(normalize)
        result = df.groupby("_pattern").agg(
            count=("query", "count"),
            representative_query=("query", "first"),
        ).reset_index()
        assert len(result) == 3, f"Expected 3 patterns, got {len(result)}"

    def test_sample_csv(self):
        """Load sample_queries.csv and check reasonable deduplication."""
        df = pd.read_csv("sample_queries.csv")
        # Detect the SQL column (first column whose values look like SQL)
        sql_col = next(
            (c for c in df.columns if df[c].astype(str).str.contains(r"\bselect\b", case=False, regex=True).any()),
            df.columns[0],
        )
        df["_pattern"] = df[sql_col].map(normalize)
        unique_patterns = df["_pattern"].nunique()
        total = len(df)
        assert unique_patterns < total, "Should have fewer patterns than raw queries"
        print(f"\n  sample_queries.csv: {total} queries → {unique_patterns} patterns (col: {sql_col!r})")

    def test_output_contains_real_sql(self):
        """Representative query must be the original SQL, not a normalised form."""
        queries = [
            "select * from orders where status = 'pending'",
            "select * from orders where status = 'shipped'",
        ]
        df = pd.DataFrame({"query": queries})
        df["_pattern"] = df["query"].map(normalize)
        result = df.groupby("_pattern").agg(
            representative_query=("query", "first")
        ).reset_index()
        rep = result["representative_query"].iloc[0]
        assert "?" not in rep, f"Representative query should not contain '?': {rep}"
        assert "pending" in rep or "shipped" in rep


# ── Run directly ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    result = pytest.main([__file__, "-v", "--tb=short"])
    sys.exit(result)
