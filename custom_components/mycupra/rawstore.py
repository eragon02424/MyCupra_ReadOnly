"""Ablage der vom Portal geladenen ZIP-Rohdateien (Aufbewahrung begrenzt).

Warum: Das Portal hält nur die letzten ~30 Dateien (ca. 7,5 h). Wer Rohdaten nachträglich
auswerten will (z. B. die UUID-Keys vergleichen), muss sie vor dem Herausfallen sichern.
Die Dateien liegen unter /share/mycupra/<VIN>/ und werden nach RAW_RETENTION_DAYS gelöscht.

Reine Standardbibliothek, keine Home-Assistant-Abhängigkeit (blockierend: nur im Executor
aufrufen).
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

_LOGGER = logging.getLogger(__name__)

RAW_ROOT = "/share/mycupra"
RAW_RETENTION_DAYS = 30

# Portal-Dateiname: 20261003150301_<VIN>.zip (UTC-Zeitstempel am Anfang)
_NAME_RE = re.compile(r"^(\d{14})_[A-Za-z0-9_\-]+\.zip$")


def raw_dir(vin: str) -> str:
    return os.path.join(RAW_ROOT, vin)


def _safe_name(name: str) -> bool:
    return bool(name) and os.path.basename(name) == name and name.endswith(".zip")


def name_timestamp(name: str) -> datetime | None:
    """UTC-Zeitstempel aus dem Dateinamen oder None."""
    m = _NAME_RE.match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def existing_names(directory: str) -> set[str]:
    try:
        return {n for n in os.listdir(directory) if n.endswith(".zip")}
    except OSError:
        return set()


def save_raw(directory: str, name: str, raw: bytes) -> bool:
    """Datei atomar ablegen. True = neu geschrieben, False = war schon identisch vorhanden."""
    if not _safe_name(name):
        raise ValueError(f"Unzulässiger Dateiname: {name!r}")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    if os.path.exists(path) and os.path.getsize(path) == len(raw):
        return False
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(raw)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return True


def prune(directory: str, max_age_days: int = RAW_RETENTION_DAYS, now: datetime | None = None) -> int:
    """Löscht ZIPs, die älter als max_age_days sind (Alter laut Dateiname, sonst Änderungszeit)."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age_days)
    removed = 0
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(directory, name)
        if name.endswith(".tmp"):
            # liegengebliebene Temp-Datei eines abgebrochenen Schreibvorgangs
            try:
                if time.time() - os.path.getmtime(path) > 3600:
                    os.remove(path)
            except OSError:
                pass
            continue
        if not name.endswith(".zip"):
            continue
        ts = name_timestamp(name)
        if ts is None:
            try:
                ts = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
            except OSError:
                continue
        if ts < cutoff:
            try:
                os.remove(path)
                removed += 1
            except OSError as err:
                _LOGGER.warning("Rohdatei %s nicht löschbar: %s", name, err)
    return removed
