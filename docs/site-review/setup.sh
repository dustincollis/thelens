#!/usr/bin/env bash
#
# Bootstrap script for site-review.
# Run this on a fresh checkout to get to a working state.
#
# Usage: bash setup.sh

set -euo pipefail

echo "==> Checking for uv..."
if ! command -v uv &> /dev/null; then
    echo "uv not found. Installing..."
    if [[ "$OSTYPE" == "darwin"* ]] || [[ "$OSTYPE" == "linux-gnu"* ]]; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    else
        echo "Please install uv manually: https://github.com/astral-sh/uv"
        exit 1
    fi
fi
echo "uv: $(uv --version)"

echo "==> Verifying Python 3.12..."
uv python install 3.12

echo "==> Syncing dependencies..."
uv sync

echo "==> Installing Playwright Chromium..."
uv run playwright install chromium

echo "==> Verifying SDK imports..."
uv run python -c "import anthropic, openai, google.generativeai, playwright, pptx, jinja2, typer, rich, pydantic; print('All core SDKs import successfully.')"

if [[ ! -f .env ]]; then
    echo "==> Creating .env from .env.example..."
    cp .env.example .env
    echo
    echo "IMPORTANT: edit .env and add your API keys before running site-review."
    echo "  - ANTHROPIC_API_KEY"
    echo "  - OPENAI_API_KEY"
    echo "  - GOOGLE_API_KEY"
    echo "  - XAI_API_KEY"
fi

mkdir -p data runs

echo
echo "Setup complete."
echo "Next steps:"
echo "  1. Edit .env with your API keys."
echo "  2. Run: uv run site-review run https://example.com"
