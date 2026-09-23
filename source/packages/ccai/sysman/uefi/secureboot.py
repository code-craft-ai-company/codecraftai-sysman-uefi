"""
    UEFI secure-boot feature.

    Implements the cross-OS secure-boot contract for UEFI Secure Boot
    inspection and (Linux-only) Machine-Owner Key (MOK) management on
    whichever host the manager runs on.  Dispatches to host-specific
    user-space tooling:

        * Linux   -> ``mokutil`` + (optionally) ``efivar``
        * Darwin  -> ``csrutil`` + ``bputil``
        * Windows -> PowerShell ``Confirm-SecureBootUEFI`` /
                     ``Get-SecureBootPolicy``

    Per-host-OS dispatch:
        :class:`UefiSecureBootManager` records the running host OS once
        at construction time (via :func:`detect_host_os`) and routes
        every verb through ``_dispatch_<linux|darwin|windows>_<verb>``
        private methods.

    Universal failures:
        ``set_secure_boot_mode`` always returns ``FAILED`` (or
        ``DRY_RUN``) on every host OS because Secure Boot is a
        firmware-setup-UI toggle.  ``enroll_mok_key`` and ``reset_mok``
        are Linux-only; on macOS and Windows they return ``FAILED``
        with a stable token-prefixed detail.

    Elevation policy:
        Writes pass ``elevated=True`` when ``config.elevated`` is True;
        reads never elevate.
"""

import os

from dataclasses import dataclass
from typing import Optional

from ccai.interfaces.icommandrunner import ICommandRunner

from ccai.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
    HOST_OS_DARWIN,
    HOST_OS_LINUX,
    HOST_OS_WINDOWS,
)


_DEFAULT_MOKUTIL = "mokutil"
_DEFAULT_EFIVAR = "efivar"
_DEFAULT_CSRUTIL = "csrutil"
_DEFAULT_BPUTIL = "bputil"
_DEFAULT_POWERSHELL = "powershell"

_FIRMWARE_UI_TOKEN = "firmware_setup_ui_only"


UefiSecureBootActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------


@dataclass
class UefiSecureBootManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UefiSecureBootManager`.

        :param override_host_os: Test-time override.
        :param mokutil_path: Path to ``mokutil`` (Linux).
        :param efivar_path: Path to ``efivar`` (Linux).
        :param csrutil_path: Path to ``csrutil`` (macOS).
        :param bputil_path: Path to ``bputil`` (Apple Silicon macOS).
        :param powershell_path: Path to PowerShell (Windows).
    """
    override_host_os: Optional[str] = None
    mokutil_path: str = _DEFAULT_MOKUTIL
    efivar_path: str = _DEFAULT_EFIVAR
    csrutil_path: str = _DEFAULT_CSRUTIL
    bputil_path: str = _DEFAULT_BPUTIL
    powershell_path: str = _DEFAULT_POWERSHELL


@dataclass
class UefiSecureBootState:
    """
        Snapshot of the host's Secure Boot state.

        :param enabled: Whether Secure Boot is currently enforcing.
        :param setup_mode: Whether the firmware is in Setup Mode.  ``None``
                           when the host tool does not report it.
        :param audit_mode: Whether the firmware is in Audit Mode.  ``None``
                           when the host tool does not report it.
        :param host_os: The host OS, lower-cased.
        :param supported: Whether Secure Boot is supported by the
                          firmware.  ``False`` when the host tool reports
                          "not supported"; ``True`` otherwise.
        :param policy_description: Free-form description, particularly
                                   useful on macOS where ``bputil --display``
                                   surfaces "Full Security", "Reduced
                                   Security", or "Permissive Security".
    """
    enabled: bool
    setup_mode: Optional[bool]
    audit_mode: Optional[bool]
    host_os: str
    supported: bool = True
    policy_description: Optional[str] = None


@dataclass
class UefiSecureBootPolicy:
    """
        Snapshot of enrolled-key policy.

        :param pk_present: Whether a Platform Key is enrolled.
        :param kek_count: Number of KEK entries, or ``None`` when not
                          available.
        :param db_count: Number of db entries, or ``None``.
        :param dbx_count: Number of dbx entries, or ``None``.
        :param mok_count: Number of MOK entries, or ``None``.
        :param apple_policy_raw: Raw ``bputil --display`` text on macOS;
                                 ``None`` elsewhere.
    """
    pk_present: bool
    kek_count: Optional[int]
    db_count: Optional[int]
    dbx_count: Optional[int]
    mok_count: Optional[int]
    apple_policy_raw: Optional[str] = None


@dataclass
class UefiKeyInfo:
    """
        One enrolled-key record.

        :param slot: One of ``"PK"``, ``"KEK"``, ``"db"``, ``"dbx"``,
                     ``"MOK"``.
        :param subject: Certificate subject (when available).
        :param fingerprint: Key fingerprint (SHA1 from ``mokutil``;
                            varies on other hosts).
        :param enrolled_at: Free-form enrollment marker (when available).
    """
    slot: str
    subject: Optional[str] = None
    fingerprint: Optional[str] = None
    enrolled_at: Optional[str] = None


@dataclass
class UefiSecureBootActionResult(FeatureActionResult):
    """
        Outcome of a Secure Boot write.

        :param host_os: The host OS the action dispatched to.
        :param requires_reboot: Whether the change becomes effective on
                                next reboot only.  ``True`` for MOK
                                writes since they are queued for shim's
                                MOK Manager UI on next boot.
    """
    host_os: Optional[str] = None
    requires_reboot: bool = False


class UefiSecureBootQueryError(Exception):
    """
        Raised when a Secure Boot query fails for a reason other than
        "Secure Boot not supported".
    """

    def __init__(
        self,
        *,
        command: Optional[list[str]] = None,
        exit_code: Optional[int] = None,
        stderr: Optional[str] = None,
        message: Optional[str] = None,
    ) -> None:
        if command is None:
            self.command: list[str] = []
        else:
            self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["UEFI secure-boot query failed."]
        if message is not None and len(message) > 0:
            err_msg_lines.append("    MESSAGE: {}".format(message))
        if len(self.command) > 0:
            err_msg_lines.append("    COMMAND: {}".format(" ".join(self.command)))
        if exit_code is not None:
            err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        if stderr is not None:
            err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class UefiSecureBootManager(BaseFeatureManager):
    """
        Per-host-OS Secure Boot manager.  Reads work on every host;
        writes are Linux-only (``enroll_mok_key`` / ``reset_mok``) or
        always-FAILED (``set_secure_boot_mode``).
    """

    result_class = UefiSecureBootActionResult
    status_class = UefiSecureBootActionStatus
    feature_name = "secure-boot"

    def __init__(
        self,
        runner: ICommandRunner,
        config: Optional[UefiSecureBootManagerConfig] = None,
    ) -> None:
        if config is None:
            config = UefiSecureBootManagerConfig()
        super().__init__(runner=runner, config=config)
        self._typed_config: UefiSecureBootManagerConfig = config
        return

    @property
    def typed_config(self) -> UefiSecureBootManagerConfig:
        """
            The active :class:`UefiSecureBootManagerConfig`.
        """
        return self._typed_config

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_secure_boot_state(self) -> UefiSecureBootState:
        """
            Read the host's Secure Boot state.

            :returns: A :class:`UefiSecureBootState` populated from the
                      host tool's output.  ``supported=False`` when the
                      tool reports "Secure Boot not supported".
            :raises UefiSecureBootQueryError: When the underlying tool
                                              fails for a reason other
                                              than "not supported".
        """
        if self._host_os == HOST_OS_LINUX:
            rtnval = self._dispatch_linux_get_state()
        elif self._host_os == HOST_OS_DARWIN:
            rtnval = self._dispatch_darwin_get_state()
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = self._dispatch_windows_get_state()
        else:
            err_msg = "Unsupported host OS for get_secure_boot_state: {}".format(
                self._host_os,
            )
            raise UefiSecureBootQueryError(message=err_msg)
        return rtnval

    def get_secure_boot_policy(self) -> UefiSecureBootPolicy:
        """
            Read the host's enrolled-key policy snapshot.

            :returns: A :class:`UefiSecureBootPolicy`.  On macOS the
                      slot-count fields are ``None`` and
                      ``apple_policy_raw`` carries the full ``bputil
                      --display`` text.
            :raises UefiSecureBootQueryError: When the underlying tool
                                              fails.
        """
        if self._host_os == HOST_OS_LINUX:
            rtnval = self._dispatch_linux_get_policy()
        elif self._host_os == HOST_OS_DARWIN:
            rtnval = self._dispatch_darwin_get_policy()
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = self._dispatch_windows_get_policy()
        else:
            err_msg = "Unsupported host OS for get_secure_boot_policy: {}".format(
                self._host_os,
            )
            raise UefiSecureBootQueryError(message=err_msg)
        return rtnval

    def list_enrolled_keys(
        self,
        slot: Optional[str] = None,
    ) -> list[UefiKeyInfo]:
        """
            List enrolled keys, optionally filtered by slot.

            :param slot: One of ``"PK"``, ``"KEK"``, ``"db"``, ``"dbx"``,
                         ``"MOK"``, or ``None`` for the union.

            :returns: One :class:`UefiKeyInfo` per enrolled key.  Returns
                      ``[]`` for ``slot="MOK"`` on macOS / Windows.
            :raises UefiSecureBootQueryError: When the underlying tool
                                              fails.
        """
        if self._host_os == HOST_OS_LINUX:
            keys = self._dispatch_linux_list_keys()
        elif self._host_os == HOST_OS_DARWIN:
            keys = self._dispatch_darwin_list_keys()
        elif self._host_os == HOST_OS_WINDOWS:
            keys = self._dispatch_windows_list_keys()
        else:
            err_msg = "Unsupported host OS for list_enrolled_keys: {}".format(
                self._host_os,
            )
            raise UefiSecureBootQueryError(message=err_msg)

        if slot is None:
            rtnval = keys
        else:
            wanted = slot.strip()
            if wanted == "MOK" and self._host_os in (HOST_OS_DARWIN, HOST_OS_WINDOWS):
                rtnval: list[UefiKeyInfo] = []
            else:
                rtnval = [k for k in keys if k.slot == wanted]
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def enroll_mok_key(self, path: str) -> UefiSecureBootActionResult:
        """
            Stage MOK enrollment from a certificate file.  Linux only;
            on macOS and Windows returns ``FAILED`` without invoking
            the runner.

            Idempotency: when the host's MOK list already contains a key
            whose fingerprint string is contained in the certificate
            path's filename token (best-effort fallback when no read of
            the certificate's fingerprint is possible), returns
            ``SKIPPED``.

            :param path: Path to the certificate file.

            :returns: The :class:`UefiSecureBootActionResult`.
        """
        if self._config.dry_run:
            command = self._build_dry_run_enroll_command(path=path)
            rtnval = self._dry_run_result(
                command=command,
                detail=path,
                requires_reboot=False,
            )
            return rtnval

        if self._host_os == HOST_OS_DARWIN:
            rtnval = self._build_failed_unsupported(
                verb="enroll_mok_key",
                host_os=self._host_os,
                message="macOS Secure Boot is managed via Apple Boot Policy (bputil)",
                requires_reboot=False,
                host_os_field=self._host_os,
            )
            return rtnval

        if self._host_os == HOST_OS_WINDOWS:
            rtnval = self._build_failed_unsupported(
                verb="enroll_mok_key",
                host_os=self._host_os,
                message="Windows Secure Boot uses OEM PK / KEK / db tooling",
                requires_reboot=False,
                host_os_field=self._host_os,
            )
            return rtnval

        if self._host_os != HOST_OS_LINUX:
            rtnval = self._build_failed_unsupported(
                verb="enroll_mok_key",
                host_os=self._host_os,
                requires_reboot=False,
                host_os_field=self._host_os,
            )
            return rtnval

        # Linux idempotency probe by basename match (best-effort).
        existing_keys = self._safe_list_enrolled_keys(slot="MOK")
        if existing_keys is not None:
            basename = os.path.basename(path)
            for key in existing_keys:
                if key.fingerprint is not None:
                    if key.fingerprint == basename:
                        skipped = self._wrap_result(
                            status=self.status_class.SKIPPED,
                            detail="MOK fingerprint already enrolled",
                            command=None,
                            requires_reboot=False,
                            host_os=self._host_os,
                        )
                        return skipped
                if key.subject is not None and basename in key.subject:
                    skipped = self._wrap_result(
                        status=self.status_class.SKIPPED,
                        detail="MOK subject already enrolled",
                        command=None,
                        requires_reboot=False,
                        host_os=self._host_os,
                    )
                    return skipped

        command = [self._typed_config.mokutil_path, "--import", path]
        rtnval = self._dispatch(
            command=command,
            detail=path,
            operation="enroll_mok_key",
            requires_reboot=True,
            host_os=self._host_os,
        )
        return rtnval

    def reset_mok(self, key_id: str) -> UefiSecureBootActionResult:
        """
            Stage MOK removal by fingerprint.  Linux only; on macOS and
            Windows returns ``FAILED``.

            Idempotency: when the fingerprint is absent from the MOK
            list, returns ``SKIPPED``.

            :param key_id: Fingerprint or path identifier.

            :returns: The :class:`UefiSecureBootActionResult`.
        """
        if self._config.dry_run:
            command = self._build_dry_run_reset_command(key_id=key_id)
            rtnval = self._dry_run_result(
                command=command,
                detail=key_id,
                requires_reboot=False,
            )
            return rtnval

        if self._host_os == HOST_OS_DARWIN:
            rtnval = self._build_failed_unsupported(
                verb="reset_mok",
                host_os=self._host_os,
                message="macOS does not expose a MOK equivalent",
                requires_reboot=False,
                host_os_field=self._host_os,
            )
            return rtnval

        if self._host_os == HOST_OS_WINDOWS:
            rtnval = self._build_failed_unsupported(
                verb="reset_mok",
                host_os=self._host_os,
                message="Windows does not expose a MOK equivalent",
                requires_reboot=False,
                host_os_field=self._host_os,
            )
            return rtnval

        if self._host_os != HOST_OS_LINUX:
            rtnval = self._build_failed_unsupported(
                verb="reset_mok",
                host_os=self._host_os,
                requires_reboot=False,
                host_os_field=self._host_os,
            )
            return rtnval

        existing = self._safe_list_enrolled_keys(slot="MOK")
        if existing is not None:
            present = False
            for key in existing:
                if key.fingerprint == key_id:
                    present = True
                    break
                if key.subject == key_id:
                    present = True
                    break
            if not present:
                skipped = self._wrap_result(
                    status=self.status_class.SKIPPED,
                    detail="MOK key not present",
                    command=None,
                    requires_reboot=False,
                    host_os=self._host_os,
                )
                return skipped

        command = [self._typed_config.mokutil_path, "--delete", key_id]
        rtnval = self._dispatch(
            command=command,
            detail=key_id,
            operation="reset_mok",
            requires_reboot=True,
            host_os=self._host_os,
        )
        return rtnval

    def set_secure_boot_mode(self, mode: str) -> UefiSecureBootActionResult:
        """
            Always returns ``FAILED`` (or ``DRY_RUN``) — Secure Boot mode
            is controlled via firmware-setup UI only.  No host tool
            command is invoked.

            :param mode: The desired mode string (recorded in the
                         ``detail`` for auditability).

            :returns: The :class:`UefiSecureBootActionResult`.
        """
        if self._config.dry_run:
            rtnval = self._wrap_result(
                status=self.status_class.DRY_RUN,
                detail="set_secure_boot_mode requires firmware setup UI: requested={}".format(
                    mode,
                ),
                command=None,
                requires_reboot=True,
                host_os=self._host_os,
            )
            return rtnval

        token = "{}: requested={}".format(_FIRMWARE_UI_TOKEN, mode)
        rtnval = self._wrap_result(
            status=self.status_class.FAILED,
            detail=token,
            command=None,
            stderr=_FIRMWARE_UI_TOKEN,
            requires_reboot=True,
            host_os=self._host_os,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Linux dispatch helpers
    # ------------------------------------------------------------------

    def _dispatch_linux_get_state(self) -> UefiSecureBootState:
        command = [self._typed_config.mokutil_path, "--sb-state"]
        run_result = self._runner.run(list(command), elevated=False)

        # ``mokutil --sb-state`` reports "This system doesn't support
        # Secure Boot" with a non-zero exit on some distributions; treat
        # it as a graceful "supported=False".
        combined = (run_result.stdout or "") + "\n" + (run_result.stderr or "")
        lowered = combined.lower()

        if "doesn't support secure boot" in lowered or "does not support secure boot" in lowered:
            rtnval = UefiSecureBootState(
                enabled=False,
                setup_mode=None,
                audit_mode=None,
                host_os=self._host_os,
                supported=False,
                policy_description="Secure Boot not supported",
            )
            return rtnval

        if run_result.exit_code != 0:
            raise UefiSecureBootQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        enabled = "secureboot enabled" in lowered
        rtnval = UefiSecureBootState(
            enabled=enabled,
            setup_mode=None,
            audit_mode=None,
            host_os=self._host_os,
            supported=True,
            policy_description="SecureBoot enabled" if enabled else "SecureBoot disabled",
        )
        return rtnval

    def _dispatch_linux_get_policy(self) -> UefiSecureBootPolicy:
        list_command = [self._typed_config.mokutil_path, "--list-enrolled"]
        run_result = self._runner.run(list(list_command), elevated=False)
        if run_result.exit_code != 0 and (run_result.stderr or "").strip() != "":
            # Not just an empty list — treat as a real failure.
            raise UefiSecureBootQueryError(
                command=list_command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        mok_keys = self._parse_mokutil_list_enrolled(text=run_result.stdout or "")

        # Approximate slot counts from ``efivar -l``.  When the tool is
        # missing, leave the counts as ``None``.
        efivar_command = [self._typed_config.efivar_path, "-l"]
        efivar_result = self._runner.run(list(efivar_command), elevated=False)
        if efivar_result.exit_code == 0:
            counts = self._count_efivar_slots(text=efivar_result.stdout or "")
            pk_present = counts.get("PK", 0) > 0
            kek_count: Optional[int] = counts.get("KEK", 0)
            db_count: Optional[int] = counts.get("db", 0)
            dbx_count: Optional[int] = counts.get("dbx", 0)
        else:
            pk_present = False
            kek_count = None
            db_count = None
            dbx_count = None

        rtnval = UefiSecureBootPolicy(
            pk_present=pk_present,
            kek_count=kek_count,
            db_count=db_count,
            dbx_count=dbx_count,
            mok_count=len(mok_keys),
            apple_policy_raw=None,
        )
        return rtnval

    def _dispatch_linux_list_keys(self) -> list[UefiKeyInfo]:
        list_command = [self._typed_config.mokutil_path, "--list-enrolled"]
        run_result = self._runner.run(list(list_command), elevated=False)
        if run_result.exit_code != 0 and (run_result.stderr or "").strip() != "":
            raise UefiSecureBootQueryError(
                command=list_command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = self._parse_mokutil_list_enrolled(text=run_result.stdout or "")
        return rtnval

    # ------------------------------------------------------------------
    # Darwin dispatch helpers
    # ------------------------------------------------------------------

    def _dispatch_darwin_get_state(self) -> UefiSecureBootState:
        csr_command = [self._typed_config.csrutil_path, "status"]
        csr_result = self._runner.run(list(csr_command), elevated=False)
        if csr_result.exit_code != 0:
            raise UefiSecureBootQueryError(
                command=csr_command,
                exit_code=csr_result.exit_code,
                stderr=csr_result.stderr,
            )

        bputil_command = [self._typed_config.bputil_path, "--display"]
        bputil_result = self._runner.run(list(bputil_command), elevated=False)
        if bputil_result.exit_code != 0:
            # Apple Silicon-only; on Intel ``bputil`` is absent.  Treat a
            # non-zero exit as "Apple Silicon path unavailable" and fall
            # back to csrutil-only.
            policy_text = ""
        else:
            policy_text = bputil_result.stdout or ""

        description = self._parse_bputil_display_summary(text=policy_text)
        if description is None:
            description = self._parse_csrutil_status_summary(
                text=csr_result.stdout or "",
            )

        # macOS does not expose UEFI Setup / Audit modes; we derive
        # ``enabled`` from the bputil "Full Security" / "Reduced
        # Security" / "Permissive Security" descriptor when available.
        enabled = False
        if description is not None:
            enabled = "full" in description.lower() or "reduced" in description.lower()

        rtnval = UefiSecureBootState(
            enabled=enabled,
            setup_mode=None,
            audit_mode=None,
            host_os=self._host_os,
            supported=True,
            policy_description=description,
        )
        return rtnval

    def _dispatch_darwin_get_policy(self) -> UefiSecureBootPolicy:
        bputil_command = [self._typed_config.bputil_path, "--display"]
        run_result = self._runner.run(list(bputil_command), elevated=False)
        if run_result.exit_code != 0:
            raise UefiSecureBootQueryError(
                command=bputil_command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = UefiSecureBootPolicy(
            pk_present=False,
            kek_count=None,
            db_count=None,
            dbx_count=None,
            mok_count=None,
            apple_policy_raw=run_result.stdout or "",
        )
        return rtnval

    def _dispatch_darwin_list_keys(self) -> list[UefiKeyInfo]:
        # macOS does not expose UEFI slot keys; we surface the Apple
        # Boot Policy summary as a single synthetic ``"Apple"``-slot
        # record so callers iterating the list see at least one entry.
        bputil_command = [self._typed_config.bputil_path, "--display"]
        run_result = self._runner.run(list(bputil_command), elevated=False)
        if run_result.exit_code != 0:
            raise UefiSecureBootQueryError(
                command=bputil_command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        description = self._parse_bputil_display_summary(text=run_result.stdout or "")
        if description is None:
            rtnval: list[UefiKeyInfo] = []
            return rtnval

        rtnval = [
            UefiKeyInfo(
                slot="Apple",
                subject=description,
                fingerprint=None,
                enrolled_at=None,
            ),
        ]
        return rtnval

    # ------------------------------------------------------------------
    # Windows dispatch helpers
    # ------------------------------------------------------------------

    def _dispatch_windows_get_state(self) -> UefiSecureBootState:
        command = [
            self._typed_config.powershell_path,
            "-NoProfile",
            "-Command",
            "Confirm-SecureBootUEFI",
        ]
        run_result = self._runner.run(list(command), elevated=False)

        combined = ((run_result.stdout or "") + "\n" + (run_result.stderr or "")).lower()
        if "not supported" in combined or "is not supported" in combined:
            rtnval = UefiSecureBootState(
                enabled=False,
                setup_mode=None,
                audit_mode=None,
                host_os=self._host_os,
                supported=False,
                policy_description="Confirm-SecureBootUEFI: not supported",
            )
            return rtnval

        if run_result.exit_code != 0:
            raise UefiSecureBootQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        stripped = (run_result.stdout or "").strip().lower()
        enabled = stripped == "true"
        rtnval = UefiSecureBootState(
            enabled=enabled,
            setup_mode=None,
            audit_mode=None,
            host_os=self._host_os,
            supported=True,
            policy_description="Confirm-SecureBootUEFI: " + stripped,
        )
        return rtnval

    def _dispatch_windows_get_policy(self) -> UefiSecureBootPolicy:
        command = [
            self._typed_config.powershell_path,
            "-NoProfile",
            "-Command",
            "Get-SecureBootPolicy | ConvertTo-Json",
        ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UefiSecureBootQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        text = run_result.stdout or ""
        publisher_present = "publisher" in text.lower()
        rtnval = UefiSecureBootPolicy(
            pk_present=publisher_present,
            kek_count=None,
            db_count=None,
            dbx_count=None,
            mok_count=0,
            apple_policy_raw=None,
        )
        return rtnval

    def _dispatch_windows_list_keys(self) -> list[UefiKeyInfo]:
        command = [
            self._typed_config.powershell_path,
            "-NoProfile",
            "-Command",
            "Get-SecureBootPolicy | ConvertTo-Json",
        ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UefiSecureBootQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        text = run_result.stdout or ""
        if "publisher" not in text.lower():
            rtnval: list[UefiKeyInfo] = []
            return rtnval

        # Best-effort extraction of the Publisher GUID; we do not parse
        # JSON here to avoid an extra dependency in this layer.
        publisher_value: Optional[str] = None
        for raw_line in text.splitlines():
            line = raw_line.strip().rstrip(",")
            if "publisher" in line.lower() and ":" in line:
                _key, _, value = line.partition(":")
                publisher_value = value.strip().strip('"')
                break

        rtnval = [
            UefiKeyInfo(
                slot="PK",
                subject=publisher_value,
                fingerprint=None,
                enrolled_at=None,
            ),
        ]
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _safe_list_enrolled_keys(
        self,
        *,
        slot: Optional[str],
    ) -> Optional[list[UefiKeyInfo]]:
        try:
            rtnval = self.list_enrolled_keys(slot=slot)
        except UefiSecureBootQueryError:
            rtnval = None
        return rtnval

    def _dry_run_result(
        self,
        *,
        command: Optional[list[str]],
        detail: Optional[str],
        requires_reboot: bool,
    ) -> UefiSecureBootActionResult:
        rtnval = self._wrap_result(
            status=self.status_class.DRY_RUN,
            detail=detail,
            command=command,
            requires_reboot=requires_reboot,
            host_os=self._host_os,
        )
        return rtnval

    def _build_dry_run_enroll_command(
        self,
        *,
        path: str,
    ) -> Optional[list[str]]:
        if self._host_os == HOST_OS_LINUX:
            rtnval = [self._typed_config.mokutil_path, "--import", path]
        else:
            rtnval = None
        return rtnval

    def _build_dry_run_reset_command(
        self,
        *,
        key_id: str,
    ) -> Optional[list[str]]:
        if self._host_os == HOST_OS_LINUX:
            rtnval = [self._typed_config.mokutil_path, "--delete", key_id]
        else:
            rtnval = None
        return rtnval

    def _wrap_result(
        self,
        *,
        status,
        detail: Optional[str] = None,
        exit_code: Optional[int] = None,
        stdout: Optional[str] = None,
        stderr: Optional[str] = None,
        command: Optional[list[str]] = None,
        **extra,
    ) -> UefiSecureBootActionResult:
        if "host_os_field" in extra and "host_os" not in extra:
            extra["host_os"] = extra.pop("host_os_field")
        elif "host_os_field" in extra:
            extra.pop("host_os_field")
        if "host_os" not in extra:
            extra["host_os"] = self._host_os

        rtnval = super()._wrap_result(
            status=status,
            detail=detail,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            command=command,
            **extra,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    def _parse_mokutil_list_enrolled(
        self,
        *,
        text: str,
    ) -> list[UefiKeyInfo]:
        """
            Parse ``mokutil --list-enrolled`` output into MOK key records.

            Sections are delimited by ``[key N]`` headers.  Each section
            yields one :class:`UefiKeyInfo` with ``slot="MOK"`` and best-
            effort ``fingerprint``, ``subject`` extraction.
        """
        rtnval: list[UefiKeyInfo] = []
        current_fingerprint: Optional[str] = None
        current_subject: Optional[str] = None
        in_section = False

        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if stripped.startswith("[key"):
                if in_section:
                    rtnval.append(UefiKeyInfo(
                        slot="MOK",
                        subject=current_subject,
                        fingerprint=current_fingerprint,
                        enrolled_at=None,
                    ))
                in_section = True
                current_fingerprint = None
                current_subject = None
                continue

            lowered = stripped.lower()
            if lowered.startswith("sha1 fingerprint:"):
                _, _, value = stripped.partition(":")
                current_fingerprint = value.strip()
                continue
            if lowered.startswith("sha256 fingerprint:"):
                _, _, value = stripped.partition(":")
                if current_fingerprint is None:
                    current_fingerprint = value.strip()
                continue
            if lowered.startswith("subject:"):
                _, _, value = stripped.partition(":")
                current_subject = value.strip()
                continue

        if in_section:
            rtnval.append(UefiKeyInfo(
                slot="MOK",
                subject=current_subject,
                fingerprint=current_fingerprint,
                enrolled_at=None,
            ))

        return rtnval

    def _count_efivar_slots(self, *, text: str) -> dict:
        rtnval = {"PK": 0, "KEK": 0, "db": 0, "dbx": 0}
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.endswith("-PK"):
                rtnval["PK"] += 1
            elif line.endswith("-KEK"):
                rtnval["KEK"] += 1
            elif line.endswith("-db"):
                rtnval["db"] += 1
            elif line.endswith("-dbx"):
                rtnval["dbx"] += 1
        return rtnval

    def _parse_bputil_display_summary(self, *, text: str) -> Optional[str]:
        if text is None or len(text) == 0:
            rtnval: Optional[str] = None
            return rtnval

        # First pass: look for the canonical Apple Boot Policy descriptor
        # anywhere in the output.
        for raw_line in text.splitlines():
            lowered = raw_line.lower()
            if "full security" in lowered:
                rtnval = "Full Security"
                return rtnval
            if "reduced security" in lowered:
                rtnval = "Reduced Security"
                return rtnval
            if "permissive security" in lowered:
                rtnval = "Permissive Security"
                return rtnval

        # Fallback: surface the first non-empty line so coordinators
        # still see something.
        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if len(stripped) > 0:
                rtnval = stripped
                return rtnval

        rtnval = None
        return rtnval

    def _parse_csrutil_status_summary(self, *, text: str) -> Optional[str]:
        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if len(stripped) > 0:
                rtnval: Optional[str] = stripped
                return rtnval
        rtnval = None
        return rtnval
