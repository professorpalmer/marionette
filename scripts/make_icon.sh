#!/bin/bash
set -e

# Resolve paths
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"

SRC_PNG="$REPO_ROOT/webapp/build/assets/icon-source.png"
ICONSET_DIR="$REPO_ROOT/webapp/build/icon.iconset"
OUT_ICNS="$REPO_ROOT/webapp/build/icon.icns"

if [ ! -f "$SRC_PNG" ]; then
    echo "Error: Source image not found at $SRC_PNG"
    exit 1
fi

echo "Creating temporary iconset directory..."
rm -rf "$ICONSET_DIR"
mkdir -p "$ICONSET_DIR"

# Generate the various sizes
echo "Resizing images using sips..."
sips -z 16 16     "$SRC_PNG" --out "$ICONSET_DIR/icon_16x16.png" > /dev/null
sips -z 32 32     "$SRC_PNG" --out "$ICONSET_DIR/icon_16x16@2x.png" > /dev/null
sips -z 32 32     "$SRC_PNG" --out "$ICONSET_DIR/icon_32x32.png" > /dev/null
sips -z 64 64     "$SRC_PNG" --out "$ICONSET_DIR/icon_32x32@2x.png" > /dev/null
sips -z 128 128   "$SRC_PNG" --out "$ICONSET_DIR/icon_128x128.png" > /dev/null
sips -z 256 256   "$SRC_PNG" --out "$ICONSET_DIR/icon_128x128@2x.png" > /dev/null
sips -z 256 256   "$SRC_PNG" --out "$ICONSET_DIR/icon_256x256.png" > /dev/null
sips -z 512 512   "$SRC_PNG" --out "$ICONSET_DIR/icon_256x256@2x.png" > /dev/null
sips -z 512 512   "$SRC_PNG" --out "$ICONSET_DIR/icon_512x512.png" > /dev/null
sips -z 1024 1024 "$SRC_PNG" --out "$ICONSET_DIR/icon_512x512@2x.png" > /dev/null

echo "Generating .icns file using iconutil..."
iconutil -c icns -o "$OUT_ICNS" "$ICONSET_DIR"

echo "Cleaning up temporary iconset..."
rm -rf "$ICONSET_DIR"

echo "Successfully generated $OUT_ICNS"
