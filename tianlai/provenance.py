"""Structured provenance declarations shared by manifests and sidecars."""

from __future__ import annotations

from typing import Any


PROJECT_AUTHORED_DSP_KIND = "project_authored_dsp"
PROJECT_IMPLEMENTATION_LICENSE = "Apache-2.0"
NO_AUDIO_ASSET_LICENSE = "not_applicable"
APPROVED_LICENSE_STATUS = "approved"


def project_authored_dsp_provenance() -> dict[str, Any]:
    """Return a fresh, JSON-ready declaration for Tianlai-authored DSP."""

    return {
        "provenance_kind": PROJECT_AUTHORED_DSP_KIND,
        "implementation_license": PROJECT_IMPLEMENTATION_LICENSE,
        "external_audio_assets": [],
        "audio_asset_license": NO_AUDIO_ASSET_LICENSE,
        "license_status": APPROVED_LICENSE_STATUS,
    }


def is_project_authored_dsp_provenance(document: dict[str, Any]) -> bool:
    """Accept only the complete declaration; never infer provenance from type."""

    return (
        document.get("provenance_kind") == PROJECT_AUTHORED_DSP_KIND
        and document.get("implementation_license")
        == PROJECT_IMPLEMENTATION_LICENSE
        and document.get("external_audio_assets") == []
        and document.get("audio_asset_license") == NO_AUDIO_ASSET_LICENSE
        and document.get("license_status") == APPROVED_LICENSE_STATUS
    )
