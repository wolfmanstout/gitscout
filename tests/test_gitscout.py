from pathlib import Path

import git
import pytest
from click.testing import CliRunner

from gitscout.cli import DocGenerator, cli


def test_version():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert result.output.startswith("cli, version ")


@pytest.fixture
def test_repo(tmp_path):
    """Create a test repository with some sample files."""
    repo_path = tmp_path / "test_repo"
    repo_path.mkdir()

    # Create some files and directories
    (repo_path / "src").mkdir()
    (repo_path / "src/main.py").write_text("def main():\n    pass")
    (repo_path / "README.md").write_text("# Test Repo")

    # Initialize git repo with a remote
    repo = git.Repo.init(repo_path)
    repo.create_remote("origin", "https://github.com/test/test_repo.git")

    return repo_path


def test_prompt_construction(test_repo, tmp_path):
    generator = DocGenerator(test_repo, tmp_path / "output")

    # Add some dummy generated readmes
    generated_readmes = {
        test_repo / "src": "## Source Code\nContains main application code."
    }

    # Test prompt construction
    dirs = [test_repo / "src"]
    files = [test_repo / "README.md"]
    prompt = generator._build_prompt(test_repo, dirs, files, generated_readmes)

    expected_prompt = f"""Current directory: {test_repo}

=====

Subdirectories:
{test_repo / "src"}

=====

Files:
{test_repo / "README.md"}
---
# Test Repo

---

=====

Previously generated documentation:

{Path("src/README.md")}:
## Source Code
Contains main application code.
---
"""

    assert prompt == expected_prompt


def test_system_prompt_construction(test_repo, tmp_path):
    generator = DocGenerator(test_repo, tmp_path / "output")

    system_prompt = generator._build_system_prompt(is_repo_root=True)

    expected_system = (
        "Provide an overview of what this directory does in Markdown, "
        "including a summary of each subdirectory and file, starting with "
        "the subdirectories. "
        "Omit heading level 1 (#) as it will be added automatically. "
        "If adding links to previously generated documentation, use the "
        "relative path to the file from the *current* directory, not the "
        "repo root. "
        "Link any files mentioned to an absolute URL starting with "
        "https://github.com/test/test_repo/blob/main/ followed by the relative file path. "
        "Begin with an overall description of the repository. List the "
        "dependencies and how they are used."
    )

    assert system_prompt == expected_system
