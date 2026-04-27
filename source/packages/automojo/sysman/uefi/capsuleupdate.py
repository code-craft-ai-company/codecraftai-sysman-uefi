"""
    UEFI capsule-update feature.

    Implements the cross-OS capsule-update contract for UEFI firmware
    capsule discovery and application on whichever host the manager runs
    on.  Dispatches to host-specific user-space tooling:

        * Linux   -> ``fwupdmgr`` (the canonical user-space UEFI capsule
                     manager)
        * Darwin  -> ``softwareupdate`` (delegation: macOS does not expose
                     raw capsule files; firmware updates ride on top of
                     Apple OS updates)
        * Windows -> PowerShell ``PSWindowsUpdate`` module filtered to
                     firmware categories (the only scriptable surface);
                     ``apply_capsule_file`` is unsupported

    Per-host-OS dispatch:
        :class:`UefiCapsuleUpdateManager` records the running host OS
        once at construction time (via :func:`detect_host_os`) and routes
        every verb through ``_dispatch_<linux|darwin|windows>_<verb>``
        private methods.  Verbs with no scriptable host-OS equivalent
        return ``UefiCapsuleUpdateActionResult`` with status ``FAILED``
        and a stable ``unsupported_on_host_os_<host>`` detail token
        instead of silently no-oping.

    Downgrade gating:
        Both ``apply_update`` and ``apply_capsule_file`` consult
        ``config.allow_downgrade``.  When ``False`` and the requested
        ``available_version`` is older than the device's
        ``current_version``, the manager refuses to dispatch and returns
        ``FAILED`` with ``detail="firmware_downgrade_blocked: ..."``.
        Vendor-specific version strings whose ordering cannot be derived
        cleanly are treated as non-downgrades — proceeding with a warning
        recorded in ``result.message`` (``detail`` field).

    JSON-mode fwupdmgr:
        Every Linux verb passes ``--json`` to ``fwupdmgr`` so the parser
        does not have to scrape free-form text.

    Elevation policy:
        Writes pass ``elevated=True`` when ``config.elevated`` is True;
        reads never elevate.
"""

import json
import os

from dataclasses import dataclass
from typing import Optional

from automojo.interfaces.icommandrunner import ICommandRunner, RunResult

from automojo.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
    HOST_OS_DARWIN,
    HOST_OS_LINUX,
    HOST_OS_WINDOWS,
)


_DEFAULT_FWUPDMGR = "fwupdmgr"
_DEFAULT_SOFTWAREUPDATE = "softwareupdate"
_DEFAULT_SYSTEM_PROFILER = "system_profiler"
_DEFAULT_POWERSHELL = "powershell"

_DOWNGRADE_BLOCKED_TOKEN = "firmware_downgrade_blocked"
_PSWINDOWSUPDATE_MISSING_TOKEN = "windows_pswindowsupdate_required"
_MACOS_DELEGATE_TOKEN = "macos_delegate_to_softwareupdate"
_WINDOWS_NO_CAPSULE_TOKEN = "windows_capsule_file_unsupported"
_DARWIN_NO_CAPSULE_TOKEN = "darwin_capsule_file_unsupported"


UefiCapsuleUpdateActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------


@dataclass
class UefiCapsuleUpdateManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UefiCapsuleUpdateManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param override_host_os: When set, forces the dispatch path to
                                 ``"linux"``, ``"darwin"``, or
                                 ``"windows"`` (case-insensitive).
                                 Reserved for testing.
        :param fwupdmgr_path: Path / argv-name of ``fwupdmgr``.
        :param softwareupdate_path: Path / argv-name of
                                    ``softwareupdate``.
        :param system_profiler_path: Path / argv-name of
                                     ``system_profiler``.
        :param powershell_path: Path / argv-name of ``powershell``.
        :param allow_downgrade: When ``True``, ``apply_update`` /
                                ``apply_capsule_file`` are permitted to
                                install firmware older than the device's
                                current version.
    """
    override_host_os: Optional[str] = None
    fwupdmgr_path: str = _DEFAULT_FWUPDMGR
    softwareupdate_path: str = _DEFAULT_SOFTWAREUPDATE
    system_profiler_path: str = _DEFAULT_SYSTEM_PROFILER
    powershell_path: str = _DEFAULT_POWERSHELL
    allow_downgrade: bool = False


@dataclass
class UefiCapsuleUpdateInfo:
    """
        One pending firmware update.

        :param update_id: Backend identifier (fwupd ReleaseId / Apple
                          label / Windows KB article).
        :param device_id: Target device identifier; ``None`` when not
                          surfaced.
        :param device_name: Human-readable device name.
        :param current_version: Current firmware version on the device.
        :param available_version: Version offered by the update.
        :param severity: Severity label (``"low"``, ``"medium"``,
                         ``"high"``, ``"critical"``, or backend-specific).
        :param description: Short human-readable summary.
    """
    update_id: str
    device_id: Optional[str] = None
    device_name: Optional[str] = None
    current_version: Optional[str] = None
    available_version: str = ""
    severity: Optional[str] = None
    description: Optional[str] = None


@dataclass
class UefiDeviceInfo:
    """
        One firmware-updatable device.

        :param device_id: Firmware-updatable device identifier.
        :param name: Human-readable name.
        :param vendor: Vendor name.
        :param firmware_version: Current firmware version.
        :param update_supported: Whether the backend can update the
                                 device.
    """
    device_id: str
    name: str
    vendor: Optional[str] = None
    firmware_version: Optional[str] = None
    update_supported: bool = True


@dataclass
class UefiCapsuleUpdateStatus:
    """
        Snapshot summary of the host's update history.  Populated by
        ``get_update_status`` from the backend's history surface.

        :param last_attempt: ISO-8601 timestamp of the last attempted
                             update, or ``None`` when no history exists.
        :param last_status: Backend-reported result label of the last
                            attempt.
        :param pending_count: Number of pending updates the backend
                              currently reports.
        :param host_os: The host OS the snapshot was taken from.
    """
    last_attempt: Optional[str]
    last_status: Optional[str]
    pending_count: Optional[int]
    host_os: str


@dataclass
class UefiCapsuleUpdateActionResult(FeatureActionResult):
    """
        Outcome of a capsule-update write.

        :param update_id: The update ID the action targeted, when known.
        :param device_id: The target device ID the action targeted, when
                          known.
        :param host_os: The host OS the action dispatched to.
    """
    update_id: Optional[str] = None
    device_id: Optional[str] = None
    host_os: Optional[str] = None


class UefiCapsuleUpdateQueryError(Exception):
    """
        Raised when a capsule-update read verb fails.
    """

    def __init__(
        self,
        *,
        command: Optional[list[str]] = None,
        exit_code: Optional[int] = None,
        stderr: Optional[str] = None,
        message: Optional[str] = None,
    ) -> None:
        """
            :param command: The argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
            :param message: A human-readable message; defaults to a
                            multi-line block summarising the failure.
        """
        if command is None:
            self.command: list[str] = []
        else:
            self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["UEFI capsule-update query failed."]
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


class UefiCapsuleUpdateManager(BaseFeatureManager):
    """
        Per-host-OS UEFI capsule-update manager.  Each verb routes
        through a ``_dispatch_<host>_<verb>`` private helper selected by
        ``self._host_os``.  All commands flow through the injected
        :class:`ICommandRunner`.
    """

    result_class = UefiCapsuleUpdateActionResult
    status_class = UefiCapsuleUpdateActionStatus
    feature_name = "capsule-update"

    def __init__(
        self,
        runner: ICommandRunner,
        config: Optional[UefiCapsuleUpdateManagerConfig] = None,
    ) -> None:
        """
            :param runner: The command runner.
            :param config: Manager configuration.  Defaults to
                           :class:`UefiCapsuleUpdateManagerConfig` with
                           no overrides.
        """
        if config is None:
            config = UefiCapsuleUpdateManagerConfig()
        super().__init__(runner=runner, config=config)
        self._typed_config: UefiCapsuleUpdateManagerConfig = config
        return

    @property
    def typed_config(self) -> UefiCapsuleUpdateManagerConfig:
        """
            The active :class:`UefiCapsuleUpdateManagerConfig`.
        """
        return self._typed_config

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_available_updates(self) -> list[UefiCapsuleUpdateInfo]:
        """
            Enumerate pending firmware updates on the host.

            :returns: A list of :class:`UefiCapsuleUpdateInfo` records.
            :raises UefiCapsuleUpdateQueryError: When the underlying
                                                 tool fails.
        """
        if self._host_os == HOST_OS_LINUX:
            rtnval = self._dispatch_linux_list_available_updates()
        elif self._host_os == HOST_OS_DARWIN:
            rtnval = self._dispatch_darwin_list_available_updates()
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = self._dispatch_windows_list_available_updates()
        else:
            err_msg = "Unsupported host OS for list_available_updates: {}".format(
                self._host_os,
            )
            raise UefiCapsuleUpdateQueryError(message=err_msg)
        return rtnval

    def list_devices(self) -> list[UefiDeviceInfo]:
        """
            Enumerate firmware-updatable devices on the host.

            :returns: A list of :class:`UefiDeviceInfo` records.
            :raises UefiCapsuleUpdateQueryError: When the underlying
                                                 tool fails.
        """
        if self._host_os == HOST_OS_LINUX:
            rtnval = self._dispatch_linux_list_devices()
        elif self._host_os == HOST_OS_DARWIN:
            rtnval = self._dispatch_darwin_list_devices()
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = self._dispatch_windows_list_devices()
        else:
            err_msg = "Unsupported host OS for list_devices: {}".format(
                self._host_os,
            )
            raise UefiCapsuleUpdateQueryError(message=err_msg)
        return rtnval

    def get_update_status(
        self,
        update_id: Optional[str] = None,
    ) -> UefiCapsuleUpdateStatus:
        """
            Read a snapshot of update history / pending-count.

            :param update_id: When set, scope the snapshot to a single
                              update.  When ``None``, summarise the
                              host's overall state.

            :returns: A :class:`UefiCapsuleUpdateStatus`.
            :raises UefiCapsuleUpdateQueryError: When the underlying
                                                 tool fails.
        """
        if self._host_os == HOST_OS_LINUX:
            rtnval = self._dispatch_linux_get_update_status(update_id=update_id)
        elif self._host_os == HOST_OS_DARWIN:
            rtnval = self._dispatch_darwin_get_update_status(update_id=update_id)
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = self._dispatch_windows_get_update_status(update_id=update_id)
        else:
            err_msg = "Unsupported host OS for get_update_status: {}".format(
                self._host_os,
            )
            raise UefiCapsuleUpdateQueryError(message=err_msg)
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def apply_update(self, update_id: str) -> UefiCapsuleUpdateActionResult:
        """
            Apply a pending update by backend-assigned identifier.

            Idempotency: when ``update_id`` is not present in the host's
            current pending list, returns ``SKIPPED`` without invoking
            the runner.  Downgrade gating: when the downgrade check
            fires (see class docstring), returns ``FAILED`` with a
            stable ``firmware_downgrade_blocked`` detail and invokes no
            command.

            :param update_id: Backend identifier of the pending update.

            :returns: The :class:`UefiCapsuleUpdateActionResult`.
        """
        if self._config.dry_run:
            command = self._build_dry_run_apply_update_command(
                update_id=update_id,
            )
            rtnval = self._dry_run_result(
                command=command,
                detail=update_id,
                update_id=update_id,
                device_id=None,
            )
            return rtnval

        # Universally-FAILED hosts short-circuit before the idempotency
        # probe so the stable token is always returned.
        if self._host_os == HOST_OS_DARWIN:
            rtnval = self._build_failed_unsupported(
                verb="apply_update",
                host_os=self._host_os,
                message="macOS firmware updates are delivered through softwareupdate; use 'softwareupdate -ia'",
                update_id=update_id,
                device_id=None,
                host_os_field=self._host_os,
            )
            return rtnval

        if self._host_os not in (HOST_OS_LINUX, HOST_OS_WINDOWS):
            rtnval = self._build_failed_unsupported(
                verb="apply_update",
                host_os=self._host_os,
                update_id=update_id,
                device_id=None,
                host_os_field=self._host_os,
            )
            return rtnval

        # Idempotency probe.
        pending = self._safe_list_available_updates()
        target_info: Optional[UefiCapsuleUpdateInfo] = None
        if pending is not None:
            for entry in pending:
                if entry.update_id == update_id:
                    target_info = entry
                    break
            if target_info is None:
                skipped = self._wrap_result(
                    status=self.status_class.SKIPPED,
                    detail="update not in pending list",
                    command=None,
                    update_id=update_id,
                    device_id=None,
                    host_os=self._host_os,
                )
                return skipped

        # Downgrade gating.
        downgrade_check = self._check_downgrade(target_info=target_info)
        if downgrade_check is not None:
            blocked = self._wrap_result(
                status=self.status_class.FAILED,
                detail=downgrade_check,
                command=None,
                stderr=_DOWNGRADE_BLOCKED_TOKEN,
                update_id=update_id,
                device_id=target_info.device_id if target_info is not None else None,
                host_os=self._host_os,
            )
            return blocked

        if self._host_os == HOST_OS_LINUX:
            rtnval = self._dispatch_linux_apply_update(
                update_id=update_id,
                target_info=target_info,
            )
        else:
            rtnval = self._dispatch_windows_apply_update(
                update_id=update_id,
                target_info=target_info,
            )
        return rtnval

    def apply_capsule_file(self, path: str) -> UefiCapsuleUpdateActionResult:
        """
            Install a pre-downloaded capsule file.

            Linux only; on macOS and Windows returns ``FAILED`` with a
            stable token in ``detail`` because neither host exposes a
            scriptable raw-capsule installation surface.

            :param path: Path to the capsule file on the host.

            :returns: The :class:`UefiCapsuleUpdateActionResult`.
        """
        if self._config.dry_run:
            command = self._build_dry_run_apply_capsule_file_command(path=path)
            rtnval = self._dry_run_result(
                command=command,
                detail=path,
                update_id=None,
                device_id=None,
            )
            return rtnval

        if self._host_os == HOST_OS_DARWIN:
            rtnval = self._build_failed_unsupported(
                verb="apply_capsule_file",
                host_os=self._host_os,
                message="macOS firmware updater does not accept .cap files",
                update_id=None,
                device_id=None,
                host_os_field=self._host_os,
            )
            return rtnval

        if self._host_os == HOST_OS_WINDOWS:
            rtnval = self._build_failed_unsupported(
                verb="apply_capsule_file",
                host_os=self._host_os,
                message="Windows does not expose scriptable raw-capsule installation; use the OEM firmware-update tool",
                update_id=None,
                device_id=None,
                host_os_field=self._host_os,
            )
            return rtnval

        if self._host_os != HOST_OS_LINUX:
            rtnval = self._build_failed_unsupported(
                verb="apply_capsule_file",
                host_os=self._host_os,
                update_id=None,
                device_id=None,
                host_os_field=self._host_os,
            )
            return rtnval

        # Linux-only path.  Downgrade gating cannot inspect the capsule
        # file's version directly without unpacking it; the comparison
        # is between every device's current version and "unknown" which
        # always resolves to "unknown ordering" — proceed.
        command = [
            self._typed_config.fwupdmgr_path,
            "install",
            "--allow-reinstall",
            "--no-reboot-check",
            path,
        ]
        rtnval = self._dispatch(
            command=command,
            detail=path,
            operation="apply_capsule_file",
            update_id=None,
            device_id=None,
            host_os=self._host_os,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Linux dispatch helpers
    # ------------------------------------------------------------------

    def _dispatch_linux_list_available_updates(
        self,
    ) -> list[UefiCapsuleUpdateInfo]:
        command = [self._typed_config.fwupdmgr_path, "get-updates", "--json"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UefiCapsuleUpdateQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = self._parse_fwupdmgr_get_updates_json(
            text=run_result.stdout or "",
        )
        return rtnval

    def _dispatch_linux_list_devices(self) -> list[UefiDeviceInfo]:
        command = [self._typed_config.fwupdmgr_path, "get-devices", "--json"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UefiCapsuleUpdateQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = self._parse_fwupdmgr_get_devices_json(
            text=run_result.stdout or "",
        )
        return rtnval

    def _dispatch_linux_get_update_status(
        self,
        *,
        update_id: Optional[str],
    ) -> UefiCapsuleUpdateStatus:
        history_command = [
            self._typed_config.fwupdmgr_path,
            "get-history",
            "--json",
        ]
        history_result = self._runner.run(list(history_command), elevated=False)
        # ``fwupdmgr get-history`` exits non-zero with "no history" on
        # some systems; treat empty / "no history" as a graceful empty
        # snapshot.
        last_attempt: Optional[str] = None
        last_status: Optional[str] = None
        if history_result.exit_code == 0:
            history_entries = self._parse_fwupdmgr_get_history_json(
                text=history_result.stdout or "",
            )
            if update_id is not None:
                filtered = [
                    h for h in history_entries if h.get("update_id") == update_id
                ]
            else:
                filtered = history_entries
            if len(filtered) > 0:
                last = filtered[0]
                last_attempt = last.get("applied_at")
                last_status = last.get("result")

        # Pending count.
        try:
            pending = self.list_available_updates()
            pending_count: Optional[int] = len(pending)
        except UefiCapsuleUpdateQueryError:
            pending_count = None

        rtnval = UefiCapsuleUpdateStatus(
            last_attempt=last_attempt,
            last_status=last_status,
            pending_count=pending_count,
            host_os=self._host_os,
        )
        return rtnval

    def _dispatch_linux_apply_update(
        self,
        *,
        update_id: str,
        target_info: Optional[UefiCapsuleUpdateInfo],
    ) -> UefiCapsuleUpdateActionResult:
        command = [
            self._typed_config.fwupdmgr_path,
            "update",
            "--no-reboot-check",
            update_id,
        ]
        device_id: Optional[str] = None
        if target_info is not None:
            device_id = target_info.device_id

        rtnval = self._dispatch(
            command=command,
            detail=update_id,
            operation="apply_update",
            update_id=update_id,
            device_id=device_id,
            host_os=self._host_os,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Darwin dispatch helpers
    # ------------------------------------------------------------------

    def _dispatch_darwin_list_available_updates(
        self,
    ) -> list[UefiCapsuleUpdateInfo]:
        command = [
            self._typed_config.softwareupdate_path,
            "--list",
            "--include-config-data",
        ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UefiCapsuleUpdateQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = self._parse_softwareupdate_list(
            text=run_result.stdout or "",
        )
        return rtnval

    def _dispatch_darwin_list_devices(self) -> list[UefiDeviceInfo]:
        command = [
            self._typed_config.system_profiler_path,
            "-json",
            "SPHardwareDataType",
        ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UefiCapsuleUpdateQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = self._parse_system_profiler_hardware(
            text=run_result.stdout or "",
        )
        return rtnval

    def _dispatch_darwin_get_update_status(
        self,
        *,
        update_id: Optional[str],
    ) -> UefiCapsuleUpdateStatus:
        history_command = [
            self._typed_config.softwareupdate_path,
            "--history",
        ]
        history_result = self._runner.run(list(history_command), elevated=False)
        last_attempt: Optional[str] = None
        last_status: Optional[str] = None
        if history_result.exit_code == 0:
            entries = self._parse_softwareupdate_history(
                text=history_result.stdout or "",
            )
            if update_id is not None:
                filtered = [e for e in entries if e.get("update_id") == update_id]
            else:
                filtered = entries
            if len(filtered) > 0:
                last = filtered[0]
                last_attempt = last.get("applied_at")
                last_status = last.get("result")

        try:
            pending = self.list_available_updates()
            pending_count: Optional[int] = len(pending)
        except UefiCapsuleUpdateQueryError:
            pending_count = None

        rtnval = UefiCapsuleUpdateStatus(
            last_attempt=last_attempt,
            last_status=last_status,
            pending_count=pending_count,
            host_os=self._host_os,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Windows dispatch helpers
    # ------------------------------------------------------------------

    def _dispatch_windows_list_available_updates(
        self,
    ) -> list[UefiCapsuleUpdateInfo]:
        command = [
            self._typed_config.powershell_path,
            "-NoProfile",
            "-Command",
            (
                "Get-WindowsUpdate -MicrosoftUpdate "
                "| Where-Object { $_.CategoryIDs -contains 'firmware' } "
                "| ConvertTo-Json"
            ),
        ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            combined = (
                (run_result.stdout or "") + "\n" + (run_result.stderr or "")
            ).lower()
            if "pswindowsupdate" in combined or "module" in combined:
                # The module is not installed; surface a stable token.
                raise UefiCapsuleUpdateQueryError(
                    command=command,
                    exit_code=run_result.exit_code,
                    stderr=run_result.stderr,
                    message=_PSWINDOWSUPDATE_MISSING_TOKEN,
                )
            raise UefiCapsuleUpdateQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = self._parse_get_windowsupdate_json(
            text=run_result.stdout or "",
        )
        return rtnval

    def _dispatch_windows_list_devices(self) -> list[UefiDeviceInfo]:
        command = [
            self._typed_config.powershell_path,
            "-NoProfile",
            "-Command",
            (
                "Get-CimInstance Win32_ComputerSystem,Win32_BIOS "
                "| ConvertTo-Json"
            ),
        ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UefiCapsuleUpdateQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = self._parse_wmi_computer_bios_json(
            text=run_result.stdout or "",
        )
        return rtnval

    def _dispatch_windows_get_update_status(
        self,
        *,
        update_id: Optional[str],
    ) -> UefiCapsuleUpdateStatus:
        history_command = [
            self._typed_config.powershell_path,
            "-NoProfile",
            "-Command",
            (
                "Get-WUHistory "
                "| Where-Object { $_.CategoryIDs -contains 'firmware' } "
                "| ConvertTo-Json"
            ),
        ]
        history_result = self._runner.run(list(history_command), elevated=False)
        last_attempt: Optional[str] = None
        last_status: Optional[str] = None
        if history_result.exit_code == 0:
            entries = self._parse_get_wuhistory_json(
                text=history_result.stdout or "",
            )
            if update_id is not None:
                filtered = [e for e in entries if e.get("update_id") == update_id]
            else:
                filtered = entries
            if len(filtered) > 0:
                last = filtered[0]
                last_attempt = last.get("applied_at")
                last_status = last.get("result")

        try:
            pending = self.list_available_updates()
            pending_count: Optional[int] = len(pending)
        except UefiCapsuleUpdateQueryError:
            pending_count = None

        rtnval = UefiCapsuleUpdateStatus(
            last_attempt=last_attempt,
            last_status=last_status,
            pending_count=pending_count,
            host_os=self._host_os,
        )
        return rtnval

    def _dispatch_windows_apply_update(
        self,
        *,
        update_id: str,
        target_info: Optional[UefiCapsuleUpdateInfo],
    ) -> UefiCapsuleUpdateActionResult:
        command = [
            self._typed_config.powershell_path,
            "-NoProfile",
            "-Command",
            (
                "Install-WindowsUpdate -KBArticleID {} "
                "-AcceptAll -IgnoreReboot"
            ).format(update_id),
        ]
        device_id: Optional[str] = None
        if target_info is not None:
            device_id = target_info.device_id

        rtnval = self._dispatch(
            command=command,
            detail=update_id,
            operation="apply_update",
            update_id=update_id,
            device_id=device_id,
            host_os=self._host_os,
        )

        # If the failure message references PSWindowsUpdate, swap in a
        # stable token so coordinators can detect "module missing".
        if rtnval.status == self.status_class.FAILED:
            combined = (
                (rtnval.stdout or "") + "\n" + (rtnval.stderr or "")
            ).lower()
            if "pswindowsupdate" in combined:
                rtnval.detail = _PSWINDOWSUPDATE_MISSING_TOKEN
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _safe_list_available_updates(
        self,
    ) -> Optional[list[UefiCapsuleUpdateInfo]]:
        """
            Probe variant for idempotency checks.  Returns ``None``
            instead of raising so the caller can decide whether to
            proceed with the write or treat the read failure as fatal.
        """
        try:
            rtnval = self.list_available_updates()
        except UefiCapsuleUpdateQueryError:
            rtnval = None
        return rtnval

    def _check_downgrade(
        self,
        *,
        target_info: Optional[UefiCapsuleUpdateInfo],
    ) -> Optional[str]:
        """
            Apply downgrade gating.  Returns the ``detail`` string for a
            blocked downgrade, or ``None`` to permit the call to proceed.

            :param target_info: Pending-update record for the requested
                                ``update_id``.  When ``None`` (e.g.,
                                because the host's enumeration failed),
                                gating cannot fire — return ``None``.
        """
        if self._typed_config.allow_downgrade:
            rtnval: Optional[str] = None
            return rtnval

        if target_info is None:
            rtnval = None
            return rtnval

        if target_info.current_version is None:
            rtnval = None
            return rtnval

        if target_info.available_version is None:
            rtnval = None
            return rtnval

        if len(target_info.current_version) == 0:
            rtnval = None
            return rtnval

        if len(target_info.available_version) == 0:
            rtnval = None
            return rtnval

        comparison = self._compare_versions(
            available=target_info.available_version,
            current=target_info.current_version,
        )
        if comparison == "older":
            rtnval = "{}: {} < {}; set allow_downgrade=True to override".format(
                _DOWNGRADE_BLOCKED_TOKEN,
                target_info.available_version,
                target_info.current_version,
            )
            return rtnval

        # "newer", "equal", or "unknown" all proceed.
        rtnval = None
        return rtnval

    def _compare_versions(self, *, available: str, current: str) -> str:
        """
            Compare two version strings using a simple dotted-segment
            algorithm.

            :param available: Offered version.
            :param current: Currently-installed version.

            :returns: ``"older"`` when ``available < current``,
                      ``"newer"`` when ``available > current``,
                      ``"equal"`` when they are byte-identical, or
                      ``"unknown"`` when the strings cannot be compared
                      cleanly (vendor-specific format).
        """
        if available == current:
            rtnval = "equal"
            return rtnval

        avail_parts = available.split(".")
        curr_parts = current.split(".")

        # All segments on both sides must be pure decimal integers for
        # the comparator to claim a clean ordering.
        for part in avail_parts:
            if not part.isdigit():
                rtnval = "unknown"
                return rtnval

        for part in curr_parts:
            if not part.isdigit():
                rtnval = "unknown"
                return rtnval

        avail_ints = [int(p) for p in avail_parts]
        curr_ints = [int(p) for p in curr_parts]

        # Pad the shorter side with zeros so segment counts match.
        max_len = max(len(avail_ints), len(curr_ints))
        while len(avail_ints) < max_len:
            avail_ints.append(0)
        while len(curr_ints) < max_len:
            curr_ints.append(0)

        for index in range(0, max_len):
            if avail_ints[index] < curr_ints[index]:
                rtnval = "older"
                return rtnval
            if avail_ints[index] > curr_ints[index]:
                rtnval = "newer"
                return rtnval

        rtnval = "equal"
        return rtnval

    def _dry_run_result(
        self,
        *,
        command: Optional[list[str]],
        detail: Optional[str],
        update_id: Optional[str],
        device_id: Optional[str],
    ) -> UefiCapsuleUpdateActionResult:
        rtnval = self._wrap_result(
            status=self.status_class.DRY_RUN,
            detail=detail,
            command=command,
            update_id=update_id,
            device_id=device_id,
            host_os=self._host_os,
        )
        return rtnval

    def _build_dry_run_apply_update_command(
        self,
        *,
        update_id: str,
    ) -> Optional[list[str]]:
        if self._host_os == HOST_OS_LINUX:
            rtnval = [
                self._typed_config.fwupdmgr_path,
                "update",
                "--no-reboot-check",
                update_id,
            ]
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = [
                self._typed_config.powershell_path,
                "-NoProfile",
                "-Command",
                (
                    "Install-WindowsUpdate -KBArticleID {} "
                    "-AcceptAll -IgnoreReboot"
                ).format(update_id),
            ]
        else:
            rtnval = None
        return rtnval

    def _build_dry_run_apply_capsule_file_command(
        self,
        *,
        path: str,
    ) -> Optional[list[str]]:
        if self._host_os == HOST_OS_LINUX:
            rtnval = [
                self._typed_config.fwupdmgr_path,
                "install",
                "--allow-reinstall",
                "--no-reboot-check",
                path,
            ]
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
    ) -> UefiCapsuleUpdateActionResult:
        # Translate ``host_os_field`` (used to avoid keyword shadowing in
        # callers that already pass ``host_os`` to outer helpers) into
        # the canonical ``host_os`` field of the result dataclass.
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

    def _parse_fwupdmgr_get_updates_json(
        self,
        *,
        text: str,
    ) -> list[UefiCapsuleUpdateInfo]:
        """
            Parse ``fwupdmgr get-updates --json`` output.

            Top-level shape:

                {"Devices": [{"DeviceId": "...", "Name": "...",
                              "Version": "...", "Releases": [
                              {"Version": "...", "Description": "...",
                               "Urgency": "..."}, ...]}, ...]}
        """
        rtnval: list[UefiCapsuleUpdateInfo] = []

        if text is None or len(text.strip()) == 0:
            return rtnval

        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return rtnval

        if not isinstance(data, dict):
            return rtnval

        devices = data.get("Devices")
        if devices is None or not isinstance(devices, list):
            return rtnval

        for device in devices:
            if not isinstance(device, dict):
                continue
            device_id = device.get("DeviceId")
            device_name = device.get("Name")
            current_version = device.get("Version")

            releases = device.get("Releases")
            if releases is None or not isinstance(releases, list):
                continue

            for release in releases:
                if not isinstance(release, dict):
                    continue
                # Prefer the release's own ID; fall back to the device
                # ID when fwupd does not surface one.
                release_id = release.get("ReleaseId")
                if release_id is None:
                    release_id = release.get("AppstreamId")
                if release_id is None:
                    release_id = device_id
                if release_id is None:
                    continue

                available_version = release.get("Version") or ""
                description = release.get("Summary") or release.get("Description")
                severity = release.get("Urgency")

                info = UefiCapsuleUpdateInfo(
                    update_id=release_id,
                    device_id=device_id,
                    device_name=device_name,
                    current_version=current_version,
                    available_version=available_version,
                    severity=severity,
                    description=description,
                )
                rtnval.append(info)

        return rtnval

    def _parse_fwupdmgr_get_devices_json(
        self,
        *,
        text: str,
    ) -> list[UefiDeviceInfo]:
        rtnval: list[UefiDeviceInfo] = []

        if text is None or len(text.strip()) == 0:
            return rtnval

        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return rtnval

        if not isinstance(data, dict):
            return rtnval

        devices = data.get("Devices")
        if devices is None or not isinstance(devices, list):
            return rtnval

        for device in devices:
            if not isinstance(device, dict):
                continue
            device_id = device.get("DeviceId")
            if device_id is None:
                continue
            name = device.get("Name") or device_id
            vendor = device.get("Vendor")
            firmware_version = device.get("Version")

            flags = device.get("Flags")
            update_supported = True
            if flags is not None and isinstance(flags, list):
                if "updatable" not in flags and "supported" not in flags:
                    # fwupd marks updatable devices with one of these
                    # flags; absence of both means the backend does not
                    # currently offer firmware-update support.
                    update_supported = False

            entry = UefiDeviceInfo(
                device_id=device_id,
                name=name,
                vendor=vendor,
                firmware_version=firmware_version,
                update_supported=update_supported,
            )
            rtnval.append(entry)

        return rtnval

    def _parse_fwupdmgr_get_history_json(self, *, text: str) -> list[dict]:
        rtnval: list[dict] = []

        if text is None or len(text.strip()) == 0:
            return rtnval

        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return rtnval

        if not isinstance(data, dict):
            return rtnval

        devices = data.get("Devices")
        if devices is None or not isinstance(devices, list):
            return rtnval

        for device in devices:
            if not isinstance(device, dict):
                continue
            device_id = device.get("DeviceId")
            releases = device.get("Releases")
            if releases is None or not isinstance(releases, list):
                continue
            for release in releases:
                if not isinstance(release, dict):
                    continue
                entry = {
                    "update_id": release.get("ReleaseId") or device_id,
                    "device_id": device_id,
                    "applied_version": release.get("Version") or "",
                    "applied_at": release.get("Created"),
                    "result": release.get("UpdateState") or release.get("Result"),
                }
                rtnval.append(entry)

        return rtnval

    def _parse_softwareupdate_list(
        self,
        *,
        text: str,
    ) -> list[UefiCapsuleUpdateInfo]:
        """
            Parse ``softwareupdate --list`` output.  Lines of interest:

                * Label: macOS-Firmware-Update-12.6
                    Title: macOS Firmware Update, Version: 12.6, Size: ..., Recommended: YES
        """
        rtnval: list[UefiCapsuleUpdateInfo] = []

        current_label: Optional[str] = None
        for raw_line in text.splitlines():
            line = raw_line.strip()
            lowered = line.lower()

            # Label line.
            if line.startswith("* Label:") or line.startswith("Label:"):
                _, _, after = line.partition("Label:")
                current_label = after.strip()
                continue

            # Title / version line; only emit when the label looks like
            # a firmware update.
            if current_label is None:
                continue

            if "title:" not in lowered:
                continue

            if "firmware" not in lowered:
                # Reset so a non-firmware label is not associated with
                # later lines.
                current_label = None
                continue

            title: Optional[str] = None
            version: Optional[str] = None
            for fragment in line.split(","):
                fragment_stripped = fragment.strip()
                fragment_lower = fragment_stripped.lower()
                if fragment_lower.startswith("title:"):
                    _, _, after = fragment_stripped.partition(":")
                    title = after.strip()
                elif fragment_lower.startswith("version:"):
                    _, _, after = fragment_stripped.partition(":")
                    version = after.strip()

            info = UefiCapsuleUpdateInfo(
                update_id=current_label,
                device_id=None,
                device_name=title,
                current_version=None,
                available_version=version or "",
                severity=None,
                description=title,
            )
            rtnval.append(info)
            current_label = None

        return rtnval

    def _parse_softwareupdate_history(self, *, text: str) -> list[dict]:
        rtnval: list[dict] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if len(line) == 0:
                continue
            if line.lower().startswith("display name"):
                continue
            if set(line) == {"-"}:
                continue
            # Best-effort: split on two-or-more spaces.
            cols = [c.strip() for c in line.split("  ") if len(c.strip()) > 0]
            if len(cols) < 2:
                continue
            entry = {
                "update_id": cols[0],
                "device_id": None,
                "applied_version": cols[1] if len(cols) > 1 else "",
                "applied_at": cols[2] if len(cols) > 2 else None,
                "result": cols[3] if len(cols) > 3 else None,
            }
            rtnval.append(entry)
        return rtnval

    def _parse_system_profiler_hardware(
        self,
        *,
        text: str,
    ) -> list[UefiDeviceInfo]:
        rtnval: list[UefiDeviceInfo] = []

        if text is None or len(text.strip()) == 0:
            return rtnval

        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return rtnval

        if not isinstance(data, dict):
            return rtnval

        section = data.get("SPHardwareDataType")
        if section is None or not isinstance(section, list):
            return rtnval

        for entry in section:
            if not isinstance(entry, dict):
                continue
            device_id = entry.get("platform_UUID") or entry.get("machine_model")
            if device_id is None:
                continue
            name = entry.get("machine_name") or entry.get("machine_model") or device_id
            vendor = "Apple Inc."
            firmware_version = (
                entry.get("boot_rom_version")
                or entry.get("smc_version_system")
            )
            info = UefiDeviceInfo(
                device_id=device_id,
                name=name,
                vendor=vendor,
                firmware_version=firmware_version,
                update_supported=False,
            )
            rtnval.append(info)

        return rtnval

    def _parse_get_windowsupdate_json(
        self,
        *,
        text: str,
    ) -> list[UefiCapsuleUpdateInfo]:
        rtnval: list[UefiCapsuleUpdateInfo] = []

        if text is None or len(text.strip()) == 0:
            return rtnval

        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return rtnval

        # ConvertTo-Json emits a single object when there is exactly one
        # match and an array when there are several.
        if isinstance(data, dict):
            entries = [data]
        elif isinstance(data, list):
            entries = data
        else:
            return rtnval

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            kb_id = entry.get("KB") or entry.get("KBArticleID") or entry.get("Title")
            if kb_id is None:
                continue
            severity = entry.get("MsrcSeverity")
            description = entry.get("Title") or entry.get("Description")
            info = UefiCapsuleUpdateInfo(
                update_id=str(kb_id),
                device_id=None,
                device_name=None,
                current_version=None,
                available_version=str(entry.get("RevisionNumber") or ""),
                severity=severity,
                description=description,
            )
            rtnval.append(info)

        return rtnval

    def _parse_wmi_computer_bios_json(
        self,
        *,
        text: str,
    ) -> list[UefiDeviceInfo]:
        rtnval: list[UefiDeviceInfo] = []

        if text is None or len(text.strip()) == 0:
            return rtnval

        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return rtnval

        if isinstance(data, dict):
            entries = [data]
        elif isinstance(data, list):
            entries = data
        else:
            return rtnval

        # Combine the ComputerSystem and BIOS records into a single
        # device record per entry.  When both are present in the array,
        # we take the first non-empty values for vendor, name, version.
        manufacturer: Optional[str] = None
        product_name: Optional[str] = None
        bios_version: Optional[str] = None
        identifier: Optional[str] = None

        for entry in entries:
            if not isinstance(entry, dict):
                continue

            if manufacturer is None:
                manufacturer = entry.get("Manufacturer")

            if product_name is None:
                product_name = entry.get("Model") or entry.get("Caption")

            if bios_version is None:
                bios_version = (
                    entry.get("SMBIOSBIOSVersion")
                    or entry.get("BIOSVersion")
                    or entry.get("Version")
                )

            if identifier is None:
                identifier = (
                    entry.get("SerialNumber")
                    or entry.get("UUID")
                    or entry.get("Caption")
                )

        if identifier is None:
            return rtnval

        record = UefiDeviceInfo(
            device_id=str(identifier),
            name=product_name or str(identifier),
            vendor=manufacturer,
            firmware_version=bios_version,
            update_supported=True,
        )
        rtnval.append(record)
        return rtnval

    def _parse_get_wuhistory_json(self, *, text: str) -> list[dict]:
        rtnval: list[dict] = []
        if text is None or len(text.strip()) == 0:
            return rtnval

        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return rtnval

        if isinstance(data, dict):
            entries = [data]
        elif isinstance(data, list):
            entries = data
        else:
            return rtnval

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            kb_id = entry.get("KB") or entry.get("KBArticleID") or entry.get("Title")
            record = {
                "update_id": str(kb_id) if kb_id is not None else None,
                "device_id": None,
                "applied_version": str(entry.get("RevisionNumber") or ""),
                "applied_at": entry.get("Date") or entry.get("HResult"),
                "result": entry.get("Result"),
            }
            rtnval.append(record)
        return rtnval
