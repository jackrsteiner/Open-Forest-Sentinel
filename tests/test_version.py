import forest_sentinel


def test_version_is_a_string() -> None:
    assert isinstance(forest_sentinel.__version__, str)
    assert forest_sentinel.__version__
