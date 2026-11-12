import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from app import config
from tests.test_api import ApiTestBase
from tools.runtime import delegate


class RuntimeDelegationTest(ApiTestBase):
    def processes(self, *, running=True, owned=True):
        mount = {"Type": "bind", "Destination": "/app/data",
                 "Source": str(config.DATA_DIR if owned else config.DATA_DIR / "other")}
        return [subprocess.CompletedProcess([],0,"container-id\n",""),
                subprocess.CompletedProcess([],0,json.dumps({"running":running,"mounts":[mount]}),""),
                subprocess.CompletedProcess([],0)]

    def test_live_maintenance_runs_in_same_container_and_keeps_arguments(self):
        with patch.object(Path,"is_file",return_value=False), patch("tools.runtime.subprocess.run",side_effect=self.processes()) as run:
            self.assertTrue(delegate("maintenance.py", ["--repair"]))
        self.assertEqual(run.call_args.args[0], ["docker","exec","-i","picwall","python","/app/tools/maintenance.py","--repair"])

    def test_stopped_or_different_data_directory_remains_local(self):
        for options in ({"running":False},{"owned":False}):
            with patch.object(Path,"is_file",return_value=False), patch("tools.runtime.subprocess.run",side_effect=self.processes(**options)) as run:
                self.assertFalse(delegate("backup.py"))
                self.assertEqual(run.call_count,2)

    def test_docker_unavailable_does_not_assume_service_is_stopped(self):
        error=subprocess.CalledProcessError(1,["docker","ps"])
        with (patch.object(Path,"is_file",return_value=False),
              patch("tools.runtime.subprocess.run",side_effect=error),
              self.assertRaisesRegex(RuntimeError,"停止本次维护")):
            delegate("backup.py")

    def test_exec_failure_does_not_fall_back_to_host(self):
        steps=self.processes()[:2]+[subprocess.CalledProcessError(1,["docker","exec"])]
        with patch.object(Path,"is_file",return_value=False), patch("tools.runtime.subprocess.run",side_effect=steps) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                delegate("backup.py")
            self.assertEqual(run.call_count,3)

    def test_container_never_recursively_delegates(self):
        with patch.object(Path,"is_file",return_value=True), patch("tools.runtime.subprocess.run") as run:
            self.assertFalse(delegate("backup.py"))
            run.assert_not_called()
