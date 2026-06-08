# Design

## Overview

The efi-variables feature is implemented as a single module,
`ccai.sysman.uefi.efivariables`, that exposes a `UefiVariableManager` class.  This
spec implements the efi-variables contract defined at
`codecraftai-sysman-common/specs/contracts/efi-variables/contract.md` for UEFI firmware
management, dispatching to host-OS tooling (`efivar` and the `/sys/firmware/efi/efivars`
filesystem on Linux, `nvram` on macOS, the PowerShell `Uefi` module on Windows).

The coordinator instantiates `UefiVariableManager` and calls its methods to read, write,
delete, and enumerate EFI NVRAM variables.  The manager detects the running host OS at
construction time and selects a per-host-OS command builder for every verb.  Output
parsing for each of the three tools is implemented in pure Python inside the module.

All system interaction is delegated to a command runner implementing the
`ICommandRunner` interface.  No direct file system access or `subprocess` calls are
made inside the module; Linux's efivars-immutable-flag handling goes through `chattr`
on the runner.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner                                      │
│  - Injects runner into UefiVariableManager                  │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            UefiVariableManager                              │
│  ccai.sysman.uefi.efivariables                          │
│                                                             │
│  get_variable(name, namespace)                              │
│  set_variable(name, value, namespace, attributes)           │
│  delete_variable(name, namespace)                           │
│  list_variables(namespace)                                  │
└────────────────────────┬────────────────────────────────────┘
                         │ per-method host-OS dispatch
                         ▼
       ┌─────────────────┼─────────────────┐
       ▼                 ▼                 ▼
  LinuxBuilder      DarwinBuilder     WindowsBuilder
  (efivar, chattr)  (nvram)           (powershell [UEFI] module)
       │                 │                 │
       └─────────────────┼─────────────────┘
                         ▼
                 ICommandRunner
```

---

## Host-OS Dispatch

`UefiVariableManager` manages EFI NVRAM variables on the host it runs on.

```
              UefiVariableManager
                     │
           platform.system() or config.override_host_os
                     │
         ┌───────────┼───────────┐
         ▼           ▼           ▼
       Linux       Darwin      Windows
    (efivar,     (nvram)      (powershell
     chattr)                    [UEFI] module)
         │           │           │
         └───────────┼───────────┘
                     ▼
             ICommandRunner.run()
```

- **Linux** — `efivar -p / -w / -D / -l` is the primary surface.  Writes and deletes
  pass through `chattr -i` first to clear the immutable flag on the efivars filesystem,
  then re-set it after a successful write.
- **Darwin (macOS)** — `nvram` is the single-tool surface.  macOS uses friendly
  namespace aliases (`Apple`, `Common`) plus full GUIDs; reads and writes use
  `<namespace>:<name>` syntax.
- **Windows** — The PowerShell `Uefi` community module (or the raw
  `[UEFI]::GetVariable(...)` managed interop) is the surface.  Writes require
  Administrator elevation.

On unsupported host OSes, all verbs raise `UefiVariableQueryError` (reads) or return
`FAILED` (writes).  The `UefiVariableManagerConfig.override_host_os` field forces a
specific dispatch path for testing.

---

## Components

### `UefiVariableManager`

**Location:** `source/packages/ccai/sysman/uefi/efivariables.py`

| Method | Signature | Description |
|---|---|---|
| `get_variable` | `(name, namespace) → UefiVariable` | Read one variable |
| `set_variable` | `(name, value, namespace, attributes) → UefiVariableActionResult` | Write one variable |
| `delete_variable` | `(name, namespace) → UefiVariableActionResult` | Delete one variable |
| `list_variables` | `(namespace) → list[UefiVariable]` | Enumerate variables |

### `UefiVariableManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands are executed |
| `elevated` | `bool` | `False` | When `True`, write commands pass `elevated=True` |
| `override_host_os` | `str \| None` | `None` | Forces dispatch path for testing |

---

## Data Models

```python
class UefiVariableActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"

@dataclass
class UefiVariableActionResult:
    status: UefiVariableActionStatus
    namespace: str
    name: str
    detail: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None

@dataclass
class UefiVariable:
    namespace: str
    name: str
    value: bytes
    attributes: int
    size_bytes: int

class UefiVariableQueryError(Exception):
    """Raised when a variable enumeration or read fails (not a simple missing variable)."""

class UefiVariableNotFoundError(UefiVariableQueryError):
    """Raised when get_variable targets a variable that does not exist."""

class UefiVariableNamespaceError(ValueError):
    """Raised when the namespace is not supported on the current host OS."""
```

Well-known attribute bits are exposed as module-level constants:

```python
NON_VOLATILE                         = 0x01
BOOTSERVICE_ACCESS                   = 0x02
RUNTIME_ACCESS                       = 0x04
HARDWARE_ERROR_RECORD                = 0x08
AUTHENTICATED_WRITE_ACCESS           = 0x10
TIME_BASED_AUTHENTICATED_WRITE_ACCESS = 0x20
APPEND_WRITE                         = 0x40

DEFAULT_ATTRIBUTES = NON_VOLATILE | BOOTSERVICE_ACCESS | RUNTIME_ACCESS  # 0x07
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

## Namespace Mapping

The abstract `namespace` is normalised through a registry:

| Alias | Canonical GUID | Host OSes where accepted |
|---|---|---|
| `EFI_GLOBAL_VARIABLE` | `8be4df61-93ca-11d2-aa0d-00e098032b8c` | Linux, Darwin, Windows |
| `EFI_IMAGE_SECURITY_DATABASE_GUID` | `d719b2cb-3d3a-4596-a3bc-dad00e67656f` | Linux, Darwin, Windows |
| `Apple` | (Apple-reserved — not a single GUID) | Darwin only |
| `Common` | (macOS common namespace) | Darwin only |

Full GUID strings (lower-cased) are accepted verbatim on any host OS subject to the
host OS accepting that namespace for raw access.  Non-alias, non-GUID strings raise
`ValueError`.  Host-OS-reserved aliases raise `UefiVariableNamespaceError` when dispatch
is not the owning host.

---

## Command Mappings

### Linux (`efivar`, `chattr`)

| Verb | Command |
|---|---|
| `get_variable` | `efivar -n <namespace>-<name> -p` |
| `set_variable` | `chattr -i /sys/firmware/efi/efivars/<name>-<namespace>` then `efivar -n <namespace>-<name> -w -f <binary>` then `chattr +i /sys/firmware/efi/efivars/<name>-<namespace>` |
| `delete_variable` | `chattr -i /sys/firmware/efi/efivars/<name>-<namespace>` then `efivar -n <namespace>-<name> -D` |
| `list_variables` | `efivar -l` |

### Darwin (`nvram`)

| Verb | Command |
|---|---|
| `get_variable` | `nvram <namespace>:<name>` (or `nvram <name>` for Common namespace) |
| `set_variable` | `nvram <namespace>:<name>=<encoded_value>` |
| `delete_variable` | `nvram -d <namespace>:<name>` |
| `list_variables` | `nvram -p` |

### Windows (PowerShell `Uefi` module)

| Verb | Command |
|---|---|
| `get_variable` | `powershell -NoProfile -Command "[UEFI]::GetVariable('<namespace>', '<name>')"` |
| `set_variable` | `powershell -NoProfile -Command "[UEFI]::SetVariable('<namespace>', '<name>', [byte[]]@(...), <attributes>)"` |
| `delete_variable` | `powershell -NoProfile -Command "[UEFI]::DeleteVariable('<namespace>', '<name>')"` |
| `list_variables` | `powershell -NoProfile -Command "[UEFI]::EnumerateVariables()"` |

All write commands pass `elevated=True` when the config enables it.  Read commands never
pass `elevated=True`.

---

## Output Parsing

### Linux `efivar -n <id> -p`

```
GUID: 8be4df61-93ca-11d2-aa0d-00e098032b8c
Name: "BootOrder"
Attributes:
        Non-Volatile
        Boot Service Access
        Runtime Service Access
Value:
00000000  01 00 00 00 02 00 2f 00  |....../.|
```

The parser extracts:

1. The hex dump lines after `Value:` are decoded into raw bytes.
2. The attribute names (`Non-Volatile`, `Boot Service Access`, `Runtime Service Access`,
   etc.) are mapped to the bitmap constants.
3. `size_bytes` is computed from the decoded-byte length.

### Linux `efivar -l`

One line per variable, format `<guid>-<name>`.  The parser splits on the first `-` and
yields `UefiVariable` records with empty `value` bytes (since `efivar -l` does not emit
values).

### Darwin `nvram -p`

```
namespace:name    value
Apple:boot-args   -v
Common:SystemAudioVolume %80
```

The parser splits each line on the first whitespace, then splits the left-hand side on
the first `:` into `(namespace, name)`.  Values are decoded according to `nvram`'s
escape rules (`%XX` hex escapes).

### Windows `[UEFI]::EnumerateVariables() | ConvertTo-Json`

Array of objects with `Namespace`, `Name`, `Value` (base64 or hex byte array), and
`Attributes` fields.  The parser walks the JSON and constructs `UefiVariable` records.

---

## Idempotency Guards

| Method | Guard | Field inspected |
|---|---|---|
| `set_variable` | Current value bytes AND attributes equal desired | `get_variable(name, namespace)` |
| `delete_variable` | Variable does not exist | `get_variable(name, namespace)` |

For `set_variable`, the bytes are compared with a strict equality check; any length or
content mismatch triggers a real write.  When `attributes=None` the check preserves the
current bitmap (see Attribute Preservation below).

---

## Attribute Preservation

Preservation logic for `set_variable` (`attributes=None`):

1. Call `get_variable(name, namespace)`.
2. If the call raises `UefiVariableNotFoundError`, use `DEFAULT_ATTRIBUTES` (`0x07`).
3. If the call raises any other exception, return
   `UefiVariableActionResult(status=FAILED, detail=str(exc))` without writing.
4. Otherwise, use the returned `UefiVariable.attributes` as the write bitmap.

When the caller passes an explicit `attributes` bitmap, the preservation logic is
bypassed.

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `UefiVariableNotFoundError` | `get_variable` targets a missing variable |
| `UefiVariableQueryError` | Any other read failure (base class of `UefiVariableNotFoundError`) |
| `UefiVariableNamespaceError` | Namespace rejected by the current host OS |

All write failures are surfaced as `UefiVariableActionResult` with `status=FAILED`.

---

## Correctness Properties

### Property 1: Get Variable — Linux Command
*For any* `get_variable(name, namespace)` on Linux, the system invokes `efivar -n <namespace>-<name> -p`.
**Validates: Requirements 1.1**

### Property 2: Get Variable — Darwin Command
*For any* `get_variable(name, namespace)` on Darwin, the system invokes `nvram <namespace>:<name>`.
**Validates: Requirements 1.2**

### Property 3: Get Variable — Windows Command
*For any* `get_variable(name, namespace)` on Windows, the system invokes `powershell ... [UEFI]::GetVariable`.
**Validates: Requirements 1.3**

### Property 4: Get Variable Missing
*For any* read against a non-existent variable, the system raises `UefiVariableNotFoundError`.
**Validates: Requirements 1.5**

### Property 5: Get Variable Namespace Rejected
*For any* namespace reserved on the current host OS, the system raises `UefiVariableNamespaceError` without invoking any command.
**Validates: Requirements 1.6**

### Property 6: Set Variable — Linux Command
*For any* `set_variable(name, value, namespace)` on Linux, the system constructs `efivar -n <namespace>-<name> -w -f <binary>` preceded by `chattr -i ...` when an existing variable has the immutable flag.
**Validates: Requirements 2.1, 2.5**

### Property 7: Set Variable — Darwin Command
*For any* `set_variable` on Darwin, the system constructs `nvram <namespace>:<name>=<encoded_value>`.
**Validates: Requirements 2.2**

### Property 8: Set Variable — Windows Command
*For any* `set_variable` on Windows, the system constructs the `[UEFI]::SetVariable(...)` PowerShell command.
**Validates: Requirements 2.3**

### Property 9: Set Variable Idempotency
*For any* request whose value AND attributes match the current variable, `set_variable` returns `SKIPPED`.
**Validates: Requirements 2.4**

### Property 10: Set Variable Attribute Preservation
*For any* `set_variable` with `attributes=None` on a pre-existing variable, the current attribute bitmap is read and reused.
**Validates: Requirements 7.1**

### Property 11: Set Variable Attribute Explicit Override
*For any* `set_variable` with an explicit `attributes` bitmap, the provided bitmap is used unchanged.
**Validates: Requirements 7.2**

### Property 12: Set Variable New-Variable Default
*For any* `set_variable` with `attributes=None` and no pre-existing variable, the system uses `DEFAULT_ATTRIBUTES` (0x07).
**Validates: Requirements 7.3**

### Property 13: Delete Variable — Linux Command
*For any* `delete_variable` on Linux, the system constructs `efivar -n <namespace>-<name> -D` preceded by `chattr -i ...`.
**Validates: Requirements 3.1**

### Property 14: Delete Variable — Darwin Command
*For any* `delete_variable` on Darwin, the system constructs `nvram -d <namespace>:<name>`.
**Validates: Requirements 3.2**

### Property 15: Delete Variable — Windows Command
*For any* `delete_variable` on Windows, the system constructs the `[UEFI]::DeleteVariable(...)` PowerShell command.
**Validates: Requirements 3.3**

### Property 16: Delete Variable Idempotency
*For any* `delete_variable` where the variable does not exist, the result is `SKIPPED`.
**Validates: Requirements 3.4**

### Property 17: List Variables — Linux Command
*For any* `list_variables()` on Linux, the system constructs `efivar -l`.
**Validates: Requirements 4.1**

### Property 18: List Variables Namespace Filter
*For any* `list_variables(namespace=<ns>)`, the result includes only variables whose namespace matches.
**Validates: Requirements 4.2**

### Property 19: List Variables — Darwin / Windows Commands
*For any* `list_variables` call on Darwin (`nvram -p`) or Windows (`[UEFI]::EnumerateVariables()`), the system constructs the corresponding command.
**Validates: Requirements 4.3, 4.4**

### Property 20: Namespace Alias Normalisation
*For any* call passing `namespace="EFI_GLOBAL_VARIABLE"`, the host-OS command receives the canonical GUID.
**Validates: Requirements 5.1**

### Property 21: Full-GUID Pass-Through
*For any* call passing a full lower-cased GUID, the host-OS command receives it verbatim.
**Validates: Requirements 5.2**

### Property 22: Foreign Alias Rejected
*For any* call passing `namespace="Apple"` on Linux, the system raises `UefiVariableNamespaceError`.
**Validates: Requirements 5.3**

### Property 23: Unknown Alias Raises ValueError
*For any* call passing a non-GUID non-alias, the system raises `ValueError` listing the accepted aliases.
**Validates: Requirements 5.4**

### Property 24: Dispatch Override Respected
*For any* `override_host_os` in `{Linux, Darwin, Windows}`, the manager uses it instead of `platform.system()`.
**Validates: Requirements 6.1**

### Property 25: Dispatch Override Rejected
*For any* invalid `override_host_os`, the constructor raises `ValueError`.
**Validates: Requirements 6.3**

### Property 26: Dry-Run Safety
*For any* write operation with `dry_run=True`, the runner is never invoked and the result status is `DRY_RUN`.
**Validates: Requirements 2.9, 3.7**

### Property 27: Elevation for Writes
*For any* write operation with `elevated=True`, the runner is invoked with `elevated=True` — including the `chattr -i` preparatory call on Linux.
**Validates: Requirements 8.1**

### Property 28: No Elevation for Reads
*For any* read operation, the runner is never invoked with `elevated=True`.
**Validates: Requirements 8.2**

### Property 29: Error Result Consistency
*For any* failed write command, the result has `status=FAILED` with `exit_code` and `stderr` populated.
**Validates: Requirements 2.8, 3.6**

### Property 30: Preservation-Read Failure Surfaces
*For any* `set_variable` with `attributes=None` whose preservation-read fails for a reason other than "variable not found", the result is `FAILED` and no write is issued.
**Validates: Requirements 7.4**

---

## Module Layout

```
source/packages/ccai/sysman/uefi/
├── __init__.py
└── efivariables.py          ← UefiVariableManager, config, models, exceptions

source/testroots/testbase/
└── ccai/tests/sysman/uefi/
    └── test_efivariables.py
```

---

## Usage Examples

### Linux — Read-Modify-Write a Boot-Variable Integer

```python
from ccai.sysman.uefi.runners import LocalCommandRunner, LocalRunnerConfig
from ccai.sysman.uefi.efivariables import (
    UefiVariableManager,
    UefiVariableManagerConfig,
    NON_VOLATILE,
    BOOTSERVICE_ACCESS,
    RUNTIME_ACCESS,
)

runner = LocalCommandRunner(LocalRunnerConfig())
config = UefiVariableManagerConfig(elevated=True)
var_mgr = UefiVariableManager(runner=runner, config=config)

current = var_mgr.get_variable(name="Timeout", namespace="EFI_GLOBAL_VARIABLE")
print(f"Current timeout bytes: {current.value.hex()} attributes=0x{current.attributes:02x}")

# Update to 10 seconds (little-endian UINT16)
new_value = (10).to_bytes(2, byteorder="little")
result = var_mgr.set_variable(
    name="Timeout",
    value=new_value,
    namespace="EFI_GLOBAL_VARIABLE",
    attributes=NON_VOLATILE | BOOTSERVICE_ACCESS | RUNTIME_ACCESS,
)
print(f"Set: {result.status}")
```

### macOS — List Variables in the Common Namespace

```python
var_mgr = UefiVariableManager(runner=runner, config=UefiVariableManagerConfig())
entries = var_mgr.list_variables(namespace="Common")
for entry in entries:
    print(f"{entry.namespace}:{entry.name} = {entry.value}")
```

### Windows — Delete a Vendor Variable with Dispatch Override

```python
# Test-time dispatch override
config = UefiVariableManagerConfig(override_host_os="Windows", elevated=True)
var_mgr = UefiVariableManager(runner=mock_runner, config=config)
result = var_mgr.delete_variable(
    name="CustomSetting",
    namespace="12345678-1234-1234-1234-123456789abc",
)
print(f"Delete: {result.status}")
```

---

## Testing Strategy

- All tests use a `MockCommandRunner` keyed on command args prefix.
- `override_host_os` is set in every test.
- `efivar -n ... -p`, `efivar -l`, `nvram` single-variable and `-p` listing, and
  `[UEFI]::GetVariable / SetVariable / DeleteVariable / EnumerateVariables` mock
  responses drive all tests.
- A dedicated test proves namespace alias normalisation for `EFI_GLOBAL_VARIABLE`.
- A dedicated test proves rejection of the `Apple` alias on Linux and Windows.
- A dedicated test proves the immutable-flag dance on Linux (chattr -i before write,
  chattr +i after success).
- A dedicated test proves the `attributes=None` preservation path on `set_variable`.
- No test requires live firmware access.

---

## Scope Mapping

The contract's abstract "namespace" scope concept is mapped per host OS:

| Host OS | Accepted namespace forms | Reserved aliases |
|---|---|---|
| Linux | Full GUID; well-known aliases | Rejects `Apple`, `Common` |
| Darwin | Full GUID; `Apple`; `Common`; well-known GUIDs | — |
| Windows | Full GUID; well-known aliases | Rejects `Apple`, `Common` |

---

## Extensions beyond the contract

- **Linux efivars-immutable handling.** The immutable-flag dance (`chattr -i` before
  write, `chattr +i` after success) is a Linux-specific concession required by the
  efivars filesystem; the base contract permits it but does not mandate it.
- **Attribute preservation on `set_variable`.** The contract mandates preservation
  when `attributes` is omitted; this spec documents the exact `get_variable` read and
  failure-fallback semantics.
- **Well-known alias registry.** Not mandated by the contract; this spec pins the set of
  aliases (`EFI_GLOBAL_VARIABLE`, `EFI_IMAGE_SECURITY_DATABASE_GUID`) accepted on every
  host OS.
- **`override_host_os` testing hook.** Not part of the contract; specific to this
  package's per-host-OS-dispatch architecture.
