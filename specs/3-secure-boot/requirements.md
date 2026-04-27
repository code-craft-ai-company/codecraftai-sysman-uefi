# Requirements

## Introduction

The secure-boot feature provides the `automojo-sysman-uefi` package with the ability to
inspect Secure Boot state and policy, and — on host OSes where user-space tooling
exposes it — enroll and reset Machine-Owner Keys (MOKs).  This spec implements the
secure-boot contract defined at
`automojo-sysman-common/specs/contracts/secure-boot/contract.md` for UEFI firmware
management, dispatching to host-OS tooling (`mokutil` and the `efivars` filesystem on
Linux, `bputil` and `csrutil` on macOS, `Confirm-SecureBootUEFI` and
`Get-SecureBootPolicy` on Windows).

At construction time the `UefiSecureBootManager` detects the running host OS via
`platform.system()` and dispatches each verb to a host-OS-specific command builder.
Because Apple's Secure Boot model on Apple Silicon and Microsoft's PK / KEK / db
lifecycle on Windows both use different key-management surfaces than the standard UEFI
`PK` / `KEK` / `db` / `dbx` / MOK model, the write verbs `enroll_mok_key` and
`reset_mok` are Linux-only; on macOS and Windows they return `UefiSecureBootActionResult`
with status `FAILED` and a descriptive `detail`.  `set_secure_boot_mode` is always
`FAILED` on every host OS because Secure Boot is a firmware-setup-only toggle.

A maintenance coordinator running in an external package drives all operations by
calling into this feature's public interfaces.  The feature does not decide *which* keys
to enroll or *whether* Secure Boot should be on — it provides the capability to
*inspect and adjust* reliably, idempotently, and with full auditability.  All system
commands are executed exclusively through the `automojo-interfaces` command-runner
abstraction, enabling dry-run simulation, auditing, and test mocking without requiring a
live system.

---

## Requirements

### Requirement 1: Query Secure Boot State

**User Story:** As a maintenance coordinator, I want to know whether Secure Boot is
currently enforcing on this host, so that I can decide whether to enroll an additional
MOK, skip the step, or escalate the host's posture.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_secure_boot_state()` on a Linux host THE SYSTEM SHALL
   invoke `mokutil --sb-state` via the command runner without elevation and, as a
   secondary source, read the content at
   `/sys/firmware/efi/efivars/SecureBoot-<guid>` via `efivar -n <guid>-SecureBoot -p`.
2. WHEN the coordinator calls `get_secure_boot_state()` on a macOS host THE SYSTEM SHALL
   invoke `csrutil status` and, on Apple Silicon, `bputil --display` via the command
   runner without elevation, and report the resulting policy as a
   `UefiSecureBootState`.
3. WHEN the coordinator calls `get_secure_boot_state()` on a Windows host THE SYSTEM
   SHALL invoke `powershell -NoProfile -Command Confirm-SecureBootUEFI` via the command
   runner without elevation and parse the boolean output.
4. WHEN the command succeeds THE SYSTEM SHALL return a `UefiSecureBootState` populated
   with `enabled`, `supported`, `setup_mode`, `audit_mode`, `deployed_mode`,
   `vendor_keys`, and `policy_description`.
5. WHEN the firmware does not expose Secure Boot at all THE SYSTEM SHALL return
   `UefiSecureBootState(enabled=False, supported=False, ...)` rather than raise.
6. WHEN the command fails for reasons other than "Secure Boot not supported" THE SYSTEM
   SHALL raise `UefiSecureBootQueryError`.

---

### Requirement 2: Query Secure Boot Policy

**User Story:** As a maintenance coordinator, I want to enumerate the key hashes
enrolled in each slot (`PK`, `KEK`, `db`, `dbx`, `MOK`), so that I can correlate
current policy with desired-state policy.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_secure_boot_policy()` on a Linux host THE SYSTEM
   SHALL invoke `mokutil --list-enrolled` and `efivar -l` via the command runner
   without elevation, and parse the outputs into `pk_hash`, `kek_hashes`, `db_hashes`,
   `dbx_hashes`, and `mok_hashes`.
2. WHEN the coordinator calls `get_secure_boot_policy()` on a macOS host THE SYSTEM
   SHALL invoke `bputil --display` via the command runner without elevation and put the
   full text in `apple_policy_raw`, leaving the UEFI-slot lists empty — Apple does not
   expose them.
3. WHEN the coordinator calls `get_secure_boot_policy()` on a Windows host THE SYSTEM
   SHALL invoke `powershell -NoProfile -Command Get-SecureBootPolicy` via the command
   runner without elevation and populate the slot-hash lists from the output.
4. WHEN the command fails THE SYSTEM SHALL raise `UefiSecureBootQueryError`.

---

### Requirement 3: List Enrolled Keys

**User Story:** As a maintenance coordinator, I want a uniform list of every key
enrolled across all slots, so that I can search for a specific fingerprint without
having to iterate per slot.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_enrolled_keys(slot=None)` THE SYSTEM SHALL return
   the union of every slot's enrolled-key list for the current host OS.
2. WHEN the coordinator calls `list_enrolled_keys(slot="MOK")` on a Linux host THE
   SYSTEM SHALL return the MOK list only.
3. WHEN the coordinator calls `list_enrolled_keys(slot="MOK")` on a macOS or Windows
   host THE SYSTEM SHALL return `[]` — neither host exposes a MOK equivalent.
4. WHEN the coordinator calls `list_enrolled_keys(slot="PK")` THE SYSTEM SHALL return
   the single-entry list containing the platform key (or `[]` when no PK is enrolled).
5. WHEN the command fails THE SYSTEM SHALL raise `UefiSecureBootQueryError`.

---

### Requirement 4: Enroll a MOK Key

**User Story:** As a maintenance coordinator, I want to stage a MOK enrollment from a
certificate file, so that a kernel module or shim-gated loader can be trusted on the
next boot.

#### Acceptance Criteria

1. WHEN the coordinator calls `enroll_mok_key(certificate_path)` on a Linux host THE
   SYSTEM SHALL invoke `mokutil --import <certificate_path>` via the command runner with
   `elevated=True` when configured.
2. WHEN the coordinator calls `enroll_mok_key(certificate_path)` on a macOS host THE
   SYSTEM SHALL NOT execute any command and SHALL return `UefiSecureBootActionResult`
   with status `FAILED` and a `detail` naming Apple's Boot Policy as the alternative
   surface.
3. WHEN the coordinator calls `enroll_mok_key(certificate_path)` on a Windows host THE
   SYSTEM SHALL NOT execute any command and SHALL return `UefiSecureBootActionResult`
   with status `FAILED` and a `detail` noting that Windows uses OEM PK / KEK / db
   tooling for equivalent operations.
4. WHEN the certificate's fingerprint matches a fingerprint already present in the MOK
   list THE SYSTEM SHALL skip execution and return `UefiSecureBootActionResult` with
   status `SKIPPED`.
5. WHEN the command succeeds THE SYSTEM SHALL return `UefiSecureBootActionResult` with
   status `SUCCESS` and `slot="MOK"`.
6. WHEN the command fails THE SYSTEM SHALL return `UefiSecureBootActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   `UefiSecureBootActionResult` with status `DRY_RUN`.

---

### Requirement 5: Reset a MOK Key

**User Story:** As a maintenance coordinator, I want to stage the removal of a MOK key
by fingerprint, so that a previously trusted key is no longer on the next boot's
allow-list.

#### Acceptance Criteria

1. WHEN the coordinator calls `reset_mok(key_fingerprint)` on a Linux host THE SYSTEM
   SHALL invoke `mokutil --delete <key_fingerprint>` via the command runner with
   `elevated=True` when configured.
2. WHEN the coordinator calls `reset_mok(key_fingerprint)` on a macOS host THE SYSTEM
   SHALL return `UefiSecureBootActionResult` with status `FAILED` and a descriptive
   `detail`.
3. WHEN the coordinator calls `reset_mok(key_fingerprint)` on a Windows host THE SYSTEM
   SHALL return `UefiSecureBootActionResult` with status `FAILED` and a descriptive
   `detail`.
4. WHEN the key fingerprint is not present in the MOK list THE SYSTEM SHALL skip
   execution and return `UefiSecureBootActionResult` with status `SKIPPED`.
5. WHEN the command succeeds THE SYSTEM SHALL return `UefiSecureBootActionResult` with
   status `SUCCESS`.
6. WHEN the command fails THE SYSTEM SHALL return `UefiSecureBootActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   `UefiSecureBootActionResult` with status `DRY_RUN`.

---

### Requirement 6: Attempt to Set Secure Boot Mode

**User Story:** As a maintenance coordinator, I want a uniform `set_secure_boot_mode`
API so that cross-OS orchestration code does not have to special-case Secure Boot as
"user-only-via-firmware-UI".

#### Acceptance Criteria

1. WHEN the coordinator calls `set_secure_boot_mode(mode)` on any host OS THE SYSTEM
   SHALL NOT execute any command and SHALL return `UefiSecureBootActionResult` with
   status `FAILED` and a `detail` reading `"User must toggle Secure Boot via firmware
   setup UI on reboot"`.
2. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   `UefiSecureBootActionResult` with status `DRY_RUN` so the caller can audit the
   rejection path without triggering the firmware-setup branch.
3. WHEN the coordinator relies on the return value to drive subsequent steps THE
   SYSTEM SHALL deterministically return either `FAILED` or `DRY_RUN` for this method
   (never `SUCCESS` or `SKIPPED`).

---

### Requirement 7: Host-OS Dispatch Override

**User Story:** As a test engineer, I want to override the host-OS detection at
construction time, so that I can exercise all three dispatch paths from any developer
workstation without live firmware.

#### Acceptance Criteria

1. WHEN `UefiSecureBootManagerConfig.override_host_os` is set to one of `"Linux"`,
   `"Darwin"`, `"Windows"` THE SYSTEM SHALL use that value as the dispatch key instead
   of calling `platform.system()`.
2. WHEN `override_host_os` is `None` THE SYSTEM SHALL use `platform.system()` at
   construction time.
3. WHEN `override_host_os` is set to a value that is not one of the three supported
   platforms THE SYSTEM SHALL raise `ValueError` at construction time.

---

### Requirement 8: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want MOK write commands to run with the
privileges required by `mokutil`, so that the command succeeds on a standard host where
the process does not run as root.

#### Acceptance Criteria

1. WHEN the system is configured with `elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the command runner for `enroll_mok_key` and `reset_mok` on Linux.
2. WHEN performing read-only operations THE SYSTEM SHALL NOT pass `elevated=True`,
   regardless of config.
3. WHEN running as root / administrator THE SYSTEM SHALL NOT pass `elevated=True`.
4. WHEN the elevation configuration is not set THE SYSTEM SHALL default to no elevation
   and allow the runner to surface any resulting permission error as a
   `UefiSecureBootActionResult` with `status=FAILED` for writes or as a
   `UefiSecureBootQueryError` for reads.
