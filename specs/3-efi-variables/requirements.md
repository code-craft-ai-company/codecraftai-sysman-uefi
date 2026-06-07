# Requirements

## Introduction

The efi-variables feature provides the `codecraftai-sysman-uefi` package with the ability
to read, write, delete, and enumerate raw EFI NVRAM variables on the host the manager is
running on.  This spec implements the efi-variables contract defined at
`codecraftai-sysman-common/specs/contracts/efi-variables/contract.md` for UEFI firmware
management, dispatching to host-OS tooling (`efivar` and the `/sys/firmware/efi/efivars`
filesystem on Linux, `nvram` on macOS, the PowerShell `Uefi` module on Windows).

At construction time the `UefiVariableManager` detects the running host OS via
`platform.system()` and dispatches each verb to a host-OS-specific command builder.
Namespace identifiers use the UEFI GUID convention (e.g., the well-known
`8be4df61-93ca-11d2-aa0d-00e098032b8c` for `EFI_GLOBAL_VARIABLE`), with a registry of
friendly aliases (`EFI_GLOBAL_VARIABLE`, `Apple`, `Common`) recognised by the namespace
normaliser.

A maintenance coordinator running in an external package drives all operations by
calling into this feature's public interfaces.  The feature does not decide *which*
variables to set — it provides the capability to *read and write* them reliably,
idempotently, and with full auditability.  All system commands are executed exclusively
through the `codecraftai-interfaces` command-runner abstraction, enabling dry-run
simulation, auditing, and test mocking without requiring a live system.

---

## Requirements

### Requirement 1: Read a Variable

**User Story:** As a maintenance coordinator, I want to read an EFI NVRAM variable's
current value bytes and attribute bitmap, so that I can branch on or compare against
the coordinator's desired state.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_variable(name, namespace)` on a Linux host THE SYSTEM
   SHALL invoke `efivar -n <namespace>-<name> -p` via the command runner without
   elevation and parse the hex-encoded value into bytes, or read the content at
   `/sys/firmware/efi/efivars/<name>-<namespace>` where permissions allow.
2. WHEN the coordinator calls `get_variable(name, namespace)` on a macOS host THE SYSTEM
   SHALL invoke `nvram <namespace>:<name>` via the command runner without elevation
   and parse the returned value.
3. WHEN the coordinator calls `get_variable(name, namespace)` on a Windows host THE
   SYSTEM SHALL invoke `powershell -NoProfile -Command "[UEFI]::GetVariable('<namespace>',
   '<name>')"` via the command runner without elevation and parse the returned byte
   array.
4. WHEN the variable exists THE SYSTEM SHALL return a `UefiVariable` record with
   `namespace`, `name`, `value`, `attributes`, and `size_bytes` populated.
5. WHEN the variable does not exist THE SYSTEM SHALL raise
   `UefiVariableNotFoundError`.
6. WHEN the namespace is rejected by the host OS (e.g., an Apple-reserved alias on
   Linux) THE SYSTEM SHALL raise `UefiVariableNamespaceError` without invoking any
   command.

---

### Requirement 2: Write a Variable

**User Story:** As a maintenance coordinator, I want to write an EFI NVRAM variable's
value bytes and optionally its attribute bitmap, so that the firmware observes the
desired setting on next boot.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_variable(name, value, namespace, attributes)` on a
   Linux host THE SYSTEM SHALL invoke `efivar -n <namespace>-<name> -w -f <binary>` via
   the command runner with `elevated=True` when configured, where `<binary>` is a
   temporary file containing the raw value bytes.
2. WHEN the coordinator calls `set_variable(name, value, namespace, attributes)` on a
   macOS host THE SYSTEM SHALL invoke `nvram <namespace>:<name>=<encoded_value>` via the
   command runner with `elevated=True` when configured.
3. WHEN the coordinator calls `set_variable(name, value, namespace, attributes)` on a
   Windows host THE SYSTEM SHALL invoke `powershell -NoProfile -Command
   "[UEFI]::SetVariable('<namespace>', '<name>', [byte[]]@(...), <attributes>)"` via
   the command runner with `elevated=True` when configured.
4. WHEN the current value and attributes equal the requested value and attributes THE
   SYSTEM SHALL skip execution and return `UefiVariableActionResult` with status
   `SKIPPED`.
5. WHEN the variable already exists with the efivars-immutable flag on Linux THE SYSTEM
   SHALL invoke `chattr -i <efivars_path>` before the write, performing the write, and
   then re-set the immutable flag only on `SUCCESS`.
6. WHEN the caller does not pass an explicit `attributes` bitmap and the variable
   already exists THE SYSTEM SHALL preserve the current variable's attributes.
7. WHEN the command succeeds THE SYSTEM SHALL return `UefiVariableActionResult` with
   status `SUCCESS`.
8. WHEN the command fails THE SYSTEM SHALL return `UefiVariableActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
9. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   `UefiVariableActionResult` with status `DRY_RUN`.
10. WHEN the namespace is rejected by the host OS THE SYSTEM SHALL return
    `UefiVariableActionResult` with status `FAILED` and a `UefiVariableNamespaceError`
    message in `detail`, without invoking any command.

---

### Requirement 3: Delete a Variable

**User Story:** As a maintenance coordinator, I want to remove an EFI NVRAM variable,
so that a previously written firmware setting is no longer present.

#### Acceptance Criteria

1. WHEN the coordinator calls `delete_variable(name, namespace)` on a Linux host THE
   SYSTEM SHALL invoke `efivar -n <namespace>-<name> -D` via the command runner with
   `elevated=True` when configured, after first clearing the efivars-immutable flag via
   `chattr -i <efivars_path>`.
2. WHEN the coordinator calls `delete_variable(name, namespace)` on a macOS host THE
   SYSTEM SHALL invoke `nvram -d <namespace>:<name>` via the command runner with
   `elevated=True` when configured.
3. WHEN the coordinator calls `delete_variable(name, namespace)` on a Windows host THE
   SYSTEM SHALL invoke `powershell -NoProfile -Command "[UEFI]::DeleteVariable('<namespace>',
   '<name>')"` via the command runner with `elevated=True` when configured.
4. WHEN the variable does not exist THE SYSTEM SHALL skip execution and return
   `UefiVariableActionResult` with status `SKIPPED`.
5. WHEN the command succeeds THE SYSTEM SHALL return `UefiVariableActionResult` with
   status `SUCCESS`.
6. WHEN the command fails THE SYSTEM SHALL return `UefiVariableActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   `UefiVariableActionResult` with status `DRY_RUN`.

---

### Requirement 4: List Variables

**User Story:** As a maintenance coordinator, I want to enumerate variables in a
namespace (or across all namespaces), so that I can audit or discover firmware settings
without knowing their names in advance.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_variables(namespace=None)` on a Linux host THE
   SYSTEM SHALL invoke `efivar -l` via the command runner without elevation and parse
   every `<name>-<namespace>` entry into `UefiVariable` records whose `value` is empty
   until a follow-up `get_variable` is issued.
2. WHEN the coordinator calls `list_variables(namespace=<ns>)` on a Linux host THE
   SYSTEM SHALL invoke `efivar -l` and filter the results to entries whose namespace
   matches.
3. WHEN the coordinator calls `list_variables` on a macOS host THE SYSTEM SHALL invoke
   `nvram -p` via the command runner without elevation and parse every line as a
   `<namespace>:<name>=<value>` tuple.
4. WHEN the coordinator calls `list_variables` on a Windows host THE SYSTEM SHALL
   invoke `powershell -NoProfile -Command "[UEFI]::EnumerateVariables()"` via the
   command runner without elevation and parse the emitted array into `UefiVariable`
   records.
5. WHEN the command fails THE SYSTEM SHALL raise `UefiVariableQueryError` carrying the
   exit code and captured stderr.

---

### Requirement 5: Namespace Normalisation

**User Story:** As a maintenance coordinator, I want a single namespace parameter that
accepts either the canonical GUID or a friendly alias, so that cross-OS code does not
have to hardcode different namespace forms per host.

#### Acceptance Criteria

1. WHEN the coordinator passes `namespace="EFI_GLOBAL_VARIABLE"` THE SYSTEM SHALL
   normalise it to `"8be4df61-93ca-11d2-aa0d-00e098032b8c"` before constructing the
   host-OS command.
2. WHEN the coordinator passes a full GUID string THE SYSTEM SHALL accept it verbatim
   (lower-cased) and pass it through to the host-OS command.
3. WHEN the coordinator passes an alias that is reserved on the current host OS (for
   example `"Apple"` on Linux) THE SYSTEM SHALL raise `UefiVariableNamespaceError`
   without invoking any command.
4. WHEN the coordinator passes a non-GUID, non-alias string THE SYSTEM SHALL raise
   `ValueError` with a `detail` listing the accepted alias set.

---

### Requirement 6: Host-OS Dispatch Override

**User Story:** As a test engineer, I want to override the host-OS detection at
construction time, so that I can exercise all three dispatch paths from any developer
workstation without live firmware.

#### Acceptance Criteria

1. WHEN `UefiVariableManagerConfig.override_host_os` is set to one of `"Linux"`,
   `"Darwin"`, `"Windows"` THE SYSTEM SHALL use that value as the dispatch key instead
   of calling `platform.system()`.
2. WHEN `override_host_os` is `None` THE SYSTEM SHALL use `platform.system()` at
   construction time.
3. WHEN `override_host_os` is set to a value that is not one of the three supported
   platforms THE SYSTEM SHALL raise `ValueError` at construction time.

---

### Requirement 7: Attribute Preservation

**User Story:** As a maintenance coordinator, I want a `set_variable` that updates only
the value bytes by default (preserving the current attribute bitmap), so that a generic
"update value" workflow does not inadvertently weaken a variable's non-volatile or
runtime-access flags.

#### Acceptance Criteria

1. WHEN the caller passes `attributes=None` and the variable already exists THE SYSTEM
   SHALL first issue an internal `get_variable` call, read the current `attributes`,
   and pass that bitmap through to the underlying tool's write.
2. WHEN the caller passes an explicit `attributes` bitmap THE SYSTEM SHALL pass it
   through verbatim, overriding any existing bitmap.
3. WHEN the variable does not yet exist and `attributes=None` THE SYSTEM SHALL default
   to `0x07` (NON_VOLATILE | BOOTSERVICE_ACCESS | RUNTIME_ACCESS) — the most common
   bitmap for a new persistent variable.
4. WHEN the internal `get_variable` required by preservation fails for a reason other
   than "variable does not exist" THE SYSTEM SHALL return
   `UefiVariableActionResult` with status `FAILED` and NOT fall back to the default
   bitmap.

---

### Requirement 8: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all variable write commands to run
with the privileges required by the underlying tool, so that writes succeed on a
standard host where the process does not run as root / administrator.

#### Acceptance Criteria

1. WHEN the system is configured with `elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the command runner for all write operations (`set_variable`,
   `delete_variable`), including the `chattr -i` preparatory call on Linux.
2. WHEN performing read-only operations THE SYSTEM SHALL NOT pass `elevated=True`,
   regardless of config.
3. WHEN running as root / administrator THE SYSTEM SHALL NOT pass `elevated=True`.
4. WHEN the elevation configuration is not set THE SYSTEM SHALL default to no elevation
   and allow the runner to surface the resulting permission error as a
   `UefiVariableActionResult` with `status=FAILED` for writes or as a
   `UefiVariableQueryError` / `UefiVariableNotFoundError` for reads.
