# Requirements

## Introduction

The capsule-update feature provides the `codecraftai-sysman-uefi` package with the ability
to enumerate, apply, and audit UEFI firmware capsule updates on the host the manager is
running on.  This spec implements the capsule-update contract defined at
`codecraftai-sysman-common/specs/contracts/capsule-update/contract.md` for UEFI
firmware management, dispatching to host-OS tooling (`fwupdmgr` on Linux, `softwareupdate`
as a delegation target on macOS, the `PSWindowsUpdate` PowerShell module filtered to
firmware on Windows).

At construction time the `UefiCapsuleUpdateManager` detects the running host OS via
`platform.system()` and dispatches each verb to a host-OS-specific command builder.
Because macOS firmware updates are not exposed as raw capsules — Apple ships them
through `softwareupdate` bundled with OS updates — the write verbs `apply_update` and
`apply_capsule_file` return `UefiCapsuleUpdateActionResult` with status `FAILED` and a
`detail` naming the delegation surface rather than silently succeeding.

A maintenance coordinator running in an external package drives all operations by
calling into this feature's public interfaces.  The feature does not decide *whether* to
apply an update — it provides the capability to *discover and apply* capsule updates
reliably, idempotently, and with full auditability.  All system commands are executed
exclusively through the `codecraftai-interfaces` command-runner abstraction, enabling
dry-run simulation, auditing, and test mocking without requiring a live system.

---

## Requirements

### Requirement 1: List Available Updates

**User Story:** As a maintenance coordinator, I want to enumerate the firmware updates
available for this host, so that I can reason about which updates should be applied.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_available_updates(device_id=None)` on a Linux host
   THE SYSTEM SHALL invoke `fwupdmgr get-updates --json` via the command runner without
   elevation and parse the returned JSON into a list of `UefiCapsuleUpdateInfo` records.
2. WHEN the coordinator calls `list_available_updates(device_id)` on a Linux host with
   a non-`None` `device_id` THE SYSTEM SHALL filter the returned list to entries whose
   `device_id` matches.
3. WHEN the coordinator calls `list_available_updates` on a macOS host THE SYSTEM SHALL
   invoke `softwareupdate --list --include-config-data` via the command runner without
   elevation and parse the firmware-marked entries into `UefiCapsuleUpdateInfo` records.
4. WHEN the coordinator calls `list_available_updates` on a Windows host THE SYSTEM
   SHALL invoke `powershell -NoProfile -Command "Get-WindowsUpdate -MicrosoftUpdate |
   Where-Object { $_.CategoryIDs -contains 'firmware' } | ConvertTo-Json"` via the
   command runner without elevation and parse the JSON array.
5. WHEN the command fails THE SYSTEM SHALL raise `UefiCapsuleUpdateQueryError` carrying
   the exit code and captured stderr.

---

### Requirement 2: Apply a Pending Update

**User Story:** As a maintenance coordinator, I want to apply a pending firmware update
by its backend-assigned identifier, so that the host takes the update at next boot.

#### Acceptance Criteria

1. WHEN the coordinator calls `apply_update(update_id)` on a Linux host THE SYSTEM SHALL
   invoke `fwupdmgr update --no-reboot-check <update_id>` via the command runner with
   `elevated=True` when configured.
2. WHEN the coordinator calls `apply_update(update_id)` on a macOS host THE SYSTEM SHALL
   NOT execute any command and SHALL return `UefiCapsuleUpdateActionResult` with status
   `FAILED` and a `detail` reading `"macOS firmware updates are delivered through
   softwareupdate bundled with OS updates; use 'softwareupdate -i' to apply."`.
3. WHEN the coordinator calls `apply_update(update_id)` on a Windows host THE SYSTEM
   SHALL invoke `powershell -NoProfile -Command "Install-WindowsUpdate -KBArticleID
   <update_id> -AcceptAll -IgnoreReboot"` via the command runner with `elevated=True`
   when configured.
4. WHEN the requested `update_id` is not present in `list_available_updates()` THE
   SYSTEM SHALL skip execution and return `UefiCapsuleUpdateActionResult` with status
   `SKIPPED` and a `detail` explaining that the update is not in the pending list.
5. WHEN the command succeeds THE SYSTEM SHALL return `UefiCapsuleUpdateActionResult`
   with status `SUCCESS`, `requires_reboot=True` on the `applied` field, and the
   `UefiCapsuleUpdateInfo` that was applied.
6. WHEN the command fails THE SYSTEM SHALL return `UefiCapsuleUpdateActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   `UefiCapsuleUpdateActionResult` with status `DRY_RUN`.

---

### Requirement 3: Apply a Capsule File

**User Story:** As a maintenance coordinator, I want to install a pre-downloaded
capsule file, so that air-gapped or vendor-staged updates can be applied without the
update backend downloading them.

#### Acceptance Criteria

1. WHEN the coordinator calls `apply_capsule_file(capsule_path)` on a Linux host THE
   SYSTEM SHALL invoke `fwupdmgr install --allow-reinstall --no-reboot-check
   <capsule_path>` via the command runner with `elevated=True` when configured.
2. WHEN the coordinator calls `apply_capsule_file(capsule_path)` on a macOS host THE
   SYSTEM SHALL NOT execute any command and SHALL return
   `UefiCapsuleUpdateActionResult` with status `FAILED` and a descriptive `detail`.
3. WHEN the coordinator calls `apply_capsule_file(capsule_path)` on a Windows host THE
   SYSTEM SHALL NOT execute any command and SHALL return
   `UefiCapsuleUpdateActionResult` with status `FAILED` and a `detail` explaining that
   Windows does not expose scriptable raw-capsule installation; the operator should use
   the OEM's firmware-update tool or `Install-WindowsUpdate`.
4. WHEN the command succeeds THE SYSTEM SHALL return `UefiCapsuleUpdateActionResult`
   with status `SUCCESS` and `requires_reboot=True`.
5. WHEN the command fails THE SYSTEM SHALL return `UefiCapsuleUpdateActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   `UefiCapsuleUpdateActionResult` with status `DRY_RUN`.

---

### Requirement 4: List Firmware-Updatable Devices

**User Story:** As a maintenance coordinator, I want to enumerate firmware-updatable
devices on this host, so that I can correlate update offerings with the hardware
inventory.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_devices()` on a Linux host THE SYSTEM SHALL invoke
   `fwupdmgr get-devices --json` via the command runner without elevation and parse the
   returned JSON into `UefiDeviceInfo` records.
2. WHEN the coordinator calls `list_devices()` on a macOS host THE SYSTEM SHALL invoke
   `system_profiler -json SPHardwareDataType` via the command runner without elevation
   and parse the hardware identifier + Apple firmware-version into a single
   `UefiDeviceInfo` with `supported=False` — Apple does not expose per-device capsule
   update targets.
3. WHEN the coordinator calls `list_devices()` on a Windows host THE SYSTEM SHALL invoke
   `powershell -NoProfile -Command "Get-WmiObject Win32_ComputerSystem,
   Win32_BIOS | ConvertTo-Json"` via the command runner without elevation and parse the
   result into `UefiDeviceInfo` records.
4. WHEN the command fails THE SYSTEM SHALL raise `UefiCapsuleUpdateQueryError` carrying
   the exit code and captured stderr.

---

### Requirement 5: Read Update History

**User Story:** As a maintenance coordinator, I want to read the firmware-update
history on this host, so that I can audit which capsules were applied when and with
what outcome.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_update_status(limit=None)` on a Linux host THE
   SYSTEM SHALL invoke `fwupdmgr get-history --json` via the command runner without
   elevation and parse the returned JSON into `UefiCapsuleUpdateHistoryEntry` records.
2. WHEN the coordinator calls `get_update_status(limit=N)` with a non-`None` integer
   THE SYSTEM SHALL truncate the returned list to the `N` most recent entries.
3. WHEN the coordinator calls `get_update_status` on a macOS host THE SYSTEM SHALL
   invoke `softwareupdate --history` via the command runner without elevation and
   parse the rows into `UefiCapsuleUpdateHistoryEntry` records.
4. WHEN the coordinator calls `get_update_status` on a Windows host THE SYSTEM SHALL
   invoke `powershell -NoProfile -Command "Get-WUHistory | Where-Object {
   $_.CategoryIDs -contains 'firmware' } | ConvertTo-Json"` via the command runner
   without elevation.
5. WHEN the command fails THE SYSTEM SHALL raise `UefiCapsuleUpdateQueryError`.

---

### Requirement 6: Downgrade Gating

**User Story:** As a maintenance coordinator, I want a safety gate preventing accidental
firmware downgrades, so that a coordinator script cannot blindly rewind a device's
firmware without an explicit flag.

#### Acceptance Criteria

1. WHEN `UefiCapsuleUpdateManagerConfig.allow_downgrade=False` and the requested
   `update_id`'s `available_version` is older (by string comparison) than the device's
   current `current_version` THE SYSTEM SHALL NOT execute any command and SHALL return
   `UefiCapsuleUpdateActionResult` with status `FAILED` and a `detail` reading
   `"Downgrade blocked: <available> < <current>; set allow_downgrade=True to override"`.
2. WHEN `UefiCapsuleUpdateManagerConfig.allow_downgrade=True` THE SYSTEM SHALL proceed
   with the update regardless of version comparison.
3. WHEN the version strings do not compare cleanly (e.g., a vendor-specific version
   format) THE SYSTEM SHALL log a warning through the `PersistenceAspect` audit trail
   and treat the update as non-downgrade.

---

### Requirement 7: Host-OS Dispatch Override

**User Story:** As a test engineer, I want to override the host-OS detection at
construction time, so that I can exercise all three dispatch paths from any developer
workstation without live firmware.

#### Acceptance Criteria

1. WHEN `UefiCapsuleUpdateManagerConfig.override_host_os` is set to one of `"Linux"`,
   `"Darwin"`, `"Windows"` THE SYSTEM SHALL use that value as the dispatch key instead
   of calling `platform.system()`.
2. WHEN `override_host_os` is `None` THE SYSTEM SHALL use `platform.system()` at
   construction time.
3. WHEN `override_host_os` is set to a value that is not one of the three supported
   platforms THE SYSTEM SHALL raise `ValueError` at construction time.

---

### Requirement 8: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all update-apply commands to run
with the privileges required by the underlying tool, so that updates succeed on a
standard host where the process does not run as root / administrator.

#### Acceptance Criteria

1. WHEN the system is configured with `elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the command runner for all write operations (`apply_update`,
   `apply_capsule_file`).
2. WHEN performing read-only operations THE SYSTEM SHALL NOT pass `elevated=True`,
   regardless of config.
3. WHEN running as root / administrator THE SYSTEM SHALL NOT pass `elevated=True`.
4. WHEN the elevation configuration is not set THE SYSTEM SHALL default to no elevation
   and allow the runner to surface the resulting permission error as a
   `UefiCapsuleUpdateActionResult` with `status=FAILED` for writes or as a
   `UefiCapsuleUpdateQueryError` for reads.
