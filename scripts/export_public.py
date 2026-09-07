"""Copy a reviewed public snapshot without replacing Git history or pushing."""
import argparse
from pathlib import Path
import shutil
import subprocess


def export(destination: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    destination = destination.resolve()
    if destination == root or not (destination / ".git").exists():
        raise ValueError("destination must be a separate existing public checkout")
    for path in (root, destination):
        status = subprocess.check_output(["git", "-C", str(path), "status", "--porcelain"])
        if status.strip():
            raise ValueError("source and destination must be clean before export")
    names = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"]).decode().split("\0")
    for name in filter(None, names):
        path = Path(name)
        if (path.parts[0] in {"notes", "outputs", "reviews", "retired"}
                or (path.name.startswith(".env") and path.name != ".env.example")
                or (path.parts[0] in {"cases", "keys"} and not path.name.startswith("example_"))):
            raise ValueError("private file in export roster")
        if (root / path).is_symlink():
            raise ValueError("symlinks require explicit export review")
    for name in filter(None, names):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / name, target)
    print("Copied public files. Existing Git history retained; review the destination diff.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    export(parser.parse_args().destination)
