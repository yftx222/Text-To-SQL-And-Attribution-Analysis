"""在独立的临时 Docker 容器中运行生产 Shell 包装程序。"""

import os
import subprocess

import pytest

from dataagent.engines.sandbox.scripts import _SHELL_JOB_WRAPPER_SCRIPT


def test_shell_limits_on_disposable_docker():
    if not os.getenv("DATAAGENT_TEST_SSH_DOCKER"):
        pytest.skip("requires project SSH Docker host")
    driver = """
import base64, json, os, pathlib, subprocess, sys, time
root = pathlib.Path("/data")
workspace = root / "work"
stage = root / "stage"
workspace.mkdir()
stage.mkdir()
os.chown(workspace,10001,10001)
evidence = root / "original.csv"
evidence.write_text("206")
evidence.chmod(0o440)
def run(job, command, timeout=10, quota=10000000):
    args = dict(workspace=str(workspace), staging=str(stage), job_id=job, command=command,
                owner_uid=10001,owner_gid=10001,file_mode=0o640,directory_mode=0o750,
                umask=0o027,max_file_bytes=1048576,volume_root="/data",timeout_seconds=timeout,storage_bytes=quota)
    blob = base64.b64encode(json.dumps(args).encode()).decode()
    subprocess.run([sys.executable, "-c", WRAPPER, blob], timeout=20, check=False)
    return json.loads((stage/"shell_jobs"/(job+".json")).read_text())
file = run("large","dd if=/dev/zero of=large bs=65536 count=24")
assert file["exit_code"] != 0 and (workspace/"large").stat().st_size <= 1048576, file
deadline = run("slow","(sleep 3 && touch escaped) & wait",timeout=1)
assert deadline["status"] == "failed" and "Timeout" in deadline["error"],deadline
time.sleep(3)
assert not (workspace/"escaped").exists()
quota = run("quota","dd if=/dev/zero of=another bs=65536 count=16",quota=1500000)
assert quota["status"] == "failed" and "空间额度" in quota["error"], quota
assert evidence.read_text()=="206"
print("Shell resource checks passed")
"""
    script = "WRAPPER = " + repr(_SHELL_JOB_WRAPPER_SCRIPT) + "\n" + driver
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        "dataagent-vm",
        "docker run --rm -i --network none --read-only --tmpfs /data:rw,size=32m --user 0 --entrypoint python3 dataagent-sandbox:latest -",
    ]
    result = subprocess.run(
        command, input=script.encode(), capture_output=True, timeout=80, check=False
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert b"Shell resource checks passed" in result.stdout
