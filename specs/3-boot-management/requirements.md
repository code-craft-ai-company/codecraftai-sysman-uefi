# Requirements

## Introduction

The boot-management feature provides the `codecraftai-sysman-uefi` package with the ability
to manage UEFI firmware boot entries and the persistent boot order on the host the
manager is running on.  This spec implements the boot-management contract defined at
`codecraftai-sysman-common/specs/contracts/boot-management/contract.md` for UEFI
firmware management, dispatching to host-OS tooling (`efibootmgr` on Linux, `bless` and
`nvram` on macOS, `bcdedit` on Windows).

At construction time the `UefiBootManager` detects the running host OS via
`platform.system()` and dispatches each verb to a host-OS-specific command builder.  On
host OSes where a verb has no scriptable equivalent (for example, `set_boot_order` on
macOS, where Apple's `bless` manages a single boot device rather than an ordered list),
the method returns `UefiBootActionResult` with status `FAILED` and a `detail` explaining
the gap rather than silently no-oping.

A maintenance coordinator running in an external package drives all operations by calling
into this feature's public interfaces.  The feature does not decide *which* firmware
entries to create or which boot order to use — it provides the capability to *do it*
reliably, idempotently, and with full auditability.  All system commands are executed
exclusively through the `codecraftai-interfaces` command-runner abstraction, enabling
dry-run simulation, auditing, and test mocking without requiring a live system or live
firmware access.

---

## Requirements

### Requirement 1: List Boot Entries

**User Story:** As a maintenance coordinator, I want to enumerate the firmware boot
entries on a host, so that I can reason about what loaders the firmware knows about
before making changes.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_boot_entries()` on a Linux host THE SYSTEM SHALL
   invoke `efibootmgr -v` via the command runner without elevation and parse the output
   into a list of `UefiBootEntryInfo` records.
2. WHEN the coordinator calls `list_boot_entries()` on a macOS host THE SYSTEM SHALL
   invoke `bless --info --verbose` via the command runner without elevation, augment it
   with `nvram -p`, and parse the combined output into `UefiBootEntryInfo` records.
3. WHEN the coordinator calls `list_boot_entries()` on a Windows host THE SYSTEM SHALL
   invoke `bcdedit /enum firmware` via the command runner without elevation and parse
   the output into `UefiBootEntryInfo` records keyed by GUID.
4. WHEN the underlying command fails with a non-zero exit code THE SYSTEM SHALL raise
   `UefiBootQueryError` carrying the exit code and captured stderr.
5. WHEN the host OS is not one of `Linux`, `Darwin`, `Windows` THE SYSTEM SHALL raise
   `UefiBootQueryError` with a `detail` naming the detected platform.

---

### Requirement 2: Get a Single Boot Entry

**User Story:** As a maintenance coordinator, I want to retrieve a single firmware boot
entry by its identifier, so that I can inspect its current label, loader, and
active-flag state.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_boot_entry(entry_id)` THE SYSTEM SHALL internally call
   `list_boot_entries()` and return the `UefiBootEntryInfo` whose `entry_id` matches.
2. WHEN no entry matches THE SYSTEM SHALL raise `UefiBootQueryError` with a `detail`
   naming the missing entry.
3. WHEN the underlying enumeration fails THE SYSTEM SHALL raise `UefiBootQueryError`
   with the exit code and stderr.

---

### Requirement 3: Create a Boot Entry

**User Story:** As a maintenance coordinator, I want to create a firmware boot entry
pointing at a loader on the ESP, so that the loader can be selected at firmware boot.

#### Acceptance Criteria

1. WHEN the coordinator calls `create_boot_entry(spec)` on a Linux host THE SYSTEM SHALL
   invoke `efibootmgr -c -d <device> -p <partition> -L <label> -l <loader_path>` via the
   command runner with `elevated=True` when configured.
2. WHEN the coordinator calls `create_boot_entry(spec)` on a macOS host THE SYSTEM SHALL
   invoke `bless --mount <mountpoint> --setBoot` via the command runner with the
   coordinator-supplied ESP mountpoint, noting that macOS does not accept a distinct
   loader path in the same way Linux does; this is documented as a gap.
3. WHEN the coordinator calls `create_boot_entry(spec)` on a Windows host THE SYSTEM
   SHALL invoke `bcdedit /create /d <description> /application BOOTSECTOR` followed by
   `bcdedit /set {guid} device partition=<letter>:` and `bcdedit /set {guid} path
   <loader_path>` via the command runner with `elevated=True` when configured.
4. WHEN an existing entry has the same `label` and `loader_path` THE SYSTEM SHALL skip
   execution and return `UefiBootActionResult` with status `SKIPPED`.
5. WHEN the command succeeds THE SYSTEM SHALL return `UefiBootActionResult` with status
   `SUCCESS` and the newly assigned `entry_id` populated.
6. WHEN the command fails THE SYSTEM SHALL return `UefiBootActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   `UefiBootActionResult` with status `DRY_RUN`.

---

### Requirement 4: Delete a Boot Entry

**User Story:** As a maintenance coordinator, I want to delete a firmware boot entry, so
that stale or obsolete loaders are no longer bootable.

#### Acceptance Criteria

1. WHEN the coordinator calls `delete_boot_entry(entry_id)` on a Linux host THE SYSTEM
   SHALL invoke `efibootmgr -b <entry_id> -B` via the command runner with
   `elevated=True` when configured.
2. WHEN the coordinator calls `delete_boot_entry(entry_id)` on a macOS host THE SYSTEM
   SHALL NOT execute any command and SHALL return `UefiBootActionResult` with status
   `FAILED` and a `detail` explaining that macOS manages EFI entries via `bless` and
   does not expose scriptable deletion of a specific entry ID.
3. WHEN the coordinator calls `delete_boot_entry(entry_id)` on a Windows host THE SYSTEM
   SHALL invoke `bcdedit /delete {entry_id}` via the command runner with `elevated=True`
   when configured.
4. WHEN the entry does not exist in the current enumeration THE SYSTEM SHALL skip
   execution and return `UefiBootActionResult` with status `SKIPPED`.
5. WHEN the command succeeds THE SYSTEM SHALL return `UefiBootActionResult` with status
   `SUCCESS`.
6. WHEN the command fails THE SYSTEM SHALL return `UefiBootActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   `UefiBootActionResult` with status `DRY_RUN`.

---

### Requirement 5: Get the Boot Order

**User Story:** As a maintenance coordinator, I want to read the persistent boot order,
so that I can confirm the firmware's preference sequence before altering it.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_boot_order()` on a Linux host THE SYSTEM SHALL invoke
   `efibootmgr` via the command runner and parse the `BootOrder:` line into a list of
   4-hex identifiers.
2. WHEN the coordinator calls `get_boot_order()` on a macOS host THE SYSTEM SHALL invoke
   `nvram efi-boot-device` via the command runner and parse the single-entry result into
   a one-element list of opaque strings.
3. WHEN the coordinator calls `get_boot_order()` on a Windows host THE SYSTEM SHALL
   invoke `bcdedit /enum {fwbootmgr}` via the command runner and parse the
   `displayorder` line into a list of GUIDs.
4. WHEN the command fails THE SYSTEM SHALL raise `UefiBootQueryError` carrying the exit
   code and stderr.

---

### Requirement 6: Set the Boot Order

**User Story:** As a maintenance coordinator, I want to write a new persistent boot
order, so that the firmware tries loaders in a specific sequence.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_boot_order(order)` on a Linux host THE SYSTEM SHALL
   invoke `efibootmgr -o <xxxx,yyyy,zzzz>` via the command runner with `elevated=True`
   when configured.
2. WHEN the coordinator calls `set_boot_order(order)` on a macOS host THE SYSTEM SHALL
   NOT execute any command and SHALL return `UefiBootActionResult` with status `FAILED`
   and a `detail` explaining that macOS's `bless --setBoot` selects a single boot device
   rather than an ordered list.
3. WHEN the coordinator calls `set_boot_order(order)` on a Windows host THE SYSTEM SHALL
   invoke `bcdedit /set {fwbootmgr} displayorder <guid1> <guid2> ...` via the command
   runner with `elevated=True` when configured.
4. WHEN the current boot order already equals the requested order THE SYSTEM SHALL skip
   execution and return `UefiBootActionResult` with status `SKIPPED`.
5. WHEN the command succeeds THE SYSTEM SHALL return `UefiBootActionResult` with status
   `SUCCESS`.
6. WHEN the command fails THE SYSTEM SHALL return `UefiBootActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   `UefiBootActionResult` with status `DRY_RUN`.

---

### Requirement 7: Get BootNext

**User Story:** As a maintenance coordinator, I want to read the one-shot `BootNext`
value, so that I can tell whether a one-time override is in place.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_boot_next()` on a Linux host THE SYSTEM SHALL invoke
   `efibootmgr` via the command runner and parse the `BootNext:` line into a single
   4-hex identifier, or return `None` when no such line is present.
2. WHEN the coordinator calls `get_boot_next()` on a macOS host THE SYSTEM SHALL return
   `None` — macOS does not expose a scriptable `BootNext` equivalent — after invoking
   no command.
3. WHEN the coordinator calls `get_boot_next()` on a Windows host THE SYSTEM SHALL
   invoke `bcdedit /enum {fwbootmgr}` via the command runner and parse the
   `bootsequence` line into a single GUID, or return `None` when absent.
4. WHEN the underlying command fails THE SYSTEM SHALL raise `UefiBootQueryError`.

---

### Requirement 8: Set BootNext

**User Story:** As a maintenance coordinator, I want to request a one-shot override of
the boot order, so that the next boot selects a specific entry.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_boot_next(entry_id)` on a Linux host THE SYSTEM SHALL
   invoke `efibootmgr -n <entry_id>` via the command runner with `elevated=True` when
   configured.
2. WHEN the coordinator calls `set_boot_next(entry_id)` on a macOS host THE SYSTEM SHALL
   NOT execute any command and SHALL return `UefiBootActionResult` with status `FAILED`
   and a `detail` explaining that macOS does not expose `BootNext` through
   user-scriptable tooling.
3. WHEN the coordinator calls `set_boot_next(entry_id)` on a Windows host THE SYSTEM
   SHALL invoke `bcdedit /set {fwbootmgr} bootsequence {entry_id}` via the command
   runner with `elevated=True` when configured.
4. WHEN `BootNext` is already set to `entry_id` THE SYSTEM SHALL skip execution and
   return `UefiBootActionResult` with status `SKIPPED`.
5. WHEN the command succeeds THE SYSTEM SHALL return `UefiBootActionResult` with status
   `SUCCESS`.
6. WHEN the command fails THE SYSTEM SHALL return `UefiBootActionResult` with status
   `FAILED`, the exit code, and the captured stderr.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   `UefiBootActionResult` with status `DRY_RUN`.

---

### Requirement 9: Get BootCurrent

**User Story:** As a maintenance coordinator, I want to know which boot entry was used
for the current boot, so that I can correlate runtime state with firmware intent.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_boot_current()` on a Linux host THE SYSTEM SHALL
   invoke `efibootmgr` via the command runner and parse the `BootCurrent:` line into a
   single 4-hex identifier.
2. WHEN the coordinator calls `get_boot_current()` on a macOS host THE SYSTEM SHALL
   invoke `nvram efi-boot-device` via the command runner and approximate `BootCurrent`
   from the running boot device identifier.
3. WHEN the coordinator calls `get_boot_current()` on a Windows host THE SYSTEM SHALL
   invoke `bcdedit /enum {current}` via the command runner and parse the identifier
   from the output.
4. WHEN the command fails THE SYSTEM SHALL raise `UefiBootQueryError` carrying the exit
   code and stderr.

---

### Requirement 10: Host-OS Dispatch Override

**User Story:** As a test engineer, I want to override the host-OS detection at
construction time, so that I can exercise all three dispatch paths from any developer
workstation without live firmware.

#### Acceptance Criteria

1. WHEN `UefiBootManagerConfig.override_host_os` is set to one of `"Linux"`, `"Darwin"`,
   `"Windows"` THE SYSTEM SHALL use that value as the dispatch key instead of calling
   `platform.system()`.
2. WHEN `override_host_os` is `None` THE SYSTEM SHALL use `platform.system()` at
   construction time.
3. WHEN `override_host_os` is set to a value that is not one of the three supported
   platforms THE SYSTEM SHALL raise `ValueError` at construction time with a `detail`
   listing the accepted values.

---

### Requirement 11: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all firmware-variable write
commands to run with the privileges required by the underlying tool, so that changes
succeed on a standard host where the process does not run as root / administrator.

#### Acceptance Criteria

1. WHEN the system is configured with `elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the command runner for all write operations (create, delete, set
   order, set next).
2. WHEN performing read-only operations THE SYSTEM SHALL NOT pass `elevated=True`,
   regardless of config.
3. WHEN running as root / administrator THE SYSTEM SHALL NOT pass `elevated=True`.
4. WHEN the elevation configuration is not set THE SYSTEM SHALL default to no elevation
   and allow the runner to surface the resulting permission error as a
   `UefiBootActionResult` with `status=FAILED` for writes or as a `UefiBootQueryError`
   for reads.
