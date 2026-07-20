import sqlite3
from pathlib import Path

import pytest

from indi_mcp import frame_store


@pytest.fixture()
def store_paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "frames", tmp_path / "indi_mcp.sqlite3"


def test_save_frame_uses_env_var_frames_dir_when_no_directory_given(
    tmp_path: Path, monkeypatch
) -> None:
    frames_dir = tmp_path / "frames-from-env"
    monkeypatch.setenv(frame_store.FRAMES_DIR_ENV, str(frames_dir))

    metadata = frame_store.save_frame(
        b"data", device="cam", extension=".fits", db_path=tmp_path / "db.sqlite3"
    )

    assert (frames_dir / f"{metadata['frameId']}.fits").read_bytes() == b"data"


def test_save_frame_writes_the_file_and_returns_metadata(store_paths: tuple[Path, Path]) -> None:
    frames_dir, db_path = store_paths

    metadata = frame_store.save_frame(
        b"fits-bytes",
        device="ZWO CCD ASI2600MM Pro",
        extension=".fits",
        run_id="run-1",
        directory=frames_dir,
        db_path=db_path,
    )

    assert metadata["device"] == "ZWO CCD ASI2600MM Pro"
    assert metadata["runId"] == "run-1"
    assert metadata["sizeBytes"] == len(b"fits-bytes")
    assert metadata["transferredAt"] is None
    assert metadata["capturedAt"]
    saved_path = frames_dir / f"{metadata['frameId']}.fits"
    assert saved_path.read_bytes() == b"fits-bytes"


def test_save_frame_defaults_run_id_to_none_for_an_ad_hoc_capture(
    store_paths: tuple[Path, Path],
) -> None:
    frames_dir, db_path = store_paths

    metadata = frame_store.save_frame(
        b"data", device="cam", extension=".fits", directory=frames_dir, db_path=db_path
    )

    assert metadata["runId"] is None


def test_save_frame_never_collides_two_frames_from_the_same_device(
    store_paths: tuple[Path, Path],
) -> None:
    frames_dir, db_path = store_paths

    first = frame_store.save_frame(
        b"one", device="cam", extension=".fits", directory=frames_dir, db_path=db_path
    )
    second = frame_store.save_frame(
        b"two", device="cam", extension=".fits", directory=frames_dir, db_path=db_path
    )

    assert first["frameId"] != second["frameId"]
    assert (frames_dir / f"{first['frameId']}.fits").read_bytes() == b"one"
    assert (frames_dir / f"{second['frameId']}.fits").read_bytes() == b"two"


def test_save_frame_deletes_the_written_file_if_the_metadata_insert_fails(
    store_paths: tuple[Path, Path], monkeypatch
) -> None:
    frames_dir, db_path = store_paths
    fixed_id = "duplicate-frame-id"
    monkeypatch.setattr(frame_store.uuid, "uuid4", lambda: fixed_id)
    frame_store.save_frame(
        b"one", device="cam", extension=".fits", directory=frames_dir, db_path=db_path
    )
    duplicate_path = frames_dir / f"{fixed_id}.fits"
    assert duplicate_path.exists()

    with pytest.raises(sqlite3.IntegrityError):
        frame_store.save_frame(
            b"two", device="cam", extension=".fits", directory=frames_dir, db_path=db_path
        )

    assert not duplicate_path.exists()


def test_get_frame_metadata_returns_the_saved_row(store_paths: tuple[Path, Path]) -> None:
    frames_dir, db_path = store_paths
    saved = frame_store.save_frame(
        b"data", device="cam", extension=".fits", directory=frames_dir, db_path=db_path
    )

    metadata = frame_store.get_frame_metadata(saved["frameId"], db_path=db_path)

    assert metadata == saved


def test_get_frame_metadata_raises_for_an_unknown_frame_id(store_paths: tuple[Path, Path]) -> None:
    _, db_path = store_paths

    with pytest.raises(frame_store.FrameNotFoundError):
        frame_store.get_frame_metadata("does-not-exist", db_path=db_path)


def test_get_frame_path_returns_the_on_disk_path(store_paths: tuple[Path, Path]) -> None:
    frames_dir, db_path = store_paths
    saved = frame_store.save_frame(
        b"data", device="cam", extension=".fits", directory=frames_dir, db_path=db_path
    )

    path = frame_store.get_frame_path(saved["frameId"], db_path=db_path)

    assert path == frames_dir / f"{saved['frameId']}.fits"


def test_get_frame_path_raises_for_an_unknown_frame_id(store_paths: tuple[Path, Path]) -> None:
    _, db_path = store_paths

    with pytest.raises(frame_store.FrameNotFoundError):
        frame_store.get_frame_path("does-not-exist", db_path=db_path)


def test_list_frames_orders_most_recently_captured_first(store_paths: tuple[Path, Path]) -> None:
    frames_dir, db_path = store_paths
    first = frame_store.save_frame(
        b"one", device="cam", extension=".fits", directory=frames_dir, db_path=db_path
    )
    second = frame_store.save_frame(
        b"two", device="cam", extension=".fits", directory=frames_dir, db_path=db_path
    )

    frames = frame_store.list_frames(db_path=db_path)

    assert [f["frameId"] for f in frames] == [second["frameId"], first["frameId"]]


def test_list_frames_filters_by_run_id(store_paths: tuple[Path, Path]) -> None:
    frames_dir, db_path = store_paths
    match = frame_store.save_frame(
        b"one",
        device="cam",
        extension=".fits",
        run_id="run-a",
        directory=frames_dir,
        db_path=db_path,
    )
    frame_store.save_frame(
        b"two",
        device="cam",
        extension=".fits",
        run_id="run-b",
        directory=frames_dir,
        db_path=db_path,
    )

    frames = frame_store.list_frames(run_id="run-a", db_path=db_path)

    assert [f["frameId"] for f in frames] == [match["frameId"]]


def test_list_frames_filters_by_device(store_paths: tuple[Path, Path]) -> None:
    frames_dir, db_path = store_paths
    match = frame_store.save_frame(
        b"one", device="cam-a", extension=".fits", directory=frames_dir, db_path=db_path
    )
    frame_store.save_frame(
        b"two", device="cam-b", extension=".fits", directory=frames_dir, db_path=db_path
    )

    frames = frame_store.list_frames(device="cam-a", db_path=db_path)

    assert [f["frameId"] for f in frames] == [match["frameId"]]


def test_list_frames_filters_by_since(store_paths: tuple[Path, Path]) -> None:
    frames_dir, db_path = store_paths
    frame_store.save_frame(
        b"one", device="cam", extension=".fits", directory=frames_dir, db_path=db_path
    )
    recent = frame_store.save_frame(
        b"two", device="cam", extension=".fits", directory=frames_dir, db_path=db_path
    )

    frames = frame_store.list_frames(since=recent["capturedAt"], db_path=db_path)

    assert [f["frameId"] for f in frames] == [recent["frameId"]]


def test_list_frames_filters_by_transferred_only(store_paths: tuple[Path, Path]) -> None:
    frames_dir, db_path = store_paths
    transferred = frame_store.save_frame(
        b"one", device="cam", extension=".fits", directory=frames_dir, db_path=db_path
    )
    frame_store.save_frame(
        b"two", device="cam", extension=".fits", directory=frames_dir, db_path=db_path
    )
    frame_store.confirm_frame_transfer(transferred["frameId"], db_path=db_path)

    frames = frame_store.list_frames(transferred_only=True, db_path=db_path)

    assert [f["frameId"] for f in frames] == [transferred["frameId"]]


def test_confirm_frame_transfer_sets_transferred_at(store_paths: tuple[Path, Path]) -> None:
    frames_dir, db_path = store_paths
    saved = frame_store.save_frame(
        b"data", device="cam", extension=".fits", directory=frames_dir, db_path=db_path
    )
    assert saved["transferredAt"] is None

    updated = frame_store.confirm_frame_transfer(saved["frameId"], db_path=db_path)

    assert updated["transferredAt"] is not None


def test_confirm_frame_transfer_raises_for_an_unknown_frame_id(
    store_paths: tuple[Path, Path],
) -> None:
    _, db_path = store_paths

    with pytest.raises(frame_store.FrameNotFoundError):
        frame_store.confirm_frame_transfer("does-not-exist", db_path=db_path)


def test_delete_frame_removes_the_file_and_the_metadata_row(
    store_paths: tuple[Path, Path],
) -> None:
    frames_dir, db_path = store_paths
    saved = frame_store.save_frame(
        b"data", device="cam", extension=".fits", directory=frames_dir, db_path=db_path
    )
    frame_store.confirm_frame_transfer(saved["frameId"], db_path=db_path)
    path = frame_store.get_frame_path(saved["frameId"], db_path=db_path)
    assert path.exists()

    frame_store.delete_frame(saved["frameId"], db_path=db_path)

    assert not path.exists()
    with pytest.raises(frame_store.FrameNotFoundError):
        frame_store.get_frame_metadata(saved["frameId"], db_path=db_path)


def test_delete_frame_raises_if_not_transferred_by_default(
    store_paths: tuple[Path, Path],
) -> None:
    frames_dir, db_path = store_paths
    saved = frame_store.save_frame(
        b"data", device="cam", extension=".fits", directory=frames_dir, db_path=db_path
    )
    assert saved["transferredAt"] is None

    with pytest.raises(frame_store.FrameNotTransferredError):
        frame_store.delete_frame(saved["frameId"], db_path=db_path)

    assert frame_store.get_frame_path(saved["frameId"], db_path=db_path).exists()


def test_delete_frame_allows_untransferred_deletion_when_overridden(
    store_paths: tuple[Path, Path],
) -> None:
    frames_dir, db_path = store_paths
    saved = frame_store.save_frame(
        b"data", device="cam", extension=".fits", directory=frames_dir, db_path=db_path
    )

    frame_store.delete_frame(saved["frameId"], require_transferred=False, db_path=db_path)

    with pytest.raises(frame_store.FrameNotFoundError):
        frame_store.get_frame_metadata(saved["frameId"], db_path=db_path)


def test_delete_frame_leaves_other_frames_untouched(store_paths: tuple[Path, Path]) -> None:
    frames_dir, db_path = store_paths
    doomed = frame_store.save_frame(
        b"one", device="cam", extension=".fits", directory=frames_dir, db_path=db_path
    )
    frame_store.confirm_frame_transfer(doomed["frameId"], db_path=db_path)
    survivor = frame_store.save_frame(
        b"two", device="cam", extension=".fits", directory=frames_dir, db_path=db_path
    )

    frame_store.delete_frame(doomed["frameId"], db_path=db_path)

    assert frame_store.get_frame_metadata(survivor["frameId"], db_path=db_path) == survivor
    assert frame_store.get_frame_path(survivor["frameId"], db_path=db_path).exists()


def test_delete_frame_raises_for_an_unknown_frame_id(store_paths: tuple[Path, Path]) -> None:
    _, db_path = store_paths

    with pytest.raises(frame_store.FrameNotFoundError):
        frame_store.delete_frame("does-not-exist", db_path=db_path)
