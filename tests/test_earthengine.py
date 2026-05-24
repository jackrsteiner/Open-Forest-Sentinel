"""Cover the Earth Engine seam by stubbing the ``ee`` module with a MagicMock.

These tests pin the exact EE interactions (collection ids, band name, export options,
bit math) without a live Earth Engine session.
"""

from unittest.mock import MagicMock

import pytest

from forest_sentinel import earthengine


@pytest.fixture
def fake_ee(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    fake = MagicMock(name="ee")
    monkeypatch.setattr(earthengine, "ee", fake)
    return fake


def test_initialize_uses_explicit_project(fake_ee: MagicMock) -> None:
    earthengine.initialize("my-project")
    fake_ee.Initialize.assert_called_once_with(project="my-project")


def test_initialize_falls_back_to_env(fake_ee: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(earthengine.GEE_PROJECT_ENV_VAR, "env-project")
    earthengine.initialize()
    fake_ee.Initialize.assert_called_once_with(project="env-project")


def test_start_image_export_submits_cog_task(fake_ee: MagicMock) -> None:
    task = fake_ee.batch.Export.image.toCloudStorage.return_value
    returned = earthengine.start_image_export_to_gcs(
        "image", bucket="b", file_name_prefix="a/b/c", scale=30, region={"type": "Polygon"}
    )
    assert returned is task
    task.start.assert_called_once_with()
    _, kwargs = fake_ee.batch.Export.image.toCloudStorage.call_args
    assert kwargs["bucket"] == "b"
    assert kwargs["fileNamePrefix"] == "a/b/c"
    assert kwargs["fileFormat"] == "GeoTIFF"
    assert kwargs["formatOptions"] == {"cloudOptimized": True}


def test_export_task_state_reads_status() -> None:
    task = MagicMock()
    task.status.return_value = {"state": "RUNNING"}
    assert earthengine.export_task_state(task) == "RUNNING"


@pytest.mark.parametrize(
    ("state", "expected"),
    [("FAILED", True), ("CANCELLED", True), ("RUNNING", False), ("COMPLETED", False)],
)
def test_is_terminal_failure(state: str, expected: bool) -> None:
    assert earthengine.is_terminal_failure(state) is expected


def test_list_image_properties_maps_features(fake_ee: MagicMock) -> None:
    chain = fake_ee.ImageCollection.return_value.filterBounds.return_value.filterDate.return_value
    chain.getInfo.return_value = {
        "features": [{"id": "img-1", "properties": {"system:index": "scene-1"}}]
    }
    result = earthengine.list_image_properties("C", {"type": "Polygon"}, "2026-01-01", "2026-01-31")
    assert result == [{"id": "img-1", "properties": {"system:index": "scene-1"}}]
    fake_ee.ImageCollection.assert_called_once_with("C")


def test_list_image_properties_handles_empty(fake_ee: MagicMock) -> None:
    chain = fake_ee.ImageCollection.return_value.filterBounds.return_value.filterDate.return_value
    chain.getInfo.return_value = None
    assert earthengine.list_image_properties("C", {}, "2026-01-01", "2026-01-31") == []


def test_apply_fmask_mask_selects_band_and_updates_mask(fake_ee: MagicMock) -> None:
    image = MagicMock(name="image")
    result = earthengine.apply_fmask_mask(image)
    image.select.assert_called_once_with("Fmask")
    image.updateMask.assert_called_once()
    assert result is image.updateMask.return_value


def test_valid_pixel_fraction_reduces_mask(fake_ee: MagicMock) -> None:
    image = MagicMock(name="image")
    reduced = image.select.return_value.mask.return_value.reduceRegion.return_value
    reduced.get.return_value.getInfo.return_value = 0.75
    assert earthengine.valid_pixel_fraction(image, "NBR", {"type": "Polygon"}, 30) == 0.75


def test_valid_pixel_fraction_none_is_zero(fake_ee: MagicMock) -> None:
    image = MagicMock(name="image")
    reduced = image.select.return_value.mask.return_value.reduceRegion.return_value
    reduced.get.return_value.getInfo.return_value = None
    assert earthengine.valid_pixel_fraction(image, "NBR", {}, 30) == 0.0


def test_apply_fmask_mask_accepts_custom_band(fake_ee: MagicMock) -> None:
    image = MagicMock(name="image")
    earthengine.apply_fmask_mask(image, fmask_band="QA")
    image.select.assert_called_once_with("QA")
