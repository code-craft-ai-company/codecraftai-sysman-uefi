"""
    Tests for :class:`UefiVariableManager`.

    All tests use a ``_MockCommandRunner`` keyed on argv tuples.  No
    live ``efivar`` / ``nvram`` / PowerShell commands are invoked.

    Per-host-OS dispatch is exercised on every developer workstation by
    setting ``UefiVariableManagerConfig.override_host_os`` to one of
    ``"Linux"``, ``"Darwin"``, ``"Windows"``.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.uefi.efivariables import (
    BOOTSERVICE_ACCESS,
    DEFAULT_ATTRIBUTES,
    NON_VOLATILE,
    RUNTIME_ACCESS,
    UefiVariable,
    UefiVariableActionResult,
    UefiVariableActionStatus,
    UefiVariableInfo,
    UefiVariableManager,
    UefiVariableManagerConfig,
    UefiVariableNamespaceError,
    UefiVariableNotFoundError,
    UefiVariableQueryError,
    normalise_namespace,
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


_GLOBAL_GUID = "8be4df61-93ca-11d2-aa0d-00e098032b8c"
_DBX_GUID = "d719b2cb-3d3a-4596-a3bc-dad00e67656f"


_EFIVAR_BOOTORDER_OUTPUT = (
    "GUID: 8be4df61-93ca-11d2-aa0d-00e098032b8c\n"
    "Name: \"BootOrder\"\n"
    "Attributes:\n"
    "        Non-Volatile\n"
    "        Boot Service Access\n"
    "        Runtime Service Access\n"
    "Value:\n"
    "00000000  01 00 00 00 02 00 2f 00  |....../.|\n"
)


_EFIVAR_L_OUTPUT = (
    "8be4df61-93ca-11d2-aa0d-00e098032b8c-BootOrder\n"
    "8be4df61-93ca-11d2-aa0d-00e098032b8c-BootCurrent\n"
    "d719b2cb-3d3a-4596-a3bc-dad00e67656f-db\n"
)


_NVRAM_BOOT_ARGS_OUTPUT = "Apple:boot-args\t-v\n"


_NVRAM_DASH_P_OUTPUT = (
    "Apple:boot-args\t-v\n"
    "Common:SystemAudioVolume\t%80\n"
)


# ----------------------------------------------------------------------
# Construction / dispatch override
# ----------------------------------------------------------------------


def test_override_host_os_forces_linux_dispatch():
    runner = _MockCommandRunner()
    config = UefiVariableManagerConfig(override_host_os="Linux", dry_run=True)
    manager = UefiVariableManager(runner=runner, config=config)
    testbase.assert_equal(manager.host_os, "linux")
    return


def test_override_host_os_forces_darwin_dispatch():
    runner = _MockCommandRunner()
    config = UefiVariableManagerConfig(override_host_os="Darwin", dry_run=True)
    manager = UefiVariableManager(runner=runner, config=config)
    testbase.assert_equal(manager.host_os, "darwin")
    return


def test_override_host_os_forces_windows_dispatch():
    runner = _MockCommandRunner()
    config = UefiVariableManagerConfig(override_host_os="Windows", dry_run=True)
    manager = UefiVariableManager(runner=runner, config=config)
    testbase.assert_equal(manager.host_os, "windows")
    return


def test_override_host_os_rejects_invalid_value_in_constructor():
    runner = _MockCommandRunner()
    config = UefiVariableManagerConfig(override_host_os="solaris")
    raised = False
    try:
        UefiVariableManager(runner=runner, config=config)
    except ValueError:
        raised = True
    testbase.assert_equal(raised, True)
    return


# ----------------------------------------------------------------------
# Namespace registry
# ----------------------------------------------------------------------


def test_namespace_efi_global_alias_normalised_to_guid():
    """
        ``EFI_GLOBAL_VARIABLE`` normalises to the canonical GUID.
    """
    rtnval = normalise_namespace(
        host_os="linux", namespace="EFI_GLOBAL_VARIABLE",
    )
    testbase.assert_equal(rtnval, _GLOBAL_GUID)
    return


def test_namespace_apple_rejected_on_linux():
    """
        ``Apple`` raises ``UefiVariableNamespaceError`` on Linux.
    """
    raised = False
    try:
        normalise_namespace(host_os="linux", namespace="Apple")
    except UefiVariableNamespaceError:
        raised = True
    testbase.assert_equal(raised, True)
    return


def test_namespace_apple_rejected_on_windows():
    """
        ``Apple`` raises ``UefiVariableNamespaceError`` on Windows.
    """
    raised = False
    try:
        normalise_namespace(host_os="windows", namespace="Apple")
    except UefiVariableNamespaceError:
        raised = True
    testbase.assert_equal(raised, True)
    return


def test_namespace_apple_accepted_on_darwin():
    """
        ``Apple`` passes through unchanged on macOS.
    """
    rtnval = normalise_namespace(host_os="darwin", namespace="Apple")
    testbase.assert_equal(rtnval, "Apple")
    return


def test_namespace_full_guid_pass_through():
    """
        A full lower-cased GUID passes through verbatim.
    """
    rtnval = normalise_namespace(host_os="linux", namespace=_GLOBAL_GUID)
    testbase.assert_equal(rtnval, _GLOBAL_GUID)
    return


def test_namespace_non_guid_non_alias_raises_value_error():
    """
        A non-GUID non-alias raises ``ValueError`` with the accepted
        aliases listed.
    """
    raised = False
    try:
        normalise_namespace(host_os="linux", namespace="bogus")
    except ValueError:
        raised = True
    testbase.assert_equal(raised, True)
    return


# ----------------------------------------------------------------------
# get_variable
# ----------------------------------------------------------------------


def test_get_variable_linux_invokes_efivar_p():
    """
        Linux ``get_variable`` invokes ``efivar -n <ns>-<name> -p``
        without elevation.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "efivar", "-n", "{}-BootOrder".format(_GLOBAL_GUID), "-p",
    )] = _ok(stdout=_EFIVAR_BOOTORDER_OUTPUT)
    config = UefiVariableManagerConfig(override_host_os="Linux")
    manager = UefiVariableManager(runner=runner, config=config)

    var = manager.get_variable(name="BootOrder", namespace="EFI_GLOBAL_VARIABLE")
    testbase.assert_equal(var.name, "BootOrder")
    testbase.assert_equal(var.namespace, _GLOBAL_GUID)
    testbase.assert_equal(var.value, b"\x01\x00\x00\x00\x02\x00\x2f\x00")
    testbase.assert_equal(
        var.attributes,
        NON_VOLATILE | BOOTSERVICE_ACCESS | RUNTIME_ACCESS,
    )
    testbase.assert_equal(runner.calls[0]["elevated"], False)
    return


def test_get_variable_linux_missing_raises_not_found():
    """
        ``efivar`` reporting "no such" raises
        ``UefiVariableNotFoundError``.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "efivar", "-n", "{}-Missing".format(_GLOBAL_GUID), "-p",
    )] = _fail(stderr="No such file or directory", exit_code=1)
    config = UefiVariableManagerConfig(override_host_os="Linux")
    manager = UefiVariableManager(runner=runner, config=config)

    raised = False
    try:
        manager.get_variable(name="Missing", namespace=_GLOBAL_GUID)
    except UefiVariableNotFoundError:
        raised = True
    testbase.assert_equal(raised, True)
    return


def test_get_variable_darwin_invokes_nvram_namespace_name():
    """
        Darwin ``get_variable`` invokes ``nvram <ns>:<name>``.
    """
    runner = _MockCommandRunner()
    runner.results[("nvram", "Apple:boot-args")] = _ok(stdout=_NVRAM_BOOT_ARGS_OUTPUT)
    config = UefiVariableManagerConfig(override_host_os="Darwin")
    manager = UefiVariableManager(runner=runner, config=config)

    var = manager.get_variable(name="boot-args", namespace="Apple")
    testbase.assert_equal(var.namespace, "Apple")
    testbase.assert_equal(var.name, "boot-args")
    testbase.assert_equal(var.value, b"-v")
    testbase.assert_equal(runner.calls[0]["elevated"], False)
    return


def test_get_variable_windows_invokes_powershell_getvariable():
    """
        Windows ``get_variable`` invokes the ``[UEFI]::GetVariable``
        PowerShell expression.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "powershell", "-NoProfile", "-Command",
        "[UEFI]::GetVariable('{}', 'BootOrder')".format(_GLOBAL_GUID),
    )] = _ok(stdout="1, 0, 2, 0\n")
    config = UefiVariableManagerConfig(override_host_os="Windows")
    manager = UefiVariableManager(runner=runner, config=config)

    var = manager.get_variable(name="BootOrder", namespace=_GLOBAL_GUID)
    testbase.assert_equal(var.value, b"\x01\x00\x02\x00")
    testbase.assert_equal(runner.calls[0]["elevated"], False)
    return


# ----------------------------------------------------------------------
# list_variables
# ----------------------------------------------------------------------


def test_list_variables_linux_invokes_efivar_l():
    """
        Linux ``list_variables`` invokes ``efivar -l`` and parses the
        ``<guid>-<name>`` lines.
    """
    runner = _MockCommandRunner()
    runner.results[("efivar", "-l")] = _ok(stdout=_EFIVAR_L_OUTPUT)
    config = UefiVariableManagerConfig(override_host_os="Linux")
    manager = UefiVariableManager(runner=runner, config=config)

    entries = manager.list_variables()
    testbase.assert_equal(len(entries), 3)
    testbase.assert_equal(entries[0].name, "BootOrder")
    testbase.assert_equal(entries[0].namespace, _GLOBAL_GUID)
    return


def test_list_variables_linux_namespace_filter():
    """
        ``list_variables(namespace=<dbx_guid>)`` filters to entries
        whose namespace matches.
    """
    runner = _MockCommandRunner()
    runner.results[("efivar", "-l")] = _ok(stdout=_EFIVAR_L_OUTPUT)
    config = UefiVariableManagerConfig(override_host_os="Linux")
    manager = UefiVariableManager(runner=runner, config=config)

    entries = manager.list_variables(namespace=_DBX_GUID)
    testbase.assert_equal(len(entries), 1)
    testbase.assert_equal(entries[0].namespace, _DBX_GUID)
    return


def test_list_variables_darwin_invokes_nvram_p():
    """
        Darwin ``list_variables`` invokes ``nvram -p``.
    """
    runner = _MockCommandRunner()
    runner.results[("nvram", "-p")] = _ok(stdout=_NVRAM_DASH_P_OUTPUT)
    config = UefiVariableManagerConfig(override_host_os="Darwin")
    manager = UefiVariableManager(runner=runner, config=config)

    entries = manager.list_variables()
    testbase.assert_equal(len(entries), 2)
    testbase.assert_equal(entries[0].namespace, "Apple")
    testbase.assert_equal(entries[0].name, "boot-args")
    return


def test_list_variables_windows_invokes_powershell_enumerate():
    """
        Windows ``list_variables`` invokes
        ``[UEFI]::EnumerateVariables() | ConvertTo-Json``.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "powershell", "-NoProfile", "-Command",
        "[UEFI]::EnumerateVariables() | ConvertTo-Json",
    )] = _ok(stdout=(
        "[\n"
        "    {\n"
        "        \"Namespace\": \"" + _GLOBAL_GUID + "\",\n"
        "        \"Name\": \"BootOrder\",\n"
        "        \"Attributes\": 7\n"
        "    },\n"
        "    {\n"
        "        \"Namespace\": \"" + _GLOBAL_GUID + "\",\n"
        "        \"Name\": \"Timeout\",\n"
        "        \"Attributes\": 7\n"
        "    }\n"
        "]\n"
    ))
    config = UefiVariableManagerConfig(override_host_os="Windows")
    manager = UefiVariableManager(runner=runner, config=config)

    entries = manager.list_variables()
    testbase.assert_equal(len(entries) >= 1, True)
    names = [e.name for e in entries]
    testbase.assert_equal("BootOrder" in names, True)
    return


# ----------------------------------------------------------------------
# set_variable
# ----------------------------------------------------------------------


def test_set_variable_linux_chattr_minus_i_then_efivar_w():
    """
        Linux ``set_variable`` issues ``chattr -i``, then ``efivar
        -w``, then ``chattr +i``.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[(
        "efivar", "-n", "{}-Timeout".format(_GLOBAL_GUID), "-p",
    )] = _fail(stderr="No such file or directory", exit_code=1)
    config = UefiVariableManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiVariableManager(runner=runner, config=config)

    new_value = (10).to_bytes(2, byteorder="little")
    result = manager.set_variable(
        name="Timeout",
        value=new_value,
        namespace="EFI_GLOBAL_VARIABLE",
        attributes=DEFAULT_ATTRIBUTES,
    )
    testbase.assert_equal(result.status, UefiVariableActionStatus.SUCCESS)

    write_argvs = [c["args"] for c in runner.calls if c["args"][:1] == ["efivar"] and "-w" in c["args"]]
    testbase.assert_equal(len(write_argvs), 1)
    chattr_clear = [c["args"] for c in runner.calls if c["args"][:2] == ["chattr", "-i"]]
    chattr_set = [c["args"] for c in runner.calls if c["args"][:2] == ["chattr", "+i"]]
    testbase.assert_equal(len(chattr_clear), 1)
    testbase.assert_equal(len(chattr_set), 1)

    write_call = [c for c in runner.calls if c["args"][:1] == ["efivar"] and "-w" in c["args"]][0]
    testbase.assert_equal(write_call["elevated"], True)
    testbase.assert_equal(chattr_clear[0][2], "/sys/firmware/efi/efivars/Timeout-{}".format(_GLOBAL_GUID))
    return


def test_set_variable_darwin_invokes_nvram_assignment():
    """
        Darwin ``set_variable`` invokes ``nvram <ns>:<name>=<value>``.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("nvram", "Apple:boot-args")] = _fail(
        stderr="not found", exit_code=1,
    )
    config = UefiVariableManagerConfig(override_host_os="Darwin", elevated=True)
    manager = UefiVariableManager(runner=runner, config=config)

    result = manager.set_variable(
        name="boot-args",
        value=b"-v",
        namespace="Apple",
        attributes=DEFAULT_ATTRIBUTES,
    )
    testbase.assert_equal(result.status, UefiVariableActionStatus.SUCCESS)
    write_calls = [c for c in runner.calls if c["args"][0] == "nvram" and "=" in (c["args"][1] if len(c["args"]) > 1 else "")]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["args"][1], "Apple:boot-args=-v")
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_set_variable_windows_invokes_powershell_setvariable():
    """
        Windows ``set_variable`` invokes the ``[UEFI]::SetVariable``
        PowerShell expression.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[(
        "powershell", "-NoProfile", "-Command",
        "[UEFI]::GetVariable('{}', 'Timeout')".format(_GLOBAL_GUID),
    )] = _fail(stderr="not found", exit_code=1)
    config = UefiVariableManagerConfig(override_host_os="Windows", elevated=True)
    manager = UefiVariableManager(runner=runner, config=config)

    result = manager.set_variable(
        name="Timeout",
        value=b"\x05\x00",
        namespace=_GLOBAL_GUID,
        attributes=DEFAULT_ATTRIBUTES,
    )
    testbase.assert_equal(result.status, UefiVariableActionStatus.SUCCESS)
    set_calls = [
        c for c in runner.calls
        if c["args"][0] == "powershell" and "SetVariable" in c["args"][-1]
    ]
    testbase.assert_equal(len(set_calls), 1)
    testbase.assert_equal(set_calls[0]["elevated"], True)
    return


def test_set_variable_skipped_when_value_and_attributes_match():
    """
        ``set_variable`` returns ``SKIPPED`` when the current value
        bytes and attributes equal the requested ones.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[(
        "efivar", "-n", "{}-BootOrder".format(_GLOBAL_GUID), "-p",
    )] = _ok(stdout=_EFIVAR_BOOTORDER_OUTPUT)
    config = UefiVariableManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiVariableManager(runner=runner, config=config)

    result = manager.set_variable(
        name="BootOrder",
        value=b"\x01\x00\x00\x00\x02\x00\x2f\x00",
        namespace="EFI_GLOBAL_VARIABLE",
        attributes=DEFAULT_ATTRIBUTES,
    )
    testbase.assert_equal(result.status, UefiVariableActionStatus.SKIPPED)
    write_calls = [c for c in runner.calls if c["args"][:1] == ["efivar"] and "-w" in c["args"]]
    testbase.assert_equal(write_calls, [])
    return


def test_set_variable_preserves_attributes_when_none():
    """
        ``set_variable`` with ``attributes=None`` reads the existing
        attributes and reuses them.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[(
        "efivar", "-n", "{}-BootOrder".format(_GLOBAL_GUID), "-p",
    )] = _ok(stdout=_EFIVAR_BOOTORDER_OUTPUT)
    config = UefiVariableManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiVariableManager(runner=runner, config=config)

    new_value = b"\x05\x00\x00\x00\x06\x00\x07\x00"
    result = manager.set_variable(
        name="BootOrder",
        value=new_value,
        namespace="EFI_GLOBAL_VARIABLE",
        attributes=None,
    )
    testbase.assert_equal(result.status, UefiVariableActionStatus.SUCCESS)

    write_calls = [c for c in runner.calls if c["args"][:1] == ["efivar"] and "-w" in c["args"]]
    testbase.assert_equal(len(write_calls), 1)
    # The attributes argument must equal the preserved bitmap (0x07).
    expected_attrs = NON_VOLATILE | BOOTSERVICE_ACCESS | RUNTIME_ACCESS
    found = False
    argv = write_calls[0]["args"]
    for idx, token in enumerate(argv):
        if token == "-a" and idx + 1 < len(argv):
            if argv[idx + 1] == str(expected_attrs):
                found = True
                break
    testbase.assert_equal(found, True)
    return


def test_set_variable_new_variable_uses_default_attributes():
    """
        ``set_variable`` with ``attributes=None`` for a new variable
        uses :data:`DEFAULT_ATTRIBUTES`.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[(
        "efivar", "-n", "{}-NewVar".format(_GLOBAL_GUID), "-p",
    )] = _fail(stderr="No such file or directory", exit_code=1)
    config = UefiVariableManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiVariableManager(runner=runner, config=config)

    result = manager.set_variable(
        name="NewVar",
        value=b"\x42",
        namespace="EFI_GLOBAL_VARIABLE",
        attributes=None,
    )
    testbase.assert_equal(result.status, UefiVariableActionStatus.SUCCESS)

    write_calls = [c for c in runner.calls if c["args"][:1] == ["efivar"] and "-w" in c["args"]]
    testbase.assert_equal(len(write_calls), 1)
    argv = write_calls[0]["args"]
    found = False
    for idx, token in enumerate(argv):
        if token == "-a" and idx + 1 < len(argv):
            if argv[idx + 1] == str(DEFAULT_ATTRIBUTES):
                found = True
                break
    testbase.assert_equal(found, True)
    return


def test_set_variable_namespace_rejected_on_linux():
    """
        ``set_variable(namespace="Apple")`` on Linux returns ``FAILED``
        with a stable token without invoking the runner.
    """
    runner = _MockCommandRunner()
    config = UefiVariableManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiVariableManager(runner=runner, config=config)

    result = manager.set_variable(
        name="boot-args",
        value=b"-v",
        namespace="Apple",
    )
    testbase.assert_equal(result.status, UefiVariableActionStatus.FAILED)
    testbase.assert_equal(
        "namespace_rejected_on_host_os_linux" in (result.stderr or ""), True,
    )
    testbase.assert_equal(runner.calls, [])
    return


# ----------------------------------------------------------------------
# delete_variable
# ----------------------------------------------------------------------


def test_delete_variable_linux_chattr_minus_i_then_efivar_d():
    """
        Linux ``delete_variable`` issues ``chattr -i`` then ``efivar
        -D``.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[(
        "efivar", "-n", "{}-BootOrder".format(_GLOBAL_GUID), "-p",
    )] = _ok(stdout=_EFIVAR_BOOTORDER_OUTPUT)
    config = UefiVariableManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiVariableManager(runner=runner, config=config)

    result = manager.delete_variable(
        name="BootOrder", namespace="EFI_GLOBAL_VARIABLE",
    )
    testbase.assert_equal(result.status, UefiVariableActionStatus.SUCCESS)

    chattr_clear = [c for c in runner.calls if c["args"][:2] == ["chattr", "-i"]]
    delete_calls = [c for c in runner.calls if c["args"][:1] == ["efivar"] and "-D" in c["args"]]
    testbase.assert_equal(len(chattr_clear), 1)
    testbase.assert_equal(len(delete_calls), 1)
    testbase.assert_equal(delete_calls[0]["elevated"], True)
    return


def test_delete_variable_darwin_invokes_nvram_d():
    """
        Darwin ``delete_variable`` invokes ``nvram -d
        <ns>:<name>``.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("nvram", "Apple:boot-args")] = _ok(stdout=_NVRAM_BOOT_ARGS_OUTPUT)
    config = UefiVariableManagerConfig(override_host_os="Darwin", elevated=True)
    manager = UefiVariableManager(runner=runner, config=config)

    result = manager.delete_variable(name="boot-args", namespace="Apple")
    testbase.assert_equal(result.status, UefiVariableActionStatus.SUCCESS)
    delete_calls = [c for c in runner.calls if c["args"][:2] == ["nvram", "-d"]]
    testbase.assert_equal(len(delete_calls), 1)
    testbase.assert_equal(delete_calls[0]["args"], ["nvram", "-d", "Apple:boot-args"])
    testbase.assert_equal(delete_calls[0]["elevated"], True)
    return


def test_delete_variable_windows_invokes_powershell_deletevariable():
    """
        Windows ``delete_variable`` invokes ``[UEFI]::DeleteVariable``.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[(
        "powershell", "-NoProfile", "-Command",
        "[UEFI]::GetVariable('{}', 'Timeout')".format(_GLOBAL_GUID),
    )] = _ok(stdout="5, 0\n")
    config = UefiVariableManagerConfig(override_host_os="Windows", elevated=True)
    manager = UefiVariableManager(runner=runner, config=config)

    result = manager.delete_variable(name="Timeout", namespace=_GLOBAL_GUID)
    testbase.assert_equal(result.status, UefiVariableActionStatus.SUCCESS)
    delete_calls = [
        c for c in runner.calls
        if c["args"][0] == "powershell" and "DeleteVariable" in c["args"][-1]
    ]
    testbase.assert_equal(len(delete_calls), 1)
    testbase.assert_equal(delete_calls[0]["elevated"], True)
    return


def test_delete_variable_skipped_when_variable_absent():
    """
        ``delete_variable`` returns ``SKIPPED`` when the read probe
        reports "not found".
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[(
        "efivar", "-n", "{}-Missing".format(_GLOBAL_GUID), "-p",
    )] = _fail(stderr="No such file or directory", exit_code=1)
    config = UefiVariableManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiVariableManager(runner=runner, config=config)

    result = manager.delete_variable(
        name="Missing", namespace="EFI_GLOBAL_VARIABLE",
    )
    testbase.assert_equal(result.status, UefiVariableActionStatus.SKIPPED)
    delete_calls = [c for c in runner.calls if c["args"][:1] == ["efivar"] and "-D" in c["args"]]
    testbase.assert_equal(delete_calls, [])
    return


# ----------------------------------------------------------------------
# Dry-run / elevation
# ----------------------------------------------------------------------


def test_dry_run_short_circuits_set_and_delete():
    """
        Dry-run short-circuits both writes without invoking the runner.
    """
    runner = _MockCommandRunner()
    config = UefiVariableManagerConfig(override_host_os="Linux", dry_run=True)
    manager = UefiVariableManager(runner=runner, config=config)

    set_result = manager.set_variable(
        name="Timeout",
        value=b"\x05\x00",
        namespace="EFI_GLOBAL_VARIABLE",
        attributes=DEFAULT_ATTRIBUTES,
    )
    delete_result = manager.delete_variable(
        name="Timeout", namespace="EFI_GLOBAL_VARIABLE",
    )
    testbase.assert_equal(set_result.status, UefiVariableActionStatus.DRY_RUN)
    testbase.assert_equal(delete_result.status, UefiVariableActionStatus.DRY_RUN)
    testbase.assert_equal(runner.calls, [])
    return


def test_writes_pass_elevated_true_when_configured():
    """
        Writes pass ``elevated=True`` to the runner when configured —
        including the ``chattr`` preparatory steps on Linux.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[(
        "efivar", "-n", "{}-Timeout".format(_GLOBAL_GUID), "-p",
    )] = _fail(stderr="No such file or directory", exit_code=1)
    config = UefiVariableManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiVariableManager(runner=runner, config=config)

    manager.set_variable(
        name="Timeout",
        value=b"\x10\x00",
        namespace="EFI_GLOBAL_VARIABLE",
        attributes=DEFAULT_ATTRIBUTES,
    )
    write_calls = [c for c in runner.calls if c["args"][:2] == ["chattr", "-i"]]
    testbase.assert_equal(len(write_calls) > 0, True)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_reads_never_pass_elevated_true():
    """
        Read operations never pass ``elevated=True`` regardless of
        config.
    """
    runner = _MockCommandRunner()
    runner.results[("efivar", "-l")] = _ok(stdout=_EFIVAR_L_OUTPUT)
    config = UefiVariableManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiVariableManager(runner=runner, config=config)

    manager.list_variables()
    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_populates_exit_code_and_stderr():
    """
        A non-zero exit on a write surfaces as ``FAILED`` with the exit
        code and stderr captured.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "efivar", "-n", "{}-Timeout".format(_GLOBAL_GUID), "-p",
    )] = _fail(stderr="No such file or directory", exit_code=1)
    runner.results[("chattr", "-i", "/sys/firmware/efi/efivars/Timeout-{}".format(_GLOBAL_GUID))] = _ok()
    runner.results[
        ("efivar", "-n", "{}-Timeout".format(_GLOBAL_GUID), "-w", "-a", str(DEFAULT_ATTRIBUTES))
    ] = _fail(stderr="permission denied", exit_code=2)

    config = UefiVariableManagerConfig(override_host_os="Linux", elevated=True)
    manager = UefiVariableManager(runner=runner, config=config)

    result = manager.set_variable(
        name="Timeout",
        value=b"\x10\x00",
        namespace="EFI_GLOBAL_VARIABLE",
        attributes=DEFAULT_ATTRIBUTES,
    )
    testbase.assert_equal(result.status, UefiVariableActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_equal("permission denied" in (result.stderr or ""), True)
    return


# ----------------------------------------------------------------------
# Parser-level tests
# ----------------------------------------------------------------------


def test_parse_efivar_variable_decodes_hex_dump_and_attributes():
    """
        The ``efivar -p`` parser decodes the hex dump and recognises
        the canonical attribute names.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "efivar", "-n", "{}-BootOrder".format(_GLOBAL_GUID), "-p",
    )] = _ok(stdout=_EFIVAR_BOOTORDER_OUTPUT)
    config = UefiVariableManagerConfig(override_host_os="Linux")
    manager = UefiVariableManager(runner=runner, config=config)

    var = manager.get_variable(
        name="BootOrder", namespace="EFI_GLOBAL_VARIABLE",
    )
    testbase.assert_equal(var.value, b"\x01\x00\x00\x00\x02\x00\x2f\x00")
    testbase.assert_equal(
        var.attributes,
        NON_VOLATILE | BOOTSERVICE_ACCESS | RUNTIME_ACCESS,
    )
    return


def test_parse_efivar_l_splits_guid_and_name():
    """
        The ``efivar -l`` parser correctly splits the GUID prefix and
        the variable name.
    """
    runner = _MockCommandRunner()
    runner.results[("efivar", "-l")] = _ok(stdout=_EFIVAR_L_OUTPUT)
    config = UefiVariableManagerConfig(override_host_os="Linux")
    manager = UefiVariableManager(runner=runner, config=config)
    entries = manager.list_variables()
    namespaces = {e.namespace for e in entries}
    testbase.assert_equal(_GLOBAL_GUID in namespaces, True)
    testbase.assert_equal(_DBX_GUID in namespaces, True)
    return


def test_parse_nvram_decodes_percent_escapes():
    """
        The ``nvram`` parser decodes ``%XX`` escape sequences into raw
        bytes.
    """
    runner = _MockCommandRunner()
    runner.results[("nvram", "-p")] = _ok(
        stdout="Common:SystemAudioVolume\t%80\n",
    )
    config = UefiVariableManagerConfig(override_host_os="Darwin")
    manager = UefiVariableManager(runner=runner, config=config)
    entries = manager.list_variables()
    testbase.assert_equal(len(entries), 1)
    testbase.assert_equal(entries[0].value, b"\x80")
    return
