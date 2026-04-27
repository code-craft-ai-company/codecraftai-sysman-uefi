# Implementation Plan — UEFI Capsule Update

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.  The capsule-update module is
landed at
`source/packages/automojo/sysman/uefi/capsuleupdate.py` with paired
tests at
`source/testroots/testbase/automojo/tests/sysman/uefi/test_capsuleupdate.py`.

## Module and Models

- [x] 1. Create the `automojo.sysman.uefi.capsuleupdate` module

- [x] 2. Define `UefiCapsuleUpdateActionStatus` enum (SUCCESS /
  FAILED / SKIPPED / DRY_RUN) — aliased to the shared `ActionStatus`
  in `featurebase.py`

- [x] 3. Define `UefiCapsuleUpdateManagerConfig` dataclass (dry_run,
  elevated, allow_downgrade, override_host_os, plus per-host-OS tool
  paths)

- [x] 4. Define `UefiCapsuleUpdateActionResult`,
  `UefiCapsuleUpdateInfo`, `UefiDeviceInfo`,
  `UefiCapsuleUpdateStatus` dataclasses

- [x] 5. Define `UefiCapsuleUpdateQueryError` exception

## Host-OS Dispatch

- [x] 6. Implement construction-time host-OS detection via
  `platform.system()` with `override_host_os` override (inherited
  from `BaseFeatureManager`)

- [x] 7. Implement `_dispatch_linux_*`, `_dispatch_darwin_*`,
  `_dispatch_windows_*` private per-method command builders

- [x] 8. Implement per-host-OS output parsers:
  `_parse_fwupdmgr_get_updates_json`,
  `_parse_fwupdmgr_get_devices_json`,
  `_parse_fwupdmgr_get_history_json`,
  `_parse_softwareupdate_list`,
  `_parse_softwareupdate_history`,
  `_parse_system_profiler_hardware`,
  `_parse_get_windowsupdate_json`,
  `_parse_wmi_computer_bios_json`,
  `_parse_get_wuhistory_json`

## UefiCapsuleUpdateManager — Read verbs

- [x] 9. Implement `list_available_updates()` — all three host-OS
  paths

- [x] 10. Implement `list_devices()` — all three host-OS paths;
  Darwin returns a single `update_supported=False` record

- [x] 11. Implement `get_update_status(update_id=None)` — all three
  host-OS paths produce a `UefiCapsuleUpdateStatus` snapshot
  combining last-history-entry data with the current pending-count

## UefiCapsuleUpdateManager — Write verbs

- [x] 12. Implement `apply_update(update_id)` — Linux `fwupdmgr
  update --no-reboot-check`; Windows `Install-WindowsUpdate
  -KBArticleID`; macOS FAILED with delegation detail

- [x] 13. Implement `apply_capsule_file(path)` — Linux `fwupdmgr
  install --allow-reinstall --no-reboot-check`; macOS and Windows
  FAILED with stable token-prefixed detail

## Downgrade gating

- [x] 14. Implement `_compare_versions(available, current)` —
  dotted-segment comparison with ambiguous-format ("unknown")
  fallback

- [x] 15. Integrate downgrade check into `apply_update`; the gate
  runs after the idempotency probe and before the runner dispatch.
  `apply_capsule_file` has no per-update version metadata to
  compare, so the gate is documented as proceeding (capsule-file
  callers opt into downgrade risk by selecting their own file).

## UefiCapsuleUpdateManager — Gaps (specified but not implemented)

- [ ] 16. Raw-capsule installation on Windows — out of scope
  (requires OEM tool); surfaced as a stable FAILED token with a
  detail naming the OEM path.

- [ ] 17. `softwareupdate` auto-reboot vs user-confirm difference on
  macOS — documented but not papered over.

- [ ] 18. ESRT decoding for device enumeration — not required by the
  contract; out of scope for initial implementation.

## Tests — Command construction

- [x] 19. `test_list_available_updates_linux_invokes_fwupdmgr_get_updates_json`
- [x] 20. `test_list_available_updates_darwin_invokes_softwareupdate_list`
- [x] 21. `test_list_available_updates_windows_invokes_get_windowsupdate`
- [x] 22. `test_apply_update_linux_constructs_fwupdmgr_update`
- [x] 23. `test_apply_update_darwin_returns_failed_with_softwareupdate_detail`
- [x] 24. `test_apply_update_windows_constructs_install_windowsupdate`
- [x] 25. `test_apply_capsule_file_linux_constructs_fwupdmgr_install`
- [x] 26. `test_apply_capsule_file_darwin_returns_failed_with_token`
- [x] 27. `test_apply_capsule_file_windows_returns_failed_with_oem_detail`
- [x] 28. `test_list_devices_linux_invokes_fwupdmgr_get_devices_json`
- [x] 29. `test_list_devices_darwin_returns_apple_record_with_supported_false`
- [x] 30. `test_get_update_status_linux_invokes_fwupdmgr_get_history_json`

## Tests — Idempotency / downgrade / DRY_RUN / elevation

- [x] 31. `test_apply_update_skipped_when_not_in_pending_list`
- [x] 32. `test_apply_update_dry_run_short_circuits` (covers the
  "post-apply reboot flag" intent because dry-run records the
  argv that would have run, including the `--no-reboot-check`
  argument that asserts the post-apply reboot policy)
- [x] 33. `test_apply_update_blocked_on_downgrade_without_allow_flag`
- [x] 34. `test_apply_update_proceeds_on_downgrade_when_allow_flag_set`
- [x] 35. `test_apply_update_proceeds_on_unknown_version_format`
- [x] 36. `test_dry_run_short_circuits_all_write_verbs`
- [x] 37. `test_writes_pass_elevated_true_when_configured`
- [x] 38. `test_reads_never_pass_elevated_true`

## Tests — Parsing / dispatch override

- [x] 39. `test_parse_fwupdmgr_get_updates_extracts_releases`
- [x] 40. `test_parse_softwareupdate_list_filters_firmware_entries`
- [x] 41. `test_parse_wmi_json_builds_device_info`
- [x] 42. `test_get_update_status_windows_invokes_get_wuhistory`
  (covers the `Get-WUHistory` parser end-to-end)
- [x] 43. `test_override_host_os_forces_linux_dispatch`,
  `test_override_host_os_forces_darwin_dispatch`,
  `test_override_host_os_forces_windows_dispatch`
- [x] 44. `test_override_host_os_rejects_invalid_value_in_constructor`

## Follow-ups

- [ ] 45. **Cross-OS contract conformance review**: verify that this
  implementation's result / query dataclass shapes match the
  contract at
  `automojo-sysman-common/specs/contracts/capsule-update/contract.md`.
  *Drift discovered:* the contract-mandated
  `CapsuleUpdateActionResult.applied: CapsuleUpdateInfo | None`
  field is not implemented in this UEFI variant; we surface the
  applied-update identity through `update_id` + `device_id` instead
  to match the existing `FeatureActionResult` shape used by the
  package's other features.  The contract's
  `CapsuleUpdateInfo.summary`, `requires_reboot`, and
  `applied_version`/`applied_at`/`result` fields on the history
  entry are surfaced through `UefiCapsuleUpdateInfo.description`
  and the `UefiCapsuleUpdateStatus` snapshot, not as separate
  fields.

- [ ] 46. **fwupd version-string normalisation**: the dotted-segment
  comparator passes the basic + ambiguous-format tests but does not
  yet implement the full `libvercmp` algorithm for edge-case vendor
  versions (e.g., `2.0.7~rc1`).

- [ ] 47. **Coordinate with `boot-management`**: a common workflow is
  to call `apply_update` then
  `set_boot_next(firmware-update-entry)`.  Both managers exist now;
  documenting this pattern in the package-level usersguide is
  pending.
