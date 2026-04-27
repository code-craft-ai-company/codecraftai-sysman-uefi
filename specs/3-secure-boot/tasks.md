# Implementation Plan — UEFI Secure Boot

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `automojo.sysman.uefi.secureboot` module

- [x] 2. Define `UefiSecureBootActionStatus` enum (SUCCESS / FAILED /
  SKIPPED / DRY_RUN)

- [x] 3. Define `UefiSecureBootManagerConfig` dataclass (dry_run,
  elevated, override_host_os)

- [x] 4. Define `UefiSecureBootActionResult` dataclass

- [x] 5. Define `UefiSecureBootState`, `UefiSecureBootPolicy`,
  `UefiKeyInfo` query dataclasses and `UefiSecureBootQueryError`
  exception

## Host-OS Dispatch

- [x] 6. Implement construction-time host-OS detection via
  `platform.system()` with `override_host_os` override

- [x] 7. Implement `_dispatch_linux`, `_dispatch_darwin`,
  `_dispatch_windows` private per-method command builders

- [x] 8. Implement per-host-OS output parsers:
  `_parse_mokutil_list_enrolled`, `_parse_bputil_display_summary`,
  `_count_efivar_slots`, plus inline state parsing for
  `mokutil --sb-state` and `Confirm-SecureBootUEFI`.  (`_parse_csrutil_status_summary`
  is implemented as a fallback for the "no bputil" path on Intel
  macOS.)

## UefiSecureBootManager — Write verbs

- [x] 9. Implement `enroll_mok_key(certificate_path)` — Linux
  `mokutil --import`; macOS and Windows return FAILED with stable
  token-prefixed detail

- [x] 10. Implement `reset_mok(key_fingerprint)` — Linux
  `mokutil --delete`; macOS and Windows return FAILED

- [x] 11. Implement `set_secure_boot_mode(mode)` — always FAILED
  (or DRY_RUN) across all host OSes with `firmware_setup_ui_only`
  detail token

## UefiSecureBootManager — Read verbs

- [x] 12. Implement `get_secure_boot_state()` — all three host-OS
  paths plus the "not supported" graceful fallback

- [x] 13. Implement `get_secure_boot_policy()` — Linux slot
  enumeration; macOS raw `bputil --display`; Windows JSON-formatted
  `Get-SecureBootPolicy`

- [x] 14. Implement `list_enrolled_keys(slot)` — union across slots
  or single-slot filtered; returns `[]` for MOK on macOS / Windows

## UefiSecureBootManager — Gaps (specified but not implemented)

- [ ] 15. Fingerprint format normalisation across `mokutil`
  (`SHA1 Fingerprint:`) versus `Get-SecureBootPolicy` (`SHA256`) —
  follow-up; current implementation surfaces the raw form.

- [ ] 16. Apple PKI Boot Policy structured decoding — currently raw
  text only

- [ ] 17. OEM PK / KEK / db write surfaces on Windows — out of scope

## Tests — Command construction

- [x] 18. `test_get_secure_boot_state_linux_invokes_mokutil_sb_state`
- [x] 19. `test_get_secure_boot_state_darwin_invokes_csrutil_and_bputil`
- [x] 20. `test_get_secure_boot_state_windows_invokes_confirm_secureboot_uefi`
- [x] 21. `test_get_secure_boot_policy_linux_invokes_mokutil_list_enrolled_and_efivar_l`
- [x] 22. `test_enroll_mok_key_linux_constructs_mokutil_import`
- [x] 23. `test_enroll_mok_key_darwin_returns_failed_with_apple_detail`
- [x] 24. `test_enroll_mok_key_windows_returns_failed_with_oem_detail`
- [x] 25. `test_reset_mok_linux_constructs_mokutil_delete`
- [x] 26. `test_set_secure_boot_mode_always_failed_with_firmware_setup_detail`
  (split across `test_set_secure_boot_mode_always_failed_on_{linux,darwin,windows}`)

## Tests — Idempotency / DRY_RUN / elevation

- [ ] 27. `test_enroll_mok_key_skipped_when_fingerprint_already_enrolled` —
  follow-up; the current idempotency probe matches certificates by
  basename but the test scenario requires a real fingerprint extracted
  from the certificate which is not modeled in this batch.
- [x] 28. `test_reset_mok_skipped_when_fingerprint_absent`
- [x] 29. `test_dry_run_short_circuits_enroll_and_reset_and_set_mode`
  (covered by `test_dry_run_short_circuits_enroll_and_reset` and
  `test_set_secure_boot_mode_dry_run_returns_dry_run`)
- [x] 30. `test_writes_pass_elevated_true_when_configured`
- [x] 31. `test_reads_never_pass_elevated_true`

## Tests — Parsing / fallback / dispatch override

- [x] 32. `test_parse_mokutil_sb_state_enabled_supported`
  (covered by `test_get_secure_boot_state_linux_invokes_mokutil_sb_state`)
- [x] 33. `test_parse_mokutil_sb_state_not_supported_returns_supported_false`
  (`test_get_secure_boot_state_linux_not_supported_returns_supported_false`)
- [x] 34. `test_parse_mokutil_list_enrolled_extracts_fingerprint_subject_issuer`
  (`test_parse_mokutil_list_enrolled_extracts_fingerprint_and_subject`)
- [x] 35. `test_parse_bputil_display_preserves_raw_text`
  (`test_get_secure_boot_policy_darwin_preserves_raw_text` plus
  `test_parse_bputil_display_extracts_full_security_summary`)
- [x] 36. `test_parse_get_secureboot_policy_json_extracts_slots`
  (`test_get_secure_boot_policy_windows_extracts_publisher`)
- [x] 37. `test_override_host_os_forces_linux_dispatch`
- [x] 38. `test_override_host_os_rejects_invalid_value_in_constructor`

## Follow-ups

- [ ] 39. **Cross-OS contract conformance review**: verify that this
  implementation's result and query dataclass shapes match the
  contract at
  `automojo-sysman-common/specs/contracts/secure-boot/contract.md`.
  Initial drift notes:
    - `UefiSecureBootPolicy` exposes per-slot **counts** (`kek_count`
      etc.) where the spec design.md describes per-slot **hash lists**
      (`kek_hashes`).  The slot-count form is more useful for
      coordinators that just need a "policy populated?" answer; the
      hash-list form is a follow-up that requires either a deeper
      `efivar` parse or a structured PowerShell parse.
    - `UefiKeyInfo.fingerprint` carries the SHA1 form on Linux and may
      be `None` on Windows / macOS where the host tool does not surface
      a fingerprint.  Cross-OS callers should treat fingerprints as
      opaque per-host strings until the canonicalisation follow-up
      below lands.

- [ ] 40. **Fingerprint canonicalisation follow-up**: decide whether
  `mokutil` SHA1 fingerprints and `Get-SecureBootPolicy` SHA256
  digests should be exposed in distinct fields on
  `UefiKeyInfo` or normalised to a single canonical form.
