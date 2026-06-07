"""
    UEFI EFI-variables feature.

    Implements the cross-OS efi-variables contract for raw NVRAM access on
    whichever host the manager runs on.  Dispatches to host-specific
    user-space tooling:

        * Linux   -> ``efivar`` (with ``chattr -i`` clearance for the
                     efivars-immutable flag) and the
                     ``/sys/firmware/efi/efivars`` filesystem
        * Darwin  -> ``nvram``
        * Windows -> PowerShell ``[UEFI]`` namespace (community module)

    Per-host-OS dispatch:
        :class:`UefiVariableManager` records the running host OS once at
        construction time (via :func:`detect_host_os`) and routes every
        verb through ``_dispatch_<linux|darwin|windows>_<verb>`` private
        methods.

    Namespace registry:
        Friendly aliases (``EFI_GLOBAL_VARIABLE``,
        ``EFI_IMAGE_SECURITY_DATABASE_GUID``, ``Apple``, ``Common``)
        normalise to canonical lower-cased GUID strings; aliases reserved
        on a host OS raise :class:`UefiVariableNamespaceError` without
        invoking any command.

    Elevation policy:
        Writes pass ``elevated=True`` when ``config.elevated`` is True;
        reads never elevate.
"""

import os
import platform

from dataclasses import dataclass, field
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
    detect_host_os,
    normalise_override_host_os,
)


_DEFAULT_EFIVAR = "efivar"
_DEFAULT_NVRAM = "nvram"
_DEFAULT_POWERSHELL = "powershell"
_DEFAULT_CHATTR = "chattr"
_DEFAULT_EFIVARS_DIR = "/sys/firmware/efi/efivars"


# ----------------------------------------------------------------------
# Attribute bit constants
# ----------------------------------------------------------------------


NON_VOLATILE                          = 0x01
BOOTSERVICE_ACCESS                    = 0x02
RUNTIME_ACCESS                        = 0x04
HARDWARE_ERROR_RECORD                 = 0x08
AUTHENTICATED_WRITE_ACCESS            = 0x10
TIME_BASED_AUTHENTICATED_WRITE_ACCESS = 0x20
APPEND_WRITE                          = 0x40

DEFAULT_ATTRIBUTES = NON_VOLATILE | BOOTSERVICE_ACCESS | RUNTIME_ACCESS  # 0x07


_ATTRIBUTE_NAME_TO_BIT = {
    "non-volatile":                          NON_VOLATILE,
    "boot service access":                   BOOTSERVICE_ACCESS,
    "runtime service access":                RUNTIME_ACCESS,
    "hardware error record":                 HARDWARE_ERROR_RECORD,
    "authenticated write access":            AUTHENTICATED_WRITE_ACCESS,
    "time based authenticated write access": TIME_BASED_AUTHENTICATED_WRITE_ACCESS,
    "append write":                          APPEND_WRITE,
}


# ----------------------------------------------------------------------
# Namespace registry
# ----------------------------------------------------------------------


_GUID_LENGTH = 36

_NAMESPACE_ALIAS_TO_GUID = {
    "EFI_GLOBAL_VARIABLE":              "8be4df61-93ca-11d2-aa0d-00e098032b8c",
    "EFI_IMAGE_SECURITY_DATABASE_GUID": "d719b2cb-3d3a-4596-a3bc-dad00e67656f",
}

# Aliases that are valid only on a specific host OS.  The host whose
# tooling owns the alias is the only one that may dispatch with it.
_HOST_OS_OWNED_ALIASES = {
    "Apple":  HOST_OS_DARWIN,
    "Common": HOST_OS_DARWIN,
}


UefiVariableActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------


@dataclass
class UefiVariableManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UefiVariableManager`.

        :param override_host_os: Test-time override.
        :param efivar_path: Path to ``efivar`` (Linux).
        :param nvram_path: Path to ``nvram`` (macOS).
        :param powershell_path: Path to PowerShell (Windows).
        :param chattr_path: Path to ``chattr`` (Linux immutable-flag
                            handling).
        :param efivars_dir: Path to the Linux efivars filesystem (used
                            for the ``chattr`` argv).
    """
    override_host_os: Optional[str] = None
    efivar_path: str = _DEFAULT_EFIVAR
    nvram_path: str = _DEFAULT_NVRAM
    powershell_path: str = _DEFAULT_POWERSHELL
    chattr_path: str = _DEFAULT_CHATTR
    efivars_dir: str = _DEFAULT_EFIVARS_DIR


@dataclass
class UefiVariable:
    """
        A read EFI NVRAM variable with raw bytes and attribute bitmap.

        :param name: Variable name.
        :param namespace: GUID-form namespace (lower-cased).
        :param value: Raw value bytes; ``None`` when only metadata is
                      available (e.g., from an enumeration call).
        :param attributes: Attribute bitmap; ``None`` when not surfaced.
        :param value_hex: Hex-encoded string form of the value when
                          present, for convenient comparison.
    """
    name: str
    namespace: str
    value: Optional[bytes] = None
    attributes: Optional[int] = None
    value_hex: Optional[str] = None


@dataclass
class UefiVariableInfo:
    """
        Listing-shape variant of :class:`UefiVariable` returned from
        ``list_variables``.  The shape matches :class:`UefiVariable` so
        callers can interchange them transparently.
    """
    name: str
    namespace: str
    value: Optional[bytes] = None
    attributes: Optional[int] = None
    value_hex: Optional[str] = None


@dataclass
class UefiVariableActionResult(FeatureActionResult):
    """
        Outcome of a variable write.

        :param name: The variable name targeted.
        :param namespace: The variable namespace targeted (GUID form).
        :param host_os: The host OS the action dispatched to.
    """
    name: Optional[str] = None
    namespace: Optional[str] = None
    host_os: Optional[str] = None


class UefiVariableQueryError(Exception):
    """
        Raised when a variable enumeration or read fails (other than a
        simple "variable does not exist").
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

        err_msg_lines = ["UEFI variable query failed."]
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


class UefiVariableNotFoundError(UefiVariableQueryError):
    """
        Raised by ``get_variable`` when the variable does not exist.
    """


class UefiVariableNamespaceError(ValueError):
    """
        Raised when a namespace is rejected by the current host OS.
    """


# ----------------------------------------------------------------------
# Namespace helpers
# ----------------------------------------------------------------------


def _looks_like_guid(value: str) -> bool:
    """
        Return ``True`` when ``value`` is a GUID-shape string.

        :param value: Candidate string.

        :returns: ``True`` when the string is 36 chars long with hyphens
                  in the canonical positions and only hex characters
                  elsewhere.
    """
    if len(value) != _GUID_LENGTH:
        rtnval = False
        return rtnval

    expected_hyphens = (8, 13, 18, 23)
    for pos in expected_hyphens:
        if value[pos] != "-":
            rtnval = False
            return rtnval

    for idx, ch in enumerate(value):
        if idx in expected_hyphens:
            continue
        if ch not in "0123456789abcdefABCDEF":
            rtnval = False
            return rtnval

    rtnval = True
    return rtnval


def normalise_namespace(host_os: str, namespace: str) -> str:
    """
        Normalise a namespace identifier to its canonical lower-cased
        GUID form, applying host-OS aliasing rules.

        :param host_os: Lower-cased host OS key.
        :param namespace: Raw namespace value passed by the coordinator.

        :returns: A lower-cased GUID string, or — for host-OS-owned
                  aliases on the owning host (``"Apple"`` /
                  ``"Common"`` on Darwin) — the alias itself unchanged.
        :raises UefiVariableNamespaceError: When the alias is reserved
                                            on a host OS other than the
                                            one that owns it.
        :raises ValueError: When the namespace is neither a known alias
                            nor a GUID-shape string.
    """
    if namespace is None or len(namespace) == 0:
        err_msg = "namespace must be a non-empty string"
        raise ValueError(err_msg)

    if namespace in _NAMESPACE_ALIAS_TO_GUID:
        rtnval = _NAMESPACE_ALIAS_TO_GUID[namespace]
        return rtnval

    if namespace in _HOST_OS_OWNED_ALIASES:
        owning = _HOST_OS_OWNED_ALIASES[namespace]
        if host_os != owning:
            err_msg_lines = [
                "Namespace alias is reserved on this host OS.",
            ]
            err_msg_lines.append("    NAMESPACE: {}".format(namespace))
            err_msg_lines.append("    OWNING_HOST_OS: {}".format(owning))
            err_msg_lines.append("    DISPATCHED_HOST_OS: {}".format(host_os))
            err_msg = os.linesep.join(err_msg_lines)
            raise UefiVariableNamespaceError(err_msg)
        rtnval = namespace
        return rtnval

    if _looks_like_guid(value=namespace):
        rtnval = namespace.lower()
        return rtnval

    accepted_aliases = sorted(_NAMESPACE_ALIAS_TO_GUID.keys()) + sorted(
        _HOST_OS_OWNED_ALIASES.keys(),
    )
    err_msg_lines = [
        "namespace is neither a recognised alias nor a GUID-shape string.",
    ]
    err_msg_lines.append("    PROVIDED: {!r}".format(namespace))
    err_msg_lines.append("    ACCEPTED_ALIASES: {}".format(accepted_aliases))
    err_msg = os.linesep.join(err_msg_lines)
    raise ValueError(err_msg)


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class UefiVariableManager(BaseFeatureManager):
    """
        Per-host-OS EFI NVRAM variable manager.  Reads and writes flow
        through host-OS-specific dispatch helpers; Linux writes wrap
        the ``efivar`` command in a ``chattr -i`` / ``chattr +i`` dance
        to handle the efivars-immutable flag.
    """

    result_class = UefiVariableActionResult
    status_class = UefiVariableActionStatus
    feature_name = "efi-variables"

    def __init__(
        self,
        runner: ICommandRunner,
        config: Optional[UefiVariableManagerConfig] = None,
    ) -> None:
        if config is None:
            config = UefiVariableManagerConfig()
        super().__init__(runner=runner, config=config)
        self._typed_config: UefiVariableManagerConfig = config
        return

    @property
    def typed_config(self) -> UefiVariableManagerConfig:
        """
            The active :class:`UefiVariableManagerConfig`.
        """
        return self._typed_config

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_variable(
        self,
        name: str,
        namespace: str,
    ) -> UefiVariable:
        """
            Read a single variable's bytes and attributes.

            :param name: Variable name.
            :param namespace: GUID or alias.

            :returns: A :class:`UefiVariable`.
            :raises UefiVariableNotFoundError: When the variable does
                                               not exist on the host.
            :raises UefiVariableQueryError: When the underlying tool
                                            fails for another reason.
            :raises UefiVariableNamespaceError: When the namespace is
                                                reserved on this host
                                                OS.
        """
        normalised = normalise_namespace(host_os=self._host_os, namespace=namespace)

        if self._host_os == HOST_OS_LINUX:
            rtnval = self._dispatch_linux_get_variable(
                name=name, namespace=normalised,
            )
        elif self._host_os == HOST_OS_DARWIN:
            rtnval = self._dispatch_darwin_get_variable(
                name=name, namespace=normalised,
            )
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = self._dispatch_windows_get_variable(
                name=name, namespace=normalised,
            )
        else:
            err_msg = "Unsupported host OS for get_variable: {}".format(
                self._host_os,
            )
            raise UefiVariableQueryError(message=err_msg)
        return rtnval

    def list_variables(
        self,
        namespace: Optional[str] = None,
    ) -> list[UefiVariableInfo]:
        """
            Enumerate variables, optionally filtered by namespace.

            :param namespace: Optional GUID or alias to filter on.

            :returns: One :class:`UefiVariableInfo` per matching
                      variable.
            :raises UefiVariableQueryError: When the underlying tool
                                            fails.
        """
        if namespace is None:
            normalised: Optional[str] = None
        else:
            normalised = normalise_namespace(
                host_os=self._host_os, namespace=namespace,
            )

        if self._host_os == HOST_OS_LINUX:
            all_entries = self._dispatch_linux_list_variables()
        elif self._host_os == HOST_OS_DARWIN:
            all_entries = self._dispatch_darwin_list_variables()
        elif self._host_os == HOST_OS_WINDOWS:
            all_entries = self._dispatch_windows_list_variables()
        else:
            err_msg = "Unsupported host OS for list_variables: {}".format(
                self._host_os,
            )
            raise UefiVariableQueryError(message=err_msg)

        if normalised is None:
            rtnval = all_entries
        else:
            rtnval = [e for e in all_entries if e.namespace == normalised]
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def set_variable(
        self,
        name: str,
        value: bytes,
        namespace: str,
        attributes: Optional[int] = None,
    ) -> UefiVariableActionResult:
        """
            Write a variable's value bytes (and optionally attribute
            bitmap).

            Idempotency: when the current value bytes and attributes
            equal the requested ones, returns ``SKIPPED``.

            :param name: Variable name.
            :param value: Raw value bytes.
            :param namespace: GUID or alias.
            :param attributes: Attribute bitmap; when ``None`` the
                               existing variable's bitmap is preserved
                               (or :data:`DEFAULT_ATTRIBUTES` is used
                               for a new variable).

            :returns: The :class:`UefiVariableActionResult`.
        """
        try:
            normalised = normalise_namespace(
                host_os=self._host_os, namespace=namespace,
            )
        except UefiVariableNamespaceError as exc:
            rtnval = self._wrap_result(
                status=self.status_class.FAILED,
                detail=str(exc),
                command=None,
                stderr="namespace_rejected_on_host_os_{}".format(self._host_os),
                name=name,
                namespace=namespace,
                host_os=self._host_os,
            )
            return rtnval

        if self._config.dry_run:
            command = self._build_dry_run_set_command(
                name=name,
                value=value,
                namespace=normalised,
                attributes=attributes,
            )
            dry_result = self._wrap_result(
                status=self.status_class.DRY_RUN,
                detail=name,
                command=command,
                name=name,
                namespace=normalised,
                host_os=self._host_os,
            )
            return dry_result

        # Idempotency probe + attribute preservation.
        existing = self._safe_get_variable(name=name, namespace=normalised)
        if existing is not None:
            current_value = existing.value or b""
            current_attrs = existing.attributes
            same_value = current_value == value
            if attributes is None:
                same_attrs = True
            else:
                same_attrs = current_attrs == attributes
            if same_value and same_attrs:
                skipped = self._wrap_result(
                    status=self.status_class.SKIPPED,
                    detail="value and attributes already match",
                    command=None,
                    name=name,
                    namespace=normalised,
                    host_os=self._host_os,
                )
                return skipped

        if attributes is None:
            if existing is not None and existing.attributes is not None:
                effective_attrs = existing.attributes
            elif existing is None and self._safe_probe_failed:
                # The probe raised something other than "not found".  Fail
                # closed rather than silently using the default bitmap.
                failed = self._wrap_result(
                    status=self.status_class.FAILED,
                    detail="attribute preservation read failed",
                    command=None,
                    stderr="attribute_preservation_read_failed",
                    name=name,
                    namespace=normalised,
                    host_os=self._host_os,
                )
                return failed
            else:
                effective_attrs = DEFAULT_ATTRIBUTES
        else:
            effective_attrs = attributes

        if self._host_os == HOST_OS_LINUX:
            rtnval = self._dispatch_linux_set_variable(
                name=name,
                value=value,
                namespace=normalised,
                attributes=effective_attrs,
            )
        elif self._host_os == HOST_OS_DARWIN:
            rtnval = self._dispatch_darwin_set_variable(
                name=name,
                value=value,
                namespace=normalised,
                attributes=effective_attrs,
            )
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = self._dispatch_windows_set_variable(
                name=name,
                value=value,
                namespace=normalised,
                attributes=effective_attrs,
            )
        else:
            rtnval = self._build_failed_unsupported(
                verb="set_variable",
                host_os=self._host_os,
                name=name,
                namespace=normalised,
                host_os_field=self._host_os,
            )
        return rtnval

    def delete_variable(
        self,
        name: str,
        namespace: str,
    ) -> UefiVariableActionResult:
        """
            Delete a variable.

            Idempotency: when the variable does not exist, returns
            ``SKIPPED``.

            :param name: Variable name.
            :param namespace: GUID or alias.

            :returns: The :class:`UefiVariableActionResult`.
        """
        try:
            normalised = normalise_namespace(
                host_os=self._host_os, namespace=namespace,
            )
        except UefiVariableNamespaceError as exc:
            rtnval = self._wrap_result(
                status=self.status_class.FAILED,
                detail=str(exc),
                command=None,
                stderr="namespace_rejected_on_host_os_{}".format(self._host_os),
                name=name,
                namespace=namespace,
                host_os=self._host_os,
            )
            return rtnval

        if self._config.dry_run:
            command = self._build_dry_run_delete_command(
                name=name, namespace=normalised,
            )
            rtnval = self._wrap_result(
                status=self.status_class.DRY_RUN,
                detail=name,
                command=command,
                name=name,
                namespace=normalised,
                host_os=self._host_os,
            )
            return rtnval

        existing = self._safe_get_variable(name=name, namespace=normalised)
        if existing is None and not self._safe_probe_failed:
            # ``not found`` short-circuits to SKIPPED.
            skipped = self._wrap_result(
                status=self.status_class.SKIPPED,
                detail="variable does not exist",
                command=None,
                name=name,
                namespace=normalised,
                host_os=self._host_os,
            )
            return skipped

        if self._host_os == HOST_OS_LINUX:
            rtnval = self._dispatch_linux_delete_variable(
                name=name, namespace=normalised,
            )
        elif self._host_os == HOST_OS_DARWIN:
            rtnval = self._dispatch_darwin_delete_variable(
                name=name, namespace=normalised,
            )
        elif self._host_os == HOST_OS_WINDOWS:
            rtnval = self._dispatch_windows_delete_variable(
                name=name, namespace=normalised,
            )
        else:
            rtnval = self._build_failed_unsupported(
                verb="delete_variable",
                host_os=self._host_os,
                name=name,
                namespace=normalised,
                host_os_field=self._host_os,
            )
        return rtnval

    # ------------------------------------------------------------------
    # Linux dispatch
    # ------------------------------------------------------------------

    def _dispatch_linux_get_variable(
        self,
        *,
        name: str,
        namespace: str,
    ) -> UefiVariable:
        ns_name = "{}-{}".format(namespace, name)
        command = [self._typed_config.efivar_path, "-n", ns_name, "-p"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            stderr_text = (run_result.stderr or "").lower()
            if "no such" in stderr_text or "not found" in stderr_text:
                raise UefiVariableNotFoundError(
                    command=command,
                    exit_code=run_result.exit_code,
                    stderr=run_result.stderr,
                    message="variable not found: {}".format(ns_name),
                )
            raise UefiVariableQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = self._parse_efivar_variable(
            text=run_result.stdout or "",
            name=name,
            namespace=namespace,
        )
        return rtnval

    def _dispatch_linux_list_variables(self) -> list[UefiVariableInfo]:
        command = [self._typed_config.efivar_path, "-l"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UefiVariableQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = self._parse_efivar_l(text=run_result.stdout or "")
        return rtnval

    def _dispatch_linux_set_variable(
        self,
        *,
        name: str,
        value: bytes,
        namespace: str,
        attributes: int,
    ) -> UefiVariableActionResult:
        ns_name = "{}-{}".format(namespace, name)
        efivars_path = "{}/{}-{}".format(
            self._typed_config.efivars_dir, name, namespace,
        )
        chattr_clear = [
            self._typed_config.chattr_path, "-i", efivars_path,
        ]
        chattr_set = [
            self._typed_config.chattr_path, "+i", efivars_path,
        ]
        # ``efivar -w`` reads bytes from a file path; the runner's
        # ``stdin`` argument is the closest portable channel to ferry
        # the bytes through, so we use ``--stdin`` here and let the
        # runner write the bytes to a temporary file behind the
        # scenes.  When that is not available (older runners), the
        # caller can pass a path themselves via the helper aspect.
        write_command = [
            self._typed_config.efivar_path,
            "-n", ns_name,
            "-w",
            "-a", str(attributes),
        ]

        # Step 1: clear immutable.
        clear_result = self._dispatch(
            command=chattr_clear,
            detail="clear immutable",
            operation="set_variable_chattr_clear",
            name=name,
            namespace=namespace,
            host_os=self._host_os,
        )
        if clear_result.status not in (
            self.status_class.SUCCESS,
            self.status_class.DRY_RUN,
        ):
            # Some kernels do not require the chattr dance; surface a
            # warning by continuing only when stderr indicates the
            # immutable bit was already clear.
            stderr_text = (clear_result.stderr or "").lower()
            if "operation not supported" not in stderr_text:
                clear_result.detail = "chattr -i preparatory step failed"
                return clear_result

        # Step 2: efivar -w (with bytes piped through stdin).
        hex_payload = value.hex()
        write_result = self._dispatch(
            command=write_command,
            detail=name,
            stdin=hex_payload,
            operation="set_variable",
            name=name,
            namespace=namespace,
            host_os=self._host_os,
        )

        if write_result.status == self.status_class.SUCCESS:
            # Step 3: re-set immutable.  Failure here does not undo the
            # successful write; surface a non-fatal note in the detail.
            reset_result = self._dispatch(
                command=chattr_set,
                detail="restore immutable",
                operation="set_variable_chattr_set",
                name=name,
                namespace=namespace,
                host_os=self._host_os,
            )
            if reset_result.status != self.status_class.SUCCESS:
                write_result.detail = (
                    "{}; chattr +i restore step did not succeed".format(
                        write_result.detail or name,
                    )
                )

        return write_result

    def _dispatch_linux_delete_variable(
        self,
        *,
        name: str,
        namespace: str,
    ) -> UefiVariableActionResult:
        ns_name = "{}-{}".format(namespace, name)
        efivars_path = "{}/{}-{}".format(
            self._typed_config.efivars_dir, name, namespace,
        )
        chattr_clear = [
            self._typed_config.chattr_path, "-i", efivars_path,
        ]
        delete_command = [
            self._typed_config.efivar_path, "-n", ns_name, "-D",
        ]

        clear_result = self._dispatch(
            command=chattr_clear,
            detail="clear immutable",
            operation="delete_variable_chattr_clear",
            name=name,
            namespace=namespace,
            host_os=self._host_os,
        )
        if clear_result.status not in (
            self.status_class.SUCCESS,
            self.status_class.DRY_RUN,
        ):
            stderr_text = (clear_result.stderr or "").lower()
            if "operation not supported" not in stderr_text:
                clear_result.detail = "chattr -i preparatory step failed"
                return clear_result

        rtnval = self._dispatch(
            command=delete_command,
            detail=name,
            operation="delete_variable",
            name=name,
            namespace=namespace,
            host_os=self._host_os,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Darwin dispatch
    # ------------------------------------------------------------------

    def _dispatch_darwin_get_variable(
        self,
        *,
        name: str,
        namespace: str,
    ) -> UefiVariable:
        ns_name = "{}:{}".format(namespace, name)
        command = [self._typed_config.nvram_path, ns_name]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            stderr_text = (run_result.stderr or "").lower()
            if "not found" in stderr_text or "no such" in stderr_text:
                raise UefiVariableNotFoundError(
                    command=command,
                    exit_code=run_result.exit_code,
                    stderr=run_result.stderr,
                    message="variable not found: {}".format(ns_name),
                )
            raise UefiVariableQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = self._parse_nvram_single_variable(
            text=run_result.stdout or "",
            name=name,
            namespace=namespace,
        )
        return rtnval

    def _dispatch_darwin_list_variables(self) -> list[UefiVariableInfo]:
        command = [self._typed_config.nvram_path, "-p"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UefiVariableQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = self._parse_nvram_listing(text=run_result.stdout or "")
        return rtnval

    def _dispatch_darwin_set_variable(
        self,
        *,
        name: str,
        value: bytes,
        namespace: str,
        attributes: int,
    ) -> UefiVariableActionResult:
        encoded = self._encode_nvram_value(value=value)
        argument = "{}:{}={}".format(namespace, name, encoded)
        command = [self._typed_config.nvram_path, argument]

        rtnval = self._dispatch(
            command=command,
            detail=name,
            operation="set_variable",
            name=name,
            namespace=namespace,
            host_os=self._host_os,
        )
        return rtnval

    def _dispatch_darwin_delete_variable(
        self,
        *,
        name: str,
        namespace: str,
    ) -> UefiVariableActionResult:
        argument = "{}:{}".format(namespace, name)
        command = [self._typed_config.nvram_path, "-d", argument]
        rtnval = self._dispatch(
            command=command,
            detail=name,
            operation="delete_variable",
            name=name,
            namespace=namespace,
            host_os=self._host_os,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Windows dispatch
    # ------------------------------------------------------------------

    def _dispatch_windows_get_variable(
        self,
        *,
        name: str,
        namespace: str,
    ) -> UefiVariable:
        ps_command = "[UEFI]::GetVariable('{}', '{}')".format(namespace, name)
        command = [
            self._typed_config.powershell_path,
            "-NoProfile",
            "-Command",
            ps_command,
        ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            stderr_text = (run_result.stderr or "").lower()
            if "not found" in stderr_text or "does not exist" in stderr_text:
                raise UefiVariableNotFoundError(
                    command=command,
                    exit_code=run_result.exit_code,
                    stderr=run_result.stderr,
                    message="variable not found: {}/{}".format(namespace, name),
                )
            raise UefiVariableQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = self._parse_powershell_uefi_variable(
            text=run_result.stdout or "",
            name=name,
            namespace=namespace,
        )
        return rtnval

    def _dispatch_windows_list_variables(self) -> list[UefiVariableInfo]:
        ps_command = "[UEFI]::EnumerateVariables() | ConvertTo-Json"
        command = [
            self._typed_config.powershell_path,
            "-NoProfile",
            "-Command",
            ps_command,
        ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UefiVariableQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = self._parse_powershell_uefi_listing(text=run_result.stdout or "")
        return rtnval

    def _dispatch_windows_set_variable(
        self,
        *,
        name: str,
        value: bytes,
        namespace: str,
        attributes: int,
    ) -> UefiVariableActionResult:
        byte_array_literal = ",".join(str(b) for b in value)
        ps_command = (
            "[UEFI]::SetVariable('{ns}', '{nm}', "
            "[byte[]]@({bytes}), {attrs})"
        ).format(
            ns=namespace,
            nm=name,
            bytes=byte_array_literal,
            attrs=attributes,
        )
        command = [
            self._typed_config.powershell_path,
            "-NoProfile",
            "-Command",
            ps_command,
        ]
        rtnval = self._dispatch(
            command=command,
            detail=name,
            operation="set_variable",
            name=name,
            namespace=namespace,
            host_os=self._host_os,
        )
        return rtnval

    def _dispatch_windows_delete_variable(
        self,
        *,
        name: str,
        namespace: str,
    ) -> UefiVariableActionResult:
        ps_command = "[UEFI]::DeleteVariable('{}', '{}')".format(namespace, name)
        command = [
            self._typed_config.powershell_path,
            "-NoProfile",
            "-Command",
            ps_command,
        ]
        rtnval = self._dispatch(
            command=command,
            detail=name,
            operation="delete_variable",
            name=name,
            namespace=namespace,
            host_os=self._host_os,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _safe_get_variable(
        self,
        *,
        name: str,
        namespace: str,
    ) -> Optional[UefiVariable]:
        """
            Try to read a variable for an idempotency / preservation
            probe.  ``UefiVariableNotFoundError`` is treated as
            ``None`` and ``self._safe_probe_failed`` is set to
            ``False``.  Any other failure leaves
            ``self._safe_probe_failed=True`` so the calling write can
            decide to fail closed.
        """
        try:
            rtnval = self.get_variable(name=name, namespace=namespace)
        except UefiVariableNotFoundError:
            self._safe_probe_failed = False
            rtnval = None
            return rtnval
        except UefiVariableQueryError:
            self._safe_probe_failed = True
            rtnval = None
            return rtnval

        self._safe_probe_failed = False
        return rtnval

    @property
    def _safe_probe_failed(self) -> bool:
        rtnval = getattr(self, "_safe_probe_failed_value", False)
        return rtnval

    @_safe_probe_failed.setter
    def _safe_probe_failed(self, value: bool) -> None:
        self._safe_probe_failed_value = value
        return

    def _build_dry_run_set_command(
        self,
        *,
        name: str,
        value: bytes,
        namespace: str,
        attributes: Optional[int],
    ) -> Optional[list[str]]:
        if attributes is None:
            attrs_resolved = DEFAULT_ATTRIBUTES
        else:
            attrs_resolved = attributes

        if self._host_os == HOST_OS_LINUX:
            ns_name = "{}-{}".format(namespace, name)
            rtnval = [
                self._typed_config.efivar_path,
                "-n", ns_name,
                "-w",
                "-a", str(attrs_resolved),
            ]
        elif self._host_os == HOST_OS_DARWIN:
            argument = "{}:{}={}".format(
                namespace, name, self._encode_nvram_value(value=value),
            )
            rtnval = [self._typed_config.nvram_path, argument]
        elif self._host_os == HOST_OS_WINDOWS:
            byte_array = ",".join(str(b) for b in value)
            ps_command = (
                "[UEFI]::SetVariable('{ns}', '{nm}', "
                "[byte[]]@({bytes}), {attrs})"
            ).format(
                ns=namespace, nm=name, bytes=byte_array, attrs=attrs_resolved,
            )
            rtnval = [
                self._typed_config.powershell_path,
                "-NoProfile",
                "-Command",
                ps_command,
            ]
        else:
            rtnval = None
        return rtnval

    def _build_dry_run_delete_command(
        self,
        *,
        name: str,
        namespace: str,
    ) -> Optional[list[str]]:
        if self._host_os == HOST_OS_LINUX:
            ns_name = "{}-{}".format(namespace, name)
            rtnval = [
                self._typed_config.efivar_path,
                "-n", ns_name,
                "-D",
            ]
        elif self._host_os == HOST_OS_DARWIN:
            argument = "{}:{}".format(namespace, name)
            rtnval = [self._typed_config.nvram_path, "-d", argument]
        elif self._host_os == HOST_OS_WINDOWS:
            ps_command = "[UEFI]::DeleteVariable('{}', '{}')".format(
                namespace, name,
            )
            rtnval = [
                self._typed_config.powershell_path,
                "-NoProfile",
                "-Command",
                ps_command,
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
    ) -> UefiVariableActionResult:
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

    def _encode_nvram_value(self, *, value: bytes) -> str:
        """
            Encode raw bytes into ``nvram``'s ``%XX`` escape form for
            assignment.  Printable ASCII passes through untouched;
            anything else is hex-escaped.
        """
        chunks: list[str] = []
        for byte in value:
            if 32 <= byte < 127 and chr(byte) not in ('=', ':', '%', ' '):
                chunks.append(chr(byte))
            else:
                chunks.append("%{:02x}".format(byte))
        rtnval = "".join(chunks)
        return rtnval

    def _decode_nvram_value(self, *, encoded: str) -> bytes:
        chunks: list[int] = []
        idx = 0
        while idx < len(encoded):
            ch = encoded[idx]
            if ch == "%" and idx + 2 < len(encoded):
                hex_byte = encoded[idx + 1: idx + 3]
                try:
                    chunks.append(int(hex_byte, 16))
                except ValueError:
                    chunks.append(ord(ch))
                    idx += 1
                    continue
                idx += 3
                continue
            chunks.append(ord(ch))
            idx += 1
        rtnval = bytes(chunks)
        return rtnval

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    def _parse_efivar_variable(
        self,
        *,
        text: str,
        name: str,
        namespace: str,
    ) -> UefiVariable:
        attributes = 0
        in_attributes = False
        in_value = False
        value_bytes: list[int] = []

        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if stripped.startswith("Attributes:"):
                in_attributes = True
                in_value = False
                continue
            if stripped.startswith("Value:"):
                in_attributes = False
                in_value = True
                continue
            if stripped.startswith("GUID:") or stripped.startswith("Name:"):
                in_attributes = False
                in_value = False
                continue

            if in_attributes:
                lowered = stripped.lower()
                bit = _ATTRIBUTE_NAME_TO_BIT.get(lowered)
                if bit is not None:
                    attributes = attributes | bit
                continue

            if in_value:
                # Hex dump format: "00000000  01 00 ...  |....|"
                # Split by '|' to drop the ASCII column, then split the
                # remaining content into hex tokens.
                head = stripped.split("|", 1)[0]
                tokens = head.split()
                # Skip the offset column (first token) when present.
                if len(tokens) > 0 and len(tokens[0]) >= 6 and all(
                    c in "0123456789abcdef" for c in tokens[0].lower()
                ):
                    tokens = tokens[1:]
                for token in tokens:
                    if len(token) == 2 and all(
                        c in "0123456789abcdefABCDEF" for c in token
                    ):
                        value_bytes.append(int(token, 16))

        value = bytes(value_bytes)
        rtnval = UefiVariable(
            name=name,
            namespace=namespace,
            value=value,
            attributes=attributes,
            value_hex=value.hex(),
        )
        return rtnval

    def _parse_efivar_l(self, *, text: str) -> list[UefiVariableInfo]:
        rtnval: list[UefiVariableInfo] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if len(line) < _GUID_LENGTH + 2:
                continue
            guid_part = line[:_GUID_LENGTH]
            if not _looks_like_guid(value=guid_part):
                continue
            if line[_GUID_LENGTH] != "-":
                continue
            name = line[_GUID_LENGTH + 1:]
            entry = UefiVariableInfo(
                name=name,
                namespace=guid_part.lower(),
                value=None,
                attributes=None,
                value_hex=None,
            )
            rtnval.append(entry)
        return rtnval

    def _parse_nvram_single_variable(
        self,
        *,
        text: str,
        name: str,
        namespace: str,
    ) -> UefiVariable:
        # ``nvram <ns>:<name>`` emits "<ns>:<name>\t<value>".
        for raw_line in text.splitlines():
            line = raw_line.rstrip("\n")
            if "\t" in line:
                _ns_name, _, raw_value = line.partition("\t")
            elif "\t" not in line and len(line) > 0:
                parts = line.split(None, 1)
                if len(parts) == 2:
                    raw_value = parts[1]
                else:
                    continue
            else:
                continue

            decoded = self._decode_nvram_value(encoded=raw_value)
            rtnval = UefiVariable(
                name=name,
                namespace=namespace,
                value=decoded,
                attributes=None,
                value_hex=decoded.hex(),
            )
            return rtnval

        rtnval = UefiVariable(
            name=name,
            namespace=namespace,
            value=b"",
            attributes=None,
            value_hex="",
        )
        return rtnval

    def _parse_nvram_listing(
        self,
        *,
        text: str,
    ) -> list[UefiVariableInfo]:
        rtnval: list[UefiVariableInfo] = []
        for raw_line in text.splitlines():
            line = raw_line.rstrip("\n")
            if len(line.strip()) == 0:
                continue

            if "\t" in line:
                left, _, raw_value = line.partition("\t")
            else:
                parts = line.split(None, 1)
                if len(parts) != 2:
                    continue
                left = parts[0]
                raw_value = parts[1]

            if ":" in left:
                ns, _, nm = left.partition(":")
                namespace = ns.strip()
                name = nm.strip()
            else:
                # Bare name -> Apple's ``Common`` namespace by convention.
                namespace = "Common"
                name = left.strip()

            decoded = self._decode_nvram_value(encoded=raw_value)
            entry = UefiVariableInfo(
                name=name,
                namespace=namespace,
                value=decoded,
                attributes=None,
                value_hex=decoded.hex(),
            )
            rtnval.append(entry)
        return rtnval

    def _parse_powershell_uefi_variable(
        self,
        *,
        text: str,
        name: str,
        namespace: str,
    ) -> UefiVariable:
        # The PowerShell ``[UEFI]::GetVariable`` cmdlet emits a byte
        # array; ``ConvertTo-Json`` is not always involved here so we
        # accept a flexible "comma-, space-, or whitespace-separated
        # decimal byte" form alongside a JSON array form.
        value_bytes: list[int] = []
        attributes: Optional[int] = None
        capture_started = False

        for raw_line in text.splitlines():
            stripped = raw_line.strip().rstrip(",")
            if len(stripped) == 0:
                continue

            if "attributes" in stripped.lower() and ":" in stripped:
                _, _, value = stripped.partition(":")
                value_clean = value.strip().strip(",").strip()
                try:
                    attributes = int(value_clean)
                except ValueError:
                    attributes = None
                continue

            tokens = stripped.replace("[", " ").replace("]", " ").replace(",", " ").split()
            for token in tokens:
                if not token.isdigit():
                    continue
                byte_value = int(token)
                if 0 <= byte_value <= 255:
                    value_bytes.append(byte_value)
                    capture_started = True

        # Some terse outputs wrap each byte on its own line and never
        # capture; we conservatively keep what we got.
        if not capture_started:
            value = b""
        else:
            value = bytes(value_bytes)

        rtnval = UefiVariable(
            name=name,
            namespace=namespace,
            value=value,
            attributes=attributes,
            value_hex=value.hex(),
        )
        return rtnval

    def _parse_powershell_uefi_listing(
        self,
        *,
        text: str,
    ) -> list[UefiVariableInfo]:
        rtnval: list[UefiVariableInfo] = []

        # Best-effort line-based parser for the JSON array form.  We do
        # not pull in ``json`` because the cmdlet output is not
        # guaranteed to be a single tidy document on every Windows
        # locale; this approach picks out the per-record fields by name.
        current_namespace: Optional[str] = None
        current_name: Optional[str] = None
        current_attributes: Optional[int] = None

        def flush() -> None:
            if current_namespace is not None and current_name is not None:
                rtnval.append(UefiVariableInfo(
                    name=current_name,
                    namespace=current_namespace,
                    value=None,
                    attributes=current_attributes,
                    value_hex=None,
                ))
            return

        for raw_line in text.splitlines():
            stripped = raw_line.strip().rstrip(",")
            if stripped in ("{", "},", "}"):
                if stripped in ("},", "}"):
                    flush()
                    current_namespace = None
                    current_name = None
                    current_attributes = None
                continue

            if ":" not in stripped:
                continue

            key, _, value_text = stripped.partition(":")
            key_stripped = key.strip().strip('"').lower()
            value_clean = value_text.strip().strip(",").strip().strip('"')

            if key_stripped == "namespace":
                current_namespace = value_clean
            elif key_stripped == "name":
                current_name = value_clean
            elif key_stripped == "attributes":
                try:
                    current_attributes = int(value_clean)
                except ValueError:
                    current_attributes = None

        # Flush a trailing record.
        flush()
        return rtnval
