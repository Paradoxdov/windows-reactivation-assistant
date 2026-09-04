import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from waa.logger import scrub
from waa.portal import MicrosoftActivationPortal, PageSnapshot, _safe_url
from waa.windows_license import WindowsLicense


class LoggingPrivacyTests(unittest.TestCase):
    def test_scrubs_identity_and_local_user_path(self):
        text = "user@example.com C:\\Users\\Alice Example\\Desktop\\tool"
        cleaned = scrub(text)
        self.assertNotIn("user@example.com", cleaned)
        self.assertNotIn("Alice Example", cleaned)
        self.assertIn("C:\\Users\\[REDACTED-USER]\\Desktop", cleaned)
        self.assertNotIn("user%40example.com", scrub("user%40example.com"))
        self.assertNotIn("user%40example%2Ecom", scrub("user%40example%2Ecom"))
        self.assertNotIn("Alice", scrub("C:/Users/Alice/Desktop/tool"))
        self.assertNotIn("Alice", scrub("C:\\Users\\Alice"))
        self.assertNotIn(
            "Alice Example", scrub("Profile: C:\\Users\\Alice Example"))

    def test_scrubs_partial_key_but_not_unrelated_five_char_value(self):
        cleaned = scrub("Partial Key: ABC12; build ABC12")
        self.assertNotIn("Partial Key: ABC12", cleaned)
        self.assertIn("build ABC12", cleaned)
        self.assertNotIn("ABC12", scrub('"PartialKey":"ABC12"'))
        self.assertNotIn("ABC12", scrub("partial_key=ABC12"))

    def test_scrubs_product_key_regardless_of_case(self):
        key = "abcde-Fg123-hijkl-MN456-opqrs"
        self.assertNotIn(key, scrub(key))

    def test_scrubs_six_and_seven_digit_grouped_iids(self):
        six = " ".join(["123456"] * 9)
        seven = "-".join(["1234567"] * 9)
        self.assertNotIn("123456", scrub(six))
        self.assertNotIn("1234567", scrub(seven))
        wrapped = ("123456\n" * 8) + "123456"
        windows_wrapped = "\r\n".join(["123456"] * 9)
        spaced = "  ".join(["123456"] * 9)
        unicode_dash = "\u2013".join(["1234567"] * 9)
        self.assertNotIn("123456", scrub(wrapped))
        self.assertNotIn("123456", scrub(windows_wrapped))
        self.assertNotIn("123456", scrub(spaced))
        self.assertNotIn("1234567", scrub(unicode_dash))
        self.assertEqual("2026-09-03 build 26100", scrub("2026-09-03 build 26100"))

    def test_scrubs_auth_headers_and_json_tokens_completely(self):
        cleaned = scrub(
            'Authorization: Bearer super.secret.token\n'
            '"access_token": "abc123" sessionId=deadbeef session_state=qwerty')
        for secret in ("super.secret.token", "abc123", "deadbeef", "qwerty"):
            self.assertNotIn(secret, cleaned)
        quoted = scrub(
            '"authorization":"Basic YWJjMTIz" "cookie":"opaque=value"')
        self.assertNotIn("YWJjMTIz", quoted)
        self.assertNotIn("opaque=value", quoted)

    def test_scrubs_url_query_and_safe_url_drops_query_and_fragment(self):
        url = "https://visualsupport.microsoft.com/activate?code=secret#account"
        self.assertNotIn("secret", scrub(url))
        self.assertEqual(
            "https://visualsupport.microsoft.com/activate", _safe_url(url))

    def test_windows_summary_never_exposes_partial_key(self):
        license_info = WindowsLicense({
            "LicenseStatus": 1,
            "PartialKey": "ABC12",
        })
        summary = dict(license_info.summary_lines())
        self.assertEqual("PRESENT (value withheld)", summary["Partial Key"])
        self.assertNotIn("ABC12", repr(summary))

    def test_safe_url_removes_userinfo(self):
        self.assertEqual("https://example.com/path", _safe_url(
            "https://username:password@example.com/path?token=secret#fragment"))

    def test_page_dump_scrubs_field_value_and_email(self):
        class CapturingLogger:
            def __init__(self):
                self.messages = []

            def debug(self, message):
                self.messages.append(scrub(message))

        logger = CapturingLogger()
        portal = MicrosoftActivationPortal(None, logger, object())
        element = type("Element", (), {
            "role": "textbox",
            "name": "Account user@example.com value secret-value",
            "value": "secret-value",
        })()
        portal._dump_page(PageSnapshot(
            "https://visualsupport.microsoft.com/activate?code=secret",
            "Activation", "", [element]))
        output = "\n".join(logger.messages)
        self.assertNotIn("user@example.com", output)
        self.assertNotIn("secret-value", output)
        self.assertNotIn("code=secret", output)


if __name__ == "__main__":
    unittest.main()
