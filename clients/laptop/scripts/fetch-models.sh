#!/usr/bin/env bash
# Download Kokoro's model files. Roughly 340 MB, fetched once.
#
# Everything else (openWakeWord, Whisper) downloads itself on first run.
set -euo pipefail

DEST="${1:-models}"
BASE="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"

mkdir -p "$DEST"

fetch() {
    local name="$1" url="$2"
    if [[ -f "$DEST/$name" ]]; then
        echo "  $name already present"
        return
    fi
    echo "  fetching $name"
    # Download to .part and rename only on success, so an interrupted download
    # is never mistaken for a working model file.
    curl --fail --location --progress-bar --output "$DEST/$name.part" "$url"
    mv "$DEST/$name.part" "$DEST/$name"
}

echo "Fetching Kokoro model files into $DEST/"
fetch "kokoro-v1.0.onnx" "$BASE/kokoro-v1.0.onnx"
fetch "voices-v1.0.bin"  "$BASE/voices-v1.0.bin"

echo
echo "Done. Now pick a voice:  juno-audition"
