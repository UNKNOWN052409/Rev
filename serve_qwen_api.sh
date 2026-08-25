#!/bin/bash
# =============================================================
# Qwen -> OpenAI-Compatible API Server
# Local GGUF model ko full API banata hai:
#   - /v1/chat/completions  (streaming supported)
#   - /v1/models
#   - TOOL CALLING (function calling) --jinja ke saath
#   - koi web scraping nahi, sab local inference
# =============================================================

set -e

LLAMA_DIR="$HOME/llama.cpp"
MODEL_DIR="$HOME/models"
PORT=8081

# --- 1. Model select karo ---
echo "Available models in $MODEL_DIR:"
ls "$MODEL_DIR"/*.gguf 2>/dev/null || echo "  (none)"
echo ""
read -rp "Model filename (ya download karna hai? 'download' likho): " MODEL_FILE

if [ "$MODEL_FILE" = "download" ]; then
    # Qwen3 official GGUF (HuggingFace se direct)
    read -rp "Qwen size [0.6B/1.7B/4B/8B/14B] (default 8B): " SIZE
    SIZE=${SIZE:-8B}
    mkdir -p "$MODEL_DIR"
    URL="https://huggingface.co/Qwen/Qwen3-${SIZE}/resolve/main/Qwen3-${SIZE}-Q4_K_M.gguf"
    echo "Downloading $URL ..."
    wget -c -O "$MODEL_DIR/Qwen3-${SIZE}-Q4_K_M.gguf" "$URL"
    MODEL_FILE="Qwen3-${SIZE}-Q4_K_M.gguf"
fi

MODEL_PATH="$MODEL_DIR/$MODEL_FILE"
[ -f "$MODEL_PATH" ] || { echo "Model not found: $MODEL_PATH"; exit 1; }

# --- 2. llama-server build (agar nahi hai) ---
if [ ! -d "$LLAMA_DIR" ]; then
    echo "[*] $LLAMA_DIR not found — cloning llama.cpp..."
    git clone --depth 1 https://github.com/ggml-org/llama.cpp "$LLAMA_DIR"
fi
SERVER_BIN=$(find "$LLAMA_DIR/build" -name "llama-server" -type f 2>/dev/null | head -1)
if [ -z "$SERVER_BIN" ]; then
    echo "[*] llama-server not built — building now..."
    cd "$LLAMA_DIR" || exit 1
    cmake -B build -DGGML_NATIVE=ON 2>/dev/null
    cmake --build build --config Release -j$(nproc)
    SERVER_BIN=$(find build -name "llama-server" -type f | head -1)
fi
[ -n "$SERVER_BIN" ] || { echo "llama-server build failed"; exit 1; }
echo "[*] Using server: $SERVER_BIN"

# --- 3. GPU check (NVIDIA hai toh already build me hoga) ---
EXTRA_FLAGS=""
if command -v nvidia-smi &>/dev/null; then
    NGPU=$(nvidia-smi -L | wc -l)
    EXTRA_FLAGS="-ngl 99"   # saari layers GPU pe
    echo "[*] NVIDIA GPU detected ($NGPU) — offloading all layers"
fi

# --- 4. Launch API server ---
echo ""
echo "=============================================="
echo " Qwen API Server starting"
echo "   endpoint : http://localhost:$PORT/v1"
echo "   api-key  : sk-local-anything (koi bhi chalega)"
echo "   tools    : ENABLED (--jinja)"
echo "=============================================="
echo ""
echo "Test:"
echo "  curl http://localhost:$PORT/v1/chat/completions \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"model\":\"qwen\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}'"
echo ""

exec "$SERVER_BIN" \
    -m "$MODEL_PATH" \
    --host 0.0.0.0 \
    --port $PORT \
    --alias qwen \
    --ctx-size 32768 \
    --jinja \
    $EXTRA_FLAGS
