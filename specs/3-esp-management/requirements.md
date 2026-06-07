# Requirements

## Introduction

The esp-management feature provides the `codecraftai-sysman-uefi` package with the ability
to locate, mount, inspect, and modify the EFI System Partition (ESP) on the host the
manager is running on.  This spec implements the esp-management contract defined at
`codecraftai-sysman-common/specs/contracts/esp-management/contract.md` for UEFI
firmware management, dispatching to host-OS tooling (`findmnt`, `lsblk`, `mount` on
Linux; `diskutil` on macOS; `Get-Partition`, `Get-Volume`, `mountvol` on Windows).

At construction time the `UefiEspManager` detects the running host OS via
`platform.system()` and dispatches each verb to a host-OS-specific command builder.
macOS support is limited because Apple manages the ESP automatically and does not
expose all of the file-I/O verbs through scriptable user-space tooling — reads succeed
but writes beyond mount / unmount return `UefiEspActionResult` with status `FAILED` and
a `detail` pointing at the Apple-managed alternative.

The manager combines an `ICommandRunner` (for mount operations and partition discovery)
with an `IFileOperator` (for file I/O on the mounted ESP).  A maintenance coordinator
running in an external package drives all operations by calling into this feature's
public interfaces.

---

## Requirements

### Requirement 1: Locate the ESP

**User Story:** As a maintenance coordinator, I want to discover the device, partition
GUID, and current mountpoint of the ESP, so that subsequent mount and file-I/O verbs
can target it without additional lookups.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_esp_info()` on a Linux host THE SYSTEM SHALL invoke
   `findmnt /boot/efi --json` and `lsblk -f --json` via the command runner without
   elevation, correlate the two outputs, and return a `UefiEspInfo` with
   `detected=True` and all fields populated when an ESP is found.
2. WHEN the coordinator calls `get_esp_info()` on a macOS host THE SYSTEM SHALL invoke
   `diskutil list -plist` via the command runner without elevation, locate the GPT
   partition with type `EFI`, and return a `UefiEspInfo`.
3. WHEN the coordinator calls `get_esp_info()` on a Windows host THE SYSTEM SHALL
   invoke `powershell -NoProfile -Command "Get-Partition | Where-Object { $_.Type -eq
   'System' } | ConvertTo-Json"` and `Get-Volume` via the command runner without
   elevation and return a `UefiEspInfo`.
4. WHEN no ESP is detected THE SYSTEM SHALL return a `UefiEspInfo` with
   `detected=False` and empty string fields rather than raise.
5. WHEN the discovery command fails for reasons other than "no ESP" THE SYSTEM SHALL
   raise `UefiEspQueryError` carrying the exit code and captured stderr.

---

### Requirement 2: Mount the ESP

**User Story:** As a maintenance coordinator, I want to mount the ESP at a chosen
mountpoint, so that I can read and write loader files.

#### Acceptance Criteria

1. WHEN the coordinator calls `mount_esp(mountpoint)` on a Linux host THE SYSTEM SHALL
   first query `get_esp_info()`; when the device is known, invoke `mount -t vfat
   <device> <mountpoint>` via the command runner with `elevated=True` when configured;
   otherwise fall back to `mount <mountpoint>` assuming an `/etc/fstab` entry.
2. WHEN the coordinator calls `mount_esp(mountpoint)` on a macOS host THE SYSTEM SHALL
   invoke `diskutil mount <esp_device>` via the command runner with `elevated=True`
   when configured.
3. WHEN the coordinator calls `mount_esp(mountpoint)` on a Windows host THE SYSTEM
   SHALL interpret `mountpoint` as a drive letter (`"S:"` style) and invoke `mountvol
   <letter>: /S` via the command runner with `elevated=True` when configured.
4. WHEN the ESP is already mounted at the requested mountpoint THE SYSTEM SHALL skip
   execution and return `UefiEspActionResult` with status `SKIPPED`.
5. WHEN the command succeeds THE SYSTEM SHALL return `UefiEspActionResult` with status
   `SUCCESS`.
6. WHEN the command fails THE SYSTEM SHALL return `UefiEspActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   `UefiEspActionResult` with status `DRY_RUN`.

---

### Requirement 3: Unmount the ESP

**User Story:** As a maintenance coordinator, I want to unmount the ESP once file-I/O
work is done, so that the firmware's view of the ESP is not subject to inconsistent
filesystem caches.

#### Acceptance Criteria

1. WHEN the coordinator calls `unmount_esp(mountpoint)` on a Linux host THE SYSTEM
   SHALL invoke `umount <mountpoint>` via the command runner with `elevated=True` when
   configured.
2. WHEN the coordinator calls `unmount_esp(mountpoint)` on a macOS host THE SYSTEM
   SHALL invoke `diskutil unmount <mountpoint>` via the command runner with
   `elevated=True` when configured.
3. WHEN the coordinator calls `unmount_esp(mountpoint)` on a Windows host THE SYSTEM
   SHALL interpret `mountpoint` as a drive letter and invoke `mountvol <letter>: /D`
   via the command runner with `elevated=True` when configured.
4. WHEN the mountpoint is not currently an ESP mount THE SYSTEM SHALL skip execution
   and return `UefiEspActionResult` with status `SKIPPED`.
5. WHEN the command succeeds THE SYSTEM SHALL return `UefiEspActionResult` with status
   `SUCCESS`.
6. WHEN the command fails THE SYSTEM SHALL return `UefiEspActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   `UefiEspActionResult` with status `DRY_RUN`.

---

### Requirement 4: Is ESP Mounted

**User Story:** As a maintenance coordinator, I want a simple boolean query of whether
the ESP is currently mounted, so that I can gate file-I/O verbs without parsing mount
tables myself.

#### Acceptance Criteria

1. WHEN the coordinator calls `is_esp_mounted()` on a Linux host THE SYSTEM SHALL
   invoke `findmnt /boot/efi` and return `True` when the exit code is zero.
2. WHEN the coordinator calls `is_esp_mounted()` on a macOS host THE SYSTEM SHALL
   invoke `diskutil info <esp_device>` and parse the `Mounted:` line.
3. WHEN the coordinator calls `is_esp_mounted()` on a Windows host THE SYSTEM SHALL
   infer mount state from the presence of a drive letter in the `UefiEspInfo` returned
   by `get_esp_info()`.
4. WHEN the command fails for reasons other than "not mounted" THE SYSTEM SHALL raise
   `UefiEspQueryError`.

---

### Requirement 5: List ESP Contents

**User Story:** As a maintenance coordinator, I want to list files at a path inside the
mounted ESP, so that I can audit which loaders are present.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_esp_contents(path)` and the ESP is mounted THE
   SYSTEM SHALL delegate to `IFileOperator.list_directory(<esp_mountpoint>/<path>)`.
2. WHEN the ESP is not mounted THE SYSTEM SHALL raise `UefiEspNotMountedError` without
   invoking the file operator.
3. WHEN the underlying file-operator call fails for reasons other than "not mounted"
   THE SYSTEM SHALL raise `UefiEspQueryError` carrying the file-operator error
   message.

---

### Requirement 6: Read ESP File

**User Story:** As a maintenance coordinator, I want to read a file on the mounted ESP,
so that I can inspect a loader's contents before replacing it.

#### Acceptance Criteria

1. WHEN the coordinator calls `read_esp_file(path)` and the ESP is mounted THE SYSTEM
   SHALL delegate to `IFileOperator.read_bytes(<esp_mountpoint>/<path>)`.
2. WHEN the ESP is not mounted THE SYSTEM SHALL raise `UefiEspNotMountedError` without
   invoking the file operator.
3. WHEN the path does not exist THE SYSTEM SHALL raise `UefiEspQueryError` with a
   `detail` naming the missing path.
4. WHEN the file-operator read fails THE SYSTEM SHALL raise `UefiEspQueryError`.

---

### Requirement 7: Write ESP File

**User Story:** As a maintenance coordinator, I want to write a file on the mounted
ESP, so that I can stage a new loader or a shim configuration.

#### Acceptance Criteria

1. WHEN the coordinator calls `write_esp_file(path, content)` and the ESP is mounted
   THE SYSTEM SHALL delegate to `IFileOperator.write_bytes(<esp_mountpoint>/<path>,
   content)` with `elevated=True` when configured.
2. WHEN the ESP is not mounted THE SYSTEM SHALL return `UefiEspActionResult` with
   status `FAILED` and a `detail` reading `"ESP is not mounted"` without invoking the
   file operator.
3. WHEN the existing file (if any) is byte-identical to `content` THE SYSTEM SHALL skip
   execution and return `UefiEspActionResult` with status `SKIPPED`.
4. WHEN the host OS is macOS THE SYSTEM SHALL delegate to `IFileOperator.write_bytes`
   the same way as Linux and Windows — no host-OS override is required for ESP file
   I/O once the ESP is mounted.
5. WHEN the command succeeds THE SYSTEM SHALL return `UefiEspActionResult` with status
   `SUCCESS`.
6. WHEN the file-operator write fails THE SYSTEM SHALL return `UefiEspActionResult`
   with status `FAILED` and a `detail` carrying the file-operator error message.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT invoke the file operator and SHALL
   return `UefiEspActionResult` with status `DRY_RUN`.

---

### Requirement 8: Delete ESP File

**User Story:** As a maintenance coordinator, I want to delete a file on the mounted
ESP, so that stale or obsolete loaders are removed.

#### Acceptance Criteria

1. WHEN the coordinator calls `delete_esp_file(path)` and the ESP is mounted THE SYSTEM
   SHALL delegate to `IFileOperator.delete(<esp_mountpoint>/<path>)` with
   `elevated=True` when configured.
2. WHEN the ESP is not mounted THE SYSTEM SHALL return `UefiEspActionResult` with
   status `FAILED`.
3. WHEN the path does not exist THE SYSTEM SHALL skip execution and return
   `UefiEspActionResult` with status `SKIPPED`.
4. WHEN the command succeeds THE SYSTEM SHALL return `UefiEspActionResult` with status
   `SUCCESS`.
5. WHEN the file-operator delete fails THE SYSTEM SHALL return `UefiEspActionResult`
   with status `FAILED`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT invoke the file operator and SHALL
   return `UefiEspActionResult` with status `DRY_RUN`.

---

### Requirement 9: Host-OS Dispatch Override

**User Story:** As a test engineer, I want to override the host-OS detection at
construction time, so that I can exercise all three dispatch paths from any developer
workstation without live firmware.

#### Acceptance Criteria

1. WHEN `UefiEspManagerConfig.override_host_os` is set to one of `"Linux"`, `"Darwin"`,
   `"Windows"` THE SYSTEM SHALL use that value as the dispatch key instead of calling
   `platform.system()`.
2. WHEN `override_host_os` is `None` THE SYSTEM SHALL use `platform.system()` at
   construction time.
3. WHEN `override_host_os` is set to a value that is not one of the three supported
   platforms THE SYSTEM SHALL raise `ValueError` at construction time.

---

### Requirement 10: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all mount, unmount, and file-I/O
write commands to run with the privileges required by the underlying tool, so that the
commands succeed on a standard host where the process does not run as root /
administrator.

#### Acceptance Criteria

1. WHEN the system is configured with `elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the command runner for all write operations (`mount_esp`,
   `unmount_esp`) and to the file operator for all file-I/O writes (`write_esp_file`,
   `delete_esp_file`).
2. WHEN performing read-only operations (`get_esp_info`, `is_esp_mounted`,
   `list_esp_contents`, `read_esp_file`) THE SYSTEM SHALL NOT pass `elevated=True`,
   regardless of config.
3. WHEN running as root / administrator THE SYSTEM SHALL NOT pass `elevated=True`.
4. WHEN the elevation configuration is not set THE SYSTEM SHALL default to no elevation
   and allow the runner / file operator to surface the resulting permission error as a
   `UefiEspActionResult` with `status=FAILED` for writes or as a `UefiEspQueryError`
   for reads.
