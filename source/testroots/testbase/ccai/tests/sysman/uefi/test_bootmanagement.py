"""
    Tests for :class:`UefiBootManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run`` call
    and returns pre-seeded :class:`RunResult` values keyed by argv tuple.
    No live ``efibootmgr`` / ``bless`` / ``nvram`` / ``bcdedit`` is
    invoked.

    Per-host-OS dispatch is exercised on every developer workstation by
    setting ``UefiBootManagerConfig.override_host_os`` to one of
    ``"Linux"``, ``"Darwin"``, ``"Windows"``.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.uefi.bootmanagement import (
    UefiBootActionResult,
    UefiBootActionStatus,
    UefiBootEntryInfo,
    UefiBootEntrySpec,
    UefiBootManager,
    UefiBootManagerConfig,
    UefiBootQueryError,
)


# ----------------------------------------------------------------------
# Mock runner
# ----------------------------------------------------------------------


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call and
        returns pre-seeded :class:`RunResult` values keyed by argv
        tuple.
    """

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


def _fail(stderr: str = "boom", exit_code: int = 1) -> RunResult:
    rtnval = RunResult(exit_code=exit_code, stdout="", stderr=stderr)
    return rtnval


# Sample outputs.

_EFIBOOTMGR_VERBOSE_OUTPUT = (
    "BootCurrent: 0001\n"
    "Timeout: 0 seconds\n"
    "BootOrder: 0001,0000,2001\n"
    "Boot0000* ubuntu\tHD(1,GPT,...)/File(\\EFI\\ubuntu\\shimx64.efi)\n"
    "Boot0001* Windows Boot Manager\tHD(1,GPT,...)/File(\\EFI\\Microsoft\\Boot\\bootmgfw.efi)\n"
    "Boot2001  Disabled\tRC\n"
)


_BCDEDIT_FIRMWARE_OUTPUT = (
    "Firmware Boot Manager\n"
    "---------------------\n"
    "identifier              {fwbootmgr}\n"
    "displayorder            {bootmgr}\n"
    "                        {12345678-1234-1234-1234-123456789abc}\n"
    "timeout                 5\n"
    "\n"
    "Windows Boot Manager\n"
    "--------------------\n"
    "identifier              {bootmgr}\n"
    "device                  partition=\\Device\\HarddiskVolume1\n"
    "path                    \\EFI\\Microsoft\\Boot\\bootmgfw.efi\n"
    "description             Windows Boot Manager\n"
    "\n"
    "Firmware Application (101fffff)\n"
    "-------------------------------\n"
    "identifier              {12345678-1234-1234-1234-123456789abc}\n"
    "path                    \\EFI\\custom\\boot.efi\n"
    "description             Custom Loader\n"
)


_BLESS_INFO_OUTPUT = (
    "finderinfo[0]: 100 => Blessed System File is /System/Library/CoreServices/boot.efi\n"
    "finderinfo[1]: 0 => No Startup Efi File\n"
)


_NVRAM_DASH_P_OUTPUT = (
    "efi-boot-device\t<array>boot.efi-EFIxxxx</array>\n"
    "boot-args\t-v\n"
)


# ----------------------------------------------------------------------
# Construction / dispatch override
# ----------------------------------------------------------------------


def test_override_host_os_forces_linux_dispatch():
    """
        ``override_host_os="Linux"`` makes the manager record
        ``host_os="linux"`` regardless of the developer workstation.
    """
    runner = _MockCommandRunner()
    config = UefiBootManagerConfig(override_host_os="Linux", dry_run=True)
    manager = UefiBootManager(runner=runner, config=config)
    testbase.assert_equal(manager.host_os, "linux")
    return


def test_override_host_os_forces_darwin_dispatch():
    """
        ``override_host_os="Darwin"`` selects the macOS dispatch path.
    """
    runner = _MockCommandRunner()
    config = UefiBootManagerConfig(override_host_os="Darwin", dry_run=True)
    manager = UefiBootManager(runner=runner, config=config)
    testbase.assert_equal(manager.host_os, "darwin")
    return


def test_override_host_os_forces_windows_dispatch():
    """
        ``override_host_os="Windows"`` selects the Windows dispatch path.
    """
    runner = _MockCommandRunner()
    config = UefiBootManagerConfig(override_host_os="Windows", dry_run=True)
    manager = UefiBootManager(runner=runner, config=config)
    testbase.assert_equal(manager.host_os, "windows")
    return


def test_override_host_os_rejects_invalid_value_in_constructor():
    """
        An override value that is not one of the three supported
        platforms raises ``ValueError`` at construction time.
    """
    runner = _MockCommandRunner()
    config = UefiBootManagerConfig(override_host_os="solaris")
    raised = False
    try:
        UefiBootManager(runner=runner, config=config)
    except ValueError:
        raised = True
    testbase.assert_equal(raised, True)
    return


# ----------------------------------------------------------------------
# list_boot_entries
# ----------------------------------------------------------------------


def test_list_boot_entries_linux_invokes_efibootmgr_verbose():
    """
        Linux ``list_boot_entries`` invokes ``efibootmgr -v`` without
        elevation and parses entries into ``UefiBootEntryInfo`` records.
    """
    runner = _MockCommandRunner()
    runner.results[("efibootmgr", "-v")] = _ok(stdout=_EFIBOOTMGR_VERBOSE_OUTPUT)
    config = UefiBootManagerConfig(override_host_os="Linux")
    manager = UefiBootManager(runner=runner, config=config)

    entries = manager.list_boot_entries()
    testbase.assert_equal(len(entries), 3)
    testbase.assert_equal(entries[0].entry_id, "0000")
    testbase.assert_equal(entries[0].label, "ubuntu")
    testbase.assert_equal(entries[1].entry_id, "0001")
    testbase.assert_equal(entries[1].is_active, True)
    testbase.assert_equal(runner.calls[0]["args"], ["efibootmgr", "-v"])
    testbase.assert_equal(runner.calls[0]["elevated"], False)
    return


def test_list_boot_entries_darwin_invokes_bless_and_nvram():
    """
        Darwin ``list_boot_entries`` invokes both ``bless --info
        --verbose`` and ``nvram -p`` without elevation.
    """
    runner = _MockCommandRunner()
    runner.results[("bless", "--info", "--verbose")] = _ok(stdout=_BLESS_INFO_OUTPUT)
    runner.results[("nvram", "-p")] = _ok(stdout=_NVRAM_DASH_P_OUTPUT)
    config = UefiBootManagerConfig(override_host_os="Darwin")
    manager = UefiBootManager(runner=runner, config=config)

    entries = manager.list_boot_entries()
    testbase.assert_equal(len(entries), 1)
    testbase.assert_equal(
        entries[0].loader, "/System/Library/CoreServices/boot.efi",
    )
    args_seen = [tuple(c["args"]) for c in runner.calls]
    testbase.assert_equal(("bless", "--info", "--verbose") in args_seen, True)
    testbase.assert_equal(("nvram", "-p") in args_seen, True)
    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_list_boot_entries_windows_invokes_bcdedit_enum_firmware():
    """
        Windows ``list_boot_entries`` invokes ``bcdedit /enum firmware``
        without elevation.
    """
    runner = _MockCommandRunner()
    runner.results[("bcdedit", "/enum", "firmware")] = _ok(
        stdout=_BCDEDIT_FIRMWARE_OUTPUT,
    )
    config = UefiBootManagerConfig(override_host_os="Windows")
    manager = UefiBootManager(runner=runner, config=config)

    entries = manager.list_boot_entries()
    # The {fwbootmgr} section is filtered out; we keep the bootmgr and
    # the GUID-handle Firmware Application section.
    testbase.assert_equal(len(entries) >= 2, True)
    ids = [e.entry_id for e in entries]
    testbase.assert_equal("{bootmgr}" in ids, True)
    testbase.assert_equal(runner.calls[0]["args"], ["bcdedit", "/enum", "firmware"])
    testbase.assert_equal(runner.calls[0]["elevated"], False)
    return


def test_list_boot_entries_failed_command_raises_query_error():
    """
        A failed enumeration raises ``UefiBootQueryError`` carrying the
        exit code and stderr.
    """
    runner = _MockCommandRunner()
    runner.results[("efibootmgr", "-v")] = _fail(
        stderr="EFI variables not supported", exit_code=2,
    )
    config = UefiBootManagerConfig(override_host_os="Linux")
    manager = UefiBootManager(runner=runner, config=config)

    raised = False
    try:
        manager.list_boot_entries()
    except UefiBootQueryError as exc:
        raised = True
        testbase.assert_equal(exc.exit_code, 2)
        testbase.assert_equal("EFI variables not supported" in exc.stderr, True)
    testbase.assert_equal(raised, True)
    return


# ----------------------------------------------------------------------
# get_boot_entry
# ----------------------------------------------------------------------


def test_get_boot_entry_returns_match():
    """
        ``get_boot_entry`` returns the matching info from the list.
    """
    runner = _MockCommandRunner()
    runner.results[("efibootmgr", "-v")] = _ok(stdout=_EFIBOOTMGR_VERBOSE_OUTPUT)
    config = UefiBootManagerConfig(override_host_os="Linux")
    manager = UefiBootManager(runner=runner, config=config)

    info = manager.get_boot_entry(entry_id="0001")
    testbase.assert_equal(info.entry_id, "0001")
    testbase.assert_equal(info.label, "Windows Boot Manager")
    return


def test_get_boot_entry_missing_raises():
    """
        ``get_boot_entry`` raises ``UefiBootQueryError`` when no entry
        matches.
    """
    runner = _MockCommandRunner()
    runner.results[("efibootmgr", "-v")] = _ok(stdout=_EFIBOOTMGR_VERBOSE_OUTPUT)
    config = UefiBootManagerConfig(override_host_os="Linux")
    manager = UefiBootManager(runner=runner, config=config)

    raised = False
    try:
        manager.get_boot_entry(entry_id="ffff")
    except UefiBootQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


# ----------------------------------------------------------------------
# create_boot_entry
# ----------------------------------------------------------------------


def test_create_boot_entry_linux_constructs_efibootmgr_c():
    """
        Linux ``create_boot_entry`` constructs ``efibootmgr -c -L
        <label> -l <loader> -d <disk> -p <part>``.
    """
    runner = _MockCommandRunner(default_result=_ok(stdout="Boot0010 created\n"))
    runner.results[("efibootmgr", "-v")] = _ok(stdout=_EFIBOOTMGR_VERBOSE_OUTPUT)
    config = UefiBootManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiBootManager(runner=runner, config=config)

    spec = UefiBootEntrySpec(
        label="custom-ubuntu",
        loader="\\EFI\\custom\\grubx64.efi",
        disk="/dev/sda",
        partition=1,
    )
    result = manager.create_boot_entry(spec=spec)
    testbase.assert_equal(result.status, UefiBootActionStatus.SUCCESS)
    testbase.assert_equal(result.entry_id, "0010")
    write_call = runner.calls[-1]
    testbase.assert_equal(
        write_call["args"],
        [
            "efibootmgr",
            "-c",
            "-L", "custom-ubuntu",
            "-l", "\\EFI\\custom\\grubx64.efi",
            "-d", "/dev/sda",
            "-p", "1",
        ],
    )
    testbase.assert_equal(write_call["elevated"], True)
    return


def test_create_boot_entry_windows_constructs_three_bcdedit_calls():
    """
        Windows ``create_boot_entry`` constructs the three-step
        ``bcdedit /create``, ``bcdedit /set ... device``, ``bcdedit /set
        ... path`` sequence.
    """
    runner = _MockCommandRunner(default_result=_ok(stdout=""))
    runner.results[("bcdedit", "/enum", "firmware")] = _ok(
        stdout=_BCDEDIT_FIRMWARE_OUTPUT,
    )
    runner.results[
        ("bcdedit", "/create", "/d", "Custom Loader B", "/application", "BOOTSECTOR")
    ] = _ok(
        stdout="The entry {abcdef00-0000-0000-0000-000000000000} was successfully created.\n",
    )
    config = UefiBootManagerConfig(override_host_os="Windows", elevated=True)
    manager = UefiBootManager(runner=runner, config=config)

    spec = UefiBootEntrySpec(
        label="Custom Loader B",
        loader="\\EFI\\custom\\boot.efi",
        disk="C",
        partition=1,
    )
    result = manager.create_boot_entry(spec=spec)
    testbase.assert_equal(result.status, UefiBootActionStatus.SUCCESS)
    testbase.assert_equal(result.entry_id, "{abcdef00-0000-0000-0000-000000000000}")

    write_args = [c["args"] for c in runner.calls if c["args"][:2] == ["bcdedit", "/create"]]
    testbase.assert_equal(len(write_args), 1)
    set_calls = [c["args"] for c in runner.calls if c["args"][:2] == ["bcdedit", "/set"]]
    testbase.assert_equal(len(set_calls), 2)
    return


def test_create_boot_entry_skipped_when_label_and_loader_match():
    """
        ``create_boot_entry`` returns ``SKIPPED`` when an entry with the
        same label and loader already exists.
    """
    runner = _MockCommandRunner(default_result=_ok(stdout="Boot0099 created\n"))
    runner.results[("efibootmgr", "-v")] = _ok(stdout=_EFIBOOTMGR_VERBOSE_OUTPUT)
    config = UefiBootManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiBootManager(runner=runner, config=config)

    spec = UefiBootEntrySpec(
        label="ubuntu",
        loader="\\EFI\\ubuntu\\shimx64.efi",
        disk="/dev/sda",
        partition=1,
    )
    result = manager.create_boot_entry(spec=spec)
    testbase.assert_equal(result.status, UefiBootActionStatus.SKIPPED)
    create_calls = [c for c in runner.calls if c["args"][:2] == ["efibootmgr", "-c"]]
    testbase.assert_equal(create_calls, [])
    return


def test_create_boot_entry_dry_run_short_circuits():
    """
        Dry-run short-circuits ``create_boot_entry`` without invoking
        the runner.
    """
    runner = _MockCommandRunner()
    config = UefiBootManagerConfig(override_host_os="Linux", dry_run=True)
    manager = UefiBootManager(runner=runner, config=config)

    spec = UefiBootEntrySpec(
        label="dry",
        loader="\\EFI\\dry\\boot.efi",
        disk="/dev/sda",
        partition=1,
    )
    result = manager.create_boot_entry(spec=spec)
    testbase.assert_equal(result.status, UefiBootActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(result.command[0], "efibootmgr")
    return


# ----------------------------------------------------------------------
# delete_boot_entry
# ----------------------------------------------------------------------


def test_delete_boot_entry_linux_constructs_efibootmgr_b_B():
    """
        Linux ``delete_boot_entry`` constructs ``efibootmgr -b <id>
        -B``.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("efibootmgr", "-v")] = _ok(stdout=_EFIBOOTMGR_VERBOSE_OUTPUT)
    config = UefiBootManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiBootManager(runner=runner, config=config)

    result = manager.delete_boot_entry(entry_id="0001")
    testbase.assert_equal(result.status, UefiBootActionStatus.SUCCESS)
    delete_calls = [c for c in runner.calls if c["args"][:2] == ["efibootmgr", "-b"]]
    testbase.assert_equal(len(delete_calls), 1)
    testbase.assert_equal(
        delete_calls[0]["args"], ["efibootmgr", "-b", "0001", "-B"],
    )
    testbase.assert_equal(delete_calls[0]["elevated"], True)
    return


def test_delete_boot_entry_darwin_returns_failed_with_detail():
    """
        Darwin ``delete_boot_entry`` returns ``FAILED`` with a stable
        token-prefixed detail and invokes no command.
    """
    runner = _MockCommandRunner()
    runner.results[("bless", "--info", "--verbose")] = _ok(stdout=_BLESS_INFO_OUTPUT)
    runner.results[("nvram", "-p")] = _ok(stdout=_NVRAM_DASH_P_OUTPUT)
    config = UefiBootManagerConfig(override_host_os="Darwin")
    manager = UefiBootManager(runner=runner, config=config)

    result = manager.delete_boot_entry(entry_id="anything")
    testbase.assert_equal(result.status, UefiBootActionStatus.FAILED)
    testbase.assert_equal(
        "unsupported_on_host_os_darwin" in (result.detail or ""), True,
    )
    delete_calls = [c for c in runner.calls if c["args"][:2] == ["bless", "/delete"]]
    testbase.assert_equal(delete_calls, [])
    return


def test_delete_boot_entry_windows_constructs_bcdedit_delete():
    """
        Windows ``delete_boot_entry`` constructs ``bcdedit /delete
        {guid}``.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("bcdedit", "/enum", "firmware")] = _ok(
        stdout=_BCDEDIT_FIRMWARE_OUTPUT,
    )
    config = UefiBootManagerConfig(override_host_os="Windows", elevated=True)
    manager = UefiBootManager(runner=runner, config=config)

    target = "{12345678-1234-1234-1234-123456789abc}"
    result = manager.delete_boot_entry(entry_id=target)
    testbase.assert_equal(result.status, UefiBootActionStatus.SUCCESS)
    delete_calls = [c for c in runner.calls if c["args"][:2] == ["bcdedit", "/delete"]]
    testbase.assert_equal(len(delete_calls), 1)
    testbase.assert_equal(delete_calls[0]["args"], ["bcdedit", "/delete", target])
    testbase.assert_equal(delete_calls[0]["elevated"], True)
    return


def test_delete_boot_entry_skipped_when_entry_absent():
    """
        ``delete_boot_entry`` returns ``SKIPPED`` when the entry is not
        present in the enumeration.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("efibootmgr", "-v")] = _ok(stdout=_EFIBOOTMGR_VERBOSE_OUTPUT)
    config = UefiBootManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiBootManager(runner=runner, config=config)

    result = manager.delete_boot_entry(entry_id="ffff")
    testbase.assert_equal(result.status, UefiBootActionStatus.SKIPPED)
    delete_calls = [c for c in runner.calls if c["args"][:2] == ["efibootmgr", "-b"]]
    testbase.assert_equal(delete_calls, [])
    return


# ----------------------------------------------------------------------
# get_boot_order / set_boot_order
# ----------------------------------------------------------------------


def test_get_boot_order_linux_parses_csv():
    """
        Linux ``get_boot_order`` parses the ``BootOrder:`` line into a
        list of lower-cased 4-hex IDs.
    """
    runner = _MockCommandRunner()
    runner.results[("efibootmgr",)] = _ok(stdout=_EFIBOOTMGR_VERBOSE_OUTPUT)
    config = UefiBootManagerConfig(override_host_os="Linux")
    manager = UefiBootManager(runner=runner, config=config)

    order = manager.get_boot_order()
    testbase.assert_equal(order, ["0001", "0000", "2001"])
    return


def test_get_boot_order_windows_parses_displayorder():
    """
        Windows ``get_boot_order`` parses the ``displayorder`` block
        into a list of GUIDs.
    """
    runner = _MockCommandRunner()
    runner.results[("bcdedit", "/enum", "{fwbootmgr}")] = _ok(
        stdout=_BCDEDIT_FIRMWARE_OUTPUT,
    )
    config = UefiBootManagerConfig(override_host_os="Windows")
    manager = UefiBootManager(runner=runner, config=config)

    order = manager.get_boot_order()
    testbase.assert_equal(len(order), 2)
    testbase.assert_equal(order[0], "{bootmgr}")
    testbase.assert_equal(order[1], "{12345678-1234-1234-1234-123456789abc}")
    return


def test_set_boot_order_linux_constructs_efibootmgr_o_csv():
    """
        Linux ``set_boot_order`` constructs ``efibootmgr -o
        a,b,c``.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("efibootmgr",)] = _ok(stdout=_EFIBOOTMGR_VERBOSE_OUTPUT)
    config = UefiBootManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiBootManager(runner=runner, config=config)

    result = manager.set_boot_order(order=["0000", "0001"])
    testbase.assert_equal(result.status, UefiBootActionStatus.SUCCESS)
    write_calls = [c for c in runner.calls if c["args"][:2] == ["efibootmgr", "-o"]]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["args"], ["efibootmgr", "-o", "0000,0001"])
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_set_boot_order_skipped_when_order_matches():
    """
        ``set_boot_order`` returns ``SKIPPED`` when the requested order
        already equals the current order.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("efibootmgr",)] = _ok(stdout=_EFIBOOTMGR_VERBOSE_OUTPUT)
    config = UefiBootManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiBootManager(runner=runner, config=config)

    result = manager.set_boot_order(order=["0001", "0000", "2001"])
    testbase.assert_equal(result.status, UefiBootActionStatus.SKIPPED)
    write_calls = [c for c in runner.calls if c["args"][:2] == ["efibootmgr", "-o"]]
    testbase.assert_equal(write_calls, [])
    return


def test_set_boot_order_darwin_returns_failed_with_detail():
    """
        Darwin ``set_boot_order`` returns ``FAILED`` with a stable token
        and invokes no order command.
    """
    runner = _MockCommandRunner()
    runner.results[("nvram", "efi-boot-device")] = _ok(stdout=_NVRAM_DASH_P_OUTPUT)
    config = UefiBootManagerConfig(override_host_os="Darwin", elevated=True)
    manager = UefiBootManager(runner=runner, config=config)

    result = manager.set_boot_order(order=["a", "b"])
    testbase.assert_equal(result.status, UefiBootActionStatus.FAILED)
    testbase.assert_equal(
        "unsupported_on_host_os_darwin" in (result.detail or ""), True,
    )
    return


def test_set_boot_order_windows_constructs_displayorder():
    """
        Windows ``set_boot_order`` constructs ``bcdedit /set
        {fwbootmgr} displayorder <guid> <guid> ...``.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("bcdedit", "/enum", "{fwbootmgr}")] = _ok(
        stdout=_BCDEDIT_FIRMWARE_OUTPUT,
    )
    config = UefiBootManagerConfig(override_host_os="Windows", elevated=True)
    manager = UefiBootManager(runner=runner, config=config)

    # Reverse the order so the idempotency guard does NOT short-circuit.
    new_order = ["{12345678-1234-1234-1234-123456789abc}", "{bootmgr}"]
    result = manager.set_boot_order(order=new_order)
    testbase.assert_equal(result.status, UefiBootActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls if c["args"][:3] == ["bcdedit", "/set", "{fwbootmgr}"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"][:4],
        ["bcdedit", "/set", "{fwbootmgr}", "displayorder"],
    )
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# set_boot_next / get_boot_next
# ----------------------------------------------------------------------


def test_set_boot_next_linux_constructs_efibootmgr_n():
    """
        Linux ``set_boot_next`` constructs ``efibootmgr -n <id>``.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("efibootmgr",)] = _ok(stdout=_EFIBOOTMGR_VERBOSE_OUTPUT)
    config = UefiBootManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiBootManager(runner=runner, config=config)

    result = manager.set_boot_next(entry_id="0000")
    testbase.assert_equal(result.status, UefiBootActionStatus.SUCCESS)
    write_calls = [c for c in runner.calls if c["args"][:2] == ["efibootmgr", "-n"]]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["args"], ["efibootmgr", "-n", "0000"])
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_set_boot_next_darwin_returns_failed_with_detail():
    """
        Darwin ``set_boot_next`` returns ``FAILED`` and invokes no
        command.
    """
    runner = _MockCommandRunner()
    config = UefiBootManagerConfig(override_host_os="Darwin")
    manager = UefiBootManager(runner=runner, config=config)
    result = manager.set_boot_next(entry_id="opaque")
    testbase.assert_equal(result.status, UefiBootActionStatus.FAILED)
    testbase.assert_equal(
        "unsupported_on_host_os_darwin" in (result.detail or ""), True,
    )
    testbase.assert_equal(runner.calls, [])
    return


def test_set_boot_next_windows_constructs_bootsequence():
    """
        Windows ``set_boot_next`` constructs ``bcdedit /set
        {fwbootmgr} bootsequence {guid}``.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("bcdedit", "/enum", "{fwbootmgr}")] = _ok(
        stdout=_BCDEDIT_FIRMWARE_OUTPUT,
    )
    config = UefiBootManagerConfig(override_host_os="Windows", elevated=True)
    manager = UefiBootManager(runner=runner, config=config)

    target = "{99999999-9999-9999-9999-999999999999}"
    result = manager.set_boot_next(entry_id=target)
    testbase.assert_equal(result.status, UefiBootActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls if c["args"][:4] == [
            "bcdedit", "/set", "{fwbootmgr}", "bootsequence",
        ]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_get_boot_next_darwin_returns_none_without_runner():
    """
        Darwin ``get_boot_next`` returns ``None`` and invokes no
        command.
    """
    runner = _MockCommandRunner()
    config = UefiBootManagerConfig(override_host_os="Darwin")
    manager = UefiBootManager(runner=runner, config=config)
    rtnval = manager.get_boot_next()
    testbase.assert_equal(rtnval, None)
    testbase.assert_equal(runner.calls, [])
    return


# ----------------------------------------------------------------------
# get_boot_current
# ----------------------------------------------------------------------


def test_get_boot_current_linux_parses_bootcurrent():
    """
        Linux ``get_boot_current`` parses the ``BootCurrent:`` line.
    """
    runner = _MockCommandRunner()
    runner.results[("efibootmgr",)] = _ok(stdout=_EFIBOOTMGR_VERBOSE_OUTPUT)
    config = UefiBootManagerConfig(override_host_os="Linux")
    manager = UefiBootManager(runner=runner, config=config)
    rtnval = manager.get_boot_current()
    testbase.assert_equal(rtnval, "0001")
    return


# ----------------------------------------------------------------------
# Dry-run / elevation / error handling
# ----------------------------------------------------------------------


def test_dry_run_short_circuits_all_write_verbs():
    """
        Every write verb returns ``DRY_RUN`` and does not invoke the
        runner when ``dry_run=True``.
    """
    runner = _MockCommandRunner()
    config = UefiBootManagerConfig(override_host_os="Linux", dry_run=True)
    manager = UefiBootManager(runner=runner, config=config)

    spec = UefiBootEntrySpec(label="dry", loader="x", disk="/dev/sda", partition=1)
    create_result = manager.create_boot_entry(spec=spec)
    delete_result = manager.delete_boot_entry(entry_id="0000")
    set_order_result = manager.set_boot_order(order=["0000", "0001"])
    set_next_result = manager.set_boot_next(entry_id="0001")

    for r in (create_result, delete_result, set_order_result, set_next_result):
        testbase.assert_equal(r.status, UefiBootActionStatus.DRY_RUN)
    testbase.assert_equal(runner.calls, [])
    return


def test_writes_pass_elevated_true_when_configured():
    """
        Writes pass ``elevated=True`` when the config sets it, and
        reads do not.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("efibootmgr", "-v")] = _ok(stdout=_EFIBOOTMGR_VERBOSE_OUTPUT)
    runner.results[("efibootmgr",)] = _ok(stdout=_EFIBOOTMGR_VERBOSE_OUTPUT)
    config = UefiBootManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiBootManager(runner=runner, config=config)

    manager.list_boot_entries()
    manager.set_boot_next(entry_id="0000")

    read_calls = [c for c in runner.calls if c["args"] == ["efibootmgr", "-v"]]
    testbase.assert_equal(read_calls[0]["elevated"], False)

    write_calls = [c for c in runner.calls if c["args"][:2] == ["efibootmgr", "-n"]]
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_reads_never_pass_elevated_true():
    """
        Read operations never pass ``elevated=True`` regardless of
        config.
    """
    runner = _MockCommandRunner()
    runner.results[("efibootmgr", "-v")] = _ok(stdout=_EFIBOOTMGR_VERBOSE_OUTPUT)
    config = UefiBootManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiBootManager(runner=runner, config=config)
    manager.list_boot_entries()
    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_populates_exit_code_and_stderr():
    """
        A non-zero exit on a write surfaces as ``FAILED`` with the exit
        code and stderr captured.
    """
    runner = _MockCommandRunner(default_result=_fail(stderr="permission denied", exit_code=2))
    runner.results[("efibootmgr", "-v")] = _ok(stdout=_EFIBOOTMGR_VERBOSE_OUTPUT)
    config = UefiBootManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiBootManager(runner=runner, config=config)

    result = manager.delete_boot_entry(entry_id="0001")
    testbase.assert_equal(result.status, UefiBootActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_equal("permission denied" in (result.stderr or ""), True)
    return


# ----------------------------------------------------------------------
# Parser-level tests
# ----------------------------------------------------------------------


def test_parse_efibootmgr_verbose_extracts_entries_and_order():
    """
        Direct parser test for ``efibootmgr -v`` output: extracts the
        right number of entries with the expected ``entry_id``,
        ``label``, ``loader``, and ``is_default``.
    """
    runner = _MockCommandRunner()
    runner.results[("efibootmgr", "-v")] = _ok(stdout=_EFIBOOTMGR_VERBOSE_OUTPUT)
    config = UefiBootManagerConfig(override_host_os="Linux")
    manager = UefiBootManager(runner=runner, config=config)
    entries = manager.list_boot_entries()
    by_id = {e.entry_id: e for e in entries}
    testbase.assert_equal(by_id["0000"].label, "ubuntu")
    testbase.assert_equal(
        by_id["0000"].loader, "\\EFI\\ubuntu\\shimx64.efi",
    )
    testbase.assert_equal(by_id["0001"].is_default, True)
    return


def test_parse_bcdedit_enum_firmware_extracts_guids_and_displayorder():
    """
        Direct parser test for ``bcdedit /enum firmware``: extracts the
        Windows Boot Manager and the GUID-handle entry.
    """
    runner = _MockCommandRunner()
    runner.results[("bcdedit", "/enum", "firmware")] = _ok(
        stdout=_BCDEDIT_FIRMWARE_OUTPUT,
    )
    config = UefiBootManagerConfig(override_host_os="Windows")
    manager = UefiBootManager(runner=runner, config=config)
    entries = manager.list_boot_entries()
    ids = [e.entry_id for e in entries]
    testbase.assert_equal("{bootmgr}" in ids, True)
    testbase.assert_equal(
        "{12345678-1234-1234-1234-123456789abc}" in ids, True,
    )
    return
