"""
    UEFI ESP-management feature.

    Implements the cross-OS esp-management contract for the EFI System
    Partition on whichever host the manager runs on.  Dispatches to
    host-specific user-space tooling:

        * Linux   -> ``findmnt``, ``lsblk``, ``mount`` / ``umount``
                     (default mountpoint ``/boot/efi``)
        * Darwin  -> ``diskutil`` (mount / unmount / list / info; default
                     ESP device ``disk0s1``)
        * Windows -> PowerShell ``Get-Partition`` / ``Get-Volume`` for
                     discovery; ``mountvol`` for mount / unmount via a
                     drive-letter handle (default ``S:``)

    Per-host-OS dispatch:
        :class:`UefiEspManager` records the running host OS once at
        construction time (via :func:`detect_host_os`) and routes every
        verb through ``_dispatch_<linux|darwin|windows>_<verb>`` private
        methods.

    File-I/O via runner only:
        Unlike the contract's expectation of an ``IFileOperator``-
        extension surface for ESP file I/O, this implementation routes
        every file-I/O verb (``list_esp_contents``, ``read_esp_file``,
        ``write_esp_file``, ``delete_esp_file``) through the same
        :class:`ICommandRunner` used for mount management.  Linux / macOS
        use ``ls -la``, ``cat``, ``tee``, and ``rm`` against the mounted
        ESP path; Windows dispatches PowerShell ``Get-ChildItem``,
        ``Get-Content``, ``Set-Content``, and ``Remove-Item``.  This
        choice keeps the package free of the additional file-operator
        dependency and matches the established pattern across every
        ``codecraftai-sysman-*`` Layer-3 feature.

    Path normalization:
        Linux / macOS use forward-slash POSIX paths.  Windows accepts
        both forward-slash and backslash and normalises to backslash
        internally (PowerShell is comfortable with either).  The
        recorded ``command`` field in the result shows the path as-given
        by the caller after host-OS normalization.

    Mount idempotency:
        ``mount_esp`` short-circuits to ``SKIPPED`` when
        :meth:`is_esp_mounted` already reports the ESP as mounted at the
        requested mountpoint.  ``unmount_esp`` short-circuits when the
        ESP is not mounted at the named target.

    Elevation policy:
        Writes pass ``elevated=True`` when ``config.elevated`` is True;
        reads never elevate.
"""

import json
import os

from dataclasses import dataclass
from typing import Optional

from ccai.interfaces.icommandrunner import ICommandRunner, RunResult

from ccai.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
    HOST_OS_DARWIN,
    HOST_OS_LINUX,
    HOST_OS_WINDOWS,
)


_DEFAULT_FINDMNT = "findmnt"
_DEFAULT_LSBLK = "lsblk"
_DEFAULT_MOUNT = "mount"
_DEFAULT_UMOUNT = "umount"
_DEFAULT_LS = "ls"
_DEFAULT_CAT = "cat"
_DEFAULT_TEE = "tee"
_DEFAULT_RM = "rm"
_DEFAULT_DISKUTIL = "diskutil"
_DEFAULT_MOUNTVOL = "mountvol"
_DEFAULT_POWERSHELL = "powershell"
_DEFAULT_LINUX_MOUNTPOINT = "/boot/efi"
_DEFAULT_DARWIN_MOUNTPOINT = "/Volumes/EFI"
_DEFAULT_WINDOWS_DRIVE_LETTER = "S"
_DEFAULT_DARWIN_ESP_DEVICE = "disk0s1"


UefiEspActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------


@dataclass
class UefiEspManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UefiEspManager`.  Inherits ``dry_run``
        and ``elevated`` from :class:`FeatureConfig`.

        :param override_host_os: Test-time override.
        :param findmnt_path: Path / argv-name of ``findmnt``.
        :param lsblk_path: Path / argv-name of ``lsblk``.
        :param mount_path: Path / argv-name of ``mount``.
        :param umount_path: Path / argv-name of ``umount``.
        :param ls_path: Path / argv-name of ``ls``.
        :param cat_path: Path / argv-name of ``cat``.
        :param tee_path: Path / argv-name of ``tee``.
        :param rm_path: Path / argv-name of ``rm``.
        :param diskutil_path: Path / argv-name of ``diskutil``.
        :param mountvol_path: Path / argv-name of ``mountvol``.
        :param powershell_path: Path / argv-name of ``powershell``.
        :param default_mountpoint_linux: Default mountpoint for the ESP
                                         on Linux.
        :param default_mountpoint_macos: Default mountpoint for the ESP
                                         on macOS.
        :param default_drive_letter_windows: Default drive-letter
                                             prefix for the ESP on
                                             Windows (without trailing
                                             colon).
        :param darwin_esp_device: Default ESP device handle on macOS.
    """
    override_host_os: Optional[str] = None
    findmnt_path: str = _DEFAULT_FINDMNT
    lsblk_path: str = _DEFAULT_LSBLK
    mount_path: str = _DEFAULT_MOUNT
    umount_path: str = _DEFAULT_UMOUNT
    ls_path: str = _DEFAULT_LS
    cat_path: str = _DEFAULT_CAT
    tee_path: str = _DEFAULT_TEE
    rm_path: str = _DEFAULT_RM
    diskutil_path: str = _DEFAULT_DISKUTIL
    mountvol_path: str = _DEFAULT_MOUNTVOL
    powershell_path: str = _DEFAULT_POWERSHELL
    default_mountpoint_linux: str = _DEFAULT_LINUX_MOUNTPOINT
    default_mountpoint_macos: str = _DEFAULT_DARWIN_MOUNTPOINT
    default_drive_letter_windows: str = _DEFAULT_WINDOWS_DRIVE_LETTER
    darwin_esp_device: str = _DEFAULT_DARWIN_ESP_DEVICE


@dataclass
class UefiEspInfo:
    """
        Snapshot of the ESP discovery state.

        :param device: Device identifier (e.g., ``/dev/sda1``,
                       ``disk0s1``, partition GUID).  ``None`` when not
                       detected.
        :param mountpoint: Current mountpoint when mounted; ``None``
                           when not mounted.
        :param mounted: Whether the ESP is currently mounted.
        :param filesystem: Filesystem type (``"vfat"``, ``"msdos"``,
                           ``"FAT32"``, ...).  ``None`` when not
                           detected.
        :param size_bytes: Partition size in bytes when known; ``None``
                           otherwise.
        :param host_os: The host OS the snapshot was taken from.
    """
    device: Optional[str]
    mountpoint: Optional[str]
    mounted: bool
    filesystem: Optional[str]
    size_bytes: Optional[int]
    host_os: str


@dataclass
class UefiEspFileInfo:
    """
        One ESP-resident file or directory entry.

        :param name: Final path component.
        :param path: Full path on the mounted ESP.
        :param size_bytes: File size in bytes; ``None`` for directories
                           or unknown.
        :param is_directory: Whether the entry is a directory.
        :param modified: Last-modified timestamp; ``None`` when not
                         surfaced by the host tool.
    """
    name: str
    path: str
    size_bytes: Optional[int] = None
    is_directory: bool = False
    modified: Optional[str] = None


@dataclass
class UefiEspFileContent:
    """
        Outcome of a successful :meth:`UefiEspManager.read_esp_file`.

        :param path: ESP-relative or full path that was read.
        :param content: Raw file bytes.
        :param is_text: Whether the bytes appear to be a text file
                        (heuristic: no NUL byte in the first 4 KiB).
        :param encoding: Text encoding when ``is_text=True``; ``None``
                         when binary.
    """
    path: str
    content: bytes
    is_text: bool
    encoding: Optional[str] = None


@dataclass
class UefiEspActionResult(FeatureActionResult):
    """
        Outcome of an ESP-management write.

        :param path: The ESP-relative path the action targeted, when
                     applicable.
        :param mountpoint: The mountpoint the action targeted, when
                           applicable.
        :param host_os: The host OS the action dispatched to.
    """
    path: Optional[str] = None
    mountpoint: Optional[str] = None
    host_os: Optional[str] = None


class UefiEspQueryError(Exception):
    """
        Raised when an ESP discovery or read verb fails.
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

        err_msg_lines = ["UEFI ESP-management query failed."]
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


class UefiEspManager(BaseFeatureManager):
    """
        Per-host-OS UEFI ESP manager.  Each verb routes through a
        ``_dispatch_<host>_<verb>`` private helper selected by
        ``self._host_os``.  All commands flow through the injected
        :class:`ICommandRunner`.
    """

    result_class = UefiEspActionResult
    status_class = UefiEspActionStatus
    feature_name = "esp-management"

    def __init__(
        self,
        runner: ICommandRunner,
        config: Optional[UefiEspManagerConfig] = None,
    ) -> None:
        """
            :param runner: The command runner.
            :param config: Manager configuration.  Defaults to
                           :class:`UefiEspManagerConfig` with no
                           overrides.
        """
        if config is None:
            config = UefiEspManagerConfig()
        super().__init__(runner=runner, config=config)
        self._typed_config: UefiEspManagerConfig = config
        return

    @property
    def typed_config(self) -> UefiEspManagerConfig:
        """
            The active :class:`UefiEspManagerConfig`.
        """
        return self._typed_config

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_esp_info(self) -> UefiEspInfo:
        """
            Discover the host's EFI System Partition.

            :returns: A :class:`UefiEspInfo`.  When no ESP is detected,
                      returns a record with ``device=None``,
                      ``mounted=False``.
            :raises UefiEspQueryError: When the discovery tool fails for
                                       reasons other than "no ESP".
        """
        if self._host_os == HOST_OS_LINUX:
            rtnval = self._dispatch_linux_get_esp_info()
        elif self._host_os == HOST_OS_DARWIN:
            rtnval = self._dispatch_darwin_get_esp_info()
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = self._dispatch_windows_get_esp_info()
        else:
            err_msg = "Unsupported host OS for get_esp_info: {}".format(
                self._host_os,
            )
            raise UefiEspQueryError(message=err_msg)
        return rtnval

    def is_esp_mounted(self) -> bool:
        """
            Read the host's ESP mount state.

            :returns: ``True`` when mounted, ``False`` otherwise.
            :raises UefiEspQueryError: When the underlying tool fails.
        """
        if self._host_os == HOST_OS_LINUX:
            rtnval = self._dispatch_linux_is_esp_mounted()
        elif self._host_os == HOST_OS_DARWIN:
            rtnval = self._dispatch_darwin_is_esp_mounted()
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = self._dispatch_windows_is_esp_mounted()
        else:
            err_msg = "Unsupported host OS for is_esp_mounted: {}".format(
                self._host_os,
            )
            raise UefiEspQueryError(message=err_msg)
        return rtnval

    def list_esp_contents(self, path: str) -> list[UefiEspFileInfo]:
        """
            Enumerate files at ``<mountpoint>/<path>`` on the mounted
            ESP.

            :param path: ESP-relative path; ``"/"`` lists the mount
                         root.

            :returns: A list of :class:`UefiEspFileInfo`.
            :raises UefiEspQueryError: When the ESP is not mounted, the
                                       host OS is unsupported, or the
                                       underlying tool fails.
        """
        mountpoint = self._require_mounted_path(verb="list_esp_contents")

        if self._host_os == HOST_OS_LINUX:
            rtnval = self._dispatch_linux_list_esp_contents(
                mountpoint=mountpoint,
                path=path,
            )
        elif self._host_os == HOST_OS_DARWIN:
            rtnval = self._dispatch_darwin_list_esp_contents(
                mountpoint=mountpoint,
                path=path,
            )
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = self._dispatch_windows_list_esp_contents(
                mountpoint=mountpoint,
                path=path,
            )
        else:
            err_msg = "Unsupported host OS for list_esp_contents: {}".format(
                self._host_os,
            )
            raise UefiEspQueryError(message=err_msg)
        return rtnval

    def read_esp_file(self, path: str) -> UefiEspFileContent:
        """
            Read a file at ``<mountpoint>/<path>`` on the mounted ESP.

            :param path: ESP-relative path.

            :returns: A :class:`UefiEspFileContent`.
            :raises UefiEspQueryError: When the ESP is not mounted, the
                                       host OS is unsupported, or the
                                       underlying tool fails.
        """
        mountpoint = self._require_mounted_path(verb="read_esp_file")

        if self._host_os == HOST_OS_LINUX:
            rtnval = self._dispatch_linux_read_esp_file(
                mountpoint=mountpoint,
                path=path,
            )
        elif self._host_os == HOST_OS_DARWIN:
            rtnval = self._dispatch_darwin_read_esp_file(
                mountpoint=mountpoint,
                path=path,
            )
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = self._dispatch_windows_read_esp_file(
                mountpoint=mountpoint,
                path=path,
            )
        else:
            err_msg = "Unsupported host OS for read_esp_file: {}".format(
                self._host_os,
            )
            raise UefiEspQueryError(message=err_msg)
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def mount_esp(
        self,
        mountpoint: Optional[str] = None,
    ) -> UefiEspActionResult:
        """
            Mount the ESP at ``mountpoint``.

            Idempotency: when :meth:`is_esp_mounted` already reports
            ``True`` and the discovered mountpoint matches the requested
            one, returns ``SKIPPED`` without invoking the runner.

            :param mountpoint: Target mountpoint.  When ``None``, falls
                               back to the host-OS default
                               (``config.default_mountpoint_linux`` /
                               ``config.default_mountpoint_macos``,
                               or a drive-letter form on Windows).

            :returns: The :class:`UefiEspActionResult`.
        """
        target = self._resolve_mountpoint(mountpoint=mountpoint)

        if self._config.dry_run:
            command = self._build_dry_run_mount_command(target=target)
            rtnval = self._dry_run_result(
                command=command,
                detail=target,
                path=None,
                mountpoint=target,
            )
            return rtnval

        # Idempotency: short-circuit if already mounted at the target.
        already_mounted = self._safe_check_mount_match(target=target)
        if already_mounted is True:
            skipped = self._wrap_result(
                status=self.status_class.SKIPPED,
                detail="ESP already mounted at requested mountpoint",
                command=None,
                path=None,
                mountpoint=target,
                host_os=self._host_os,
            )
            return skipped

        if self._host_os == HOST_OS_LINUX:
            rtnval = self._dispatch_linux_mount_esp(target=target)
        elif self._host_os == HOST_OS_DARWIN:
            rtnval = self._dispatch_darwin_mount_esp(target=target)
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = self._dispatch_windows_mount_esp(target=target)
        else:
            rtnval = self._build_failed_unsupported(
                verb="mount_esp",
                host_os=self._host_os,
                path=None,
                mountpoint=target,
                host_os_field=self._host_os,
            )
        return rtnval

    def unmount_esp(
        self,
        mountpoint: Optional[str] = None,
    ) -> UefiEspActionResult:
        """
            Unmount the ESP at ``mountpoint``.

            Idempotency: when :meth:`is_esp_mounted` already reports
            ``False``, returns ``SKIPPED`` without invoking the runner.

            :param mountpoint: Target mountpoint.  When ``None``, falls
                               back to the host-OS default.

            :returns: The :class:`UefiEspActionResult`.
        """
        target = self._resolve_mountpoint(mountpoint=mountpoint)

        if self._config.dry_run:
            command = self._build_dry_run_unmount_command(target=target)
            rtnval = self._dry_run_result(
                command=command,
                detail=target,
                path=None,
                mountpoint=target,
            )
            return rtnval

        currently_mounted = self._safe_is_esp_mounted()
        if currently_mounted is False:
            skipped = self._wrap_result(
                status=self.status_class.SKIPPED,
                detail="ESP is not mounted",
                command=None,
                path=None,
                mountpoint=target,
                host_os=self._host_os,
            )
            return skipped

        if self._host_os == HOST_OS_LINUX:
            rtnval = self._dispatch_linux_unmount_esp(target=target)
        elif self._host_os == HOST_OS_DARWIN:
            rtnval = self._dispatch_darwin_unmount_esp(target=target)
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = self._dispatch_windows_unmount_esp(target=target)
        else:
            rtnval = self._build_failed_unsupported(
                verb="unmount_esp",
                host_os=self._host_os,
                path=None,
                mountpoint=target,
                host_os_field=self._host_os,
            )
        return rtnval

    def write_esp_file(
        self,
        path: str,
        content: bytes,
    ) -> UefiEspActionResult:
        """
            Write ``content`` to ``<mountpoint>/<path>``.

            Mount precondition: when the ESP is not mounted, returns
            ``FAILED`` with a stable detail and invokes no command.

            :param path: ESP-relative target path.
            :param content: Raw bytes to write.

            :returns: The :class:`UefiEspActionResult`.
        """
        if self._config.dry_run:
            assumed_mountpoint = self._resolve_mountpoint(mountpoint=None)
            joined = self._join_esp_path(
                mountpoint=assumed_mountpoint,
                relative=path,
            )
            command = self._build_dry_run_write_command(
                full_path=joined,
                content=content,
            )
            rtnval = self._dry_run_result(
                command=command,
                detail=path,
                path=path,
                mountpoint=assumed_mountpoint,
            )
            return rtnval

        mountpoint = self._safe_resolve_active_mountpoint()

        if mountpoint is None:
            failed = self._wrap_result(
                status=self.status_class.FAILED,
                detail="ESP is not mounted",
                command=None,
                stderr="ESP is not mounted",
                path=path,
                mountpoint=None,
                host_os=self._host_os,
            )
            return failed

        full_path = self._join_esp_path(
            mountpoint=mountpoint,
            relative=path,
        )

        # Byte-match idempotency: a successful read whose bytes equal
        # the desired content is a SKIPPED.
        existing = self._safe_read_existing_bytes(full_path=full_path)
        if existing is not None and existing == content:
            skipped = self._wrap_result(
                status=self.status_class.SKIPPED,
                detail="existing file is byte-identical",
                command=None,
                path=path,
                mountpoint=mountpoint,
                host_os=self._host_os,
            )
            return skipped

        if self._host_os == HOST_OS_LINUX:
            rtnval = self._dispatch_linux_write_esp_file(
                full_path=full_path,
                content=content,
                relative_path=path,
                mountpoint=mountpoint,
            )
        elif self._host_os == HOST_OS_DARWIN:
            rtnval = self._dispatch_darwin_write_esp_file(
                full_path=full_path,
                content=content,
                relative_path=path,
                mountpoint=mountpoint,
            )
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = self._dispatch_windows_write_esp_file(
                full_path=full_path,
                content=content,
                relative_path=path,
                mountpoint=mountpoint,
            )
        else:
            rtnval = self._build_failed_unsupported(
                verb="write_esp_file",
                host_os=self._host_os,
                path=path,
                mountpoint=mountpoint,
                host_os_field=self._host_os,
            )
        return rtnval

    def delete_esp_file(self, path: str) -> UefiEspActionResult:
        """
            Delete ``<mountpoint>/<path>`` on the mounted ESP.

            Mount precondition: when the ESP is not mounted, returns
            ``FAILED`` with a stable detail and invokes no command.

            :param path: ESP-relative path.

            :returns: The :class:`UefiEspActionResult`.
        """
        if self._config.dry_run:
            assumed_mountpoint = self._resolve_mountpoint(mountpoint=None)
            joined = self._join_esp_path(
                mountpoint=assumed_mountpoint,
                relative=path,
            )
            command = self._build_dry_run_delete_command(full_path=joined)
            rtnval = self._dry_run_result(
                command=command,
                detail=path,
                path=path,
                mountpoint=assumed_mountpoint,
            )
            return rtnval

        mountpoint = self._safe_resolve_active_mountpoint()

        if mountpoint is None:
            failed = self._wrap_result(
                status=self.status_class.FAILED,
                detail="ESP is not mounted",
                command=None,
                stderr="ESP is not mounted",
                path=path,
                mountpoint=None,
                host_os=self._host_os,
            )
            return failed

        full_path = self._join_esp_path(
            mountpoint=mountpoint,
            relative=path,
        )

        if self._host_os == HOST_OS_LINUX:
            rtnval = self._dispatch_linux_delete_esp_file(
                full_path=full_path,
                relative_path=path,
                mountpoint=mountpoint,
            )
        elif self._host_os == HOST_OS_DARWIN:
            rtnval = self._dispatch_darwin_delete_esp_file(
                full_path=full_path,
                relative_path=path,
                mountpoint=mountpoint,
            )
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = self._dispatch_windows_delete_esp_file(
                full_path=full_path,
                relative_path=path,
                mountpoint=mountpoint,
            )
        else:
            rtnval = self._build_failed_unsupported(
                verb="delete_esp_file",
                host_os=self._host_os,
                path=path,
                mountpoint=mountpoint,
                host_os_field=self._host_os,
            )
        return rtnval

    # ------------------------------------------------------------------
    # Linux dispatch helpers
    # ------------------------------------------------------------------

    def _dispatch_linux_get_esp_info(self) -> UefiEspInfo:
        target = self._typed_config.default_mountpoint_linux

        findmnt_command = [
            self._typed_config.findmnt_path,
            target,
            "--json",
        ]
        findmnt_result = self._runner.run(
            list(findmnt_command), elevated=False,
        )
        device: Optional[str] = None
        mountpoint: Optional[str] = None
        filesystem: Optional[str] = None

        if findmnt_result.exit_code == 0:
            parsed = self._parse_findmnt_json(text=findmnt_result.stdout or "")
            if parsed is not None:
                device = parsed.get("source")
                mountpoint = parsed.get("target")
                filesystem = parsed.get("fstype")

        # Best-effort lsblk for size info.
        size_bytes: Optional[int] = None
        if device is not None:
            lsblk_command = [
                self._typed_config.lsblk_path,
                "-f",
                "--json",
            ]
            lsblk_result = self._runner.run(
                list(lsblk_command), elevated=False,
            )
            if lsblk_result.exit_code == 0:
                size_bytes = self._parse_lsblk_size(
                    text=lsblk_result.stdout or "",
                    device_path=device,
                )

        rtnval = UefiEspInfo(
            device=device,
            mountpoint=mountpoint,
            mounted=mountpoint is not None,
            filesystem=filesystem,
            size_bytes=size_bytes,
            host_os=self._host_os,
        )
        return rtnval

    def _dispatch_linux_is_esp_mounted(self) -> bool:
        target = self._typed_config.default_mountpoint_linux
        command = [self._typed_config.findmnt_path, target]
        run_result = self._runner.run(list(command), elevated=False)
        rtnval = run_result.exit_code == 0
        return rtnval

    def _dispatch_linux_list_esp_contents(
        self,
        *,
        mountpoint: str,
        path: str,
    ) -> list[UefiEspFileInfo]:
        full_path = self._join_esp_path(mountpoint=mountpoint, relative=path)
        command = [self._typed_config.ls_path, "-la", full_path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UefiEspQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = self._parse_ls_la(
            text=run_result.stdout or "",
            base_path=full_path,
        )
        return rtnval

    def _dispatch_linux_read_esp_file(
        self,
        *,
        mountpoint: str,
        path: str,
    ) -> UefiEspFileContent:
        full_path = self._join_esp_path(mountpoint=mountpoint, relative=path)
        command = [self._typed_config.cat_path, full_path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UefiEspQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        raw = run_result.stdout or ""
        rtnval = self._wrap_read_content(path=full_path, raw_text=raw)
        return rtnval

    def _dispatch_linux_mount_esp(
        self,
        *,
        target: str,
    ) -> UefiEspActionResult:
        info = self._safe_get_esp_info()
        if info is not None and info.device is not None:
            command = [
                self._typed_config.mount_path,
                "-t", "vfat",
                info.device,
                target,
            ]
        else:
            # Fallback: rely on /etc/fstab.
            command = [self._typed_config.mount_path, target]

        rtnval = self._dispatch(
            command=command,
            detail=target,
            operation="mount_esp",
            path=None,
            mountpoint=target,
            host_os=self._host_os,
        )
        return rtnval

    def _dispatch_linux_unmount_esp(
        self,
        *,
        target: str,
    ) -> UefiEspActionResult:
        command = [self._typed_config.umount_path, target]
        rtnval = self._dispatch(
            command=command,
            detail=target,
            operation="unmount_esp",
            path=None,
            mountpoint=target,
            host_os=self._host_os,
        )
        return rtnval

    def _dispatch_linux_write_esp_file(
        self,
        *,
        full_path: str,
        content: bytes,
        relative_path: str,
        mountpoint: str,
    ) -> UefiEspActionResult:
        command = [self._typed_config.tee_path, full_path]
        decoded = self._payload_to_text(content=content)
        rtnval = self._dispatch(
            command=command,
            detail=relative_path,
            stdin=decoded,
            operation="write_esp_file",
            path=relative_path,
            mountpoint=mountpoint,
            host_os=self._host_os,
        )
        return rtnval

    def _dispatch_linux_delete_esp_file(
        self,
        *,
        full_path: str,
        relative_path: str,
        mountpoint: str,
    ) -> UefiEspActionResult:
        # Idempotency: probe existence via ls before issuing rm.
        ls_command = [self._typed_config.ls_path, full_path]
        ls_result = self._runner.run(list(ls_command), elevated=False)
        if ls_result.exit_code != 0:
            skipped = self._wrap_result(
                status=self.status_class.SKIPPED,
                detail="path does not exist",
                command=None,
                path=relative_path,
                mountpoint=mountpoint,
                host_os=self._host_os,
            )
            return skipped

        command = [self._typed_config.rm_path, "-f", full_path]
        rtnval = self._dispatch(
            command=command,
            detail=relative_path,
            operation="delete_esp_file",
            path=relative_path,
            mountpoint=mountpoint,
            host_os=self._host_os,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Darwin dispatch helpers
    # ------------------------------------------------------------------

    def _dispatch_darwin_get_esp_info(self) -> UefiEspInfo:
        list_command = [self._typed_config.diskutil_path, "list", "-plist"]
        list_result = self._runner.run(list(list_command), elevated=False)
        if list_result.exit_code != 0:
            raise UefiEspQueryError(
                command=list_command,
                exit_code=list_result.exit_code,
                stderr=list_result.stderr,
            )

        device = self._parse_diskutil_list_for_esp(text=list_result.stdout or "")

        mountpoint: Optional[str] = None
        filesystem: Optional[str] = None
        size_bytes: Optional[int] = None
        if device is not None:
            info_command = [self._typed_config.diskutil_path, "info", device]
            info_result = self._runner.run(
                list(info_command), elevated=False,
            )
            if info_result.exit_code == 0:
                parsed = self._parse_diskutil_info(
                    text=info_result.stdout or "",
                )
                mountpoint = parsed.get("mountpoint")
                filesystem = parsed.get("filesystem")
                raw_size = parsed.get("size_bytes")
                if isinstance(raw_size, int):
                    size_bytes = raw_size

        rtnval = UefiEspInfo(
            device=device,
            mountpoint=mountpoint,
            mounted=mountpoint is not None,
            filesystem=filesystem,
            size_bytes=size_bytes,
            host_os=self._host_os,
        )
        return rtnval

    def _dispatch_darwin_is_esp_mounted(self) -> bool:
        device = self._typed_config.darwin_esp_device
        command = [self._typed_config.diskutil_path, "info", device]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval
        parsed = self._parse_diskutil_info(text=run_result.stdout or "")
        is_mounted = parsed.get("mounted")
        if isinstance(is_mounted, bool):
            rtnval = is_mounted
        else:
            rtnval = parsed.get("mountpoint") is not None
        return rtnval

    def _dispatch_darwin_list_esp_contents(
        self,
        *,
        mountpoint: str,
        path: str,
    ) -> list[UefiEspFileInfo]:
        # macOS ``ls -la`` shape matches Linux's well enough for the
        # parser.
        full_path = self._join_esp_path(mountpoint=mountpoint, relative=path)
        command = [self._typed_config.ls_path, "-la", full_path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UefiEspQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = self._parse_ls_la(
            text=run_result.stdout or "",
            base_path=full_path,
        )
        return rtnval

    def _dispatch_darwin_read_esp_file(
        self,
        *,
        mountpoint: str,
        path: str,
    ) -> UefiEspFileContent:
        full_path = self._join_esp_path(mountpoint=mountpoint, relative=path)
        command = [self._typed_config.cat_path, full_path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UefiEspQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        raw = run_result.stdout or ""
        rtnval = self._wrap_read_content(path=full_path, raw_text=raw)
        return rtnval

    def _dispatch_darwin_mount_esp(
        self,
        *,
        target: str,
    ) -> UefiEspActionResult:
        device = self._typed_config.darwin_esp_device
        command = [self._typed_config.diskutil_path, "mount", device]
        rtnval = self._dispatch(
            command=command,
            detail=target,
            operation="mount_esp",
            path=None,
            mountpoint=target,
            host_os=self._host_os,
        )
        return rtnval

    def _dispatch_darwin_unmount_esp(
        self,
        *,
        target: str,
    ) -> UefiEspActionResult:
        command = [self._typed_config.diskutil_path, "unmount", target]
        rtnval = self._dispatch(
            command=command,
            detail=target,
            operation="unmount_esp",
            path=None,
            mountpoint=target,
            host_os=self._host_os,
        )
        return rtnval

    def _dispatch_darwin_write_esp_file(
        self,
        *,
        full_path: str,
        content: bytes,
        relative_path: str,
        mountpoint: str,
    ) -> UefiEspActionResult:
        command = [self._typed_config.tee_path, full_path]
        decoded = self._payload_to_text(content=content)
        rtnval = self._dispatch(
            command=command,
            detail=relative_path,
            stdin=decoded,
            operation="write_esp_file",
            path=relative_path,
            mountpoint=mountpoint,
            host_os=self._host_os,
        )
        return rtnval

    def _dispatch_darwin_delete_esp_file(
        self,
        *,
        full_path: str,
        relative_path: str,
        mountpoint: str,
    ) -> UefiEspActionResult:
        ls_command = [self._typed_config.ls_path, full_path]
        ls_result = self._runner.run(list(ls_command), elevated=False)
        if ls_result.exit_code != 0:
            skipped = self._wrap_result(
                status=self.status_class.SKIPPED,
                detail="path does not exist",
                command=None,
                path=relative_path,
                mountpoint=mountpoint,
                host_os=self._host_os,
            )
            return skipped

        command = [self._typed_config.rm_path, "-f", full_path]
        rtnval = self._dispatch(
            command=command,
            detail=relative_path,
            operation="delete_esp_file",
            path=relative_path,
            mountpoint=mountpoint,
            host_os=self._host_os,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Windows dispatch helpers
    # ------------------------------------------------------------------

    def _dispatch_windows_get_esp_info(self) -> UefiEspInfo:
        partition_command = [
            self._typed_config.powershell_path,
            "-NoProfile",
            "-Command",
            (
                "Get-Partition "
                "| Where-Object { $_.Type -eq 'System' } "
                "| ConvertTo-Json"
            ),
        ]
        partition_result = self._runner.run(
            list(partition_command), elevated=False,
        )
        if partition_result.exit_code != 0:
            raise UefiEspQueryError(
                command=partition_command,
                exit_code=partition_result.exit_code,
                stderr=partition_result.stderr,
            )

        device, size_bytes = self._parse_get_partition_json(
            text=partition_result.stdout or "",
        )

        # Mount-state probe via Get-Volume.
        volume_command = [
            self._typed_config.powershell_path,
            "-NoProfile",
            "-Command",
            "Get-Volume | ConvertTo-Json",
        ]
        volume_result = self._runner.run(
            list(volume_command), elevated=False,
        )
        mountpoint: Optional[str] = None
        filesystem: Optional[str] = None
        if volume_result.exit_code == 0:
            parsed = self._parse_get_volume_for_drive_letter(
                text=volume_result.stdout or "",
            )
            if parsed is not None:
                drive_letter, vol_fs = parsed
                if drive_letter is not None:
                    mountpoint = "{}:".format(drive_letter)
                filesystem = vol_fs

        rtnval = UefiEspInfo(
            device=device,
            mountpoint=mountpoint,
            mounted=mountpoint is not None,
            filesystem=filesystem,
            size_bytes=size_bytes,
            host_os=self._host_os,
        )
        return rtnval

    def _dispatch_windows_is_esp_mounted(self) -> bool:
        info = self._safe_get_esp_info()
        if info is None:
            rtnval = False
            return rtnval
        rtnval = info.mountpoint is not None
        return rtnval

    def _dispatch_windows_list_esp_contents(
        self,
        *,
        mountpoint: str,
        path: str,
    ) -> list[UefiEspFileInfo]:
        full_path = self._join_esp_path(mountpoint=mountpoint, relative=path)
        command = [
            self._typed_config.powershell_path,
            "-NoProfile",
            "-Command",
            "Get-ChildItem -LiteralPath '{}' | ConvertTo-Json".format(full_path),
        ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UefiEspQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = self._parse_get_childitem_json(
            text=run_result.stdout or "",
            base_path=full_path,
        )
        return rtnval

    def _dispatch_windows_read_esp_file(
        self,
        *,
        mountpoint: str,
        path: str,
    ) -> UefiEspFileContent:
        full_path = self._join_esp_path(mountpoint=mountpoint, relative=path)
        command = [
            self._typed_config.powershell_path,
            "-NoProfile",
            "-Command",
            "Get-Content -LiteralPath '{}' -Raw".format(full_path),
        ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UefiEspQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        raw = run_result.stdout or ""
        rtnval = self._wrap_read_content(path=full_path, raw_text=raw)
        return rtnval

    def _dispatch_windows_mount_esp(
        self,
        *,
        target: str,
    ) -> UefiEspActionResult:
        letter = self._extract_drive_letter(mountpoint=target)
        command = [
            self._typed_config.mountvol_path,
            "{}:".format(letter),
            "/S",
        ]
        rtnval = self._dispatch(
            command=command,
            detail=target,
            operation="mount_esp",
            path=None,
            mountpoint=target,
            host_os=self._host_os,
        )
        return rtnval

    def _dispatch_windows_unmount_esp(
        self,
        *,
        target: str,
    ) -> UefiEspActionResult:
        letter = self._extract_drive_letter(mountpoint=target)
        command = [
            self._typed_config.mountvol_path,
            "{}:".format(letter),
            "/D",
        ]
        rtnval = self._dispatch(
            command=command,
            detail=target,
            operation="unmount_esp",
            path=None,
            mountpoint=target,
            host_os=self._host_os,
        )
        return rtnval

    def _dispatch_windows_write_esp_file(
        self,
        *,
        full_path: str,
        content: bytes,
        relative_path: str,
        mountpoint: str,
    ) -> UefiEspActionResult:
        decoded = self._payload_to_text(content=content)
        # PowerShell ``Set-Content`` accepts the payload via stdin when
        # the surrounding runner does not allow escaping the content
        # safely on the command line.  We forward ``decoded`` as stdin.
        command = [
            self._typed_config.powershell_path,
            "-NoProfile",
            "-Command",
            (
                "$payload = [Console]::In.ReadToEnd(); "
                "Set-Content -LiteralPath '{}' -Value $payload -NoNewline"
            ).format(full_path),
        ]
        rtnval = self._dispatch(
            command=command,
            detail=relative_path,
            stdin=decoded,
            operation="write_esp_file",
            path=relative_path,
            mountpoint=mountpoint,
            host_os=self._host_os,
        )
        return rtnval

    def _dispatch_windows_delete_esp_file(
        self,
        *,
        full_path: str,
        relative_path: str,
        mountpoint: str,
    ) -> UefiEspActionResult:
        # Idempotency: probe via Test-Path.
        probe_command = [
            self._typed_config.powershell_path,
            "-NoProfile",
            "-Command",
            "if (Test-Path -LiteralPath '{}') {{ exit 0 }} else {{ exit 1 }}".format(
                full_path,
            ),
        ]
        probe_result = self._runner.run(list(probe_command), elevated=False)
        if probe_result.exit_code != 0:
            skipped = self._wrap_result(
                status=self.status_class.SKIPPED,
                detail="path does not exist",
                command=None,
                path=relative_path,
                mountpoint=mountpoint,
                host_os=self._host_os,
            )
            return skipped

        command = [
            self._typed_config.powershell_path,
            "-NoProfile",
            "-Command",
            "Remove-Item -LiteralPath '{}' -Force".format(full_path),
        ]
        rtnval = self._dispatch(
            command=command,
            detail=relative_path,
            operation="delete_esp_file",
            path=relative_path,
            mountpoint=mountpoint,
            host_os=self._host_os,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_mountpoint(
        self,
        *,
        mountpoint: Optional[str],
    ) -> str:
        if mountpoint is not None:
            rtnval = mountpoint
            return rtnval

        if self._host_os == HOST_OS_LINUX:
            rtnval = self._typed_config.default_mountpoint_linux
        elif self._host_os == HOST_OS_DARWIN:
            rtnval = self._typed_config.default_mountpoint_macos
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = "{}:".format(
                self._typed_config.default_drive_letter_windows,
            )
        else:
            rtnval = ""
        return rtnval

    def _extract_drive_letter(self, *, mountpoint: str) -> str:
        # Accept "S", "S:", "S:\\", or "S:/" forms.
        cleaned = mountpoint.strip()
        if len(cleaned) == 0:
            rtnval = self._typed_config.default_drive_letter_windows
            return rtnval

        # Take the first character; PowerShell drive letters are single
        # characters.
        first = cleaned[0]
        rtnval = first.upper()
        return rtnval

    def _safe_check_mount_match(
        self,
        *,
        target: str,
    ) -> Optional[bool]:
        """
            Probe variant returning ``True`` when the ESP is currently
            mounted at the requested target, ``False`` when mounted
            elsewhere or not mounted, and ``None`` when the probe itself
            fails.
        """
        try:
            info = self.get_esp_info()
        except UefiEspQueryError:
            rtnval: Optional[bool] = None
            return rtnval

        if not info.mounted:
            rtnval = False
            return rtnval

        if info.mountpoint is None:
            rtnval = False
            return rtnval

        if self._host_os == HOST_OS_WINDOWS:
            target_letter = self._extract_drive_letter(mountpoint=target)
            current_letter = self._extract_drive_letter(
                mountpoint=info.mountpoint,
            )
            rtnval = target_letter == current_letter
        else:
            rtnval = info.mountpoint.rstrip("/") == target.rstrip("/")
        return rtnval

    def _safe_is_esp_mounted(self) -> Optional[bool]:
        try:
            rtnval = self.is_esp_mounted()
        except UefiEspQueryError:
            rtnval = None
        return rtnval

    def _safe_get_esp_info(self) -> Optional[UefiEspInfo]:
        try:
            rtnval = self.get_esp_info()
        except UefiEspQueryError:
            rtnval = None
        return rtnval

    def _safe_resolve_active_mountpoint(self) -> Optional[str]:
        info = self._safe_get_esp_info()
        if info is None:
            rtnval: Optional[str] = None
            return rtnval
        if not info.mounted:
            rtnval = None
            return rtnval
        rtnval = info.mountpoint
        return rtnval

    def _safe_read_existing_bytes(
        self,
        *,
        full_path: str,
    ) -> Optional[bytes]:
        # Probe via host-OS read.  Returns ``None`` on any error so the
        # caller proceeds with the write.
        if self._host_os in (HOST_OS_LINUX, HOST_OS_DARWIN):
            command = [self._typed_config.cat_path, full_path]
        elif self._host_os == HOST_OS_WINDOWS:
            command = [
                self._typed_config.powershell_path,
                "-NoProfile",
                "-Command",
                "Get-Content -LiteralPath '{}' -Raw".format(full_path),
            ]
        else:
            rtnval: Optional[bytes] = None
            return rtnval

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = None
            return rtnval

        raw = run_result.stdout or ""
        rtnval = raw.encode("utf-8", errors="surrogateescape")
        return rtnval

    def _require_mounted_path(self, *, verb: str) -> str:
        info = self._safe_get_esp_info()
        if info is None:
            err_msg = "{}: ESP discovery failed".format(verb)
            raise UefiEspQueryError(message=err_msg)
        if not info.mounted or info.mountpoint is None:
            err_msg = "{}: ESP is not mounted".format(verb)
            raise UefiEspQueryError(message=err_msg)
        rtnval = info.mountpoint
        return rtnval

    def _join_esp_path(self, *, mountpoint: str, relative: str) -> str:
        normalised = self._normalise_relative_path(relative=relative)
        if self._host_os == HOST_OS_WINDOWS:
            mp_clean = mountpoint.rstrip("\\/")
            if normalised.startswith("\\"):
                rtnval = mp_clean + normalised
            else:
                rtnval = mp_clean + "\\" + normalised
        else:
            mp_clean = mountpoint.rstrip("/")
            if normalised.startswith("/"):
                rtnval = mp_clean + normalised
            else:
                rtnval = mp_clean + "/" + normalised
        return rtnval

    def _normalise_relative_path(self, *, relative: str) -> str:
        if self._host_os == HOST_OS_WINDOWS:
            rtnval = relative.replace("/", "\\")
        else:
            rtnval = relative.replace("\\", "/")
        return rtnval

    def _payload_to_text(self, *, content: bytes) -> str:
        try:
            rtnval = content.decode("utf-8")
        except UnicodeDecodeError:
            rtnval = content.decode("utf-8", errors="surrogateescape")
        return rtnval

    def _wrap_read_content(
        self,
        *,
        path: str,
        raw_text: str,
    ) -> UefiEspFileContent:
        encoded = raw_text.encode("utf-8", errors="surrogateescape")
        is_text = self._is_probably_text(content=encoded)
        encoding: Optional[str]
        if is_text:
            encoding = "utf-8"
        else:
            encoding = None
        rtnval = UefiEspFileContent(
            path=path,
            content=encoded,
            is_text=is_text,
            encoding=encoding,
        )
        return rtnval

    def _is_probably_text(self, *, content: bytes) -> bool:
        if len(content) == 0:
            rtnval = True
            return rtnval

        sample_size = min(4096, len(content))
        sample = content[:sample_size]
        if b"\x00" in sample:
            rtnval = False
            return rtnval
        rtnval = True
        return rtnval

    def _dry_run_result(
        self,
        *,
        command: Optional[list[str]],
        detail: Optional[str],
        path: Optional[str],
        mountpoint: Optional[str],
    ) -> UefiEspActionResult:
        rtnval = self._wrap_result(
            status=self.status_class.DRY_RUN,
            detail=detail,
            command=command,
            path=path,
            mountpoint=mountpoint,
            host_os=self._host_os,
        )
        return rtnval

    def _build_dry_run_mount_command(
        self,
        *,
        target: str,
    ) -> Optional[list[str]]:
        if self._host_os == HOST_OS_LINUX:
            rtnval = [self._typed_config.mount_path, target]
        elif self._host_os == HOST_OS_DARWIN:
            rtnval = [
                self._typed_config.diskutil_path,
                "mount",
                self._typed_config.darwin_esp_device,
            ]
        elif self._host_os == HOST_OS_WINDOWS:
            letter = self._extract_drive_letter(mountpoint=target)
            rtnval = [
                self._typed_config.mountvol_path,
                "{}:".format(letter),
                "/S",
            ]
        else:
            rtnval = None
        return rtnval

    def _build_dry_run_unmount_command(
        self,
        *,
        target: str,
    ) -> Optional[list[str]]:
        if self._host_os == HOST_OS_LINUX:
            rtnval = [self._typed_config.umount_path, target]
        elif self._host_os == HOST_OS_DARWIN:
            rtnval = [self._typed_config.diskutil_path, "unmount", target]
        elif self._host_os == HOST_OS_WINDOWS:
            letter = self._extract_drive_letter(mountpoint=target)
            rtnval = [
                self._typed_config.mountvol_path,
                "{}:".format(letter),
                "/D",
            ]
        else:
            rtnval = None
        return rtnval

    def _build_dry_run_write_command(
        self,
        *,
        full_path: str,
        content: bytes,
    ) -> Optional[list[str]]:
        if self._host_os in (HOST_OS_LINUX, HOST_OS_DARWIN):
            rtnval = [self._typed_config.tee_path, full_path]
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = [
                self._typed_config.powershell_path,
                "-NoProfile",
                "-Command",
                (
                    "$payload = [Console]::In.ReadToEnd(); "
                    "Set-Content -LiteralPath '{}' -Value $payload -NoNewline"
                ).format(full_path),
            ]
        else:
            rtnval = None
        return rtnval

    def _build_dry_run_delete_command(
        self,
        *,
        full_path: str,
    ) -> Optional[list[str]]:
        if self._host_os in (HOST_OS_LINUX, HOST_OS_DARWIN):
            rtnval = [self._typed_config.rm_path, "-f", full_path]
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = [
                self._typed_config.powershell_path,
                "-NoProfile",
                "-Command",
                "Remove-Item -LiteralPath '{}' -Force".format(full_path),
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
    ) -> UefiEspActionResult:
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

    def _parse_findmnt_json(self, *, text: str) -> Optional[dict]:
        if text is None or len(text.strip()) == 0:
            rtnval: Optional[dict] = None
            return rtnval

        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            rtnval = None
            return rtnval

        if not isinstance(data, dict):
            rtnval = None
            return rtnval

        filesystems = data.get("filesystems")
        if filesystems is None or not isinstance(filesystems, list):
            rtnval = None
            return rtnval

        if len(filesystems) == 0:
            rtnval = None
            return rtnval

        first = filesystems[0]
        if not isinstance(first, dict):
            rtnval = None
            return rtnval

        record = {
            "target": first.get("target"),
            "source": first.get("source"),
            "fstype": first.get("fstype"),
        }
        rtnval = record
        return rtnval

    def _parse_lsblk_size(
        self,
        *,
        text: str,
        device_path: str,
    ) -> Optional[int]:
        if text is None or len(text.strip()) == 0:
            rtnval: Optional[int] = None
            return rtnval

        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            rtnval = None
            return rtnval

        if not isinstance(data, dict):
            rtnval = None
            return rtnval

        block_devices = data.get("blockdevices")
        if block_devices is None or not isinstance(block_devices, list):
            rtnval = None
            return rtnval

        target_basename = device_path.rsplit("/", 1)[-1]

        for device in block_devices:
            if not isinstance(device, dict):
                continue
            children = device.get("children")
            if children is None or not isinstance(children, list):
                continue
            for child in children:
                if not isinstance(child, dict):
                    continue
                name = child.get("name")
                if name == target_basename:
                    raw_size = child.get("size")
                    if isinstance(raw_size, int):
                        rtnval = raw_size
                        return rtnval
                    if isinstance(raw_size, str):
                        # ``lsblk -b`` would emit numeric strings; ignore
                        # human-readable forms.
                        if raw_size.isdigit():
                            rtnval = int(raw_size)
                            return rtnval

        rtnval = None
        return rtnval

    def _parse_diskutil_list_for_esp(self, *, text: str) -> Optional[str]:
        # The ``diskutil list -plist`` output is XML; this best-effort
        # parser extracts the device identifier of the EFI partition.
        # Real plist parsing would belong in a separate helper module;
        # for our needs the string-search heuristic is sufficient.
        rtnval: Optional[str] = None
        in_efi_section = False
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if "<string>EFI</string>" in line:
                in_efi_section = True
                continue
            if in_efi_section and line.startswith("<key>DeviceIdentifier</key>"):
                continue
            if in_efi_section and line.startswith("<string>") and line.endswith("</string>"):
                value = line[len("<string>"):-len("</string>")]
                if value != "EFI":
                    rtnval = value
                    return rtnval
        return rtnval

    def _parse_diskutil_info(self, *, text: str) -> dict:
        rtnval: dict = {}
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key_clean = key.strip()
            value_clean = value.strip()

            if key_clean == "Mount Point":
                if len(value_clean) > 0 and value_clean.lower() != "not applicable":
                    rtnval["mountpoint"] = value_clean
            elif key_clean == "Mounted":
                rtnval["mounted"] = value_clean.lower().startswith("yes")
            elif key_clean == "File System Personality":
                rtnval["filesystem"] = value_clean
            elif key_clean == "Volume Total Space":
                # Format: "209.7 MB (209715200 Bytes) (...)".
                if "(" in value_clean and "Bytes" in value_clean:
                    head, _, after = value_clean.partition("(")
                    digits, _, _ = after.partition(" ")
                    if digits.isdigit():
                        rtnval["size_bytes"] = int(digits)
        return rtnval

    def _parse_get_partition_json(
        self,
        *,
        text: str,
    ) -> tuple[Optional[str], Optional[int]]:
        if text is None or len(text.strip()) == 0:
            return None, None

        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return None, None

        if isinstance(data, dict):
            entries = [data]
        elif isinstance(data, list):
            entries = data
        else:
            return None, None

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            partition_type = entry.get("Type")
            if partition_type != "System":
                continue
            device = (
                entry.get("DiskNumber") is not None
                and "Disk{}Partition{}".format(
                    entry.get("DiskNumber"),
                    entry.get("PartitionNumber"),
                )
                or entry.get("ObjectId")
            )
            raw_size = entry.get("Size")
            if isinstance(raw_size, int):
                size_bytes: Optional[int] = raw_size
            else:
                size_bytes = None
            return device, size_bytes

        return None, None

    def _parse_get_volume_for_drive_letter(
        self,
        *,
        text: str,
    ) -> Optional[tuple[Optional[str], Optional[str]]]:
        if text is None or len(text.strip()) == 0:
            rtnval: Optional[tuple[Optional[str], Optional[str]]] = None
            return rtnval

        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            rtnval = None
            return rtnval

        if isinstance(data, dict):
            entries = [data]
        elif isinstance(data, list):
            entries = data
        else:
            rtnval = None
            return rtnval

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            drive_type = entry.get("DriveType")
            file_system_label = entry.get("FileSystemLabel")
            file_system = entry.get("FileSystem") or entry.get("FileSystemType")
            drive_letter_value = entry.get("DriveLetter")

            is_system = False
            if file_system_label is not None and "EFI" in str(file_system_label).upper():
                is_system = True
            if isinstance(file_system, str) and "FAT" in file_system.upper():
                # FAT volume — likely the ESP only when also lacking a
                # writable role; we accept it as a best-effort match.
                is_system = is_system or drive_type == "Fixed"

            if not is_system:
                continue

            if isinstance(drive_letter_value, str) and len(drive_letter_value) > 0:
                rtnval = (drive_letter_value, file_system)
                return rtnval
            rtnval = (None, file_system)
            return rtnval

        rtnval = (None, None)
        return rtnval

    def _parse_ls_la(
        self,
        *,
        text: str,
        base_path: str,
    ) -> list[UefiEspFileInfo]:
        rtnval: list[UefiEspFileInfo] = []
        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            if len(line.strip()) == 0:
                continue
            if line.startswith("total "):
                continue

            parts = line.split(None, 8)
            if len(parts) < 9:
                continue

            mode = parts[0]
            size_str = parts[4]
            modified = " ".join(parts[5:8])
            name = parts[8]

            if name in (".", ".."):
                continue

            is_directory = mode.startswith("d")
            size_bytes: Optional[int]
            if size_str.isdigit():
                size_bytes = int(size_str)
            else:
                size_bytes = None

            full_path = base_path.rstrip("/") + "/" + name
            entry = UefiEspFileInfo(
                name=name,
                path=full_path,
                size_bytes=size_bytes,
                is_directory=is_directory,
                modified=modified,
            )
            rtnval.append(entry)
        return rtnval

    def _parse_get_childitem_json(
        self,
        *,
        text: str,
        base_path: str,
    ) -> list[UefiEspFileInfo]:
        rtnval: list[UefiEspFileInfo] = []
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
            name = entry.get("Name")
            if name is None:
                continue
            full_path = entry.get("FullName") or (
                base_path.rstrip("\\/") + "\\" + str(name)
            )
            mode = entry.get("Mode") or entry.get("Attributes") or ""
            mode_str = str(mode)
            is_directory = "d" in mode_str.lower() or "directory" in mode_str.lower()
            raw_size = entry.get("Length")
            size_bytes: Optional[int]
            if isinstance(raw_size, int):
                size_bytes = raw_size
            else:
                size_bytes = None
            modified = entry.get("LastWriteTime")
            modified_str: Optional[str]
            if modified is None:
                modified_str = None
            else:
                modified_str = str(modified)

            record = UefiEspFileInfo(
                name=str(name),
                path=str(full_path),
                size_bytes=size_bytes,
                is_directory=is_directory,
                modified=modified_str,
            )
            rtnval.append(record)
        return rtnval
