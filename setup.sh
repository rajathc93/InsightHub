#!/bin/bash
# SQL Pattern Deduplicator — Mac setup script
# Run this once from the SQLPattern directory:
#   chmod +x setup.sh && ./setup.sh

set -e

echo "→ Creating virtual environment..."
python3 -m venv venv

echo "→ Activating venv..."
source venv/bin/activate

echo "→ Upgrading pip..."
pip install --upgrade pip -q

echo "→ Installing dependencies..."
pip install streamlit sqlglot pandas pyarrow s3fs boto3

echo ""
echo "✅ Setup complete!"
echo ""
echo "To run the app:"
echo "  source venv/bin/activate"
echo "  streamlit run app.py"
