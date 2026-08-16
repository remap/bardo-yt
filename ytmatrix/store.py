# ytmatrix/store.py
"""Where persistent state lives.

Every module that used to take a `cache_dir: Path` takes a `Store` instead.
The container this app runs in has no durable disk -- Cloudflare hands each
instance a fresh copy of the image on every start -- so the search cache, the
quota ledger, each user's config and each user's wall state all have to live
somewhere the container does not own.

`FileStore` is the old on-disk behaviour, kept so local development and the
whole test suite run with no Cloudflare account. `R2Store` is production.

The interface is deliberately tiny: bytes in, bytes out, a prefix listing, and
one compare-and-swap. Only the budget ledger needs the CAS -- it is the single
piece of state with more than one writer, because every user's container
spends from the same 10,000-unit daily allowance. Everything else is either
immutable (content-addressed cache entries) or single-writer (a user's own
config and wall state, written only by that user's own container).
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any, Protocol


class Store(Protocol):
    async def get(self, key: str) -> bytes | None: ...

    async def put(self, key: str, data: bytes) -> None: ...

    async def get_with_version(self, key: str) -> tuple[bytes, str] | None: ...

    async def put_if_version(self, key: str, data: bytes, version: str | None) -> bool: ...

    async def list_keys(self, prefix: str) -> list[str]: ...


class FileStore:
    """A Store backed by a directory. Local development and every test.

    `put_if_version` is check-then-write rather than genuinely atomic. That is
    fine here and nowhere else: this store only ever runs under a single local
    process. Production concurrency is R2Store's problem, and it solves it
    properly with a conditional request.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def _path(self, key: str) -> Path:
        return self._root / key

    async def get(self, key: str) -> bytes | None:
        try:
            return self._path(key).read_bytes()
        except OSError:
            # Missing, or a directory, or unreadable -- all of them are a miss.
            return None

    async def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        try:
            tmp.replace(path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    async def get_with_version(self, key: str) -> tuple[bytes, str] | None:
        data = await self.get(key)
        if data is None:
            return None
        return data, hashlib.sha256(data).hexdigest()

    async def put_if_version(self, key: str, data: bytes, version: str | None) -> bool:
        current = await self.get_with_version(key)
        if version is None:
            if current is not None:
                return False
        elif current is None or current[1] != version:
            return False
        await self.put(key, data)
        return True

    async def list_keys(self, prefix: str) -> list[str]:
        root = self._root
        if not root.is_dir():
            return []
        keys = [
            str(path.relative_to(root))
            for path in root.rglob("*")
            if path.is_file() and not path.name.endswith(".tmp")
        ]
        return sorted(key for key in keys if key.startswith(prefix))


def r2_client(account_id: str, access_key_id: str, secret_access_key: str) -> Any:
    """A boto3 S3 client pointed at R2, taught to send conditional headers.

    boto3 has no first-class parameter for the If-Match/If-None-Match headers
    that make `put_if_version` atomic, so the two event handlers below smuggle
    them through: the first lifts our `custom_headers` kwarg out before
    botocore's parameter validation rejects it, the second puts it on the wire.
    This is the pattern Cloudflare documents for R2 conditional writes.
    """
    import boto3
    from botocore.config import Config as BotoConfig

    client = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name="auto",
        config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 3}),
    )

    def lift_custom_headers(params, context, **kwargs):
        if custom_headers := params.pop("custom_headers", None):
            context["custom_headers"] = custom_headers

    def apply_custom_headers(params, context, **kwargs):
        if custom_headers := context.get("custom_headers"):
            params["headers"].update(custom_headers)

    events = client.meta.events
    events.register("before-parameter-build.s3.PutObject", lift_custom_headers)
    events.register("before-call.s3.PutObject", apply_custom_headers)
    return client


class R2Store:
    """A Store backed by one R2 bucket over the S3 API.

    boto3 is synchronous, so every call goes through `asyncio.to_thread`: these
    are network round trips, and blocking the event loop on them would stall
    every other player waiting on the same container.
    """

    #: A failed conditional write means someone else won the race; re-read and
    #: try again. Ten is far more than 5-10 users can realistically contend for.
    CAS_ATTEMPTS = 10

    def __init__(self, client: Any, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    async def get(self, key: str) -> bytes | None:
        found = await self.get_with_version(key)
        return None if found is None else found[0]

    async def put(self, key: str, data: bytes) -> None:
        await asyncio.to_thread(self._client.put_object, Bucket=self._bucket, Key=key, Body=data)

    async def get_with_version(self, key: str) -> tuple[bytes, str] | None:
        return await asyncio.to_thread(self._get_with_version, key)

    def _get_with_version(self, key: str) -> tuple[bytes, str] | None:
        from botocore.exceptions import ClientError

        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                return None
            raise
        return response["Body"].read(), response["ETag"]

    async def put_if_version(self, key: str, data: bytes, version: str | None) -> bool:
        return await asyncio.to_thread(self._put_if_version, key, data, version)

    def _put_if_version(self, key: str, data: bytes, version: str | None) -> bool:
        from botocore.exceptions import ClientError

        # If-None-Match: * means "only if it does not exist yet".
        headers = {"If-None-Match": "*"} if version is None else {"If-Match": version}
        try:
            self._client.put_object(Bucket=self._bucket, Key=key, Body=data, custom_headers=headers)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {
                "PreconditionFailed",
                "ConditionalRequestConflict",
                "412",
                "409",
            }:
                return False
            raise
        return True

    async def list_keys(self, prefix: str) -> list[str]:
        return await asyncio.to_thread(self._list_keys, prefix)

    def _list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token: str | None = None
        while True:
            kwargs = {"Bucket": self._bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            response = self._client.list_objects_v2(**kwargs)
            keys.extend(item["Key"] for item in response.get("Contents", []))
            if not response.get("IsTruncated"):
                break
            token = response.get("NextContinuationToken")
        return sorted(keys)
