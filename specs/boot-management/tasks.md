# Boot Management Tasks

This document lists the discrete, trackable tasks required to implement the Boot Management feature.

## Phase 1: Foundation (UEFI Variable Access)

- [ ] **Task 1.1**: Define the `UEFIVariableInterface` abstraction (abstract base class).
    - *Goal*: Establish the contract for reading/writing UEFI variables.
- [ ] **Task 1.2**: Implement a mock `UEFIVariableInterface` for unit testing.
    - *Goal*: Enable test-driven development without real hardware access.
- [ ] **Task 1.3**: Implement a concrete `UEFIVariableInterface` for Linux (`efivarfs`).
    - *Goal*: Support Linux-based systems.
- [ ] **Task 1.4**: (Optional) Implement a concrete `UEFIVariableInterface` for Windows (`GetFirmwareEnvironmentVariable`).
    - *Goal*: Support Windows-based systems (if required).

## Phase 2: Data Structures and Parsing (BootOption)

- [ ] **Task 2.1**: Implement `BootOption` class structure and attributes.
    - *Goal*: Represent a `Boot####` option in Python.
- [ ] **Task 2.2**: Implement `BootOption.unpack()` method.
    - *Goal*: Parse raw binary data from a `Boot####` variable into a `BootOption` object.
    - *Sub-task*: Handle variable length strings (Description) and device paths.
- [ ] **Task 2.3**: Implement `BootOption.pack()` method.
    - *Goal*: Serialize a `BootOption` object back into binary format for writing.
- [ ] **Task 2.4**: Create unit tests for `BootOption` packing/unpacking using sample binary data.
    - *Goal*: Verify correctness of data handling.

## Phase 3: Boot Manager Logic (BootManager)

- [ ] **Task 3.1**: Implement `BootManager` class skeleton and dependency injection (take `UEFIVariableInterface`).
    - *Goal*: Create the main controller class.
- [ ] **Task 3.2**: Implement `get_boot_order()` method.
    - *Goal*: Read and parse the `BootOrder` variable (array of UINT16).
- [ ] **Task 3.3**: Implement `set_boot_order()` method.
    - *Goal*: Update the `BootOrder` variable with a new sequence.
- [ ] **Task 3.4**: Implement `get_boot_next()` and `set_boot_next()` methods.
    - *Goal*: Manage the `BootNext` variable for one-time boot.
- [ ] **Task 3.5**: Implement `get_boot_option(id)` method.
    - *Goal*: Retrieve a specific boot option by ID.
- [ ] **Task 3.6**: Implement `set_boot_option(boot_option)` method.
    - *Goal*: Write a boot option to the variable store.
- [ ] **Task 3.7**: Implement `delete_boot_option(id)` method.
    - *Goal*: Remove a boot option and optionally update `BootOrder`.

## Phase 4: Integration and Advanced Features

- [ ] **Task 4.1**: Implement `create_boot_option(...)` helper method.
    - *Goal*: Automate the process of finding an available ID and creating a new option.
- [ ] **Task 4.2**: Implement Device Path handling utilities.
    - *Goal*: Construct common device paths (e.g., HD, PXE) from user-friendly inputs.
- [ ] **Task 4.3**: Integrate `BootManager` with the main `SystemManager` API.
    - *Goal*: Expose functionality to the package users.

## Phase 5: Documentation and Final Review

- [ ] **Task 5.1**: Update User Guide with Boot Management usage examples.
    - *Goal*: Provide clear instructions for end-users.
- [ ] **Task 5.2**: Perform final code review and cleanup.
    - *Goal*: Ensure code quality and adherence to standards.
