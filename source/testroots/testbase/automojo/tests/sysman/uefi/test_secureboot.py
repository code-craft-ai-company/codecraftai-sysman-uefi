"""
    Tests for :class:`UefiSecureBootManager`.

    All tests use a ``_MockCommandRunner`` keyed on argv tuples.  No
    live ``mokutil`` / ``efivar`` / ``csrutil`` / ``bputil`` /
    PowerShell commands are invoked.

    Per-host-OS dispatch is exercised on every developer workstation by
    setting ``UefiSecureBootManagerConfig.override_host_os`` to one of
    ``"Linux"``, ``"Darwin"``, ``"Windows"``.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.uefi.secureboot import (
    UefiKeyInfo,
    UefiSecureBootActionResult,
    UefiSecureBootActionStatus,
    UefiSecureBootManager,
    UefiSecureBootManagerConfig,
    UefiSecureBootPolicy,
    UefiSecureBootQueryError,
    UefiSecureBootState,
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


def _fail(
    stderr: str = "boom",
    exit_code: int = 1,
    stdout: str = "",
) -> RunResult:
    rtnval = RunResult(exit_code=exit_code, stdout=stdout, stderr=stderr)
    return rtnval


_MOKUTIL_LIST_ENROLLED_OUTPUT = (
    "[key 1]\n"
    "SHA1 Fingerprint: aa:bb:cc:dd:ee:ff:00:11:22:33\n"
    "        Data:\n"
    "            Version: 3 (0x2)\n"
    "            Subject: CN=My Module Signer\n"
    "            Issuer: CN=My Module Signer\n"
    "[key 2]\n"
    "SHA1 Fingerprint: 11:22:33:44:55:66:77:88:99:00\n"
    "        Subject: CN=Other Signer\n"
)


_BPUTIL_FULL_SECURITY_OUTPUT = (
    "Boot Policy: 1.0\n"
    "Full Security\n"
    "  Allows installation of any unrestricted version of macOS\n"
)


_CSRUTIL_STATUS_OUTPUT = "System Integrity Protection status: enabled.\n"


# ----------------------------------------------------------------------
# Construction / dispatch override
# ----------------------------------------------------------------------


def test_override_host_os_forces_linux_dispatch():
    runner = _MockCommandRunner()
    config = UefiSecureBootManagerConfig(override_host_os="Linux", dry_run=True)
    manager = UefiSecureBootManager(runner=runner, config=config)
    testbase.assert_equal(manager.host_os, "linux")
    return


def test_override_host_os_forces_darwin_dispatch():
    runner = _MockCommandRunner()
    config = UefiSecureBootManagerConfig(override_host_os="Darwin", dry_run=True)
    manager = UefiSecureBootManager(runner=runner, config=config)
    testbase.assert_equal(manager.host_os, "darwin")
    return


def test_override_host_os_forces_windows_dispatch():
    runner = _MockCommandRunner()
    config = UefiSecureBootManagerConfig(override_host_os="Windows", dry_run=True)
    manager = UefiSecureBootManager(runner=runner, config=config)
    testbase.assert_equal(manager.host_os, "windows")
    return


def test_override_host_os_rejects_invalid_value_in_constructor():
    runner = _MockCommandRunner()
    config = UefiSecureBootManagerConfig(override_host_os="solaris")
    raised = False
    try:
        UefiSecureBootManager(runner=runner, config=config)
    except ValueError:
        raised = True
    testbase.assert_equal(raised, True)
    return


# ----------------------------------------------------------------------
# get_secure_boot_state
# ----------------------------------------------------------------------


def test_get_secure_boot_state_linux_invokes_mokutil_sb_state():
    """
        Linux ``get_secure_boot_state`` invokes ``mokutil --sb-state``
        without elevation and parses the output.
    """
    runner = _MockCommandRunner()
    runner.results[("mokutil", "--sb-state")] = _ok(stdout="SecureBoot enabled\n")
    config = UefiSecureBootManagerConfig(override_host_os="Linux")
    manager = UefiSecureBootManager(runner=runner, config=config)

    state = manager.get_secure_boot_state()
    testbase.assert_equal(state.enabled, True)
    testbase.assert_equal(state.supported, True)
    testbase.assert_equal(state.host_os, "linux")
    testbase.assert_equal(runner.calls[0]["args"], ["mokutil", "--sb-state"])
    testbase.assert_equal(runner.calls[0]["elevated"], False)
    return


def test_get_secure_boot_state_linux_not_supported_returns_supported_false():
    """
        ``mokutil`` reporting "doesn't support Secure Boot" returns
        ``supported=False`` without raising.
    """
    runner = _MockCommandRunner()
    runner.results[("mokutil", "--sb-state")] = RunResult(
        exit_code=1,
        stdout="This system doesn't support Secure Boot\n",
        stderr="",
    )
    config = UefiSecureBootManagerConfig(override_host_os="Linux")
    manager = UefiSecureBootManager(runner=runner, config=config)

    state = manager.get_secure_boot_state()
    testbase.assert_equal(state.supported, False)
    testbase.assert_equal(state.enabled, False)
    return


def test_get_secure_boot_state_linux_unrecoverable_failure_raises():
    """
        A non-zero exit with an unrelated stderr surfaces as
        ``UefiSecureBootQueryError``.
    """
    runner = _MockCommandRunner()
    runner.results[("mokutil", "--sb-state")] = _fail(stderr="permission denied", exit_code=2)
    config = UefiSecureBootManagerConfig(override_host_os="Linux")
    manager = UefiSecureBootManager(runner=runner, config=config)

    raised = False
    try:
        manager.get_secure_boot_state()
    except UefiSecureBootQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


def test_get_secure_boot_state_darwin_invokes_csrutil_and_bputil():
    """
        Darwin ``get_secure_boot_state`` invokes both ``csrutil status``
        and ``bputil --display`` without elevation.
    """
    runner = _MockCommandRunner()
    runner.results[("csrutil", "status")] = _ok(stdout=_CSRUTIL_STATUS_OUTPUT)
    runner.results[("bputil", "--display")] = _ok(stdout=_BPUTIL_FULL_SECURITY_OUTPUT)
    config = UefiSecureBootManagerConfig(override_host_os="Darwin")
    manager = UefiSecureBootManager(runner=runner, config=config)

    state = manager.get_secure_boot_state()
    testbase.assert_equal(state.host_os, "darwin")
    testbase.assert_equal(state.policy_description, "Full Security")
    testbase.assert_equal(state.enabled, True)
    args_seen = [tuple(c["args"]) for c in runner.calls]
    testbase.assert_equal(("csrutil", "status") in args_seen, True)
    testbase.assert_equal(("bputil", "--display") in args_seen, True)
    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_get_secure_boot_state_windows_invokes_confirm_secureboot_uefi():
    """
        Windows ``get_secure_boot_state`` invokes ``Confirm-
        SecureBootUEFI`` via PowerShell without elevation.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "powershell", "-NoProfile", "-Command", "Confirm-SecureBootUEFI",
    )] = _ok(stdout="True\n")
    config = UefiSecureBootManagerConfig(override_host_os="Windows")
    manager = UefiSecureBootManager(runner=runner, config=config)

    state = manager.get_secure_boot_state()
    testbase.assert_equal(state.enabled, True)
    testbase.assert_equal(state.host_os, "windows")
    testbase.assert_equal(runner.calls[0]["elevated"], False)
    return


# ----------------------------------------------------------------------
# get_secure_boot_policy
# ----------------------------------------------------------------------


def test_get_secure_boot_policy_linux_invokes_mokutil_list_enrolled_and_efivar_l():
    """
        Linux ``get_secure_boot_policy`` invokes both ``mokutil --list-
        enrolled`` and ``efivar -l``.
    """
    runner = _MockCommandRunner()
    runner.results[("mokutil", "--list-enrolled")] = _ok(
        stdout=_MOKUTIL_LIST_ENROLLED_OUTPUT,
    )
    runner.results[("efivar", "-l")] = _ok(stdout=(
        "8be4df61-93ca-11d2-aa0d-00e098032b8c-PK\n"
        "8be4df61-93ca-11d2-aa0d-00e098032b8c-KEK\n"
        "d719b2cb-3d3a-4596-a3bc-dad00e67656f-db\n"
        "d719b2cb-3d3a-4596-a3bc-dad00e67656f-dbx\n"
    ))
    config = UefiSecureBootManagerConfig(override_host_os="Linux")
    manager = UefiSecureBootManager(runner=runner, config=config)

    policy = manager.get_secure_boot_policy()
    testbase.assert_equal(policy.pk_present, True)
    testbase.assert_equal(policy.kek_count, 1)
    testbase.assert_equal(policy.db_count, 1)
    testbase.assert_equal(policy.dbx_count, 1)
    testbase.assert_equal(policy.mok_count, 2)
    testbase.assert_equal(policy.apple_policy_raw, None)
    return


def test_get_secure_boot_policy_darwin_preserves_raw_text():
    """
        Darwin ``get_secure_boot_policy`` puts the full ``bputil
        --display`` output into ``apple_policy_raw`` and leaves the
        slot counts ``None``.
    """
    runner = _MockCommandRunner()
    runner.results[("bputil", "--display")] = _ok(stdout=_BPUTIL_FULL_SECURITY_OUTPUT)
    config = UefiSecureBootManagerConfig(override_host_os="Darwin")
    manager = UefiSecureBootManager(runner=runner, config=config)

    policy = manager.get_secure_boot_policy()
    testbase.assert_equal(policy.apple_policy_raw, _BPUTIL_FULL_SECURITY_OUTPUT)
    testbase.assert_equal(policy.kek_count, None)
    testbase.assert_equal(policy.db_count, None)
    testbase.assert_equal(policy.mok_count, None)
    return


def test_get_secure_boot_policy_windows_extracts_publisher():
    """
        Windows ``get_secure_boot_policy`` invokes ``Get-
        SecureBootPolicy | ConvertTo-Json`` and treats the Publisher
        token as the PK indicator.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "powershell", "-NoProfile", "-Command", "Get-SecureBootPolicy | ConvertTo-Json",
    )] = _ok(stdout=(
        "{\n"
        "    \"Publisher\": \"77fa9abd-0359-4d32-bd60-28f4e78f784b\",\n"
        "    \"Version\": 1\n"
        "}\n"
    ))
    config = UefiSecureBootManagerConfig(override_host_os="Windows")
    manager = UefiSecureBootManager(runner=runner, config=config)

    policy = manager.get_secure_boot_policy()
    testbase.assert_equal(policy.pk_present, True)
    return


# ----------------------------------------------------------------------
# list_enrolled_keys
# ----------------------------------------------------------------------


def test_list_enrolled_keys_linux_parses_mokutil_blocks():
    """
        Linux ``list_enrolled_keys`` parses ``mokutil --list-enrolled``
        into ``UefiKeyInfo`` records with ``slot="MOK"``.
    """
    runner = _MockCommandRunner()
    runner.results[("mokutil", "--list-enrolled")] = _ok(
        stdout=_MOKUTIL_LIST_ENROLLED_OUTPUT,
    )
    config = UefiSecureBootManagerConfig(override_host_os="Linux")
    manager = UefiSecureBootManager(runner=runner, config=config)

    keys = manager.list_enrolled_keys(slot="MOK")
    testbase.assert_equal(len(keys), 2)
    testbase.assert_equal(keys[0].slot, "MOK")
    testbase.assert_equal(
        keys[0].fingerprint, "aa:bb:cc:dd:ee:ff:00:11:22:33",
    )
    testbase.assert_equal(keys[0].subject, "CN=My Module Signer")
    return


def test_list_enrolled_keys_mok_empty_on_macos():
    """
        ``list_enrolled_keys(slot="MOK")`` returns ``[]`` on macOS.
    """
    runner = _MockCommandRunner()
    runner.results[("bputil", "--display")] = _ok(stdout=_BPUTIL_FULL_SECURITY_OUTPUT)
    config = UefiSecureBootManagerConfig(override_host_os="Darwin")
    manager = UefiSecureBootManager(runner=runner, config=config)

    rtnval = manager.list_enrolled_keys(slot="MOK")
    testbase.assert_equal(rtnval, [])
    return


def test_list_enrolled_keys_mok_empty_on_windows():
    """
        ``list_enrolled_keys(slot="MOK")`` returns ``[]`` on Windows.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "powershell", "-NoProfile", "-Command", "Get-SecureBootPolicy | ConvertTo-Json",
    )] = _ok(stdout="{ \"Publisher\": \"abc\" }")
    config = UefiSecureBootManagerConfig(override_host_os="Windows")
    manager = UefiSecureBootManager(runner=runner, config=config)
    rtnval = manager.list_enrolled_keys(slot="MOK")
    testbase.assert_equal(rtnval, [])
    return


# ----------------------------------------------------------------------
# enroll_mok_key / reset_mok
# ----------------------------------------------------------------------


def test_enroll_mok_key_linux_constructs_mokutil_import():
    """
        Linux ``enroll_mok_key`` constructs ``mokutil --import <path>``.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("mokutil", "--list-enrolled")] = _ok(
        stdout=_MOKUTIL_LIST_ENROLLED_OUTPUT,
    )
    config = UefiSecureBootManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiSecureBootManager(runner=runner, config=config)

    result = manager.enroll_mok_key(path="/tmp/new-key.der")
    testbase.assert_equal(result.status, UefiSecureBootActionStatus.SUCCESS)
    testbase.assert_equal(result.requires_reboot, True)
    write_calls = [c for c in runner.calls if c["args"][:2] == ["mokutil", "--import"]]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"], ["mokutil", "--import", "/tmp/new-key.der"],
    )
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_enroll_mok_key_darwin_returns_failed_with_apple_detail():
    """
        Darwin ``enroll_mok_key`` returns ``FAILED`` with a stable
        token-prefixed detail and invokes no command.
    """
    runner = _MockCommandRunner()
    config = UefiSecureBootManagerConfig(override_host_os="Darwin", elevated=True)
    manager = UefiSecureBootManager(runner=runner, config=config)

    result = manager.enroll_mok_key(path="/tmp/key.der")
    testbase.assert_equal(result.status, UefiSecureBootActionStatus.FAILED)
    testbase.assert_equal(
        "unsupported_on_host_os_darwin" in (result.detail or ""), True,
    )
    testbase.assert_equal(runner.calls, [])
    return


def test_enroll_mok_key_windows_returns_failed_with_oem_detail():
    """
        Windows ``enroll_mok_key`` returns ``FAILED`` and invokes no
        command.
    """
    runner = _MockCommandRunner()
    config = UefiSecureBootManagerConfig(override_host_os="Windows", elevated=True)
    manager = UefiSecureBootManager(runner=runner, config=config)

    result = manager.enroll_mok_key(path="/tmp/key.der")
    testbase.assert_equal(result.status, UefiSecureBootActionStatus.FAILED)
    testbase.assert_equal(
        "unsupported_on_host_os_windows" in (result.detail or ""), True,
    )
    testbase.assert_equal(runner.calls, [])
    return


def test_reset_mok_linux_constructs_mokutil_delete():
    """
        Linux ``reset_mok`` constructs ``mokutil --delete <id>``.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("mokutil", "--list-enrolled")] = _ok(
        stdout=_MOKUTIL_LIST_ENROLLED_OUTPUT,
    )
    config = UefiSecureBootManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiSecureBootManager(runner=runner, config=config)

    result = manager.reset_mok(key_id="aa:bb:cc:dd:ee:ff:00:11:22:33")
    testbase.assert_equal(result.status, UefiSecureBootActionStatus.SUCCESS)
    write_calls = [c for c in runner.calls if c["args"][:2] == ["mokutil", "--delete"]]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_reset_mok_skipped_when_fingerprint_absent():
    """
        ``reset_mok`` returns ``SKIPPED`` when the fingerprint is not in
        the MOK list.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("mokutil", "--list-enrolled")] = _ok(
        stdout=_MOKUTIL_LIST_ENROLLED_OUTPUT,
    )
    config = UefiSecureBootManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiSecureBootManager(runner=runner, config=config)

    result = manager.reset_mok(key_id="ff:ff:ff:ff:ff")
    testbase.assert_equal(result.status, UefiSecureBootActionStatus.SKIPPED)
    delete_calls = [c for c in runner.calls if c["args"][:2] == ["mokutil", "--delete"]]
    testbase.assert_equal(delete_calls, [])
    return


# ----------------------------------------------------------------------
# set_secure_boot_mode (universal FAILED)
# ----------------------------------------------------------------------


def test_set_secure_boot_mode_always_failed_on_linux():
    """
        ``set_secure_boot_mode`` returns ``FAILED`` on Linux without
        invoking any command.
    """
    runner = _MockCommandRunner()
    config = UefiSecureBootManagerConfig(override_host_os="Linux")
    manager = UefiSecureBootManager(runner=runner, config=config)

    result = manager.set_secure_boot_mode(mode="user")
    testbase.assert_equal(result.status, UefiSecureBootActionStatus.FAILED)
    testbase.assert_equal(
        "firmware_setup_ui_only" in (result.detail or ""), True,
    )
    testbase.assert_equal(runner.calls, [])
    return


def test_set_secure_boot_mode_always_failed_on_darwin():
    runner = _MockCommandRunner()
    config = UefiSecureBootManagerConfig(override_host_os="Darwin")
    manager = UefiSecureBootManager(runner=runner, config=config)
    result = manager.set_secure_boot_mode(mode="user")
    testbase.assert_equal(result.status, UefiSecureBootActionStatus.FAILED)
    testbase.assert_equal(runner.calls, [])
    return


def test_set_secure_boot_mode_always_failed_on_windows():
    runner = _MockCommandRunner()
    config = UefiSecureBootManagerConfig(override_host_os="Windows")
    manager = UefiSecureBootManager(runner=runner, config=config)
    result = manager.set_secure_boot_mode(mode="user")
    testbase.assert_equal(result.status, UefiSecureBootActionStatus.FAILED)
    testbase.assert_equal(runner.calls, [])
    return


def test_set_secure_boot_mode_dry_run_returns_dry_run():
    """
        Dry-run mode returns ``DRY_RUN`` for ``set_secure_boot_mode``
        on every host.
    """
    runner = _MockCommandRunner()
    config = UefiSecureBootManagerConfig(override_host_os="Linux", dry_run=True)
    manager = UefiSecureBootManager(runner=runner, config=config)
    result = manager.set_secure_boot_mode(mode="setup")
    testbase.assert_equal(result.status, UefiSecureBootActionStatus.DRY_RUN)
    testbase.assert_equal(runner.calls, [])
    return


# ----------------------------------------------------------------------
# Dry-run / elevation
# ----------------------------------------------------------------------


def test_dry_run_short_circuits_enroll_and_reset():
    """
        Dry-run short-circuits both writes without invoking the runner.
    """
    runner = _MockCommandRunner()
    config = UefiSecureBootManagerConfig(override_host_os="Linux", dry_run=True)
    manager = UefiSecureBootManager(runner=runner, config=config)

    enroll = manager.enroll_mok_key(path="/tmp/x.der")
    reset = manager.reset_mok(key_id="aa:bb")

    testbase.assert_equal(enroll.status, UefiSecureBootActionStatus.DRY_RUN)
    testbase.assert_equal(reset.status, UefiSecureBootActionStatus.DRY_RUN)
    testbase.assert_equal(runner.calls, [])
    return


def test_writes_pass_elevated_true_when_configured():
    """
        Linux MOK writes pass ``elevated=True`` when the config sets
        it.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("mokutil", "--list-enrolled")] = _ok(stdout="")
    config = UefiSecureBootManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiSecureBootManager(runner=runner, config=config)

    manager.enroll_mok_key(path="/tmp/key.der")
    write_calls = [c for c in runner.calls if c["args"][:2] == ["mokutil", "--import"]]
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_reads_never_pass_elevated_true():
    """
        Read operations never pass ``elevated=True`` regardless of
        config.
    """
    runner = _MockCommandRunner()
    runner.results[("mokutil", "--sb-state")] = _ok(stdout="SecureBoot enabled\n")
    config = UefiSecureBootManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiSecureBootManager(runner=runner, config=config)
    manager.get_secure_boot_state()
    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_write_populates_exit_code_and_stderr():
    """
        A non-zero exit on a Linux MOK write surfaces as ``FAILED``
        with the exit code and stderr captured.
    """
    runner = _MockCommandRunner()
    runner.results[("mokutil", "--list-enrolled")] = _ok(stdout="")
    runner.results[("mokutil", "--import", "/tmp/key.der")] = _fail(
        stderr="permission denied", exit_code=2,
    )
    config = UefiSecureBootManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiSecureBootManager(runner=runner, config=config)

    result = manager.enroll_mok_key(path="/tmp/key.der")
    testbase.assert_equal(result.status, UefiSecureBootActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_equal("permission denied" in (result.stderr or ""), True)
    return


# ----------------------------------------------------------------------
# Parser-level tests
# ----------------------------------------------------------------------


def test_parse_mokutil_list_enrolled_extracts_fingerprint_and_subject():
    """
        The MOK parser extracts both the SHA1 fingerprint and the
        Subject from each ``[key N]`` block.
    """
    runner = _MockCommandRunner()
    runner.results[("mokutil", "--list-enrolled")] = _ok(
        stdout=_MOKUTIL_LIST_ENROLLED_OUTPUT,
    )
    config = UefiSecureBootManagerConfig(override_host_os="Linux")
    manager = UefiSecureBootManager(runner=runner, config=config)
    keys = manager.list_enrolled_keys()
    testbase.assert_equal(len(keys), 2)
    testbase.assert_equal(keys[1].subject, "CN=Other Signer")
    testbase.assert_equal(
        keys[1].fingerprint, "11:22:33:44:55:66:77:88:99:00",
    )
    return


def test_parse_bputil_display_extracts_full_security_summary():
    """
        The bputil parser surfaces "Full Security" as the
        ``policy_description``.
    """
    runner = _MockCommandRunner()
    runner.results[("csrutil", "status")] = _ok(stdout=_CSRUTIL_STATUS_OUTPUT)
    runner.results[("bputil", "--display")] = _ok(stdout=_BPUTIL_FULL_SECURITY_OUTPUT)
    config = UefiSecureBootManagerConfig(override_host_os="Darwin")
    manager = UefiSecureBootManager(runner=runner, config=config)
    state = manager.get_secure_boot_state()
    testbase.assert_equal(state.policy_description, "Full Security")
    return
