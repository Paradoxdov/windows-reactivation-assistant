"""Everything that knows about visualsupport.microsoft.com.

The portal is a moving target, so this module never assumes a fixed CSS
selector or a fixed button caption. Each step:

  1. classifies the page it is actually looking at,
  2. verifies the element it is about to use,
  3. verifies that the click produced a different page,
  4. fails with a named error code instead of guessing.

Observed workflow (August 2026):
    /            "Let's Get Started"
    (Azure WAF human-verification challenge, sometimes)
    (Microsoft account sign-in, when the profile has no session)
    /welcome     "Activate a Microsoft Product"
    /select      product = "Windows"
    /select/windows/windows   Windows version = "Windows 11" / "Windows 10" ...
    /select/windows/windows/Windows_Unspecified
                 "6 Digits" / "7 Digits" per block - shown instead of the
                 version step when the version is not known to the portal
    /activate    Installation ID fields + "Submit"

HARD SAFETY BOUNDARY: once the Installation ID has been typed in, this module
switches to safe-stop mode. From that point every click attempt raises
SafetyViolation. The Submit/Confirm button is located and reported, never
pressed; no form submit, no Enter key, no direct endpoint call.
"""

import hashlib
import re
import time
import urllib.parse

from .browser import url_matches_hosts
from .cdp import CDPError
from .errors import (
    IidFieldNotFound,
    IidInsertFailed,
    PortalLayoutChanged,
    PortalTimeout,
    SafetyViolation,
    SubmitButtonNotFound,
)

PORTAL_URL = "https://visualsupport.microsoft.com/"
PORTAL_HOSTS = ("visualsupport.microsoft.com",)
LOGIN_HOSTS = ("login.microsoftonline.com", "login.live.com", "login.microsoft.com",
               "account.microsoft.com", "account.live.com", "signup.live.com",
               "msft.sts.microsoft.com")

CLICKABLE_ROLES = {"button", "link", "menuitem", "tab", "radio", "checkbox",
                   "option", "listitem", "cell", "gridcell", "treeitem"}
TEXT_ROLES = {"textbox", "searchbox", "spinbutton"}

# --- page-state names -------------------------------------------------------
STATE_HUMAN_VERIFICATION = "HUMAN_VERIFICATION"
STATE_LOGIN = "MICROSOFT_LOGIN"
STATE_LANDING = "PORTAL_LANDING"
STATE_PRODUCT_CHOICE = "ACTIVATE_A_MICROSOFT_PRODUCT"
STATE_PRODUCT_TYPE = "SELECT_PRODUCT_WINDOWS"
STATE_WINDOWS_VERSION = "SELECT_WINDOWS_VERSION"
STATE_IID_DIGIT_FORMAT = "INSTALLATION_ID_BLOCK_SIZE"
STATE_IID_CHOICE = "ENTER_INSTALLATION_ID_CHOICE"
STATE_IID_ENTRY = "INSTALLATION_ID_ENTRY"
STATE_UNKNOWN = "UNKNOWN_PAGE"

# --- caption patterns (deliberately broad) ----------------------------------
RE_START = re.compile(
    r"(let.{0,3}s get started|get started|proceed to sign\s*in|sign in|continue to sign)",
    re.I)
RE_ACTIVATE_PRODUCT = re.compile(r"activate a (microsoft\s+)?product", re.I)
RE_WINDOWS_PRODUCT = re.compile(r"^windows$", re.I)
# Only real client versions. "Windows Server" belongs to the product page and
# must not be mistaken for a version choice.
RE_WINDOWS_VERSION = re.compile(r"^windows\s*(11|10|8\.1|8|7|vista|xp)$", re.I)
RE_IID_CHOICE = re.compile(r"^(enter|provide|submit)\b.{0,20}installation id", re.I)
RE_IID_WHOLE_FIELD = re.compile(r"(complete|full|entire).{0,20}installation id|"
                                r"installation id.{0,20}(complete|full)", re.I)
RE_IID_ANY_FIELD = re.compile(r"installation id", re.I)
RE_IID_GROUP_FIELD = re.compile(r"^\s*(group\s*)?([1-9]|1[0-2])\s*$", re.I)
# "Does your Installation ID have 6 or 7 digits in each block?" -> "6 Digits"
RE_DIGIT_BLOCK = re.compile(r"^\s*(\d)\s*digits?\s*$", re.I)
# "activate" is deliberately absent: "Activate a Microsoft Product" is a
# navigation button, not the control that sends the Installation ID.
RE_SUBMIT = re.compile(
    r"^\s*(submit|confirm|get confirmation|generate confirmation|"
    r"continue|next|verify)\b", re.I)
RE_REJECT_COOKIES = re.compile(r"^\s*(reject|decline|deny)(\s+(all|optional|"
                               r"non-essential))?(\s+cookies)?\s*$", re.I)
RE_CAPTCHA = re.compile(r"(verification challenge|captcha|are you a human|"
                        r"press and hold)", re.I)
RE_LOGIN_HINTS = re.compile(r"(sign in|email, phone|enter password|stay signed in|"
                            r"pick an account|use another account)", re.I)

DIGITS = re.compile(r"\d")
# The portal renders chevrons with an icon font, so button captions contain
# Private Use Area glyphs that must not take part in text comparisons.
PRIVATE_USE = re.compile("[-󰀀-󿿽]")


def normalise_text(value):
    return " ".join(PRIVATE_USE.sub(" ", value or "").split())


def _hosts_match(url, hosts):
    return url_matches_hosts(url, hosts)


def _safe_url(url):
    """Strip query parameters and fragments before a URL reaches a log."""
    try:
        parsed = urllib.parse.urlsplit(url or "")
        host = parsed.hostname or ""
        if ":" in host:
            host = "[" + host + "]"
        if parsed.port is not None:
            host += ":%s" % parsed.port
        return urllib.parse.urlunsplit(
            (parsed.scheme, host, parsed.path, "", ""))
    except (TypeError, ValueError):
        return "[INVALID-URL]"


def _digits_of(value):
    return "".join(DIGITS.findall(value or ""))


class PageSnapshot(object):
    """One observation of the browser: url, title, text and AX elements."""

    def __init__(self, url, title, text, elements):
        self.url = url or ""
        self.title = title or ""
        self.text = text or ""
        self.elements = elements

    @property
    def signature(self):
        names = "|".join("%s:%s" % (e.role, e.name) for e in self.elements[:120])
        raw = "%s##%s##%s" % (self.url.split("?")[0], self.title, names)
        return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()

    def matching(self, pattern, roles=None):
        result = []
        for element in self.elements:
            if roles and element.role not in roles:
                continue
            haystack = " ".join(x for x in (element.name, element.description) if x)
            if haystack.strip() and pattern.search(haystack):
                result.append(element)
        return result

    def clickable(self, pattern):
        return [e for e in self.matching(pattern, CLICKABLE_ROLES) if not e.disabled]

    def textboxes(self):
        return [e for e in self.elements if e.role in TEXT_ROLES and not e.disabled]


class MicrosoftActivationPortal(object):
    """Drives the portal up to - and no further than - the safe stop."""

    def __init__(self, browser, logger, config):
        self.browser = browser
        self.logger = logger
        self.config = config
        self.session = None
        self.safe_stop = False
        self.windows_version = ""
        self.status = {
            "portal_opened": False,
            "authenticated": False,
            "product_selected": False,
            "windows_selected": False,
            "version_selected": False,
            "iid_page": False,
            "iid_inserted": False,
            "submit_found": False,
            "submit_label": "",
        }
        self._text_cache = {}
        self._iid = None

    # ------------------------------------------------------------ plumbing
    def _refresh_session(self):
        self.session = self.browser.active_session(
            prefer_hosts=PORTAL_HOSTS + LOGIN_HOSTS)
        return self.session

    def snapshot(self):
        session = self._refresh_session()
        url = self.browser.session_url(session)
        if not _hosts_match(url, PORTAL_HOSTS + LOGIN_HOSTS):
            # Do not inspect arbitrary user tabs that happen to be open in Edge.
            return PageSnapshot(url, "", "", [])
        title = self.browser.page_title(session)
        text = self.browser.page_text(session, 4000)
        elements = self.browser.elements(
            session, allowed_hosts=PORTAL_HOSTS + LOGIN_HOSTS)
        return PageSnapshot(url, title, text, elements)

    def _text_of(self, element):
        """The element's own visible text.

        The portal reuses one heading as part of several accessible names
        ("What product are you trying to activate? Windows"), so the visible
        text of the control itself is the reliable discriminator.
        """
        key = (element.session_id, element.backend_node_id)
        if key not in self._text_cache:
            try:
                info = self.browser.describe(element)
            except Exception as exc:
                self.logger.debug("describe() failed for %r: %s" % (element, exc))
                info = {}
            self._text_cache[key] = normalise_text(info.get("text"))
        return self._text_cache[key]

    def _click(self, element, what):
        if self.safe_stop:
            raise SafetyViolation(
                "Refusing to click %r: the safe stop before Submit is active." % what)
        if RE_SUBMIT.search((element.name or "").strip()) and self.status["iid_page"]:
            raise SafetyViolation(
                "Refusing to click %r: it looks like the Submit control." % element.name)
        self.logger.debug("Clicking %s -> %r" % (what, element))
        if not self.browser.click_if_host_allowed(element, PORTAL_HOSTS):
            raise SafetyViolation(
                "Refusing to click %r outside the Microsoft activation portal."
                % what)

    def _fill(self, element, value, what):
        """Fill only a live element that still belongs to the official portal."""
        if not self.browser.fill_if_host_allowed(element, value, PORTAL_HOSTS):
            raise SafetyViolation(
                "Refusing to fill %s outside the Microsoft activation portal."
                % what)

    def _require_portal_url(self, url, action):
        if not _hosts_match(url, PORTAL_HOSTS):
            raise SafetyViolation(
                "Refusing to %s outside the Microsoft activation portal (%s)."
                % (action, _safe_url(url)))

    def _wait_for_change(self, previous_signature, timeout=None, settle=1.2):
        """Wait until the page actually becomes something else."""
        timeout = timeout or self.config.step_timeout
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.6)
            try:
                snapshot = self.snapshot()
            except Exception as exc:
                self.logger.debug("Snapshot during wait failed: %s" % exc)
                continue
            if snapshot.signature != previous_signature and snapshot.elements:
                time.sleep(settle)
                self._text_cache.clear()
                return self.snapshot()
        return None

    # ---------------------------------------------------------- classifier
    def classify(self, snapshot):
        url = (snapshot.url or "").lower()
        title = (snapshot.title or "").lower()
        is_portal = _hosts_match(url, PORTAL_HOSTS)
        is_login = _hosts_match(url, LOGIN_HOSTS)

        if not (is_portal or is_login):
            return STATE_UNKNOWN

        if ("captcha" in url or "azure waf" in title
                or RE_CAPTCHA.search(snapshot.text[:800] or "")
                or snapshot.matching(RE_CAPTCHA, {"Iframe", "iframe"})):
            return STATE_HUMAN_VERIFICATION

        if is_login:
            return STATE_LOGIN

        if self._iid_fields(snapshot)[0] or self._iid_fields(snapshot)[1]:
            return STATE_IID_ENTRY

        if self._digit_block_options(snapshot):
            return STATE_IID_DIGIT_FORMAT

        if snapshot.clickable(RE_ACTIVATE_PRODUCT):
            return STATE_PRODUCT_CHOICE

        # Product page first: it is the only one offering a plain "Windows"
        # tile, and its "Windows Server" tile must not look like a version.
        if self._windows_product_options(snapshot):
            return STATE_PRODUCT_TYPE

        if len(self._version_options(snapshot)) >= 2:
            return STATE_WINDOWS_VERSION

        iid_choice = [e for e in snapshot.clickable(RE_IID_CHOICE)
                      if "help" not in (e.name or "").lower()]
        if iid_choice:
            return STATE_IID_CHOICE

        if snapshot.clickable(RE_START):
            return STATE_LANDING

        return STATE_UNKNOWN

    def _windows_product_options(self, snapshot):
        """The 'Windows' tile on the product page - not 'Windows Server'."""
        options = []
        for element in snapshot.elements:
            if element.role not in CLICKABLE_ROLES or element.disabled:
                continue
            name = (element.name or "").strip()
            if not name or "windows" not in name.lower():
                continue
            if RE_WINDOWS_PRODUCT.match(self._text_of(element)):
                options.append(element)
        return options

    def _digit_block_options(self, snapshot):
        """Buttons of the "6 Digits" / "7 Digits" question, as (element, size)."""
        options = []
        for element in snapshot.elements:
            if element.role not in CLICKABLE_ROLES or element.disabled:
                continue
            name = (element.name or "")
            if "digit" not in name.lower():
                continue
            match = RE_DIGIT_BLOCK.match(self._text_of(element))
            if match:
                options.append((element, int(match.group(1))))
        return options

    def _version_options(self, snapshot):
        """Buttons on the 'Select Windows version' page."""
        options = []
        for element in snapshot.elements:
            if element.role not in CLICKABLE_ROLES or element.disabled:
                continue
            name = (element.name or "").strip()
            if not name or "windows" not in name.lower():
                continue
            text = self._text_of(element)
            if RE_WINDOWS_VERSION.match(text) and not RE_WINDOWS_PRODUCT.match(text):
                options.append(element)
        return options

    def _iid_fields(self, snapshot):
        """Return (whole_field, group_fields) for the Installation ID form."""
        boxes = snapshot.textboxes()
        if not boxes:
            return None, []
        page_mentions_iid = bool(RE_IID_ANY_FIELD.search(snapshot.text or ""))

        whole = None
        groups = []
        for element in boxes:
            label = " ".join(x for x in (element.name, element.description) if x)
            if RE_IID_WHOLE_FIELD.search(label):
                whole = whole or element
            elif RE_IID_GROUP_FIELD.match(label) and page_mentions_iid:
                groups.append(element)

        if whole is None and page_mentions_iid:
            labelled = [e for e in boxes
                        if RE_IID_ANY_FIELD.search(
                            " ".join(x for x in (e.name, e.description) if x))]
            if labelled:
                whole = labelled[0]
        if len(groups) < 6:
            groups = []
        return whole, groups

    # --------------------------------------------------------------- steps
    def _handle_cookie_banner(self, snapshot):
        """Decline optional cookies if the portal asks. Never accepts."""
        for element in snapshot.clickable(RE_REJECT_COOKIES):
            name = (element.name or "").strip().lower()
            if "accept" in name or "allow" in name:
                continue
            self.logger.step("Declining optional cookies (%s)" % element.name)
            try:
                self._click(element, "cookie reject")
                time.sleep(1.0)
                return True
            except SafetyViolation:
                raise
            except Exception as exc:
                self.logger.debug("Cookie banner click failed: %s" % exc)
        return False

    def open_portal(self):
        session = self._refresh_session()
        current = self.browser.session_url(session)
        if _hosts_match(current, PORTAL_HOSTS) and "captcha" not in current.lower():
            # A reused window is already inside the portal. Reloading it would
            # only trigger another human-verification challenge.
            snapshot = self.snapshot()
            if snapshot.elements:
                self.status["portal_opened"] = True
                self.logger.info("Microsoft Portal: OK (already open at %s)"
                                 % _safe_url(current))
                return snapshot
        self.logger.step("Opening the Microsoft Product Activation Portal")
        self.browser.navigate(session, PORTAL_URL)
        deadline = time.time() + self.config.step_timeout
        while time.time() < deadline:
            time.sleep(0.8)
            try:
                snapshot = self.snapshot()
            except CDPError as exc:
                self.logger.debug("Portal still loading: %s" % exc)
                continue
            if _hosts_match(snapshot.url, PORTAL_HOSTS) and snapshot.elements:
                self.status["portal_opened"] = True
                self.logger.info("Microsoft Portal: OK (%s)"
                                 % _safe_url(snapshot.url))
                return snapshot
        raise PortalTimeout("The activation portal did not load in %ds."
                            % self.config.step_timeout)

    def _wait_for_human(self, snapshot, kind):
        """Hand control to the technician; resume automatically afterwards."""
        if kind == STATE_HUMAN_VERIFICATION:
            code = "HUMAN_VERIFICATION_REQUIRED"
            headline = "Microsoft is showing a human-verification challenge (CAPTCHA)."
            action = "Please complete the challenge in the Edge window."
        else:
            code = "MICROSOFT_LOGIN_REQUIRED"
            headline = "Microsoft is asking you to sign in."
            action = ("Please sign in with your Microsoft account in the Edge window. "
                      "The assistant does not enter or log your credentials.")

        self.logger.info("")
        self.logger.info("  [ACTION REQUIRED] %s" % code)
        self.logger.info("  %s" % headline)
        self.logger.info("  %s" % action)
        self.logger.info("  Waiting - the assistant continues on its own once the page moves on.")

        deadline = time.time() + self.config.human_timeout
        while time.time() < deadline:
            time.sleep(1.5)
            try:
                self._text_cache.clear()
                current = self.snapshot()
            except Exception as exc:
                self.logger.debug("Snapshot while waiting for user: %s" % exc)
                continue
            state = self.classify(current)
            if state not in (STATE_HUMAN_VERIFICATION, STATE_LOGIN, STATE_UNKNOWN):
                self.logger.info("  Continuing automatically (page is now: %s)." % state)
                if kind == STATE_LOGIN:
                    self.status["authenticated"] = True
                    self.logger.info("Authentication: OK")
                return current
        raise PortalTimeout(
            "Timed out after %ds waiting for %s to be completed."
            % (self.config.human_timeout, code))

    def _step_click(self, snapshot, elements, description, log_line, find_again=None):
        if not elements:
            raise PortalLayoutChanged("Could not find: %s" % description)
        target = elements[0]
        if len(elements) > 1:
            self.logger.debug("Multiple candidates for %s: %s"
                              % (description, elements[:5]))
        caption = self._text_of(target) or target.name.strip() or target.role
        self.logger.step('%s ("%s")' % (log_line, caption))
        signature = snapshot.signature

        try:
            self._click(target, description)
        except CDPError as exc:
            # The portal navigated between reading the page and clicking, so
            # the element we picked no longer exists. Re-read and decide.
            self.logger.debug("Click on a stale element failed (%s); re-reading" % exc)
            time.sleep(1.0)
            fresh = self.snapshot()
            if fresh.signature != signature:
                self.logger.debug("The page moved on by itself; continuing.")
                return fresh
            if find_again is None:
                raise PortalLayoutChanged(
                    "Could not act on: %s (the page changed while reading it)."
                    % description)
            self._text_cache.clear()
            retry = find_again(fresh)
            if not retry:
                raise PortalLayoutChanged("Could not find: %s" % description)
            self._click(retry[0], description)
            signature = fresh.signature

        result = self._wait_for_change(signature)
        if result is None:
            raise PortalTimeout("The page did not change after selecting: %s" % description)
        return result

    # ------------------------------------------------------------ workflow
    def run_until_safe_stop(self, installation_id, windows_version=""):
        self.windows_version = windows_version or ""
        self._iid = installation_id
        snapshot = self.open_portal()
        deadline = time.time() + self.config.total_timeout
        repeats = {}

        while time.time() < deadline:
            self._text_cache.clear()
            state = self.classify(snapshot)
            self.logger.debug("Page state=%s url=%s title=%r"
                              % (state, _safe_url(snapshot.url), snapshot.title))

            if state == STATE_IID_ENTRY:
                self.status["iid_page"] = True
                self.status["authenticated"] = True
                self.logger.info("Windows Activation Page: OK (%s)"
                                 % _safe_url(snapshot.url))
                self._insert_iid(snapshot, installation_id)
                self._find_submit()
                return

            if state in (STATE_HUMAN_VERIFICATION, STATE_LOGIN):
                snapshot = self._wait_for_human(snapshot, state)
                continue

            if state == STATE_LANDING:
                self._handle_cookie_banner(snapshot)
                snapshot = self._step_click(
                    snapshot, snapshot.clickable(RE_START),
                    "portal start button", "Starting the portal workflow",
                    find_again=lambda s: s.clickable(RE_START))
                continue

            if state == STATE_PRODUCT_CHOICE:
                if not self.status["authenticated"]:
                    self.status["authenticated"] = True
                    self.logger.info("Authentication: OK (existing session reused)")
                snapshot = self._step_click(
                    snapshot, snapshot.clickable(RE_ACTIVATE_PRODUCT),
                    "'Activate a Microsoft Product'",
                    "Choosing 'Activate a Microsoft Product'",
                    find_again=lambda s: s.clickable(RE_ACTIVATE_PRODUCT))
                self.status["product_selected"] = True
                continue

            if state == STATE_PRODUCT_TYPE:
                snapshot = self._step_click(
                    snapshot, self._windows_product_options(snapshot),
                    "'Windows' product", "Choosing product 'Windows'",
                    find_again=self._windows_product_options)
                self.status["windows_selected"] = True
                continue

            if state == STATE_WINDOWS_VERSION:
                snapshot = self._select_windows_version(snapshot)
                continue

            if state == STATE_IID_DIGIT_FORMAT:
                snapshot = self._select_digit_block(snapshot)
                continue

            if state == STATE_IID_CHOICE:
                choices = [e for e in snapshot.clickable(RE_IID_CHOICE)
                           if "help" not in (e.name or "").lower()]
                snapshot = self._step_click(
                    snapshot, choices, "'Enter Installation ID'",
                    "Choosing 'Enter Installation ID'",
                    find_again=lambda s: [e for e in s.clickable(RE_IID_CHOICE)
                                          if "help" not in (e.name or "").lower()])
                continue

            # Unknown page: allow a short settle, then fail loudly.
            key = snapshot.signature
            repeats[key] = repeats.get(key, 0) + 1
            if repeats[key] > self.config.unknown_page_retries:
                self._dump_page(snapshot)
                raise PortalLayoutChanged(
                    "The portal showed a page this assistant does not recognise "
                    "(url=%s, title=%r)."
                    % (_safe_url(snapshot.url), snapshot.title),
                    hint="The portal layout has probably changed. "
                         "The debug log contains a dump of the page.")
            time.sleep(1.5)
            try:
                snapshot = self.snapshot()
            except CDPError as exc:
                self.logger.debug("Page was navigating while being read: %s" % exc)
                time.sleep(1.0)

        raise PortalTimeout("The portal workflow did not reach the Installation ID "
                            "page within %ds." % self.config.total_timeout)

    def _select_digit_block(self, snapshot):
        """Answer "how many digits per block" from the real Installation ID."""
        wanted = self._iid.group_size if self._iid else 7
        options = self._digit_block_options(snapshot)
        match = [element for element, size in options if size == wanted]
        if not match:
            offered = sorted({size for _, size in options})
            self._dump_page(snapshot)
            raise PortalLayoutChanged(
                "The portal asks how many digits each Installation ID block has, "
                "but %d is not offered (available: %s)."
                % (wanted, ", ".join(str(x) for x in offered)))
        return self._step_click(
            snapshot, match, "Installation ID block size",
            "Answering block size: %d digits" % wanted,
            find_again=lambda s: [e for e, size in self._digit_block_options(s)
                                  if size == wanted])

    def _select_windows_version(self, snapshot):
        options = self._version_options(snapshot)
        wanted = (self.windows_version or "").strip().lower()
        match = [e for e in options if self._text_of(e).strip().lower() == wanted]
        if not match:
            available = sorted({self._text_of(e) for e in options})
            self._dump_page(snapshot)
            raise PortalLayoutChanged(
                "The portal is asking for the Windows version, but %r is not one of "
                "the offered options (%s)." % (self.windows_version, ", ".join(available)))
        snapshot = self._step_click(
            snapshot, match, "Windows version", "Choosing Windows version",
            find_again=lambda s: [e for e in self._version_options(s)
                                  if self._text_of(e).strip().lower() == wanted])
        self.status["version_selected"] = True
        return snapshot

    # ----------------------------------------------------------------- IID
    def _usable(self, element):
        """A field must be a real, visible, writable text input."""
        try:
            info = self.browser.describe(element)
        except Exception as exc:
            self.logger.debug("describe() failed for %r: %s" % (element, exc))
            return None
        if not info.get("visible"):
            return None
        if str(info.get("tag", "")).upper() not in ("INPUT", "TEXTAREA"):
            return None
        if str(info.get("type", "")).lower() in ("password", "hidden", "checkbox",
                                                 "radio", "submit", "button"):
            return None
        return info

    def _insert_iid(self, snapshot, installation_id):
        self._require_portal_url(snapshot.url, "enter the Installation ID")
        whole, groups = self._iid_fields(snapshot)
        if whole is None and not groups:
            raise IidFieldNotFound("No Installation ID input field was found on the page.")

        digits = installation_id.digits
        entered = ""

        if whole is not None:
            info = self._usable(whole)
            if info is None:
                self.logger.debug("The 'complete Installation ID' field is not usable.")
            else:
                maxlength = info.get("maxlength", -1)
                text = digits
                if isinstance(maxlength, int) and 0 < maxlength < len(text):
                    raise IidInsertFailed(
                        "The Installation ID field accepts only %d characters "
                        "but the ID has %d digits." % (maxlength, len(text)))
                self.logger.step("Entering the Installation ID into the "
                                 "'%s' field" % (info.get("placeholder")
                                                 or whole.name or "Installation ID"))
                self._fill(whole, text, "the Installation ID")
                time.sleep(0.8)
                entered = self._read_back(installation_id, whole)

        if entered != digits and groups:
            self.logger.step("Entering the Installation ID into %d grouped fields"
                             % len(groups))
            usable = [g for g in groups if self._usable(g) is not None]
            if len(usable) < len(installation_id.groups):
                raise IidFieldNotFound(
                    "Only %d of the %d Installation ID group fields are usable."
                    % (len(usable), len(installation_id.groups)))
            for element, group in zip(usable, installation_id.groups):
                self._fill(element, group, "an Installation ID group")
                time.sleep(0.1)
            time.sleep(0.5)
            entered = self._read_back(installation_id, whole)

        if entered != digits:
            self.logger.debug("Verification mismatch: %d digits present, %d expected"
                              % (len(entered), len(digits)))
            raise IidInsertFailed(
                "The Installation ID did not appear correctly in the form "
                "(%d of %d digits present)." % (len(entered), len(digits)))

        self.status["iid_inserted"] = True
        self.safe_stop = True  # hard boundary from here on
        self.logger.info("IID Inserted: OK (verified on the page; value not logged)")

    def _read_back(self, installation_id, whole):
        """Re-read the form and return the digits it actually contains."""
        self._text_cache.clear()
        snapshot = self.snapshot()
        self._require_portal_url(snapshot.url, "verify the Installation ID")
        fresh_whole, fresh_groups = self._iid_fields(snapshot)
        if fresh_groups:
            collected = ""
            for element in fresh_groups:
                try:
                    collected += _digits_of(self.browser.read_value(element))
                except Exception as exc:
                    self.logger.debug("read_value failed: %s" % exc)
            if collected == installation_id.digits:
                return collected
            partial = collected
        else:
            partial = ""
        target = fresh_whole or whole
        if target is not None:
            try:
                value = _digits_of(self.browser.read_value(target))
                if value == installation_id.digits:
                    return value
                partial = partial or value
            except Exception as exc:
                self.logger.debug("read_value failed for whole field: %s" % exc)
        return partial

    # -------------------------------------------------------------- submit
    def _find_submit(self):
        """Locate the Submit/Confirm control. It is never clicked."""
        self._text_cache.clear()
        snapshot = self.snapshot()
        self._require_portal_url(snapshot.url, "inspect the Submit control")
        candidates = []
        for element in snapshot.elements:
            if element.role not in ("button", "link"):
                continue
            if RE_SUBMIT.search((element.name or "").strip()):
                candidates.append(element)
        if not candidates:
            for element in snapshot.elements:
                if element.role == "button" and RE_SUBMIT.search(self._text_of(element)):
                    candidates.append(element)
        if not candidates:
            self._dump_page(snapshot)
            raise SubmitButtonNotFound(
                "The Installation ID was entered, but no Submit/Confirm button "
                "could be identified on the page.")

        enabled = [e for e in candidates if not e.disabled]
        target = enabled[0] if enabled else candidates[0]
        try:
            info = self.browser.describe(target)
        except Exception:
            info = {}
        self.status["submit_found"] = True
        self.status["submit_label"] = (target.name.strip()
                                       or info.get("text", "") or target.role)
        self.logger.info('Submit Button: FOUND ("%s", <%s>%s)'
                         % (self.status["submit_label"], info.get("tag", "?"),
                            ", disabled" if target.disabled else ""))
        self.logger.info("Submit Button: NOT PRESSED - safe stop is active")
        return target

    def _dump_page(self, snapshot):
        self.logger.debug("---- page dump ----")
        self.logger.debug("url=%s title=%r"
                          % (_safe_url(snapshot.url), snapshot.title))
        for element in snapshot.elements[:250]:
            if element.role in ("StaticText", "InlineTextBox", "none", "generic",
                                "LineBreak"):
                continue
            label = element.name or ""
            if element.value and element.value in label:
                label = label.replace(element.value, "[REDACTED-VALUE]")
            self.logger.debug("  %-16s %r" % (element.role, label[:90]))
        self.logger.debug("---- end dump ----")
