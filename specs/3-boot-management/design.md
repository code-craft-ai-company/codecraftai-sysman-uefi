# Design

## Overview

The boot-management feature is implemented as a single module,
`ccai.sysman.uefi.bootmanagement`, that exposes a `UefiBootManager` class.  This
spec implements the boot-management contract defined at
`codecraftai-sysman-common/specs/contracts/boot-management/contract.md` for UEFI
firmware management, dispatching to host-OS tooling (`efibootmgr` on Linux, `bless` and
`nvram` on macOS, `bcdedit` on Windows).

The coordinator instantiates `UefiBootManager` and calls its methods to perform discrete
firmware boot-entry actions.  The manager detects the running host OS at construction
time and internally selects a per-host-OS command-builder for every verb.  Output
parsing for each of the three tools is implemented in pure Python inside the module.

All system interaction is delegated to a command runner implementing the
`ICommandRunner` interface.  No direct file system access or `subprocess` calls are made
inside the module.

The manager works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH / WinRM (via `SSHCommandRunner`).  When using
a remote runner, the coordinator can apply execution aspects (persistence, retry,
timeout) without any changes to the manager code.

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)
for details on local / remote execution, reliability tracking, and aspect configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH/WinRM)                 │
│  - Configures aspects (optional)                            │
│  - Injects runner into UefiBootManager                      │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            UefiBootManager                                  │
│  ccai.sysman.uefi.bootmanagement                        │
│                                                             │
│  list_boot_entries()    get_boot_entry(id)                  │
│  create_boot_entry(s)   delete_boot_entry(id)               │
│  get_boot_order()       set_boot_order(order)               │
│  get_boot_next()        set_boot_next(id)                   │
│  get_boot_current()                                         │
└────────────────────────┬────────────────────────────────────┘
                         │ per-method host-OS dispatch
                         ▼
       ┌─────────────────┼─────────────────┐
       ▼                 ▼                 ▼
  LinuxBuilder      DarwinBuilder     WindowsBuilder
  (efibootmgr)      (bless, nvram)    (bcdedit)
       │                 │                 │
       └─────────────────┼─────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
│  run(args, elevated, capture_output) → RunResult            │
└────────────────────────┬────────────────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
┌──────────────────────┐      ┌──────────────────────┐
│ LocalCommandRunner   │      │ SSHCommandRunner     │
└──────────────────────┘      └──────────────────────┘
                         │ optional
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                    Execution Aspects                        │
│  - PersistenceAspect, RetryAspect, TimeoutAspect            │
└─────────────────────────────────────────────────────────────┘
```

---

## Host-OS Dispatch

`UefiBootManager` manages UEFI firmware on the host it runs on.  At construction time
the manager records the detected host OS once and reuses that value for the lifetime of
the instance.  Every feature method selects a per-host-OS command-builder at call time
based on the detected OS.

```
              UefiBootManager
                     │
           platform.system() or config.override_host_os
                     │
         ┌───────────┼───────────┐
         ▼           ▼           ▼
       Linux       Darwin      Windows
     (efibootmgr)  (bless +    (bcdedit)
                    nvram)
         │           │           │
         └───────────┼───────────┘
                     ▼
             ICommandRunner.run()
```

- **Linux** — Uses `efibootmgr` for the whole verb set.  Entry IDs are 4-hex-digit
  strings parsed from `efibootmgr -v`.
- **Darwin (macOS)** — Uses `bless --info --verbose` for enumeration and `nvram` for
  a subset of variables.  `delete_boot_entry`, `set_boot_order`, and `set_boot_next`
  return `FAILED` with a descriptive `detail` — these verbs are not exposed through
  scriptable user-space tooling.  Entry IDs are opaque strings taken from `bless`.
- **Windows** — Uses `bcdedit` for the whole verb set.  Entry IDs are GUIDs (the same
  GUIDs `bcdedit` already uses as handles).

On unsupported host OSes (an arbitrary Unix variant), `list_boot_entries()` raises
`UefiBootQueryError` and writes return `FAILED`.  The `UefiBootManagerConfig.override_
host_os` field (optional) forces a specific dispatch path for testing — always exercised
by the test suite so all three paths are covered on any developer workstation.

---

## Components

### `UefiBootManager`

**Location:** `source/packages/ccai/sysman/uefi/bootmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `list_boot_entries` | `() → list[UefiBootEntryInfo]` | Enumerate firmware boot entries |
| `get_boot_entry` | `(entry_id) → UefiBootEntryInfo` | Fetch single entry by ID |
| `create_boot_entry` | `(spec) → UefiBootActionResult` | Create a new entry |
| `delete_boot_entry` | `(entry_id) → UefiBootActionResult` | Delete an entry |
| `get_boot_order` | `() → list[str]` | Read `BootOrder` |
| `set_boot_order` | `(order) → UefiBootActionResult` | Write `BootOrder` |
| `get_boot_next` | `() → str \| None` | Read `BootNext` |
| `set_boot_next` | `(entry_id) → UefiBootActionResult` | Write `BootNext` |
| `get_boot_current` | `() → str` | Read `BootCurrent` |

### `UefiBootManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands are executed |
| `elevated` | `bool` | `False` | When `True`, write commands pass `elevated=True` |
| `override_host_os` | `str \| None` | `None` | Forces dispatch to `"Linux"`, `"Darwin"`, or `"Windows"`; testing hook |

---

## Data Models

### Result Types

```python
class UefiBootActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"

@dataclass
class UefiBootActionResult:
    status: UefiBootActionStatus
    entry_id: str | None
    detail: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
```

### Query Result Types

```python
@dataclass
class UefiBootEntryInfo:
    entry_id: str
    label: str
    loader_path: str
    device: str | None
    partition: int | None
    is_active: bool
    is_current: bool

@dataclass
class UefiBootEntrySpec:
    label: str
    device: str
    partition: int
    loader_path: str
    description: str | None

class UefiBootQueryError(Exception):
    """Raised when a boot-entry query fails or a target entry is missing."""
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

The boot manager requires no other interfaces.

---

## Entry-ID Mapping

The abstract `entry_id` is mapped to a host-OS-specific identifier form:

| Host OS | Native form | Canonicalisation |
|---|---|---|
| Linux | `Boot<XXXX>` (4 hex digits) | Lower-cased; leading zeros preserved |
| Darwin | Opaque string from `bless --info` | Preserved verbatim |
| Windows | GUID `{xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx}` | Lower-cased; enclosing braces preserved |

Cross-host comparisons are never performed — identifiers are opaque to the coordinator.

---

## Command Mappings

### Linux (`efibootmgr`)

| Verb | Command |
|---|---|
| `list_boot_entries` | `efibootmgr -v` |
| `get_boot_entry` | `efibootmgr -v` then filter by `Boot<id>` in pure Python |
| `create_boot_entry` | `efibootmgr -c -d <device> -p <partition> -L <label> -l <loader_path>` |
| `delete_boot_entry` | `efibootmgr -b <entry_id> -B` |
| `get_boot_order` | `efibootmgr` (parse `BootOrder:` line) |
| `set_boot_order` | `efibootmgr -o <xxxx,yyyy,zzzz>` |
| `get_boot_next` | `efibootmgr` (parse `BootNext:` line) |
| `set_boot_next` | `efibootmgr -n <entry_id>` |
| `get_boot_current` | `efibootmgr` (parse `BootCurrent:` line) |

### Darwin (`bless`, `nvram`)

| Verb | Command |
|---|---|
| `list_boot_entries` | `bless --info --verbose` + `nvram -p` |
| `get_boot_entry` | internal filter of `list_boot_entries()` result |
| `create_boot_entry` | `bless --mount <mountpoint> --setBoot` (limited — does not take arbitrary label/loader) |
| `delete_boot_entry` | returns `FAILED` — macOS has no scriptable single-entry delete |
| `get_boot_order` | `nvram efi-boot-device` (parse single entry) |
| `set_boot_order` | returns `FAILED` — `bless --setBoot` handles a single boot device only |
| `get_boot_next` | returns `None` — not exposed |
| `set_boot_next` | returns `FAILED` — not exposed |
| `get_boot_current` | `nvram efi-boot-device` |

### Windows (`bcdedit`)

| Verb | Command |
|---|---|
| `list_boot_entries` | `bcdedit /enum firmware` |
| `get_boot_entry` | `bcdedit /enum firmware` then filter by GUID |
| `create_boot_entry` | `bcdedit /create /d <description> /application BOOTSECTOR`, then `bcdedit /set {guid} device partition=<letter>:`, then `bcdedit /set {guid} path <loader_path>` |
| `delete_boot_entry` | `bcdedit /delete {entry_id}` |
| `get_boot_order` | `bcdedit /enum {fwbootmgr}` (parse `displayorder`) |
| `set_boot_order` | `bcdedit /set {fwbootmgr} displayorder <guid1> <guid2> ...` |
| `get_boot_next` | `bcdedit /enum {fwbootmgr}` (parse `bootsequence`) |
| `set_boot_next` | `bcdedit /set {fwbootmgr} bootsequence {entry_id}` |
| `get_boot_current` | `bcdedit /enum {current}` |

All write commands pass `elevated=True` when the config enables it.  Read commands never
pass `elevated=True`.

---

## Output Parsing

### Linux `efibootmgr -v`

```
BootCurrent: 0001
Timeout: 0 seconds
BootOrder: 0001,0000,2001,2002
Boot0000* ubuntu    HD(1,GPT,...)/File(\EFI\ubuntu\shimx64.efi)
Boot0001* Windows Boot Manager  HD(1,GPT,...)/File(\EFI\Microsoft\Boot\bootmgfw.efi)
Boot2001* EFI USB Device        RC
```

The parser splits on lines.  Lines starting with `Boot<XXXX>` are entry rows; `*` after
the ID indicates active; the trailing `File(...)` fragment is the loader path.
`BootCurrent:`, `BootOrder:`, and `BootNext:` prefix-match against their prefixes.

### Darwin `bless --info --verbose`

```
finderinfo[0]: 100 => Blessed System File is /System/Library/CoreServices/boot.efi
finderinfo[1]: 0 => No Startup Efi File
...
```

The parser extracts the blessed system file path as the single `UefiBootEntryInfo`.
`nvram -p` output is additionally scanned for `efi-boot-device` and other entries.

### Windows `bcdedit /enum firmware`

```
Firmware Boot Manager
---------------------
identifier              {fwbootmgr}
displayorder            {bootmgr}
                        {12345678-1234-1234-1234-123456789abc}
timeout                 5

Windows Boot Manager
--------------------
identifier              {bootmgr}
device                  partition=\Device\HarddiskVolume1
path                    \EFI\Microsoft\Boot\bootmgfw.efi
description             Windows Boot Manager
...
```

The parser splits on blank-line-separated sections, extracts the `identifier`, `device`,
`path`, `description` fields, and treats `{fwbootmgr}`'s `displayorder` as the
`BootOrder`.

---

## Idempotency Guards

| Method | Guard | Field inspected |
|---|---|---|
| `create_boot_entry` | Entry with same `label` and `loader_path` already present | `list_boot_entries()` |
| `delete_boot_entry` | Entry does not exist | `list_boot_entries()` |
| `set_boot_order` | Current boot order equals requested order | `get_boot_order()` |
| `set_boot_next` | Current `BootNext` equals requested id | `get_boot_next()` |

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `UefiBootQueryError` | Any read verb fails with non-zero exit, or `get_boot_entry` targets a missing entry, or the host OS is not one of `Linux`/`Darwin`/`Windows` |

All failures from write operations are surfaced as `UefiBootActionResult` with
`status=FAILED`, including the "host OS does not implement this verb" case.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do.*

### Property 1: List Boot Entries — Linux Command
*For any* `list_boot_entries()` call on a Linux host, the system should invoke `efibootmgr -v` without elevation.
**Validates: Requirements 1.1**

### Property 2: List Boot Entries — Darwin Command
*For any* `list_boot_entries()` call on a Darwin host, the system should invoke `bless --info --verbose` and `nvram -p` without elevation.
**Validates: Requirements 1.2**

### Property 3: List Boot Entries — Windows Command
*For any* `list_boot_entries()` call on a Windows host, the system should invoke `bcdedit /enum firmware` without elevation.
**Validates: Requirements 1.3**

### Property 4: List Boot Entries Error Surface
*For any* failed enumeration, the system should raise `UefiBootQueryError` carrying the exit code and stderr.
**Validates: Requirements 1.4**

### Property 5: Unsupported Host OS Raises
*For any* host OS not in `{Linux, Darwin, Windows}`, read verbs raise `UefiBootQueryError` naming the detected platform.
**Validates: Requirements 1.5**

### Property 6: Get Single Entry Missing
*For any* `get_boot_entry(entry_id)` where the entry is absent, the system should raise `UefiBootQueryError`.
**Validates: Requirements 2.2**

### Property 7: Create Entry — Linux Command
*For any* `create_boot_entry(spec)` on Linux, the system should construct `efibootmgr -c -d <dev> -p <part> -L <label> -l <loader>`.
**Validates: Requirements 3.1**

### Property 8: Create Entry — Windows Command
*For any* `create_boot_entry(spec)` on Windows, the system should construct the three-step `bcdedit /create`, `bcdedit /set ... device`, `bcdedit /set ... path` sequence.
**Validates: Requirements 3.3**

### Property 9: Create Entry Idempotency
*For any* spec whose `label` and `loader_path` match an existing entry, `create_boot_entry` returns `SKIPPED`.
**Validates: Requirements 3.4**

### Property 10: Delete Entry — Linux Command
*For any* `delete_boot_entry(entry_id)` on Linux, the system should construct `efibootmgr -b <entry_id> -B`.
**Validates: Requirements 4.1**

### Property 11: Delete Entry — macOS Returns FAILED
*For any* `delete_boot_entry` on macOS, the system should return `FAILED` with a descriptive detail and invoke no command.
**Validates: Requirements 4.2**

### Property 12: Delete Entry — Windows Command
*For any* `delete_boot_entry(entry_id)` on Windows, the system should construct `bcdedit /delete {entry_id}`.
**Validates: Requirements 4.3**

### Property 13: Delete Entry Idempotency
*For any* entry absent from `list_boot_entries()`, `delete_boot_entry` returns `SKIPPED`.
**Validates: Requirements 4.4**

### Property 14: Boot Order Parsing — Linux
*For any* `efibootmgr` output containing `BootOrder: 0001,0000,2001`, `get_boot_order` returns `["0001","0000","2001"]`.
**Validates: Requirements 5.1**

### Property 15: Set Boot Order — Linux Command
*For any* `set_boot_order([a,b,c])` on Linux, the system should construct `efibootmgr -o a,b,c`.
**Validates: Requirements 6.1**

### Property 16: Set Boot Order — macOS Returns FAILED
*For any* `set_boot_order` on macOS, the system returns `FAILED` with a detail explaining the `bless` gap.
**Validates: Requirements 6.2**

### Property 17: Set Boot Order — Windows Command
*For any* `set_boot_order([a,b,c])` on Windows, the system should construct `bcdedit /set {fwbootmgr} displayorder a b c`.
**Validates: Requirements 6.3**

### Property 18: Set Boot Order Idempotency
*For any* requested order equal to the current order, `set_boot_order` returns `SKIPPED`.
**Validates: Requirements 6.4**

### Property 19: Get BootNext — macOS Returns None
*For any* `get_boot_next()` on macOS, the system returns `None` without invoking any command.
**Validates: Requirements 7.2**

### Property 20: Set BootNext — Linux Command
*For any* `set_boot_next(entry_id)` on Linux, the system should construct `efibootmgr -n <entry_id>`.
**Validates: Requirements 8.1**

### Property 21: Set BootNext — Windows Command
*For any* `set_boot_next(entry_id)` on Windows, the system should construct `bcdedit /set {fwbootmgr} bootsequence {entry_id}`.
**Validates: Requirements 8.3**

### Property 22: Set BootNext Idempotency
*For any* `entry_id` equal to the current `BootNext`, `set_boot_next` returns `SKIPPED`.
**Validates: Requirements 8.4**

### Property 23: Dispatch Override Respected
*For any* `override_host_os` value in `{Linux, Darwin, Windows}`, the manager uses it instead of `platform.system()`.
**Validates: Requirements 10.1**

### Property 24: Dispatch Override Rejected
*For any* `override_host_os` outside `{Linux, Darwin, Windows, None}`, the constructor raises `ValueError`.
**Validates: Requirements 10.3**

### Property 25: Dry-Run Safety
*For any* write operation with `dry_run=True`, the command runner is never invoked and the result status is `DRY_RUN`.
**Validates: Requirements 3.7, 4.7, 6.7, 8.7**

### Property 26: Elevation for Writes
*For any* write operation with `elevated=True` in the config, the runner is invoked with `elevated=True`.
**Validates: Requirements 11.1**

### Property 27: No Elevation for Reads
*For any* read operation, the runner is never invoked with `elevated=True` regardless of config.
**Validates: Requirements 11.2**

### Property 28: Error Result Consistency
*For any* failed write command, the `UefiBootActionResult` has `status=FAILED` with `exit_code` and `stderr` populated.
**Validates: Requirements 3.6, 4.6, 6.6, 8.6**

---

## Module Layout

```
source/packages/ccai/sysman/uefi/
├── __init__.py
└── bootmanagement.py         ← UefiBootManager, config, models, exceptions

source/testroots/testbase/
└── ccai/tests/sysman/uefi/
    └── test_bootmanagement.py
```

---

## Usage Examples

### Linux Host — Create and Order an Entry

```python
from ccai.sysman.uefi.runners import LocalCommandRunner, LocalRunnerConfig
from ccai.sysman.uefi.bootmanagement import (
    UefiBootManager,
    UefiBootManagerConfig,
    UefiBootEntrySpec,
)

runner = LocalCommandRunner(LocalRunnerConfig())
config = UefiBootManagerConfig(elevated=True, dry_run=False)
boot_mgr = UefiBootManager(runner=runner, config=config)

spec = UefiBootEntrySpec(
    label="custom-ubuntu",
    device="/dev/sda",
    partition=1,
    loader_path="\\EFI\\custom\\grubx64.efi",
    description="Custom Ubuntu loader",
)
create_result = boot_mgr.create_boot_entry(spec=spec)
print(f"Create: {create_result.status} id={create_result.entry_id}")

order = boot_mgr.get_boot_order()
new_order = [create_result.entry_id] + [eid for eid in order if eid != create_result.entry_id]
set_result = boot_mgr.set_boot_order(order=new_order)
print(f"Set order: {set_result.status}")
```

### macOS Host — Read-Only Inspection

```python
# macOS does not expose scriptable boot-entry writes; reads still work.
boot_mgr = UefiBootManager(runner=runner, config=UefiBootManagerConfig())
entries = boot_mgr.list_boot_entries()
for entry in entries:
    print(f"{entry.entry_id}: {entry.label} -> {entry.loader_path}")

current = boot_mgr.get_boot_current()
print(f"BootCurrent: {current}")

# Writes explicitly return FAILED on macOS with a clear detail.
result = boot_mgr.set_boot_order(order=["opaque-id-a", "opaque-id-b"])
assert result.status.value == "failed"
print(f"Expected failure detail: {result.detail}")
```

### Test-Time Dispatch Override

```python
# Force Linux dispatch on a macOS developer workstation to unit-test parsing.
config = UefiBootManagerConfig(override_host_os="Linux", dry_run=True)
mock_runner = MockCommandRunner()
mock_runner.register(
    args_prefix=["efibootmgr"],
    stdout="BootCurrent: 0001\nBootOrder: 0001,0000\nBoot0000* ubuntu HD(...)File(\\EFI\\ubuntu\\shimx64.efi)\n",
)
boot_mgr = UefiBootManager(runner=mock_runner, config=config)
entries = boot_mgr.list_boot_entries()
assert entries[0].entry_id == "0000"
```

---

## Testing Strategy

- All tests use a `MockCommandRunner` keyed on command args prefix.
- `override_host_os` is set to one of `"Linux"`, `"Darwin"`, `"Windows"` in every test so
  the developer workstation's actual OS is irrelevant.
- `efibootmgr -v`, `bless --info --verbose`, `nvram -p`, and `bcdedit /enum firmware`
  mock responses drive all idempotency guards.
- Parser tests cover representative multi-entry outputs for each host OS.
- A dedicated test proves `delete_boot_entry`, `set_boot_order`, and `set_boot_next`
  return `FAILED` with descriptive `detail` on macOS without invoking the runner.
- A dedicated test proves every write method's `DRY_RUN` branch.
- A dedicated test proves read methods never set `elevated=True`.
- No test requires a live UEFI firmware or root / administrator access.

---

## Scope Mapping

The contract's abstract "boot entry ID" scope concept is mapped per host OS:

| Host OS | Identifier form | Example |
|---|---|---|
| Linux | 4-hex-digit | `"0001"` |
| Darwin | Opaque string | `"boot.efi-EFIxxxx"` |
| Windows | GUID | `"{12345678-1234-1234-1234-123456789abc}"` |

Cross-host identifier comparison is not supported — IDs are meaningful only within a
single host-OS dispatch path.

---

## Extensions beyond the contract

- **Three-step Windows create.** The contract's `create_boot_entry(spec)` is a single
  verb; on Windows the manager internally issues three `bcdedit` commands and reports
  them as a single `UefiBootActionResult` with the `command` field holding the first
  (creating) command.  Subsequent step failures surface with `status=FAILED` and
  `detail` pointing at which step failed.
- **macOS `bless` gap documentation.** `UefiBootActionResult.detail` on macOS-rejected
  verbs names Apple's `bless --setBoot` as the nearest alternative so that a cross-OS
  coordinator can log a clear delegation hint rather than a silent no-op.
- **`override_host_os` testing hook.** Not part of the contract; specific to this
  package's per-host-OS-dispatch architecture.
