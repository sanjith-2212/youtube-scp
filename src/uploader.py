import boto3
from pathlib import Path
from botocore.config import Config as BotoConfig


class S3Uploader:
    def __init__(self, config, logger):
        self.config = config
        self.logger = logger
        self.bucket = config["s3"]["bucket"]
        self.prefix = config["s3"]["prefix"]

        boto_config = BotoConfig(
            region_name=config["s3"]["region"],
            retries={"max_attempts": 5, "mode": "adaptive"},
        )
        self.client = boto3.client("s3", config=boto_config)

    def upload_file(self, local_path, lang, filename):
        s3_key = f"{self.prefix}/{lang}/{filename}"
        try:
            self.client.upload_file(str(local_path), self.bucket, s3_key)
            self.logger.debug(f"Uploaded: s3://{self.bucket}/{s3_key}")
            return True
        except Exception as e:
            self.logger.error(f"Upload failed for {filename}: {e}")
            return False

    def upload_batch(self, file_list, lang):
        """Upload a list of (local_path, filename) tuples."""
        success = 0
        failed = 0
        for local_path, filename in file_list:
            if self.upload_file(local_path, lang, filename):
                success += 1
            else:
                failed += 1
        self.logger.info(f"Batch upload for {lang}: {success} ok, {failed} failed")
        return success, failed

    def upload_metadata(self, csv_path, lang):
        return self.upload_file(csv_path, lang, "metadata.csv")

    def check_existing(self, lang):
        """Return set of filenames already uploaded for a language."""
        prefix = f"{self.prefix}/{lang}/"
        existing = set()
        try:
            paginator = self.client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    existing.add(obj["Key"].split("/")[-1])
        except Exception as e:
            self.logger.warning(f"Could not check existing for {lang}: {e}")
        return existing
