# Boot Management Requirements

## User Stories

*   **US-BM-01**: As a system administrator, I want to view the current boot order so that I can verify the system's boot configuration.
*   **US-BM-02**: As a system administrator, I want to change the boot order so that I can prioritize network boot for reimaging.
*   **US-BM-03**: As an automation engineer, I want to set a one-time boot option so that the system boots into a specific environment for the next restart only.
*   **US-BM-04**: As a developer, I want to create and delete boot options programmatically so that I can manage boot entries without manual BIOS interaction.

## Functional Requirements (EARS Notation)

### Boot Order Control

*   **REQ-BM-001 (Ubiquitous)**: The `BootManager` shall provide a method to retrieve the current value of the `BootOrder` UEFI variable.
*   **REQ-BM-002 (Event-Driven)**: When `set_boot_order` is called with a list of boot option IDs, the `BootManager` shall update the `BootOrder` UEFI variable with the provided sequence.
*   **REQ-BM-003 (Ubiquitous)**: The `BootManager` shall provide a method to retrieve the current value of the `BootNext` UEFI variable.
*   **REQ-BM-004 (Event-Driven)**: When `set_boot_next` is called with a boot option ID, the `BootManager` shall update the `BootNext` UEFI variable to that ID.

### Boot Option Management

*   **REQ-BM-005 (Event-Driven)**: When `create_boot_option` is invoked with valid parameters (description, device path), the `BootManager` shall create a new `Boot####` variable with a unique ID.
*   **REQ-BM-006 (Event-Driven)**: When `delete_boot_option` is invoked with a valid boot option ID, the `BootManager` shall remove the corresponding `Boot####` variable.
*   **REQ-BM-007 (Event-Driven)**: When `set_boot_option` is invoked with an updated `BootOption` object, the `BootManager` shall overwrite the existing `Boot####` variable with the new data.
*   **REQ-BM-008 (Ubiquitous)**: The `BootOption` class shall support parsing and serialization of UEFI Device Paths.
*   **REQ-BM-009 (Ubiquitous)**: The `BootOption` class shall support handling of optional data (binary blobs) associated with a boot option.

### One-Time Boot

*   **REQ-BM-010 (Event-Driven)**: When a one-time boot is requested, the `BootManager` shall set the `BootNext` variable to the specified boot option ID.

## Non-Functional Requirements

*   **REQ-BM-NF-001 (Ubiquitous)**: The implementation shall raise a `UEFIVariableError` if the underlying system call to read/write UEFI variables fails.
*   **REQ-BM-NF-002 (Ubiquitous)**: The implementation shall raise an `InvalidBootOptionError` if the binary data for a boot option is malformed.
*   **REQ-BM-NF-003 (Ubiquitous)**: The API shall be Pythonic, using standard types and exceptions where possible.
