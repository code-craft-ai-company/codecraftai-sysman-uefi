"""
    Tests for :class:`UefiCapsuleUpdateManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``fwupdmgr`` / ``softwareupdate`` /
    ``Get-WindowsUpdate`` is invoked.

    Per-host-OS dispatch is exercised on every developer workstation by
    setting ``UefiCapsuleUpdateManagerConfig.override_host_os`` to one of
    ``"Linux"``, ``"Darwin"``, ``"Windows"``.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.uefi.capsuleupdate import (
    UefiCapsuleUpdateActionStatus,
    UefiCapsuleUpdateManager,
    UefiCapsuleUpdateManagerConfig,
    UefiCapsuleUpdateQueryError,
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


def _fail(stderr: str = "boom", exit_code: int = 1, stdout: str = "") -> RunResult:
    rtnval = RunResult(exit_code=exit_code, stdout=stdout, stderr=stderr)
    return rtnval


# ----------------------------------------------------------------------
# Sample outputs
# ----------------------------------------------------------------------


_FWUPDMGR_GET_UPDATES_JSON = json.dumps({
    "Devices": [
        {
            "DeviceId": "dev-aaa",
            "Name": "System Firmware",
            "Vendor": "ACME",
            "Version": "1.0.5",
            "Releases": [
                {
                    "ReleaseId": "release-aaa-2.0.0",
                    "AppstreamId": "com.acme.firmware",
                    "Version": "2.0.0",
                    "Summary": "Adds USB-C charge support",
                    "Urgency": "high",
                },
            ],
        },
        {
            "DeviceId": "dev-bbb",
            "Name": "TPM",
            "Vendor": "ACME",
            "Version": "5.0.0",
            "Releases": [
                {
                    "ReleaseId": "release-bbb-4.5.0",
                    "Version": "4.5.0",
                    "Summary": "Older TPM firmware",
                    "Urgency": "low",
                },
            ],
        },
    ],
})


_FWUPDMGR_GET_DEVICES_JSON = json.dumps({
    "Devices": [
        {
            "DeviceId": "dev-aaa",
            "Name": "System Firmware",
            "Vendor": "ACME",
            "Version": "1.0.5",
            "Flags": ["updatable"],
        },
        {
            "DeviceId": "dev-ccc",
            "Name": "Embedded Controller",
            "Vendor": "ACME",
            "Version": "9.9.0",
            "Flags": ["internal"],
        },
    ],
})


_FWUPDMGR_GET_HISTORY_JSON = json.dumps({
    "Devices": [
        {
            "DeviceId": "dev-aaa",
            "Releases": [
                {
                    "ReleaseId": "release-aaa-1.0.5",
                    "Version": "1.0.5",
                    "Created": "2025-12-01T08:00:00Z",
                    "UpdateState": "success",
                },
            ],
        },
    ],
})


_SOFTWAREUPDATE_LIST_OUTPUT = (
    "Software Update Tool\n"
    "\n"
    "Finding available software\n"
    "Software Update found the following new or updated software:\n"
    "* Label: macOS-Firmware-Update-12.6.1\n"
    "    Title: macOS Firmware Update, Version: 12.6.1, Size: 540MiB, Recommended: YES\n"
    "* Label: SafariUpdate\n"
    "    Title: Safari, Version: 17.0, Size: 90MiB, Recommended: YES\n"
)


_SOFTWAREUPDATE_HISTORY_OUTPUT = (
    "Display Name                                  Version    Date                  Result\n"
    "------------                                  -------    ----                  ------\n"
    "macOS Firmware Update                         12.6.0     2025-11-30 12:00:00   success\n"
)


_SYSTEM_PROFILER_HARDWARE_JSON = json.dumps({
    "SPHardwareDataType": [
        {
            "machine_name": "MacBook Pro",
            "machine_model": "Mac15,3",
            "platform_UUID": "ABCDEF12-1234-5678-90AB-CDEF12345678",
            "boot_rom_version": "iBridge: 19.16.10322.0.0,0",
            "smc_version_system": "2.46f8",
        },
    ],
})


_GET_WINDOWSUPDATE_JSON = json.dumps([
    {
        "KB": "KB5031455",
        "Title": "Firmware update for Surface Laptop",
        "MsrcSeverity": "Critical",
        "RevisionNumber": 7,
    },
    {
        "KB": "KB5031999",
        "Title": "Firmware update for Surface Pro",
        "MsrcSeverity": "Important",
        "RevisionNumber": 4,
    },
])


_GET_WMI_JSON = json.dumps([
    {
        "Manufacturer": "Microsoft",
        "Model": "Surface Laptop 5",
        "SerialNumber": "ABC123",
    },
    {
        "SMBIOSBIOSVersion": "12.345.678",
        "Caption": "BIOS Date: 06/01/2025 Ver: 12.345.678",
    },
])


_GET_WUHISTORY_JSON = json.dumps([
    {
        "KB": "KB5031455",
        "Title": "Firmware update for Surface Laptop",
        "Date": "2025-12-01T08:00:00",
        "RevisionNumber": 7,
        "Result": "Succeeded",
    },
])


# ----------------------------------------------------------------------
# Construction / dispatch override
# ----------------------------------------------------------------------


def test_override_host_os_forces_linux_dispatch():
    """
        ``override_host_os="Linux"`` makes the manager record
        ``host_os="linux"`` regardless of the developer workstation.
    """
    runner = _MockCommandRunner()
    config = UefiCapsuleUpdateManagerConfig(override_host_os="Linux", dry_run=True)
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)
    testbase.assert_equal(manager.host_os, "linux")
    return


def test_override_host_os_forces_darwin_dispatch():
    """
        ``override_host_os="Darwin"`` selects the macOS dispatch path.
    """
    runner = _MockCommandRunner()
    config = UefiCapsuleUpdateManagerConfig(override_host_os="Darwin", dry_run=True)
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)
    testbase.assert_equal(manager.host_os, "darwin")
    return


def test_override_host_os_forces_windows_dispatch():
    """
        ``override_host_os="Windows"`` selects the Windows dispatch path.
    """
    runner = _MockCommandRunner()
    config = UefiCapsuleUpdateManagerConfig(override_host_os="Windows", dry_run=True)
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)
    testbase.assert_equal(manager.host_os, "windows")
    return


def test_override_host_os_rejects_invalid_value_in_constructor():
    """
        An override value that is not one of the three supported
        platforms raises ``ValueError`` at construction time.
    """
    runner = _MockCommandRunner()
    config = UefiCapsuleUpdateManagerConfig(override_host_os="solaris")
    raised = False
    try:
        UefiCapsuleUpdateManager(runner=runner, config=config)
    except ValueError:
        raised = True
    testbase.assert_equal(raised, True)
    return


# ----------------------------------------------------------------------
# list_available_updates
# ----------------------------------------------------------------------


def test_list_available_updates_linux_invokes_fwupdmgr_get_updates_json():
    """
        Linux ``list_available_updates`` invokes ``fwupdmgr get-updates
        --json`` and parses the JSON.
    """
    runner = _MockCommandRunner()
    runner.results[("fwupdmgr", "get-updates", "--json")] = _ok(
        stdout=_FWUPDMGR_GET_UPDATES_JSON,
    )
    config = UefiCapsuleUpdateManagerConfig(override_host_os="Linux")
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)

    updates = manager.list_available_updates()
    testbase.assert_equal(len(updates), 2)
    testbase.assert_equal(updates[0].update_id, "release-aaa-2.0.0")
    testbase.assert_equal(updates[0].available_version, "2.0.0")
    testbase.assert_equal(updates[0].current_version, "1.0.5")
    testbase.assert_equal(updates[0].severity, "high")
    testbase.assert_equal(runner.calls[0]["elevated"], False)
    testbase.assert_equal(
        runner.calls[0]["args"], ["fwupdmgr", "get-updates", "--json"],
    )
    return


def test_list_available_updates_darwin_invokes_softwareupdate_list():
    """
        Darwin ``list_available_updates`` invokes ``softwareupdate
        --list --include-config-data`` and filters to firmware entries.
    """
    runner = _MockCommandRunner()
    runner.results[("softwareupdate", "--list", "--include-config-data")] = _ok(
        stdout=_SOFTWAREUPDATE_LIST_OUTPUT,
    )
    config = UefiCapsuleUpdateManagerConfig(override_host_os="Darwin")
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)

    updates = manager.list_available_updates()
    # Firmware entries only.
    testbase.assert_equal(len(updates), 1)
    testbase.assert_equal(updates[0].update_id, "macOS-Firmware-Update-12.6.1")
    testbase.assert_equal(updates[0].available_version, "12.6.1")
    return


def test_list_available_updates_windows_invokes_get_windowsupdate():
    """
        Windows ``list_available_updates`` invokes the firmware-filtered
        ``Get-WindowsUpdate`` PowerShell pipeline and parses the JSON.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok(stdout=_GET_WINDOWSUPDATE_JSON)
    config = UefiCapsuleUpdateManagerConfig(override_host_os="Windows")
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)

    updates = manager.list_available_updates()
    testbase.assert_equal(len(updates), 2)
    ids = [u.update_id for u in updates]
    testbase.assert_equal("KB5031455" in ids, True)
    testbase.assert_equal("KB5031999" in ids, True)

    invoked = runner.calls[0]["args"]
    testbase.assert_equal(invoked[0], "powershell")
    testbase.assert_equal(invoked[1], "-NoProfile")
    testbase.assert_equal(invoked[2], "-Command")
    testbase.assert_equal("Get-WindowsUpdate" in invoked[3], True)
    testbase.assert_equal("firmware" in invoked[3], True)
    return


def test_list_available_updates_failed_command_raises_query_error():
    """
        A failed enumeration raises ``UefiCapsuleUpdateQueryError``.
    """
    runner = _MockCommandRunner()
    runner.results[("fwupdmgr", "get-updates", "--json")] = _fail(
        stderr="fwupd: not running", exit_code=2,
    )
    config = UefiCapsuleUpdateManagerConfig(override_host_os="Linux")
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)

    raised = False
    try:
        manager.list_available_updates()
    except UefiCapsuleUpdateQueryError as exc:
        raised = True
        testbase.assert_equal(exc.exit_code, 2)
        testbase.assert_equal("fwupd: not running" in exc.stderr, True)
    testbase.assert_equal(raised, True)
    return


# ----------------------------------------------------------------------
# list_devices
# ----------------------------------------------------------------------


def test_list_devices_linux_invokes_fwupdmgr_get_devices_json():
    """
        Linux ``list_devices`` invokes ``fwupdmgr get-devices --json``.
    """
    runner = _MockCommandRunner()
    runner.results[("fwupdmgr", "get-devices", "--json")] = _ok(
        stdout=_FWUPDMGR_GET_DEVICES_JSON,
    )
    config = UefiCapsuleUpdateManagerConfig(override_host_os="Linux")
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)

    devices = manager.list_devices()
    testbase.assert_equal(len(devices), 2)
    by_id = {d.device_id: d for d in devices}
    testbase.assert_equal(by_id["dev-aaa"].update_supported, True)
    testbase.assert_equal(by_id["dev-ccc"].update_supported, False)
    testbase.assert_equal(runner.calls[0]["elevated"], False)
    return


def test_list_devices_darwin_returns_apple_record_with_supported_false():
    """
        Darwin ``list_devices`` invokes ``system_profiler`` and returns a
        single Apple-hardware record with ``update_supported=False``.
    """
    runner = _MockCommandRunner()
    runner.results[("system_profiler", "-json", "SPHardwareDataType")] = _ok(
        stdout=_SYSTEM_PROFILER_HARDWARE_JSON,
    )
    config = UefiCapsuleUpdateManagerConfig(override_host_os="Darwin")
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)

    devices = manager.list_devices()
    testbase.assert_equal(len(devices), 1)
    testbase.assert_equal(devices[0].update_supported, False)
    testbase.assert_equal(devices[0].vendor, "Apple Inc.")
    return


def test_list_devices_windows_invokes_wmi_pipeline():
    """
        Windows ``list_devices`` invokes the ``Get-CimInstance``
        Win32_ComputerSystem,Win32_BIOS pipeline.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok(stdout=_GET_WMI_JSON)
    config = UefiCapsuleUpdateManagerConfig(override_host_os="Windows")
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)

    devices = manager.list_devices()
    testbase.assert_equal(len(devices), 1)
    testbase.assert_equal(devices[0].vendor, "Microsoft")
    testbase.assert_equal(devices[0].firmware_version, "12.345.678")

    invoked = runner.calls[0]["args"]
    testbase.assert_equal("Win32_ComputerSystem" in invoked[3], True)
    testbase.assert_equal("Win32_BIOS" in invoked[3], True)
    return


# ----------------------------------------------------------------------
# apply_update
# ----------------------------------------------------------------------


def test_apply_update_linux_constructs_fwupdmgr_update():
    """
        Linux ``apply_update`` constructs ``fwupdmgr update
        --no-reboot-check <id>`` and passes ``elevated=True``.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("fwupdmgr", "get-updates", "--json")] = _ok(
        stdout=_FWUPDMGR_GET_UPDATES_JSON,
    )
    config = UefiCapsuleUpdateManagerConfig(
        override_host_os="Linux", elevated=True,
    )
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)

    result = manager.apply_update(update_id="release-aaa-2.0.0")
    testbase.assert_equal(result.status, UefiCapsuleUpdateActionStatus.SUCCESS)
    testbase.assert_equal(result.update_id, "release-aaa-2.0.0")
    write_calls = [
        c for c in runner.calls if c["args"][:3] == [
            "fwupdmgr", "update", "--no-reboot-check",
        ]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        ["fwupdmgr", "update", "--no-reboot-check", "release-aaa-2.0.0"],
    )
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_apply_update_darwin_returns_failed_with_softwareupdate_detail():
    """
        Darwin ``apply_update`` returns ``FAILED`` with a stable
        ``unsupported_on_host_os_darwin`` token and invokes no command.
    """
    runner = _MockCommandRunner()
    config = UefiCapsuleUpdateManagerConfig(override_host_os="Darwin")
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)

    result = manager.apply_update(update_id="macOS-Firmware-Update-12.6.1")
    testbase.assert_equal(result.status, UefiCapsuleUpdateActionStatus.FAILED)
    testbase.assert_equal(
        "unsupported_on_host_os_darwin" in (result.detail or ""), True,
    )
    testbase.assert_equal(
        "softwareupdate" in (result.detail or ""), True,
    )
    testbase.assert_equal(runner.calls, [])
    return


def test_apply_update_windows_constructs_install_windowsupdate():
    """
        Windows ``apply_update`` constructs ``Install-WindowsUpdate
        -KBArticleID ...``.
    """
    runner = _MockCommandRunner(default_result=_ok())
    # Pending list returns the KB so the idempotency probe passes.
    pending = json.dumps([
        {"KB": "KB5031455", "Title": "Firmware", "RevisionNumber": 7},
    ])
    powershell_pending_command = (
        "Get-WindowsUpdate -MicrosoftUpdate | "
        "Where-Object { $_.CategoryIDs -contains 'firmware' } | "
        "ConvertTo-Json"
    )
    runner.results[
        ("powershell", "-NoProfile", "-Command", powershell_pending_command)
    ] = _ok(stdout=pending)
    config = UefiCapsuleUpdateManagerConfig(
        override_host_os="Windows", elevated=True,
    )
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)

    result = manager.apply_update(update_id="KB5031455")
    testbase.assert_equal(result.status, UefiCapsuleUpdateActionStatus.SUCCESS)
    testbase.assert_equal(result.update_id, "KB5031455")

    write_calls = [
        c for c in runner.calls if "Install-WindowsUpdate" in (c["args"][-1] or "")
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal("KB5031455" in write_calls[0]["args"][-1], True)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_apply_update_skipped_when_not_in_pending_list():
    """
        ``apply_update`` returns ``SKIPPED`` when ``update_id`` is not
        present in the host's current pending list.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("fwupdmgr", "get-updates", "--json")] = _ok(
        stdout=_FWUPDMGR_GET_UPDATES_JSON,
    )
    config = UefiCapsuleUpdateManagerConfig(
        override_host_os="Linux", elevated=True,
    )
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)

    result = manager.apply_update(update_id="release-zzz-not-present")
    testbase.assert_equal(result.status, UefiCapsuleUpdateActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls if c["args"][:3] == [
            "fwupdmgr", "update", "--no-reboot-check",
        ]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_apply_update_dry_run_short_circuits():
    """
        Dry-run short-circuits ``apply_update`` without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    config = UefiCapsuleUpdateManagerConfig(
        override_host_os="Linux", dry_run=True,
    )
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)

    result = manager.apply_update(update_id="release-aaa-2.0.0")
    testbase.assert_equal(result.status, UefiCapsuleUpdateActionStatus.DRY_RUN)
    testbase.assert_equal(runner.calls, [])
    testbase.assert_equal(result.command[0], "fwupdmgr")
    return


def test_apply_update_blocked_on_downgrade_without_allow_flag():
    """
        ``apply_update`` returns ``FAILED`` with a stable
        ``firmware_downgrade_blocked`` token when the available version
        is older than the device's current version and
        ``allow_downgrade=False``.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("fwupdmgr", "get-updates", "--json")] = _ok(
        stdout=_FWUPDMGR_GET_UPDATES_JSON,
    )
    config = UefiCapsuleUpdateManagerConfig(
        override_host_os="Linux", elevated=True, allow_downgrade=False,
    )
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)

    # release-bbb has available 4.5.0 vs current 5.0.0 — a downgrade.
    result = manager.apply_update(update_id="release-bbb-4.5.0")
    testbase.assert_equal(result.status, UefiCapsuleUpdateActionStatus.FAILED)
    testbase.assert_equal(
        "firmware_downgrade_blocked" in (result.detail or ""), True,
    )
    write_calls = [
        c for c in runner.calls if c["args"][:3] == [
            "fwupdmgr", "update", "--no-reboot-check",
        ]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_apply_update_proceeds_on_downgrade_when_allow_flag_set():
    """
        ``apply_update`` proceeds normally on a downgrade when
        ``allow_downgrade=True`` is set.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("fwupdmgr", "get-updates", "--json")] = _ok(
        stdout=_FWUPDMGR_GET_UPDATES_JSON,
    )
    config = UefiCapsuleUpdateManagerConfig(
        override_host_os="Linux", elevated=True, allow_downgrade=True,
    )
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)

    result = manager.apply_update(update_id="release-bbb-4.5.0")
    testbase.assert_equal(result.status, UefiCapsuleUpdateActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls if c["args"][:3] == [
            "fwupdmgr", "update", "--no-reboot-check",
        ]
    ]
    testbase.assert_equal(len(write_calls), 1)
    return


def test_apply_update_proceeds_on_unknown_version_format():
    """
        Vendor-specific version strings whose ordering cannot be derived
        cleanly are treated as non-downgrades — ``apply_update``
        proceeds.
    """
    runner = _MockCommandRunner(default_result=_ok())
    custom_payload = json.dumps({
        "Devices": [
            {
                "DeviceId": "dev-vendor",
                "Name": "Vendor Device",
                "Version": "0.A.B.0123",
                "Releases": [
                    {
                        "ReleaseId": "release-vendor",
                        "Version": "0.A.B.0124",
                    },
                ],
            },
        ],
    })
    runner.results[("fwupdmgr", "get-updates", "--json")] = _ok(
        stdout=custom_payload,
    )
    config = UefiCapsuleUpdateManagerConfig(
        override_host_os="Linux", elevated=True,
    )
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)

    result = manager.apply_update(update_id="release-vendor")
    testbase.assert_equal(result.status, UefiCapsuleUpdateActionStatus.SUCCESS)
    return


# ----------------------------------------------------------------------
# apply_capsule_file
# ----------------------------------------------------------------------


def test_apply_capsule_file_linux_constructs_fwupdmgr_install():
    """
        Linux ``apply_capsule_file`` constructs ``fwupdmgr install
        --allow-reinstall --no-reboot-check <path>``.
    """
    runner = _MockCommandRunner(default_result=_ok())
    config = UefiCapsuleUpdateManagerConfig(
        override_host_os="Linux", elevated=True,
    )
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)

    result = manager.apply_capsule_file(path="/tmp/firmware.cab")
    testbase.assert_equal(result.status, UefiCapsuleUpdateActionStatus.SUCCESS)
    testbase.assert_equal(
        runner.calls[0]["args"],
        [
            "fwupdmgr",
            "install",
            "--allow-reinstall",
            "--no-reboot-check",
            "/tmp/firmware.cab",
        ],
    )
    testbase.assert_equal(runner.calls[0]["elevated"], True)
    return


def test_apply_capsule_file_darwin_returns_failed_with_token():
    """
        Darwin ``apply_capsule_file`` returns ``FAILED`` with a stable
        ``unsupported_on_host_os_darwin`` token and invokes no command.
    """
    runner = _MockCommandRunner()
    config = UefiCapsuleUpdateManagerConfig(override_host_os="Darwin")
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)

    result = manager.apply_capsule_file(path="/tmp/firmware.cap")
    testbase.assert_equal(result.status, UefiCapsuleUpdateActionStatus.FAILED)
    testbase.assert_equal(
        "unsupported_on_host_os_darwin" in (result.detail or ""), True,
    )
    testbase.assert_equal(runner.calls, [])
    return


def test_apply_capsule_file_windows_returns_failed_with_oem_detail():
    """
        Windows ``apply_capsule_file`` returns ``FAILED`` with a stable
        ``unsupported_on_host_os_windows`` token and a detail explaining
        the Windows gap.
    """
    runner = _MockCommandRunner()
    config = UefiCapsuleUpdateManagerConfig(override_host_os="Windows")
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)

    result = manager.apply_capsule_file(path="C:\\firmware.cap")
    testbase.assert_equal(result.status, UefiCapsuleUpdateActionStatus.FAILED)
    testbase.assert_equal(
        "unsupported_on_host_os_windows" in (result.detail or ""), True,
    )
    testbase.assert_equal("OEM" in (result.detail or ""), True)
    testbase.assert_equal(runner.calls, [])
    return


def test_apply_capsule_file_dry_run_short_circuits():
    """
        Dry-run short-circuits ``apply_capsule_file`` without invoking
        the runner.
    """
    runner = _MockCommandRunner()
    config = UefiCapsuleUpdateManagerConfig(
        override_host_os="Linux", dry_run=True,
    )
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)

    result = manager.apply_capsule_file(path="/tmp/firmware.cab")
    testbase.assert_equal(result.status, UefiCapsuleUpdateActionStatus.DRY_RUN)
    testbase.assert_equal(runner.calls, [])
    testbase.assert_equal("install" in (result.command or []), True)
    return


# ----------------------------------------------------------------------
# get_update_status
# ----------------------------------------------------------------------


def test_get_update_status_linux_invokes_fwupdmgr_get_history_json():
    """
        Linux ``get_update_status`` invokes ``fwupdmgr get-history
        --json`` and surfaces the most-recent entry on the result.
    """
    runner = _MockCommandRunner()
    runner.results[("fwupdmgr", "get-history", "--json")] = _ok(
        stdout=_FWUPDMGR_GET_HISTORY_JSON,
    )
    runner.results[("fwupdmgr", "get-updates", "--json")] = _ok(
        stdout=_FWUPDMGR_GET_UPDATES_JSON,
    )
    config = UefiCapsuleUpdateManagerConfig(override_host_os="Linux")
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)

    status = manager.get_update_status()
    testbase.assert_equal(status.host_os, "linux")
    testbase.assert_equal(status.last_status, "success")
    testbase.assert_equal(status.last_attempt, "2025-12-01T08:00:00Z")
    testbase.assert_equal(status.pending_count, 2)
    return


def test_get_update_status_darwin_invokes_softwareupdate_history():
    """
        Darwin ``get_update_status`` invokes ``softwareupdate --history``
        and produces a snapshot.
    """
    runner = _MockCommandRunner()
    runner.results[("softwareupdate", "--history")] = _ok(
        stdout=_SOFTWAREUPDATE_HISTORY_OUTPUT,
    )
    runner.results[("softwareupdate", "--list", "--include-config-data")] = _ok(
        stdout=_SOFTWAREUPDATE_LIST_OUTPUT,
    )
    config = UefiCapsuleUpdateManagerConfig(override_host_os="Darwin")
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)

    status = manager.get_update_status()
    testbase.assert_equal(status.host_os, "darwin")
    testbase.assert_equal(status.pending_count, 1)
    return


def test_get_update_status_windows_invokes_get_wuhistory():
    """
        Windows ``get_update_status`` invokes ``Get-WUHistory`` filtered
        to firmware.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok(stdout=_GET_WUHISTORY_JSON)
    config = UefiCapsuleUpdateManagerConfig(override_host_os="Windows")
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)

    status = manager.get_update_status()
    testbase.assert_equal(status.host_os, "windows")
    history_calls = [
        c for c in runner.calls if "Get-WUHistory" in (c["args"][-1] or "")
    ]
    testbase.assert_equal(len(history_calls) >= 1, True)
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
    config = UefiCapsuleUpdateManagerConfig(
        override_host_os="Linux", dry_run=True,
    )
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)

    apply_result = manager.apply_update(update_id="any-id")
    capsule_result = manager.apply_capsule_file(path="/tmp/x.cab")

    testbase.assert_equal(apply_result.status, UefiCapsuleUpdateActionStatus.DRY_RUN)
    testbase.assert_equal(capsule_result.status, UefiCapsuleUpdateActionStatus.DRY_RUN)
    testbase.assert_equal(runner.calls, [])
    return


def test_writes_pass_elevated_true_when_configured():
    """
        Writes pass ``elevated=True`` when the config sets it; reads do
        not.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("fwupdmgr", "get-updates", "--json")] = _ok(
        stdout=_FWUPDMGR_GET_UPDATES_JSON,
    )
    config = UefiCapsuleUpdateManagerConfig(
        override_host_os="Linux", elevated=True,
    )
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)

    manager.list_available_updates()
    manager.apply_update(update_id="release-aaa-2.0.0")

    read_calls = [c for c in runner.calls if c["args"][:2] == [
        "fwupdmgr", "get-updates",
    ]]
    testbase.assert_equal(read_calls[0]["elevated"], False)

    write_calls = [c for c in runner.calls if c["args"][:2] == [
        "fwupdmgr", "update",
    ]]
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_reads_never_pass_elevated_true():
    """
        Read operations never pass ``elevated=True`` regardless of
        config.
    """
    runner = _MockCommandRunner()
    runner.results[("fwupdmgr", "get-updates", "--json")] = _ok(
        stdout=_FWUPDMGR_GET_UPDATES_JSON,
    )
    runner.results[("fwupdmgr", "get-devices", "--json")] = _ok(
        stdout=_FWUPDMGR_GET_DEVICES_JSON,
    )
    config = UefiCapsuleUpdateManagerConfig(
        override_host_os="Linux", elevated=True,
    )
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)
    manager.list_available_updates()
    manager.list_devices()
    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_apply_command_populates_exit_code_and_stderr():
    """
        A non-zero exit on a write surfaces as ``FAILED`` with the exit
        code and stderr captured.
    """
    runner = _MockCommandRunner(
        default_result=_fail(stderr="permission denied", exit_code=2),
    )
    runner.results[("fwupdmgr", "get-updates", "--json")] = _ok(
        stdout=_FWUPDMGR_GET_UPDATES_JSON,
    )
    config = UefiCapsuleUpdateManagerConfig(
        override_host_os="Linux", elevated=True,
    )
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)

    result = manager.apply_update(update_id="release-aaa-2.0.0")
    testbase.assert_equal(result.status, UefiCapsuleUpdateActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_equal("permission denied" in (result.stderr or ""), True)
    return


# ----------------------------------------------------------------------
# Parser-level tests
# ----------------------------------------------------------------------


def test_parse_fwupdmgr_get_updates_extracts_releases():
    """
        Direct parser test for the ``fwupdmgr get-updates --json``
        payload.
    """
    runner = _MockCommandRunner()
    runner.results[("fwupdmgr", "get-updates", "--json")] = _ok(
        stdout=_FWUPDMGR_GET_UPDATES_JSON,
    )
    config = UefiCapsuleUpdateManagerConfig(override_host_os="Linux")
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)
    updates = manager.list_available_updates()
    by_id = {u.update_id: u for u in updates}
    testbase.assert_equal(by_id["release-aaa-2.0.0"].device_id, "dev-aaa")
    testbase.assert_equal(by_id["release-aaa-2.0.0"].device_name, "System Firmware")
    return


def test_parse_softwareupdate_list_filters_firmware_entries():
    """
        Direct parser test for ``softwareupdate --list``: filters to
        entries whose Title contains "Firmware".
    """
    runner = _MockCommandRunner()
    runner.results[("softwareupdate", "--list", "--include-config-data")] = _ok(
        stdout=_SOFTWAREUPDATE_LIST_OUTPUT,
    )
    config = UefiCapsuleUpdateManagerConfig(override_host_os="Darwin")
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)
    updates = manager.list_available_updates()
    testbase.assert_equal(len(updates), 1)
    testbase.assert_equal("Firmware" in (updates[0].description or ""), True)
    return


def test_parse_wmi_json_builds_device_info():
    """
        Direct parser test for the ``Get-CimInstance`` JSON payload.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok(stdout=_GET_WMI_JSON)
    config = UefiCapsuleUpdateManagerConfig(override_host_os="Windows")
    manager = UefiCapsuleUpdateManager(runner=runner, config=config)
    devices = manager.list_devices()
    testbase.assert_equal(len(devices), 1)
    testbase.assert_equal(devices[0].vendor, "Microsoft")
    testbase.assert_equal(devices[0].name, "Surface Laptop 5")
    return
