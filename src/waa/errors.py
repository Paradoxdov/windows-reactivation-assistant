"""Domain error codes for the Windows Reactivation Assistant."""


class WaaError(Exception):
    """Base error carrying a stable machine-readable code."""

    code = "UNEXPECTED_ERROR"

    def __init__(self, message="", code=None, hint=""):
        self.code = code or self.__class__.code
        self.hint = hint
        super().__init__(message or self.code)

    def __str__(self):
        base = super().__str__()
        return base if base else self.code


class EdgeNotFound(WaaError):
    code = "EDGE_NOT_FOUND"


class EdgeLaunchFailed(WaaError):
    code = "EDGE_LAUNCH_FAILED"


class BrowserConnectionFailed(WaaError):
    code = "BROWSER_CONNECTION_FAILED"


class WindowsInfoUnavailable(WaaError):
    code = "WINDOWS_INFO_UNAVAILABLE"


class InstallationIdNotAvailable(WaaError):
    code = "INSTALLATION_ID_NOT_AVAILABLE"


class MicrosoftLoginRequired(WaaError):
    code = "MICROSOFT_LOGIN_REQUIRED"


class HumanVerificationRequired(WaaError):
    code = "HUMAN_VERIFICATION_REQUIRED"


class PortalTimeout(WaaError):
    code = "PORTAL_TIMEOUT"


class PortalLayoutChanged(WaaError):
    code = "PORTAL_LAYOUT_CHANGED"


class IidFieldNotFound(WaaError):
    code = "IID_FIELD_NOT_FOUND"


class IidInsertFailed(WaaError):
    code = "IID_INSERT_FAILED"


class SubmitButtonNotFound(WaaError):
    code = "SUBMIT_BUTTON_NOT_FOUND"


class UnsafeProfileDirectory(WaaError):
    """Raised when a configured profile directory is not owned by the app."""

    code = "UNSAFE_PROFILE_DIRECTORY"


class SafetyViolation(WaaError):
    """Raised when code tries to cross the hard safe-stop boundary."""

    code = "SAFETY_BOUNDARY_VIOLATION"
