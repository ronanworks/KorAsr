"""scripts/service_ctl.py: the parts that do not touch processes, ports or the network."""

import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch

_SPEC = importlib.util.spec_from_file_location(
    "service_ctl", Path(__file__).resolve().parent.parent / "scripts" / "service_ctl.py"
)
ctl = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ctl)

SERVE_STATUS = """\
# Funnel on:
#     - https://4090ai.tail1447d0.ts.net:10000
#     - https://4090ai.tail1447d0.ts.net:8443

https://4090ai.tail1447d0.ts.net:10000 (Funnel on)
|-- / proxy http://127.0.0.1:8768

https://4090ai.tail1447d0.ts.net:8443 (Funnel on)
|-- / proxy https+insecure://localhost:8000

https://4090ai.tail1447d0.ts.net:8444 (tailnet only)
|-- / proxy https+insecure://localhost:8000

https://4090ai.tail1447d0.ts.net:8600 (tailnet only)
|-- / proxy http://127.0.0.1:8600
"""


class ServiceCtlTests(unittest.TestCase):
    def test_only_entries_forwarding_to_the_service_port_are_listed(self):
        with patch.object(ctl, "PORT", 8000):
            urls = ctl.tailscale_urls(SERVE_STATUS)
        self.assertEqual(urls, [
            ("https://4090ai.tail1447d0.ts.net:8443", True),
            ("https://4090ai.tail1447d0.ts.net:8444", False),
        ])
        self.assertEqual(ctl.tailscale_urls(""), [])

    def test_last_run_reads_only_the_latest_start(self):
        log = (
            "\n===== service start @ 2026-10-05T10:00:00.1 =====\n"
            "10:00:05.000 Traceback (most recent call last):\n"
            "\n===== service start @ 2026-10-05T17:15:46.747164 =====\n"
            "17:15:57.687 [Startup] Initializing database...\n"
            "17:16:03.017 [Startup] Ready\n"
            "17:20:00.000 [Polish] LLM attempt 1 failed after 45.00s: TimeoutError\n"
        )
        run = ctl.last_run(log)
        self.assertEqual(run["started"], "2026-10-05T17:15:46.747164")
        self.assertTrue(run["ready"])
        self.assertEqual(len(run["errors"]), 1)          # the old run's traceback is not counted
        self.assertFalse(ctl.last_run("17:00 [Startup] Initializing database...")["ready"])

    def test_only_korasr_processes_count_as_the_service(self):
        pythonw = r"C:\Users\4090\.conda\envs\korasr\pythonw.exe"
        self.assertTrue(ctl.is_korasr({"path": pythonw, "cmd": f'"{pythonw}" start.py --host 127.0.0.1 --port 8000'}))
        self.assertFalse(ctl.is_korasr({"path": r"C:\Program Files\Other\server.exe", "cmd": "server.exe"}))
        self.assertFalse(ctl.is_korasr({"path": pythonw, "cmd": f'"{pythonw}" other_tool.py'}))
        self.assertFalse(ctl.is_korasr(None))
        # Another machine: the venv named in service-env.local.cmd counts, other Pythons do not.
        venv = r"E:\KorASR\venv\Scripts\pythonw.exe"
        with patch.dict(os.environ, {"KORASR_PYW": venv, "KORASR_PY": r"E:\KorASR\venv\Scripts\python.exe"}):
            self.assertTrue(ctl.is_korasr({"path": venv, "cmd": f'"{venv}" start.py --port 8100'}))
            korstudy = r"C:\Users\A8000\AppData\Local\Programs\Python\Python312\python.exe"
            self.assertFalse(ctl.is_korasr({"path": korstudy, "cmd": f'"{korstudy}" -m uvicorn korstudy.web.app'}))
            # venv\Scripts\pythonw.exe is a launcher; the base interpreter it starts holds the port.
            base = r"C:\Users\A8000\AppData\Local\Programs\Python\Python312\pythonw.exe"
            self.assertTrue(ctl.is_korasr({"path": base, "cmd": f'"{base}" start.py --port 8100', "parent_path": venv}))
            self.assertFalse(ctl.is_korasr({"path": base, "cmd": f'"{base}" start.py', "parent_path": r"C:\Windows\explorer.exe"}))

    def test_database_path_follows_the_service_environment(self):
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite:///./data/korasr-local-test.db"}):
            self.assertEqual(ctl.database_path(), ctl.ROOT / "data" / "korasr-local-test.db")
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://example/db"}):
            self.assertIsNone(ctl.database_path())


if __name__ == "__main__":
    unittest.main()
