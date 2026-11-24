import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from evals import profile


@pytest.mark.parametrize(
    ("username", "role", "admin"),
    [
        ("other", "reader", False),
        ("evaluation", "other", False),
        ("evaluation", "reader", True),
    ],
)
def test_profile_access_precedes_database(monkeypatch, username, role, admin):
    monkeypatch.setattr(
        profile,
        "deployment_inputs",
        lambda: {"git": "abc", "source": "code", "config": "cfg"},
    )
    monkeypatch.setattr(profile, "local_inputs", dict)
    target = profile.EvaluationProfile("evaluation", "reader", "fixture_db", "v1")
    user = SimpleNamespace(username=username, doris_role_name=role, is_admin=admin)
    with pytest.raises(HTTPException) as rejected:
        asyncio.run(target.read(user, None))
    assert rejected.value.status_code == 403


def test_static_resource_digest_depends_on_names_and_contents(tmp_path):
    path = tmp_path / "prompt.md"
    path.write_text("first", encoding="utf8")
    initial = profile.tree(tmp_path, [path])
    path.write_text("new instructions", encoding="utf8")
    changed = profile.tree(tmp_path, [path])
    renamed = path.rename(tmp_path / "planner.md")
    assert len({initial, changed, profile.tree(tmp_path, [renamed])}) == 3
