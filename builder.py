#!/usr/bin/env python3
"""
NexusBridge Builder
Web app that lets anyone self-host NexusBridge with their own server URL.
Substitutes the URL into all source files, builds the APK, and serves a zip.
"""

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request, send_file, abort

app = Flask(__name__)

# ── paths relative to this file ───────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.resolve()

# ── in-memory job store (fine for a single-user builder) ─────────────────────
jobs: dict = {}   # job_id → dict


# ── helpers ───────────────────────────────────────────────────────────────────

def _validate_url(url: str) -> str | None:
    """Return normalised https URL or None if invalid."""
    url = url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None
    # Only allow https in production; http only for localhost dev
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1"):
        return None
    return url


def _to_wss(https_url: str) -> str:
    """Convert https://host to wss://host (or ws:// for http)."""
    return https_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)


def _hostname(url: str) -> str:
    return urlparse(url).netloc


def _substitute_files(build_dir: Path, https_url: str) -> None:
    """Replace the hardcoded URL everywhere in the build directory."""
    wss_url   = _to_wss(https_url)
    host      = _hostname(https_url)
    OLD_HTTPS = "https://your.domain.com"
    OLD_WSS   = "wss://your.domain.com"
    OLD_HOST  = "your.domain.com"

    text_extensions = {
        ".py", ".html", ".kt", ".xml", ".gradle", ".properties", ".md"
    }

    for path in build_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in text_extensions:
            continue
        # Skip binary build artifacts
        if any(part in path.parts for part in ("build", ".gradle", "__pycache__")):
            continue
        try:
            text = path.read_text(encoding="utf-8")
            original = text
            text = text.replace(OLD_HTTPS, https_url)
            text = text.replace(OLD_WSS,   wss_url)
            text = text.replace(OLD_HOST,  host)
            if text != original:
                path.write_text(text, encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            pass


# ── local SDK / JDK detection ─────────────────────────────────────────────────

def _find_local_jdk() -> Path | None:
    """
    Return the JDK home to use, checking in priority order:
      1. tools/jdk21 (installed by install-sdk.sh)
      2. /opt/jdk21  (installed inside Docker image)
      3. JAVA_HOME env var already set by the caller's environment
    """
    candidates = [
        PROJECT_ROOT / "tools" / "jdk21",
        Path("/opt/jdk21"),
    ]
    for c in candidates:
        if (c / "bin" / "java").exists():
            return c
    # Already set in the environment (e.g. system package, sdkman, CI)
    env_home = os.environ.get("JAVA_HOME", "")
    if env_home and (Path(env_home) / "bin" / "java").exists():
        return Path(env_home)
    return None


def _find_local_sdk() -> Path | None:
    """
    Return the Android SDK root, checking in priority order:
      1. tools/android-sdk  (installed by install-sdk.sh)
      2. /opt/android-sdk   (installed inside Docker image)
      3. ANDROID_HOME / ANDROID_SDK_ROOT env var
    """
    candidates = [
        PROJECT_ROOT / "tools" / "android-sdk",
        Path("/opt/android-sdk"),
    ]
    for c in candidates:
        if (c / "cmdline-tools" / "latest" / "bin" / "sdkmanager").exists():
            return c
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        val = os.environ.get(var, "")
        if val and Path(val).is_dir():
            return Path(val)
    return None


def _build_env(build_dir: Path) -> dict:
    """
    Build the environment dict for the Gradle subprocess.
    Priority: tools/ locals > Docker paths > existing env vars.
    Also writes local.properties into the android/ sub-dir of the build copy.
    """
    env = os.environ.copy()

    jdk = _find_local_jdk()
    sdk = _find_local_sdk()

    if jdk:
        env["JAVA_HOME"] = str(jdk)
        env["PATH"] = str(jdk / "bin") + os.pathsep + env.get("PATH", "")

    if sdk:
        env["ANDROID_HOME"]     = str(sdk)
        env["ANDROID_SDK_ROOT"] = str(sdk)

    android_home = env.get("ANDROID_HOME") or env.get("ANDROID_SDK_ROOT", "")

    # Write local.properties so Gradle finds the SDK without user action
    if android_home:
        lp_path = build_dir / "android" / "local.properties"
        sdk_path_escaped = android_home.replace("\\", "\\\\")
        lp_path.write_text(f"sdk.dir={sdk_path_escaped}\n", encoding="utf-8")

    # Point Gradle/Android user homes at writable dirs (containers may have no real $HOME)
    gradle_home = Path(tempfile.gettempdir()) / "gradle_home"
    gradle_home.mkdir(parents=True, exist_ok=True)
    env["GRADLE_USER_HOME"] = str(gradle_home)

    android_user_home = Path(tempfile.gettempdir()) / "android_home"
    android_user_home.mkdir(parents=True, exist_ok=True)
    env["ANDROID_USER_HOME"] = str(android_user_home)
    env["HOME"] = str(Path(tempfile.gettempdir()))

    return env


def _check_java(env: dict, job: dict) -> bool:
    """Log the detected Java version; return False if Java is missing."""
    import shutil as _shutil

    java_home = env.get("JAVA_HOME", "")
    if java_home:
        java_bin = str(Path(java_home) / "bin" / "java")
    else:
        # Search the (possibly augmented) PATH for java
        java_bin = _shutil.which("java", path=env.get("PATH")) or ""

    if not java_bin:
        job["log"].append("[Java] NOT FOUND in PATH or JAVA_HOME.")
        job["log"].append("Run  bash install-sdk.sh  on the server, then restart the builder.")
        return False

    try:
        result = subprocess.run(
            [java_bin, "-version"],
            capture_output=True, text=True, env=env, timeout=10
        )
        version_line = (result.stderr or result.stdout).splitlines()[0]
        job["log"].append(f"[Java] {version_line}")
        return True
    except Exception as e:
        job["log"].append(f"[Java] Failed to run {java_bin} — {e}")
        job["log"].append("Run  bash install-sdk.sh  on the server, then restart the builder.")
        return False


def _run_gradle_build(build_dir: Path, job: dict) -> None:
    """Run assembleDebug and stream log lines into job['log']."""
    android_dir = build_dir / "android"

    # Choose gradlew script
    gradlew = android_dir / ("gradlew.bat" if os.name == "nt" else "gradlew")
    if not gradlew.exists():
        job["log"].append("ERROR: gradlew not found in android/")
        job["status"] = "failed"
        return

    if os.name != "nt":
        gradlew.chmod(gradlew.stat().st_mode | 0o111)

    # Build environment with local JDK/SDK detection
    env = _build_env(build_dir)
    if not _check_java(env, job):
        job["status"] = "failed"
        return

    job["log"].append(f"[JDK]  {env.get('JAVA_HOME', '(system)')}")
    job["log"].append(f"[SDK]  {env.get('ANDROID_HOME', '(system)')}")

    cmd = [str(gradlew), "assembleDebug", "--no-daemon", "--warning-mode", "all"]
    job["log"].append(f"$ {' '.join(cmd)}")

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(android_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        for line in proc.stdout:
            job["log"].append(line.rstrip())
        proc.wait()
        if proc.returncode != 0:
            job["status"] = "failed"
            job["log"].append(f"\nGradle exited with code {proc.returncode}")
            return
    except Exception as e:
        job["status"] = "failed"
        job["log"].append(f"ERROR: {e}")
        return

    # Find APK
    apk_candidates = list(android_dir.rglob("app-debug.apk"))
    if not apk_candidates:
        job["status"] = "failed"
        job["log"].append("ERROR: APK not found after build")
        return

    job["apk_path"] = str(apk_candidates[0])
    job["log"].append(f"\nAPK: {apk_candidates[0]}")


def _package_zip(build_dir: Path, job: dict, https_url: str) -> None:
    """Bundle APK + index.html into a single zip."""
    zip_path = build_dir / "nexusbridge-release.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # APK
        if job.get("apk_path"):
            zf.write(job["apk_path"], "nexusbridge.apk")

        # index.html (already substituted in build_dir)
        src = build_dir / "index.html"
        if src.exists():
            zf.write(str(src), "index.html")

        # README snippet
        readme = (
            "# NexusBridge – your custom build\n\n"
            f"Server URL: {https_url}\n\n"
            "## Quick start\n"
            "1. Install nexusbridge.apk on your Android phone\n"
            "2. Open index.html in your browser\n"
        )
        zf.writestr("README.txt", readme)

    job["zip_path"] = str(zip_path)


def _auto_install_sdk(job: dict) -> bool:
    """Run install-sdk.sh if Java or Android SDK are not already present."""
    if _find_local_jdk() and _find_local_sdk():
        return True  # already installed

    script = PROJECT_ROOT / "install-sdk.sh"
    if not script.exists():
        job["log"].append("ERROR: install-sdk.sh not found — cannot auto-install SDK")
        return False

    job["log"].append("Java/SDK not found — running install-sdk.sh (first-time setup, may take a few minutes)…")
    try:
        proc = subprocess.Popen(
            ["bash", str(script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(PROJECT_ROOT),
        )
        for line in proc.stdout:
            job["log"].append(line.rstrip())
        proc.wait()
        if proc.returncode != 0:
            job["log"].append(f"ERROR: install-sdk.sh failed (exit {proc.returncode})")
            return False
        job["log"].append("✓ SDK installed successfully")
        return True
    except Exception as e:
        job["log"].append(f"ERROR running install-sdk.sh: {e}")
        return False


def _build_job(job_id: str, https_url: str, build_apk: bool) -> None:
    """Background thread: substitute → (optionally) build APK → package zip."""
    job = jobs[job_id]
    job["status"] = "running"
    job["log"] = []

    try:
        build_dir = Path(tempfile.mkdtemp(prefix="nexusbridge_"))
        job["build_dir"] = str(build_dir)
        job["log"].append(f"Build directory: {build_dir}")
        job["log"].append(f"Server URL: {https_url}")

        # Copy project (skip heavy build artefacts)
        job["log"].append("Copying project files…")

        def _ignore(src, names):
            ignored = set()
            for n in names:
                p = Path(src) / n
                if p.is_dir() and n in ("build", ".gradle", ".git", "__pycache__",
                                        "node_modules", ".idea"):
                    ignored.add(n)
            return ignored

        shutil.copytree(str(PROJECT_ROOT), str(build_dir), dirs_exist_ok=True,
                        ignore=_ignore)
        job["log"].append("Substituting server URL in all files…")
        _substitute_files(build_dir, https_url)

        if build_apk:
            job["log"].append("Checking Java + Android SDK…")
            if not _auto_install_sdk(job):
                job["status"] = "failed"
                return
            job["log"].append("Building APK (this takes ~1–3 minutes)…")
            _run_gradle_build(build_dir, job)
            if job["status"] == "failed":
                return

        job["log"].append("Packaging zip…")
        _package_zip(build_dir, job, https_url)
        job["status"] = "done"
        job["log"].append("Done ✓")

    except Exception as e:
        job["status"] = "failed"
        job.setdefault("log", []).append(f"Unexpected error: {e}")


# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("builder.html")


@app.route("/sdk-status")
def sdk_status():
    jdk = _find_local_jdk()
    sdk = _find_local_sdk()
    # Also check system Java
    import shutil as _shutil
    system_java = _shutil.which("java")
    system_sdk  = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    return jsonify({
        "jdk_local":    str(jdk) if jdk else None,
        "sdk_local":    str(sdk) if sdk else None,
        "java_system":  system_java,
        "sdk_system":   system_sdk,
        "apk_build_ready": bool(jdk or system_java) and bool(sdk or system_sdk),
    })


@app.route("/build", methods=["POST"])
def start_build():
    url = request.form.get("server_url", "").strip()

    https_url = _validate_url(url)
    if not https_url:
        return jsonify({"error": "Invalid URL. Use https://yourdomain.com"}), 400

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "queued", "log": [], "started": time.time()}

    t = threading.Thread(target=_build_job, args=(job_id, https_url, True),
                         daemon=True)
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def job_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        abort(404)
    return jsonify({
        "status": job["status"],
        "log": job.get("log", []),
        "ready": job["status"] == "done",
    })


@app.route("/download/<job_id>")
def download(job_id: str):
    job = jobs.get(job_id)
    if not job or job.get("status") != "done":
        abort(404)
    zip_path = job.get("zip_path")
    if not zip_path or not Path(zip_path).exists():
        abort(404)
    return send_file(zip_path, as_attachment=True,
                     download_name="nexusbridge-release.zip",
                     mimetype="application/zip")


if __name__ == "__main__":
    # Development only — use gunicorn/uvicorn in production
    app.run(host="0.0.0.0", port=8000, debug=False)
