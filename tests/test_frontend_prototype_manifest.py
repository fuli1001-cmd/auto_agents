"""A standalone prototype may carry explicitly declared supporting artifacts."""

import pytest

from auto_agents.frontend_design import validate_prototype_manifest


@pytest.fixture
def package(tmp_path):
    prefix = ".auto-agents/docs/frontend_prototype"
    prototype = tmp_path / prefix
    prototype.mkdir(parents=True)
    (prototype / "home.html").write_text(
        '<html><meta name="viewport" content="width=device-width">Prototype</html>'
    )
    payload = {
        "pages": [{
            "id": "home", "title": "Home", "route": "/",
            "requirement_ids": ["REQ-001"], "html_ref": prefix + "/home.html",
        }],
        "index_ref": prefix + "/home.html",
        "viewports": ["1440x900"],
    }
    return tmp_path, prototype, payload


def test_manifest_allows_declared_supporting_assets_and_evidence(package):
    root, prototype, payload = package
    payload["artifacts"] = [payload["index_ref"]]
    for name in (
        "handoff.md", "assets/font.woff2", "assets/sample.mp4", "catalog/LICENSE",
        "evidence/desktop.png", "evidence/results.json", "verification/test_page.py",
    ):
        path = prototype / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"declared supporting artifact")
        payload["artifacts"].append(path.relative_to(root).as_posix())
    assert validate_prototype_manifest(root, payload, max_pages=3) == []


def test_manifest_still_rejects_undeclared_supporting_files(package):
    root, prototype, payload = package
    (prototype / "undeclared.md").write_text("not declared")
    payload["artifacts"] = [payload["index_ref"]]
    errors = validate_prototype_manifest(root, payload, max_pages=3)
    assert any("undeclared.md" in error for error in errors)


@pytest.mark.parametrize("invalid", [None, "evidence/report.json", [None], [{}], [""], ["../outside.md"]])
def test_manifest_rejects_invalid_artifact_declarations(package, invalid):
    root, _, payload = package
    payload["artifacts"] = invalid
    assert validate_prototype_manifest(root, payload, max_pages=3)


@pytest.mark.parametrize("kind", ["absolute", "outside_package", "traversal", "missing", "directory", "credential"])
def test_manifest_artifacts_must_be_owned_files_in_the_package(package, kind):
    root, prototype, payload = package
    path = prototype / "evidence/report.json"
    path.parent.mkdir()
    path.write_text("{}")
    baseline_ref = path.relative_to(root).as_posix()
    if kind == "absolute":
        ref = str(path)
    elif kind == "outside_package":
        path = root / "outside.md"
        path.write_text("outside prototype")
        ref = path.relative_to(root).as_posix()
    elif kind == "traversal":
        ref = path.parent.relative_to(root).as_posix() + "/../evidence/report.json"
    elif kind == "missing":
        ref = path.parent.relative_to(root).as_posix() + "/missing.json"
    elif kind == "directory":
        ref = path.parent.relative_to(root).as_posix()
    else:
        path = prototype / ".env"
        path.write_text("private")
        ref = path.relative_to(root).as_posix()
    payload["artifacts"] = [baseline_ref, ref]
    assert validate_prototype_manifest(root, payload, max_pages=3)


@pytest.mark.parametrize("kind", ["local_file", "external_file", "directory"])
def test_manifest_artifact_declarations_cannot_authorize_symlinks(package, kind):
    root, prototype, payload = package
    target = root / "external.md" if kind == "external_file" else prototype / "report.md"
    if kind == "directory":
        target.mkdir()
        (target / "report.json").write_text("{}")
    else:
        target.write_text("report")
    alias = prototype / "alias"
    alias.symlink_to(target, target_is_directory=kind == "directory")
    ref = alias / "report.json" if kind == "directory" else alias
    payload["artifacts"] = [ref.relative_to(root).as_posix()]
    assert validate_prototype_manifest(root, payload, max_pages=3)


def test_declaring_html_as_an_artifact_does_not_replace_page_registration(package):
    root, prototype, payload = package
    path = prototype / "unlisted.html"
    path.write_text('<html><meta name="viewport" content="width=device-width"></html>')
    payload["artifacts"] = [path.relative_to(root).as_posix()]
    assert validate_prototype_manifest(root, payload, max_pages=3)


@pytest.mark.parametrize("dependency", [
    '<img src="https://cdn.invalid/image.png">',
    '<script src="local.js"></script>',
])
def test_declared_page_artifacts_still_require_standalone_html(package, dependency):
    root, prototype, payload = package
    path = prototype / "home.html"
    path.write_text(path.read_text().replace("</html>", dependency + "</html>"))
    payload["artifacts"] = [payload["index_ref"]]
    assert validate_prototype_manifest(root, payload, max_pages=3)
