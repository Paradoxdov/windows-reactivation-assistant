"""Read-only detection of Windows edition, architecture and license state.

Every query in this module is READ-ONLY. It uses CIM/WMI properties only and
never calls a method that could change licensing state (no slmgr /upk, /cpky,
/ato, /atp, no product key installation).
"""

from . import _ps
from .errors import WindowsInfoUnavailable

# ApplicationId of the Windows product family in SoftwareLicensingProduct.
WINDOWS_APP_ID = "55c92734-d682-4d71-983e-d6ec3f16059f"

LICENSE_STATUS = {
    0: "UNLICENSED",
    1: "ACTIVATED",
    2: "OOB_GRACE",
    3: "OOT_GRACE",
    4: "NON_GENUINE_GRACE",
    5: "NOTIFICATION",
    6: "EXTENDED_GRACE",
}

_QUERY = r"""
$ErrorActionPreference = 'Stop'
$os  = Get-CimInstance -ClassName Win32_OperatingSystem
$cv  = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -ErrorAction SilentlyContinue
$lic = Get-CimInstance -ClassName SoftwareLicensingProduct -Filter "ApplicationID='%APPID%' AND PartialProductKey IS NOT NULL" |
       Select-Object -First 1
$result = [ordered]@{
  Caption          = $os.Caption
  Version          = $os.Version
  BuildNumber      = $os.BuildNumber
  OSArchitecture   = $os.OSArchitecture
  DisplayVersion   = $(if ($cv) { $cv.DisplayVersion } else { $null })
  EditionID        = $(if ($cv) { $cv.EditionID } else { $null })
  LicenseFound     = [bool]$lic
  LicenseName      = $(if ($lic) { $lic.Name } else { $null })
  LicenseDesc      = $(if ($lic) { $lic.Description } else { $null })
  LicenseStatus    = $(if ($lic) { [int]$lic.LicenseStatus } else { -1 })
  PartialKey       = $(if ($lic) { $lic.PartialProductKey } else { $null })
  KeyChannel       = $(if ($lic) { $lic.ProductKeyChannel } else { $null })
  GraceMinutes     = $(if ($lic) { [int]$lic.GracePeriodRemaining } else { 0 })
  ProductId        = $(if ($lic) { $lic.ID } else { $null })
}
$result | ConvertTo-Json -Compress -Depth 3
""".replace("%APPID%", WINDOWS_APP_ID)


def classify_channel(channel):
    """Map ProductKeyChannel to a coarse, technician-friendly bucket."""
    if not channel:
        return "UNKNOWN"
    value = str(channel).upper()
    if value.startswith("OEM"):
        return "OEM"
    if value.startswith("VOLUME"):
        return "VOLUME"
    if "RETAIL" in value:
        return "RETAIL"
    return value


class WindowsLicense(object):
    """Snapshot of the Windows install and its licensing state."""

    def __init__(self, data):
        self._raw = data
        self.caption = (data.get("Caption") or "").strip()
        self.version = data.get("Version") or ""
        self.build = str(data.get("BuildNumber") or "")
        self.display_version = data.get("DisplayVersion") or ""
        self.edition_id = data.get("EditionID") or ""
        self.architecture = self._normalise_arch(data.get("OSArchitecture"))
        self.license_found = bool(data.get("LicenseFound"))
        self.license_name = data.get("LicenseName") or ""
        self.license_description = data.get("LicenseDesc") or ""
        self.status_code = int(data.get("LicenseStatus", -1))
        self.status = LICENSE_STATUS.get(self.status_code, "UNKNOWN")
        self.partial_key = (data.get("PartialKey") or "").strip()
        self.key_channel_raw = data.get("KeyChannel") or ""
        self.channel = classify_channel(self.key_channel_raw)
        self.grace_minutes = int(data.get("GraceMinutes") or 0)
        self.product_id = data.get("ProductId") or ""

    @staticmethod
    def _normalise_arch(value):
        text = (value or "").lower()
        if "64" in text:
            return "x64"
        if "arm" in text:
            return "ARM64"
        if "32" in text:
            return "x86"
        return value or "UNKNOWN"

    @property
    def product_family(self):
        """Windows 10 vs Windows 11 - build number is the reliable signal."""
        try:
            build = int(self.build)
        except (TypeError, ValueError):
            build = 0
        if build >= 22000:
            return "Windows 11"
        if build >= 10240:
            return "Windows 10"
        return self.caption or "Unknown Windows"

    @property
    def edition(self):
        caption = self.caption.replace("Microsoft ", "").strip()
        return caption or self.edition_id or "Unknown edition"

    @property
    def is_activated(self):
        return self.status_code == 1

    def summary_lines(self):
        return [
            ("Windows", "%s (build %s%s)" % (
                self.edition, self.build,
                ", %s" % self.display_version if self.display_version else "")),
            ("Architecture", self.architecture),
            ("Channel", "%s (%s)" % (self.channel, self.key_channel_raw)
             if self.key_channel_raw else self.channel),
            ("Partial Key", "PRESENT (value withheld)" if self.partial_key else "UNKNOWN"),
            ("Activation Status", self.status),
        ]


def detect(logger):
    """Collect the Windows/licensing snapshot. Read-only."""
    logger.debug("Querying Win32_OperatingSystem and SoftwareLicensingProduct (read-only)")
    try:
        data = _ps.run_json(_QUERY)
    except Exception as exc:
        logger.exception("Windows license query failed")
        raise WindowsInfoUnavailable(
            "Could not read Windows licensing information: %s" % exc,
            hint="Run the assistant on the machine being serviced; WMI must be available.",
        )
    license_info = WindowsLicense(data)
    if not license_info.license_found:
        raise WindowsInfoUnavailable(
            "No Windows license entry with an installed product key was found.",
            hint="A product key must be installed before an Installation ID exists.",
        )
    logger.debug("Detected %s / channel=%s / status=%s" % (
        license_info.product_family, license_info.key_channel_raw, license_info.status))
    return license_info
