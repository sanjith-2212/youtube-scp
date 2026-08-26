import torch
import numpy as np
import soundfile as sf
from pathlib import Path
from silero_vad import load_silero_vad, get_speech_timestamps


class AudioSegmenter:
    def __init__(self, config, logger):
        self.config = config
        self.logger = logger
        self.model = load_silero_vad()
        self.sample_rate = config["audio"]["sample_rate"]
        self.max_duration = config["audio"]["max_segment_duration"]
        self.min_duration = config["audio"]["min_segment_duration"]
        self.min_speech_ratio = config["audio"]["min_speech_ratio"]

    def segment_audio(self, audio_path, output_dir):
        try:
            # Load audio with soundfile (reliable, no torchaudio backend issues)
            waveform_np, sr = sf.read(str(audio_path), dtype="float32")

            # Convert to mono if stereo
            if len(waveform_np.shape) > 1:
                waveform_np = waveform_np.mean(axis=1)

            # Resample to 16kHz for VAD
            if sr != 16000:
                # Simple resampling using numpy
                vad_audio = self._resample(waveform_np, sr, 16000)
            else:
                vad_audio = waveform_np

            vad_tensor = torch.from_numpy(vad_audio)

            speech_timestamps = get_speech_timestamps(
                vad_tensor,
                self.model,
                sampling_rate=16000,
                min_speech_duration_ms=500,
                min_silence_duration_ms=300,
                speech_pad_ms=100,
                return_seconds=False,
            )

            if not speech_timestamps:
                self.logger.debug(f"No speech found in {audio_path.name}")
                return []

            # Resample full audio to target sample rate if needed
            if sr != self.sample_rate:
                waveform_np = self._resample(waveform_np, sr, self.sample_rate)

            segments = self._merge_timestamps_to_segments(speech_timestamps, len(vad_audio), 16000)
            output_segments = []

            for i, (start_sec, end_sec) in enumerate(segments):
                duration = end_sec - start_sec
                if duration < self.min_duration or duration > self.max_duration:
                    continue

                speech_ratio = self._calculate_speech_ratio(
                    speech_timestamps, start_sec, end_sec, 16000
                )
                if speech_ratio < self.min_speech_ratio:
                    continue

                # Extract segment
                start_sample = int(start_sec * self.sample_rate)
                end_sample = int(end_sec * self.sample_rate)
                segment_data = waveform_np[start_sample:end_sample]

                # Save segment
                stem = audio_path.stem
                seg_filename = f"{stem}_seg{i:04d}.wav"
                seg_path = output_dir / seg_filename

                sf.write(
                    str(seg_path),
                    segment_data,
                    self.sample_rate,
                    subtype="PCM_16",
                )

                output_segments.append({
                    "filename": seg_filename,
                    "path": seg_path,
                    "start_time_sec": round(start_sec, 3),
                    "end_time_sec": round(end_sec, 3),
                    "duration_sec": round(duration, 3),
                    "speech_ratio": round(speech_ratio, 3),
                })

            self.logger.debug(
                f"Segmented {audio_path.name}: {len(output_segments)} valid segments"
            )
            return output_segments

        except Exception as e:
            self.logger.error(f"Segmentation failed for {audio_path.name}: {e}")
            return []

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
