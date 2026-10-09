#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python -m backend.update_all --from 2024-07-09 --to 2026-07-09 --mock-if-empty
python -m backend.api.main > data/processed/api.log 2>&1 &

cd "$ROOT/frontend"
npm install
npm run dev
