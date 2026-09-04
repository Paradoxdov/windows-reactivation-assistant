"""Microsoft Edge discovery, launch and DOM-level driving.

Design notes
------------
* Uses the Edge that is already installed on the machine - no bundled Chromium.
* Talks CDP directly, so no msedgedriver.exe has to be version-matched.
* All element addressing goes through the accessibility tree (role +
  accessible name). Mouse coordinates are never used.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from . import _ps
from . import config
from .cdp import CDPClient, CDPError
from .errors import BrowserConnectionFailed, EdgeLaunchFailed, EdgeNotFound

CREATE_NO_WINDOW = 0x08000000
PORT_FILE_NAME = "waa-devtools-port"

_EDGE_RELATIVE = os.path.join("Microsoft", "Edge", "Application", "msedge.exe")
_USER_DATA_DIR_ARG = re.compile(
    r'(?:^|\s)(?:"--user-data-dir(?:=|\s+)([^"]+)"|'
    r'--user-data-dir(?:=|\s+)"([^"]+)"|'
    r'--user-data-dir(?:=|\s+)([^\s"]+))',
    re.I,
)


def url_matches_hosts(url, hosts):
    """Return True only for HTTPS URLs on an exact/child allowed hostname."""
    if not isinstance(url, str) or "\\" in url:
        return False
    try:
        parsed = urllib.parse.urlsplit(url or "")
        hostname = (parsed.hostname or "").lower().rstrip(".")
        parsed.port  # validate malformed/non-numeric port syntax
    except (TypeError, ValueError):
        return False
    if (parsed.scheme.lower() != "https" or not hostname
            or parsed.username is not None or parsed.password is not None):
        return False
    for allowed in hosts or ():
        allowed = str(allowed or "").lower().rstrip(".")
        if allowed and (hostname == allowed or hostname.endswith("." + allowed)):
            return True
    return False


def command_uses_profile(command_line, profile_dir):
    """Match Edge's --user-data-dir argument as a complete path value."""
    match = _USER_DATA_DIR_ARG.search(command_line or "")
    if not match:
        return False
    value = next((group for group in match.groups() if group is not None), "")
    try:
        actual = os.path.normcase(os.path.realpath(os.path.abspath(value)))
        expected = os.path.normcase(os.path.realpath(os.path.abspath(profile_dir)))
    except (OSError, TypeError, ValueError):
        return False
    return actual == expected


def find_edge(logger, configured_path=None):
    """Locate msedge.exe without assuming a fixed install location."""
    candidates = []
    if configured_path:
        candidates.append(configured_path)

    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    key = winreg.OpenKey(
                        hive,
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
                        0, winreg.KEY_READ | view)
                    value, _ = winreg.QueryValueEx(key, None)
                    winreg.CloseKey(key)
                    if value:
                        candidates.append(value.strip('"'))
                except OSError:
                    continue
    except ImportError:  # pragma: no cover - Windows only
        pass

    for env_var in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432", "LOCALAPPDATA"):
        base = os.environ.get(env_var)
        if base:
            candidates.append(os.path.join(base, _EDGE_RELATIVE))

    which = shutil.which("msedge")
    if which:
        candidates.append(which)

    seen = set()
    for candidate in candidates:
        candidate = os.path.expandvars(candidate)
        key = candidate.lower()
        if key in seen:
            continue
        seen.add(key)
        if os.path.isfile(candidate):
            logger.debug("Edge found at %s" % candidate)
            return candidate

    raise EdgeNotFound(
        "Microsoft Edge (msedge.exe) was not found on this PC.",
        hint='Install Microsoft Edge, or set "edge_path" in config.json.',
    )


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Element(object):
    """An accessibility-tree node we can act on."""

    __slots__ = ("session_id", "backend_node_id", "role", "name", "value",
                 "disabled", "focusable", "description", "frame_id")

    def __init__(self, session_id, backend_node_id, role, name, value,
                 disabled, focusable, description="", frame_id=""):
        self.session_id = session_id
        self.backend_node_id = backend_node_id
        self.role = role
        self.name = name
        self.value = value
        self.disabled = disabled
        self.focusable = focusable
        self.description = description
        self.frame_id = frame_id or ""

    def __repr__(self):
        return "<%s %r%s>" % (self.role, self.name[:60],
                              " disabled" if self.disabled else "")


class Browser(object):
    """Owns the Edge process and the CDP connection."""

    def __init__(self, logger, profile_dir, edge_path=None, keep_open=True):
        self.logger = logger
        self.profile_dir = profile_dir
        self.edge_path = edge_path
        self.keep_open = keep_open
        self.process = None
        self.port = None
        self.cdp = None
        self.executable = None
        self._sessions = {}       # sessionId -> targetInfo
        self._targets = {}        # targetId -> sessionId
        self._session_order = []  # attach order, newest last

    # ----------------------------------------------------------- lifecycle
    @property
    def _port_file(self):
        return os.path.join(self.profile_dir, PORT_FILE_NAME)

    def _remember_port(self):
        try:
            with open(self._port_file, "w", encoding="utf-8") as handle:
                handle.write(str(self.port))
        except OSError as exc:
            self.logger.debug("Could not record the debugging port: %s" % exc)

    def _running_devtools_port(self):
        """Port of an Edge this program already left running on our profile.

        Edge does not write DevToolsActivePort the way Chrome does, so the
        port is recorded by us. Reusing that instance means a re-run after an
        error does not fail on a profile that is still locked.
        """
        try:
            with open(self._port_file, "r", encoding="utf-8") as handle:
                port = int(handle.readline().strip())
        except (OSError, ValueError):
            return None
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/version" % port, timeout=2):
                return port
        except (urllib.error.URLError, OSError):
            return None

    def _profile_edge_pids(self):
        """PIDs of Edge processes bound to *our* automation profile."""
        script = ("$p=@(Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" |"
                  " Select-Object ProcessId,CommandLine);"
                  " ConvertTo-Json -Compress -InputObject @($p)")
        try:
            data = _ps.run_json(script, timeout=30)
        except Exception as exc:
            self.logger.debug("Could not enumerate Edge processes: %s" % exc)
            return []
        if isinstance(data, dict):
            data = [data]
        pids = []
        for entry in data or []:
            command = entry.get("CommandLine") or ""
            if command_uses_profile(command, self.profile_dir):
                pids.append(int(entry["ProcessId"]))
        return pids

    def _release_profile(self):
        """Close only the Edge windows using this program's own profile."""
        pids = self._profile_edge_pids()
        if not pids:
            return False
        self.logger.warn("Closing %d Edge process(es) still holding the automation "
                         "profile (the normal Edge of the user is untouched)." % len(pids))
        for pid in pids:
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=CREATE_NO_WINDOW, timeout=15)
            except (OSError, subprocess.SubprocessError) as exc:
                self.logger.debug("taskkill %d failed: %s" % (pid, exc))
        time.sleep(2.0)
        return True

    def start(self, start_url="about:blank"):
        self.profile_dir = config.claim_profile_dir(self.profile_dir)
        exe = find_edge(self.logger, self.edge_path)
        self.executable = exe

        existing = self._running_devtools_port()
        if existing:
            self.logger.info("Reusing the Edge window already open on this profile.")
            self.attach(existing)
            return

        try:
            self._launch(exe, start_url)
        except (EdgeLaunchFailed, BrowserConnectionFailed) as exc:
            # Almost always a leftover Edge holding the profile from an earlier
            # run. Close that one - and only that one - then try once more.
            self.logger.debug("First launch attempt failed: %s" % exc)
            if not self._release_profile():
                raise
            self._launch(exe, start_url)

    def _launch(self, exe, start_url):
        self.port = _free_port()
        self._clear_stale_locks()
        args = [
            exe,
            "--remote-debugging-port=%d" % self.port,
            "--user-data-dir=%s" % self.profile_dir,
            "--no-first-run",
            "--no-default-browser-check",
            "--no-service-autorun",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-popup-blocking",
            "--start-maximized",
            start_url,
        ]
        self.logger.debug("Launching Edge: %s (port %d)" % (exe, self.port))
        try:
            self.process = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW, env=self._child_env())
        except OSError as exc:
            raise EdgeLaunchFailed("Could not start Microsoft Edge: %s" % exc)

        version = self._wait_for_devtools(timeout=45)
        self.logger.info("Edge started (%s)" % version.get("Browser", "unknown build"))
        ws_url = version.get("webSocketDebuggerUrl")
        if not ws_url:
            raise BrowserConnectionFailed("Edge did not expose a DevTools endpoint.")
        try:
            self.cdp = CDPClient(ws_url, self.logger)
        except Exception as exc:
            raise BrowserConnectionFailed("Could not attach to Edge: %s" % exc)

        self.cdp.send("Target.setDiscoverTargets", {"discover": True})
        self.cdp.send("Target.setAutoAttach", {
            "autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
        self._sync_sessions(timeout=15)
        self._remember_port()

    def _child_env(self):
        """Environment for Edge: its scratch files stay in the program folder."""
        env = os.environ.copy()
        scratch = config.local_temp_dir(create=True)
        if scratch:
            env["TEMP"] = scratch
            env["TMP"] = scratch
            self.logger.debug("Edge scratch directory: %s" % scratch)
        return env

    def _clear_stale_locks(self):
        """Drop singleton markers left behind by a browser that was killed.

        Without this, the next launch can hand off to a dead instance and exit
        immediately instead of opening its debugging port.
        """
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            path = os.path.join(self.profile_dir, name)
            if os.path.lexists(path):
                try:
                    os.remove(path)
                    self.logger.debug("Removed stale profile lock %s" % name)
                except OSError as exc:
                    self.logger.debug("Could not remove %s: %s" % (name, exc))

    def attach(self, port):
        """Attach to an Edge that is already listening on a debugging port."""
        self.port = port
        version = self._wait_for_devtools(timeout=10)
        ws_url = version.get("webSocketDebuggerUrl")
        if not ws_url:
            raise BrowserConnectionFailed("Edge did not expose a DevTools endpoint.")
        self.cdp = CDPClient(ws_url, self.logger)
        self.cdp.send("Target.setDiscoverTargets", {"discover": True})
        self.cdp.send("Target.setAutoAttach", {
            "autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
        self._sync_sessions(timeout=15)

    def _wait_for_devtools(self, timeout=45):
        endpoint = "http://127.0.0.1:%d/json/version" % self.port
        deadline = time.time() + timeout
        last_error = None
        launcher_exited = False
        while time.time() < deadline:
            # Edge often re-launches itself: the process we started can exit
            # while the real browser process keeps running. So a dead launcher
            # is only a symptom, never the verdict - the endpoint decides.
            if (not launcher_exited and self.process is not None
                    and self.process.poll() is not None):
                launcher_exited = True
                self.logger.debug("Edge launcher process exited with code %s; "
                                  "still waiting for the DevTools endpoint."
                                  % self.process.returncode)
            try:
                with urllib.request.urlopen(endpoint, timeout=3) as response:
                    return json.loads(response.read().decode("utf-8"))
            except (urllib.error.URLError, OSError, ValueError) as exc:
                last_error = exc
                time.sleep(0.3)
        if launcher_exited:
            raise EdgeLaunchFailed(
                "Edge exited without opening its automation endpoint.",
                hint="Close every Edge window that uses the BrowserProfile folder, "
                     "or run with --reset-profile.")
        raise BrowserConnectionFailed(
            "Edge did not open its DevTools endpoint in time (%s)." % last_error)

    def stop(self):
        if self.keep_open:
            self.logger.debug("Leaving the Edge window open for inspection.")
            if self.cdp:
                self.cdp.close()
            return
        if self.cdp:
            try:
                self.cdp.send("Browser.close", timeout=5)
            except Exception:
                pass
            self.cdp.close()
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
            except OSError:
                pass

    # ------------------------------------------------------------ sessions
    def _sync_sessions(self, timeout=0):
        """Process pending Target events; optionally wait for a first page."""
        deadline = time.time() + max(timeout, 0)
        while True:
            for event in self.cdp.drain_events():
                method = event.get("method")
                params = event.get("params", {})
                if method == "Target.attachedToTarget":
                    session_id = params["sessionId"]
                    info = params["targetInfo"]
                    self._sessions[session_id] = info
                    self._targets[info["targetId"]] = session_id
                    if session_id in self._session_order:
                        self._session_order.remove(session_id)
                    self._session_order.append(session_id)
                    self._prepare_session(session_id, info)
                elif method == "Target.detachedFromTarget":
                    session_id = params.get("sessionId")
                    info = self._sessions.pop(session_id, None)
                    if info:
                        self._targets.pop(info["targetId"], None)
                    if session_id in self._session_order:
                        self._session_order.remove(session_id)
                elif method in ("Target.targetInfoChanged", "Target.targetCreated"):
                    info = params.get("targetInfo", {})
                    session_id = self._targets.get(info.get("targetId"))
                    if session_id:
                        self._sessions[session_id] = info
            if self._has_page() or time.time() >= deadline:
                return
            time.sleep(0.1)

    def _has_page(self):
        return any(info.get("type") == "page" for info in self._sessions.values())

    def _prepare_session(self, session_id, info):
        if info.get("type") not in ("page", "iframe"):
            return
        for domain in ("Page.enable", "DOM.enable", "Runtime.enable"):
            try:
                self.cdp.send(domain, {}, session_id=session_id, timeout=10)
            except CDPError as exc:
                self.logger.debug("%s failed while preparing a page: %s"
                                  % (domain, exc))
        if info.get("type") == "page":
            try:
                self.cdp.send("Target.setAutoAttach", {
                    "autoAttach": True, "waitForDebuggerOnStart": False,
                    "flatten": True}, session_id=session_id, timeout=10)
            except CDPError:
                pass

    def page_sessions(self):
        self._sync_sessions()
        return [s for s in self._session_order
                if self._sessions.get(s, {}).get("type") == "page"]

    def active_session(self, prefer_hosts=()):
        """Pick the page target the workflow should act on."""
        sessions = self.page_sessions()
        if not sessions:
            raise BrowserConnectionFailed("No browser page is available.")
        scored = []
        for index, session_id in enumerate(sessions):
            url = self._sessions[session_id].get("url", "")
            score = index
            if url_matches_hosts(url, prefer_hosts):
                score += 1000
            if url and not url.startswith("about:"):
                score += 100
            if url.startswith("edge://") or url.startswith("chrome://"):
                score -= 500
            scored.append((score, index, session_id))
        scored.sort()
        return scored[-1][2]

    def related_sessions(self, session_id):
        """The page session plus any out-of-process iframe sessions."""
        self._sync_sessions()
        root = self._sessions.get(session_id, {})
        root_target_id = root.get("targetId")
        if not root_target_id:
            return [session_id]

        result = [session_id]
        related_target_ids = {root_target_id}
        remaining = [sid for sid in self._session_order
                     if sid != session_id
                     and self._sessions.get(sid, {}).get("type") == "iframe"]
        # TargetInfo.parentId is browser-owned metadata. Follow the chain from
        # this page only; an iframe belonging to another tab must never enter
        # the page's accessibility snapshot.
        while remaining:
            added = False
            for other in list(remaining):
                info = self._sessions.get(other, {})
                if info.get("parentId") not in related_target_ids:
                    continue
                target_id = info.get("targetId")
                if not target_id:
                    remaining.remove(other)
                    continue
                result.append(other)
                related_target_ids.add(target_id)
                remaining.remove(other)
                added = True
            if not added:
                break
        return result

    def session_url(self, session_id):
        self._sync_sessions()
        return self._sessions.get(session_id, {}).get("url", "")

    # -------------------------------------------------------------- actions
    def navigate(self, session_id, url):
        self.cdp.send("Page.navigate", {"url": url}, session_id=session_id, timeout=30)

    def evaluate(self, session_id, expression, timeout=15):
        result = self.cdp.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        }, session_id=session_id, timeout=timeout)
        if result.get("exceptionDetails"):
            raise CDPError("JS evaluation failed: %s"
                           % result["exceptionDetails"].get("text"))
        return result.get("result", {}).get("value")

    def page_text(self, session_id, limit=6000):
        try:
            text = self.evaluate(
                session_id,
                "(document.body && document.body.innerText || '').slice(0, %d)" % limit)
            return text or ""
        except CDPError:
            return ""

    def page_title(self, session_id):
        try:
            return self.evaluate(session_id, "document.title") or ""
        except CDPError:
            return ""

    def document_ready(self, session_id):
        try:
            return self.evaluate(session_id, "document.readyState") or ""
        except CDPError:
            return ""

    def _frame_metadata(self, session_id):
        """Return (root frame id, {frame id: URL}) from trusted CDP data."""
        result = self.cdp.send(
            "Page.getFrameTree", {}, session_id=session_id, timeout=15)
        root = result.get("frameTree") or {}
        root_frame = root.get("frame") or {}
        root_id = root_frame.get("id") or ""
        urls = {}
        pending = [root] if root else []
        while pending:
            entry = pending.pop()
            frame = entry.get("frame") or {}
            frame_id = frame.get("id")
            if frame_id:
                urls[frame_id] = frame.get("url") or ""
            pending.extend(entry.get("childFrames") or ())
        return root_id, urls

    @staticmethod
    def _ax_frame_ids(nodes, root_frame_id):
        """Resolve each AX node's document frame through the AX parent tree.

        Chromium normally emits ``frameId`` only on a document's root AX node,
        not on every descendant.  Carry that trusted marker down through
        ``parentId`` rather than assuming every marker-less node belongs to the
        target's root frame.  An incomplete or cyclic ancestry stays unknown.
        """
        by_id = {
            str(node.get("nodeId")): node
            for node in nodes
            if node.get("nodeId") is not None
        }
        first_node = nodes[0] if nodes else None
        resolved = {}
        resolving = set()

        def frame_for(node):
            node_id = node.get("nodeId")
            key = str(node_id) if node_id is not None else None
            if key is not None and key in resolved:
                return resolved[key]
            explicit = node.get("frameId") or ""
            if explicit:
                result = explicit
            elif node is first_node and not node.get("parentId"):
                # Compatibility with older Chromium builds that may omit the
                # root marker. Page.getFrameTree still identifies this target.
                result = root_frame_id
            else:
                parent_id = node.get("parentId")
                parent_key = str(parent_id) if parent_id is not None else None
                if (not parent_key or parent_key in resolving
                        or parent_key not in by_id):
                    result = ""
                else:
                    if key is not None:
                        resolving.add(key)
                    result = frame_for(by_id[parent_key])
                    if key is not None:
                        resolving.discard(key)
            if key is not None:
                resolved[key] = result
            return result

        return [frame_for(node) for node in nodes]

    def elements(self, session_id, include_ignored=False, allowed_hosts=()):
        """Accessibility snapshot of the page and its iframes."""
        found = []
        for sid in self.related_sessions(session_id):
            if (allowed_hosts
                    and not url_matches_hosts(
                        self._sessions.get(sid, {}).get("url", ""), allowed_hosts)):
                continue
            try:
                root_frame_id, frame_urls = self._frame_metadata(sid)
                if allowed_hosts and not root_frame_id:
                    self.logger.debug(
                        "Frame metadata unavailable for an allowed page target; skipping it.")
                    continue
                tree = self.cdp.send("Accessibility.getFullAXTree", {},
                                     session_id=sid, timeout=30)
            except CDPError as exc:
                self.logger.debug("AX tree unavailable for a page frame: %s" % exc)
                continue
            nodes = tree.get("nodes", []) or []
            node_frame_ids = self._ax_frame_ids(nodes, root_frame_id)
            for node, frame_id in zip(nodes, node_frame_ids):
                if node.get("ignored") and not include_ignored:
                    continue
                backend_id = node.get("backendDOMNodeId")
                if not backend_id:
                    continue
                if (allowed_hosts
                        and not url_matches_hosts(
                            frame_urls.get(frame_id, ""), allowed_hosts)):
                    continue
                role = (node.get("role") or {}).get("value") or ""
                name = (node.get("name") or {}).get("value") or ""
                value = (node.get("value") or {}).get("value") or ""
                description = (node.get("description") or {}).get("value") or ""
                disabled = False
                focusable = False
                for prop in node.get("properties", []) or []:
                    prop_name = prop.get("name")
                    prop_value = (prop.get("value") or {}).get("value")
                    if prop_name == "disabled":
                        disabled = bool(prop_value)
                    elif prop_name == "focusable":
                        focusable = bool(prop_value)
                found.append(Element(sid, backend_id, role, str(name).strip(),
                                     str(value), disabled, focusable,
                                     str(description).strip(), frame_id))
        return found

    def describe(self, element):
        """Tag/attribute detail for an element - used to verify our target."""
        script = ("function(){return JSON.stringify({tag:this.tagName,"
                  "type:this.type||'',id:this.id||'',name:this.name||'',"
                  "cls:(this.className&&this.className.toString?"
                  "this.className.toString():'').slice(0,120),"
                  "placeholder:this.placeholder||'',"
                  "maxlength:(this.maxLength===undefined?-1:this.maxLength),"
                  "value:(this.value===undefined?'':String(this.value)),"
                  "inputmode:(this.getAttribute?(this.getAttribute('inputmode')||''):''),"
                  "ariaLabel:(this.getAttribute?(this.getAttribute('aria-label')||''):''),"
                  "text:(this.innerText||'').slice(0,120),"
                  "visible:!!(this.offsetWidth||this.offsetHeight||"
                  "(this.getClientRects&&this.getClientRects().length))});}")
        raw = self._call_on(element, script)
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return {}

    def _resolve(self, element, execution_context_id=None):
        params = {"backendNodeId": element.backend_node_id}
        if execution_context_id is not None:
            params["executionContextId"] = execution_context_id
        resolved = self.cdp.send("DOM.resolveNode", params,
                                 session_id=element.session_id, timeout=15)
        object_id = resolved.get("object", {}).get("objectId")
        if not object_id:
            raise CDPError("Could not resolve element in the page")
        return object_id

    def _call_on(self, element, function_declaration, args=None,
                 execution_context_id=None):
        object_id = self._resolve(element, execution_context_id)
        params = {
            "objectId": object_id,
            "functionDeclaration": function_declaration,
            "returnByValue": True,
            "awaitPromise": True,
        }
        if args:
            params["arguments"] = [{"value": a} for a in args]
        result = self.cdp.send("Runtime.callFunctionOn", params,
                               session_id=element.session_id, timeout=20)
        if result.get("exceptionDetails"):
            raise CDPError("Element call failed: %s"
                           % result["exceptionDetails"].get("text"))
        return result.get("result", {}).get("value")

    def _guarded_context(self, element, hosts):
        """Create a clean execution world and validate its live frame URL."""
        frame_id = getattr(element, "frame_id", "") or ""
        if not frame_id:
            return None
        try:
            world = self.cdp.send("Page.createIsolatedWorld", {
                "frameId": frame_id,
                "worldName": "waa-safety-boundary",
                "grantUniveralAccess": False,
            }, session_id=element.session_id, timeout=15)
            context_id = world.get("executionContextId")
            if context_id is None:
                return None
            # Query after creating the world. A cross-document navigation
            # after this point destroys that context, making resolveNode fail.
            _, frame_urls = self._frame_metadata(element.session_id)
        except CDPError as exc:
            self.logger.debug("Could not establish a guarded element context: %s" % exc)
            return None
        if not url_matches_hosts(frame_urls.get(frame_id, ""), hosts):
            return None
        return context_id

    def click(self, element):
        """DOM click on a specific, already-identified element."""
        self._call_on(element, "function(){this.scrollIntoView({block:'center'});}")
        time.sleep(0.15)
        self._call_on(element, "function(){this.click();}")

    def click_if_host_allowed(self, element, hosts):
        """Click only from a trusted frame in a clean isolated world."""
        context_id = self._guarded_context(element, hosts)
        if context_id is None:
            return False
        script = (
            "function(allowed){try{"
            "var l=this.ownerDocument&&this.ownerDocument.location;"
            "var h=(l&&l.hostname||'').toLowerCase().replace(/\\.$/,'');"
            "var ok=l&&l.protocol==='https:'&&allowed.some(function(a){"
            "a=String(a||'').toLowerCase().replace(/\\.$/,'');"
            "return a&&(h===a||h.endsWith('.'+a));});"
            "if(!ok||!this.isConnected)return false;"
            "this.scrollIntoView({block:'center'});this.click();return true;"
            "}catch(e){return false;}}")
        try:
            return bool(self._call_on(
                element, script, [list(hosts or ())], context_id))
        except CDPError as exc:
            self.logger.debug("Guarded click failed closed: %s" % exc)
            return False

    def fill(self, element, text):
        """Type text into a field using real input events (never Enter)."""
        self._call_on(element, "function(){this.scrollIntoView({block:'center'});}")
        try:
            self.cdp.send("DOM.focus", {"backendNodeId": element.backend_node_id},
                          session_id=element.session_id, timeout=10)
            self._call_on(element, "function(){if(this.select)this.select();}")
            self.cdp.send("Input.insertText", {"text": text},
                          session_id=element.session_id, timeout=10)
        except CDPError as exc:
            self.logger.debug("Input.insertText path failed (%s); using value setter" % exc)
            setter = ("function(v){var p=Object.getPrototypeOf(this);"
                      "var d=Object.getOwnPropertyDescriptor(p,'value');"
                      "if(d&&d.set){d.set.call(this,v);}else{this.value=v;}"
                      "this.dispatchEvent(new Event('input',{bubbles:true}));"
                      "this.dispatchEvent(new Event('change',{bubbles:true}));"
                      "return this.value;}")
            self._call_on(element, setter, [text])
        return self.read_value(element)

    def fill_if_host_allowed(self, element, text, hosts):
        """Set a sensitive value only in a trusted, isolated frame world."""
        context_id = self._guarded_context(element, hosts)
        if context_id is None:
            return False
        script = (
            "function(v,allowed){try{"
            "var l=this.ownerDocument&&this.ownerDocument.location;"
            "var h=(l&&l.hostname||'').toLowerCase().replace(/\\.$/,'');"
            "var ok=l&&l.protocol==='https:'&&allowed.some(function(a){"
            "a=String(a||'').toLowerCase().replace(/\\.$/,'');"
            "return a&&(h===a||h.endsWith('.'+a));});"
            "if(!ok||!this.isConnected)return false;"
            "var tag=String(this.tagName||'').toUpperCase();"
            "var type=String(this.type||'').toLowerCase();"
            "if((tag!=='INPUT'&&tag!=='TEXTAREA')||this.disabled||this.readOnly||"
            "['password','hidden','checkbox','radio','submit','button'].indexOf(type)>=0)"
            "return false;"
            "this.scrollIntoView({block:'center'});"
            "var p=Object.getPrototypeOf(this);"
            "var d=Object.getOwnPropertyDescriptor(p,'value');"
            "if(d&&d.set){d.set.call(this,v);}else{this.value=v;}"
            "this.dispatchEvent(new Event('input',{bubbles:true}));"
            "this.dispatchEvent(new Event('change',{bubbles:true}));return true;"
            "}catch(e){return false;}}")
        try:
            return bool(self._call_on(
                element, script, [str(text), list(hosts or ())], context_id))
        except CDPError as exc:
            self.logger.debug("Guarded fill failed closed: %s" % exc)
            return False

    def read_value(self, element):
        return self._call_on(
            element, "function(){return this.value===undefined?'':String(this.value);}")
