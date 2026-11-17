"""This fork's migrations: a branch of their own beside upstream's line.

Upstream numbers its migrations 001, 002, ... in `alembic/versions`, and the
Migration Chain CI job checks that line. The fork's migrations live in
`alembic/fork_versions` (f01, f02, ...), rooted on no upstream revision, so
an upstream release can add any number without a clash: `alembic upgrade
heads` runs both lines. These check the fork's line and how the two meet.
"""
import importlib.util
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND = Path(__file__).resolve().parent.parent
_SCRIPT = BACKEND / "scripts" / "check_migration_chain.py"
_spec = importlib.util.spec_from_file_location("check_migration_chain", _SCRIPT)
assert _spec and _spec.loader
check_migration_chain = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_migration_chain)

FORK_DIR = BACKEND / "alembic" / "fork_versions"


def scripts() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(BACKEND / "alembic.ini")))


def test_the_fork_line_is_a_single_line_of_its_own():
    assert check_migration_chain.check(FORK_DIR) == []


def test_alembic_sees_upstream_and_the_fork_as_two_heads():
    script = scripts()
    upstream_head = check_migration_chain.load_revisions(BACKEND / "alembic" / "versions")[0]
    parents = {rev.down_revision for rev in upstream_head if rev.down_revision}
    upstream_tip = next(rev.revision for rev in upstream_head if rev.revision not in parents)
    fork_tip = max(path.name.split("_", 1)[0] for path in FORK_DIR.glob("f*.py"))
    assert set(script.get_heads()) == {upstream_tip, fork_tip}
    assert [rev.revision for rev in script.get_revisions("fork@head")] == [fork_tip]


def test_fork_revisions_never_take_an_upstream_number():
    for path in FORK_DIR.glob("*.py"):
        if path.name == "__init__.py":
            continue
        revision = path.name.split("_", 1)[0]
        assert revision.startswith("f") and not revision.isdigit(), path.name


def test_the_fork_root_waits_for_the_tables_it_points_at():
    root = scripts().get_revision("f01")
    assert root.down_revision is None
    assert root.dependencies == "052"
    # Below every upstream revision env.py restamps a database to, or
    # alembic reads the two stamps as overlapping and refuses to upgrade.
    assert isinstance(root.dependencies, str) and int(root.dependencies) < 98
