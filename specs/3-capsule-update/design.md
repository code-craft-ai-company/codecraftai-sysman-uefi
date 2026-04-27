# Design

## Overview

The capsule-update feature is implemented as a single module,
`automojo.sysman.uefi.capsuleupdate`, that exposes a `UefiCapsuleUpdateManager` class.
This spec implements the capsule-update contract defined at
`automojo-sysman-common/specs/contracts/capsule-update/contract.md` for UEFI
firmware management, dispatching to host-OS tooling (`fwupdmgr` on Linux,
`softwareupdate` as a delegation target on macOS, the `PSWindowsUpdate` PowerShell
module filtered to firmware on Windows).

The coordinator instantiates `UefiCapsuleUpdateManager` and calls its methods to
enumerate available updates, apply updates by ID or capsule file, list
firmware-updatable devices, and audit update history.  The manager detects the running
host OS at construction time and selects a per-host-OS command builder for every verb.
Output parsing — typically JSON on Linux and Windows, line-oriented text on macOS — is
implemented in pure Python inside the module.

All system interaction is delegated to a command runner implementing the
`ICommandRunner` interface.  No direct file system access or `subprocess` calls are
made inside the module.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner                                      │
│  - Injects runner into UefiCapsuleUpdateManager             │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            UefiCapsuleUpdateManager                         │
│  automojo.sysman.uefi.capsuleupdate                         │
│                                                             │
│  list_available_updates(device_id)                          │
│  apply_update(update_id)                                    │
│  apply_capsule_file(capsule_path)                           │
│  list_devices()                                             │
│  get_update_status(limit)                                   │
└────────────────────────┬────────────────────────────────────┘
                         │ per-method host-OS dispatch
                         ▼
       ┌─────────────────┼─────────────────┐
       ▼                 ▼                 ▼
  LinuxBuilder      DarwinBuilder     WindowsBuilder
  (fwupdmgr)        (softwareupdate,  (PSWindowsUpdate,
                     system_profiler)  Win32_* WMI)
       │                 │                 │
       └─────────────────┼─────────────────┘
                         ▼
                 ICommandRunner
```

---

## Host-OS Dispatch

`UefiCapsuleUpdateManager` manages firmware capsule-update workflows on the host it
runs on.

```
              UefiCapsuleUpdateManager
                     │
           platform.system() or config.override_host_os
                     │
         ┌───────────┼───────────┐
         ▼           ▼           ▼
       Linux       Darwin      Windows
     (fwupdmgr)   (software-   (PSWindowsUpdate,
                   update,      Win32_BIOS)
                   system_
                   profiler)
         │           │           │
         └───────────┼───────────┘
                     ▼
             ICommandRunner.run()
```

- **Linux** — Full feature coverage through `fwupdmgr`, the canonical user-space UEFI
  capsule manager.  Both `apply_update` and `apply_capsule_file` dispatch here; both
  pass `elevated=True` when configured.
- **Darwin (macOS)** — Reads work but writes delegate: `apply_update` and
  `apply_capsule_file` return `FAILED` with a `detail` naming `softwareupdate -i` as the
  correct path.  `list_devices` returns a single Apple-hardware record with
  `supported=False`.
- **Windows** — Apply works through `Install-WindowsUpdate` filtered to firmware
  categories.  `apply_capsule_file` returns `FAILED` because Windows does not expose
  scriptable raw-capsule installation.

On unsupported host OSes, all verbs raise `UefiCapsuleUpdateQueryError` (reads) or
return `FAILED` (writes).  The `UefiCapsuleUpdateManagerConfig.override_host_os` field
forces a specific dispatch path for testing.

---

## Components

### `UefiCapsuleUpdateManager`

**Location:** `source/packages/automojo/sysman/uefi/capsuleupdate.py`

| Method | Signature | Description |
|---|---|---|
| `list_available_updates` | `(device_id) → list[UefiCapsuleUpdateInfo]` | Enumerate pending updates |
| `apply_update` | `(update_id) → UefiCapsuleUpdateActionResult` | Apply by update ID |
| `apply_capsule_file` | `(capsule_path) → UefiCapsuleUpdateActionResult` | Apply a capsule file |
| `list_devices` | `() → list[UefiDeviceInfo]` | Enumerate firmware-updatable devices |
| `get_update_status` | `(limit) → list[UefiCapsuleUpdateHistoryEntry]` | Read update history |

### `UefiCapsuleUpdateManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands are executed |
| `elevated` | `bool` | `False` | When `True`, write commands pass `elevated=True` |
| `allow_downgrade` | `bool` | `False` | When `True`, allow applying an older version than the current one |
| `override_host_os` | `str \| None` | `None` | Forces dispatch path for testing |

---

## Data Models

```python
class UefiCapsuleUpdateActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"

@dataclass
class UefiCapsuleUpdateActionResult:
    status: UefiCapsuleUpdateActionStatus
    update_id: str | None
    capsule_path: str | None
    applied: UefiCapsuleUpdateInfo | None
    detail: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None

@dataclass
class UefiCapsuleUpdateInfo:
    update_id: str
    device_id: str
    current_version: str
    available_version: str
    summary: str
    requires_reboot: bool
    severity: str

@dataclass
class UefiDeviceInfo:
    device_id: str
    vendor: str
    product: str
    current_version: str
    supported: bool

@dataclass
class UefiCapsuleUpdateHistoryEntry:
    update_id: str
    device_id: str
    applied_version: str
    applied_at: str
    result: str

class UefiCapsuleUpdateQueryError(Exception):
    """Raised when a capsule-update enumeration or history query fails."""
```

---

## Interfaces

### `ICommandRunner` (from `automojo-interfaces`)

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

## Device-ID Mapping

The abstract `device_id` is mapped per host OS:

| Host OS | Native form | Example |
|---|---|---|
| Linux | fwupd GUID | `"b8fe6b66-c42a-5c69-a2c9-10d1f1e7e4b1"` |
| Darwin | Apple PlatformIdentifier | `"Mac15,3"` |
| Windows | `Win32_BIOS.Caption` hash or OEM device GUID | `"{e9b1c7a2-...}"` |

Cross-host comparison is not supported; `device_id` values are meaningful only within a
single host-OS dispatch path.

---

## Command Mappings

### Linux (`fwupdmgr`)

| Verb | Command |
|---|---|
| `list_available_updates` | `fwupdmgr get-updates --json` |
| `apply_update` | `fwupdmgr update --no-reboot-check <update_id>` |
| `apply_capsule_file` | `fwupdmgr install --allow-reinstall --no-reboot-check <capsule_path>` |
| `list_devices` | `fwupdmgr get-devices --json` |
| `get_update_status` | `fwupdmgr get-history --json` |

### Darwin (`softwareupdate`, `system_profiler`)

| Verb | Command |
|---|---|
| `list_available_updates` | `softwareupdate --list --include-config-data` |
| `apply_update` | returns `FAILED` with delegation `detail` |
| `apply_capsule_file` | returns `FAILED` |
| `list_devices` | `system_profiler -json SPHardwareDataType` |
| `get_update_status` | `softwareupdate --history` |

### Windows (`PSWindowsUpdate`, `Win32_*`)

| Verb | Command |
|---|---|
| `list_available_updates` | `powershell -NoProfile -Command "Get-WindowsUpdate -MicrosoftUpdate \| Where-Object { $_.CategoryIDs -contains 'firmware' } \| ConvertTo-Json"` |
| `apply_update` | `powershell -NoProfile -Command "Install-WindowsUpdate -KBArticleID <update_id> -AcceptAll -IgnoreReboot"` |
| `apply_capsule_file` | returns `FAILED` — no scriptable raw-capsule surface |
| `list_devices` | `powershell -NoProfile -Command "Get-WmiObject Win32_ComputerSystem, Win32_BIOS \| ConvertTo-Json"` |
| `get_update_status` | `powershell -NoProfile -Command "Get-WUHistory \| Where-Object { $_.CategoryIDs -contains 'firmware' } \| ConvertTo-Json"` |

All write commands pass `elevated=True` when the config enables it.  Read commands never
pass `elevated=True`.

---

## Output Parsing

### Linux `fwupdmgr get-updates --json`

```json
{
  "Releases": [
    {
      "AppstreamId": "com.hughski.ColorHug.firmware",
      "ReleaseId": "b8fe6b66-c42a-5c69-a2c9-10d1f1e7e4b1",
      "Version": "2.0.7",
      "Summary": "ColorHug 2 firmware update",
      "Flags": ["is-upgrade"],
      "RequiresReboot": true,
      "Urgency": "medium",
      "DeviceId": "b8fe6b66-c42a-5c69-a2c9-10d1f1e7e4b1",
      "CurrentVersion": "2.0.6"
    }
  ]
}
```

The parser yields one `UefiCapsuleUpdateInfo` per release with `severity` mapped from
`Urgency`.

### Darwin `softwareupdate --list --include-config-data`

Lines containing `* Label:` followed by `Title:` and `Version:`.  The parser filters to
entries whose Title contains `Firmware` (case-insensitive).

### Windows JSON outputs

The PowerShell outputs are piped through `ConvertTo-Json` so the parser is a single
`json.loads` plus a schema translation.

---

## Idempotency Guards

| Method | Guard | Field inspected |
|---|---|---|
| `apply_update` | `update_id` not in current pending list | `list_available_updates()` |
| `apply_capsule_file` | — (no idempotency guard; the underlying tool handles reinstall detection) | — |
| `apply_update` / `apply_capsule_file` | `allow_downgrade=False` + older version | `list_devices()` + pending-list version |

---

## Downgrade Gating

When `allow_downgrade=False`:

1. Before dispatching `apply_update(update_id)`, the manager locates the pending
   entry's `available_version` and the device's `current_version` via
   `list_devices()`.
2. If `available_version < current_version` (via a simple dotted-segment comparison), the
   manager returns `FAILED` with a "downgrade blocked" `detail` and invokes no command.
3. If the versions do not compare cleanly (vendor-specific formats), the manager logs
   a warning via the runner's persistence aspect and treats the update as
   non-downgrade (proceeds).

When `allow_downgrade=True`, the check is skipped entirely.

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `UefiCapsuleUpdateQueryError` | Any read verb fails with non-zero exit |

All write failures (including "host OS does not implement this verb" and "downgrade
blocked") are surfaced as `UefiCapsuleUpdateActionResult` with `status=FAILED`.

---

## Correctness Properties

### Property 1: List Updates — Linux Command
*For any* `list_available_updates()` on Linux, the system invokes `fwupdmgr get-updates --json`.
**Validates: Requirements 1.1**

### Property 2: List Updates — Device Filter
*For any* `list_available_updates(device_id)` with a non-`None` device_id, results are filtered to matching device_ids.
**Validates: Requirements 1.2**

### Property 3: List Updates — Darwin Command
*For any* `list_available_updates()` on Darwin, the system invokes `softwareupdate --list --include-config-data`.
**Validates: Requirements 1.3**

### Property 4: List Updates — Windows Command
*For any* `list_available_updates()` on Windows, the system invokes the firmware-filtered `Get-WindowsUpdate` pipeline.
**Validates: Requirements 1.4**

### Property 5: List Updates Errors Raise
*For any* failed list command, the system raises `UefiCapsuleUpdateQueryError` with exit code and stderr.
**Validates: Requirements 1.5**

### Property 6: Apply Update — Linux Command
*For any* `apply_update(update_id)` on Linux, the system constructs `fwupdmgr update --no-reboot-check <update_id>`.
**Validates: Requirements 2.1**

### Property 7: Apply Update — Darwin Delegation
*For any* `apply_update` on Darwin, the system returns `FAILED` with a `softwareupdate`-delegation detail and invokes no command.
**Validates: Requirements 2.2**

### Property 8: Apply Update — Windows Command
*For any* `apply_update(update_id)` on Windows, the system constructs the `Install-WindowsUpdate` command filtered by `-KBArticleID`.
**Validates: Requirements 2.3**

### Property 9: Apply Update Idempotency
*For any* `update_id` not present in the current pending list, `apply_update` returns `SKIPPED`.
**Validates: Requirements 2.4**

### Property 10: Apply Update Post-Apply Reboot Flag
*For any* successful `apply_update`, the returned `UefiCapsuleUpdateActionResult.applied.requires_reboot` is `True`.
**Validates: Requirements 2.5**

### Property 11: Apply Capsule File — Linux Command
*For any* `apply_capsule_file(capsule_path)` on Linux, the system constructs `fwupdmgr install --allow-reinstall --no-reboot-check <capsule_path>`.
**Validates: Requirements 3.1**

### Property 12: Apply Capsule File — Windows Rejected
*For any* `apply_capsule_file` on Windows, the system returns `FAILED` with a detail explaining the Windows gap.
**Validates: Requirements 3.3**

### Property 13: List Devices — Linux Command
*For any* `list_devices()` on Linux, the system constructs `fwupdmgr get-devices --json`.
**Validates: Requirements 4.1**

### Property 14: List Devices — Darwin Supported False
*For any* `list_devices()` on Darwin, the returned record has `supported=False`.
**Validates: Requirements 4.2**

### Property 15: List Devices — Windows Command
*For any* `list_devices()` on Windows, the system constructs the `Get-WmiObject` command for `Win32_ComputerSystem` + `Win32_BIOS`.
**Validates: Requirements 4.3**

### Property 16: Update History — Linux Command
*For any* `get_update_status()` on Linux, the system constructs `fwupdmgr get-history --json`.
**Validates: Requirements 5.1**

### Property 17: Update History Limit
*For any* `get_update_status(limit=N)`, the returned list has length ≤ N.
**Validates: Requirements 5.2**

### Property 18: Update History — Darwin Command
*For any* `get_update_status()` on Darwin, the system constructs `softwareupdate --history`.
**Validates: Requirements 5.3**

### Property 19: Update History — Windows Command
*For any* `get_update_status()` on Windows, the system constructs the firmware-filtered `Get-WUHistory` pipeline.
**Validates: Requirements 5.4**

### Property 20: Downgrade Block
*For any* apply request whose `available_version` is older than `current_version` with `allow_downgrade=False`, the system returns `FAILED` with a "Downgrade blocked" detail and invokes no command.
**Validates: Requirements 6.1**

### Property 21: Downgrade Allowed
*For any* apply request with `allow_downgrade=True`, the system proceeds regardless of the version comparison.
**Validates: Requirements 6.2**

### Property 22: Downgrade Ambiguous Version
*For any* apply request whose versions do not compare cleanly, the system logs via `PersistenceAspect` and proceeds.
**Validates: Requirements 6.3**

### Property 23: Dispatch Override Respected
*For any* `override_host_os` in `{Linux, Darwin, Windows}`, the manager uses it instead of `platform.system()`.
**Validates: Requirements 7.1**

### Property 24: Dispatch Override Rejected
*For any* invalid `override_host_os`, the constructor raises `ValueError`.
**Validates: Requirements 7.3**

### Property 25: Dry-Run Safety
*For any* write operation with `dry_run=True`, the runner is never invoked and the result status is `DRY_RUN`.
**Validates: Requirements 2.7, 3.6**

### Property 26: Elevation for Writes
*For any* write operation with `elevated=True`, the runner is invoked with `elevated=True`.
**Validates: Requirements 8.1**

### Property 27: No Elevation for Reads
*For any* read operation, the runner is never invoked with `elevated=True`.
**Validates: Requirements 8.2**

### Property 28: Error Result Consistency
*For any* failed write command, the `UefiCapsuleUpdateActionResult` has `status=FAILED` with `exit_code` and `stderr` populated.
**Validates: Requirements 2.6, 3.5**

---

## Module Layout

```
source/packages/automojo/sysman/uefi/
├── __init__.py
└── capsuleupdate.py          ← UefiCapsuleUpdateManager, config, models, exceptions

source/testroots/testbase/
└── automojo/tests/sysman/uefi/
    └── test_capsuleupdate.py
```

---

## Usage Examples

### Linux — Enumerate and Apply All Pending Firmware Updates

```python
from automojo.sysman.uefi.runners import LocalCommandRunner, LocalRunnerConfig
from automojo.sysman.uefi.capsuleupdate import (
    UefiCapsuleUpdateManager,
    UefiCapsuleUpdateManagerConfig,
)

runner = LocalCommandRunner(LocalRunnerConfig())
config = UefiCapsuleUpdateManagerConfig(elevated=True)
cu_mgr = UefiCapsuleUpdateManager(runner=runner, config=config)

pending = cu_mgr.list_available_updates()
for update in pending:
    print(f"  {update.update_id}: {update.current_version} -> {update.available_version}")
    result = cu_mgr.apply_update(update_id=update.update_id)
    print(f"  -> {result.status} (reboot required: {result.applied.requires_reboot if result.applied else 'n/a'})")
```

### macOS — Observe Delegation Behavior

```python
cu_mgr = UefiCapsuleUpdateManager(runner=runner, config=UefiCapsuleUpdateManagerConfig())
result = cu_mgr.apply_update(update_id="any-id")
assert result.status.value == "failed"
print(f"macOS delegation detail: {result.detail}")
# Expected: "macOS firmware updates are delivered through softwareupdate bundled with OS updates; use 'softwareupdate -i' to apply."
```

### Windows — Apply a Specific KB Firmware Update

```python
config = UefiCapsuleUpdateManagerConfig(
    override_host_os="Windows",
    elevated=True,
    allow_downgrade=False,
)
cu_mgr = UefiCapsuleUpdateManager(runner=runner, config=config)
result = cu_mgr.apply_update(update_id="KB5031455")
print(f"Apply: {result.status}")
```

---

## Testing Strategy

- All tests use a `MockCommandRunner` keyed on command args prefix.
- `override_host_os` is set in every test.
- `fwupdmgr get-updates --json`, `fwupdmgr get-devices --json`, `fwupdmgr get-history
  --json`, `softwareupdate --list`, `softwareupdate --history`, and the three PowerShell
  pipelines have fixture JSON inputs.
- A dedicated test proves `apply_update` returns `FAILED` with a `softwareupdate`
  delegation `detail` on macOS without invoking the runner.
- A dedicated test proves `apply_capsule_file` returns `FAILED` on Windows.
- A dedicated test proves downgrade-gating blocks by default and allows when the flag
  is set.
- A dedicated test proves version-string-ambiguity falls back to "proceed".
- No test requires live firmware-update backends.

---

## Scope Mapping

The contract's abstract "device identifier" scope concept is mapped per host OS:

| Host OS | Identifier form | Example |
|---|---|---|
| Linux | fwupd GUID | `"b8fe6b66-c42a-5c69-a2c9-10d1f1e7e4b1"` |
| Darwin | Apple PlatformIdentifier | `"Mac15,3"` |
| Windows | OEM device GUID / BIOS caption hash | `"{e9b1c7a2-...}"` |

---

## Extensions beyond the contract

- **Downgrade gating (`allow_downgrade` config field).** The base contract does not
  mandate a gate; this spec adds one because firmware downgrades are a documented cause
  of field bricks.
- **macOS delegation detail strings.** The base contract only mandates `FAILED` with a
  descriptive `detail`; this spec pins the exact delegation text so that a cross-OS
  coordinator can scrape it for delegation routing.
- **`override_host_os` testing hook.** Not part of the contract; specific to this
  package's per-host-OS-dispatch architecture.
