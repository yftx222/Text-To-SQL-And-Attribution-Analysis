"""远程 Docker 目标和只读挂载的主机路径必须明确分开。"""

from pathlib import PurePosixPath
from unittest.mock import MagicMock, patch

import pytest

from dataagent.engines.sandbox.contracts import SandboxReadonlyMount
from dataagent.engines.sandbox.runtime import DockerRuntime
from tests.sandbox.fakes import FakeSandboxOwnership, build_sandbox_config


def test_remote_target_does_not_use_local_docker_environment(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "npipe:////./pipe/dockerDesktopLinuxEngine")
    runtime = DockerRuntime(
        build_sandbox_config(docker_host="ssh://dataagent-vm"), FakeSandboxOwnership()
    )
    client = MagicMock()
    client.images.get.return_value.id = "sha256:test"
    with (
        patch(
            "dataagent.engines.sandbox.runtime.docker.DockerClient", return_value=client
        ) as create,
        patch.object(runtime, "reconcile"),
    ):
        runtime.initialize()
    create.assert_called_once_with(
        base_url="ssh://dataagent-vm",
        version="auto",
        use_ssh_client=False,
        num_pools=3,
        max_pool_size=2,
    )
    runtime.close()


def test_readonly_mount_uses_remote_source_but_keeps_local_files(tmp_path):
    mount = SandboxReadonlyMount(
        source=tmp_path,
        target=PurePosixPath("/skills/analyst"),
        docker_source=PurePosixPath("/opt/dataagent/skills"),
    )
    runtime = DockerRuntime(
        build_sandbox_config(), FakeSandboxOwnership(), readonly_mounts=(mount,)
    )
    assert mount.source == tmp_path.resolve()
    assert runtime._readonly_mount_volumes() == {
        "/opt/dataagent/skills": {"bind": "/skills/analyst", "mode": "ro"}
    }


@pytest.mark.parametrize("source", ["relative/skills", "/", "/opt/../skills"])
def test_remote_source_rejects_ambiguous_paths(tmp_path, source):
    with pytest.raises(ValueError, match="Docker 主机"):
        SandboxReadonlyMount(
            source=tmp_path,
            target=PurePosixPath("/skills/analyst"),
            docker_source=PurePosixPath(source),
        )
