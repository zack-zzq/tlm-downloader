from __future__ import annotations

import hashlib
import json
import logging
import os
import posixpath
import re
import shutil
import signal
import sys
import time
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo, is_zipfile


LOGGER = logging.getLogger("tlm-auto-download")
DEFAULT_INFO_URL = "https://tlmdl.cfpa.team/info.json"
DEFAULT_PACK_MAX_BYTES = 25 * 1024 * 1024
MANIFEST_NAME = "tlm_auto_download_manifest.json"
STATE_NAME = "aggregate_state.json"
INFO_CACHE_NAME = "info.json"
CHUNK_SIZE = 1024 * 1024
SAFE_FILE_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Config:
    info_url: str
    cache_dir: Path
    output_zip: Path
    state_dir: Path
    interval_seconds: int
    download_delay_seconds: float
    timeout_seconds: int
    max_pack_bytes: int
    run_once: bool
    delete_stale: bool

    @staticmethod
    def from_env() -> "Config":
        return Config(
            info_url=os.getenv("TLM_INFO_URL", DEFAULT_INFO_URL),
            cache_dir=Path(os.getenv("TLM_CACHE_DIR", "/data/cache")),
            output_zip=Path(os.getenv("TLM_OUTPUT_ZIP", "/data/output/tlm_all_packs.zip")),
            state_dir=Path(os.getenv("TLM_STATE_DIR", "/data/state")),
            interval_seconds=env_int("TLM_INTERVAL_SECONDS", 6 * 60 * 60, minimum=60),
            download_delay_seconds=env_float("TLM_DOWNLOAD_DELAY_SECONDS", 3.0, minimum=0.0),
            timeout_seconds=env_int("TLM_HTTP_TIMEOUT_SECONDS", 60, minimum=5),
            max_pack_bytes=env_int("TLM_MAX_PACK_BYTES", DEFAULT_PACK_MAX_BYTES, minimum=1),
            run_once=env_bool("TLM_RUN_ONCE", False),
            delete_stale=env_bool("TLM_DELETE_STALE", False),
        )


@dataclass(frozen=True)
class DownloadEntry:
    index: int
    name: str
    file_name: str
    url: str
    checksum: int
    file_size: int
    raw_type: tuple[str, ...]

    @property
    def cache_key(self) -> str:
        return f"{self.checksum}-{self.file_name}"


@dataclass
class DownloadResult:
    entry: DownloadEntry
    path: Path
    downloaded: bool
    crc32: int
    bytes_size: int


class StopFlag:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self, signum: int, _frame: Any) -> None:
        LOGGER.info("Received signal %s, stopping after current operation", signum)
        self.stopped = True


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, *, minimum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        LOGGER.warning("Invalid integer for %s=%r, using %s", name, raw, default)
        return default
    return max(value, minimum)


def env_float(name: str, default: float, *, minimum: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        LOGGER.warning("Invalid number for %s=%r, using %s", name, raw, default)
        return default
    return max(value, minimum)


def configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("TLM_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def request_headers() -> dict[str, str]:
    return {
        "User-Agent": "TLM-Auto-Download/1.0",
        "Accept": "application/json,application/octet-stream,*/*",
    }


def fetch_info_json(config: Config) -> list[dict[str, Any]]:
    LOGGER.info("Fetching pack index from %s", config.info_url)
    request = Request(config.info_url, headers=request_headers())
    try:
        with urlopen(request, timeout=config.timeout_seconds) as response:
            payload = response.read(config.max_pack_bytes)
            if response.read(1):
                raise RuntimeError("info.json exceeds configured max read size")
    except (HTTPError, URLError, TimeoutError) as error:
        raise RuntimeError(f"failed to fetch info.json: {error}") from error

    config.state_dir.mkdir(parents=True, exist_ok=True)
    (config.state_dir / INFO_CACHE_NAME).write_bytes(payload)

    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, list):
        raise RuntimeError("info.json root must be a list")
    return data


def parse_entries(raw_entries: list[dict[str, Any]], info_url: str) -> list[DownloadEntry]:
    entries: list[DownloadEntry] = []
    seen_cache_keys: set[str] = set()

    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            LOGGER.warning("Skipping non-object entry at index %s", index)
            continue

        url = str(raw.get("url") or "").strip()
        if not url:
            LOGGER.warning("Skipping entry %s without url", index)
            continue

        checksum_raw = raw.get("checksum")
        try:
            checksum = int(checksum_raw)
        except (TypeError, ValueError):
            LOGGER.warning("Skipping entry %s without numeric checksum", index)
            continue

        file_name = safe_file_name(str(raw.get("file_name") or ""), url, index)
        file_size = int(raw.get("file_size") or 0)
        name = str(raw.get("name") or file_name)
        raw_type = tuple(str(item) for item in raw.get("type") or ())

        entry = DownloadEntry(
            index=index,
            name=name,
            file_name=file_name,
            url=absolute_pack_url(info_url, url),
            checksum=checksum,
            file_size=file_size,
            raw_type=raw_type,
        )

        if entry.cache_key in seen_cache_keys:
            LOGGER.info("Skipping duplicate index entry for %s", entry.file_name)
            continue
        seen_cache_keys.add(entry.cache_key)
        entries.append(entry)

    LOGGER.info("Parsed %s downloadable entries", len(entries))
    return entries


def absolute_pack_url(info_url: str, raw_url: str) -> str:
    root_url = info_url.rsplit("/", 1)[0] + "/"
    return urljoin(root_url, raw_url)


def safe_file_name(raw_file_name: str, raw_url: str, index: int) -> str:
    parsed_name = posixpath.basename(urlparse(raw_url).path)
    name = raw_file_name.strip() or parsed_name or f"pack-{index}.zip"
    name = PurePosixPath(name.replace("\\", "/")).name
    name = SAFE_FILE_RE.sub("_", name)
    if not name:
        name = f"pack-{index}.zip"
    if not name.lower().endswith(".zip"):
        name = f"{name}.zip"
    return name


def crc32_file(path: Path) -> int:
    checksum = 0
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(CHUNK_SIZE), b""):
            checksum = zlib.crc32(chunk, checksum)
    return checksum & 0xFFFFFFFF


def ensure_downloaded(entry: DownloadEntry, config: Config) -> DownloadResult:
    config.cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = config.cache_dir / entry.file_name

    if cache_path.is_file():
        local_crc = crc32_file(cache_path)
        if local_crc == entry.checksum and is_zipfile(cache_path):
            LOGGER.info("Cache hit: %s", entry.file_name)
            return DownloadResult(entry, cache_path, False, local_crc, cache_path.stat().st_size)
        LOGGER.info("Cache stale: %s local=%s remote=%s", entry.file_name, local_crc, entry.checksum)

    LOGGER.info("Downloading %s", entry.url)
    tmp_path = cache_path.with_name(f".{cache_path.name}.part")
    request = Request(entry.url, headers=request_headers())
    bytes_read = 0
    checksum = 0

    try:
        with urlopen(request, timeout=config.timeout_seconds) as response:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > config.max_pack_bytes:
                raise RuntimeError(f"{entry.file_name} exceeds max size before download")

            with tmp_path.open("wb") as output:
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    bytes_read += len(chunk)
                    if bytes_read > config.max_pack_bytes:
                        raise RuntimeError(f"{entry.file_name} exceeds max size during download")
                    checksum = zlib.crc32(chunk, checksum)
                    output.write(chunk)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    checksum &= 0xFFFFFFFF
    if checksum != entry.checksum:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"{entry.file_name} checksum mismatch: got {checksum}, expected {entry.checksum}")

    if not is_zipfile(tmp_path):
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"{entry.file_name} is not a valid zip file")

    tmp_path.replace(cache_path)
    LOGGER.info("Downloaded %s (%s bytes)", entry.file_name, bytes_read)
    return DownloadResult(entry, cache_path, True, checksum, bytes_read)


def delete_stale_cache(entries: list[DownloadEntry], cache_dir: Path) -> None:
    live_names = {entry.file_name for entry in entries}
    for path in cache_dir.glob("*.zip"):
        if path.name not in live_names:
            LOGGER.info("Deleting stale cached pack %s", path.name)
            path.unlink(missing_ok=True)


def source_state(results: list[DownloadResult], config: Config) -> dict[str, Any]:
    return {
        "info_url": config.info_url,
        "packs": [
            {
                "file_name": result.entry.file_name,
                "url": result.entry.url,
                "checksum": result.entry.checksum,
                "file_size": result.entry.file_size,
                "local_crc32": result.crc32,
                "local_size": result.bytes_size,
                "type": list(result.entry.raw_type),
            }
            for result in results
        ],
    }


def state_hash(state: dict[str, Any]) -> str:
    encoded = json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_previous_state(config: Config) -> dict[str, Any] | None:
    path = config.state_dir / STATE_NAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        LOGGER.warning("Ignoring invalid state file %s", path)
        return None


def save_state(config: Config, state: dict[str, Any]) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    state_path = config.state_dir / STATE_NAME
    state_path.write_text(json.dumps(state, ensure_ascii=True, indent=2), encoding="utf-8")


def should_rebuild(config: Config, new_state: dict[str, Any]) -> bool:
    if not config.output_zip.is_file() or not is_zipfile(config.output_zip):
        return True
    previous = load_previous_state(config)
    if previous is None:
        return True
    return previous.get("hash") != new_state.get("hash")


def valid_zip_member(name: str) -> str | None:
    normalized = name.replace("\\", "/").lstrip("/")
    if not normalized or normalized.endswith("/"):
        return None
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        return None
    if path.parts and path.parts[0] == "__MACOSX":
        return None
    if path.name == ".DS_Store":
        return None
    return str(path)


def add_directory(zip_file: ZipFile, directories: set[str], directory: str) -> None:
    if not directory.endswith("/"):
        directory = f"{directory}/"
    if directory in directories:
        return
    info = ZipInfo(directory)
    info.external_attr = 0o755 << 16
    zip_file.writestr(info, b"")
    directories.add(directory)


def ensure_parent_directories(zip_file: ZipFile, directories: set[str], member_name: str) -> None:
    parts = member_name.split("/")[:-1]
    current: list[str] = []
    for part in parts:
        current.append(part)
        add_directory(zip_file, directories, "/".join(current))


def merge_packs(results: list[DownloadResult], config: Config, state: dict[str, Any]) -> None:
    config.output_zip.parent.mkdir(parents=True, exist_ok=True)
    tmp_output = config.output_zip.with_name(f".{config.output_zip.name}.part")
    directories: set[str] = set()
    seen_files: dict[str, tuple[int, int, str]] = {}
    conflicts: list[dict[str, Any]] = []
    written_files = 0

    with ZipFile(tmp_output, "w", compression=ZIP_DEFLATED, compresslevel=6, allowZip64=True) as output:
        add_directory(output, directories, "assets")

        for result in results:
            with ZipFile(result.path, "r") as source:
                for member in source.infolist():
                    member_name = valid_zip_member(member.filename)
                    if member_name is None:
                        continue

                    previous = seen_files.get(member_name)
                    current = (member.CRC, member.file_size, result.entry.file_name)
                    if previous is not None:
                        if previous[:2] != current[:2]:
                            conflicts.append({
                                "path": member_name,
                                "kept_from": previous[2],
                                "skipped_from": result.entry.file_name,
                            })
                            LOGGER.warning(
                                "Conflict for %s, keeping %s and skipping %s",
                                member_name,
                                previous[2],
                                result.entry.file_name,
                            )
                        continue

                    ensure_parent_directories(output, directories, member_name)
                    target_info = ZipInfo(member_name, date_time=member.date_time)
                    target_info.external_attr = member.external_attr
                    target_info.compress_type = ZIP_DEFLATED
                    with source.open(member, "r") as source_file, output.open(target_info, "w") as target_file:
                        shutil.copyfileobj(source_file, target_file, CHUNK_SIZE)
                    seen_files[member_name] = current
                    written_files += 1

        manifest = {
            "generated_at": int(time.time()),
            "source": config.info_url,
            "pack_count": len(results),
            "written_file_count": written_files,
            "conflicts": conflicts,
            "packs": state["packs"],
        }
        output.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=True, indent=2))

    tmp_output.replace(config.output_zip)
    LOGGER.info("Wrote merged pack %s with %s files", config.output_zip, written_files)


def run_once(config: Config, stop_flag: StopFlag | None = None) -> None:
    raw_entries = fetch_info_json(config)
    entries = parse_entries(raw_entries, config.info_url)
    if not entries:
        raise RuntimeError("no downloadable packs found")

    if config.delete_stale:
        delete_stale_cache(entries, config.cache_dir)

    results: list[DownloadResult] = []
    for entry in entries:
        if stop_flag and stop_flag.stopped:
            LOGGER.info("Stop requested, ending before processing remaining packs")
            break
        result = ensure_downloaded(entry, config)
        results.append(result)
        if result.downloaded and config.download_delay_seconds > 0:
            time.sleep(config.download_delay_seconds)

    if len(results) != len(entries):
        raise RuntimeError("run stopped before all packs were processed")

    state = source_state(results, config)
    state["hash"] = state_hash(state)

    if should_rebuild(config, state):
        merge_packs(results, config, state)
        save_state(config, state)
    else:
        LOGGER.info("Merged pack is up to date: %s", config.output_zip)


def main() -> None:
    configure_logging()
    config = Config.from_env()
    stop_flag = StopFlag()
    signal.signal(signal.SIGTERM, stop_flag.stop)
    signal.signal(signal.SIGINT, stop_flag.stop)

    LOGGER.info("Starting TLM auto downloader")
    while not stop_flag.stopped:
        try:
            run_once(config, stop_flag)
        except Exception:
            LOGGER.exception("Auto-download cycle failed")
            if config.run_once:
                sys.exit(1)

        if config.run_once:
            break

        LOGGER.info("Sleeping for %s seconds", config.interval_seconds)
        deadline = time.monotonic() + config.interval_seconds
        while not stop_flag.stopped and time.monotonic() < deadline:
            time.sleep(min(5.0, deadline - time.monotonic()))

    LOGGER.info("Stopped TLM auto downloader")


if __name__ == "__main__":
    main()
