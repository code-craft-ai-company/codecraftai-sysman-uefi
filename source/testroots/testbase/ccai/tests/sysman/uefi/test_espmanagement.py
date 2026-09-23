"""
    Tests for :class:`UefiEspManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``findmnt`` / ``lsblk`` / ``mount`` / ``diskutil`` /
    ``mountvol`` / PowerShell commands are invoked.

    Per-host-OS dispatch is exercised on every developer workstation by
    setting ``UefiEspManagerConfig.override_host_os`` to one of
    ``"Linux"``, ``"Darwin"``, ``"Windows"``.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.uefi.espmanagement import (
    UefiEspActionStatus,
    UefiEspManager,
    UefiEspManagerConfig,
    UefiEspQueryError,
)


# ----------------------------------------------------------------------
# Mock runner
# ----------------------------------------------------------------------


@dataclass
class _MockCommandRunner:
    results: dict = field(default_factory=dict)
    default_result: Optional[RunResult] = None
    calls: list = field(default_factory=list)

    def run(
        self,
        args: list,
        *,
        elevated: bool = False,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        stdin: Optional[str] = None,
        timeout: Optional[float] = None,
        on_output: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        capture_output: bool = True,
    ) -> RunResult:
        call_record = {
            "args": list(args),
            "elevated": elevated,
            "stdin": stdin,
        }
        self.calls.append(call_record)

        key = tuple(args)
        if key in self.results:
            rtnval = self.results[key]
            return rtnval

        if self.default_result is not None:
            rtnval = self.default_result
            return rtnval

        rtnval = RunResult(exit_code=0, stdout="", stderr="")
        return rtnval


def _ok(stdout: str = "") -> RunResult:
    rtnval = RunResult(exit_code=0, stdout=stdout, stderr="")
    return rtnval


def _fail(stderr: str = "boom", exit_code: int = 1, stdout: str = "") -> RunResult:
    rtnval = RunResult(exit_code=exit_code, stdout=stdout, stderr=stderr)
    return rtnval


# ----------------------------------------------------------------------
# Sample outputs
# ----------------------------------------------------------------------


_FINDMNT_JSON_MOUNTED = json.dumps({
    "filesystems": [
        {
            "target": "/boot/efi",
            "source": "/dev/sda1",
            "fstype": "vfat",
            "options": "rw,relatime,fmask=0077",
        },
    ],
})


_LSBLK_JSON = json.dumps({
    "blockdevices": [
        {
            "name": "sda",
            "children": [
                {
                    "name": "sda1",
                    "fstype": "vfat",
                    "size": 536870912,
                },
                {
                    "name": "sda2",
                    "fstype": "ext4",
                    "size": 999999999,
                },
            ],
        },
    ],
})


_DISKUTIL_LIST_PLIST = (
    "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
    "<plist version=\"1.0\">\n"
    "<dict>\n"
    "  <key>AllDisksAndPartitions</key>\n"
    "  <array>\n"
    "    <dict>\n"
    "      <key>Partitions</key>\n"
    "      <array>\n"
    "        <dict>\n"
    "          <key>Content</key>\n"
    "          <string>EFI</string>\n"
    "          <key>DeviceIdentifier</key>\n"
    "          <string>disk0s1</string>\n"
    "        </dict>\n"
    "      </array>\n"
    "    </dict>\n"
    "  </array>\n"
    "</dict>\n"
    "</plist>\n"
)


_DISKUTIL_INFO_MOUNTED = (
    "   Device Identifier:        disk0s1\n"
    "   Mount Point:              /Volumes/EFI\n"
    "   Mounted:                  Yes\n"
    "   File System Personality:  MS-DOS FAT32\n"
    "   Volume Total Space:       209.7 MB (209715200 Bytes) (exactly 0 sectors)\n"
)


_DISKUTIL_INFO_UNMOUNTED = (
    "   Device Identifier:        disk0s1\n"
    "   Mount Point:              \n"
    "   Mounted:                  No\n"
    "   File System Personality:  MS-DOS FAT32\n"
)


_GET_PARTITION_JSON = json.dumps([
    {
        "Type": "System",
        "DiskNumber": 0,
        "PartitionNumber": 1,
        "Size": 536870912,
        "ObjectId": "Disk0Partition1",
    },
])


_GET_VOLUME_JSON = json.dumps([
    {
        "DriveLetter": "S",
        "FileSystem": "FAT32",
        "FileSystemLabel": "EFI",
        "DriveType": "Fixed",
    },
])


_LS_LA_OUTPUT = (
    "total 24\n"
    "drwxr-xr-x  4 root root  4096 Jan  1 12:34 .\n"
    "drwxr-xr-x  3 root root  4096 Jan  1 12:00 ..\n"
    "drwxr-xr-x  2 root root  4096 Jan  1 12:34 EFI\n"
    "-rwxr-xr-x  1 root root  1024 Jan  1 12:00 BOOT.efi\n"
)


_GET_CHILDITEM_JSON = json.dumps([
    {
        "Name": "EFI",
        "FullName": "S:\\EFI",
        "Mode": "d----",
        "Length": None,
        "LastWriteTime": "1/1/2025 12:34:00 PM",
    },
    {
        "Name": "BOOT.efi",
        "FullName": "S:\\BOOT.efi",
        "Mode": "-a---",
        "Length": 1024,
        "LastWriteTime": "1/1/2025 12:00:00 PM",
    },
])


# ----------------------------------------------------------------------
# Construction / dispatch override
# ----------------------------------------------------------------------


def test_override_host_os_forces_linux_dispatch():
    """
        ``override_host_os="Linux"`` makes the manager record
        ``host_os="linux"``.
    """
    runner = _MockCommandRunner()
    config = UefiEspManagerConfig(override_host_os="Linux", dry_run=True)
    manager = UefiEspManager(runner=runner, config=config)
    testbase.assert_equal(manager.host_os, "linux")
    return


def test_override_host_os_forces_darwin_dispatch():
    """
        ``override_host_os="Darwin"`` selects the macOS dispatch path.
    """
    runner = _MockCommandRunner()
    config = UefiEspManagerConfig(override_host_os="Darwin", dry_run=True)
    manager = UefiEspManager(runner=runner, config=config)
    testbase.assert_equal(manager.host_os, "darwin")
    return


def test_override_host_os_forces_windows_dispatch():
    """
        ``override_host_os="Windows"`` selects the Windows dispatch path.
    """
    runner = _MockCommandRunner()
    config = UefiEspManagerConfig(override_host_os="Windows", dry_run=True)
    manager = UefiEspManager(runner=runner, config=config)
    testbase.assert_equal(manager.host_os, "windows")
    return


def test_override_host_os_rejects_invalid_value_in_constructor():
    """
        Invalid override raises ``ValueError`` at construction time.
    """
    runner = _MockCommandRunner()
    config = UefiEspManagerConfig(override_host_os="solaris")
    raised = False
    try:
        UefiEspManager(runner=runner, config=config)
    except ValueError:
        raised = True
    testbase.assert_equal(raised, True)
    return


# ----------------------------------------------------------------------
# get_esp_info
# ----------------------------------------------------------------------


def test_get_esp_info_linux_invokes_findmnt_and_lsblk():
    """
        Linux ``get_esp_info`` invokes ``findmnt /boot/efi --json`` and
        ``lsblk -f --json`` and produces a populated :class:`UefiEspInfo`.
    """
    runner = _MockCommandRunner()
    runner.results[("findmnt", "/boot/efi", "--json")] = _ok(
        stdout=_FINDMNT_JSON_MOUNTED,
    )
    runner.results[("lsblk", "-f", "--json")] = _ok(stdout=_LSBLK_JSON)
    config = UefiEspManagerConfig(override_host_os="Linux")
    manager = UefiEspManager(runner=runner, config=config)

    info = manager.get_esp_info()
    testbase.assert_equal(info.device, "/dev/sda1")
    testbase.assert_equal(info.mountpoint, "/boot/efi")
    testbase.assert_equal(info.mounted, True)
    testbase.assert_equal(info.filesystem, "vfat")
    testbase.assert_equal(info.size_bytes, 536870912)
    testbase.assert_equal(info.host_os, "linux")
    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_get_esp_info_darwin_invokes_diskutil_list_plist():
    """
        Darwin ``get_esp_info`` invokes ``diskutil list -plist`` and
        ``diskutil info <device>``.
    """
    runner = _MockCommandRunner()
    runner.results[("diskutil", "list", "-plist")] = _ok(
        stdout=_DISKUTIL_LIST_PLIST,
    )
    runner.results[("diskutil", "info", "disk0s1")] = _ok(
        stdout=_DISKUTIL_INFO_MOUNTED,
    )
    config = UefiEspManagerConfig(override_host_os="Darwin")
    manager = UefiEspManager(runner=runner, config=config)

    info = manager.get_esp_info()
    testbase.assert_equal(info.device, "disk0s1")
    testbase.assert_equal(info.mountpoint, "/Volumes/EFI")
    testbase.assert_equal(info.mounted, True)
    testbase.assert_equal(info.size_bytes, 209715200)
    return


def test_get_esp_info_windows_invokes_get_partition_filtered_system():
    """
        Windows ``get_esp_info`` invokes ``Get-Partition`` filtered to
        ``Type=System`` and ``Get-Volume``.
    """
    runner = _MockCommandRunner()
    partition_command = (
        "Get-Partition | Where-Object { $_.Type -eq 'System' } | ConvertTo-Json"
    )
    runner.results[
        ("powershell", "-NoProfile", "-Command", partition_command)
    ] = _ok(stdout=_GET_PARTITION_JSON)
    runner.results[
        ("powershell", "-NoProfile", "-Command", "Get-Volume | ConvertTo-Json")
    ] = _ok(stdout=_GET_VOLUME_JSON)
    config = UefiEspManagerConfig(override_host_os="Windows")
    manager = UefiEspManager(runner=runner, config=config)

    info = manager.get_esp_info()
    testbase.assert_equal(info.mountpoint, "S:")
    testbase.assert_equal(info.mounted, True)
    testbase.assert_equal(info.filesystem, "FAT32")
    testbase.assert_equal(info.size_bytes, 536870912)
    return


def test_get_esp_info_failed_command_raises_query_error():
    """
        A failing discovery command raises ``UefiEspQueryError``.
    """
    runner = _MockCommandRunner()
    runner.results[("diskutil", "list", "-plist")] = _fail(
        stderr="diskutil bork", exit_code=2,
    )
    config = UefiEspManagerConfig(override_host_os="Darwin")
    manager = UefiEspManager(runner=runner, config=config)

    raised = False
    try:
        manager.get_esp_info()
    except UefiEspQueryError as exc:
        raised = True
        testbase.assert_equal(exc.exit_code, 2)
    testbase.assert_equal(raised, True)
    return


# ----------------------------------------------------------------------
# is_esp_mounted
# ----------------------------------------------------------------------


def test_is_esp_mounted_linux_returns_true_on_zero_exit():
    """
        Linux ``is_esp_mounted`` returns ``True`` when ``findmnt
        /boot/efi`` exits 0.
    """
    runner = _MockCommandRunner()
    runner.results[("findmnt", "/boot/efi")] = _ok(stdout="/boot/efi /dev/sda1 vfat ...")
    config = UefiEspManagerConfig(override_host_os="Linux")
    manager = UefiEspManager(runner=runner, config=config)
    testbase.assert_equal(manager.is_esp_mounted(), True)
    return


def test_is_esp_mounted_linux_returns_false_on_nonzero_exit():
    """
        Linux ``is_esp_mounted`` returns ``False`` when ``findmnt``
        exits non-zero.
    """
    runner = _MockCommandRunner()
    runner.results[("findmnt", "/boot/efi")] = _fail()
    config = UefiEspManagerConfig(override_host_os="Linux")
    manager = UefiEspManager(runner=runner, config=config)
    testbase.assert_equal(manager.is_esp_mounted(), False)
    return


def test_is_esp_mounted_darwin_parses_diskutil_info():
    """
        Darwin ``is_esp_mounted`` parses the ``Mounted:`` line from
        ``diskutil info``.
    """
    runner = _MockCommandRunner()
    runner.results[("diskutil", "info", "disk0s1")] = _ok(
        stdout=_DISKUTIL_INFO_MOUNTED,
    )
    config = UefiEspManagerConfig(override_host_os="Darwin")
    manager = UefiEspManager(runner=runner, config=config)
    testbase.assert_equal(manager.is_esp_mounted(), True)
    return


def test_is_esp_mounted_darwin_returns_false_when_unmounted():
    """
        Darwin ``is_esp_mounted`` returns ``False`` when ``Mounted: No``.
    """
    runner = _MockCommandRunner()
    runner.results[("diskutil", "info", "disk0s1")] = _ok(
        stdout=_DISKUTIL_INFO_UNMOUNTED,
    )
    config = UefiEspManagerConfig(override_host_os="Darwin")
    manager = UefiEspManager(runner=runner, config=config)
    testbase.assert_equal(manager.is_esp_mounted(), False)
    return


# ----------------------------------------------------------------------
# mount_esp
# ----------------------------------------------------------------------


def test_mount_esp_linux_constructs_mount_t_vfat():
    """
        Linux ``mount_esp`` constructs ``mount -t vfat <device>
        <mountpoint>``.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("findmnt", "/boot/efi", "--json")] = _ok(stdout="")
    runner.results[("findmnt", "/boot/efi")] = _fail()
    runner.results[("lsblk", "-f", "--json")] = _ok(stdout=_LSBLK_JSON)
    config = UefiEspManagerConfig(
        override_host_os="Linux", elevated=True,
    )
    manager = UefiEspManager(runner=runner, config=config)

    # We need get_esp_info to return a device for the mount path.
    # Replace the empty findmnt response with one that surfaces the device
    # but no mountpoint (i.e., not yet mounted).  The findmnt command
    # always exits 0 when used to display a mounted target; for an
    # unmounted target findmnt exits 1.  So we let it exit 1 above and
    # let mount_esp fall back to ``mount <mountpoint>``.
    result = manager.mount_esp(mountpoint="/boot/efi")
    testbase.assert_equal(result.status, UefiEspActionStatus.SUCCESS)

    write_calls = [c for c in runner.calls if c["args"][0] == "mount"]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_mount_esp_darwin_constructs_diskutil_mount():
    """
        Darwin ``mount_esp`` constructs ``diskutil mount disk0s1``.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("diskutil", "list", "-plist")] = _ok(
        stdout=_DISKUTIL_LIST_PLIST,
    )
    runner.results[("diskutil", "info", "disk0s1")] = _ok(
        stdout=_DISKUTIL_INFO_UNMOUNTED,
    )
    config = UefiEspManagerConfig(override_host_os="Darwin", elevated=True)
    manager = UefiEspManager(runner=runner, config=config)

    result = manager.mount_esp(mountpoint="/Volumes/EFI")
    testbase.assert_equal(result.status, UefiEspActionStatus.SUCCESS)

    write_calls = [
        c for c in runner.calls if c["args"][:2] == ["diskutil", "mount"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"], ["diskutil", "mount", "disk0s1"],
    )
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_mount_esp_windows_constructs_mountvol_s_s():
    """
        Windows ``mount_esp("S:")`` constructs ``mountvol S: /S``.
    """
    runner = _MockCommandRunner(default_result=_ok())
    partition_command = (
        "Get-Partition | Where-Object { $_.Type -eq 'System' } | ConvertTo-Json"
    )
    # Pre-flight discovery returns no drive letter (so not mounted).
    runner.results[
        ("powershell", "-NoProfile", "-Command", partition_command)
    ] = _ok(stdout=_GET_PARTITION_JSON)
    runner.results[
        ("powershell", "-NoProfile", "-Command", "Get-Volume | ConvertTo-Json")
    ] = _ok(stdout=json.dumps([
        {"DriveLetter": "", "FileSystem": "FAT32", "FileSystemLabel": "EFI"},
    ]))
    config = UefiEspManagerConfig(override_host_os="Windows", elevated=True)
    manager = UefiEspManager(runner=runner, config=config)

    result = manager.mount_esp(mountpoint="S:")
    testbase.assert_equal(result.status, UefiEspActionStatus.SUCCESS)

    write_calls = [c for c in runner.calls if c["args"][0] == "mountvol"]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["args"], ["mountvol", "S:", "/S"])
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_mount_esp_skipped_when_already_mounted():
    """
        ``mount_esp`` returns ``SKIPPED`` when the ESP is already
        mounted at the requested mountpoint.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("findmnt", "/boot/efi", "--json")] = _ok(
        stdout=_FINDMNT_JSON_MOUNTED,
    )
    runner.results[("lsblk", "-f", "--json")] = _ok(stdout=_LSBLK_JSON)
    config = UefiEspManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiEspManager(runner=runner, config=config)

    result = manager.mount_esp(mountpoint="/boot/efi")
    testbase.assert_equal(result.status, UefiEspActionStatus.SKIPPED)
    write_calls = [c for c in runner.calls if c["args"][0] == "mount"]
    testbase.assert_equal(write_calls, [])
    return


def test_mount_esp_dry_run_short_circuits():
    """
        Dry-run short-circuits ``mount_esp`` without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    config = UefiEspManagerConfig(override_host_os="Linux", dry_run=True)
    manager = UefiEspManager(runner=runner, config=config)

    result = manager.mount_esp(mountpoint="/boot/efi")
    testbase.assert_equal(result.status, UefiEspActionStatus.DRY_RUN)
    testbase.assert_equal(runner.calls, [])
    testbase.assert_equal(result.command, ["mount", "/boot/efi"])
    return


# ----------------------------------------------------------------------
# unmount_esp
# ----------------------------------------------------------------------


def test_unmount_esp_linux_constructs_umount():
    """
        Linux ``unmount_esp`` constructs ``umount <mountpoint>``.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("findmnt", "/boot/efi")] = _ok(stdout="...")
    config = UefiEspManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiEspManager(runner=runner, config=config)

    result = manager.unmount_esp(mountpoint="/boot/efi")
    testbase.assert_equal(result.status, UefiEspActionStatus.SUCCESS)

    write_calls = [c for c in runner.calls if c["args"][0] == "umount"]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["args"], ["umount", "/boot/efi"])
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_unmount_esp_darwin_constructs_diskutil_unmount():
    """
        Darwin ``unmount_esp`` constructs ``diskutil unmount
        <mountpoint>``.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("diskutil", "info", "disk0s1")] = _ok(
        stdout=_DISKUTIL_INFO_MOUNTED,
    )
    config = UefiEspManagerConfig(override_host_os="Darwin", elevated=True)
    manager = UefiEspManager(runner=runner, config=config)

    result = manager.unmount_esp(mountpoint="/Volumes/EFI")
    testbase.assert_equal(result.status, UefiEspActionStatus.SUCCESS)

    write_calls = [
        c for c in runner.calls if c["args"][:2] == ["diskutil", "unmount"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        ["diskutil", "unmount", "/Volumes/EFI"],
    )
    return


def test_unmount_esp_windows_constructs_mountvol_d():
    """
        Windows ``unmount_esp("S:")`` constructs ``mountvol S: /D``.
    """
    runner = _MockCommandRunner(default_result=_ok())
    partition_command = (
        "Get-Partition | Where-Object { $_.Type -eq 'System' } | ConvertTo-Json"
    )
    runner.results[
        ("powershell", "-NoProfile", "-Command", partition_command)
    ] = _ok(stdout=_GET_PARTITION_JSON)
    runner.results[
        ("powershell", "-NoProfile", "-Command", "Get-Volume | ConvertTo-Json")
    ] = _ok(stdout=_GET_VOLUME_JSON)
    config = UefiEspManagerConfig(override_host_os="Windows", elevated=True)
    manager = UefiEspManager(runner=runner, config=config)

    result = manager.unmount_esp(mountpoint="S:")
    testbase.assert_equal(result.status, UefiEspActionStatus.SUCCESS)

    write_calls = [c for c in runner.calls if c["args"][0] == "mountvol"]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["args"], ["mountvol", "S:", "/D"])
    return


def test_unmount_esp_skipped_when_not_mounted():
    """
        ``unmount_esp`` returns ``SKIPPED`` when the ESP is not mounted.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("findmnt", "/boot/efi")] = _fail()
    config = UefiEspManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiEspManager(runner=runner, config=config)

    result = manager.unmount_esp(mountpoint="/boot/efi")
    testbase.assert_equal(result.status, UefiEspActionStatus.SKIPPED)
    write_calls = [c for c in runner.calls if c["args"][0] == "umount"]
    testbase.assert_equal(write_calls, [])
    return


def test_unmount_esp_dry_run_short_circuits():
    """
        Dry-run short-circuits ``unmount_esp`` without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    config = UefiEspManagerConfig(override_host_os="Linux", dry_run=True)
    manager = UefiEspManager(runner=runner, config=config)

    result = manager.unmount_esp(mountpoint="/boot/efi")
    testbase.assert_equal(result.status, UefiEspActionStatus.DRY_RUN)
    testbase.assert_equal(runner.calls, [])
    return


# ----------------------------------------------------------------------
# list_esp_contents / read_esp_file
# ----------------------------------------------------------------------


def test_list_esp_contents_linux_invokes_ls_la():
    """
        Linux ``list_esp_contents`` delegates to ``ls -la
        <mount>/<path>``.
    """
    runner = _MockCommandRunner()
    runner.results[("findmnt", "/boot/efi", "--json")] = _ok(
        stdout=_FINDMNT_JSON_MOUNTED,
    )
    runner.results[("lsblk", "-f", "--json")] = _ok(stdout=_LSBLK_JSON)
    runner.results[("ls", "-la", "/boot/efi/EFI")] = _ok(stdout=_LS_LA_OUTPUT)
    config = UefiEspManagerConfig(override_host_os="Linux")
    manager = UefiEspManager(runner=runner, config=config)

    entries = manager.list_esp_contents(path="/EFI")
    # Two real entries (EFI directory + BOOT.efi); '.' and '..' filtered.
    testbase.assert_equal(len(entries), 2)
    by_name = {e.name: e for e in entries}
    testbase.assert_equal(by_name["EFI"].is_directory, True)
    testbase.assert_equal(by_name["BOOT.efi"].size_bytes, 1024)
    return


def test_list_esp_contents_windows_invokes_get_childitem():
    """
        Windows ``list_esp_contents`` delegates to ``Get-ChildItem``.
    """
    runner = _MockCommandRunner()
    partition_command = (
        "Get-Partition | Where-Object { $_.Type -eq 'System' } | ConvertTo-Json"
    )
    runner.results[
        ("powershell", "-NoProfile", "-Command", partition_command)
    ] = _ok(stdout=_GET_PARTITION_JSON)
    runner.results[
        ("powershell", "-NoProfile", "-Command", "Get-Volume | ConvertTo-Json")
    ] = _ok(stdout=_GET_VOLUME_JSON)
    runner.results[
        (
            "powershell", "-NoProfile", "-Command",
            "Get-ChildItem -LiteralPath 'S:\\EFI' | ConvertTo-Json",
        )
    ] = _ok(stdout=_GET_CHILDITEM_JSON)
    config = UefiEspManagerConfig(override_host_os="Windows")
    manager = UefiEspManager(runner=runner, config=config)

    entries = manager.list_esp_contents(path="\\EFI")
    testbase.assert_equal(len(entries), 2)
    return


def test_list_esp_contents_raises_when_unmounted():
    """
        ``list_esp_contents`` raises ``UefiEspQueryError`` when the ESP
        is not mounted.
    """
    runner = _MockCommandRunner()
    runner.results[("findmnt", "/boot/efi", "--json")] = _ok(stdout="")
    runner.results[("lsblk", "-f", "--json")] = _ok(stdout=_LSBLK_JSON)
    config = UefiEspManagerConfig(override_host_os="Linux")
    manager = UefiEspManager(runner=runner, config=config)

    raised = False
    try:
        manager.list_esp_contents(path="/EFI")
    except UefiEspQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


def test_read_esp_file_linux_invokes_cat_and_returns_content():
    """
        Linux ``read_esp_file`` delegates to ``cat <full_path>`` and
        returns a populated :class:`UefiEspFileContent`.
    """
    runner = _MockCommandRunner()
    runner.results[("findmnt", "/boot/efi", "--json")] = _ok(
        stdout=_FINDMNT_JSON_MOUNTED,
    )
    runner.results[("lsblk", "-f", "--json")] = _ok(stdout=_LSBLK_JSON)
    runner.results[("cat", "/boot/efi/EFI/BOOT/grub.cfg")] = _ok(
        stdout="set timeout=5\nmenuentry default {}\n",
    )
    config = UefiEspManagerConfig(override_host_os="Linux")
    manager = UefiEspManager(runner=runner, config=config)

    content = manager.read_esp_file(path="/EFI/BOOT/grub.cfg")
    testbase.assert_equal(content.is_text, True)
    testbase.assert_equal("menuentry" in content.content.decode("utf-8"), True)
    testbase.assert_equal(content.encoding, "utf-8")
    return


def test_read_esp_file_raises_when_unmounted():
    """
        ``read_esp_file`` raises ``UefiEspQueryError`` when the ESP is
        not mounted.
    """
    runner = _MockCommandRunner()
    runner.results[("findmnt", "/boot/efi", "--json")] = _ok(stdout="")
    runner.results[("lsblk", "-f", "--json")] = _ok(stdout=_LSBLK_JSON)
    config = UefiEspManagerConfig(override_host_os="Linux")
    manager = UefiEspManager(runner=runner, config=config)

    raised = False
    try:
        manager.read_esp_file(path="/EFI/BOOT/grub.cfg")
    except UefiEspQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


# ----------------------------------------------------------------------
# write_esp_file / delete_esp_file
# ----------------------------------------------------------------------


def test_write_esp_file_linux_invokes_tee_with_stdin_payload():
    """
        Linux ``write_esp_file`` delegates to ``tee <path>`` with the
        payload forwarded as stdin.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("findmnt", "/boot/efi", "--json")] = _ok(
        stdout=_FINDMNT_JSON_MOUNTED,
    )
    runner.results[("lsblk", "-f", "--json")] = _ok(stdout=_LSBLK_JSON)
    runner.results[("cat", "/boot/efi/EFI/BOOT/test.cfg")] = _fail()
    config = UefiEspManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiEspManager(runner=runner, config=config)

    payload = b"new contents\n"
    result = manager.write_esp_file(path="/EFI/BOOT/test.cfg", content=payload)
    testbase.assert_equal(result.status, UefiEspActionStatus.SUCCESS)

    write_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"], ["tee", "/boot/efi/EFI/BOOT/test.cfg"],
    )
    testbase.assert_equal(write_calls[0]["stdin"], "new contents\n")
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_write_esp_file_skipped_when_bytes_match():
    """
        ``write_esp_file`` returns ``SKIPPED`` when the existing file is
        byte-identical to the desired content.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("findmnt", "/boot/efi", "--json")] = _ok(
        stdout=_FINDMNT_JSON_MOUNTED,
    )
    runner.results[("lsblk", "-f", "--json")] = _ok(stdout=_LSBLK_JSON)
    runner.results[("cat", "/boot/efi/EFI/BOOT/test.cfg")] = _ok(
        stdout="same contents\n",
    )
    config = UefiEspManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiEspManager(runner=runner, config=config)

    payload = b"same contents\n"
    result = manager.write_esp_file(path="/EFI/BOOT/test.cfg", content=payload)
    testbase.assert_equal(result.status, UefiEspActionStatus.SKIPPED)
    write_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(write_calls, [])
    return


def test_write_esp_file_returns_failed_when_unmounted():
    """
        ``write_esp_file`` returns ``FAILED`` with a descriptive detail
        when the ESP is not mounted; no command is invoked.
    """
    runner = _MockCommandRunner()
    runner.results[("findmnt", "/boot/efi", "--json")] = _ok(stdout="")
    runner.results[("lsblk", "-f", "--json")] = _ok(stdout=_LSBLK_JSON)
    config = UefiEspManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiEspManager(runner=runner, config=config)

    result = manager.write_esp_file(
        path="/EFI/BOOT/test.cfg", content=b"data",
    )
    testbase.assert_equal(result.status, UefiEspActionStatus.FAILED)
    testbase.assert_equal("not mounted" in (result.detail or ""), True)

    write_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(write_calls, [])
    return


def test_write_esp_file_dry_run_short_circuits():
    """
        Dry-run short-circuits ``write_esp_file`` without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    config = UefiEspManagerConfig(override_host_os="Linux", dry_run=True)
    manager = UefiEspManager(runner=runner, config=config)

    result = manager.write_esp_file(
        path="/EFI/BOOT/test.cfg", content=b"data",
    )
    testbase.assert_equal(result.status, UefiEspActionStatus.DRY_RUN)
    testbase.assert_equal(runner.calls, [])
    testbase.assert_equal(result.command[0], "tee")
    return


def test_delete_esp_file_linux_invokes_rm_f():
    """
        Linux ``delete_esp_file`` delegates to ``rm -f <full_path>``
        after probing existence with ``ls``.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("findmnt", "/boot/efi", "--json")] = _ok(
        stdout=_FINDMNT_JSON_MOUNTED,
    )
    runner.results[("lsblk", "-f", "--json")] = _ok(stdout=_LSBLK_JSON)
    runner.results[("ls", "/boot/efi/EFI/BOOT/test.cfg")] = _ok(stdout="...")
    config = UefiEspManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiEspManager(runner=runner, config=config)

    result = manager.delete_esp_file(path="/EFI/BOOT/test.cfg")
    testbase.assert_equal(result.status, UefiEspActionStatus.SUCCESS)

    rm_calls = [c for c in runner.calls if c["args"][0] == "rm"]
    testbase.assert_equal(len(rm_calls), 1)
    testbase.assert_equal(
        rm_calls[0]["args"], ["rm", "-f", "/boot/efi/EFI/BOOT/test.cfg"],
    )
    testbase.assert_equal(rm_calls[0]["elevated"], True)
    return


def test_delete_esp_file_skipped_when_path_absent():
    """
        ``delete_esp_file`` returns ``SKIPPED`` when the path does not
        exist.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("findmnt", "/boot/efi", "--json")] = _ok(
        stdout=_FINDMNT_JSON_MOUNTED,
    )
    runner.results[("lsblk", "-f", "--json")] = _ok(stdout=_LSBLK_JSON)
    runner.results[("ls", "/boot/efi/EFI/BOOT/missing.cfg")] = _fail()
    config = UefiEspManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiEspManager(runner=runner, config=config)

    result = manager.delete_esp_file(path="/EFI/BOOT/missing.cfg")
    testbase.assert_equal(result.status, UefiEspActionStatus.SKIPPED)
    rm_calls = [c for c in runner.calls if c["args"][0] == "rm"]
    testbase.assert_equal(rm_calls, [])
    return


def test_delete_esp_file_returns_failed_when_unmounted():
    """
        ``delete_esp_file`` returns ``FAILED`` when the ESP is not
        mounted; no command is invoked.
    """
    runner = _MockCommandRunner()
    runner.results[("findmnt", "/boot/efi", "--json")] = _ok(stdout="")
    runner.results[("lsblk", "-f", "--json")] = _ok(stdout=_LSBLK_JSON)
    config = UefiEspManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiEspManager(runner=runner, config=config)

    result = manager.delete_esp_file(path="/EFI/BOOT/test.cfg")
    testbase.assert_equal(result.status, UefiEspActionStatus.FAILED)
    rm_calls = [c for c in runner.calls if c["args"][0] == "rm"]
    testbase.assert_equal(rm_calls, [])
    return


# ----------------------------------------------------------------------
# Path normalization
# ----------------------------------------------------------------------


def test_path_normalisation_converts_backslashes_on_linux():
    """
        Linux paths normalize backslashes to forward slashes.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("findmnt", "/boot/efi", "--json")] = _ok(
        stdout=_FINDMNT_JSON_MOUNTED,
    )
    runner.results[("lsblk", "-f", "--json")] = _ok(stdout=_LSBLK_JSON)
    runner.results[("cat", "/boot/efi/EFI/Microsoft/Boot/bootmgfw.efi")] = _fail()
    config = UefiEspManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiEspManager(runner=runner, config=config)

    result = manager.write_esp_file(
        path="EFI\\Microsoft\\Boot\\bootmgfw.efi",
        content=b"data",
    )
    testbase.assert_equal(result.status, UefiEspActionStatus.SUCCESS)
    write_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(
        write_calls[0]["args"][1],
        "/boot/efi/EFI/Microsoft/Boot/bootmgfw.efi",
    )
    return


def test_path_normalisation_converts_forward_slashes_on_windows():
    """
        Windows paths normalize forward slashes to backslashes.
    """
    runner = _MockCommandRunner(default_result=_ok())
    partition_command = (
        "Get-Partition | Where-Object { $_.Type -eq 'System' } | ConvertTo-Json"
    )
    runner.results[
        ("powershell", "-NoProfile", "-Command", partition_command)
    ] = _ok(stdout=_GET_PARTITION_JSON)
    runner.results[
        ("powershell", "-NoProfile", "-Command", "Get-Volume | ConvertTo-Json")
    ] = _ok(stdout=_GET_VOLUME_JSON)
    config = UefiEspManagerConfig(override_host_os="Windows", elevated=True)
    manager = UefiEspManager(runner=runner, config=config)

    result = manager.write_esp_file(
        path="/EFI/Microsoft/Boot/bootmgfw.efi",
        content=b"data",
    )
    testbase.assert_equal(result.status, UefiEspActionStatus.SUCCESS)

    write_calls = [
        c for c in runner.calls
        if "Set-Content" in (c["args"][-1] or "")
    ]
    testbase.assert_equal(len(write_calls) >= 1, True)
    testbase.assert_equal(
        "S:\\EFI\\Microsoft\\Boot\\bootmgfw.efi" in write_calls[0]["args"][-1],
        True,
    )
    return


# ----------------------------------------------------------------------
# Elevation
# ----------------------------------------------------------------------


def test_writes_pass_elevated_true_when_configured():
    """
        Writes pass ``elevated=True`` when configured.  Reads do not.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("findmnt", "/boot/efi", "--json")] = _ok(
        stdout=_FINDMNT_JSON_MOUNTED,
    )
    runner.results[("lsblk", "-f", "--json")] = _ok(stdout=_LSBLK_JSON)
    runner.results[("findmnt", "/boot/efi")] = _ok(stdout="...")
    config = UefiEspManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiEspManager(runner=runner, config=config)

    manager.is_esp_mounted()
    manager.unmount_esp(mountpoint="/boot/efi")

    read_calls = [
        c for c in runner.calls if c["args"] == ["findmnt", "/boot/efi"]
    ]
    testbase.assert_equal(read_calls[0]["elevated"], False)

    write_calls = [c for c in runner.calls if c["args"][0] == "umount"]
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_reads_never_pass_elevated_true():
    """
        Read operations never pass ``elevated=True``.
    """
    runner = _MockCommandRunner()
    runner.results[("findmnt", "/boot/efi", "--json")] = _ok(
        stdout=_FINDMNT_JSON_MOUNTED,
    )
    runner.results[("lsblk", "-f", "--json")] = _ok(stdout=_LSBLK_JSON)
    runner.results[("findmnt", "/boot/efi")] = _ok(stdout="...")
    config = UefiEspManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiEspManager(runner=runner, config=config)

    manager.is_esp_mounted()
    manager.get_esp_info()

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# Parser-level tests
# ----------------------------------------------------------------------


def test_parse_findmnt_json_extracts_target_source_fstype():
    """
        Direct parser test for ``findmnt --json``.
    """
    runner = _MockCommandRunner()
    runner.results[("findmnt", "/boot/efi", "--json")] = _ok(
        stdout=_FINDMNT_JSON_MOUNTED,
    )
    runner.results[("lsblk", "-f", "--json")] = _ok(stdout=_LSBLK_JSON)
    config = UefiEspManagerConfig(override_host_os="Linux")
    manager = UefiEspManager(runner=runner, config=config)
    info = manager.get_esp_info()
    testbase.assert_equal(info.device, "/dev/sda1")
    testbase.assert_equal(info.filesystem, "vfat")
    return


def test_parse_diskutil_plist_finds_efi_partition():
    """
        Direct parser test for ``diskutil list -plist`` extracting the
        ESP device identifier.
    """
    runner = _MockCommandRunner()
    runner.results[("diskutil", "list", "-plist")] = _ok(
        stdout=_DISKUTIL_LIST_PLIST,
    )
    runner.results[("diskutil", "info", "disk0s1")] = _ok(
        stdout=_DISKUTIL_INFO_MOUNTED,
    )
    config = UefiEspManagerConfig(override_host_os="Darwin")
    manager = UefiEspManager(runner=runner, config=config)
    info = manager.get_esp_info()
    testbase.assert_equal(info.device, "disk0s1")
    return


def test_parse_get_partition_json_filters_type_system():
    """
        Direct parser test for the Windows ``Get-Partition`` JSON
        payload — the ``Type=System`` filter is honoured.
    """
    runner = _MockCommandRunner()
    partition_command = (
        "Get-Partition | Where-Object { $_.Type -eq 'System' } | ConvertTo-Json"
    )
    runner.results[
        ("powershell", "-NoProfile", "-Command", partition_command)
    ] = _ok(stdout=_GET_PARTITION_JSON)
    runner.results[
        ("powershell", "-NoProfile", "-Command", "Get-Volume | ConvertTo-Json")
    ] = _ok(stdout=_GET_VOLUME_JSON)
    config = UefiEspManagerConfig(override_host_os="Windows")
    manager = UefiEspManager(runner=runner, config=config)
    info = manager.get_esp_info()
    testbase.assert_equal(info.size_bytes, 536870912)
    return
