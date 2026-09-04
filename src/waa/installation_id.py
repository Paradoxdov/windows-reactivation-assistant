"""Read-only retrieval of the Windows Offline Installation ID (IID).

The IID is read from the SoftwareLicensingProduct.OfflineInstallationId
property. This is the same value `slmgr /dti` prints, but reading the WMI
property does not invoke any method and does not change licensing state.

The IID is kept in memory only. It is never written to a log file; the logger
records "Installation ID: RECEIVED" instead.
"""

from . import _ps
from .errors import InstallationIdNotAvailable
from .windows_license import WINDOWS_APP_ID

GROUP_SIZE = 7
GROUP_COUNT = 9
EXPECTED_DIGITS = 63

_QUERY = r"""
$ErrorActionPreference = 'Stop'
$lic = Get-CimInstance -ClassName SoftwareLicensingProduct -Filter "ApplicationID='%APPID%' AND PartialProductKey IS NOT NULL" |
       Select-Object -First 1
if (-not $lic) { @{ Ok = $false; Reason = 'NO_LICENSED_PRODUCT' } | ConvertTo-Json -Compress; exit }
$iid = $lic.OfflineInstallationId
if ([string]::IsNullOrWhiteSpace($iid)) { @{ Ok = $false; Reason = 'EMPTY_IID' } | ConvertTo-Json -Compress; exit }
@{ Ok = $true; Iid = $iid } | ConvertTo-Json -Compress
""".replace("%APPID%", WINDOWS_APP_ID)


class InstallationId(object):
    """Holds the IID. Deliberately hides its own value from repr/str."""

    def __init__(self, raw):
        self.digits = "".join(ch for ch in str(raw) if ch.isdigit())
        if not self.digits:
            raise InstallationIdNotAvailable("Installation ID contained no digits.")

    @property
    def group_size(self):
        """Digits per block - the portal asks whether it is 6 or 7."""
        total = len(self.digits)
        if total % GROUP_COUNT == 0:
            return total // GROUP_COUNT
        return GROUP_SIZE

    @property
    def groups(self):
        size = self.group_size
        return [self.digits[i:i + size]
                for i in range(0, len(self.digits), size)]

    @property
    def dashed(self):
        return "-".join(self.groups)

    @property
    def looks_standard(self):
        return len(self.digits) == EXPECTED_DIGITS

    @property
    def masked(self):
        """Safe for console/log output: shows shape, never the value."""
        return "%d digits / %d blocks of %d (value withheld)" % (
            len(self.digits), len(self.groups), self.group_size)

    def __repr__(self):
        return "<InstallationId %s>" % self.masked

    __str__ = __repr__


def obtain(logger):
    """Fetch the current machine's Installation ID. Read-only."""
    logger.debug("Reading SoftwareLicensingProduct.OfflineInstallationId (read-only property)")
    try:
        data = _ps.run_json(_QUERY)
    except Exception as exc:
        logger.exception("Installation ID query failed")
        raise InstallationIdNotAvailable(
            "Could not read the Offline Installation ID: %s" % exc)

    if not data.get("Ok"):
        reason = data.get("Reason", "UNKNOWN")
        raise InstallationIdNotAvailable(
            "Windows did not provide an Offline Installation ID (%s)." % reason,
            hint="Offline activation data is only available when a product key is installed.",
        )

    iid = InstallationId(data["Iid"])
    if not iid.looks_standard:
        logger.warn("Installation ID has an unusual length (%d digits, expected %d)."
                    % (len(iid.digits), EXPECTED_DIGITS))
    logger.info("Installation ID: RECEIVED (%s)" % iid.masked)
    return iid
