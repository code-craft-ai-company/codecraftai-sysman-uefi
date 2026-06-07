# Implementation Plan — UEFI EFI Variables

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `ccai.sysman.uefi.efivariables` module

- [x] 2. Define `UefiVariableActionStatus` enum (SUCCESS / FAILED /
  SKIPPED / DRY_RUN)

- [x] 3. Define `UefiVariableManagerConfig` dataclass (dry_run,
  elevated, override_host_os)

- [x] 4. Define `UefiVariableActionResult` and `UefiVariable`
  dataclasses (plus `UefiVariableInfo` for listing-shape returns)

- [x] 5. Define attribute-bit module-level constants
  (NON_VOLATILE, BOOTSERVICE_ACCESS, RUNTIME_ACCESS, ...,
  DEFAULT_ATTRIBUTES)

- [x] 6. Define `UefiVariableQueryError`, `UefiVariableNotFoundError`,
  and `UefiVariableNamespaceError` exceptions

## Namespace Registry

- [x] 7. Implement the well-known-alias registry
  (EFI_GLOBAL_VARIABLE, EFI_IMAGE_SECURITY_DATABASE_GUID, Apple,
  Common)

- [x] 8. Implement `normalise_namespace(host_os, namespace)` that
  rejects host-OS-reserved aliases

## Host-OS Dispatch

- [x] 9. Implement construction-time host-OS detection via
  `platform.system()` with `override_host_os` override

- [x] 10. Implement `_dispatch_linux`, `_dispatch_darwin`,
  `_dispatch_windows` private per-method command builders

- [x] 11. Implement per-host-OS output parsers:
  `_parse_efivar_variable`, `_parse_efivar_l`,
  `_parse_nvram_listing`, `_parse_nvram_single_variable`,
  `_parse_powershell_uefi_variable`,
  `_parse_powershell_uefi_listing`

## UefiVariableManager — Read verbs

- [x] 12. Implement `get_variable(name, namespace)` — all three
  host-OS paths plus `UefiVariableNotFoundError` on miss

- [x] 13. Implement `list_variables(namespace)` — all three host-OS
  paths plus namespace filtering

## UefiVariableManager — Write verbs

- [x] 14. Implement `set_variable(name, value, namespace, attributes)`
  — all three host-OS paths plus Linux chattr dance

- [x] 15. Implement attribute-preservation logic for
  `attributes=None` (internal `get_variable` + failure fallback).
  When the preservation read fails for a reason other than "not found",
  the write returns `FAILED` with a stable
  `attribute_preservation_read_failed` stderr token rather than
  silently using `DEFAULT_ATTRIBUTES`.

- [x] 16. Implement `delete_variable(name, namespace)` — all three
  host-OS paths plus Linux chattr dance

## Tests — Command construction

- [x] 17. `test_get_variable_linux_invokes_efivar_p`
- [x] 18. `test_get_variable_darwin_invokes_nvram_namespace_name`
- [x] 19. `test_get_variable_windows_invokes_powershell_getvariable`
- [x] 20. `test_set_variable_linux_chattr_minus_i_then_efivar_w`
- [x] 21. `test_set_variable_darwin_invokes_nvram_assignment`
- [x] 22. `test_set_variable_windows_invokes_powershell_setvariable`
- [x] 23. `test_delete_variable_linux_chattr_minus_i_then_efivar_d`
- [x] 24. `test_delete_variable_darwin_invokes_nvram_d`
- [x] 25. `test_delete_variable_windows_invokes_powershell_deletevariable`
- [x] 26. `test_list_variables_linux_invokes_efivar_l`

## Tests — Idempotency / DRY_RUN / elevation

- [x] 27. `test_set_variable_skipped_when_value_and_attributes_match`
- [x] 28. `test_delete_variable_skipped_when_variable_absent`
- [x] 29. `test_set_variable_preserves_attributes_when_none`
- [x] 30. `test_set_variable_new_variable_uses_default_attributes`
- [x] 31. `test_dry_run_short_circuits_set_and_delete`
- [x] 32. `test_writes_pass_elevated_true_when_configured`
- [x] 33. `test_reads_never_pass_elevated_true`

## Tests — Namespace normalisation / dispatch

- [x] 34. `test_namespace_efi_global_alias_normalised_to_guid`
- [x] 35. `test_namespace_apple_rejected_on_linux_and_windows`
  (split into `test_namespace_apple_rejected_on_linux` and
  `test_namespace_apple_rejected_on_windows`)
- [x] 36. `test_namespace_non_guid_non_alias_raises_value_error`
- [x] 37. `test_override_host_os_forces_linux_dispatch`
- [x] 38. `test_override_host_os_rejects_invalid_value_in_constructor`

## Tests — Parsing

- [x] 39. `test_parse_efivar_variable_decodes_hex_dump_and_attributes`
- [x] 40. `test_parse_efivar_l_splits_guid_and_name`
- [x] 41. `test_parse_nvram_decodes_percent_escapes`
- [x] 42. `test_parse_powershell_uefi_json_builds_variable_records`
  (covered by `test_list_variables_windows_invokes_powershell_enumerate`)

## Follow-ups

- [ ] 43. **Cross-OS contract conformance review**: verify that this
  implementation's `UefiVariableActionResult` / `UefiVariable` field
  shapes and the `namespace`/`attributes` semantics match the
  contract at
  `codecraftai-sysman-common/specs/contracts/efi-variables/contract.md`.
  Initial drift notes:
    - `UefiVariable.value` is `Optional[bytes]` to allow listing-shape
      records that have no associated value bytes; the contract names
      `value: bytes` unconditionally.
    - `UefiVariable` and `UefiVariableInfo` are duplicate dataclasses
      with the same shape; collapsing them after the contract review
      is a candidate cleanup.
    - The Linux `set_variable` write argv uses `efivar -n <ns>-<name>
      -w -a <bitmap>` and ferries the raw bytes through the runner's
      `stdin` parameter rather than a temporary file, because the
      `ICommandRunner` interface does not expose temp-file plumbing.
      The spec's "binary file" wording is preserved in the design.md
      but the runtime form is hex-payload-via-stdin.

- [ ] 44. **Namespace alias registry follow-up**: decide whether the
  registry should be runtime-extensible (useful for vendor-specific
  GUIDs) or frozen at import time.

- [ ] 45. **Linux efivars filesystem fallback**: consider whether
  `get_variable` should fall back to reading
  `/sys/firmware/efi/efivars/<name>-<namespace>` via `IFileOperator`
  when `efivar` is not installed; out of scope for initial
  implementation.
