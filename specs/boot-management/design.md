# Boot Management Design

## System Architecture

The Boot Management feature is designed as a module within the `automojo.sysman.uefi` package. It provides a high-level Python API to interact with UEFI variables related to boot configuration (`BootOrder`, `BootNext`, `Boot####`).

### Component Diagram

```mermaid
classDiagram
    class BootManager {
        +get_boot_order() List[int]
        +set_boot_order(List[int])
        +get_boot_next() int
        +set_boot_next(int)
        +get_boot_option(int) BootOption
        +create_boot_option(...) BootOption
        +delete_boot_option(int)
    }

    class BootOption {
        +int number
        +str description
        +bytes device_path
        +bytes optional_data
        +int attributes
        +pack() bytes
        +unpack(bytes) BootOption
    }

    class UEFIVariableInterface {
        <<interface>>
        +read_variable(name, guid) bytes
        +write_variable(name, guid, attributes, data)
    }

    BootManager --> BootOption : uses
    BootManager --> UEFIVariableInterface : uses
```

### Key Components

1.  **`BootManager`**: The primary entry point for consumers. It orchestrates reading/writing variables using the `UEFIVariableInterface` and parsing/serializing data using `BootOption`.
2.  **`BootOption`**: A data class responsible for representing a single boot entry (`Boot####`). It handles the binary format defined by the UEFI specification (EFI_LOAD_OPTION).
3.  **`UEFIVariableInterface`**: An abstraction layer for the OS-specific mechanism to access UEFI variables (e.g., Linux efivarfs, Windows GetFirmwareEnvironmentVariable).

## Sequence Diagrams

### Reading Boot Order

```mermaid
sequenceDiagram
    participant User
    participant BM as BootManager
    participant UVI as UEFIVariableInterface

    User->>BM: get_boot_order()
    BM->>UVI: read_variable("BootOrder", GLOBAL_GUID)
    UVI-->>BM: raw_bytes (UINT16[])
    BM->>BM: specific parse logic (unpack UINT16 array)
    BM-->>User: List[int]
```

### Creating a New Boot Option

```mermaid
sequenceDiagram
    participant User
    participant BM as BootManager
    participant BO as BootOption
    participant UVI as UEFIVariableInterface

    User->>BM: create_boot_option(description, path)
    BM->>BM: Find next available ID (e.g., 0005)
    BM->>BO: new(0005, description, path)
    BO-->>BM: boot_option_obj
    BM->>BO: pack()
    BO-->>BM: raw_bytes
    BM->>UVI: write_variable("Boot0005", GLOBAL_GUID, raw_bytes)
    BM-->>User: boot_option_obj
```

## Data Structures

### `Boot####` Variable Format (EFI_LOAD_OPTION)

| Field | Type | Description |
| :--- | :--- | :--- |
| Attributes | UINT32 | Bitmask (Active, Hidden, etc.) |
| FilePathListLength | UINT16 | Length of the device path in bytes |
| Description | CHAR16[] | Null-terminated string (UCS-2) |
| FilePathList | EFI_DEVICE_PATH_PROTOCOL[] | The device path structure |
| OptionalData | UINT8[] | Remaining bytes |

### `BootOrder` Variable Format

Array of `UINT16` values representing the BootOption numbers.

## Error Handling Strategy

*   **`UEFIVariableError`**: Raised when the underlying OS call fails (e.g., permission denied, variable not found).
    *   *Mitigation*: Ensure user has appropriate privileges (root/admin). The library should check this or handle `EACCES`/`EPERM` gracefully.
*   **`InvalidBootOptionError`**: Raised when parsing `Boot####` data fails (e.g., length mismatch, invalid format).
    *   *Mitigation*: Validate lengths before parsing. Log warning and skip malformed entries during enumeration.

## Testing Strategy

1.  **Unit Tests**:
    *   Mock `UEFIVariableInterface` to simulate reading/writing variables without touching real hardware.
    *   Test `BootOption.pack()` and `BootOption.unpack()` with known binary vectors (captured from real systems).
    *   Test `BootManager` logic (e.g., finding free IDs, reordering lists).

2.  **Integration Tests**:
    *   Run on a virtual machine (e.g., QEMU/KVM with OVMF) to verify interaction with a real UEFI implementation.
    *   *Caution*: Modification of `BootOrder` on a real machine can render it unbootable. Integration tests should be careful or run in a safe environment (e.g., only modifying a test variable or restoring state immediately).
