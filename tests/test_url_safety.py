import os
import sys
import unittest
from types import SimpleNamespace

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from waa.browser import Browser, Element, command_uses_profile, url_matches_hosts
from waa.errors import SafetyViolation
from waa.portal import (
    LOGIN_HOSTS,
    PORTAL_HOSTS,
    MicrosoftActivationPortal,
    PageSnapshot,
    STATE_IID_ENTRY,
    STATE_LOGIN,
    STATE_UNKNOWN,
)


class _Logger:
    def debug(self, message):
        pass

    info = warn = error = step = debug


class _ActionBrowser:
    def __init__(self, urls, description=None):
        self.urls = urls
        self.description = description or {
            "visible": True,
            "tag": "INPUT",
            "type": "text",
            "maxlength": 100,
            "placeholder": "Installation ID",
        }
        self.clicks = 0
        self.fills = 0

    def session_url(self, session_id):
        return self.urls[session_id]

    def click(self, element):
        self.clicks += 1

    def fill(self, element, value):
        self.fills += 1

    def _element_allowed(self, element, hosts):
        url = element.document_url or self.urls[element.session_id]
        return url_matches_hosts(url, hosts)

    def click_if_host_allowed(self, element, hosts):
        if not self._element_allowed(element, hosts):
            return False
        self.clicks += 1
        return True

    def fill_if_host_allowed(self, element, value, hosts):
        if not self._element_allowed(element, hosts):
            return False
        self.fills += 1
        return True

    def describe(self, element):
        return self.description


def _iid_field(session_id="page", document_url=None, frame_id="frame"):
    return SimpleNamespace(
        session_id=session_id,
        backend_node_id=1,
        role="textbox",
        name="Complete Installation ID",
        description="",
        value="",
        disabled=False,
        focusable=True,
        document_url=document_url,
        frame_id=frame_id,
    )


def _browser_element(frame_id="frame"):
    return Element(
        "page", 1, "textbox", "Complete Installation ID", "",
        False, True, "", frame_id)


class UrlSafetyTests(unittest.TestCase):
    def test_accepts_only_https_exact_or_child_hosts(self):
        good = (
            "https://visualsupport.microsoft.com/activate",
            "https://VISUALSUPPORT.MICROSOFT.COM.:443/activate",
            "https://child.visualsupport.microsoft.com/activate",
        )
        bad = (
            "http://visualsupport.microsoft.com/activate",
            "https://visualsupport.microsoft.com.evil.example/",
            "https://evilvisualsupport.microsoft.com/",
            "https://visualsupport.microsoft.com@evil.example/",
            "https://evil.example/visualsupport.microsoft.com",
            "https://evil.example/?next=visualsupport.microsoft.com",
            "https://evil.example\\@visualsupport.microsoft.com/",
            "https://visualsupport.microsoft.com:443.evil/",
            "file:///visualsupport.microsoft.com",
            "not a url",
        )
        for url in good:
            self.assertTrue(url_matches_hosts(url, PORTAL_HOSTS), url)
        for url in bad:
            self.assertFalse(url_matches_hosts(url, PORTAL_HOSTS), url)

    def test_session_selection_prefers_real_host_not_query_impostor(self):
        browser = object.__new__(Browser)
        browser._session_order = ["real", "fake"]
        browser._sessions = {
            "real": {"type": "page", "url": "https://visualsupport.microsoft.com/"},
            "fake": {"type": "page", "url": "https://evil.example/?q=visualsupport.microsoft.com"},
        }
        browser.page_sessions = lambda: ["real", "fake"]
        self.assertEqual("real", browser.active_session(PORTAL_HOSTS))

    def test_edge_profile_argument_is_matched_as_an_exact_path(self):
        profile = os.path.join(PROJECT_ROOT, "BrowserProfile")
        command = '"msedge.exe" "--user-data-dir=%s" --type=gpu-process' % profile
        self.assertTrue(command_uses_profile(command, profile))
        self.assertFalse(command_uses_profile(command, profile + "Backup"))

    def test_classifier_rejects_iid_form_outside_portal(self):
        portal = MicrosoftActivationPortal(None, _Logger(), SimpleNamespace())
        for url in (
                "https://evil.example/",
                "https://support.microsoft.com/",
                "https://visualsupport.microsoft.com.evil.example/"):
            snapshot = PageSnapshot(url, "", "Installation ID", [_iid_field()])
            self.assertEqual(STATE_UNKNOWN, portal.classify(snapshot), url)

    def test_classifier_keeps_official_iid_and_login_states(self):
        portal = MicrosoftActivationPortal(None, _Logger(), SimpleNamespace())
        official = PageSnapshot(
            "https://visualsupport.microsoft.com/activate", "",
            "Installation ID", [_iid_field()])
        login = PageSnapshot("https://login.live.com/", "Sign in", "", [])
        self.assertEqual(STATE_IID_ENTRY, portal.classify(official))
        self.assertEqual(STATE_LOGIN, portal.classify(login))

    def test_live_click_and_fill_guards_block_external_page(self):
        browser = _ActionBrowser({"evil": "https://evil.example/"})
        portal = MicrosoftActivationPortal(browser, _Logger(), SimpleNamespace())
        element = _iid_field("evil")
        with self.assertRaises(SafetyViolation):
            portal._click(element, "test control")
        with self.assertRaises(SafetyViolation):
            portal._fill(element, "123", "test field")
        self.assertEqual(0, browser.clicks)
        self.assertEqual(0, browser.fills)

    def test_iid_fill_blocks_cross_origin_frame(self):
        browser = _ActionBrowser({
            "page": "https://visualsupport.microsoft.com/activate",
        })
        portal = MicrosoftActivationPortal(browser, _Logger(), SimpleNamespace())
        snapshot = PageSnapshot(
            "https://visualsupport.microsoft.com/activate", "",
            "Installation ID", [_iid_field(
                "page", document_url="https://support.microsoft.com/")])
        iid = SimpleNamespace(digits="123456" * 7, groups=["123456"] * 7)
        with self.assertRaises(SafetyViolation):
            portal._insert_iid(snapshot, iid)
        self.assertEqual(0, browser.fills)

    def test_official_element_click_and_fill_delegate(self):
        browser = _ActionBrowser({
            "page": "https://visualsupport.microsoft.com/activate",
        })
        portal = MicrosoftActivationPortal(browser, _Logger(), SimpleNamespace())
        element = _iid_field(
            "page", document_url="https://visualsupport.microsoft.com/activate")
        portal._click(element, "safe control")
        portal._fill(element, "123", "safe field")
        self.assertEqual(1, browser.clicks)
        self.assertEqual(1, browser.fills)

    def test_login_host_is_observable_but_never_automated(self):
        browser = _ActionBrowser({"page": "https://account.live.com/"})
        portal = MicrosoftActivationPortal(browser, _Logger(), SimpleNamespace())
        element = _iid_field(
            "page", document_url="https://account.live.com/proofs/")
        with self.assertRaises(SafetyViolation):
            portal._click(element, "login control")
        with self.assertRaises(SafetyViolation):
            portal._fill(element, "secret", "login field")
        self.assertEqual(0, browser.clicks)
        self.assertEqual(0, browser.fills)

    def test_guarded_fill_uses_trusted_frame_url_and_isolated_world(self):
        class CdpSpy:
            def __init__(self):
                self.calls = []

            def send(self, method, params, session_id=None, timeout=None):
                self.calls.append((method, params, session_id))
                if method == "Page.createIsolatedWorld":
                    return {"executionContextId": 42}
                if method == "Page.getFrameTree":
                    return {"frameTree": {"frame": {
                        "id": "frame",
                        "url": "https://visualsupport.microsoft.com/activate",
                    }}}
                if method == "DOM.resolveNode":
                    return {"object": {"objectId": "isolated-node"}}
                if method == "Runtime.callFunctionOn":
                    return {"result": {"value": True}}
                raise AssertionError("unexpected CDP method: %s" % method)

        browser = object.__new__(Browser)
        browser.cdp = CdpSpy()
        browser.logger = _Logger()
        self.assertTrue(browser.fill_if_host_allowed(
            _browser_element(), "123456", PORTAL_HOSTS))

        methods = [call[0] for call in browser.cdp.calls]
        self.assertEqual([
            "Page.createIsolatedWorld", "Page.getFrameTree",
            "DOM.resolveNode", "Runtime.callFunctionOn",
        ], methods)
        resolve = browser.cdp.calls[2][1]
        self.assertEqual(42, resolve["executionContextId"])
        runtime = browser.cdp.calls[3][1]
        self.assertIn("ownerDocument", runtime["functionDeclaration"])
        self.assertEqual("123456", runtime["arguments"][0]["value"])

    def test_guarded_action_fails_before_resolving_foreign_frame(self):
        class CdpSpy:
            def __init__(self):
                self.methods = []

            def send(self, method, params, session_id=None, timeout=None):
                self.methods.append(method)
                if method == "Page.createIsolatedWorld":
                    return {"executionContextId": 42}
                if method == "Page.getFrameTree":
                    return {"frameTree": {"frame": {
                        "id": "frame", "url": "https://support.microsoft.com/",
                    }}}
                raise AssertionError("foreign frame must not be resolved or acted on")

        browser = object.__new__(Browser)
        browser.cdp = CdpSpy()
        browser.logger = _Logger()
        self.assertFalse(browser.click_if_host_allowed(
            _browser_element(), PORTAL_HOSTS))
        self.assertEqual(
            ["Page.createIsolatedWorld", "Page.getFrameTree"],
            browser.cdp.methods)

    def test_related_sessions_follow_only_target_parent_chain(self):
        browser = object.__new__(Browser)
        browser._sync_sessions = lambda timeout=0: None
        browser._session_order = ["page", "child", "unrelated", "nested", "orphan"]
        browser._sessions = {
            "page": {"type": "page", "targetId": "page-target"},
            "child": {
                "type": "iframe", "targetId": "child-target",
                "parentId": "page-target",
            },
            "nested": {
                "type": "iframe", "targetId": "nested-target",
                "parentId": "child-target",
            },
            "unrelated": {
                "type": "iframe", "targetId": "other-frame",
                "parentId": "other-page",
            },
            "orphan": {"type": "iframe", "targetId": "orphan-frame"},
        }
        self.assertEqual(
            ["page", "child", "nested"], browser.related_sessions("page"))

    def test_accessibility_scan_inherits_and_filters_each_node_frame(self):
        class CdpSpy:
            def __init__(self):
                self.methods = []

            def send(self, method, params, session_id=None, timeout=None):
                self.methods.append(method)
                if method == "Page.getFrameTree":
                    return {"frameTree": {
                        "frame": {
                            "id": "portal-frame",
                            "url": "https://visualsupport.microsoft.com/",
                        },
                        "childFrames": [
                            {"frame": {
                                "id": "login-frame",
                                "url": "https://login.live.com/",
                            }},
                            {"frame": {
                                "id": "foreign-frame",
                                "url": "https://support.microsoft.com/",
                            }},
                        ],
                    }}
                if method == "Accessibility.getFullAXTree":
                    def node(node_id, backend_id, frame_id=None, parent_id=None):
                        result = {
                            "nodeId": node_id,
                            "backendDOMNodeId": backend_id,
                            "role": {"value": "textbox"},
                            "name": {"value": "field-%d" % backend_id},
                        }
                        if frame_id is not None:
                            result["frameId"] = frame_id
                        if parent_id is not None:
                            result["parentId"] = parent_id
                        return result
                    return {"nodes": [
                        node("portal-root", 1, "portal-frame"),
                        node("portal-field", 2, parent_id="portal-root"),
                        node("login-root", 3, "login-frame"),
                        node("login-field", 4, parent_id="login-root"),
                        node("foreign-root", 5, "foreign-frame"),
                        node("foreign-field", 6, parent_id="foreign-root"),
                        node("unknown-field", 7, parent_id="missing-parent"),
                    ]}
                raise AssertionError("unexpected CDP method: %s" % method)

        browser = object.__new__(Browser)
        browser._sessions = {
            "portal": {"type": "page", "url": "https://visualsupport.microsoft.com/"},
        }
        browser.related_sessions = lambda session_id: ["portal"]
        browser.cdp = CdpSpy()
        browser.logger = _Logger()
        elements = browser.elements(
            "portal", allowed_hosts=PORTAL_HOSTS + LOGIN_HOSTS)
        self.assertEqual([1, 2, 3, 4], [e.backend_node_id for e in elements])
        self.assertEqual(
            ["portal-frame", "portal-frame", "login-frame", "login-frame"],
            [e.frame_id for e in elements])
        self.assertEqual(
            ["Page.getFrameTree", "Accessibility.getFullAXTree"],
            browser.cdp.methods)

    def test_snapshot_does_not_inspect_untrusted_tab(self):
        class BrowserSpy:
            def active_session(self, prefer_hosts=()):
                return "page"

            def session_url(self, session_id):
                return "https://evil.example/"

            def page_title(self, session_id):
                raise AssertionError("title inspected")

            def page_text(self, session_id, limit):
                raise AssertionError("text inspected")

            def elements(self, session_id, allowed_hosts=()):
                raise AssertionError("elements inspected")

        portal = MicrosoftActivationPortal(BrowserSpy(), _Logger(), SimpleNamespace())
        snapshot = portal.snapshot()
        self.assertEqual([], snapshot.elements)
        self.assertEqual("", snapshot.text)

    def test_ax_frame_inheritance_rejects_cycles_and_missing_parents(self):
        nodes = [
            {"nodeId": "root", "frameId": "main"},
            {"nodeId": "child", "parentId": "root"},
            {"nodeId": "foreign", "frameId": "other", "parentId": "root"},
            {"nodeId": "nested", "parentId": "foreign"},
            {"nodeId": "a", "parentId": "b"},
            {"nodeId": "b", "parentId": "a"},
            {"nodeId": "orphan", "parentId": "missing"},
        ]
        self.assertEqual(["main", "main", "other", "other", "", "", ""],
                         Browser._ax_frame_ids(nodes, "main"))


if __name__ == "__main__":
    unittest.main()
