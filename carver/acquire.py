import os
import platform
import subprocess
import threading
from dataclasses import dataclass


@dataclass
class DriveInfo:
    device: str
    name: str
    size: int
    kind: str = ""
    removable: bool = False


def list_drives() -> list:
    """Enumerate drives available for imaging.

    Returns an empty list if the platform is unsupported or
    enumeration fails.
    """
    try:
        system = platform.system()

        if system == "Windows":
            return _list_windows_drives()

        if system == "Linux":
            return _list_linux_drives()

        return []

    except Exception:
        return []


def _list_windows_drives() -> list:
    """List Windows physical disks using PowerShell."""
    drives = []

    try:
        command = [
            "powershell",
            "-NoProfile",
            "-Command",
            (
                "Get-CimInstance Win32_DiskDrive | "
                "Select-Object DeviceID,Model,Size,MediaType | "
                "ConvertTo-Json -Compress"
            ),
        ]

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        if result.returncode != 0 or not result.stdout.strip():
            return []

        import json

        data = json.loads(result.stdout)

        if isinstance(data, dict):
            data = [data]

        for disk in data:
            device = str(disk.get("DeviceID") or "")
            name = str(disk.get("Model") or device)
            size = int(disk.get("Size") or 0)

            if device:
                drives.append(
                    DriveInfo(
                        device=device,
                        name=name,
                        size=size,
                        kind=str(disk.get("MediaType") or ""),
                    )
                )

    except Exception:
        return []

    return drives


def _list_linux_drives() -> list:
    """List Linux block devices using /sys/block."""
    drives = []

    try:
        base = "/sys/block"

        for device_name in os.listdir(base):
            device_path = os.path.join(base, device_name)
            size_path = os.path.join(device_path, "size")

            try:
                with open(size_path, "r", encoding="utf-8") as f:
                    sectors = int(f.read().strip())

                size = sectors * 512
                removable = False

                removable_path = os.path.join(device_path, "removable")
                if os.path.exists(removable_path):
                    with open(removable_path, "r", encoding="utf-8") as f:
                        removable = f.read().strip() == "1"

                drives.append(
                    DriveInfo(
                        device=f"/dev/{device_name}",
                        name=device_name,
                        size=size,
                        kind="block",
                        removable=removable,
                    )
                )

            except Exception:
                continue

    except Exception:
        return []

    return drives


def image_source(
    source,
    out_path,
    max_bytes=None,
    block=1 << 20,
    progress=None,
    cancel=None,
) -> int:
    """Read-only copy source -> out_path; return bytes written."""

    if block <= 0:
        raise ValueError("block must be greater than zero")

    if max_bytes is not None and max_bytes < 0:
        raise ValueError("max_bytes must not be negative")

    written = 0

    try:
        with open(source, "rb") as src:
            target = None

            try:
                if max_bytes is not None:
                    target = max_bytes
                else:
                    try:
                        target = os.fstat(src.fileno()).st_size
                    except OSError:
                        target = None

                with open(out_path, "wb") as dst:
                    while True:
                        if cancel is not None and cancel.is_set():
                            break

                        if max_bytes is not None:
                            remaining = max_bytes - written

                            if remaining <= 0:
                                break

                            chunk_size = min(block, remaining)
                        else:
                            chunk_size = block

                        chunk = src.read(chunk_size)

                        if not chunk:
                            break

                        dst.write(chunk)
                        written += len(chunk)

                        if progress is not None:
                            progress(written, target)

            except PermissionError as exc:
                raise PermissionError(
                    f"Unable to image source. "
                    f"Administrator/root privileges may be required: {exc}"
                ) from exc

    except PermissionError as exc:
        raise PermissionError(
            f"Unable to open source for read-only imaging: {source}. "
            f"Administrator/root privileges may be required."
        ) from exc

    return written