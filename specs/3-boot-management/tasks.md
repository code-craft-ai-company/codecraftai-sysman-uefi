# Implementation Plan — UEFI Boot Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `automojo.sysman.uefi.bootmanagement` module

- [x] 2. Define `UefiBootActionStatus` enum (SUCCESS / FAILED / SKIPPED
  / DRY_RUN)

- [x] 3. Define `UefiBootManagerConfig` dataclass (dry_run, elevated,
  override_host_os)

- [x] 4. Define `UefiBootActionResult` dataclass

- [x] 5. Define `UefiBootEntryInfo`, `UefiBootEntrySpec` query
  dataclasses and `UefiBootQueryError` exception

## Host-OS Dispatch

- [x] 6. Implement construction-time host-OS detection via
  `platform.system()` with `override_host_os` override

- [x] 7. Implement `_dispatch_linux`, `_dispatch_darwin`,
  `_dispatch_windows` private per-method command builders

- [x] 8. Implement per-host-OS output parsers:
  `_parse_efibootmgr_verbose`, `_parse_bless_info`,
  `_parse_bcdedit_enum_firmware`

## UefiBootManager — Write verbs

- [x] 9. Implement `create_boot_entry(spec)` — Linux `efibootmgr -c
  -d -p -L -l`; Windows three-step `bcdedit`; macOS `bless --setBoot`
  with documented gap

- [x] 10. Implement `delete_boot_entry(entry_id)` — Linux `efibootmgr
  -b -B`; Windows `bcdedit /delete`; macOS FAILED with detail

- [x] 11. Implement `set_boot_order(order)` — Linux `efibootmgr -o`;
  Windows `bcdedit /set {fwbootmgr} displayorder`; macOS FAILED

- [x] 12. Implement `set_boot_next(entry_id)` — Linux `efibootmgr -n`;
  Windows `bcdedit /set {fwbootmgr} bootsequence`; macOS FAILED

## UefiBootManager — Read verbs

- [x] 13. Implement `list_boot_entries()` — all three host-OS paths

- [x] 14. Implement `get_boot_entry(entry_id)` — filters
  `list_boot_entries()` result; raises `UefiBootQueryError` on miss

- [x] 15. Implement `get_boot_order()` — all three host-OS paths

- [x] 16. Implement `get_boot_next()` — Linux parse `BootNext:`;
  Windows parse `bootsequence`; macOS returns `None`

- [x] 17. Implement `get_boot_current()` — all three host-OS paths

## UefiBootManager — Gaps (specified but not implemented)

- [x] 18. macOS `create_boot_entry` — `bless --setBoot` does not take a
  distinct loader path; documented in the Darwin dispatch helper:
  returns `FAILED` with a stable token when no mountpoint is supplied,
  otherwise issues `bless --mount <mp> --setBoot`

- [ ] 19. Non-scriptable firmware-setup toggles (e.g., Setup mode) —
  out of scope for this verb set

- [ ] 20. Legacy BIOS / CSM entries — out of scope

## Tests — Command construction

- [x] 21. `test_list_boot_entries_linux_invokes_efibootmgr_verbose`
- [x] 22. `test_list_boot_entries_darwin_invokes_bless_and_nvram`
- [x] 23. `test_list_boot_entries_windows_invokes_bcdedit_enum_firmware`
- [x] 24. `test_create_boot_entry_linux_constructs_efibootmgr_c`
- [x] 25. `test_create_boot_entry_windows_constructs_three_bcdedit_calls`
- [x] 26. `test_delete_boot_entry_linux_constructs_efibootmgr_b_B`
- [x] 27. `test_delete_boot_entry_darwin_returns_failed_with_detail`
- [x] 28. `test_set_boot_order_linux_constructs_efibootmgr_o_csv`
- [x] 29. `test_set_boot_order_darwin_returns_failed_with_detail`
- [x] 30. `test_set_boot_order_windows_constructs_bcdedit_displayorder`
- [x] 31. `test_set_boot_next_linux_constructs_efibootmgr_n`
- [x] 32. `test_set_boot_next_darwin_returns_failed_with_detail`

## Tests — Idempotency / DRY_RUN / elevation / errors

- [x] 33. `test_create_boot_entry_skipped_when_label_and_loader_match`
- [x] 34. `test_delete_boot_entry_skipped_when_entry_absent`
- [x] 35. `test_set_boot_order_skipped_when_order_matches`
- [ ] 36. `test_set_boot_next_skipped_when_already_set` — covered
  implicitly by the dry-run / Linux happy-path test pair; not a
  standalone test in this batch
- [x] 37. `test_dry_run_short_circuits_all_write_verbs`
- [x] 38. `test_writes_pass_elevated_true_when_configured`
- [x] 39. `test_reads_never_pass_elevated_true`
- [x] 40. `test_failed_command_populates_exit_code_and_stderr`

## Tests — Parsing / dispatch override

- [x] 41. `test_parse_efibootmgr_verbose_extracts_entries_and_order`
- [ ] 42. `test_parse_bless_info_extracts_blessed_loader` — covered by
  the Darwin happy-path enumeration test
- [x] 43. `test_parse_bcdedit_enum_firmware_extracts_guids_and_displayorder`
- [x] 44. `test_override_host_os_forces_linux_dispatch`
- [x] 45. `test_override_host_os_rejects_invalid_value_in_constructor`

## Follow-ups

- [ ] 46. **Cross-OS contract conformance review**: verify that this
  implementation's `UefiBootActionResult` / `UefiBootEntryInfo`
  field shapes match the cross-OS contract at
  `automojo-sysman-common/specs/contracts/boot-management/contract.md`.
  Initial review notes:
    - `UefiBootEntryInfo` exposes `loader: str | None` and `is_default:
      bool` whereas the contract names `loader_path: str` and
      `is_current: bool`.  This is intentional because the UEFI package
      treats `BootCurrent` and the firmware default order as distinct
      concepts; an upstream renormalisation pass should converge the
      names.
    - `UefiBootActionResult.detail` carries a stable
      `unsupported_on_host_os_<host>` token for verbs that have no
      host-OS implementation; the contract permits but does not
      mandate this token form.

- [ ] 47. **Coordinator integration**: confirm that a
  firmware-provisioning coordinator calling this manager does not
  invoke `efibootmgr` / `bcdedit` / `bless` directly anywhere else
  in the suite — all such calls should route through
  `UefiBootManager` to preserve the dispatch + dry-run + audit path.
