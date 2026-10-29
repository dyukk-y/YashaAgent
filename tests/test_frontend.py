import re
import shutil
import subprocess
from pathlib import Path

import pytest


def test_mini_app_inline_javascript_syntax(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed; skipping browser JavaScript syntax check")

    html = (Path(__file__).resolve().parents[1] / "webapp" / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, flags=re.IGNORECASE | re.DOTALL)
    inline_scripts = [script for script in scripts if script.strip()]
    assert inline_scripts, "Expected an inline Mini App script"

    for index, script in enumerate(inline_scripts):
        script_path = tmp_path / f"mini-app-{index}.js"
        script_path.write_text(script, encoding="utf-8")
        result = subprocess.run([node, "--check", str(script_path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr or result.stdout
