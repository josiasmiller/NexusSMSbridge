#!/usr/bin/env bash
# install-sdk.sh — download Java 21 + Android SDK into tools/ (no root needed)
# Run once before starting the builder:  bash install-sdk.sh
set -euo pipefail

TOOLS_DIR="$(cd "$(dirname "$0")" && pwd)/tools"
JDK_DIR="$TOOLS_DIR/jdk21"
SDK_DIR="$TOOLS_DIR/android-sdk"

OS="$(uname -s)"
ARCH="$(uname -m)"

# ── portable download helper ──────────────────────────────────────────────────
# Usage: download <url> <dest_file>
download() {
  local url="$1" dest="$2"
  if command -v curl > /dev/null 2>&1; then
    curl -fsSL "$url" -o "$dest"
  elif command -v wget > /dev/null 2>&1; then
    wget -q "$url" -O "$dest"
  elif command -v python3 > /dev/null 2>&1; then
    python3 -c "
import urllib.request, sys
url, dest = sys.argv[1], sys.argv[2]
print('  (downloading via python3 urllib…)', flush=True)
urllib.request.urlretrieve(url, dest)
" "$url" "$dest"
  elif command -v python > /dev/null 2>&1; then
    python -c "
import urllib.request, sys
url, dest = sys.argv[1], sys.argv[2]
print('  (downloading via python urllib…)', flush=True)
urllib.request.urlretrieve(url, dest)
" "$url" "$dest"
  else
    echo "ERROR: no download tool found (tried curl, wget, python3, python)."
    echo "       Install curl or wget and retry."
    exit 1
  fi
}

# ── Java 21 (Eclipse Temurin) ─────────────────────────────────────────────────
install_jdk() {
  if [ -f "$JDK_DIR/bin/java" ]; then
    echo "[JDK] Already installed at $JDK_DIR"
    return
  fi
  echo "[JDK] Installing Eclipse Temurin 21…"
  mkdir -p "$JDK_DIR"

  case "$OS" in
    Linux)
      case "$ARCH" in
        x86_64)  JDK_URL="https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.5%2B11/OpenJDK21U-jdk_x64_linux_hotspot_21.0.5_11.tar.gz" ;;
        aarch64) JDK_URL="https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.5%2B11/OpenJDK21U-jdk_aarch64_linux_hotspot_21.0.5_11.tar.gz" ;;
        *) echo "Unsupported arch: $ARCH"; exit 1 ;;
      esac
      ;;
    Darwin)
      case "$ARCH" in
        x86_64)  JDK_URL="https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.5%2B11/OpenJDK21U-jdk_x64_mac_hotspot_21.0.5_11.tar.gz" ;;
        arm64)   JDK_URL="https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.5%2B11/OpenJDK21U-jdk_aarch64_mac_hotspot_21.0.5_11.tar.gz" ;;
        *) echo "Unsupported arch: $ARCH"; exit 1 ;;
      esac
      ;;
    *) echo "Unsupported OS: $OS"; exit 1 ;;
  esac

  TMP="$(mktemp -d)"
  echo "  Downloading $JDK_URL …"
  download "$JDK_URL" "$TMP/jdk.tar.gz"
  echo "  Extracting…"
  tar -xzf "$TMP/jdk.tar.gz" -C "$JDK_DIR" --strip-components=1
  # macOS JDK tarballs have an extra Contents/Home layer
  if [ -d "$JDK_DIR/Contents/Home" ]; then
    mv "$JDK_DIR/Contents/Home"/* "$JDK_DIR/"
    rm -rf "$JDK_DIR/Contents"
  fi
  rm -rf "$TMP"
  echo "[JDK] Installed → $JDK_DIR/bin/java"
}

# ── Android SDK command-line tools ────────────────────────────────────────────
install_android_sdk() {
  local CMDLINE_TOOLS="$SDK_DIR/cmdline-tools/latest"
  if [ -f "$CMDLINE_TOOLS/bin/sdkmanager" ]; then
    echo "[SDK] Already installed at $SDK_DIR"
  else
    echo "[SDK] Installing Android command-line tools…"
    mkdir -p "$SDK_DIR/cmdline-tools"

    CMDTOOLS_URL="https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip"
    TMP="$(mktemp -d)"
    echo "  Downloading command-line tools…"
    download "$CMDTOOLS_URL" "$TMP/cmdtools.zip"
    echo "  Extracting…"
    if command -v unzip > /dev/null 2>&1; then
      unzip -q "$TMP/cmdtools.zip" -d "$TMP/cmdtools"
    elif command -v python3 > /dev/null 2>&1; then
      python3 -c "
import zipfile, sys
zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])
" "$TMP/cmdtools.zip" "$TMP/cmdtools"
    elif command -v python > /dev/null 2>&1; then
      python -c "
import zipfile, sys
zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])
" "$TMP/cmdtools.zip" "$TMP/cmdtools"
    else
      echo "ERROR: no unzip tool found (tried unzip, python3, python)."
      exit 1
    fi
    # Google packages the tools inside a 'cmdline-tools' sub-folder
    mv "$TMP/cmdtools/cmdline-tools" "$CMDLINE_TOOLS"
    rm -rf "$TMP"
    # Ensure binaries are executable (python zipfile doesn't preserve permissions)
    chmod -R +x "$CMDLINE_TOOLS/bin"
    echo "[SDK] command-line tools → $CMDLINE_TOOLS"
  fi

  # Accept licenses and install required packages
  export JAVA_HOME="$JDK_DIR"
  export PATH="$JAVA_HOME/bin:$CMDLINE_TOOLS/bin:$PATH"
  export ANDROID_HOME="$SDK_DIR"
  export ANDROID_SDK_ROOT="$SDK_DIR"

  echo "[SDK] Accepting licenses…"
  yes | sdkmanager --sdk_root="$SDK_DIR" --licenses > /dev/null 2>&1 || true

  echo "[SDK] Installing platform-tools, build-tools 35.0.0, android-35…"
  sdkmanager --sdk_root="$SDK_DIR" \
    "platform-tools" \
    "platforms;android-35" \
    "build-tools;35.0.0"

  echo "[SDK] Done."
}

# ── Write an env file the builder can source ─────────────────────────────────
write_env() {
  cat > "$TOOLS_DIR/env.sh" <<EOF
# Auto-generated by install-sdk.sh — source this before running builder.py
export JAVA_HOME="$JDK_DIR"
export ANDROID_HOME="$SDK_DIR"
export ANDROID_SDK_ROOT="$SDK_DIR"
export PATH="\$JAVA_HOME/bin:\$ANDROID_HOME/platform-tools:\$PATH"
EOF
  echo "[ENV] Written to $TOOLS_DIR/env.sh"
}

# ── main ──────────────────────────────────────────────────────────────────────
mkdir -p "$TOOLS_DIR"
install_jdk
install_android_sdk
write_env

echo ""
echo "✓ All done!  Start the builder with:"
echo "  source tools/env.sh && python builder.py"
