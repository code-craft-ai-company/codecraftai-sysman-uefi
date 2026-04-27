"""
    UEFI boot-management feature.

    Implements the cross-OS boot-management contract for UEFI firmware on
    whichever host the manager runs on.  Dispatches to host-specific
    user-space tooling:

        * Linux   -> ``efibootmgr``
        * Darwin  -> ``bless`` + ``nvram``
        * Windows -> ``bcdedit``

    Every verb is dispatched through an :class:`ICommandRunner` so the
    manager works identically under local and SSH execution.

    Per-host-OS dispatch:
        :class:`UefiBootManager` records the running host OS once at
        construction time (via :func:`detect_host_os`) and routes every
        verb through ``_dispatch_<linux|darwin|windows>_<verb>`` private
        methods.  Verbs with no scriptable host-OS equivalent return
        ``UefiBootActionResult`` with status ``FAILED`` and a stable
        ``unsupported_on_host_os_<host>`` detail token instead of silently
        no-oping.

    Elevation policy:
        Writes pass ``elevated=True`` when ``config.elevated`` is True;
        reads never elevate.
"""

import os
import platform

from dataclasses import dataclass, field
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
    detect_host_os,
    normalise_override_host_os,
)


_DEFAULT_EFIBOOTMGR = "efibootmgr"
_DEFAULT_BCDEDIT = "bcdedit"
_DEFAULT_BLESS = "bless"
_DEFAULT_NVRAM = "nvram"


UefiBootActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------


@dataclass
class UefiBootManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UefiBootManager`.  Inherits ``dry_run``
        and ``elevated`` from :class:`FeatureConfig`.

        :param override_host_os: When set, forces the dispatch path to
                                 ``"linux"``, ``"darwin"``, or ``"windows"``
                                 (case-insensitive) instead of calling
                                 :func:`platform.system`.  Reserved for
                                 testing.
        :param efibootmgr_path: Path / argv-name of ``efibootmgr``.
                                Override only for testing or non-standard
                                installations.
        :param bcdedit_path: Path / argv-name of ``bcdedit``.
        :param bless_path: Path / argv-name of ``bless``.
        :param nvram_path: Path / argv-name of ``nvram``.
    """
    override_host_os: Optional[str] = None
    efibootmgr_path: str = _DEFAULT_EFIBOOTMGR
    bcdedit_path: str = _DEFAULT_BCDEDIT
    bless_path: str = _DEFAULT_BLESS
    nvram_path: str = _DEFAULT_NVRAM


@dataclass
class UefiBootEntrySpec:
    """
        Specification for a new firmware boot entry.

        :param label: Human-readable label.
        :param loader: Loader path on the ESP (e.g.,
                       ``\\EFI\\ubuntu\\shimx64.efi``).
        :param disk: Backing disk device (Linux/macOS: ``/dev/sda``,
                     ``disk0``; Windows: drive letter without colon).
                     Optional for hosts that do not consume it.
        :param partition: Partition number (Linux ``efibootmgr -p``,
                          Windows ``device partition=...``).  Optional.
        :param args: Additional bootloader arguments to encode into the
                     load option.  Optional.
    """
    label: str
    loader: str
    disk: Optional[str] = None
    partition: Optional[int] = None
    args: Optional[str] = None


@dataclass
class UefiBootEntryInfo:
    """
        One firmware boot-entry record.

        :param entry_id: Firmware identifier (4-hex on Linux; opaque
                         string on Darwin; GUID on Windows).
        :param label: Human-readable label.
        :param loader: Loader path on the ESP, or ``None`` when the host
                       tool does not surface one.
        :param is_active: Whether the firmware marks the entry as active.
        :param is_default: Whether the entry is the firmware's default.
                           On Linux this is the first entry in
                           ``BootOrder``; on Windows it is the first GUID
                           in ``displayorder``; on Darwin it is whichever
                           entry matches ``BootCurrent``.
    """
    entry_id: str
    label: str
    loader: Optional[str] = None
    is_active: bool = True
    is_default: bool = False


@dataclass
class UefiBootActionResult(FeatureActionResult):
    """
        Outcome of a boot-management write.

        :param entry_id: The boot entry the action targeted, when known.
        :param host_os: The host OS the action dispatched to, lower-cased.
    """
    entry_id: Optional[str] = None
    host_os: Optional[str] = None


class UefiBootQueryError(Exception):
    """
        Raised when a boot-entry read fails or a target entry is missing.
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
            :param command: The argv that failed (when applicable).
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

        err_msg_lines = ["UEFI boot-entry query failed."]
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


class UefiBootManager(BaseFeatureManager):
    """
        Per-host-OS UEFI boot-entry manager.  Each verb routes through a
        ``_dispatch_<host>_<verb>`` private helper selected by
        ``self._host_os``.  All commands flow through the injected
        :class:`ICommandRunner`.
    """

    result_class = UefiBootActionResult
    status_class = UefiBootActionStatus
    feature_name = "boot-management"

    def __init__(
        self,
        runner: ICommandRunner,
        config: Optional[UefiBootManagerConfig] = None,
    ) -> None:
        """
            :param runner: The command runner.
            :param config: Manager configuration.  Defaults to a
                           :class:`UefiBootManagerConfig` with no
                           overrides.
        """
        if config is None:
            config = UefiBootManagerConfig()
        super().__init__(runner=runner, config=config)
        self._typed_config: UefiBootManagerConfig = config
        return

    @property
    def typed_config(self) -> UefiBootManagerConfig:
        """
            The active :class:`UefiBootManagerConfig`.
        """
        return self._typed_config

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_boot_entries(self) -> list[UefiBootEntryInfo]:
        """
            Enumerate firmware boot entries on the host OS.

            :returns: A list of :class:`UefiBootEntryInfo` records.
            :raises UefiBootQueryError: When the underlying tool fails.
        """
        if self._host_os == HOST_OS_LINUX:
            rtnval = self._dispatch_linux_list_boot_entries()
        elif self._host_os == HOST_OS_DARWIN:
            rtnval = self._dispatch_darwin_list_boot_entries()
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = self._dispatch_windows_list_boot_entries()
        else:
            err_msg = "Unsupported host OS for list_boot_entries: {}".format(
                self._host_os,
            )
            raise UefiBootQueryError(message=err_msg)
        return rtnval

    def get_boot_entry(self, entry_id: str) -> UefiBootEntryInfo:
        """
            Fetch a single boot entry by identifier.

            :param entry_id: Firmware identifier.

            :returns: The matching :class:`UefiBootEntryInfo`.
            :raises UefiBootQueryError: When the entry is not found.
        """
        all_entries = self.list_boot_entries()
        target = entry_id.strip()

        for entry in all_entries:
            if entry.entry_id == target:
                rtnval = entry
                return rtnval

        err_msg = "boot entry not found: {}".format(target)
        raise UefiBootQueryError(message=err_msg)

    def get_boot_order(self) -> list[str]:
        """
            Read the persistent ``BootOrder`` (Linux), ``displayorder``
            (Windows), or single ``efi-boot-device`` (macOS).

            :returns: Ordered list of identifiers.
            :raises UefiBootQueryError: When the underlying tool fails.
        """
        if self._host_os == HOST_OS_LINUX:
            rtnval = self._dispatch_linux_get_boot_order()
        elif self._host_os == HOST_OS_DARWIN:
            rtnval = self._dispatch_darwin_get_boot_order()
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = self._dispatch_windows_get_boot_order()
        else:
            err_msg = "Unsupported host OS for get_boot_order: {}".format(
                self._host_os,
            )
            raise UefiBootQueryError(message=err_msg)
        return rtnval

    def get_boot_current(self) -> Optional[str]:
        """
            Read the firmware identifier of the entry used for the current
            boot.

            :returns: The 4-hex / GUID / opaque string, or ``None`` when
                      the host tool does not surface a current entry.
            :raises UefiBootQueryError: When the underlying tool fails.
        """
        if self._host_os == HOST_OS_LINUX:
            rtnval = self._dispatch_linux_get_boot_current()
        elif self._host_os == HOST_OS_DARWIN:
            rtnval = self._dispatch_darwin_get_boot_current()
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = self._dispatch_windows_get_boot_current()
        else:
            err_msg = "Unsupported host OS for get_boot_current: {}".format(
                self._host_os,
            )
            raise UefiBootQueryError(message=err_msg)
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def create_boot_entry(self, spec: UefiBootEntrySpec) -> UefiBootActionResult:
        """
            Create a firmware boot entry.

            Idempotency: when an entry with the same ``label`` and
            ``loader`` already exists, returns ``SKIPPED``.

            :param spec: The boot-entry specification.

            :returns: The :class:`UefiBootActionResult`.
        """
        if self._config.dry_run:
            rtnval = self._dry_run_result(
                command=self._build_dry_run_create_command(spec=spec),
                detail=spec.label,
            )
            return rtnval

        existing = self._safe_list_boot_entries()
        if existing is not None:
            for entry in existing:
                same_label = entry.label == spec.label
                same_loader = (entry.loader is not None and entry.loader == spec.loader)
                if same_label and same_loader:
                    skipped = self._wrap_result(
                        status=self.status_class.SKIPPED,
                        detail="entry with same label+loader already exists",
                        command=None,
                        entry_id=entry.entry_id,
                        host_os=self._host_os,
                    )
                    return skipped

        if self._host_os == HOST_OS_LINUX:
            rtnval = self._dispatch_linux_create_boot_entry(spec=spec)
        elif self._host_os == HOST_OS_DARWIN:
            rtnval = self._dispatch_darwin_create_boot_entry(spec=spec)
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = self._dispatch_windows_create_boot_entry(spec=spec)
        else:
            rtnval = self._build_failed_unsupported(
                verb="create_boot_entry",
                host_os=self._host_os,
                entry_id=None,
                host_os_field=self._host_os,
            )
        return rtnval

    def delete_boot_entry(self, entry_id: str) -> UefiBootActionResult:
        """
            Delete a firmware boot entry.

            Idempotency: when the entry does not exist, returns ``SKIPPED``.

            :param entry_id: Firmware identifier.

            :returns: The :class:`UefiBootActionResult`.
        """
        if self._config.dry_run:
            rtnval = self._dry_run_result(
                command=self._build_dry_run_delete_command(entry_id=entry_id),
                detail=entry_id,
                entry_id=entry_id,
            )
            return rtnval

        # Hosts where this verb has no scriptable implementation are
        # short-circuited before the idempotency probe so the stable
        # FAILED token is always returned.
        if self._host_os == HOST_OS_DARWIN:
            rtnval = self._build_failed_unsupported(
                verb="delete_boot_entry",
                host_os=self._host_os,
                message="bless does not expose scriptable single-entry deletion",
                entry_id=entry_id,
                host_os_field=self._host_os,
            )
            return rtnval

        existing = self._safe_list_boot_entries()
        if existing is not None:
            present = any(entry.entry_id == entry_id for entry in existing)
            if not present:
                skipped = self._wrap_result(
                    status=self.status_class.SKIPPED,
                    detail="entry does not exist",
                    command=None,
                    entry_id=entry_id,
                    host_os=self._host_os,
                )
                return skipped

        if self._host_os == HOST_OS_LINUX:
            rtnval = self._dispatch_linux_delete_boot_entry(entry_id=entry_id)
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = self._dispatch_windows_delete_boot_entry(entry_id=entry_id)
        else:
            rtnval = self._build_failed_unsupported(
                verb="delete_boot_entry",
                host_os=self._host_os,
                entry_id=entry_id,
                host_os_field=self._host_os,
            )
        return rtnval

    def set_boot_order(self, order: list[str]) -> UefiBootActionResult:
        """
            Write a new persistent boot order.

            Idempotency: when the current order equals ``order``, returns
            ``SKIPPED``.

            :param order: Desired sequence of entry identifiers.

            :returns: The :class:`UefiBootActionResult`.
        """
        order_copy = list(order)

        if self._config.dry_run:
            rtnval = self._dry_run_result(
                command=self._build_dry_run_set_order_command(order=order_copy),
                detail=",".join(order_copy),
            )
            return rtnval

        current = self._safe_get_boot_order()
        if current is not None and current == order_copy:
            skipped = self._wrap_result(
                status=self.status_class.SKIPPED,
                detail="boot order already matches",
                command=None,
                entry_id=None,
                host_os=self._host_os,
            )
            return skipped

        if self._host_os == HOST_OS_LINUX:
            rtnval = self._dispatch_linux_set_boot_order(order=order_copy)
        elif self._host_os == HOST_OS_DARWIN:
            rtnval = self._build_failed_unsupported(
                verb="set_boot_order",
                host_os=self._host_os,
                message="bless --setBoot manages a single boot device, not an ordered list",
                entry_id=None,
                host_os_field=self._host_os,
            )
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = self._dispatch_windows_set_boot_order(order=order_copy)
        else:
            rtnval = self._build_failed_unsupported(
                verb="set_boot_order",
                host_os=self._host_os,
                entry_id=None,
                host_os_field=self._host_os,
            )
        return rtnval

    def set_boot_next(self, entry_id: str) -> UefiBootActionResult:
        """
            Write a one-shot ``BootNext`` (Linux) / ``bootsequence``
            (Windows).

            Idempotency: when the current ``BootNext`` already matches the
            requested ``entry_id``, returns ``SKIPPED``.

            :param entry_id: Firmware identifier.

            :returns: The :class:`UefiBootActionResult`.
        """
        if self._config.dry_run:
            rtnval = self._dry_run_result(
                command=self._build_dry_run_set_next_command(entry_id=entry_id),
                detail=entry_id,
                entry_id=entry_id,
            )
            return rtnval

        current = self._safe_get_boot_next()
        if current is not None and current == entry_id:
            skipped = self._wrap_result(
                status=self.status_class.SKIPPED,
                detail="BootNext already set",
                command=None,
                entry_id=entry_id,
                host_os=self._host_os,
            )
            return skipped

        if self._host_os == HOST_OS_LINUX:
            rtnval = self._dispatch_linux_set_boot_next(entry_id=entry_id)
        elif self._host_os == HOST_OS_DARWIN:
            rtnval = self._build_failed_unsupported(
                verb="set_boot_next",
                host_os=self._host_os,
                message="macOS user-space tooling does not expose BootNext",
                entry_id=entry_id,
                host_os_field=self._host_os,
            )
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = self._dispatch_windows_set_boot_next(entry_id=entry_id)
        else:
            rtnval = self._build_failed_unsupported(
                verb="set_boot_next",
                host_os=self._host_os,
                entry_id=entry_id,
                host_os_field=self._host_os,
            )
        return rtnval

    def get_boot_next(self) -> Optional[str]:
        """
            Read the one-shot ``BootNext`` value, or ``None`` when not
            set.

            :returns: The 4-hex / GUID identifier or ``None``.
            :raises UefiBootQueryError: When the underlying tool fails.
        """
        if self._host_os == HOST_OS_LINUX:
            rtnval = self._dispatch_linux_get_boot_next()
        elif self._host_os == HOST_OS_DARWIN:
            rtnval = None
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = self._dispatch_windows_get_boot_next()
        else:
            err_msg = "Unsupported host OS for get_boot_next: {}".format(
                self._host_os,
            )
            raise UefiBootQueryError(message=err_msg)
        return rtnval

    # ------------------------------------------------------------------
    # Linux dispatch helpers
    # ------------------------------------------------------------------

    def _dispatch_linux_list_boot_entries(self) -> list[UefiBootEntryInfo]:
        command = [self._typed_config.efibootmgr_path, "-v"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UefiBootQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = self._parse_efibootmgr_verbose(text=run_result.stdout)
        return rtnval

    def _dispatch_linux_get_boot_order(self) -> list[str]:
        command = [self._typed_config.efibootmgr_path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UefiBootQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = self._parse_efibootmgr_boot_order(text=run_result.stdout)
        return rtnval

    def _dispatch_linux_get_boot_current(self) -> Optional[str]:
        command = [self._typed_config.efibootmgr_path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UefiBootQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = self._parse_efibootmgr_named_line(
            text=run_result.stdout,
            prefix="BootCurrent:",
        )
        return rtnval

    def _dispatch_linux_get_boot_next(self) -> Optional[str]:
        command = [self._typed_config.efibootmgr_path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UefiBootQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = self._parse_efibootmgr_named_line(
            text=run_result.stdout,
            prefix="BootNext:",
        )
        return rtnval

    def _dispatch_linux_create_boot_entry(
        self,
        *,
        spec: UefiBootEntrySpec,
    ) -> UefiBootActionResult:
        command = [
            self._typed_config.efibootmgr_path,
            "-c",
            "-L", spec.label,
            "-l", spec.loader,
        ]
        if spec.disk is not None:
            command.append("-d")
            command.append(spec.disk)
        if spec.partition is not None:
            command.append("-p")
            command.append(str(spec.partition))
        if spec.args is not None:
            command.append("-u")
            command.append(spec.args)

        rtnval = self._dispatch(
            command=command,
            detail=spec.label,
            operation="create_boot_entry",
            entry_id=None,
            host_os=self._host_os,
        )

        if rtnval.status == self.status_class.SUCCESS:
            new_id = self._parse_efibootmgr_new_id(text=rtnval.stdout or "")
            if new_id is not None:
                rtnval.entry_id = new_id
        return rtnval

    def _dispatch_linux_delete_boot_entry(
        self,
        *,
        entry_id: str,
    ) -> UefiBootActionResult:
        command = [
            self._typed_config.efibootmgr_path,
            "-b", entry_id,
            "-B",
        ]
        rtnval = self._dispatch(
            command=command,
            detail=entry_id,
            operation="delete_boot_entry",
            entry_id=entry_id,
            host_os=self._host_os,
        )
        return rtnval

    def _dispatch_linux_set_boot_order(
        self,
        *,
        order: list[str],
    ) -> UefiBootActionResult:
        joined = ",".join(order)
        command = [
            self._typed_config.efibootmgr_path,
            "-o", joined,
        ]
        rtnval = self._dispatch(
            command=command,
            detail=joined,
            operation="set_boot_order",
            entry_id=None,
            host_os=self._host_os,
        )
        return rtnval

    def _dispatch_linux_set_boot_next(
        self,
        *,
        entry_id: str,
    ) -> UefiBootActionResult:
        command = [
            self._typed_config.efibootmgr_path,
            "-n", entry_id,
        ]
        rtnval = self._dispatch(
            command=command,
            detail=entry_id,
            operation="set_boot_next",
            entry_id=entry_id,
            host_os=self._host_os,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Darwin dispatch helpers
    # ------------------------------------------------------------------

    def _dispatch_darwin_list_boot_entries(self) -> list[UefiBootEntryInfo]:
        bless_command = [self._typed_config.bless_path, "--info", "--verbose"]
        nvram_command = [self._typed_config.nvram_path, "-p"]

        bless_result = self._runner.run(list(bless_command), elevated=False)
        if bless_result.exit_code != 0:
            raise UefiBootQueryError(
                command=bless_command,
                exit_code=bless_result.exit_code,
                stderr=bless_result.stderr,
            )

        nvram_result = self._runner.run(list(nvram_command), elevated=False)
        if nvram_result.exit_code != 0:
            raise UefiBootQueryError(
                command=nvram_command,
                exit_code=nvram_result.exit_code,
                stderr=nvram_result.stderr,
            )

        current = self._parse_nvram_efi_boot_device(text=nvram_result.stdout)
        rtnval = self._parse_bless_info(
            text=bless_result.stdout,
            current_id=current,
        )
        return rtnval

    def _dispatch_darwin_get_boot_order(self) -> list[str]:
        # macOS exposes only the single ``efi-boot-device`` value.
        command = [self._typed_config.nvram_path, "efi-boot-device"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UefiBootQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        single = self._parse_nvram_efi_boot_device(text=run_result.stdout)
        if single is None:
            rtnval: list[str] = []
        else:
            rtnval = [single]
        return rtnval

    def _dispatch_darwin_get_boot_current(self) -> Optional[str]:
        command = [self._typed_config.nvram_path, "efi-boot-device"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UefiBootQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = self._parse_nvram_efi_boot_device(text=run_result.stdout)
        return rtnval

    def _dispatch_darwin_create_boot_entry(
        self,
        *,
        spec: UefiBootEntrySpec,
    ) -> UefiBootActionResult:
        # macOS ``bless --setBoot`` does not take a distinct loader path
        # the way ``efibootmgr`` does; the closest equivalent is to bless
        # a mounted ESP/system folder.  We use ``--mount`` when the spec
        # has a usable ``disk`` field and surface this gap in the detail.
        if spec.disk is None:
            rtnval = self._build_failed_unsupported(
                verb="create_boot_entry",
                host_os=self._host_os,
                message="macOS bless requires a mountpoint via spec.disk",
                entry_id=None,
                host_os_field=self._host_os,
            )
            return rtnval

        command = [
            self._typed_config.bless_path,
            "--mount", spec.disk,
            "--setBoot",
        ]
        rtnval = self._dispatch(
            command=command,
            detail=spec.label,
            operation="create_boot_entry",
            entry_id=None,
            host_os=self._host_os,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Windows dispatch helpers
    # ------------------------------------------------------------------

    def _dispatch_windows_list_boot_entries(self) -> list[UefiBootEntryInfo]:
        command = [self._typed_config.bcdedit_path, "/enum", "firmware"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UefiBootQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = self._parse_bcdedit_enum_firmware(text=run_result.stdout)
        return rtnval

    def _dispatch_windows_get_boot_order(self) -> list[str]:
        command = [self._typed_config.bcdedit_path, "/enum", "{fwbootmgr}"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UefiBootQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = self._parse_bcdedit_displayorder(text=run_result.stdout)
        return rtnval

    def _dispatch_windows_get_boot_current(self) -> Optional[str]:
        command = [self._typed_config.bcdedit_path, "/enum", "{current}"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UefiBootQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = self._parse_bcdedit_identifier(text=run_result.stdout)
        return rtnval

    def _dispatch_windows_get_boot_next(self) -> Optional[str]:
        command = [self._typed_config.bcdedit_path, "/enum", "{fwbootmgr}"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UefiBootQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = self._parse_bcdedit_bootsequence(text=run_result.stdout)
        return rtnval

    def _dispatch_windows_create_boot_entry(
        self,
        *,
        spec: UefiBootEntrySpec,
    ) -> UefiBootActionResult:
        # Three-step process; we surface the first command in the result.
        create_command = [
            self._typed_config.bcdedit_path,
            "/create",
            "/d", spec.label,
            "/application", "BOOTSECTOR",
        ]
        first = self._dispatch(
            command=create_command,
            detail=spec.label,
            operation="create_boot_entry",
            entry_id=None,
            host_os=self._host_os,
        )
        if first.status != self.status_class.SUCCESS:
            return first

        new_guid = self._parse_bcdedit_create_guid(text=first.stdout or "")
        if new_guid is None:
            failed = self._wrap_result(
                status=self.status_class.FAILED,
                detail="bcdedit /create succeeded but did not emit a GUID",
                command=create_command,
                exit_code=first.exit_code,
                stdout=first.stdout,
                stderr=first.stderr,
                entry_id=None,
                host_os=self._host_os,
            )
            return failed

        if spec.disk is not None and spec.partition is not None:
            partition_arg = "partition={}:".format(spec.disk)
            device_command = [
                self._typed_config.bcdedit_path,
                "/set", new_guid,
                "device", partition_arg,
            ]
            device_result = self._dispatch(
                command=device_command,
                detail=spec.label,
                operation="create_boot_entry",
                entry_id=new_guid,
                host_os=self._host_os,
            )
            if device_result.status != self.status_class.SUCCESS:
                device_result.detail = "bcdedit /set device step failed"
                return device_result

        path_command = [
            self._typed_config.bcdedit_path,
            "/set", new_guid,
            "path", spec.loader,
        ]
        path_result = self._dispatch(
            command=path_command,
            detail=spec.label,
            operation="create_boot_entry",
            entry_id=new_guid,
            host_os=self._host_os,
        )
        if path_result.status != self.status_class.SUCCESS:
            path_result.detail = "bcdedit /set path step failed"
            return path_result

        # Re-stamp the originating command in the success result so the
        # coordinator has a single representative argv to log.
        path_result.command = create_command
        path_result.entry_id = new_guid
        return path_result

    def _dispatch_windows_delete_boot_entry(
        self,
        *,
        entry_id: str,
    ) -> UefiBootActionResult:
        command = [
            self._typed_config.bcdedit_path,
            "/delete", entry_id,
        ]
        rtnval = self._dispatch(
            command=command,
            detail=entry_id,
            operation="delete_boot_entry",
            entry_id=entry_id,
            host_os=self._host_os,
        )
        return rtnval

    def _dispatch_windows_set_boot_order(
        self,
        *,
        order: list[str],
    ) -> UefiBootActionResult:
        command = [
            self._typed_config.bcdedit_path,
            "/set", "{fwbootmgr}",
            "displayorder",
        ]
        command.extend(order)
        rtnval = self._dispatch(
            command=command,
            detail=" ".join(order),
            operation="set_boot_order",
            entry_id=None,
            host_os=self._host_os,
        )
        return rtnval

    def _dispatch_windows_set_boot_next(
        self,
        *,
        entry_id: str,
    ) -> UefiBootActionResult:
        command = [
            self._typed_config.bcdedit_path,
            "/set", "{fwbootmgr}",
            "bootsequence", entry_id,
        ]
        rtnval = self._dispatch(
            command=command,
            detail=entry_id,
            operation="set_boot_next",
            entry_id=entry_id,
            host_os=self._host_os,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _safe_list_boot_entries(self) -> Optional[list[UefiBootEntryInfo]]:
        """
            Try to enumerate boot entries for an idempotency probe.
            Returns ``None`` when the underlying tool errors so the
            caller can proceed with the write rather than mask a real
            failure as ``SKIPPED``.
        """
        try:
            rtnval = self.list_boot_entries()
        except UefiBootQueryError:
            rtnval = None
        return rtnval

    def _safe_get_boot_order(self) -> Optional[list[str]]:
        try:
            rtnval = self.get_boot_order()
        except UefiBootQueryError:
            rtnval = None
        return rtnval

    def _safe_get_boot_next(self) -> Optional[str]:
        try:
            rtnval = self.get_boot_next()
        except UefiBootQueryError:
            rtnval = None
        return rtnval

    def _dry_run_result(
        self,
        *,
        command: Optional[list[str]],
        detail: Optional[str],
        entry_id: Optional[str] = None,
    ) -> UefiBootActionResult:
        rtnval = self._wrap_result(
            status=self.status_class.DRY_RUN,
            detail=detail,
            command=command,
            entry_id=entry_id,
            host_os=self._host_os,
        )
        return rtnval

    def _build_dry_run_create_command(
        self,
        *,
        spec: UefiBootEntrySpec,
    ) -> Optional[list[str]]:
        if self._host_os == HOST_OS_LINUX:
            command = [self._typed_config.efibootmgr_path, "-c", "-L", spec.label, "-l", spec.loader]
            if spec.disk is not None:
                command.extend(["-d", spec.disk])
            if spec.partition is not None:
                command.extend(["-p", str(spec.partition)])
            if spec.args is not None:
                command.extend(["-u", spec.args])
            rtnval = command
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = [
                self._typed_config.bcdedit_path,
                "/create",
                "/d", spec.label,
                "/application", "BOOTSECTOR",
            ]
        elif self._host_os == HOST_OS_DARWIN:
            if spec.disk is not None:
                rtnval = [
                    self._typed_config.bless_path,
                    "--mount", spec.disk,
                    "--setBoot",
                ]
            else:
                rtnval = None
        else:
            rtnval = None
        return rtnval

    def _build_dry_run_delete_command(
        self,
        *,
        entry_id: str,
    ) -> Optional[list[str]]:
        if self._host_os == HOST_OS_LINUX:
            rtnval = [self._typed_config.efibootmgr_path, "-b", entry_id, "-B"]
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = [self._typed_config.bcdedit_path, "/delete", entry_id]
        else:
            rtnval = None
        return rtnval

    def _build_dry_run_set_order_command(
        self,
        *,
        order: list[str],
    ) -> Optional[list[str]]:
        if self._host_os == HOST_OS_LINUX:
            rtnval = [self._typed_config.efibootmgr_path, "-o", ",".join(order)]
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = [
                self._typed_config.bcdedit_path,
                "/set", "{fwbootmgr}",
                "displayorder",
            ] + list(order)
        else:
            rtnval = None
        return rtnval

    def _build_dry_run_set_next_command(
        self,
        *,
        entry_id: str,
    ) -> Optional[list[str]]:
        if self._host_os == HOST_OS_LINUX:
            rtnval = [self._typed_config.efibootmgr_path, "-n", entry_id]
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = [
                self._typed_config.bcdedit_path,
                "/set", "{fwbootmgr}",
                "bootsequence", entry_id,
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
    ) -> UefiBootActionResult:
        # Translate ``host_os_field`` (used to avoid keyword shadowing in
        # callers that already pass ``host_os`` to outer helpers) into the
        # canonical ``host_os`` field of the result dataclass.
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

    def _parse_efibootmgr_verbose(
        self,
        *,
        text: str,
    ) -> list[UefiBootEntryInfo]:
        """
            Parse ``efibootmgr -v`` output.

            Recognised fragments:
                BootCurrent: 0001
                BootOrder:   0001,0000,2001
                Boot0000* ubuntu       HD(...)/File(\\EFI\\ubuntu\\shimx64.efi)
                Boot0001  Disabled     ...
        """
        boot_current = self._parse_efibootmgr_named_line(
            text=text, prefix="BootCurrent:",
        )
        boot_order = self._parse_efibootmgr_boot_order(text=text)

        rtnval: list[UefiBootEntryInfo] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line.startswith("Boot"):
                continue
            if line.startswith("BootOrder") or line.startswith("BootCurrent"):
                continue
            if line.startswith("BootNext"):
                continue

            # Format: BootXXXX[* ] <label>[ <hex-data>]
            if len(line) < 8:
                continue

            entry_id_raw = line[4:8]
            if not all(ch in "0123456789abcdefABCDEF" for ch in entry_id_raw):
                continue

            entry_id = entry_id_raw.lower()
            tail = line[8:]

            is_active = False
            if tail.startswith("*"):
                is_active = True
                tail = tail[1:]
            tail = tail.strip()

            label = tail
            loader: Optional[str] = None

            file_marker = "/File("
            if file_marker in tail:
                head, _, after = tail.partition(file_marker)
                label = head
                if ")" in after:
                    raw_loader, _, _rest = after.partition(")")
                    loader = raw_loader.strip()

            # The label may be separated from a trailing column (tab,
            # then "RC" / hex / device path) by either a tab or two or
            # more spaces.
            if "\t" in label:
                label = label.split("\t")[0]
            if "  " in label:
                label = label.split("  ")[0]
            label = label.strip()

            is_default = False
            if len(boot_order) > 0 and boot_order[0] == entry_id:
                is_default = True

            entry = UefiBootEntryInfo(
                entry_id=entry_id,
                label=label,
                loader=loader,
                is_active=is_active,
                is_default=is_default,
            )
            rtnval.append(entry)

        return rtnval

    def _parse_efibootmgr_boot_order(self, *, text: str) -> list[str]:
        named = self._parse_efibootmgr_named_line(
            text=text, prefix="BootOrder:",
        )
        if named is None:
            rtnval: list[str] = []
            return rtnval

        parts = [p.strip().lower() for p in named.split(",")]
        rtnval = [p for p in parts if len(p) > 0]
        return rtnval

    def _parse_efibootmgr_named_line(
        self,
        *,
        text: str,
        prefix: str,
    ) -> Optional[str]:
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith(prefix):
                _, _, value = line.partition(prefix)
                stripped = value.strip()
                if len(stripped) == 0:
                    rtnval: Optional[str] = None
                else:
                    rtnval = stripped.lower()
                return rtnval
        rtnval = None
        return rtnval

    def _parse_efibootmgr_new_id(self, *, text: str) -> Optional[str]:
        # ``efibootmgr -c`` emits "BootXXXX created\n" or includes the new
        # entry in the BootOrder line.  Prefer the first form.
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith("Boot") and "created" in line:
                hex_part = line[4:8]
                if all(ch in "0123456789abcdefABCDEF" for ch in hex_part):
                    rtnval: Optional[str] = hex_part.lower()
                    return rtnval
        rtnval = None
        return rtnval

    def _parse_bless_info(
        self,
        *,
        text: str,
        current_id: Optional[str],
    ) -> list[UefiBootEntryInfo]:
        rtnval: list[UefiBootEntryInfo] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if "=>" not in line:
                continue
            head, _, after = line.partition("=>")
            head_stripped = head.strip()
            after_stripped = after.strip()

            # finderinfo[0]: 100 => Blessed System File is /System/Library/CoreServices/boot.efi
            if "is " not in after_stripped:
                continue
            _label_text, _, path_part = after_stripped.partition("is ")
            loader = path_part.strip()

            entry_id = head_stripped
            label = head_stripped

            is_default = False
            if current_id is not None and current_id == entry_id:
                is_default = True

            entry = UefiBootEntryInfo(
                entry_id=entry_id,
                label=label,
                loader=loader,
                is_active=True,
                is_default=is_default,
            )
            rtnval.append(entry)
        return rtnval

    def _parse_nvram_efi_boot_device(self, *, text: str) -> Optional[str]:
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith("efi-boot-device"):
                # Format: efi-boot-device   <opaque-value>
                parts = line.split(None, 1)
                if len(parts) == 2:
                    rtnval: Optional[str] = parts[1].strip()
                else:
                    rtnval = None
                return rtnval
        rtnval = None
        return rtnval

    def _parse_bcdedit_enum_firmware(
        self,
        *,
        text: str,
    ) -> list[UefiBootEntryInfo]:
        rtnval: list[UefiBootEntryInfo] = []

        sections = self._split_bcdedit_sections(text=text)
        for section in sections:
            identifier = section.get("identifier")
            if identifier is None:
                continue
            if identifier in ("{fwbootmgr}",):
                continue

            label = section.get("description") or identifier
            loader = section.get("path")
            entry = UefiBootEntryInfo(
                entry_id=identifier.lower(),
                label=label,
                loader=loader,
                is_active=True,
                is_default=False,
            )
            rtnval.append(entry)
        return rtnval

    def _parse_bcdedit_displayorder(self, *, text: str) -> list[str]:
        rtnval: list[str] = []
        in_displayorder = False
        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if stripped.startswith("displayorder"):
                in_displayorder = True
                _, _, after = stripped.partition("displayorder")
                value = after.strip()
                if len(value) > 0:
                    rtnval.append(value.lower())
                continue

            if in_displayorder:
                if len(stripped) == 0:
                    in_displayorder = False
                    continue
                if stripped.startswith("{") and stripped.endswith("}"):
                    rtnval.append(stripped.lower())
                else:
                    in_displayorder = False
        return rtnval

    def _parse_bcdedit_bootsequence(self, *, text: str) -> Optional[str]:
        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if stripped.startswith("bootsequence"):
                _, _, after = stripped.partition("bootsequence")
                value = after.strip()
                if len(value) == 0:
                    rtnval: Optional[str] = None
                else:
                    rtnval = value.lower()
                return rtnval
        rtnval = None
        return rtnval

    def _parse_bcdedit_identifier(self, *, text: str) -> Optional[str]:
        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if stripped.startswith("identifier"):
                _, _, after = stripped.partition("identifier")
                value = after.strip()
                if len(value) == 0:
                    rtnval: Optional[str] = None
                else:
                    rtnval = value.lower()
                return rtnval
        rtnval = None
        return rtnval

    def _parse_bcdedit_create_guid(self, *, text: str) -> Optional[str]:
        # ``bcdedit /create`` emits something like:
        # "The entry {12345678-1234-1234-1234-123456789abc} was successfully created."
        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if "{" not in stripped or "}" not in stripped:
                continue
            start = stripped.find("{")
            end = stripped.find("}", start)
            if end <= start:
                continue
            candidate = stripped[start:end + 1]
            rtnval: Optional[str] = candidate.lower()
            return rtnval
        rtnval = None
        return rtnval

    def _split_bcdedit_sections(
        self,
        *,
        text: str,
    ) -> list[dict]:
        rtnval: list[dict] = []
        current: dict = {}
        current_key: Optional[str] = None

        for raw_line in text.splitlines():
            stripped = raw_line.rstrip()
            if len(stripped.strip()) == 0:
                if len(current) > 0:
                    rtnval.append(current)
                    current = {}
                    current_key = None
                continue

            if set(stripped.strip()) == {"-"}:
                # Underline separator; ignore.
                continue

            if stripped[:1].isspace():
                # Continuation of previous key (e.g., displayorder's GUIDs).
                if current_key is not None:
                    existing = current.get(current_key, "")
                    extra = stripped.strip()
                    if len(existing) > 0:
                        current[current_key] = existing + "\n" + extra
                    else:
                        current[current_key] = extra
                continue

            # Two whitespace-separated fields: key value (single or
            # leading line of a multi-line value).
            parts = stripped.split(None, 1)
            if len(parts) == 1:
                current_key = parts[0]
                current[current_key] = ""
            else:
                key = parts[0]
                value = parts[1].strip()
                current_key = key
                current[key] = value

        if len(current) > 0:
            rtnval.append(current)
        return rtnval
