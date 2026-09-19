"""Fixed-gain kick+bass render and the two playback backends.

The render rule and the gain are identical for every pair in every session; see
_docs/pair-rating-workflow.md for the operator-facing description.
"""

import shlex
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

from backend import audio
from backend.evaluation.manifest import require


PLAYBACK_GAIN_DB = -6.0
PLAYBACK_TIMEOUT_SECONDS = 60.0
BACKENDS = ("command", "fake")
AUDIO_PLACEHOLDER = "{audio}"
FAKE_MONITORING_PREFIX = "dry-run"
GAIN_FACTOR = 10.0 ** (PLAYBACK_GAIN_DB / 20.0)
RENDER_SUBTYPE = "DOUBLE"
MONITORING_MAXIMUM = 200


class PlaybackError(ValueError):
    """A pair cannot be rendered or played; the presentation stays unrated."""


def monitoring_problem(backend, monitoring):
    """Return the first rule a monitoring description breaks, or None."""
    if backend not in BACKENDS:
        return "Unknown playback backend."
    if type(monitoring) is not str or not monitoring.strip() or len(monitoring) > MONITORING_MAXIMUM:
        return "The monitoring description must be nonblank text of at most 200 characters."
    if backend == "fake" and not monitoring.startswith(FAKE_MONITORING_PREFIX):
        return "A fake playback backend requires a monitoring description beginning with dry-run."
    return None


def player_command_problem(player_command):
    """Return the first rule a player command breaks, or None."""
    if type(player_command) is not str or AUDIO_PLACEHOLDER not in player_command:
        return "The command playback backend requires a player command containing the {audio} placeholder."
    try:
        if not shlex.split(player_command):
            return "The player command cannot be empty."
    except ValueError:
        return "The player command cannot be split into arguments."
    return None


def require_monitoring(backend, monitoring):
    problem = monitoring_problem(backend, monitoring)
    require(problem is None, problem or "Invalid monitoring description.")


def player_argv(player_command):
    problem = player_command_problem(player_command)
    require(problem is None, problem or "Invalid player command.")
    return shlex.split(player_command)


def render_pair(kick_path, bass_path, destination):
    """Align both elements at frame 0, zero-pad, sum, scale by the fixed gain, write one float WAV.

    No normalisation, loudness matching, limiting, fading, resampling or per-element gain is
    applied, and the sources are only read.
    """
    try:
        kick = audio.load_wav(kick_path)
        bass = audio.load_wav(bass_path)
    except (OSError, ValueError) as error:
        raise PlaybackError("An element of the pair cannot be read.") from error
    if kick.sample_rate_hz != bass.sample_rate_hz:
        raise PlaybackError("The two elements do not share one sample rate.")
    channels = max(kick.channels, bass.channels)
    left = _broadcast(kick.samples, channels)
    right = _broadcast(bass.samples, channels)
    frames = max(left.shape[0], right.shape[0])
    mixed = np.zeros((frames, channels), dtype=np.float64)
    mixed[: left.shape[0]] += left
    mixed[: right.shape[0]] += right
    mixed *= GAIN_FACTOR
    try:
        sf.write(str(destination), mixed, kick.sample_rate_hz, format="WAV", subtype=RENDER_SUBTYPE)
    except (OSError, RuntimeError, sf.SoundFileError) as error:
        raise PlaybackError("The pair render could not be written.") from error


def _broadcast(samples, channels):
    if samples.shape[1] == channels:
        return samples
    return np.repeat(samples, channels, axis=1)


def play(backend, render_path, player_command=None):
    """Return whether one playback completed; the player's output is discarded, never printed."""
    if backend == "fake":
        return True
    argv = [part.replace(AUDIO_PLACEHOLDER, str(render_path)) for part in player_argv(player_command)]
    try:
        completed = subprocess.run(argv, shell=False, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=PLAYBACK_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def render_path(session_directory, presentation_index):
    return Path(session_directory) / "playback" / f"{presentation_index}.wav"
