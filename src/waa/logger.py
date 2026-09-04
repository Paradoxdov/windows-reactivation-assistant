"""Safe logging.

Hard rule: this logger must never persist secrets. Full product keys,
Microsoft credentials, cookies, session tokens and the full Installation ID
are scrubbed before anything reaches a file or the console.
"""

import datetime
import logging
import os
import re
import sys

LOG_DIR_NAME = "Logs"

# Secrets and machine/user identifiers are redacted defensively, even if a
# caller accidentally includes them in a message or exception traceback.
_SCRUB_PATTERNS = [
    (re.compile(r"\b([A-Z0-9]{5}-){4}[A-Z0-9]{5}\b", re.I),
     "[REDACTED-PRODUCT-KEY]"),
    (re.compile(r"\b\d{6,7}(?:[\s\u2010-\u2015-]*\d{6,7}){8,}\b"),
     "[REDACTED-IID]"),
    (re.compile(r"\b\d{40,}\b"), "[REDACTED-IID]"),
    (re.compile(
        r"(?i)\b[A-Z0-9._%+-]+(?:@|%40)[A-Z0-9-]+"
        r"(?:(?:\.|%2e)[A-Z0-9-]+)+\b"),
     "[REDACTED-EMAIL]"),
    (re.compile(r"(?i)\b([A-Z]:[\\/]+Users[\\/]+)[^\\/\r\n]+(?=[\\/])"),
     r"\1[REDACTED-USER]"),
    (re.compile(
        r"(?im)\b([A-Z]:[\\/]+Users[\\/]+)(?!\[REDACTED-USER\])"
        r"[^\\/\r\n'\";,)\]}]+(?=$|['\";,)\]}])"),
     r"\1[REDACTED-USER]"),
    (re.compile(
        r"(?i)\b([A-Z]:[\\/]+Users[\\/]+)(?!\[REDACTED-USER\])"
        r"[^\\/\s'\";,)\]}]+"),
     r"\1[REDACTED-USER]"),
    (re.compile(
        r"(?i)([\"']?)(Partial(?:[ _-]?Product)?[ _-]?Key)\1\s*[:=]\s*"
        r"[\"']?[A-Z0-9]{5}[\"']?\b"),
     r"\2=[REDACTED]"),
    (re.compile(r"(?i)\b(authorization|cookie|set-cookie)\s*:\s*[^\r\n]+"),
     r"\1: [REDACTED]"),
    (re.compile(
        r"(?i)([\"'])(authorization|cookie|set-cookie)\1\s*[:=]\s*"
        r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;}\]]+)"),
     r"\2=[REDACTED]"),
    (re.compile(
        r"(?i)\b(authorization|cookie|set-cookie)\s*=\s*[^\s,;}\]]+"),
     r"\1=[REDACTED]"),
    (re.compile(r"(?i)\bBearer\s+[A-Z0-9._~+/=-]+"), "Bearer [REDACTED]"),
    (re.compile(
        r"(?i)([\"']?)(password|passwd|pwd|id[_-]?token|access[_-]?token|"
        r"refresh[_-]?token|session[_-]?token|session[_-]?id|session[_-]?state)"
        r"\1\s*[:=]\s*(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;}\]]+)"),
     r"\2=[REDACTED]"),
    (re.compile(r"(?i)(https?://[^\s?#'\"<>]+)\?[^\s'\"<>]*"),
     r"\1?[REDACTED-QUERY]"),
]


def scrub(text):
    """Remove anything secret-looking from a string."""
    if not isinstance(text, str):
        text = str(text)
    for pattern, replacement in _SCRUB_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class _ScrubbingFormatter(logging.Formatter):
    def format(self, record):
        return scrub(super().format(record))


class Logger:
    """Thin wrapper around logging with a console mirror."""

    def __init__(self, program_dir, verbose=False):
        self.log_dir = os.path.join(program_dir, LOG_DIR_NAME)
        os.makedirs(self.log_dir, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.session_log = os.path.join(self.log_dir, "waa-%s.log" % stamp)
        self.debug_log = os.path.join(self.log_dir, "waa-%s.debug.log" % stamp)

        self._log = logging.getLogger("waa")
        self._log.setLevel(logging.DEBUG)
        self._log.handlers[:] = []
        self._log.propagate = False

        fmt = _ScrubbingFormatter("%(asctime)s  %(levelname)-7s  %(message)s",
                                  datefmt="%Y-%m-%d %H:%M:%S")

        info_handler = logging.FileHandler(self.session_log, encoding="utf-8")
        info_handler.setLevel(logging.INFO)
        info_handler.setFormatter(fmt)
        self._log.addHandler(info_handler)

        debug_handler = logging.FileHandler(self.debug_log, encoding="utf-8")
        debug_handler.setLevel(logging.DEBUG)
        debug_handler.setFormatter(fmt)
        self._log.addHandler(debug_handler)

        self.verbose = verbose

    # -- file+console -----------------------------------------------------
    def info(self, message):
        self._log.info(message)
        self._console(message)

    def warn(self, message):
        self._log.warning(message)
        self._console("WARNING: " + str(message))

    def error(self, message):
        self._log.error(message)
        self._console("ERROR: " + str(message))

    def step(self, message):
        """A user-facing workflow step."""
        self._log.info("STEP: %s", message)
        self._console("  -> " + str(message))

    # -- debug log only ---------------------------------------------------
    def debug(self, message):
        self._log.debug(message)
        if self.verbose:
            self._console("  [debug] " + str(message))

    def exception(self, message):
        self._log.exception(message)

    def _console(self, message):
        sys.stdout.write(scrub(str(message)) + "\n")
        sys.stdout.flush()

    def prune_old_logs(self, keep=20):
        """Keep the program directory tidy on the machines being serviced."""
        try:
            files = sorted(
                (f for f in os.listdir(self.log_dir) if f.startswith("waa-")),
                reverse=True,
            )
            for stale in files[keep * 2:]:
                try:
                    os.remove(os.path.join(self.log_dir, stale))
                except OSError:
                    pass
        except OSError:
            pass
