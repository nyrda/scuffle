"""Shared contracts for the anonymous Forever updater."""
from dataclasses import dataclass

@dataclass(frozen=True)
class DownloadEntry:
    ekey: bytes
    encoded_size: int
    priority: int

@dataclass(frozen=True)
class InstallEntry:
    path: str
    ckey: bytes
    decoded_size: int

@dataclass(frozen=True)
class EncodingInfo:
    ekey: bytes
    encoded_size: int
    espec: str

@dataclass(frozen=True)
class Location:
    archive: str
    offset: int
    size: int
