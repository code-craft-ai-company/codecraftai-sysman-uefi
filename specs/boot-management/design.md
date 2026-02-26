# Boot Management Design

This document describes the design for the Boot Management feature of `automojo-sysman-uefi`.

## Architecture

The Boot Management feature will be implemented as a module within the `automojo.sysman.uefi` package. It will rely on a lower-level UEFI variable access service (part of the Configuration Management feature, or a shared utility).

### Components

1.  **BootOption Class**:
    *   Represents a single `Boot####` load option.
    *   **Attributes**:
        *   `number`: Integer (the #### part).
        *   `description`: String (boot option description).
        *   `device_path`: Object/String (representation of the device path).
        *   `optional_data`: Bytes (optional data associated with the option).
        *   `attributes`: Integer (attributes like LOAD_OPTION_ACTIVE).
    *   **Methods**:
        *   `pack()`: Serialize the object into bytes suitable for writing to a UEFI variable.
        *   `unpack(bytes)`: Static method or constructor to create an object from raw bytes.

2.  **BootManager Class**:
    *   High-level interface for boot management.
    *   **Methods**:
        *   `get_boot_order()`: Returns a list of `BootOption` numbers (integers).
        *   `set_boot_order(order_list)`: Updates the `BootOrder` variable.
        *   `get_boot_next()`: Returns the `BootNext` value (integer or None).
        *   `set_boot_next(boot_number)`: Sets the `BootNext` variable.
        *   `get_boot_option(boot_number)`: Returns a `BootOption` object.
        *   `set_boot_option(boot_option)`: Writes a `BootOption` to the corresponding `Boot####` variable.
        *   `delete_boot_option(boot_number)`: Deletes the corresponding `Boot####` variable.
        *   `create_boot_option(...)`: Helper to create and write a new boot option, finding an unused number if necessary.

3.  **UEFIVariableInterface (Dependency)**:
    *   This component (likely defined elsewhere) is responsible for the actual syscalls or I/O to read/write UEFI variables (e.g., via `/sys/firmware/efi/efivars` on Linux or via Windows APIs).
    *   `BootManager` will use this interface.

## Data Structures

### Boot#### Variable Format

The `Boot####` variables follow the EFI_LOAD_OPTION format defined in the UEFI Specification:

```c
typedef struct {
    UINT32 Attributes;
    UINT16 FilePathListLength;
    CHAR16 Description[];
    EFI_DEVICE_PATH_PROTOCOL FilePathList[];
    UINT8 OptionalData[];
} EFI_LOAD_OPTION;
```

The `BootOption` class will be responsible for parsing and constructing this binary format.

### BootOrder Variable Format

The `BootOrder` variable is an array of UINT16 values, representing the `Boot####` numbers in order.

## Error Handling

*   `UEFIVariableError`: Custom exception for failures in reading/writing variables.
*   `InvalidBootOptionError`: Custom exception for malformed boot option data.

## Integration

The `BootManager` will be exposed as part of the main `SystemManager` or similar high-level API for the package users.
