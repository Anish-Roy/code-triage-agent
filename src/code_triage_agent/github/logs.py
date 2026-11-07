"""Downloaded job logs: ZIP extraction and safe decoding."""

from __future__ import annotations

import io
import zipfile

MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
_ZIP_MAGIC = b"PK\x03\x04"


class LogExtractionError(RuntimeError):
    """Raised when a downloaded log archive is invalid or unreasonably large."""


def extract_log_text(data: bytes) -> str:
    """Return plain text from a GitHub job-log response.

    GitHub serves job logs as a ZIP archive containing one text file per step.
    A plain-text response is accepted too so recorded fixtures stay simple.
    """
    if not data:
        return ""
    if not data.startswith(_ZIP_MAGIC):
        return data.decode("utf-8", errors="replace")

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            total = sum(info.file_size for info in infos)
            if total > MAX_UNCOMPRESSED_BYTES:
                raise LogExtractionError(
                    f"job log archive expands to {total} bytes, over the "
                    f"{MAX_UNCOMPRESSED_BYTES} byte limit"
                )
            parts = [
                archive.read(info).decode("utf-8", errors="replace")
                for info in sorted(infos, key=lambda item: item.filename)
            ]
    except zipfile.BadZipFile as exc:
        raise LogExtractionError(f"invalid job log archive: {exc}") from exc

    return "\n".join(parts)
