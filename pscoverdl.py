#!/usr/bin/env python3
"""
pscoverdl_standalone.py — download PS1/PS2 box-art covers for Duckstation or
PCSX2, from the command line, with ZERO third-party dependencies.

Unlike cli.py, this script does NOT import the repo's pscoverdl.py and does
NOT need pip install at all: everything it uses (urllib, concurrent.futures,
json, re, ssl, configparser, argparse) is in the Python standard library.
It only needs two files from the repo for game names (optional — download
still works by serial number without them):
    src/resources/GameIndex.yaml   (PCSX2 names)
    src/resources/gamedb.json      (Duckstation names)

Usage:
    python3 pscoverdl_standalone.py --emulator pcsx2 \
        --cover-dir ~/.config/PCSX2/covers \
        --gamelist ~/.config/PCSX2/cache/gamelist.cache \
        --resources-dir /path/to/pscoverdl/src/resources

    python3 pscoverdl_standalone.py -e duckstation -o ~/covers -g ~/gamelist.cache \
        -t 3d --no-ssl --fallback --save

Run once with --save to remember --cover-dir / --gamelist / options per
emulator in the same config file the GUI uses, then just:
    python3 pscoverdl_standalone.py -e pcsx2
"""

import argparse
import configparser
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PS1_COVERS_URL_DEFAULT = "https://raw.githubusercontent.com/xlenore/psx-covers/main/covers/default"
PS1_COVERS_URL_3D = "https://raw.githubusercontent.com/xlenore/psx-covers/main/covers/3d"
PS2_COVERS_URL_DEFAULT = "https://raw.githubusercontent.com/xlenore/ps2-covers/main/covers/default"
PS2_COVERS_URL_3D = "https://raw.githubusercontent.com/xlenore/ps2-covers/main/covers/3d"

EMULATORS = ("pcsx2", "duckstation")
SECTION_NAME = {"pcsx2": "PCSX2", "duckstation": "Duckstation"}
SERIAL_RE = re.compile(r"(\w{4}-\d{5})")

# Minimal ANSI colors so we don't need the 'termcolor' package.
USE_COLOR = sys.stdout.isatty()


def c(text, color):
    codes = {"red": "31", "green": "32", "yellow": "33"}
    if not USE_COLOR or color not in codes:
        return text
    return f"\033[{codes[color]}m{text}\033[0m"


def get_config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    config_dir = base / "pscoverdl"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / "pscoverdl.ini"


def load_section(emulator: str) -> dict:
    path = get_config_path()
    if not path.is_file():
        return {}
    config = configparser.ConfigParser()
    config.read(path)
    section = SECTION_NAME[emulator]
    if not config.has_section(section):
        return {}
    try:
        return {
            "cover_dir": config.get(section, "cover_directory", fallback=None),
            "gamelist_dir": config.get(section, "game_cache", fallback=None),
            "cover_type": config.getint(section, "cover_type", fallback=None),
            "use_ssl": config.getboolean(section, "use_ssl", fallback=None),
            "fallback": config.getboolean(section, "fallback", fallback=None),
            "resources_dir": config.get(section, "resources_dir", fallback=None),
        }
    except ValueError:
        return {}


def save_section(emulator, cover_dir, gamelist_dir, cover_type, use_ssl, fallback, resources_dir):
    path = get_config_path()
    config = configparser.ConfigParser()
    if path.is_file():
        config.read(path)
    config[SECTION_NAME[emulator]] = {
        "cover_directory": cover_dir,
        "game_cache": gamelist_dir,
        "cover_type": str(cover_type),
        "use_ssl": str(use_ssl),
        "fallback": str(fallback),
        "resources_dir": resources_dir or "",
    }
    with open(path, "w") as f:
        config.write(f)


def get_serial_list(gamelist_cache_path: str, existing_covers: set):
    """Returns None if the file is missing (distinct from an empty-but-valid file,
    which returns [] and should NOT be reported as 'nothing to do')."""
    if not os.path.exists(gamelist_cache_path):
        print(c(f"[ERROR]: gamelist.cache not found at: {gamelist_cache_path}", "red"))
        return None
    with open(gamelist_cache_path, errors="ignore") as f:
        found = set(SERIAL_RE.findall(f.read()))
    print(c(f"[LOG]: {len(found)} games found", "green"))
    print(c("[LOG]: Removing already downloaded covers from queue...", "green"))
    return [s for s in found if s not in existing_covers]


def existing_covers(cover_dir: Path) -> set:
    covers = set()
    for pattern in ("*.jpg", "*.png"):
        for filename in cover_dir.glob(pattern):
            covers.add(filename.stem)
    return covers


def load_pcsx2_names(resources_dir: Path) -> dict:
    """Parse GameIndex.yaml without PyYAML: only the shape we need
    (a top-level `SERIAL:` key per entry, with a `name: "..."` line under it)."""
    path = resources_dir / "GameIndex.yaml" if resources_dir else None
    if not path or not path.exists():
        return {}
    text = path.read_text(encoding="utf-8-sig")
    names = {}
    current_serial = None
    for line in text.splitlines():
        top_level = re.match(r"^([A-Za-z0-9]{4}-[0-9]{5}):", line)
        if top_level:
            current_serial = top_level.group(1)
            continue
        if current_serial and re.match(r"^\s+name:", line):
            m = re.search(r'name:\s*"(.*)"\s*$', line) or re.search(r"name:\s*(.*\S)\s*$", line)
            if m:
                names[current_serial] = m.group(1)
            current_serial = None  # name is the line right after the key in this file
    return names


def load_duckstation_names(resources_dir: Path) -> dict:
    path = resources_dir / "gamedb.json" if resources_dir else None
    if not path or not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {item["serial"]: item["name"] for item in data if "serial" in item}


def download_one(url: str, dest: Path, use_ssl: bool) -> bool:
    if not use_ssl:
        url = url.replace("https://", "http://")
    try:
        ctx = ssl.create_default_context() if url.startswith("https://") else None
        with urllib.request.urlopen(url, timeout=20, context=ctx) as resp:
            if resp.status == 200:
                dest.write_bytes(resp.read())
                return True
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        pass
    return False


def run_batch(label, pairs, use_ssl, names, workers=4):
    """pairs: list of (serial, url). Returns list of serials that failed."""
    failed = []
    total = len(pairs)
    if total == 0:
        return failed
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {}
        for serial, url, dest in pairs:
            fut = pool.submit(download_one, url, dest, use_ssl)
            future_map[fut] = (serial, dest)
        for fut in future_map:
            serial, dest = future_map[fut]
            done += 1
            ok = fut.result()
            name = names.get(serial, "Unknown")
            if ok:
                print(c(f"[{done}/{total}] {serial} | {name}", "green"))
            else:
                failed.append(serial)
    return failed


def download_covers(cover_dir, gamelist_dir, cover_type, use_ssl, emulator, fallback, resources_dir):
    cover_dir = Path(cover_dir)
    cover_dir.mkdir(parents=True, exist_ok=True)
    resources_dir = Path(resources_dir) if resources_dir else None

    if emulator == "pcsx2":
        covers_default, covers_3d = PS2_COVERS_URL_DEFAULT, PS2_COVERS_URL_3D
        names = load_pcsx2_names(resources_dir)
    elif emulator == "duckstation":
        covers_default, covers_3d = PS1_COVERS_URL_DEFAULT, PS1_COVERS_URL_3D
        names = load_duckstation_names(resources_dir)
    else:
        print(c(f"[ERROR]: Invalid emulator: {emulator}", "red"))
        return

    existing = existing_covers(cover_dir)
    serials = get_serial_list(gamelist_dir, existing)
    if serials is None:
        return  # gamelist.cache missing; get_serial_list already printed why
    if not serials:
        print(c("[LOG]: All covers have already been downloaded", "green"))
        return

    primary_base = covers_3d if cover_type == 1 else covers_default
    primary_ext = ".png" if cover_type == 1 else ".jpg"
    pairs = [(s, f"{primary_base}/{s}{primary_ext}", cover_dir / f"{s}{primary_ext}") for s in serials]

    print(c(f"[LOG]: Downloading {len(pairs)} covers...", "green"))
    failed = run_batch("primary", pairs, use_ssl, names)

    if failed and fallback:
        fb_base = covers_default if cover_type == 1 else covers_3d
        fb_ext = ".jpg" if cover_type == 1 else ".png"
        fb_pairs = [(s, f"{fb_base}/{s}{fb_ext}", cover_dir / f"{s}{fb_ext}") for s in failed]
        print(c(f"[LOG]: Trying fallback style for {len(fb_pairs)} covers...", "yellow"))
        still_failed = run_batch("fallback", fb_pairs, use_ssl, names)
        for s in still_failed:
            print(c(f"[{s} | {names.get(s, 'Unknown')}] not found. Skipping...", "yellow"))
    elif failed:
        for s in failed:
            print(c(f"[{s} | {names.get(s, 'Unknown')}] not found. Skipping...", "yellow"))


def main():
    parser = argparse.ArgumentParser(
        prog="pscoverdl_standalone",
        description="Download PS1/PS2 box-art covers (Duckstation/PCSX2), no GUI, no pip install.",
    )
    parser.add_argument("--emulator", "-e", choices=EMULATORS, required=True)
    parser.add_argument("--cover-dir", "-o", help="Output folder for covers.")
    parser.add_argument("--gamelist", "-g", help="Path to gamelist.cache.")
    parser.add_argument("--resources-dir", "-r",
                         help="Folder with GameIndex.yaml / gamedb.json, for game names "
                              "(optional; download still works without it).")
    parser.add_argument("--cover-type", "-t", choices=("default", "3d"), default=None)
    parser.add_argument("--no-ssl", action="store_true")
    parser.add_argument("--fallback", action="store_true", default=None)
    parser.add_argument("--save", action="store_true",
                         help="Save the given options as this emulator's default config.")
    args = parser.parse_args()

    saved = load_section(args.emulator)

    cover_dir = args.cover_dir or saved.get("cover_dir")
    gamelist_dir = args.gamelist or saved.get("gamelist_dir")
    if not cover_dir or not gamelist_dir:
        parser.error("--cover-dir and --gamelist are required the first time "
                      "(or run once with --save to store them).")

    resources_dir = args.resources_dir or saved.get("resources_dir")
    cover_type_name = args.cover_type or ({0: "default", 1: "3d"}.get(saved.get("cover_type"), "default"))
    cover_type = 1 if cover_type_name == "3d" else 0
    use_ssl = saved.get("use_ssl", True)
    if args.no_ssl:
        use_ssl = False
    fallback = args.fallback if args.fallback is not None else bool(saved.get("fallback", False))

    cover_dir = str(Path(cover_dir).expanduser())
    gamelist_dir = str(Path(gamelist_dir).expanduser())
    resources_dir = str(Path(resources_dir).expanduser()) if resources_dir else None

    if args.save:
        save_section(args.emulator, cover_dir, gamelist_dir, cover_type, use_ssl, fallback, resources_dir)
        print(f"Saved settings for {args.emulator} to {get_config_path()}")

    download_covers(cover_dir, gamelist_dir, cover_type, use_ssl, args.emulator, fallback, resources_dir)


if __name__ == "__main__":
    main()
