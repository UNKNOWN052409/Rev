#!/bin/bash
# Rev Firecrawl (Rust) — startup script
# ======================================
# Self-hosted web intelligence service. No Docker needed.
#
# Usage:
#   ./run-firecrawl.sh           # start on :3002
#   ./run-firecrawl.sh 8080      # start on :8080
#
# Build (if needed):
#   cd rev-firecrawl-rs && cargo build --release

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BINARY="$SCRIPT_DIR/rev-firecrawl-rs/target/release/rev-firecrawl"
PORT="${1:-3002}"

# Build if binary doesn't exist
if [ ! -f "$BINARY" ]; then
    echo "[*] Building rev-firecrawl (Rust)..."
    cd "$SCRIPT_DIR/rev-firecrawl-rs"
    cargo build --release
    cd "$SCRIPT_DIR"
fi

echo "=========================================="
echo " Rev Firecrawl (Rust) — self-hosted"
echo " URL: http://localhost:$PORT"
echo " Health: http://localhost:$PORT/health"
echo "=========================================="

export FIRECRAWL_URL="http://localhost:$PORT"
exec "$BINARY" --port "$PORT"
