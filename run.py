#!/usr/bin/env python3
"""
One-command launcher for the invoice fraud detection platform.

    python3 run.py                    # check the environment, then open the web app
    python3 run.py --port 9000        # a different port
    python3 run.py --no-browser       # start the server without opening a browser
    python3 run.py --demo             # no web server: run the CLI demo instead
    python3 run.py --selftest         # run the test suite
    python3 run.py --check            # verify the environment and exit
    python3 run.py --install          # install missing Python packages, then run

On Windows use `py run.py`.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IS_WINDOWS = platform.system() == "Windows"

GREEN, RED, YELLOW, DIM, RESET = "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[0m"
if IS_WINDOWS and not os.environ.get("WT_SESSION"):
    GREEN = RED = YELLOW = DIM = RESET = ""
OK, BAD, WARN = f"{GREEN}OK{RESET}", f"{RED}MISSING{RESET}", f"{YELLOW}WARN{RESET}"

TESSERACT_HINTS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
]

REQUIRED = [
    ("numpy", "numpy"),
    ("cv2", "opencv-python-headless"),
    ("pytesseract", "pytesseract"),
    ("PIL", "Pillow"),
    ("sklearn", "scikit-learn"),
    ("networkx", "networkx"),
    ("rapidfuzz", "rapidfuzz"),
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("multipart", "python-multipart"),
]


def line(label: str, status: str, note: str = "") -> None:
    tail = f"  {DIM}{note}{RESET}" if note else ""
    print(f"  {label:<36} {status}{tail}")


def check_python() -> bool:
    v = sys.version_info
    ok = v >= (3, 9)
    line(f"Python {v.major}.{v.minor}.{v.micro}", OK if ok else BAD,
         "" if ok else "need Python 3.9 or newer")
    return ok


def find_tesseract() -> str | None:
    exe = shutil.which("tesseract")
    if exe:
        return exe
    for candidate in TESSERACT_HINTS:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def check_tesseract() -> bool:
    exe = find_tesseract()
    if not exe:
        line("Tesseract OCR engine", BAD, "the OCR step cannot run without it")
        return False
    try:
        result = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20)
        first = (result.stdout or result.stderr).splitlines()
        version = first[0].split()[1] if first else "?"
        line(f"Tesseract OCR engine {version}", OK, exe)
    except Exception as exc:  # noqa: BLE001
        line("Tesseract OCR engine", WARN, f"found but could not run it: {exc}")
    if IS_WINDOWS:
        try:
            import pytesseract
            pytesseract.pytesseract.tesseract_cmd = exe
        except Exception:  # noqa: BLE001
            pass
    return True


def missing_packages() -> list[str]:
    return [pip_name for module, pip_name in REQUIRED
            if importlib.util.find_spec(module) is None]


def check_packages(install: bool) -> bool:
    missing = missing_packages()
    if not missing:
        line(f"Python packages ({len(REQUIRED)} required)", OK)
        return True
    for module, pip_name in REQUIRED:
        if importlib.util.find_spec(module) is None:
            line(f"Python package: {pip_name}", BAD)
    print()
    print(f"  Install them with:\n\n    {sys.executable} -m pip install -r requirements.txt\n")
    if not install:
        return False
    print("  --install given, installing now...\n")
    code = subprocess.run([sys.executable, "-m", "pip", "install",
                           "-r", str(ROOT / "requirements.txt")]).returncode
    if code != 0:
        print(f"\n  {RED}Install failed.{RESET} If pip is blocked, try: "
              f"{sys.executable} -m pip install --user -r requirements.txt")
        return False
    return not missing_packages()


def check_corpus(auto_build: bool = True) -> bool:
    labels = ROOT / "data" / "samples" / "labels.json"
    images = ROOT / "data" / "samples" / "invoice_images"
    n_images = len(list(images.glob("*.png"))) if images.exists() else 0
    if labels.exists() and n_images:
        line(f"Invoice corpus ({n_images} documents)", OK)
        return True
    if not auto_build:
        line("Invoice corpus", WARN, "missing")
        return False
    line("Invoice corpus", WARN, "not found - building it now")
    code = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_dataset.py")]).returncode
    if code != 0:
        line("Invoice corpus", BAD, "generation failed")
        return False
    line("Invoice corpus", OK, "generated")
    return True


def free_port(preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    return preferred


def open_browser_later(url: str, delay: float = 1.5) -> None:
    def worker() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=worker, daemon=True).start()


def serve(port: int, host: str, open_browser: bool, reload: bool) -> int:
    import os as _os
    os.environ.setdefault("PYTHONPATH", str(ROOT / "src"))
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(ROOT))

    import uvicorn
    display_host = "localhost" if host in ("0.0.0.0", "127.0.0.1") else host
    url = f"http://{display_host}:{port}"
    print()
    print("  " + "=" * 74)
    print(f"  {GREEN}Starting the AP workbench{RESET}")
    print("  " + "-" * 74)
    print(f"    Open in your browser:  {url}")
    print(f"    API documentation:     {url}/docs")
    print(f"    Stop the server:       Ctrl+C")
    print("  " + "=" * 74)
    print()
    if open_browser:
        open_browser_later(url)
    uvicorn.run("app.main:app", host=host, port=port, reload=reload,
                log_level="info", access_log=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Launcher for the Automated Invoice Matching & Fraud Detection platform.")
    parser.add_argument("--check", action="store_true", help="check the environment and exit")
    parser.add_argument("--install", action="store_true", help="install missing Python packages")
    parser.add_argument("--demo", action="store_true", help="run the CLI demo instead of the web app")
    parser.add_argument("--selftest", action="store_true", help="run the test suite")
    parser.add_argument("--limit", type=int, default=None, help="with --demo: process only N invoices")
    parser.add_argument("--port", type=int, default=8000, help="web app port (default 8000)")
    parser.add_argument("--host", default="0.0.0.0", help="bind address (default 0.0.0.0)")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument("--reload", action="store_true", help="auto-reload on code changes (development)")
    args = parser.parse_args()

    print()
    print("  Automated Invoice Matching & Fraud Detection")
    print(f"  {DIM}{platform.system()} {platform.release()} | {sys.executable}{RESET}")
    print()

    python_ok = check_python()
    tesseract_ok = check_tesseract()
    packages_ok = check_packages(args.install)
    corpus_ok = check_corpus()

    print()
    if not (python_ok and tesseract_ok and packages_ok and corpus_ok):
        print(f"  {YELLOW}Not ready yet.{RESET} Fix the lines marked MISSING above, then run this again.")
        if not tesseract_ok:
            print("  Tesseract install:")
            print("    Ubuntu/Debian :  sudo apt-get install -y tesseract-ocr")
            print("    macOS         :  brew install tesseract")
            print("    Windows       :  https://github.com/UB-Mannheim/tesseract/wiki")
        if not packages_ok:
            print(f"    Python deps   :  {sys.executable} -m pip install -r requirements.txt")
        return 1

    if args.check:
        print(f"  {GREEN}Environment ready.{RESET}")
        return 0

    if args.selftest:
        print(f"  {GREEN}Environment ready.{RESET} Running the test suite...\n")
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "selftest.py")]).returncode

    if args.demo:
        print(f"  {GREEN}Environment ready.{RESET} Running the CLI demo...\n")
        cmd = [sys.executable, str(ROOT / "scripts" / "run_demo.py")]
        if args.limit:
            cmd += ["--limit", str(args.limit)]
        return subprocess.run(cmd).returncode

    port = free_port(args.port)
    if port != args.port:
        print(f"  {YELLOW}Port {args.port} is busy; using {port} instead.{RESET}")
    print(f"  {GREEN}Environment ready.{RESET}")
    try:
        return serve(port, args.host, not args.no_browser, args.reload)
    except KeyboardInterrupt:
        print("\n  Stopped.")
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  Stopped.")
        sys.exit(130)
