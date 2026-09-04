"""Portable configuration: a single optional config.json next to the EXE.

The assistant does not create registry settings. Its optional cleanup can
remove current-user shell-history entries that match its dedicated Edge
profile; runtime configuration itself remains local to the program directory.
"""

import json
import os
import sys

from .errors import UnsafeProfileDirectory

CONFIG_NAME = "config.json"
PROFILE_MARKER_NAME = ".windows-reactivation-assistant-profile"
PROFILE_MARKER_CONTENT = "Portable Windows Reactivation Assistant profile\n"
LEGACY_PORT_FILE_NAME = "waa-devtools-port"
LOCAL_TEMP_NAME = "Temp"

DEFAULTS = {
    "edge_path": "",              # override msedge.exe location if auto-detect fails
    "profile_dir": "BrowserProfile",
    "keep_browser_open": True,    # leave Edge open at the safe stop for inspection
    # The tool is shared between technicians, so the Microsoft sign-in session
    # must not travel on the USB stick. The profile is erased once the operator
    # is done looking at the page.
    "clear_profile_on_exit": True,
    "step_timeout": 60,           # seconds for one portal transition
    "human_timeout": 600,         # seconds to wait for sign-in / CAPTCHA
    "total_timeout": 1200,        # seconds for the whole portal workflow
    "unknown_page_retries": 8,    # settle attempts before PORTAL_LAYOUT_CHANGED
    "verbose": False,
}


def program_dir():
    """The directory the portable program lives in (EXE dir when frozen)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    # Running from source: waa/config.py -> src -> project root.
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _real_path(path):
    return os.path.realpath(os.path.abspath(path))


def validate_program_data_dir(path):
    """Return a canonical path that is strictly below program_dir()."""
    lexical_base = os.path.abspath(program_dir())
    base = _real_path(lexical_base)
    raw = str(path or "")
    lexical_candidate = os.path.abspath(
        raw if os.path.isabs(raw) else os.path.join(lexical_base, raw))
    candidate = _real_path(lexical_candidate)
    try:
        lexical_inside = os.path.commonpath((os.path.normcase(lexical_base),
                                             os.path.normcase(lexical_candidate)))
        relative = os.path.relpath(lexical_candidate, lexical_base)
        expected = os.path.abspath(os.path.join(base, relative))
        real_inside = os.path.commonpath((os.path.normcase(base),
                                          os.path.normcase(candidate)))
    except ValueError:
        lexical_inside = real_inside = expected = ""
    if (lexical_inside != os.path.normcase(lexical_base)
            or os.path.normcase(lexical_candidate) == os.path.normcase(lexical_base)
            or real_inside != os.path.normcase(base)
            or os.path.normcase(candidate) != os.path.normcase(expected)):
        raise UnsafeProfileDirectory(
            "Managed data must stay in a non-redirected directory inside the "
            "program directory.",
            hint='Use "BrowserProfile" or another empty local subdirectory.',
        )
    return candidate


def validate_profile_dir(profile_dir):
    """Return a canonical profile path outside the reserved scratch tree."""
    profile_dir = validate_program_data_dir(profile_dir)
    if _path_is_under_local_temp(profile_dir):
        raise UnsafeProfileDirectory(
            "profile_dir cannot be Temp or a directory below Temp.",
            hint='Use "BrowserProfile" or another local directory outside Temp.',
        )
    return profile_dir


def _path_is_under_local_temp(profile_dir):
    temp_root = os.path.normcase(os.path.abspath(
        os.path.join(_real_path(program_dir()), LOCAL_TEMP_NAME)))
    try:
        return os.path.commonpath(
            (temp_root, os.path.normcase(profile_dir))) == temp_root
    except ValueError:
        return False


def profile_uses_local_temp(profile_dir):
    """Whether a contained configured profile overlaps the reserved Temp tree."""
    return _path_is_under_local_temp(validate_program_data_dir(profile_dir))


def _profile_marker(profile_dir):
    return os.path.join(profile_dir, PROFILE_MARKER_NAME)


def _has_valid_profile_marker(profile_dir):
    try:
        with open(_profile_marker(profile_dir), "r", encoding="ascii") as handle:
            return handle.read(len(PROFILE_MARKER_CONTENT) + 1) == PROFILE_MARKER_CONTENT
    except (OSError, UnicodeError):
        return False


def _looks_like_legacy_profile(profile_dir):
    """Recognise a profile made by pre-marker versions of this application."""
    port_file = os.path.join(profile_dir, LEGACY_PORT_FILE_NAME)
    try:
        with open(port_file, "r", encoding="ascii") as handle:
            port = int(handle.readline(8).strip())
        if 1 <= port <= 65535:
            return True
    except (OSError, ValueError):
        pass

    default_path = _real_path(os.path.join(program_dir(), DEFAULTS["profile_dir"]))
    if os.path.normcase(profile_dir) != os.path.normcase(default_path):
        return False
    return (os.path.isfile(os.path.join(profile_dir, "Local State"))
            and os.path.isfile(os.path.join(
                profile_dir, "Default", "Preferences")))


def claim_profile_dir(profile_dir):
    """Create or claim a safe profile directory before giving it to Edge."""
    profile_dir = validate_profile_dir(profile_dir)
    if os.path.exists(profile_dir) and not os.path.isdir(profile_dir):
        raise UnsafeProfileDirectory("profile_dir exists but is not a directory.")
    if not os.path.exists(profile_dir):
        os.makedirs(profile_dir)

    marker = _profile_marker(profile_dir)
    if _has_valid_profile_marker(profile_dir):
        return profile_dir

    try:
        with os.scandir(profile_dir) as entries:
            is_empty = next(entries, None) is None
    except OSError as exc:
        raise UnsafeProfileDirectory(
            "Could not inspect profile_dir: %s" % exc) from exc

    if not is_empty and not _looks_like_legacy_profile(profile_dir):
        raise UnsafeProfileDirectory(
            "Refusing to use a non-empty directory that is not an assistant profile.",
            hint='Use an empty local directory or the default "BrowserProfile".',
        )
    try:
        with open(marker, "w", encoding="ascii") as handle:
            handle.write(PROFILE_MARKER_CONTENT)
    except OSError as exc:
        raise UnsafeProfileDirectory(
            "Could not mark profile_dir as owned by the assistant: %s" % exc) from exc
    return profile_dir


def require_owned_profile_dir(profile_dir):
    """Validate ownership before recursive deletion of a browser profile."""
    profile_dir = validate_profile_dir(profile_dir)
    if not os.path.exists(profile_dir):
        return profile_dir
    if not os.path.isdir(profile_dir):
        raise UnsafeProfileDirectory("profile_dir exists but is not a directory.")
    if (_has_valid_profile_marker(profile_dir)
            or _looks_like_legacy_profile(profile_dir)):
        return profile_dir
    raise UnsafeProfileDirectory(
        "Refusing to delete a directory that is not owned by the assistant.",
        hint="Remove it manually only after checking its contents.",
    )


def local_temp_dir(create=False):
    """Scratch directory inside the program folder.

    Edge is pointed here through TEMP/TMP so that its own scratch folders
    (Importer_*, BITS_*, scoped_dir*) land next to the program instead of
    littering %TEMP% on the serviced PC.
    """
    path = os.path.join(program_dir(), LOCAL_TEMP_NAME)
    if create:
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            return None
    return path


class Config(object):
    def __init__(self, data, path):
        self.path = path
        for key, default in DEFAULTS.items():
            setattr(self, key, data.get(key, default))
        base = program_dir()
        if not os.path.isabs(self.profile_dir):
            self.profile_dir = os.path.join(base, self.profile_dir)

    def as_dict(self):
        return {key: getattr(self, key) for key in DEFAULTS}


def load(logger=None):
    base = program_dir()
    path = os.path.join(base, CONFIG_NAME)
    data = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle) or {}
            if logger:
                logger.debug("Loaded configuration from %s" % path)
        except (OSError, ValueError) as exc:
            if logger:
                logger.warn("config.json could not be read (%s); using defaults." % exc)
            data = {}
    return Config(data, path)


def write_default(path):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(DEFAULTS, handle, indent=2)
    return path
