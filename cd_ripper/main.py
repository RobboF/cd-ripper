"""
cd-ripper — Detect CD insertion and rip to FLAC with MusicBrainz metadata.

System deps (must be installed separately):
    sudo dnf install cdparanoia flac libdiscid

Python deps (managed by Poetry):
    discid, musicbrainzngs, pyudev

Usage:
    poetry run python -m cd_ripper.main
"""

import fcntl
import os
import re
import shutil
import subprocess
import urllib.request
import tempfile
import threading
import time
from pathlib import Path

import discid
import musicbrainzngs
import pyudev

musicbrainzngs.set_useragent("cd-ripper", "0.1.0", "rob@0x9.uk")

OUTPUT_ROOT = Path.home() / "Music"
_REQUIRED_BINS = ("cdparanoia", "flac")


def check_tools() -> None:
    missing = [t for t in _REQUIRED_BINS if not shutil.which(t)]
    if missing:
        raise SystemExit(
            f"Missing required tools: {', '.join(missing)}\n"
            f"Install with: sudo dnf install {' '.join(missing)} libdiscid"
        )


def _safe(name: str) -> str:
    """Strip characters that are illegal in file/directory names."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ")


def get_disc_metadata(device_node: str) -> dict | None:
    try:
        disc = discid.read(device_node)
    except discid.DiscError as exc:
        print(f"[!] Could not read disc ID: {exc}")
        return None

    print(f"[*] Disc ID: {disc.id} — querying MusicBrainz...")
    try:
        result = musicbrainzngs.get_releases_by_discid(
            disc.id,
            includes=["artists", "recordings"],
            toc=disc.toc_string,
        )
    except musicbrainzngs.ResponseError as exc:
        print(f"[!] MusicBrainz lookup failed: {exc}")
        return None

    release = (
        result["disc"]["release-list"][0]
        if "disc" in result
        else result["release"]
    )

    artist = release.get("artist-credit-phrase", "Unknown Artist")
    album = release.get("title", "Unknown Album")
    tracks = [
        (int(t["position"]), t["recording"]["title"])
        for t in release["medium-list"][0]["track-list"]
    ]
    return {"artist": artist, "album": album, "tracks": tracks}


def rip_cd(device_node: str, metadata: dict) -> None:
    out_dir = OUTPUT_ROOT / _safe(metadata["artist"]) / _safe(metadata["album"])
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[*] Output: {out_dir}")

    flac_paths = {
        track_num: out_dir / f"{str(track_num).zfill(2)}-{_safe(track_title)}.flac"
        for track_num, track_title in metadata["tracks"]
    }
    if all(p.exists() for p in flac_paths.values()):
        print(f"[*] All {len(flac_paths)} tracks already exist — skipping rip.")
        return

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        print("[*] Ripping with cdparanoia (-B batch mode)...")
        subprocess.run(
            ["cdparanoia", "-d", device_node, "-B"],
            cwd=tmp_path,
            check=True,
        )

        written = 0
        for track_num, track_title in metadata["tracks"]:
            num = str(track_num).zfill(2)
            wav = tmp_path / f"track{num}.cdda.wav"
            if not wav.exists():
                print(f"[!] {wav.name} not found — skipping track {num}")
                continue

            flac_out = flac_paths[track_num]
            if flac_out.exists():
                print(f"[*] {num}: already exists — skipping")
                continue

            print(f"[*] Encoding {num}: {track_title}")
            subprocess.run(
                [
                    "flac", "--best", "--silent",
                    f"--tag=ARTIST={metadata['artist']}",
                    f"--tag=ALBUM={metadata['album']}",
                    f"--tag=TRACKNUMBER={track_num}",
                    f"--tag=TITLE={track_title}",
                    f"--output-name={flac_out}",
                    str(wav),
                ],
                check=True,
            )
            written += 1

    print(f"[+] Done — {written} tracks written to {out_dir}")


_CDROM_DRIVE_STATUS = 0x5326  # <linux/cdrom.h>
_CDS_DISC_OK = 4


def _has_media(device_node: str) -> bool:
    """Return True if a disc is present (uses ioctl, never triggers block-layer reads)."""
    try:
        # O_NONBLOCK prevents the kernel from probing the disc on open, which
        # causes READ(10) failures on audio CDs and floods dmesg with I/O errors.
        fd = os.open(device_node, os.O_RDONLY | os.O_NONBLOCK)
        try:
            return fcntl.ioctl(fd, _CDROM_DRIVE_STATUS, 0) == _CDS_DISC_OK
        finally:
            os.close(fd)
    except OSError:
        return False


_NTFY_URL = os.environ.get("NTFY_URL", "http://ntfy.ntfy.svc.cluster.local/ripper")


def notify(title: str, message: str) -> None:
    if not _NTFY_URL:
        return
    try:
        req = urllib.request.Request(
            _NTFY_URL,
            data=message.encode(),
            headers={"Title": title},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        print(f"[!] ntfy notification failed: {exc}")


_CDROMEJECT = 0x5309  # <linux/cdrom.h>


def eject_cd(device_node: str) -> None:
    try:
        fd = os.open(device_node, os.O_RDONLY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(fd, _CDROMEJECT)
        finally:
            os.close(fd)
        print(f"[+] Ejected {device_node}")
    except OSError as exc:
        print(f"[!] Eject failed: {exc}")


def on_cd_inserted(device_node: str) -> None:
    print(f"[+] CD detected: {device_node}")
    metadata = get_disc_metadata(device_node)
    if metadata is None:
        print("[!] No metadata — aborting rip.")
        eject_cd(device_node)
        return
    print(f"[*] {metadata['artist']} — {metadata['album']} ({len(metadata['tracks'])} tracks)")
    rip_cd(device_node, metadata)
    notify(
        title="Rip complete",
        message=f"{metadata['artist']} — {metadata['album']} ({len(metadata['tracks'])} tracks)",
    )
    eject_cd(device_node)


POLL_DEVICE = os.environ.get("CD_DEVICE", "/dev/sr0")
POLL_INTERVAL = 5  # seconds


def _udev_monitor_loop(ripping: threading.Event, device_override: str) -> None:
    """Forward kernel block uevents to on_cd_inserted (requires hostNetwork: true)."""
    context = pyudev.Context()
    monitor = pyudev.Monitor.from_netlink(context)
    monitor.filter_by(subsystem="block")
    print("[udev] Listening for block device events.")

    for device in iter(monitor.poll, None):
        if device.action not in ("add", "change"):
            continue

        dev_type  = device.get("ID_TYPE", "")
        is_cdrom  = device.get("ID_CDROM", "")
        has_media = device.get("ID_CDROM_MEDIA", "")
        node      = device.device_node or ""

        if dev_type != "cd" and is_cdrom != "1" and "sr" not in node:
            continue

        target = node or device_override
        if has_media == "1":
            print(f"[udev] CD event on {target} (ID_CDROM_MEDIA=1).")
        else:
            # ID_CDROM_MEDIA is not set by TalosOS udev when the block-layer probe
            # fails (audio CDs return "Illegal mode for this track" on READ(10)).
            # Confirm via ioctl before proceeding.
            if not _has_media(target):
                print(f"[udev] CD event on {target} but ioctl reports no media — skipping.")
                continue
            print(f"[udev] CD event on {target} (ID_CDROM_MEDIA not set, confirmed via ioctl).")

        if ripping.is_set():
            print("[udev] Rip already in progress — ignoring event.")
            continue
        ripping.set()
        try:
            on_cd_inserted(target)
        finally:
            ripping.clear()


def _poll_loop(ripping: threading.Event, device_node: str) -> None:
    """Fallback: poll the drive directly so events missed by udev are still caught."""
    disc_was_present = False
    udev_triggered = False
    print(f"[poll] Polling {device_node} every {POLL_INTERVAL}s as fallback.")
    while True:
        time.sleep(POLL_INTERVAL)
        present = _has_media(device_node)
        if present and not disc_was_present:
            if ripping.is_set():
                # udev beat us to it
                udev_triggered = True
            else:
                print(f"[poll] Disc detected on {device_node} — udev did not fire, using poll fallback.")
                udev_triggered = False
                ripping.set()
                try:
                    on_cd_inserted(device_node)
                finally:
                    ripping.clear()
        elif not present and disc_was_present and not udev_triggered:
            print(f"[poll] Disc removed from {device_node}.")
        disc_was_present = present
        udev_triggered = False


def main() -> None:
    check_tools()

    ripping = threading.Event()

    print(f"Watching for CD/DVD insertion on {POLL_DEVICE}. Press Ctrl+C to stop.")

    poll_thread = threading.Thread(
        target=_poll_loop, args=(ripping, POLL_DEVICE), daemon=True
    )
    poll_thread.start()

    udev_thread = threading.Thread(
        target=_udev_monitor_loop, args=(ripping, POLL_DEVICE), daemon=True
    )
    udev_thread.start()

    try:
        udev_thread.join()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
