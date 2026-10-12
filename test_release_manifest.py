import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import build_github_release


class ReleaseManifestTests(unittest.TestCase):
    def test_full_package_url_points_to_versioned_release_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "release"

            def fake_pyinstaller(arguments, check):
                dist = Path(arguments[arguments.index("--distpath") + 1])
                app = dist / "小说处理中心"
                app.mkdir(parents=True)
                (app / "小说处理中心.exe").write_bytes(b"synthetic exe")

            with patch.object(build_github_release.subprocess, "run", side_effect=fake_pyinstaller):
                with patch.object(sys, "argv", ["build_github_release.py", "--version", "1.0.24", "--output", str(output)]):
                    build_github_release.main()

            manifest = json.loads((output / "latest.json").read_text(encoding="utf-8"))

        self.assertEqual(
            manifest["url"],
            "https://luotuoqiluotuozhaoma-download.oss-cn-beijing.aliyuncs.com/"
            "updates/novel/releases/1.0.24/novel-processing-center-1.0.24-update.zip",
        )
        self.assertEqual(
            manifest["fullPackageUrl"],
            "https://luotuoqiluotuozhaoma-download.oss-cn-beijing.aliyuncs.com/"
            "updates/novel/releases/1.0.24/novel-processing-center-1.0.24-windows-x64.zip",
        )


if __name__ == "__main__":
    unittest.main()
