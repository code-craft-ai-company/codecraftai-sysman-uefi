============================
Code Craft AI Sysman UEFI
============================

The ``codecraftai-sysman-uefi`` package is the firmware-management tier of the ``codecraftai-sysman-*`` suite. It manages the UEFI firmware on the host the package runs on — boot entries and boot order, Secure Boot state and MOK enrollment, raw EFI NVRAM variables, UEFI capsule firmware updates, and the EFI System Partition — exposed through the same ``ICommandRunner`` / ``IFileOperator`` interfaces and ``Aspects``-style behaviour parameters used elsewhere in the suite.

Unlike the per-OS ``codecraftai-sysman-*`` packages, each manager in this package detects the running host OS at construction time and internally dispatches every verb to a host-OS-specific command builder (``efibootmgr`` / ``mokutil`` / ``efivar`` / ``fwupdmgr`` on Linux, ``bless`` / ``nvram`` / ``bputil`` / ``diskutil`` on macOS, ``bcdedit`` / PowerShell ``Uefi`` module / ``PSWindowsUpdate`` / ``mountvol`` on Windows).

Status
======

5 firmware features implemented (boot-management, secure-boot, efi-variables, capsule-update, esp-management). Test suite: 175/175 passing under ``testbase``. See ``specs/3-<feature>/`` for the per-OS requirements/design/tasks for each feature.

Highlights
==========

- ``UefiBootManagementManager`` — firmware boot entries and persistent boot order.
- ``UefiSecureBootManager`` — Secure Boot state, policy, and MOK management.
- ``UefiEfiVariablesManager`` — raw EFI NVRAM variable read/write/delete/list.
- ``UefiCapsuleUpdateManager`` — UEFI firmware capsule updates (fwupd family).
- ``UefiEspManagementManager`` — EFI System Partition discovery, mount, file I/O.
- ``override_host_os`` config field on every manager so dispatch paths can be exercised from any developer workstation.

Repository Structure
====================

- ``development/`` - Runtime environment scripts.
- ``repository-setup/`` - Repository homing and setup scripts.
- ``source/packages/ccai/sysman/uefi/`` - Implementation modules.
- ``source/testroots/testbase/ccai/tests/sysman/uefi/`` - testbase test suite.
- ``specs/`` - Feature specifications organised by phase and domain area.
- ``userguide/`` - Standards, conventions, and documentation.

Environment Setup
=================

When working in a Codex container:

.. code-block:: bash

   ./development/setup-agent-container

For local development:

.. code-block:: bash

   ./repository-setup/rehome-repository
   ./development/setup-environment

Running Tests
=============

Activate the virtual environment and run the testbase suite:

.. code-block:: bash

   source .venv/bin/activate
   testbase testing run --root source/testroots/testbase \
       --includes=testbase.ccai.tests.sysman.uefi \
       --output=./.output

Standards and Conventions
=========================

- Naming conventions: ``userguide/10-02-naming-conventions.rst``
- Code organization: ``userguide/10-01-code-organization.rst``
- Coding standards: ``userguide/10-00-coding-standards.rst``

Additional Resources
====================

- User guide entry point: ``userguide/userguide.rst``
- Cross-OS contracts: ``../codecraftai-sysman-common/specs/contracts/``
