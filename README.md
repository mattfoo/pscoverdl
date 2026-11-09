# pscoverdl
Download cover art for PS1 (duckstation) and PS2 (pcsx2) games

pscoverdl.py download PS1/PS2 box-art covers for Duckstation or PCSX2, from the
command line, with ZERO third-party dependencies.

Unlike cli.py, this script does NOT import the repo's pscoverdl.py and does NOT
need pip install at all: everything it uses (urllib, concurrent.futures, json,
re, ssl, configparser, argparse) is in the Python standard library.  It only
needs two files from the repo for game names (optional download still works by
serial number without them):

- GameIndex.yaml   (PCSX2 names)
- gamedb.json      (Duckstation names)

# USAGE:

## PCSX2

```bash
    python3 pscoverdl.py --emulator pcsx2 \
        --cover-dir ~/.config/PCSX2/covers \
        --gamelist ~/.config/PCSX2/cache/gamelist.cache \
        --resources-dir /path/to/gamedb_files
```

## Duckstation

```bash
    python3 pscoverdl_standalone.py -e duckstation \
        -o ~/covers -g ~/gamelist.cache \
        -t 3d --no-ssl --fallback --save
```

Run once with --save to remember --cover-dir / --gamelist / options per
emulator in the same config file the GUI uses, then just:

```bash
python3 pscoverdl.py -e [pcsx2|duckstation]
```
