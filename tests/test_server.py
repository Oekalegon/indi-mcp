from unittest.mock import AsyncMock

import pytest

from indi_mcp import indi_driver, indi_messaging, rig_store, server


async def test_draft_rig_only_fetches_properties_relevant_to_each_devices_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    devices = [
        "CCD Simulator",
        "Filter Wheel Simulator",
        "Focuser Simulator",
        "Telescope Simulator",
        "Unknown Device",
    ]
    families = {
        "CCD Simulator": "CCDs",
        "Filter Wheel Simulator": "Filter Wheels",
        "Focuser Simulator": "Focusers",
        "Telescope Simulator": "Telescopes",
        "Unknown Device": None,
    }
    monkeypatch.setattr(indi_messaging, "list_devices", lambda: devices)
    monkeypatch.setattr(
        indi_driver, "classify_device", AsyncMock(side_effect=lambda name: families[name])
    )

    calls: dict[str, list[tuple]] = {"values": [], "range": []}

    def fake_get_property_values(device: str, name: str) -> dict[str, str] | None:
        calls["values"].append((device, name))
        return {"member": "value"}

    def fake_get_property_range(device: str, name: str, member: str) -> tuple[float, float] | None:
        calls["range"].append((device, name, member))
        return (0.0, 100.0)

    monkeypatch.setattr(indi_messaging, "get_property_values", fake_get_property_values)
    monkeypatch.setattr(indi_messaging, "get_property_range", fake_get_property_range)

    captured: list[rig_store.DraftDeviceInfo] = []

    def fake_draft_rig(devices: list[rig_store.DraftDeviceInfo]) -> rig_store.RigDraft:
        captured.extend(devices)
        return {"kind": "rigDraft", "components": [], "notes": []}

    monkeypatch.setattr(rig_store, "draft_rig", fake_draft_rig)

    result = await server.draft_rig()

    assert result == {"kind": "rigDraft", "components": [], "notes": []}
    # Only cameras get CCD_INFO, only filter wheels get FILTER_NAME, only focusers get a range.
    assert calls["values"] == [
        ("CCD Simulator", "CCD_INFO"),
        ("Filter Wheel Simulator", "FILTER_NAME"),
    ]
    assert calls["range"] == [
        ("Focuser Simulator", "ABS_FOCUS_POSITION", "FOCUS_ABSOLUTE_POSITION")
    ]

    by_name = {device["name"]: device for device in captured}
    assert by_name["CCD Simulator"]["family"] == "CCDs"
    assert by_name["CCD Simulator"]["ccdInfo"] == {"member": "value"}
    assert by_name["CCD Simulator"]["filterNames"] is None
    assert by_name["CCD Simulator"]["focusRange"] is None

    assert by_name["Filter Wheel Simulator"]["filterNames"] == {"member": "value"}
    assert by_name["Filter Wheel Simulator"]["ccdInfo"] is None

    assert by_name["Focuser Simulator"]["focusRange"] == (0.0, 100.0)
    assert by_name["Focuser Simulator"]["ccdInfo"] is None

    assert by_name["Telescope Simulator"]["family"] == "Telescopes"
    assert by_name["Telescope Simulator"]["ccdInfo"] is None
    assert by_name["Telescope Simulator"]["filterNames"] is None
    assert by_name["Telescope Simulator"]["focusRange"] is None

    assert by_name["Unknown Device"]["family"] is None
    assert by_name["Unknown Device"]["ccdInfo"] is None
    assert by_name["Unknown Device"]["filterNames"] is None
    assert by_name["Unknown Device"]["focusRange"] is None


def test_save_rig_delegates_to_rig_store_with_the_overwrite_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = rig_store.Rig(id="minimal", name="Minimal rig", components=[])
    calls: list[tuple[rig_store.Rig, bool]] = []

    def fake_save_rig(rig: rig_store.Rig, *, overwrite: bool = False) -> rig_store.Rig:
        calls.append((rig, overwrite))
        return rig

    monkeypatch.setattr(rig_store, "save_rig", fake_save_rig)

    result = server.save_rig(rig, overwrite=True)

    assert result == rig
    assert calls == [(rig, True)]
