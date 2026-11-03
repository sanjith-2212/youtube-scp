import os
import re
import threading
from pathlib import Path

from azure.storage.blob import BlobServiceClient, ContentSettings


# Content types worth setting explicitly: the blob store serves these back to
# dataloaders and browsers, and the default (octet-stream) makes previewing and
# range-reading the media awkward.
CONTENT_TYPES = {
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
    ".csv": "text/csv",
    ".json": "application/json",
}

_ENV_LINE = re.compile(r'^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$')


def load_azure_credentials(path="~/.azure_sas_token"):
    """Read the account name and SAS token from the shell-style export file.

    Already-exported environment wins, so a run can be pointed at a different
    account without editing the file.
    """
    creds = {}
    token_path = Path(path).expanduser()
    if token_path.exists():
        with open(token_path) as f:
            for line in f:
                m = _ENV_LINE.match(line)
                if m:
                    creds[m.group(1)] = m.group(2).strip('"').strip("'")

    account = os.environ.get("AZURE_STORAGE_ACCOUNT") or creds.get("AZURE_STORAGE_ACCOUNT")
    sas = os.environ.get("AZURE_STORAGE_SAS_TOKEN") or creds.get("AZURE_STORAGE_SAS_TOKEN")

    if not account or not sas:
        raise RuntimeError(
            f"Azure credentials not found. Expected AZURE_STORAGE_ACCOUNT and "
            f"AZURE_STORAGE_SAS_TOKEN in the environment or in {token_path}."
        )
    return account, sas


class AzureUploader:
    """Uploads clips to a blob container under <prefix>/{audio,video}/<lang>/.

    Mirrors the S3Uploader interface so the pipeline stages do not care which
    backend they are writing to.
    """

    def __init__(self, config, logger):
        self.config = config
        self.logger = logger

        az = config["azure"]
        self.container = az["container"]
        self.prefix = az["prefix"].strip("/")
        self.audio_subdir = az.get("audio_subdir", "audio")
        self.video_subdir = az.get("video_subdir", "video")

        account, sas = load_azure_credentials(az.get("sas_token_file", "~/.azure_sas_token"))
        self.account = account

        # One client per uploader; the SDK's pipeline is thread-safe, and the
        # upload stage runs several threads against the same instance.
        self._svc = BlobServiceClient(
            f"https://{account}.blob.core.windows.net", credential=sas
        )
        self._container = self._svc.get_container_client(self.container)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ paths

    def blob_name(self, kind, lang, filename):
        """kind is "audio" or "video"."""
        subdir = self.audio_subdir if kind == "audio" else self.video_subdir
        return f"{self.prefix}/{subdir}/{lang}/{filename}"

    # ---------------------------------------------------------------- uploads

    def upload_file(self, local_path, kind, lang, filename):
        blob = self.blob_name(kind, lang, filename)
        ctype = CONTENT_TYPES.get(Path(filename).suffix.lower())
        try:
            with open(local_path, "rb") as f:
                self._container.upload_blob(
                    name=blob,
                    data=f,
                    overwrite=True,
                    content_settings=ContentSettings(content_type=ctype) if ctype else None,
                    max_concurrency=2,
                )
            self.logger.debug(f"Uploaded: {self.container}/{blob}")
            return True
        except Exception as e:
            self.logger.error(f"Upload failed for {filename}: {e}")
            return False

    def upload_metadata(self, csv_path, lang):
        """metadata.csv sits beside the audio clips it describes."""
        return self.upload_file(csv_path, "audio", lang, "metadata.csv")

    # ----------------------------------------------------------------- resume

    def check_existing(self, kind, lang):
        """Filenames already in the container for a language, for resume."""
        subdir = self.audio_subdir if kind == "audio" else self.video_subdir
        prefix = f"{self.prefix}/{subdir}/{lang}/"
        existing = set()
        try:
            for blob in self._container.list_blobs(name_starts_with=prefix):
                existing.add(blob.name.rsplit("/", 1)[-1])
        except Exception as e:
            self.logger.warning(f"Could not list existing {kind} for {lang}: {e}")
        return existing
