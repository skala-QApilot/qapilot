from qapilot.shared.project_slug import project_slug_from_path, slugify_project_name


def test_slugify_project_name_rules():
    assert slugify_project_name('My Project') == 'my-project'
    assert slugify_project_name('Hello---World!!') == 'hello-world'
    assert slugify_project_name('  MIXED  Case --- Name  ') == 'mixed-case-name'
    assert slugify_project_name('Project__Name 2026') == 'projectname-2026'
    assert slugify_project_name('---') == 'project'


def test_project_slug_from_path_uses_directory_name(tmp_path):
    project_dir = tmp_path / 'My Sample-Repo!'
    project_dir.mkdir()
    assert project_slug_from_path(project_dir) == 'my-sample-repo'
