"""Tests for resources.py, which loads Ahmed's profile data (CV, LinkedIn, summary...)."""

import importlib

import resources


def test_profile_data_loads_from_any_working_directory(monkeypatch, tmp_path):
    # Start "the server" from a different folder than backend/
    monkeypatch.chdir(tmp_path)

    importlib.reload(resources)  # re-run the module's file loading from the new folder

    assert resources.summary
    assert resources.linkedin != "LinkedIn profile not available"
    assert resources.resume != "resume is not available"
