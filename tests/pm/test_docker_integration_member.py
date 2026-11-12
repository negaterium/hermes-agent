"""Docker PM probes include the deployment-owned integration member."""
import json

from pm import install
from pm.plugin_inputs import Members


def _docker_project(root, *, distribution="docker"):
    member = root / "deployment" / "darkserver" / "integration-member"
    member.mkdir(parents=True)
    (member / "pyproject.toml").write_text(
        '[project]\nname="darkserver-integration-test"\nversion="0.1.0"\n'
        'dependencies=["example-dependency==1.0"]\n'
        '[tool.uv]\npackage=false\n',
        encoding="utf-8",
    )
    (root / "install-stamp.json").write_text(
        json.dumps({"distribution": distribution, "updateMechanism": "external"}),
        encoding="utf-8",
    )
    return member


def test_docker_implicit_member_inputs_include_deployment_integrations(tmp_path):
    root = tmp_path / "payload"
    root.mkdir()
    member = _docker_project(root)

    assert install._member_inputs(None, project_root=root) == {"plugin_dirs": [member]}


def test_other_install_types_and_explicit_members_keep_their_selection(tmp_path):
    root = tmp_path / "payload"
    root.mkdir()
    _docker_project(root, distribution="source")

    assert install._member_inputs(None, project_root=root) == {}
    assert install._member_inputs(Members([]), project_root=root) == {"plugin_dirs": []}
