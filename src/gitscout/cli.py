import os
import shutil
import textwrap
import webbrowser
from datetime import datetime
from pathlib import Path

import click
import git
import llm
from mkdocs.commands.serve import serve as mkdocs_serve


class DocGenerator:
    def __init__(
        self,
        repo_path: Path,
        output_path: Path,
        model_name: str | None = None,
        count_tokens: bool = False,
    ):
        self.repo_path = repo_path
        self.output_path = output_path
        self.repo = git.Repo(repo_path)
        self.model = llm.get_model(model_name) if model_name else llm.get_model()
        self.count_tokens = count_tokens
        self.total_tokens = 0
        # Parse repo URL from self.repo remote, handling both HTTPS and SSH URLs
        self.repo_url = (
            self.repo.remote().url.replace(".git", "").replace("git@", "https://")
        )
        if "github.com" in self.repo_url:
            origin_refs = [
                ref for ref in self.repo.remote().refs if ref.remote_name == "origin"
            ]
            if origin_refs:
                default_branch = origin_refs[0].remote_head
                self.repo_url_file_prefix = f"{self.repo_url}/blob/{default_branch}/"
            else:
                self.repo_url_file_prefix = f"{self.repo_url}/blob/main/"
        else:
            self.repo_url_file_prefix = None

    def get_recent_changes(self, num_commits=5):
        commits = list(self.repo.iter_commits("main", max_count=num_commits))
        changes = []

        for commit in commits:
            # Get the diff for this commit
            diff = (
                commit.parents[0].diff(commit)
                if commit.parents
                else commit.diff(git.NULL_TREE)
            )

            # Extract modified files and their diffs (truncated)
            files_changed = []
            for d in diff:
                if d.a_path:
                    try:
                        # Get truncated diff content
                        diff_content = d.diff.decode("utf-8")[
                            :500
                        ]  # Truncate long diffs
                        files_changed.append({"path": d.a_path, "diff": diff_content})
                    except Exception as e:
                        print(f"Error processing diff for {d.a_path}: {e}")
                        continue

            changes.append(
                {
                    "hash": commit.hexsha[:8],
                    "message": commit.message,
                    "author": commit.author.name,
                    "date": datetime.fromtimestamp(commit.committed_date),
                    "files": files_changed,
                }
            )

        return changes

    def generate_changelog(self, changes):
        # Start a conversation for maintaining context
        response = self.model.prompt(
            """Generate a detailed changelog entry for the following git commits.
            Focus on user-facing changes and group similar changes together.
            Format the output in markdown with appropriate headers and bullet points.

            Commit details:
            """
            + str(changes),
            system="You are a technical writer creating clear, organized changelog entries.",
        )

        if self.count_tokens:
            self.total_tokens += response.usage().input or 0
            self.total_tokens += response.usage().output or 0

        return response.text()

    def _build_prompt(self, root, dirs, files, generated_readmes):
        prompt_parts = [f"Current directory: {root}\n"]

        if dirs:
            dir_list = "\n".join(str(d) for d in dirs)
            prompt_parts.append(f"Subdirectories:\n{dir_list}\n")

        if files:
            file_template = textwrap.dedent(
                """\
                {path}
                ---
                {content}

                ---
                """
            )
            file_contents = "".join(
                file_template.format(path=f, content=f.read_text()) for f in files
            )
            prompt_parts.append(f"Files:\n{file_contents}")

        if generated_readmes:
            readme_context = ""
            for subdir, content in generated_readmes.items():
                if subdir.is_relative_to(root):
                    rel_path = subdir.relative_to(self.repo_path) / "README.md"
                    readme_context += f"\n{rel_path}:\n"
                    readme_context += content
                    readme_context += "\n---\n"
            if readme_context:
                prompt_parts.append(
                    f"Previously generated documentation:\n{readme_context}"
                )

        return "\n=====\n\n".join(prompt_parts)

    def _build_system_prompt(self, is_repo_root):
        parts = [
            "Provide an overview of what this directory does in Markdown, "
            "including a summary of each subdirectory and file, starting with "
            "the subdirectories. "
            "Omit heading level 1 (#) as it will be added automatically. "
            "If adding links to previously generated documentation, use the "
            "relative path to the file from the *current* directory, not the "
            "repo root."
        ]
        if self.repo_url_file_prefix:
            parts.append(
                "Link any files mentioned to an absolute URL starting with "
                f"{self.repo_url_file_prefix} followed by the relative file path."
            )
        if is_repo_root:
            parts.append(
                "Begin with an overall description of the repository. List the "
                "dependencies and how they are used."
            )
        return " ".join(parts)

    def generate_docs(self):
        all_files = set(
            str(Path(f).resolve()) for f in self.repo.git.ls_files().splitlines()
        )
        resolved_repo_path = self.repo_path.resolve()
        all_directories = set(
            str(d)
            for f in all_files
            for d in Path(f).parents
            if d.is_relative_to(resolved_repo_path)
        )
        generated_readmes = {}
        for root, dirs, files in os.walk(self.repo_path, topdown=False):
            root = Path(root)
            resolved_root = root.resolve()
            if str(resolved_root) not in all_directories:
                continue
            is_repo_root = resolved_root == resolved_repo_path

            dirs = [root / Path(d) for d in dirs]
            dirs = [d for d in dirs if str(d.resolve()) in all_directories]
            files = [root / Path(f) for f in files]
            files = [f for f in files if str(f.resolve()) in all_files]

            prompt = self._build_prompt(root, dirs, files, generated_readmes)
            system_prompt = self._build_system_prompt(is_repo_root)

            response = self.model.prompt(
                prompt,
                system=system_prompt,
            )

            if self.count_tokens:
                self.total_tokens += response.usage().input or 0
                self.total_tokens += response.usage().output or 0

            output_path = self.output_path / "docs" / root / "README.md"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            dir_name = resolved_repo_path.name if is_repo_root else str(root)
            output_path.write_text(f"# {dir_name}\n\n{response.text()}")

            # Store the generated README
            generated_readmes[root] = response.text()

    def write_mkdocs_configuration(self):
        config_template = """\
            site_name: {repo_name} docs by gitscout
            theme: material
            exclude_docs: |
                !.*
                !/templates/
            hooks:
                - my_hooks.py
            repo_url: {repo_url}
            edit_uri: 
            """
        config_content = textwrap.dedent(
            config_template.format(
                repo_name=self.repo_path.resolve().name, repo_url=self.repo_url
            )
        )
        hooks_content = textwrap.dedent("""\
            import bleach
            from bleach_allowlist import markdown_tags, markdown_attrs

            def on_page_content(html, **kwargs):
                return bleach.clean(html, markdown_tags, markdown_attrs)
            """)
        Path(self.output_path / "mkdocs.yml").write_text(config_content)
        Path(self.output_path / "my_hooks.py").write_text(hooks_content)


@click.command()
@click.version_option()
@click.argument(
    "repo_path",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
@click.option("--model", help="LLM model to use (defaults to system default)")
@click.option(
    "--serve/--no-serve", default=False, help="Start local documentation server"
)
@click.option(
    "--open/--no-open",
    default=False,
    help="Open documentation in browser (implies --serve)",
)
@click.option("--port", default=8000, help="Port for local server")
@click.option("--gen/--no-gen", default=True, help="Generate documentation")
@click.option(
    "--count-tokens/--no-count-tokens", default=True, help="Count tokens used"
)
@click.option(
    "--output-path",
    type=click.Path(file_okay=False, dir_okay=True, writable=True, path_type=Path),
    default="generated_docs",
    help="Output directory for generated documentation",
)
@click.option(
    "--include-changelog/--no-include-changelog",
    default=False,
    help="Generate changelog from recent commits",
)
def cli(
    repo_path: Path,
    model: str,
    serve: bool,
    open: bool,
    port: int,
    gen: bool,
    count_tokens: bool,
    output_path: Path,
    include_changelog: bool,
):
    "Uses AI to help understand repositories and their changes."
    generator = DocGenerator(repo_path, output_path, model, count_tokens)
    if gen:
        # Remove existing generated docs
        if output_path.exists():
            shutil.rmtree(output_path)
        docs_path = output_path / "docs"
        docs_path.mkdir(parents=True)

        # Generate documentation
        generator.generate_docs()

        # Generate changelog only if requested
        if include_changelog:
            changes = generator.get_recent_changes()
            changelog = generator.generate_changelog(changes)
            Path(output_path / "docs/CHANGELOG.md").write_text(changelog)

        if count_tokens:
            if generator.total_tokens:
                click.echo(f"Total tokens used: {generator.total_tokens:,}")
            else:
                click.echo("Unable to count tokens. Add --no-count-tokens to disable.")
    else:
        # Ensure the docs directory exists when serving
        if serve and not output_path.exists():
            click.echo(
                "Error: No generated documentation found. Use --gen to generate docs first."
            )
            return

    generator.write_mkdocs_configuration()

    if open or serve:
        url = f"http://127.0.0.1:{port}/"
        click.echo(f"Serving docs at {url}")
        if open:
            webbrowser.open(url)
        mkdocs_serve(
            f"{output_path}/mkdocs.yml",
            dev_addr=f"127.0.0.1:{port}",
            livereload=True,
        )
