"""Scuffle's own log of what it did and what happened to the games.

It lives in the logs folder beside the games' own output, so a crash can be explained afterwards.
Only names, versions, codes and times go in. Never a ticket, a token or an email.
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
import re
import time

from .settings import data_dir

NAME = 'scuffle.log'
GAME_LOGS = 'games'
KEEP_GAME_LOGS = 10           # per client, the newest
EXIT_LINE = re.compile(r'scuffle-bridge: game exited with code 0x([0-9a-fA-F]{1,8})')
# NTSTATUS codes a game ends with when it crashes. Others are shown as the number alone.
CRASH_NAMES = {
    0xC0000005: 'access violation', 0xC000001D: 'illegal instruction', 0xC0000094: 'division by zero',
    0xC00000FD: 'stack overflow', 0xC0000409: 'stack buffer overrun', 0xC0000374: 'heap corruption',
    0x80000003: 'breakpoint', 0xC0000142: 'DLL initialisation failed', 0xC000013A: 'stopped with Ctrl+C',
    0xE06D7363: 'C++ exception',
}

log = logging.getLogger('scuffle')


def folder() -> Path:
    return data_dir() / 'logs'


def setup() -> None:
    """Write the log to its file. A folder that cannot be written to leaves Scuffle without it."""
    if any(isinstance(handler, logging.handlers.RotatingFileHandler) for handler in log.handlers):
        return
    try:
        folder().mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(folder() / NAME, maxBytes=1_000_000, backupCount=3,
                                                       encoding='utf-8')
    except OSError:
        return
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def game_log(code: str) -> Path:
    """A new file for one run of a client's game, with the oldest beyond the kept ones removed."""
    games = folder() / GAME_LOGS
    games.mkdir(parents=True, exist_ok=True)
    for old in sorted(games.glob(f'{code}-*.log'))[:-(KEEP_GAME_LOGS - 1)]:
        old.unlink(missing_ok=True)
    return games / f'{code}-{time.strftime("%Y%m%d-%H%M%S")}.log'


def game_exit_code(path: Path | None) -> int | None:
    """The code the game itself ended with, as the bridge wrote it at the end of the run's log."""
    if path is None:
        return None
    try:
        with path.open('rb') as handle:
            handle.seek(max(0, path.stat().st_size - 65536))
            tail = handle.read().decode(errors='replace')
    except OSError:
        return None
    found = EXIT_LINE.findall(tail)
    return int(found[-1], 16) if found else None


def exit_text(code: int) -> str:
    name = CRASH_NAMES.get(code)
    return f'{code:#010x}, {name}' if name else f'{code:#x}' if code > 255 else str(code)
