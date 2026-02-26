# Boot Management Requirements

This document outlines the requirements for the Boot Management feature of the `automojo-sysman-uefi` package.

## Functional Requirements

### Boot Order Control

*   **REQ-BM-001**: The system shall provide a mechanism to read the current `BootOrder` UEFI variable.
*   **REQ-BM-002**: The system shall provide a mechanism to modify the `BootOrder` UEFI variable to change the boot sequence.
*   **REQ-BM-003**: The system shall provide a mechanism to read the `BootNext` UEFI variable.
*   **REQ-BM-004**: The system shall provide a mechanism to modify the `BootNext` UEFI variable.

### Boot Option Management

*   **REQ-BM-005**: The system shall be able to create new `Boot####` load options.
*   **REQ-BM-006**: The system shall be able to delete existing `Boot####` load options.
*   **REQ-BM-007**: The system shall be able to modify existing `Boot####` load options.
*   **REQ-BM-008**: The system shall support handling of device paths for `Boot####` options.
*   **REQ-BM-009**: The system shall support handling of optional data for `Boot####` options.

### One-Time Boot

*   **REQ-BM-010**: The system shall support setting the "Next Boot" option (`BootNext`) to facilitate one-time boot scenarios (e.g., seamless reimaging).

## Non-Functional Requirements

*   **REQ-BM-NF-001**: The implementation should abstract low-level UEFI interactions into a Pythonic API.
*   **REQ-BM-NF-002**: The implementation must be robust and handle errors gracefully (e.g., invalid variable formats).
