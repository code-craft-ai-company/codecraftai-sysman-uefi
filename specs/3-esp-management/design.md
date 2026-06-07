# Design

## Overview

The esp-management feature is implemented as a single module,
`ccai.sysman.uefi.espmanagement`, that exposes a `UefiEspManager` class.  This
spec implements the esp-management contract defined at
`codecraftai-sysman-common/specs/contracts/esp-management/contract.md` for UEFI
firmware management, dispatching to host-OS tooling (`findmnt`, `lsblk`, `mount` on
Linux, `diskutil` on macOS, `Get-Partition`, `Get-Volume`, `mountvol` on Windows).

The coordinator instantiates `UefiEspManager` and calls its methods to locate the ESP,
mount and unmount it, enumerate its contents, and read / write / delete files on the
mounted ESP.  The manager combines an `ICommandRunner` (for mount operations and
partition discovery) with an `IFileOperator` (for file I/O).  The manager detects the
running host OS at construction time and selects a per-host-OS command builder for every
mount-related verb.  Once the ESP is mounted, file I/O is host-OS-agnostic because
`IFileOperator` abstracts the operating-system file-I/O surface.

All system interaction is delegated to either the command runner or the file operator.
No direct file-system access or `subprocess` calls are made inside the module.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner + file operator                      │
│  - Injects both into UefiEspManager                         │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            UefiEspManager                                   │
│  ccai.sysman.uefi.espmanagement                         │
│                                                             │
│  get_esp_info()         is_esp_mounted()                    │
│  mount_esp(mountpoint)  unmount_esp(mountpoint)             │
│  list_esp_contents(path)                                    │
│  read_esp_file(path)    write_esp_file(path, content)       │
│  delete_esp_file(path)                                      │
└────────────────────────┬────────────────────────────────────┘
                         │
       ┌─────────────────┴─────────────────┐
       │                                   │
       ▼                                   ▼
 ICommandRunner                      IFileOperator
 (mount / discover)                  (read / write on mounted ESP)
       │                                   │
 ┌─────┴─────┐                             │
 ▼           ▼                             │
Linux     Darwin    Windows                │
(findmnt, (diskutil) (Get-Partition,       │
 lsblk,               mountvol)            │
 mount)                                    │
       │                                   │
       └───────────────┬───────────────────┘
                       │  aspects compose on runner
                       ▼
              PersistenceAspect, RetryAspect, TimeoutAspect
```

---

## Host-OS Dispatch

`UefiEspManager` manages the EFI System Partition on the host it runs on.

```
              UefiEspManager
                     │
           platform.system() or config.override_host_os
                     │
         ┌───────────┼───────────┐
         ▼           ▼           ▼
       Linux       Darwin      Windows
   (findmnt,     (diskutil)   (Get-Partition,
    lsblk,                     Get-Volume,
    mount,                     mountvol)
    umount)
         │           │           │
         └───────────┼───────────┘
                     ▼
             ICommandRunner.run()

         (file I/O, all host OSes)
                     │
                     ▼
              IFileOperator
              (.list_directory, .read_bytes,
               .write_bytes, .delete)
```

- **Linux** — Full feature coverage.  `findmnt` + `lsblk` discovers the ESP; `mount` /
  `umount` manage the mount.
- **Darwin (macOS)** — Reads work cleanly through `diskutil`.  Writes (mount / unmount)
  work but note that Apple automatically mounts/unmounts the ESP around firmware
  updates, so manual mount management is rarely needed.  File I/O on the mounted ESP
  works through `IFileOperator` the same way it does on Linux.
- **Windows** — Mount management uses drive letters via `mountvol`.  Path translation
  between Windows (`S:\EFI\...`) and cross-OS (`/EFI/...`) happens inside the file
  operator.

On unsupported host OSes, all verbs raise `UefiEspQueryError` (reads) or return
`FAILED` (writes).  The `UefiEspManagerConfig.override_host_os` field forces a specific
dispatch path for testing.

---

## Components

### `UefiEspManager`

**Location:** `source/packages/ccai/sysman/uefi/espmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `get_esp_info` | `() → UefiEspInfo` | Locate the ESP |
| `mount_esp` | `(mountpoint) → UefiEspActionResult` | Mount the ESP |
| `unmount_esp` | `(mountpoint) → UefiEspActionResult` | Unmount the ESP |
| `is_esp_mounted` | `() → bool` | Query mount state |
| `list_esp_contents` | `(path) → list[str]` | Enumerate files at `<mount>/<path>` |
| `read_esp_file` | `(path) → bytes` | Read a file at `<mount>/<path>` |
| `write_esp_file` | `(path, content) → UefiEspActionResult` | Write a file at `<mount>/<path>` |
| `delete_esp_file` | `(path) → UefiEspActionResult` | Delete a file at `<mount>/<path>` |

### `UefiEspManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no mutations execute |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |
| `override_host_os` | `str \| None` | `None` | Forces dispatch path for testing |

---

## Data Models

```python
class UefiEspActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"

@dataclass
class UefiEspActionResult:
    status: UefiEspActionStatus
    path: str | None
    detail: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None

@dataclass
class UefiEspInfo:
    detected: bool
    device: str
    mountpoint: str | None
    fstype: str
    size_bytes: int
    uuid: str | None
    partition_guid: str | None

class UefiEspQueryError(Exception):
    """Raised when an ESP discovery or read verb fails."""

class UefiEspNotMountedError(UefiEspQueryError):
    """Raised by read verbs that require the ESP to be mounted."""
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

### `IFileOperator` (from `codecraftai-interfaces`)

```python
class IFileOperator(Protocol):
    def list_directory(self, path: str, elevated: bool = False) -> list[str]: ...
    def read_bytes(self, path: str, elevated: bool = False) -> bytes: ...
    def write_bytes(self, path: str, content: bytes, elevated: bool = False) -> None: ...
    def delete(self, path: str, elevated: bool = False) -> None: ...
    def exists(self, path: str) -> bool: ...
```

---

## Command Mappings

### Linux

| Verb | Command |
|---|---|
| `get_esp_info` | `findmnt /boot/efi --json`; `lsblk -f --json` |
| `mount_esp` | `mount -t vfat <device> <mountpoint>` (or `mount <mountpoint>` when fstab entry exists) |
| `unmount_esp` | `umount <mountpoint>` |
| `is_esp_mounted` | `findmnt /boot/efi` (exit code) |
| file I/O verbs | via `IFileOperator` |

### Darwin

| Verb | Command |
|---|---|
| `get_esp_info` | `diskutil list -plist`; filter `EFI` partition |
| `mount_esp` | `diskutil mount <esp_device>` |
| `unmount_esp` | `diskutil unmount <mountpoint>` |
| `is_esp_mounted` | `diskutil info <esp_device>`; parse `Mounted:` |
| file I/O verbs | via `IFileOperator` |

### Windows

| Verb | Command |
|---|---|
| `get_esp_info` | `powershell -NoProfile -Command "Get-Partition \| Where-Object { $_.Type -eq 'System' } \| ConvertTo-Json"`; `Get-Volume` |
| `mount_esp` | `mountvol <letter>: /S` |
| `unmount_esp` | `mountvol <letter>: /D` |
| `is_esp_mounted` | check drive letter in `get_esp_info()` |
| file I/O verbs | via `IFileOperator` (Windows path translation) |

All write commands pass `elevated=True` when the config enables it.  Read commands never
pass `elevated=True`.

---

## Output Parsing

### Linux `findmnt /boot/efi --json`

```json
{
  "filesystems": [
    {"target": "/boot/efi", "source": "/dev/sda1", "fstype": "vfat", "options": "rw,relatime,..."}
  ]
}
```

Parser pulls `target`, `source`, `fstype` into `UefiEspInfo`.

### Linux `lsblk -f --json`

Used to fetch `UUID` and `PARTUUID` fields for the ESP device; correlated with
`findmnt` output by `source`.

### Darwin `diskutil list -plist`

Plist representation of all partitions; parser walks the `AllDisksAndPartitions` array
looking for `Content == "EFI"` to populate `device`, `size_bytes`, `partition_guid`.

### Windows `Get-Partition | ConvertTo-Json`

Array of partition objects; parser filters for `Type == "System"` and correlates with a
subsequent `Get-Volume` call for the `DriveLetter` → `mountpoint` field.

---

## Idempotency Guards

| Method | Guard | Field inspected |
|---|---|---|
| `mount_esp` | ESP already mounted at requested mountpoint | `get_esp_info().mountpoint` |
| `unmount_esp` | Mountpoint is not currently an ESP mount | `get_esp_info().mountpoint` |
| `write_esp_file` | Existing file byte-identical to desired content | `IFileOperator.read_bytes` |
| `delete_esp_file` | Path does not exist | `IFileOperator.exists` |

---

## Path Normalisation

ESP paths passed to file-I/O verbs are normalised to the host-OS convention before
being passed to `IFileOperator`:

| Input | Linux / macOS | Windows |
|---|---|---|
| `/EFI/ubuntu/shimx64.efi` | `<mountpoint>/EFI/ubuntu/shimx64.efi` | `<drive>:\EFI\ubuntu\shimx64.efi` |
| `EFI\ubuntu\shimx64.efi` | `<mountpoint>/EFI/ubuntu/shimx64.efi` | `<drive>:\EFI\ubuntu\shimx64.efi` |

Forward slashes and back slashes are both accepted in input and translated to the
host-OS separator.

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `UefiEspQueryError` | Discovery or read-verb failure |
| `UefiEspNotMountedError` | Read verb requires the ESP to be mounted but it is not (subclass of `UefiEspQueryError`) |

All write failures (including "ESP not mounted" for `write_esp_file` /
`delete_esp_file`) are surfaced as `UefiEspActionResult` with `status=FAILED`.

---

## Correctness Properties

### Property 1: Get ESP Info — Linux Command
*For any* `get_esp_info()` on Linux, the system invokes `findmnt /boot/efi --json` and `lsblk -f --json`.
**Validates: Requirements 1.1**

### Property 2: Get ESP Info — Darwin Command
*For any* `get_esp_info()` on Darwin, the system invokes `diskutil list -plist`.
**Validates: Requirements 1.2**

### Property 3: Get ESP Info — Windows Command
*For any* `get_esp_info()` on Windows, the system invokes `Get-Partition` filtered to `Type=System` and `Get-Volume`.
**Validates: Requirements 1.3**

### Property 4: Get ESP Info — No ESP Detected
*For any* host without an ESP, `get_esp_info()` returns `UefiEspInfo(detected=False, ...)` rather than raising.
**Validates: Requirements 1.4**

### Property 5: Get ESP Info Errors Raise
*For any* discovery-command failure that is not a "no ESP" signal, the system raises `UefiEspQueryError`.
**Validates: Requirements 1.5**

### Property 6: Mount — Linux Command
*For any* `mount_esp(mountpoint)` on Linux, the system constructs `mount -t vfat <device> <mountpoint>`.
**Validates: Requirements 2.1**

### Property 7: Mount — Darwin Command
*For any* `mount_esp(mountpoint)` on Darwin, the system constructs `diskutil mount <esp_device>`.
**Validates: Requirements 2.2**

### Property 8: Mount — Windows Command
*For any* `mount_esp("S:")` on Windows, the system constructs `mountvol S: /S`.
**Validates: Requirements 2.3**

### Property 9: Mount Idempotency
*For any* request to mount the ESP at a mountpoint where it is already mounted, the result is `SKIPPED`.
**Validates: Requirements 2.4**

### Property 10: Unmount — Linux Command
*For any* `unmount_esp(mountpoint)` on Linux, the system constructs `umount <mountpoint>`.
**Validates: Requirements 3.1**

### Property 11: Unmount — Darwin Command
*For any* `unmount_esp(mountpoint)` on Darwin, the system constructs `diskutil unmount <mountpoint>`.
**Validates: Requirements 3.2**

### Property 12: Unmount — Windows Command
*For any* `unmount_esp("S:")` on Windows, the system constructs `mountvol S: /D`.
**Validates: Requirements 3.3**

### Property 13: Unmount Idempotency
*For any* request to unmount a path that is not currently an ESP mount, the result is `SKIPPED`.
**Validates: Requirements 3.4**

### Property 14: Is Mounted — Linux
*For any* `is_esp_mounted()` on Linux, the system invokes `findmnt /boot/efi` and returns `True` on exit-code zero.
**Validates: Requirements 4.1**

### Property 15: List Contents Mount Precondition
*For any* `list_esp_contents(path)` when the ESP is not mounted, the system raises `UefiEspNotMountedError`.
**Validates: Requirements 5.2**

### Property 16: List Contents Delegates to File Operator
*For any* `list_esp_contents(path)` with the ESP mounted, the system invokes `IFileOperator.list_directory(<mount>/<path>)`.
**Validates: Requirements 5.1**

### Property 17: Read File Mount Precondition
*For any* `read_esp_file(path)` when the ESP is not mounted, the system raises `UefiEspNotMountedError`.
**Validates: Requirements 6.2**

### Property 18: Read File Delegates to File Operator
*For any* `read_esp_file(path)` with the ESP mounted, the system invokes `IFileOperator.read_bytes(<mount>/<path>)`.
**Validates: Requirements 6.1**

### Property 19: Write File Precondition Fails Not-Mounted
*For any* `write_esp_file(path, content)` with the ESP not mounted, the system returns `FAILED` with a not-mounted detail.
**Validates: Requirements 7.2**

### Property 20: Write File Idempotency
*For any* `write_esp_file(path, content)` where the existing file is byte-identical to `content`, the result is `SKIPPED`.
**Validates: Requirements 7.3**

### Property 21: Write File Delegates to File Operator
*For any* `write_esp_file(path, content)` with the ESP mounted and bytes differing, the system invokes `IFileOperator.write_bytes(<mount>/<path>, content)`.
**Validates: Requirements 7.1**

### Property 22: Delete File Idempotency
*For any* `delete_esp_file(path)` where the path does not exist, the result is `SKIPPED`.
**Validates: Requirements 8.3**

### Property 23: Delete File Delegates to File Operator
*For any* `delete_esp_file(path)` with the ESP mounted and the path existing, the system invokes `IFileOperator.delete(<mount>/<path>)`.
**Validates: Requirements 8.1**

### Property 24: Dispatch Override Respected
*For any* `override_host_os` in `{Linux, Darwin, Windows}`, the manager uses it instead of `platform.system()`.
**Validates: Requirements 9.1**

### Property 25: Dispatch Override Rejected
*For any* invalid `override_host_os`, the constructor raises `ValueError`.
**Validates: Requirements 9.3**

### Property 26: Dry-Run Safety
*For any* write operation with `dry_run=True`, neither the runner nor the file operator is invoked and the result status is `DRY_RUN`.
**Validates: Requirements 2.7, 3.7, 7.7, 8.6**

### Property 27: Elevation for Writes
*For any* write operation with `elevated=True`, the runner and file operator are invoked with `elevated=True`.
**Validates: Requirements 10.1**

### Property 28: No Elevation for Reads
*For any* read operation, neither the runner nor the file operator is invoked with `elevated=True`.
**Validates: Requirements 10.2**

### Property 29: Error Result Consistency
*For any* failed write command, the `UefiEspActionResult` has `status=FAILED` with `exit_code` and `stderr` populated when available.
**Validates: Requirements 2.6, 3.6, 7.6, 8.5**

### Property 30: Path Normalisation
*For any* input path with `\` separators, the file-operator call receives the host-OS-native separator form.
**Validates: Implementation concern derived from Requirements 5, 6, 7, 8**

---

## Module Layout

```
source/packages/ccai/sysman/uefi/
├── __init__.py
└── espmanagement.py          ← UefiEspManager, config, models, exceptions

source/testroots/testbase/
└── ccai/tests/sysman/uefi/
    └── test_espmanagement.py
```

---

## Usage Examples

### Linux — Mount, Write a Loader, Unmount

```python
from ccai.sysman.uefi.runners import LocalCommandRunner, LocalRunnerConfig
from ccai.sysman.uefi.fileoperators import LocalFileOperator
from ccai.sysman.uefi.espmanagement import (
    UefiEspManager,
    UefiEspManagerConfig,
)

runner = LocalCommandRunner(LocalRunnerConfig())
file_op = LocalFileOperator()
config = UefiEspManagerConfig(elevated=True)
esp_mgr = UefiEspManager(runner=runner, file_operator=file_op, config=config)

info = esp_mgr.get_esp_info()
print(f"ESP device={info.device} fstype={info.fstype} mount={info.mountpoint}")

if not esp_mgr.is_esp_mounted():
    esp_mgr.mount_esp(mountpoint="/boot/efi")

with open("/tmp/new-shim.efi", "rb") as f:
    new_loader_bytes = f.read()

result = esp_mgr.write_esp_file(
    path="/EFI/custom/shimx64.efi",
    content=new_loader_bytes,
)
print(f"Write: {result.status}")

esp_mgr.unmount_esp(mountpoint="/boot/efi")
```

### macOS — Inspect an Already-Mounted ESP

```python
esp_mgr = UefiEspManager(runner=runner, file_operator=file_op, config=UefiEspManagerConfig())
info = esp_mgr.get_esp_info()
if info.detected and esp_mgr.is_esp_mounted():
    entries = esp_mgr.list_esp_contents(path="/EFI")
    for entry in entries:
        print(entry)
```

### Windows — Assign a Drive Letter and Write

```python
config = UefiEspManagerConfig(
    override_host_os="Windows",
    elevated=True,
)
esp_mgr = UefiEspManager(runner=runner, file_operator=file_op, config=config)
esp_mgr.mount_esp(mountpoint="S:")
esp_mgr.write_esp_file(path=r"\EFI\Boot\bootx64.efi", content=b"...loader bytes...")
esp_mgr.unmount_esp(mountpoint="S:")
```

---

## Testing Strategy

- All tests use a `MockCommandRunner` keyed on command args prefix and a
  `MockFileOperator` for the file-I/O verbs.
- `override_host_os` is set in every test.
- `findmnt /boot/efi --json`, `lsblk -f --json`, `diskutil list -plist`, and the two
  PowerShell pipelines have fixture inputs.
- A dedicated test proves `list_esp_contents`, `read_esp_file`, `write_esp_file`, and
  `delete_esp_file` respect the mount precondition.
- A dedicated test proves byte-match idempotency on `write_esp_file`.
- A dedicated test proves path normalisation translates between `/` and `\`.
- A dedicated test proves writes pass `elevated=True` to both the runner and the file
  operator.
- No test requires live ESP access.

---

## Scope Mapping

The contract's scope note is that there is exactly one ESP per host.  This manager
honors that — there is no "ESP ID" parameter in any verb.  Discovery is implicit in
`get_esp_info()`.

| Host OS | Mountpoint form | Device form |
|---|---|---|
| Linux | Filesystem path (e.g., `/boot/efi`) | `/dev/sdX1` |
| Darwin | Filesystem path (e.g., `/Volumes/EFI`) | `disk0s1` |
| Windows | Drive letter (e.g., `S:`) | GPT partition GUID |

---

## Extensions beyond the contract

- **`IFileOperator` delegation.** The base contract lists `IFileOperator` as an
  implementation detail that "MUST" be used for ESP file I/O.  This spec is explicit
  about which verbs delegate and how path translation occurs, and about how
  `IFileOperator` errors surface as `UefiEspActionResult.status=FAILED` with a
  file-operator-message `detail`.
- **Byte-identical write idempotency.** The contract mandates this but does not pin
  how the comparison is performed; this spec specifies a raw `bytes ==` comparison on
  the `read_bytes` result.
- **Path normalisation.** Accepting both `/` and `\` separators on any host input is
  not required by the contract; this spec adds it for ergonomic cross-OS scripting.
- **`override_host_os` testing hook.** Not part of the contract; specific to this
  package's per-host-OS-dispatch architecture.
