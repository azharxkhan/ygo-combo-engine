"""Thin Python wrapper around the ygopro-core shared library (ocgcore).

Two classes:

  Environment  loaded once. Holds the engine library, the card database and
               the card scripts. Safe to reuse across many duels.
  Duel         one game state. The engine cannot copy a duel, so the search
               re-creates duels and replays choices (see search.py).
"""

from __future__ import annotations

import ctypes
import re
import struct
import sys
from ctypes import (
    CFUNCTYPE,
    POINTER,
    Structure,
    c_char_p,
    c_int,
    c_int32,
    c_uint8,
    c_uint16,
    c_uint32,
    c_uint64,
    c_void_p,
)
from dataclasses import dataclass
from pathlib import Path

from . import constants as C
from .carddb import CardDatabase, validate_code

SUPPORTED_API_MAJOR = 11

# Script names the engine may ask for: "constant.lua", "c12345.lua", "proc_link.lua".
# No slashes, no dots other than the extension, so no path tricks.
_SCRIPT_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}\.lua$")
MAX_SCRIPT_BYTES = 2 * 1024 * 1024
BOOT_SCRIPTS = ("constant.lua", "utility.lua")


class EngineError(RuntimeError):
    """The engine could not be loaded or a duel could not be created."""


class InvalidResponse(RuntimeError):
    """The engine rejected a response (it sent MSG_RETRY)."""


# ---------------------------------------------------------------- C structs


class OCG_CardData(Structure):
    _fields_ = [
        ("code", c_uint32),
        ("alias", c_uint32),
        ("setcodes", POINTER(c_uint16)),
        ("type", c_uint32),
        ("level", c_uint32),
        ("attribute", c_uint32),
        ("race", c_uint64),
        ("attack", c_int32),
        ("defense", c_int32),
        ("lscale", c_uint32),
        ("rscale", c_uint32),
        ("link_marker", c_uint32),
    ]


class OCG_Player(Structure):
    _fields_ = [
        ("startingLP", c_uint32),
        ("startingDrawCount", c_uint32),
        ("drawCountPerTurn", c_uint32),
    ]


DataReader = CFUNCTYPE(None, c_void_p, c_uint32, POINTER(OCG_CardData))
DataReaderDone = CFUNCTYPE(None, c_void_p, POINTER(OCG_CardData))
ScriptReader = CFUNCTYPE(c_int, c_void_p, c_void_p, c_char_p)
LogHandler = CFUNCTYPE(None, c_void_p, c_char_p, c_int)


class OCG_DuelOptions(Structure):
    _fields_ = [
        ("seed", c_uint64 * 4),
        ("flags", c_uint64),
        ("team1", OCG_Player),
        ("team2", OCG_Player),
        ("cardReader", DataReader),
        ("payload1", c_void_p),
        ("scriptReader", ScriptReader),
        ("payload2", c_void_p),
        ("logHandler", LogHandler),
        ("payload3", c_void_p),
        ("cardReaderDone", DataReaderDone),
        ("payload4", c_void_p),
        ("enableUnsafeLibraries", c_uint8),
    ]


class OCG_NewCardInfo(Structure):
    _fields_ = [
        ("team", c_uint8),
        ("duelist", c_uint8),
        ("code", c_uint32),
        ("con", c_uint8),
        ("loc", c_uint32),
        ("seq", c_uint32),
        ("pos", c_uint32),
    ]


class OCG_QueryInfo(Structure):
    _fields_ = [
        ("flags", c_uint32),
        ("con", c_uint8),
        ("loc", c_uint32),
        ("seq", c_uint32),
        ("overlay_seq", c_uint32),
    ]


# -------------------------------------------------------------- Environment


def default_library_path(root: Path) -> Path:
    if sys.platform == "win32":
        name = "ocgcore.dll"
    elif sys.platform == "darwin":
        name = "libocgcore.dylib"
    else:
        name = "libocgcore.so"
    return root / "build" / name


class Environment:
    """Engine library + card data + card scripts. Create once, reuse."""

    def __init__(self, library: Path, databases: list[Path], script_dir: Path):
        library = Path(library)
        script_dir = Path(script_dir)
        if not library.is_file():
            raise EngineError(f"engine library not found: {library} (run scripts/build_engine.sh, or scripts/build_engine.ps1 on Windows)")
        if not script_dir.is_dir():
            raise EngineError(f"card script folder not found: {script_dir}")

        self.db = CardDatabase(databases)
        self.lib = ctypes.CDLL(str(library))
        self._declare_api()

        major, minor = c_int(), c_int()
        self.lib.OCG_GetVersion(ctypes.byref(major), ctypes.byref(minor))
        self.api_version = (major.value, minor.value)
        if major.value != SUPPORTED_API_MAJOR:
            raise EngineError(
                f"engine API {major.value}.{minor.value} is not supported "
                f"(this code was written for {SUPPORTED_API_MAJOR}.x)"
            )

        self._script_paths = self._index_scripts(script_dir)
        self._script_cache: dict[str, bytes] = {}
        self._setcode_arrays: dict[int, ctypes.Array] = {}
        self.log: list[tuple[int, str]] = []

        # ctypes callbacks must stay referenced or they get garbage collected.
        self._card_reader = DataReader(self._read_card)
        self._script_reader = ScriptReader(self._read_script)
        self._log_handler = LogHandler(self._handle_log)

    @classmethod
    def from_project_root(cls, root: Path) -> "Environment":
        """Standard layout: build/ for the library, vendor/ for data."""
        root = Path(root)
        cdb_dir = root / "vendor" / "BabelCDB"
        databases = [cdb_dir / "cards.cdb"]
        # Newest official releases live in their own files until merged.
        databases += sorted(cdb_dir.glob("release-*.cdb"))
        return cls(
            library=default_library_path(root),
            databases=databases,
            script_dir=root / "vendor" / "CardScripts",
        )

    def _declare_api(self) -> None:
        lib = self.lib
        lib.OCG_GetVersion.argtypes = [POINTER(c_int), POINTER(c_int)]
        lib.OCG_GetVersion.restype = None
        lib.OCG_CreateDuel.argtypes = [POINTER(c_void_p), POINTER(OCG_DuelOptions)]
        lib.OCG_CreateDuel.restype = c_int
        lib.OCG_DestroyDuel.argtypes = [c_void_p]
        lib.OCG_DestroyDuel.restype = None
        lib.OCG_DuelNewCard.argtypes = [c_void_p, POINTER(OCG_NewCardInfo)]
        lib.OCG_DuelNewCard.restype = None
        lib.OCG_StartDuel.argtypes = [c_void_p]
        lib.OCG_StartDuel.restype = None
        lib.OCG_DuelProcess.argtypes = [c_void_p]
        lib.OCG_DuelProcess.restype = c_int
        lib.OCG_DuelGetMessage.argtypes = [c_void_p, POINTER(c_uint32)]
        lib.OCG_DuelGetMessage.restype = c_void_p
        lib.OCG_DuelSetResponse.argtypes = [c_void_p, c_void_p, c_uint32]
        lib.OCG_DuelSetResponse.restype = None
        lib.OCG_LoadScript.argtypes = [c_void_p, c_char_p, c_uint32, c_char_p]
        lib.OCG_LoadScript.restype = c_int
        lib.OCG_DuelQueryCount.argtypes = [c_void_p, c_uint8, c_uint32]
        lib.OCG_DuelQueryCount.restype = c_uint32
        lib.OCG_DuelQueryLocation.argtypes = [c_void_p, POINTER(c_uint32), POINTER(OCG_QueryInfo)]
        lib.OCG_DuelQueryLocation.restype = c_void_p

    @staticmethod
    def _index_scripts(script_dir: Path) -> dict[str, Path]:
        """Map script file name -> path. Top level plus one folder level deep.

        Official scripts win over pre-release ones when both exist.
        """
        index: dict[str, Path] = {}
        subdirs = sorted(
            (p for p in script_dir.iterdir() if p.is_dir() and not p.name.startswith(".")),
            key=lambda p: (p.name != "official", p.name),
        )
        for folder in [script_dir, *subdirs]:
            for path in folder.glob("*.lua"):
                index.setdefault(path.name, path)
        if not all(name in index for name in BOOT_SCRIPTS):
            raise EngineError(f"{script_dir} does not look like a CardScripts checkout")
        return index

    # ---- callbacks (called by the C engine; must never raise)

    def _read_card(self, _payload, code, data_ptr) -> None:
        try:
            record = self.db.get(code)
            if record is None:
                return  # data stays zeroed; the engine treats it as "no such card"
            data = data_ptr.contents
            data.code = record.code
            data.alias = record.alias
            data.type = record.type
            data.level = record.level
            data.attribute = record.attribute
            data.race = record.race
            data.attack = record.attack
            data.defense = record.defense
            data.lscale = record.lscale
            data.rscale = record.rscale
            data.link_marker = record.link_marker
            array = self._setcode_arrays.get(code)
            if array is None:
                # Zero-terminated list of archetype codes, kept alive for the engine.
                array = (c_uint16 * (len(record.setcodes) + 1))(*record.setcodes, 0)
                self._setcode_arrays[code] = array
            data.setcodes = ctypes.cast(array, POINTER(c_uint16))
        except Exception as exc:  # noqa: BLE001 - must not propagate into C
            self.log.append((0, f"card reader failed for {code}: {exc!r}"))

    def _load_script_bytes(self, name: str) -> bytes | None:
        if not _SCRIPT_NAME_RE.fullmatch(name):
            self.log.append((0, f"refused script with unsafe name: {name[:80]!r}"))
            return None
        cached = self._script_cache.get(name)
        if cached is not None:
            return cached
        path = self._script_paths.get(name)
        if path is None:
            return None
        if path.stat().st_size > MAX_SCRIPT_BYTES:
            self.log.append((0, f"refused oversized script: {name}"))
            return None
        content = path.read_bytes()
        self._script_cache[name] = content
        return content

    def _read_script(self, _payload, duel, name) -> int:
        try:
            script_name = name.decode("utf-8", errors="strict")
            content = self._load_script_bytes(script_name)
            if content is None:
                return 0
            return self.lib.OCG_LoadScript(duel, content, len(content), name)
        except Exception as exc:  # noqa: BLE001 - must not propagate into C
            self.log.append((0, f"script reader failed: {exc!r}"))
            return 0

    def _handle_log(self, _payload, message, log_type) -> None:
        try:
            self.log.append((log_type, message.decode("utf-8", errors="replace")))
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------- Duel


@dataclass(frozen=True)
class Message:
    type: int
    data: bytes  # payload without the leading type byte


@dataclass(frozen=True)
class CardOnBoard:
    code: int
    position: int
    sequence: int


class Duel:
    """One duel. Use as a context manager so the engine memory is freed."""

    def __init__(
        self,
        env: Environment,
        seed: int,
        *,
        flags: int = C.DUEL_MODE_MR5,
        starting_lp: int = 8000,
        starting_draw: tuple[int, int] = (5, 5),
        draw_per_turn: int = 1,
    ):
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ValueError("seed must be a non-negative integer")
        self.env = env
        self._handle = c_void_p()
        self.pending: Message | None = None
        self.ended = False

        options = OCG_DuelOptions()
        # Spread the seed over the four 64-bit words; the engine rejects all-zero.
        for i in range(4):
            options.seed[i] = ((seed + 1) * 0x9E3779B97F4A7C15 + i * 0xBF58476D1CE4E5B9) & (
                (1 << 64) - 1
            ) or 1
        options.flags = flags
        options.team1 = OCG_Player(starting_lp, starting_draw[0], draw_per_turn)
        options.team2 = OCG_Player(starting_lp, starting_draw[1], draw_per_turn)
        options.cardReader = env._card_reader
        options.scriptReader = env._script_reader
        options.logHandler = env._log_handler
        options.enableUnsafeLibraries = 0  # card scripts get no file/OS access

        status = env.lib.OCG_CreateDuel(ctypes.byref(self._handle), ctypes.byref(options))
        if status != 0 or not self._handle:
            raise EngineError(f"OCG_CreateDuel failed with status {status}")
        for name in BOOT_SCRIPTS:
            content = env._load_script_bytes(name)
            if content is None or not env.lib.OCG_LoadScript(
                self._handle, content, len(content), name.encode()
            ):
                self.close()
                raise EngineError(f"could not load boot script {name}")

    # ---- lifecycle

    def close(self) -> None:
        if self._handle:
            self.env.lib.OCG_DestroyDuel(self._handle)
            self._handle = c_void_p()

    def __enter__(self) -> "Duel":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass

    # ---- setup

    def add_card(
        self,
        code: int,
        player: int,
        location: int,
        *,
        sequence: int = 0,
        position: int = C.POS_FACEDOWN_DEFENSE,
    ) -> None:
        validate_code(code)
        if player not in (0, 1):
            raise ValueError("player must be 0 or 1")
        if location not in C.LOCATION_NAMES:
            raise ValueError(f"unknown location: {location:#x}")
        if code not in self.env.db:
            raise ValueError(f"unknown card code: {code}")
        info = OCG_NewCardInfo(
            team=player, duelist=0, code=code, con=player, loc=location, seq=sequence, pos=position
        )
        self.env.lib.OCG_DuelNewCard(self._handle, ctypes.byref(info))

    def start(self) -> None:
        self.env.lib.OCG_StartDuel(self._handle)

    # ---- running

    def advance(self) -> list[Message]:
        """Run the engine until it needs a decision or the duel ends.

        Returns every message produced on the way. If a decision is needed it
        is stored in self.pending (and is also the last message returned).
        """
        if self.ended:
            return []
        out: list[Message] = []
        self.pending = None
        while True:
            status = self.env.lib.OCG_DuelProcess(self._handle)
            length = c_uint32()
            pointer = self.env.lib.OCG_DuelGetMessage(self._handle, ctypes.byref(length))
            buffer = ctypes.string_at(pointer, length.value) if length.value else b""
            for message in _split_messages(buffer):
                if message.type == C.MSG_RETRY:
                    raise InvalidResponse("engine rejected the last response")
                out.append(message)
                if message.type in C.DECISION_MESSAGES:
                    self.pending = message
            if status == C.DUEL_STATUS_END:
                self.ended = True
                return out
            if status == C.DUEL_STATUS_AWAITING:
                if self.pending is None:
                    raise EngineError("engine is waiting but sent no decision message")
                return out

    def respond(self, response: bytes) -> None:
        if self.pending is None:
            raise EngineError("no decision is pending")
        if not isinstance(response, (bytes, bytearray)) or not 0 < len(response) <= 4096:
            raise ValueError("response must be 1-4096 bytes")
        self.env.lib.OCG_DuelSetResponse(self._handle, bytes(response), len(response))
        self.pending = None

    # ---- reading the board

    def count(self, player: int, location: int) -> int:
        return self.env.lib.OCG_DuelQueryCount(self._handle, player, location)

    def cards_in(self, player: int, location: int) -> list[CardOnBoard]:
        """Cards a player has in one location, in engine order."""
        info = OCG_QueryInfo(
            flags=C.QUERY_CODE | C.QUERY_POSITION, con=player, loc=location, seq=0, overlay_seq=0
        )
        length = c_uint32()
        pointer = self.env.lib.OCG_DuelQueryLocation(
            self._handle, ctypes.byref(length), ctypes.byref(info)
        )
        if not pointer or length.value < 4:
            return []
        return _parse_location_query(ctypes.string_at(pointer, length.value))


def _split_messages(buffer: bytes) -> list[Message]:
    """The engine returns [u32 length][u8 type][payload] repeated."""
    messages: list[Message] = []
    offset = 0
    while offset + 4 <= len(buffer):
        (size,) = struct.unpack_from("<I", buffer, offset)
        offset += 4
        if size == 0 or offset + size > len(buffer):
            raise EngineError("malformed message buffer from engine")
        messages.append(Message(buffer[offset], buffer[offset + 1 : offset + size]))
        offset += size
    return messages


def _parse_location_query(buffer: bytes) -> list[CardOnBoard]:
    """Format: [u32 total] then per slot either [u16 0] (empty zone) or a list
    of [u16 size][u32 flag][value] entries ending with flag QUERY_END."""
    cards: list[CardOnBoard] = []
    offset = 4
    sequence = 0
    while offset + 2 <= len(buffer):
        code = position = 0
        (size,) = struct.unpack_from("<H", buffer, offset)
        if size == 0:  # empty zone
            offset += 2
            sequence += 1
            continue
        while offset + 6 <= len(buffer):
            size, flag = struct.unpack_from("<HI", buffer, offset)
            body = offset + 6
            offset += 2 + size
            if flag == C.QUERY_END:
                break
            if flag == C.QUERY_CODE:
                (code,) = struct.unpack_from("<I", buffer, body)
            elif flag == C.QUERY_POSITION:
                (position,) = struct.unpack_from("<I", buffer, body)
        if code:
            cards.append(CardOnBoard(code, position, sequence))
        sequence += 1
    return cards
