# Design

## Overview

The secure-boot feature is implemented as a single module,
`ccai.sysman.uefi.secureboot`, that exposes a `UefiSecureBootManager` class.  This
spec implements the secure-boot contract defined at
`codecraftai-sysman-common/specs/contracts/secure-boot/contract.md` for UEFI firmware
management, dispatching to host-OS tooling (`mokutil` and `efivar` on Linux, `bputil`
and `csrutil` on macOS, `Confirm-SecureBootUEFI` and `Get-SecureBootPolicy` on Windows).

The coordinator instantiates `UefiSecureBootManager` and calls its methods to inspect
Secure Boot state and, on Linux, to enroll or reset MOK keys.  The manager detects the
running host OS at construction time and selects a per-host-OS command builder for every
verb.  Output parsing is implemented in pure Python inside the module.

All system interaction is delegated to a command runner implementing the
`ICommandRunner` interface.  No direct file system access or `subprocess` calls are
made inside the module.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner                                      │
│  - Injects runner into UefiSecureBootManager                │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            UefiSecureBootManager                            │
│  ccai.sysman.uefi.secureboot                            │
│                                                             │
│  get_secure_boot_state()   get_secure_boot_policy()         │
│  list_enrolled_keys(slot)  enroll_mok_key(path)             │
│  reset_mok(fingerprint)    set_secure_boot_mode(mode)       │
└────────────────────────┬────────────────────────────────────┘
                         │ per-method host-OS dispatch
                         ▼
       ┌─────────────────┼─────────────────┐
       ▼                 ▼                 ▼
  LinuxBuilder      DarwinBuilder     WindowsBuilder
  (mokutil,         (csrutil,         (Confirm-SecureBootUEFI,
   efivar)           bputil)           Get-SecureBootPolicy)
       │                 │                 │
       └─────────────────┼─────────────────┘
                         ▼
                 ICommandRunner
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
    LocalCommandRunner            SSHCommandRunner
```

---

## Host-OS Dispatch

`UefiSecureBootManager` manages Secure Boot inspection and MOK management on the host
it runs on.

```
              UefiSecureBootManager
                     │
           platform.system() or config.override_host_os
                     │
         ┌───────────┼───────────┐
         ▼           ▼           ▼
       Linux       Darwin      Windows
    (mokutil,    (csrutil,    (Confirm-
     efivar)      bputil)      SecureBootUEFI,
                               Get-SecureBootPolicy)
         │           │           │
         └───────────┼───────────┘
                     ▼
             ICommandRunner.run()
```

- **Linux** — Full feature coverage.  `mokutil` is the canonical MOK tool; `efivar`
  reads the PK / KEK / db / dbx variables.  MOK writes require `elevated=True` because
  `mokutil` writes to `/sys/firmware/efi/efivars`.
- **Darwin (macOS)** — Read-only for policy inspection.  `bputil` (Apple Silicon) and
  `csrutil` (System Integrity Protection) report the Apple-specific Secure Boot model,
  which does not use the UEFI slot surface.  All write verbs return `FAILED` with a
  descriptive `detail`.
- **Windows** — Read-only for policy inspection.  Writes (MOK-equivalent tooling)
  require OEM-specific surfaces, so `enroll_mok_key` and `reset_mok` return `FAILED`.

On every host OS, `set_secure_boot_mode` is a non-operation: Secure Boot mode is
controlled in firmware-setup UI only.  The method always returns `FAILED` (or
`DRY_RUN`) with a firmware-setup-only `detail`.

On unsupported host OSes, all verbs raise `UefiSecureBootQueryError` (reads) or return
`FAILED` (writes).  The `UefiSecureBootManagerConfig.override_host_os` field forces a
specific dispatch path for testing.

---

## Components

### `UefiSecureBootManager`

**Location:** `source/packages/ccai/sysman/uefi/secureboot.py`

| Method | Signature | Description |
|---|---|---|
| `get_secure_boot_state` | `() → UefiSecureBootState` | Read enabled / supported / mode flags |
| `get_secure_boot_policy` | `() → UefiSecureBootPolicy` | Read all slot hashes |
| `list_enrolled_keys` | `(slot) → list[UefiSecureBootKey]` | Read keys optionally filtered by slot |
| `enroll_mok_key` | `(certificate_path) → UefiSecureBootActionResult` | Stage MOK enrollment |
| `reset_mok` | `(key_fingerprint) → UefiSecureBootActionResult` | Stage MOK removal |
| `set_secure_boot_mode` | `(mode) → UefiSecureBootActionResult` | Always FAILED / DRY_RUN |

### `UefiSecureBootManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands are executed |
| `elevated` | `bool` | `False` | When `True`, write commands pass `elevated=True` |
| `override_host_os` | `str \| None` | `None` | Forces dispatch path for testing |

---

## Data Models

```python
class UefiSecureBootActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"

@dataclass
class UefiSecureBootActionResult:
    status: UefiSecureBootActionStatus
    slot: str | None              # "PK", "KEK", "db", "dbx", "MOK", or None
    detail: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None

@dataclass
class UefiSecureBootState:
    enabled: bool
    supported: bool
    setup_mode: bool
    audit_mode: bool
    deployed_mode: bool
    vendor_keys: bool
    policy_description: str

@dataclass
class UefiSecureBootPolicy:
    pk_hash: str | None
    kek_hashes: list[str]
    db_hashes: list[str]
    dbx_hashes: list[str]
    mok_hashes: list[str]
    apple_policy_raw: str | None

@dataclass
class UefiSecureBootKey:
    slot: str
    fingerprint: str
    subject: str | None
    issuer: str | None

class UefiSecureBootQueryError(Exception):
    """Raised when a Secure Boot query fails for a reason other than 'not supported'."""
```

---

## Interfaces

### `ICommandRunner` (from `codecraftai-interfaces`)

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        elevated: bool = False,
        capture_output: bool = True,
    ) -> RunResult: ...
```

---

## Key-Slot Mapping

The abstract `slot` parameter maps per host OS:

| Host OS | Recognised slots |
|---|---|
| Linux | `"PK"`, `"KEK"`, `"db"`, `"dbx"`, `"MOK"` |
| Darwin | — (writes rejected; `get_secure_boot_policy` populates only `apple_policy_raw`) |
| Windows | `"PK"`, `"KEK"`, `"db"`, `"dbx"` (writes rejected — requires OEM tooling) |

`list_enrolled_keys(slot="MOK")` returns `[]` on macOS and Windows.

---

## Command Mappings

### Linux

| Verb | Command |
|---|---|
| `get_secure_boot_state` | `mokutil --sb-state`; secondary `efivar -n 8be4df61-93ca-11d2-aa0d-00e098032b8c-SecureBoot -p` |
| `get_secure_boot_policy` | `mokutil --list-enrolled`; `efivar -l` to enumerate slot variables |
| `list_enrolled_keys` | `mokutil --list-enrolled` (MOK); parse `efivar` output for `PK`/`KEK`/`db`/`dbx` |
| `enroll_mok_key` | `mokutil --import <certificate_path>` |
| `reset_mok` | `mokutil --delete <key_fingerprint>` |
| `set_secure_boot_mode` | returns `FAILED` unconditionally |

### Darwin

| Verb | Command |
|---|---|
| `get_secure_boot_state` | `csrutil status`; `bputil --display` on Apple Silicon |
| `get_secure_boot_policy` | `bputil --display` (raw text into `apple_policy_raw`) |
| `list_enrolled_keys` | returns `[]` — macOS does not expose the UEFI slot model |
| `enroll_mok_key` | returns `FAILED` |
| `reset_mok` | returns `FAILED` |
| `set_secure_boot_mode` | returns `FAILED` |

### Windows

| Verb | Command |
|---|---|
| `get_secure_boot_state` | `powershell -NoProfile -Command Confirm-SecureBootUEFI` |
| `get_secure_boot_policy` | `powershell -NoProfile -Command Get-SecureBootPolicy` (formatted as JSON via `ConvertTo-Json` pipeline) |
| `list_enrolled_keys` | enumerate from `Get-SecureBootPolicy` output |
| `enroll_mok_key` | returns `FAILED` |
| `reset_mok` | returns `FAILED` |
| `set_secure_boot_mode` | returns `FAILED` |

All write commands pass `elevated=True` when the config enables it.  Read commands never
pass `elevated=True`.

---

## Output Parsing

### Linux `mokutil --sb-state`

```
SecureBoot enabled
```

or

```
SecureBoot disabled
```

or

```
This system doesn't support Secure Boot
```

The parser maps the first form to `enabled=True, supported=True`, the second to
`enabled=False, supported=True`, and the third to `enabled=False, supported=False`.

### Linux `mokutil --list-enrolled`

Per-key blocks:

```
[key 1]
SHA1 Fingerprint: aa:bb:cc:...
        Data:
            Version: 3 (0x2)
            Subject: CN=My Module Signer
            Issuer: CN=My Module Signer
```

The parser yields one `UefiSecureBootKey` per block with `slot="MOK"`, extracting
`fingerprint`, `subject`, and `issuer`.

### Darwin `bputil --display`

Free-form multi-line Apple Boot Policy output; the full text is preserved in
`apple_policy_raw`, and `policy_description` is set to the first-line summary (e.g.,
`"Full Security"`, `"Reduced Security"`, `"Permissive Security"`).

### Windows `Confirm-SecureBootUEFI`

Single-line boolean:

```
True
```

or

```
False
```

Parser sets `enabled` accordingly and `supported=True`.  When the cmdlet returns a
non-zero exit with a "Cmdlet not supported on this platform" stderr, the parser surfaces
`supported=False`.

### Windows `Get-SecureBootPolicy | ConvertTo-Json`

JSON object with a `Publisher` GUID and a `Version`.  Parser treats `Publisher` as the
PK identifier and extracts the slot hashes from the expanded object form.

---

## Idempotency Guards

| Method | Guard | Field inspected |
|---|---|---|
| `enroll_mok_key` | Certificate fingerprint already in MOK list | `list_enrolled_keys(slot="MOK")` |
| `reset_mok` | Fingerprint not in MOK list | `list_enrolled_keys(slot="MOK")` |
| `set_secure_boot_mode` | N/A — always `FAILED` / `DRY_RUN` | — |

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `UefiSecureBootQueryError` | Any read verb fails with non-zero exit for a reason other than "Secure Boot not supported" |

"Secure Boot not supported" is surfaced as a successful `UefiSecureBootState` with
`supported=False`, `enabled=False`, analogous to the firewall contract's "firewall not
running" exception rule.

All write failures (including the "host OS does not implement this verb" case) are
surfaced as `UefiSecureBootActionResult` with `status=FAILED`.

---

## Correctness Properties

### Property 1: State Query — Linux Command
*For any* `get_secure_boot_state()` on Linux, the system invokes `mokutil --sb-state`.
**Validates: Requirements 1.1**

### Property 2: State Query — Darwin Command
*For any* `get_secure_boot_state()` on Darwin, the system invokes `csrutil status` and (on Apple Silicon) `bputil --display`.
**Validates: Requirements 1.2**

### Property 3: State Query — Windows Command
*For any* `get_secure_boot_state()` on Windows, the system invokes `Confirm-SecureBootUEFI`.
**Validates: Requirements 1.3**

### Property 4: State Parsing — Enabled
*For any* `mokutil --sb-state` output containing `"SecureBoot enabled"`, the system returns `UefiSecureBootState(enabled=True, supported=True, ...)`.
**Validates: Requirements 1.4**

### Property 5: State Parsing — Not Supported
*For any* output containing `"doesn't support Secure Boot"`, the system returns `UefiSecureBootState(enabled=False, supported=False, ...)` without raising.
**Validates: Requirements 1.5**

### Property 6: State Query Errors Raise
*For any* failed state query with a reason other than "not supported", the system raises `UefiSecureBootQueryError`.
**Validates: Requirements 1.6**

### Property 7: Policy Query — Linux
*For any* `get_secure_boot_policy()` on Linux, the system invokes `mokutil --list-enrolled` and `efivar -l`.
**Validates: Requirements 2.1**

### Property 8: Policy Query — Darwin Raw
*For any* `get_secure_boot_policy()` on Darwin, the full `bputil --display` text lands in `apple_policy_raw` and the slot lists are empty.
**Validates: Requirements 2.2**

### Property 9: Policy Query — Windows
*For any* `get_secure_boot_policy()` on Windows, the system invokes `Get-SecureBootPolicy` and populates the slot-hash lists.
**Validates: Requirements 2.3**

### Property 10: List Keys Union
*For any* `list_enrolled_keys(slot=None)`, the return is the union of all slot lists.
**Validates: Requirements 3.1**

### Property 11: List Keys — MOK on Linux
*For any* `list_enrolled_keys(slot="MOK")` on Linux, the system returns the parsed MOK list.
**Validates: Requirements 3.2**

### Property 12: List Keys — MOK Empty Elsewhere
*For any* `list_enrolled_keys(slot="MOK")` on macOS or Windows, the result is `[]`.
**Validates: Requirements 3.3**

### Property 13: Enroll MOK — Linux Command
*For any* `enroll_mok_key(path)` on Linux, the system constructs `mokutil --import <path>` with `elevated=True` when configured.
**Validates: Requirements 4.1**

### Property 14: Enroll MOK — Darwin FAILED
*For any* `enroll_mok_key` on Darwin, the system returns `FAILED` with a detail naming Apple's Boot Policy.
**Validates: Requirements 4.2**

### Property 15: Enroll MOK — Windows FAILED
*For any* `enroll_mok_key` on Windows, the system returns `FAILED` with a detail naming OEM tooling.
**Validates: Requirements 4.3**

### Property 16: Enroll MOK Idempotency
*For any* certificate whose fingerprint is already in the MOK list, `enroll_mok_key` returns `SKIPPED`.
**Validates: Requirements 4.4**

### Property 17: Reset MOK — Linux Command
*For any* `reset_mok(fingerprint)` on Linux, the system constructs `mokutil --delete <fingerprint>`.
**Validates: Requirements 5.1**

### Property 18: Reset MOK — Unsupported Host OSes
*For any* `reset_mok` on macOS or Windows, the system returns `FAILED` with descriptive detail.
**Validates: Requirements 5.2, 5.3**

### Property 19: Reset MOK Idempotency
*For any* fingerprint absent from the MOK list, `reset_mok` returns `SKIPPED`.
**Validates: Requirements 5.4**

### Property 20: Set Mode Always FAILED
*For any* `set_secure_boot_mode(mode)` on any host, the system returns `FAILED` with a firmware-setup-only detail, invoking no command.
**Validates: Requirements 6.1**

### Property 21: Set Mode Dry-Run
*For any* `set_secure_boot_mode(mode)` with `dry_run=True`, the system returns `DRY_RUN`.
**Validates: Requirements 6.2**

### Property 22: Set Mode Deterministic
*For any* `set_secure_boot_mode`, the status is either `FAILED` or `DRY_RUN`, never `SUCCESS` or `SKIPPED`.
**Validates: Requirements 6.3**

### Property 23: Dispatch Override Respected
*For any* `override_host_os` value in `{Linux, Darwin, Windows}`, the manager uses it instead of `platform.system()`.
**Validates: Requirements 7.1**

### Property 24: Dispatch Override Rejected
*For any* invalid `override_host_os` value, the constructor raises `ValueError`.
**Validates: Requirements 7.3**

### Property 25: Dry-Run Safety
*For any* write operation with `dry_run=True`, the command runner is never invoked and the status is `DRY_RUN`.
**Validates: Requirements 4.7, 5.7, 6.2**

### Property 26: Elevation for Writes
*For any* write operation with `elevated=True`, the runner is invoked with `elevated=True`.
**Validates: Requirements 8.1**

### Property 27: No Elevation for Reads
*For any* read operation, the runner is never invoked with `elevated=True`.
**Validates: Requirements 8.2**

### Property 28: Error Result Consistency
*For any* failed write command, the `UefiSecureBootActionResult` has `status=FAILED` with `exit_code` and `stderr` populated.
**Validates: Requirements 4.6, 5.6**

---

## Module Layout

```
source/packages/ccai/sysman/uefi/
├── __init__.py
└── secureboot.py            ← UefiSecureBootManager, config, models, exceptions

source/testroots/testbase/
└── ccai/tests/sysman/uefi/
    └── test_secureboot.py
```

---

## Usage Examples

### Linux — Enroll a MOK Key Idempotently

```python
from ccai.sysman.uefi.runners import LocalCommandRunner, LocalRunnerConfig
from ccai.sysman.uefi.secureboot import (
    UefiSecureBootManager,
    UefiSecureBootManagerConfig,
)

runner = LocalCommandRunner(LocalRunnerConfig())
config = UefiSecureBootManagerConfig(elevated=True)
sb_mgr = UefiSecureBootManager(runner=runner, config=config)

state = sb_mgr.get_secure_boot_state()
print(f"Secure Boot enabled={state.enabled} supported={state.supported}")

result = sb_mgr.enroll_mok_key(certificate_path="/tmp/my-signer.der")
print(f"Enroll: {result.status}")
```

### macOS — Read Apple Secure Boot Policy

```python
sb_mgr = UefiSecureBootManager(runner=runner, config=UefiSecureBootManagerConfig())
policy = sb_mgr.get_secure_boot_policy()
print(policy.apple_policy_raw)
print(f"Description: {policy.kek_hashes}")  # empty on macOS
```

### Windows — Read-Only Inspection with Dispatch Override

```python
# Test-time: force Windows dispatch from a Linux developer workstation.
mock_runner = MockCommandRunner()
mock_runner.register(
    args_prefix=["powershell", "-NoProfile", "-Command", "Confirm-SecureBootUEFI"],
    stdout="True\n",
)
sb_mgr = UefiSecureBootManager(
    runner=mock_runner,
    config=UefiSecureBootManagerConfig(override_host_os="Windows"),
)
assert sb_mgr.get_secure_boot_state().enabled is True
```

---

## Testing Strategy

- All tests use a `MockCommandRunner` keyed on command args prefix.
- `override_host_os` is set in every test.
- `mokutil --sb-state`, `mokutil --list-enrolled`, `csrutil status`, `bputil --display`,
  `Confirm-SecureBootUEFI`, and `Get-SecureBootPolicy` mock responses drive all tests.
- A dedicated test proves `enroll_mok_key` and `reset_mok` return `FAILED` on macOS and
  Windows without invoking the runner.
- A dedicated test proves `set_secure_boot_mode` always returns `FAILED` (non-dry-run)
  or `DRY_RUN` (dry-run) on every host OS.
- A dedicated test proves MOK enrollment idempotency by matching fingerprints.
- No test requires live Secure Boot access.

---

## Scope Mapping

The contract's abstract "key slot" scope concept is mapped per host OS:

| Host OS | Supported slots | Write surface |
|---|---|---|
| Linux | `PK`, `KEK`, `db`, `dbx`, `MOK` | MOK writable via `mokutil`; others read-only |
| Darwin | — (Apple Boot Policy raw text only) | Writes rejected |
| Windows | `PK`, `KEK`, `db`, `dbx` (read-only) | Writes rejected |

---

## Extensions beyond the contract

- **Apple Boot Policy raw text.** `UefiSecureBootPolicy.apple_policy_raw` is a non-
  standard field that preserves the full `bputil --display` output so a coordinator can
  apply vendor-specific logic without re-running the command.
- **`override_host_os` testing hook.** Not part of the contract; specific to this
  package's per-host-OS-dispatch architecture.
- **Permissive `set_secure_boot_mode`.** The base contract mandates `FAILED` always; this
  spec additionally honors `dry_run=True` to return `DRY_RUN` so that audit-only
  coordinators can log the rejection without triggering a firmware-setup path.
