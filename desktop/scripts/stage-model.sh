#!/usr/bin/env bash
# Stage the embedding model: intfloat/multilingual-e5-small, fp32 ONNX.
# Mirrors the layout apps/api/Dockerfile bakes into its image — the repo keeps
# the graph under onnx/ and the tokenizer at the root, and app/llm/embedder.py
# expects the two files FLAT:
#   resources/models/e5-small/model.onnx
#   resources/models/e5-small/tokenizer.json
#
# fp32, NOT int8 — the int8 export changes top-5 retrieval ranking (see the
# Dockerfile's comment). Downloads are cached; the staged copy is cheap.
#
# Usage: TARGET=linux-x64 ./stage-model.sh   (TARGET accepted for symmetry;
# the model is platform-neutral.)
# shellcheck source=./common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
target_init "${1:-}"

HF_BASE="https://huggingface.co/intfloat/multilingual-e5-small/resolve/main"
CACHE="$CACHE_DIR/model/e5-small"
OUT="$RES_DIR/models/e5-small"

fetch "$HF_BASE/onnx/model.onnx" "$CACHE/model.onnx"
fetch "$HF_BASE/tokenizer.json" "$CACHE/tokenizer.json"

# Sanity: fp32 graph is ~470MB, tokenizer ~17MB. A tiny file is an HTML error
# page or a truncated fetch, not a model.
size_of() { wc -c < "$1" | tr -d ' '; }
[ "$(size_of "$CACHE/model.onnx")" -gt 100000000 ] || die "model.onnx is suspiciously small — delete $CACHE and refetch"
[ "$(size_of "$CACHE/tokenizer.json")" -gt 1000000 ] || die "tokenizer.json is suspiciously small — delete $CACHE and refetch"

rm -rf "$OUT"
mkdir -p "$OUT"
cp "$CACHE/model.onnx" "$OUT/model.onnx"
cp "$CACHE/tokenizer.json" "$OUT/tokenizer.json"

log "model staged: $OUT ($(du -sh "$OUT" | awk '{print $1}'))"
