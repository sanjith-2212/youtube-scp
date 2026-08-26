import whisper
import torch
import numpy as np
import soundfile as sf


class Transcriber:
    def __init__(self, config, logger):
        self.config = config
        self.logger = logger
        self.device = config["processing"]["whisper_device"]
        self.model_name = config["processing"]["whisper_model"]
        self.model = None

    def load_model(self):
        if self.model is None:
            self.logger.info(f"Loading Whisper {self.model_name} on {self.device}")
            self.model = whisper.load_model(self.model_name, device=self.device)
            self.logger.info("Whisper model loaded")

    def transcribe(self, audio_path, language=None):
        self.load_model()

        try:
            result = self.model.transcribe(
                str(audio_path),
                language=language if language != "zh-cn" else "zh",
                fp16=(self.device == "cuda"),
                condition_on_previous_text=False,
            )
            return result.get("text", "").strip()
        except Exception as e:
            self.logger.warning(f"Transcription failed for {audio_path}: {e}")
            return ""

    def transcribe_batch(self, audio_paths, language=None):
        """Transcribe a list of audio files, returning list of (transcript, detected_lang) tuples."""
        self.load_model()
        results = []
        lang_code = language if language != "zh-cn" else "zh"

        for path in audio_paths:
            try:
                result = self.model.transcribe(
                    str(path),
                    language=lang_code,
                    fp16=(self.device == "cuda"),
                    condition_on_previous_text=False,
                )
                results.append(result.get("text", "").strip())
            except Exception as e:
                self.logger.warning(f"Transcription failed for {path}: {e}")
                results.append("")

        return results

    def detect_language(self, audio_path):
        """Detect language of audio without forcing. Returns language code."""
        self.load_model()
        try:
            audio = whisper.load_audio(str(audio_path))
            audio = whisper.pad_or_trim(audio)
            mel = whisper.log_mel_spectrogram(audio).to(self.device)
            _, probs = self.model.detect_language(mel)
            return max(probs, key=probs.get)
        except Exception:
            return None
