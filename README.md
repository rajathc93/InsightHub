# InsightsHub

A multi-tool analytics app built with Streamlit. Currently ships with **SQL Query Deduplicator** — more tools coming soon.

---

## Tools

### SQL Query Deduplicator

Identifies structurally identical SQL queries that differ only in filter values and returns one runnable query per unique pattern.

**Example** — these three queries collapse to one pattern:
```sql
SELECT * FROM orders WHERE status = 'pending'
SELECT * FROM orders WHERE status = 'shipped'
SELECT * FROM orders WHERE status = 'delivered'
```

**How it works:**
1. Strips comments and normalises whitespace/casing
2. Replaces all literal values (`'pending'`, `42`, `true`) with `?` placeholders using [sqlglot](https://github.com/tobymao/sqlglot) (regex fallback for unsupported dialects)
3. Groups queries by their normalised fingerprint
4. Returns one original, runnable query per unique pattern — comments, casing, and filter values untouched

**Supports:**
- Local CSV or Parquet upload
- S3 paths with glob or regex file patterns (multi-file concat)
- Any column in the file as the query source
- Output as Parquet (avoids CSV cell character limits)

---

## Getting Started

### Prerequisites

- Python 3.9+
- pip

### Local setup

```bash
git clone https://github.com/rajathc93/InsightHub.git
cd InsightHub

python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt

streamlit run app.py
```

App runs at `http://localhost:8501`.

### S3 access

Leave the credential fields blank if AWS credentials are already configured on your machine (`~/.aws/credentials` or environment variables). Fill them in only if you need to override:

| Field | Description |
|---|---|
| `AWS_ACCESS_KEY_ID` | IAM user access key |
| `AWS_SECRET_ACCESS_KEY` | IAM user secret key |
| `AWS_SESSION_TOKEN` | Only for STS / AssumeRole / SSO temporary credentials |

---

## Deployment (Streamlit Community Cloud)

1. Push this repo to GitHub
2. Go to [share.streamlit.io](https://share.streamlit.io) → **New app**
3. Select the repo, branch `main`, main file `app.py`
4. For S3 access, add your credentials under **Settings → Secrets**:

```toml
[aws]
AWS_ACCESS_KEY_ID = "AKIAxxxxxxxxxxxxxxxx"
AWS_SECRET_ACCESS_KEY = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
AWS_SESSION_TOKEN = ""
```

---

## Project Structure

```
InsightsHub/
├── app.py                  # Streamlit app — all pages and UI
├── normalizer.py           # SQL fingerprinting logic (sqlglot + regex)
├── requirements.txt        # Python dependencies
├── sample_queries.csv      # Sample data for testing
├── test_normalizer.py      # pytest tests for the normalizer
├── setup.sh                # One-shot local setup script (Mac/Linux)
└── .streamlit/
    └── config.toml         # Theme config (forces light mode)
```

---

## Running Tests

```bash
source venv/bin/activate
pytest test_normalizer.py -v
```

---

## Requirements

```
streamlit>=1.35.0
sqlglot>=23.0.0
pandas>=2.0.0
pyarrow>=14.0.0
s3fs>=2024.2.0
boto3>=1.34.0
```
