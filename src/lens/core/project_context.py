"""One-project CLI selection; context never lives in an indexed source tree."""
import json
import os
from pathlib import Path
from lens.core.models import stable_id


def state_path():
    base = Path(os.getenv("LENS_STATE_DIR", str(Path(os.getenv("LOCALAPPDATA", str(Path.home() / ".local/share"))) / "Lens")))
    return base / "project-context.json"


def active():
    path = state_path()
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value.get("project_id")
    except (ValueError, AttributeError):
        raise ValueError("Invalid local project context; select a project with 'lens project use <path>'") from None


def select(projects, explicit=None, cwd=None):
    if explicit is not None:
        project_id = stable_id(str(Path(explicit).resolve()))
        match = next((p for p in projects if p["id"] == project_id), None)
        if not match:
            raise ValueError("Project is not indexed. Run 'lens index <path>' first")
        return match
    current = active()
    match = next((p for p in projects if p["id"] == current), None)
    if match:
        return match
    directory = Path(cwd or Path.cwd()).resolve()
    matches = [p for p in projects if directory.is_relative_to(Path(p["root"]).resolve())]
    if matches:
        return max(matches, key=lambda p: len(Path(p["root"]).parts))
    raise ValueError("Current directory is not indexed and no current project is available. Use 'lens index .' or 'lens project use <path>'")


def save(project, projects):
    path = state_path().resolve()
    if any(path.is_relative_to(Path(p["root"]).resolve()) for p in projects):
        raise ValueError("LENS_STATE_DIR must be outside every indexed repository")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"project_id": project["id"], "root": project["root"]}, indent=2), encoding="utf-8")
    temporary.replace(path)


def clear_if(project_id):
    """Clear the saved selection when its indexed project is deleted."""
    path = state_path()
    if active() == project_id and path.exists():
        path.unlink()
