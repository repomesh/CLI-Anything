from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli_anything.wavetone.core import audio as audio_core
from cli_anything.wavetone.core.audio import probe_audio
from cli_anything.wavetone.core.project import (
    DEFAULT_ANALYSIS_SETTINGS,
    add_label,
    create_project,
    load_project,
    save_project,
    set_tempo,
    update_analysis,
)
from cli_anything.wavetone.core.session import append_event, load_events
from cli_anything.wavetone.tests.helpers import make_wav
from cli_anything.wavetone.utils import wavetone_backend
from cli_anything.wavetone.wavetone_cli import _split_repl_args, cli


def test_create_project_manifest(tmp_path: Path) -> None:
    wav = make_wav(tmp_path / "tone.wav")
    project = create_project(wav, name="Tone Test")

    assert project["schema_version"] == "wavetone-project/v1"
    assert project["project"]["name"] == "Tone Test"
    assert project["audio"]["path"] == str(wav.resolve())
    assert project["analysis"] == DEFAULT_ANALYSIS_SETTINGS


def test_rejects_unsupported_audio(tmp_path: Path) -> None:
    txt = tmp_path / "not-audio.txt"
    txt.write_text("x", encoding="utf-8")

    with pytest.raises(ValueError):
        create_project(txt)


def test_save_load_project_roundtrip(tmp_path: Path) -> None:
    wav = make_wav(tmp_path / "tone.wav")
    project = create_project(wav)
    add_label(project, "chorus", 12.5)
    set_tempo(project, 128, first_bar_time_seconds=0.2)
    output = save_project(project, tmp_path / "project.json")

    loaded = load_project(output)
    assert loaded["labels"][0]["name"] == "chorus"
    assert loaded["tempo"]["bpm"] == 128
    assert loaded["tempo"]["first_bar_time_seconds"] == 0.2


def test_labels_are_sorted(tmp_path: Path) -> None:
    wav = make_wav(tmp_path / "tone.wav")
    project = create_project(wav)
    add_label(project, "late", 4.0)
    add_label(project, "early", 1.0)
    add_label(project, "middle", "2.5")

    assert [label["name"] for label in project["labels"]] == ["early", "middle", "late"]


def test_update_analysis_settings(tmp_path: Path) -> None:
    wav = make_wav(tmp_path / "tone.wav")
    project = create_project(wav)
    update_analysis(project, channel="L+R", blocks_per_second=24, analyze_fundamental_frequency=False)

    assert project["analysis"]["channel"] == "L+R"
    assert project["analysis"]["blocks_per_second"] == 24
    assert project["analysis"]["analyze_fundamental_frequency"] is False


def test_rejects_non_finite_project_numbers(tmp_path: Path) -> None:
    wav = make_wav(tmp_path / "tone.wav")
    project = create_project(wav)

    with pytest.raises(ValueError, match="BPM.*finite"):
        set_tempo(project, float("nan"))

    with pytest.raises(ValueError, match="reference_frequency_hz.*finite"):
        update_analysis(project, reference_frequency_hz=float("inf"))


def test_load_project_rejects_non_object_json(tmp_path: Path) -> None:
    project_path = tmp_path / "broken.wt.json"
    project_path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="JSON object"):
        load_project(project_path)


def test_probe_wav_metadata(tmp_path: Path) -> None:
    wav = make_wav(tmp_path / "tone.wav", duration=0.5, sample_rate=16000)
    info = probe_audio(wav)

    assert info["probe_method"] == "python-wave"
    assert info["sample_rate"] == 16000
    assert info["channels"] == 1
    assert info["duration_seconds"] == 0.5
    assert info["size_bytes"] > 0


def test_probe_malformed_wav_falls_back_to_stat(tmp_path: Path) -> None:
    wav = tmp_path / "broken.wav"
    wav.write_bytes(b"")

    info = probe_audio(wav)

    assert info["probe_method"] == "stat"
    assert info["format"] == "wav"
    assert info["duration_seconds"] is None
    assert info["size_bytes"] == 0


def test_ffprobe_uses_single_show_entries_argument(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audio = tmp_path / "tone.mp3"
    audio.write_bytes(b"mp3")
    captured: dict[str, list[str]] = {}

    monkeypatch.setattr(audio_core.shutil, "which", lambda name: "ffprobe")

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        stdout = json.dumps(
            {
                "streams": [
                    {
                        "codec_type": "audio",
                        "codec_name": "mp3",
                        "sample_rate": "44100",
                        "channels": "2",
                    }
                ],
                "format": {
                    "duration": "1.25",
                    "format_name": "mp3",
                    "bit_rate": "128000",
                    "size": "3",
                },
            }
        )
        return subprocess.CompletedProcess(args, 0, stdout=stdout)

    monkeypatch.setattr(audio_core.subprocess, "run", fake_run)

    info = audio_core._probe_ffprobe(audio)
    entries = captured["args"][captured["args"].index("-show_entries") + 1]

    assert captured["args"].count("-show_entries") == 1
    assert "stream=codec_type,codec_name,sample_rate,channels" in entries
    assert ":format=duration,format_name,bit_rate,size" in entries
    assert info["probe_method"] == "ffprobe"
    assert info["sample_rate"] == 44100
    assert info["channels"] == 2


def test_ffprobe_handles_non_numeric_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audio = tmp_path / "tone.mp3"
    audio.write_bytes(b"mp3")

    monkeypatch.setattr(audio_core.shutil, "which", lambda name: "ffprobe")

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        stdout = json.dumps(
            {
                "streams": [{"codec_type": "audio", "sample_rate": "N/A"}],
                "format": {
                    "duration": "N/A",
                    "format_name": "mp3",
                    "bit_rate": "N/A",
                    "size": "N/A",
                },
            }
        )
        return subprocess.CompletedProcess(args, 0, stdout=stdout)

    monkeypatch.setattr(audio_core.subprocess, "run", fake_run)

    info = audio_core._probe_ffprobe(audio)

    assert info["duration_seconds"] is None
    assert info["sample_rate"] is None
    assert info["bit_rate"] is None
    assert info["size_bytes"] == audio.stat().st_size


def test_session_event_log(tmp_path: Path) -> None:
    session_path = tmp_path / "session.json"
    append_event(session_path, "created", {"project": "demo"})
    append_event(session_path, "launched", {"pid": 123})

    events = load_events(session_path)
    assert [event["event"] for event in events] == ["created", "launched"]


def test_session_rejects_invalid_schema(tmp_path: Path) -> None:
    session_path = tmp_path / "session.json"
    session_path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="JSON object"):
        append_event(session_path, "created", {})

    session_path.write_text(json.dumps({"events": {}}), encoding="utf-8")

    with pytest.raises(ValueError, match="events.*list"):
        load_events(session_path)

    session_path.unlink()

    with pytest.raises(ValueError, match="finite"):
        append_event(session_path, "bad", {"value": float("nan")})

    assert not session_path.exists()


def test_find_wavetone_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "wavetone.exe"
    fake.write_bytes(b"MZ")
    monkeypatch.setenv("WAVETONE_EXE", str(fake))

    assert wavetone_backend.find_wavetone() == fake.resolve()


def test_cli_preserves_inherited_project_and_json_context(tmp_path: Path) -> None:
    wav = make_wav(tmp_path / "tone.wav")
    project_path = save_project(create_project(wav), tmp_path / "tone.wt.json")

    result = CliRunner().invoke(
        cli,
        ["audio", "probe"],
        obj={"project": str(project_path), "json": True},
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["audio"]["path"] == str(wav.resolve())


def test_wavetone_launch_fails_on_early_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_launch_wavetone(**kwargs: object) -> dict[str, object]:
        return {
            "backend": "wavetone.exe",
            "executable": "C:/fake/wavetone.exe",
            "running_after_wait": False,
            "terminated": False,
            "exit_code": 42,
        }

    monkeypatch.setattr(wavetone_backend, "launch_wavetone", fake_launch_wavetone)

    result = CliRunner().invoke(cli, ["--json", "wavetone", "launch", "--wait", "1"])

    assert result.exit_code == 42
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["launch"]["exit_code"] == 42


def test_wavetone_launch_reports_runtime_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_launch_wavetone(**kwargs: object) -> dict[str, object]:
        raise RuntimeError("WaveTone launch requires Windows")

    monkeypatch.setattr(wavetone_backend, "launch_wavetone", fake_launch_wavetone)

    result = CliRunner().invoke(cli, ["--json", "wavetone", "launch"])

    assert result.exit_code == 1
    assert "WaveTone launch requires Windows" in result.output
    assert "Traceback" not in result.output


def test_launch_requires_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wavetone_backend.platform, "system", lambda: "Linux")

    with pytest.raises(RuntimeError, match="requires Windows"):
        wavetone_backend.launch_wavetone()


def test_repl_split_strips_windows_quotes() -> None:
    line = 'project new "C:\\Users\\me\\My Music\\song.wav" -o "C:\\Users\\me\\song.wt.json"'

    assert _split_repl_args(line) == [
        "project",
        "new",
        "C:\\Users\\me\\My Music\\song.wav",
        "-o",
        "C:\\Users\\me\\song.wt.json",
    ]
