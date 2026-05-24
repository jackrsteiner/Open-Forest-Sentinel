from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from forest_sentinel import earthengine, pipeline, storage
from forest_sentinel.cli import main
from forest_sentinel.models import Aoi
from forest_sentinel.pipeline import PipelineSummary

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
SAMPLE_AOI = EXAMPLES / "aoi-sample.geojson"


def test_run_persists_aoi_and_reports(
    migrated_database: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["run", "--aoi", str(SAMPLE_AOI)])
    assert exit_code == 0

    output = capsys.readouterr().out
    assert "Example AOI" in output
    assert "id=" in output
    assert "Total AOIs in database: 1" in output

    with Session(migrated_database) as session:
        rows = session.execute(select(Aoi)).scalars().all()
    assert [row.name for row in rows] == ["Example AOI"]


def test_run_with_bad_config_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["run", "--aoi", str(tmp_path / "missing.geojson")])
    assert exit_code == 1
    assert "error:" in capsys.readouterr().err


def test_run_with_duplicate_aoi_exits_nonzero(
    migrated_database: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", "--aoi", str(SAMPLE_AOI)]) == 0
    capsys.readouterr()

    exit_code = main(["run", "--aoi", str(SAMPLE_AOI)])
    assert exit_code == 1
    assert "already exists" in capsys.readouterr().err


def test_run_reports_database_connection_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "FOREST_SENTINEL_DATABASE_URL",
        "postgresql+psycopg://nobody:nobody@localhost:1/nowhere",
    )
    exit_code = main(["run", "--aoi", str(SAMPLE_AOI)])
    assert exit_code == 1
    assert "could not connect to the database" in capsys.readouterr().err


def test_pipeline_mode_runs_and_reports_summary(
    migrated_database: Engine,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_pipeline(session: object, **kwargs: object) -> PipelineSummary:
        captured.update(kwargs)
        return PipelineSummary(
            observations_discovered=6,
            observations_recorded=6,
            observations_skipped=0,
            index_rasters=12,
            change_rasters=10,
            candidates=5,
            events_created=1,
            event_observations=5,
        )

    monkeypatch.setattr(earthengine, "initialize", lambda project=None: None)
    monkeypatch.setattr(storage, "local_disk_storage_from_env", lambda: object())
    monkeypatch.setattr(pipeline, "run_pipeline", fake_run_pipeline)

    exit_code = main(
        ["run", "--aoi", str(SAMPLE_AOI), "--since", "2026-01-01", "--until", "2026-02-01"]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Ran Slice 1 pipeline" in out
    assert "Disturbance candidates: 5" in out
    assert "Disturbance events: 1 created" in out
    # The configured window was threaded through to the pipeline.
    assert str(captured["since"]) == "2026-01-01"
    assert str(captured["until"]) == "2026-02-01"


def test_pipeline_mode_reuses_existing_aoi(
    migrated_database: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(earthengine, "initialize", lambda project=None: None)
    monkeypatch.setattr(storage, "local_disk_storage_from_env", lambda: object())
    monkeypatch.setattr(
        pipeline,
        "run_pipeline",
        lambda session, **kwargs: PipelineSummary(0, 0, 0, 0, 0, 0, 0, 0),
    )
    args = ["run", "--aoi", str(SAMPLE_AOI), "--since", "2026-01-01", "--until", "2026-02-01"]
    assert main(args) == 0
    assert main(args) == 0  # idempotent: re-running reuses the AOI row, no duplicate error

    with Session(migrated_database) as session:
        assert len(session.execute(select(Aoi)).scalars().all()) == 1


def test_pipeline_mode_reports_storage_misconfiguration(
    migrated_database: Engine,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(earthengine, "initialize", lambda project=None: None)
    monkeypatch.delenv("FOREST_SENTINEL_GCS_STAGING_BUCKET", raising=False)
    exit_code = main(
        ["run", "--aoi", str(SAMPLE_AOI), "--since", "2026-01-01", "--until", "2026-02-01"]
    )
    assert exit_code == 1
    assert "storage is not configured" in capsys.readouterr().err
