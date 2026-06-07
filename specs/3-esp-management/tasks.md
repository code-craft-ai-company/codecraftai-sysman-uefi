# Implementation Plan — UEFI ESP Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.  The esp-management module is
landed at
`source/packages/ccai/sysman/uefi/espmanagement.py` with paired
tests at
`source/testroots/testbase/ccai/tests/sysman/uefi/test_espmanagement.py`.

## Module and Models

- [x] 1. Create the `ccai.sysman.uefi.espmanagement` module

- [x] 2. Define `UefiEspActionStatus` enum (SUCCESS / FAILED /
  SKIPPED / DRY_RUN) — aliased to the shared `ActionStatus` in
  `featurebase.py`

- [x] 3. Define `UefiEspManagerConfig` dataclass (dry_run, elevated,
  override_host_os, plus per-host-OS tool paths and default
  mountpoint / drive-letter values)

- [x] 4. Define `UefiEspActionResult`, `UefiEspInfo`,
  `UefiEspFileInfo`, and `UefiEspFileContent` dataclasses

- [x] 5. Define `UefiEspQueryError` exception.  *Drift from
  contract:* `UefiEspNotMountedError` is folded into
  `UefiEspQueryError` (with a "ESP is not mounted" detail); reads
  raise the parent type when the ESP is not mounted rather than a
  separate sub-exception.

## Host-OS Dispatch

- [x] 6. Implement construction-time host-OS detection via
  `platform.system()` with `override_host_os` override (inherited
  from `BaseFeatureManager`)

- [x] 7. Implement `_dispatch_linux_*`, `_dispatch_darwin_*`,
  `_dispatch_windows_*` private per-method command builders for
  every verb

- [x] 8. Implement per-host-OS output parsers:
  `_parse_findmnt_json`, `_parse_lsblk_size`,
  `_parse_diskutil_list_for_esp`, `_parse_diskutil_info`,
  `_parse_get_partition_json`,
  `_parse_get_volume_for_drive_letter`,
  `_parse_ls_la`, `_parse_get_childitem_json`

## UefiEspManager — Discovery & mount verbs

- [x] 9. Implement `get_esp_info()` — all three host-OS paths plus
  graceful "no ESP" fallback (returns
  `UefiEspInfo(device=None, mounted=False, ...)` instead of
  raising)

- [x] 10. Implement `is_esp_mounted()` — all three host-OS paths

- [x] 11. Implement `mount_esp(mountpoint=None)` — all three
  host-OS paths plus already-mounted idempotency

- [x] 12. Implement `unmount_esp(mountpoint=None)` — all three
  host-OS paths plus not-mounted idempotency

## UefiEspManager — File I/O verbs

- [x] 13. Implement `_normalise_relative_path(relative)` — accepts
  both `/` and `\` separators, returns host-OS native form

- [x] 14. Implement `_join_esp_path(mountpoint, relative)` — joins
  the active mountpoint with a normalised ESP-relative path

- [x] 15. Implement `list_esp_contents(path)` with mount precondition

- [x] 16. Implement `read_esp_file(path)` with mount precondition

- [x] 17. Implement `write_esp_file(path, content)` — mount
  precondition; byte-match idempotency; ICommandRunner-driven
  ``tee`` (Linux/macOS) or ``Set-Content`` (Windows) writes.
  *Drift from contract:* the contract specifies
  `IFileOperator.write_bytes` delegation; this implementation routes
  every file-I/O verb through the same `ICommandRunner` to match the
  established pattern across all `codecraftai-sysman-*` Layer-3
  features.

- [x] 18. Implement `delete_esp_file(path)` — mount precondition;
  existence-probe idempotency; ICommandRunner-driven ``rm -f`` /
  ``Remove-Item`` deletes (same drift as Task 17)

## UefiEspManager — Gaps (specified but not implemented)

- [ ] 19. Recursive directory delete on the ESP — deferred to a
  future filesystem-management `remove_directory(recursive=True)`
  verb.

- [ ] 20. ESP filesystem-check (`fsck.vfat` / `chkdsk`) — out of
  scope.

- [ ] 21. Apple pre-boot volume / NVMe FFS partition — out of scope.

## Tests — Command construction

- [x] 22. `test_get_esp_info_linux_invokes_findmnt_and_lsblk`
- [x] 23. `test_get_esp_info_darwin_invokes_diskutil_list_plist`
- [x] 24. `test_get_esp_info_windows_invokes_get_partition_filtered_system`
- [x] 25. `test_mount_esp_linux_constructs_mount_t_vfat`
- [x] 26. `test_mount_esp_darwin_constructs_diskutil_mount`
- [x] 27. `test_mount_esp_windows_constructs_mountvol_s_s`
- [x] 28. `test_unmount_esp_linux_constructs_umount`
- [x] 29. `test_unmount_esp_darwin_constructs_diskutil_unmount`
- [x] 30. `test_unmount_esp_windows_constructs_mountvol_d`

## Tests — Idempotency / preconditions / DRY_RUN / elevation

- [x] 31. `test_mount_esp_skipped_when_already_mounted`
- [x] 32. `test_unmount_esp_skipped_when_not_mounted`
- [x] 33. `test_write_esp_file_skipped_when_bytes_match`
- [x] 34. `test_delete_esp_file_skipped_when_path_absent`
- [x] 35. `test_list_esp_contents_raises_when_unmounted`
- [x] 36. `test_read_esp_file_raises_when_unmounted`
- [x] 37. `test_write_esp_file_returns_failed_when_unmounted`
- [x] 38. `test_delete_esp_file_returns_failed_when_unmounted`
- [x] 39. `test_mount_esp_dry_run_short_circuits`,
  `test_unmount_esp_dry_run_short_circuits`,
  `test_write_esp_file_dry_run_short_circuits`
- [x] 40. `test_writes_pass_elevated_true_when_configured`
- [x] 41. `test_reads_never_pass_elevated_true`

## Tests — Parsing / path normalisation / dispatch override

- [x] 42. `test_parse_findmnt_json_extracts_target_source_fstype`
- [x] 43. `test_parse_diskutil_plist_finds_efi_partition`
- [x] 44. `test_parse_get_partition_json_filters_type_system`
- [x] 45. `test_path_normalisation_converts_backslashes_on_linux`
- [x] 46. `test_path_normalisation_converts_forward_slashes_on_windows`
- [x] 47. `test_override_host_os_forces_windows_dispatch`,
  `test_override_host_os_forces_linux_dispatch`,
  `test_override_host_os_forces_darwin_dispatch`
- [x] 48. `test_override_host_os_rejects_invalid_value_in_constructor`

## Follow-ups

- [ ] 49. **Cross-OS contract conformance review**: verify that this
  implementation's result / info dataclass shapes match the
  contract at
  `codecraftai-sysman-common/specs/contracts/esp-management/contract.md`.
  *Drift discovered:* the contract specifies
  `EspInfo.detected: bool`, `EspInfo.fstype: str`, `EspInfo.uuid`,
  and `EspInfo.partition_guid`; this implementation surfaces the
  detection signal as the union of `device is None` /
  `mounted=False`, names the filesystem field `filesystem`, and does
  not yet expose UUID or GPT partition GUID.  The contract also
  separates `EspNotMountedError` from `EspQueryError`; this
  implementation folds them into a single `UefiEspQueryError` with a
  descriptive detail token.  Finally, the contract mandates
  `IFileOperator` delegation for file I/O; this implementation
  uses the runner-only pattern (see Task 17 drift note).

- [ ] 50. **Coordinate with `boot-management`**: a common workflow is
  `mount_esp` → `write_esp_file(<loader>)` → `create_boot_entry`.
  Both managers exist now; documenting this cross-feature usage
  pattern in the package-level usersguide is pending.

- [ ] 51. **Windows drive-letter availability check**: the current
  `mount_esp` implementation does not pre-verify that the requested
  drive letter is unassigned before invoking ``mountvol``.  A clear
  detail when the letter is already taken would be preferable to a
  raw `mountvol` failure.
