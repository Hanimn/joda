"""
joda — storage abstraction.

Two interchangeable backends, chosen by ``settings.storage_backend``:

  * ``local`` — files on local disk (dev default; zero external deps).
  * ``s3``    — any S3-compatible object store (AWS S3, Cloudflare R2, GCS,
                or minio locally). Stems are served to the browser via
                **presigned URLs**, so downloads bypass the app entirely.

The rest of the app talks only to this interface, never to disk or boto3
directly, so swapping backends is a config change.

Object keys:
    uploads/<job_id>/input<suffix>
    stems/<job_id>/<stem>.wav
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path

from .config import get_settings


def upload_key(job_id: str, suffix: str) -> str:
    return f"uploads/{job_id}/input{suffix}"


def stem_key(job_id: str, stem: str) -> str:
    return f"stems/{job_id}/{stem}.wav"


class Storage(ABC):
    """Backend-agnostic blob store."""

    @abstractmethod
    def save(self, key: str, data: bytes) -> None: ...

    @abstractmethod
    def save_file(self, key: str, path: Path) -> None: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def open_local(self, key: str) -> Path:
        """Return a local filesystem path to the blob's bytes.

        For the local backend this is the file itself; for S3 it downloads to
        a temp path. Workers need a real file to hand to the Demucs CLI.
        """

    @abstractmethod
    def url_for(self, key: str) -> str:
        """A URL the browser can GET to download the blob.

        Local backend returns an app route; S3 returns a presigned URL.
        """

    @abstractmethod
    def delete_prefix(self, prefix: str) -> int:
        """Delete every object under ``prefix``; return count removed."""


# --- Local disk --------------------------------------------------------------


class LocalStorage(Storage):
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _p(self, key: str) -> Path:
        return self.root / key

    def save(self, key: str, data: bytes) -> None:
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def save_file(self, key: str, path: Path) -> None:
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, p)

    def exists(self, key: str) -> bool:
        return self._p(key).is_file()

    def open_local(self, key: str) -> Path:
        return self._p(key)

    def url_for(self, key: str) -> str:
        # key is "stems/<job_id>/<stem>.wav" -> app route.
        _, job_id, name = key.split("/", 2)
        return f"/api/stems/{job_id}/{name}"

    def delete_prefix(self, prefix: str) -> int:
        target = self._p(prefix)
        if not target.exists():
            return 0
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            return 1
        target.unlink(missing_ok=True)
        return 1


# --- S3 / minio --------------------------------------------------------------


class S3Storage(Storage):
    def __init__(self):
        import boto3

        s = get_settings()
        self.bucket = s.s3_bucket
        self.presign_ttl = s.s3_presign_ttl
        self._local_root = s.separated_dir  # scratch for downloads
        self._client = boto3.client(
            "s3",
            endpoint_url=s.s3_endpoint_url or None,
            region_name=s.s3_region,
            aws_access_key_id=s.s3_access_key or None,
            aws_secret_access_key=s.s3_secret_key or None,
        )
        # Presigned URLs must be signed against a host the *browser* can reach.
        # In docker, the internal endpoint (minio:9000) isn't resolvable
        # client-side, so sign against the public endpoint when one is set.
        public = s.s3_public_endpoint_url or s.s3_endpoint_url
        if public and public != s.s3_endpoint_url:
            self._presign_client = boto3.client(
                "s3",
                endpoint_url=public,
                region_name=s.s3_region,
                aws_access_key_id=s.s3_access_key or None,
                aws_secret_access_key=s.s3_secret_key or None,
            )
        else:
            self._presign_client = self._client

    def save(self, key: str, data: bytes) -> None:
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data)

    def save_file(self, key: str, path: Path) -> None:
        self._client.upload_file(str(path), self.bucket, key)

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False

    def open_local(self, key: str) -> Path:
        dest = self._local_root / "_dl" / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._client.download_file(self.bucket, key, str(dest))
        return dest

    def url_for(self, key: str) -> str:
        return self._presign_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=self.presign_ttl,
        )

    def delete_prefix(self, prefix: str) -> int:
        paginator = self._client.get_paginator("list_objects_v2")
        removed = 0
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if objs:
                self._client.delete_objects(
                    Bucket=self.bucket, Delete={"Objects": objs}
                )
                removed += len(objs)
        return removed


@lru_cache
def get_storage() -> Storage:
    s = get_settings()
    if s.storage_backend == "s3":
        return S3Storage()
    return LocalStorage(s.storage_root)
