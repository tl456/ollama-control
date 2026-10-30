"""Keep self-authored public attribution on the selected GitHub handle.

These positive field checks do not embed previous identities or an encoded
blocklist. Upstream author and license text is outside this attribution scope.
"""
import ast
import re
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
AUTHOR = "tl456"


def text(relative):
    return (SOURCE / relative).read_text(encoding="utf-8")


class PublicIdentityTests(unittest.TestCase):
    def test_own_mit_copyrights_use_public_handle(self):
        for relative in ("LICENSE", "plugins/examples/time_demo/LICENSE"):
            with self.subTest(file=relative):
                matches = re.findall(r"^Copyright \(c\) \d{4}(?:-\d{4})? (.+)$", text(relative), re.MULTILINE)
                self.assertEqual(matches, [AUTHOR])

    def test_public_readme_and_own_notice_attribution(self):
        self.assertEqual(re.findall(r"作者：\*\*([^*\r\n]+)\*\*", text("README.md")), [AUTHOR])
        self.assertEqual(re.findall(r"版权属于([^。\s]+)及相应贡献者", text("THIRD_PARTY_NOTICES.md")), [AUTHOR])

    def test_ui_and_own_tool_documentation_use_public_handle(self):
        self.assertEqual(re.findall(r'class="about-author">作者：([^<]+)</p>', text("web/workspace.js")), [AUTHOR])
        self.assertIn("作者" + AUTHOR + "。", text("plugins/examples/time_demo/README.md"))
        for tool in ("read_attachment", "python_run", "list_directory", "file_info", "search_files", "system_time", "read_file"):
            with self.subTest(tool=tool):
                self.assertIn("自有代码MIT，" + AUTHOR + "。", text("app/toolkit/" + tool + "/README.md"))

    def test_windows_publisher_and_resource_use_public_handle(self):
        installer = text("packaging/installer.iss")
        self.assertEqual(re.findall(r"^AppPublisher=(.+)$", installer, re.MULTILINE), [AUTHOR])
        self.assertEqual(re.findall(r"^AppCopyright=Copyright \(c\) \d{4}(?:-\d{4})? (\S+)\. MIT License\.$", installer, re.MULTILINE), [AUTHOR])
        fields = {}
        for node in ast.walk(ast.parse(text("packaging/version.txt"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "StringStruct" and len(node.args) == 2:
                fields[ast.literal_eval(node.args[0])] = ast.literal_eval(node.args[1])
        self.assertEqual(fields["CompanyName"], AUTHOR)
        self.assertRegex(fields["LegalCopyright"], r"^Copyright \(c\) \d{4}(?:-\d{4})? " + re.escape(AUTHOR) + r"\. MIT License\.$")

    def test_windows_application_identity_uses_public_handle(self):
        identities = []
        for node in ast.walk(ast.parse(text("main.py"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "SetCurrentProcessExplicitAppUserModelID":
                self.assertEqual(len(node.args), 1)
                identities.append(ast.literal_eval(node.args[0]))
        self.assertEqual(identities, [AUTHOR + ".LocalAIWorkbench"])


if __name__ == "__main__":
    unittest.main()
