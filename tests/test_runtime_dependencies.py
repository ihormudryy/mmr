from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_docker_requirements_match_project_runtime_dependencies():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    declared = set(project["project"]["dependencies"])
    docker_requirements = {
        line.strip()
        for line in (ROOT / "requirements.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert docker_requirements == declared
