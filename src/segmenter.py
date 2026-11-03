import torch
import numpy as np
import soundfile as sf
from pathlib import Path
from silero_vad import load_silero_vad, get_speech_timestamps

VAD_SR = 16000


class AudioSegmenter:
    """Finds speech windows with Silero VAD.

    Two entry points share one definition of what a usable window is, so the
    real (audio-only) and fake (audio+video) datasets cannot drift apart:

        detect_windows()  -> timings only, nothing written. The AV pipeline
                             hands these to yt-dlp as --download-sections so
                             the video clip covers exactly the same span as
                             its paired audio clip.
        segment_audio()   -> the same windows, cut out and written as WAVs.
    """

    def __init__(self, config, logger):
        self.config = config
        self.logger = logger
        self.model = load_silero_vad()
        self.sample_rate = config["audio"]["sample_rate"]
        self.max_duration = config["audio"]["max_segment_duration"]
        self.min_duration = config["audio"]["min_segment_duration"]
        self.min_speech_ratio = config["audio"]["min_speech_ratio"]

    # ------------------------------------------------------------- public API

    def detect_windows(self, audio_path):
        """Speech windows in `audio_path`, without writing any audio.

        Only the 16 kHz VAD copy is materialised, so this stays cheap on the
        multi-hour videos the AI channels publish.
        """
        audio_path = Path(audio_path)
        try:
            vad_audio, _ = self._load_for_vad(audio_path)
            if vad_audio is None:
                return []

            timestamps = self._vad_timestamps(vad_audio)
            if not timestamps:
                self.logger.debug(f"No speech found in {audio_path.name}")
                return []

            windows = self._windows(timestamps, len(vad_audio))
            self.logger.debug(f"{audio_path.name}: {len(windows)} candidate windows")
            return windows
        except Exception as e:
            self.logger.error(f"Window detection failed for {audio_path.name}: {e}")
            return []

    def measure_speech_ratio(self, audio_path):
        """Fraction of a file that is speech, or None if it could not be read.

        The long-clip mode cannot filter on speech up front -- a two minute span
        is picked by position, not by speech -- so the ratio is measured after
        the fact on the clip that was actually kept. VAD over 120s is cheap
        compared to the download, and it keeps the column meaningful for
        downstream filtering.
        """
        audio_path = Path(audio_path)
        try:
            vad_audio, _ = self._load_for_vad(audio_path)
            if vad_audio is None or not len(vad_audio):
                return None
            timestamps = self._vad_timestamps(vad_audio)
            total = len(vad_audio) / VAD_SR
            if total <= 0:
                return None
            speech = sum(
                (ts["end"] - ts["start"]) / VAD_SR for ts in timestamps
            )
            return round(min(speech / total, 1.0), 3)
        except Exception as e:
            self.logger.debug(f"Speech-ratio measurement failed for {audio_path.name}: {e}")
            return None

    def segment_audio(self, audio_path, output_dir):
        """Cut every qualifying window out of `audio_path` as a WAV."""
        audio_path = Path(audio_path)
        try:
            waveform_np, sr = sf.read(str(audio_path), dtype="float32")

            if len(waveform_np.shape) > 1:
                waveform_np = waveform_np.mean(axis=1)

            vad_audio = self._resample(waveform_np, sr, VAD_SR) if sr != VAD_SR else waveform_np

            timestamps = self._vad_timestamps(vad_audio)
            if not timestamps:
                self.logger.debug(f"No speech found in {audio_path.name}")
                return []

            # Full-rate copy for the actual cuts.
            if sr != self.sample_rate:
                waveform_np = self._resample(waveform_np, sr, self.sample_rate)

            windows = self._windows(timestamps, len(vad_audio))
            output_segments = []

            for w in windows:
                start_sample = int(w["start_time_sec"] * self.sample_rate)
                end_sample = int(w["end_time_sec"] * self.sample_rate)
                segment_data = waveform_np[start_sample:end_sample]

                seg_filename = f"{audio_path.stem}_seg{w['index']:04d}.wav"
                seg_path = Path(output_dir) / seg_filename

                sf.write(str(seg_path), segment_data, self.sample_rate, subtype="PCM_16")

                output_segments.append({
                    "filename": seg_filename,
                    "path": seg_path,
                    "start_time_sec": w["start_time_sec"],
                    "end_time_sec": w["end_time_sec"],
                    "duration_sec": w["duration_sec"],
                    "speech_ratio": w["speech_ratio"],
                })

            self.logger.debug(
                f"Segmented {audio_path.name}: {len(output_segments)} valid segments"
            )
            return output_segments

        except Exception as e:
            self.logger.error(f"Segmentation failed for {audio_path.name}: {e}")
            return []

    # --------------------------------------------------------------- internals

    def _load_for_vad(self, audio_path):
        """Mono 16 kHz float32 copy of the audio, for VAD only."""
        waveform_np, sr = sf.read(str(audio_path), dtype="float32")
        if len(waveform_np.shape) > 1:
            waveform_np = waveform_np.mean(axis=1)
        if sr != VAD_SR:
            waveform_np = self._resample(waveform_np, sr, VAD_SR)
        return waveform_np, sr

    def _vad_timestamps(self, vad_audio):
        return get_speech_timestamps(
            torch.from_numpy(vad_audio),
            self.model,
            sampling_rate=VAD_SR,
            min_speech_duration_ms=500,
            min_silence_duration_ms=300,
            speech_pad_ms=100,
            return_seconds=False,
        )

    def _windows(self, timestamps, n_vad_samples):
        """Merged speech spans that pass the duration and speech-ratio filters.

        `index` is the position in the merged list, including spans that were
        filtered out -- segment filenames are built from it, so it must stay
        stable or resume logic stops recognising already-uploaded clips.
        """
        merged = self._merge_timestamps_to_segments(timestamps, n_vad_samples, VAD_SR)
        windows = []

        for i, (start_sec, end_sec) in enumerate(merged):
            duration = end_sec - start_sec
            if duration < self.min_duration or duration > self.max_duration:
                continue

            speech_ratio = self._calculate_speech_ratio(timestamps, start_sec, end_sec, VAD_SR)
            if speech_ratio < self.min_speech_ratio:
                continue

            windows.append({
                "index": i,
                "start_time_sec": round(start_sec, 3),
                "end_time_sec": round(end_sec, 3),
                "duration_sec": round(duration, 3),
                "speech_ratio": round(speech_ratio, 3),
            })

        return windows

    def _resample(self, audio, orig_sr, target_sr):
        """Simple resampling using linear interpolation."""
        if orig_sr == target_sr:
            return audio
        duration = len(audio) / orig_sr
        target_len = int(duration * target_sr)
        indices = np.linspace(0, len(audio) - 1, target_len)
        return np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)

    def _merge_timestamps_to_segments(self, timestamps, total_samples, vad_sr):
        """Merge speech timestamps into natural segments up to max_duration."""
        segments = []
        current_start = None
        current_end = None

        for ts in timestamps:
            start_sec = ts["start"] / vad_sr
            end_sec = ts["end"] / vad_sr

            if current_start is None:
                current_start = start_sec
                current_end = end_sec
                continue

            if end_sec - current_start > self.max_duration:
                segments.append((current_start, current_end))
                current_start = start_sec
                current_end = end_sec
            else:
                current_end = end_sec

        if current_start is not None:
            segments.append((current_start, current_end))

        return segments

    def _calculate_speech_ratio(self, timestamps, seg_start, seg_end, vad_sr):
        """Calculate what fraction of a segment contains speech."""
        speech_duration = 0.0
        seg_duration = seg_end - seg_start

        if seg_duration <= 0:
            return 0.0

        for ts in timestamps:
            ts_start = ts["start"] / vad_sr
            ts_end = ts["end"] / vad_sr

            overlap_start = max(ts_start, seg_start)
            overlap_end = min(ts_end, seg_end)

            if overlap_end > overlap_start:
                speech_duration += overlap_end - overlap_start

        return speech_duration / seg_duration
