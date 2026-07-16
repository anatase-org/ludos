from __future__ import annotations

import hashlib
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from ludos.__main__ import build_parser
from ludos.model import ConfigError
from ludos.upload.common import (
    REGISTRY_SHORT_CACHE_CONTROL,
    S3Config,
    _create_s3_client,
    _s3_config_from_env,
)
from ludos.upload.file import (
    delete_file,
    upload_file,
)


ENV = {
    "LUDOS_S3_API": "https://s3.example.com/anatase-artifacts",
    "LUDOS_S3_KEY": "key",
    "LUDOS_S3_SECRET": "secret",
}


class FakeClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3Client:
    def __init__(
        self,
        objects: dict[tuple[str, str], bytes] | None = None,
        *,
        cache_controls: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self.objects = {} if objects is None else dict(objects)
        self.cache_controls = {} if cache_controls is None else dict(cache_controls)
        self.uploads: list[dict[str, object]] = []
        self.puts: list[dict[str, object]] = []
        self.deletes: list[dict[str, object]] = []
        self.gets: list[dict[str, object]] = []
        self.heads: list[dict[str, object]] = []
        self.lists: list[dict[str, object]] = []
        self.delete_errors: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, str]] = []

    def upload_file(
        self,
        filename: str,
        bucket: str,
        key: str,
        *,
        ExtraArgs: dict[str, str],
        Callback: object | None = None,
    ) -> None:
        data = Path(filename).read_bytes()
        self.uploads.append(
            {
                "Filename": filename,
                "Bucket": bucket,
                "Key": key,
                "ExtraArgs": ExtraArgs,
                "Callback": Callback,
            }
        )
        self.calls.append(("upload_file", key))
        if Callback is not None:
            Callback(len(data))  # type: ignore[operator]
        self.objects[(bucket, key)] = data
        self.cache_controls[(bucket, key)] = ExtraArgs.get("CacheControl")

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, BytesIO]:
        self.gets.append({"Bucket": Bucket, "Key": Key})
        self.calls.append(("get_object", Key))
        try:
            body = self.objects[(Bucket, Key)]
        except KeyError as exc:
            raise FakeClientError("NoSuchKey") from exc
        return {"Body": BytesIO(body)}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        self.heads.append({"Bucket": Bucket, "Key": Key})
        self.calls.append(("head_object", Key))
        try:
            body = self.objects[(Bucket, Key)]
        except KeyError as exc:
            raise FakeClientError("NoSuchKey") from exc
        response: dict[str, object] = {"ContentLength": len(body)}
        cache_control = self.cache_controls.get((Bucket, Key))
        if cache_control is not None:
            response["CacheControl"] = cache_control
        return response

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
        CacheControl: str | None = None,
    ) -> None:
        put: dict[str, object] = {
            "Bucket": Bucket,
            "Key": Key,
            "Body": Body,
            "ContentType": ContentType,
        }
        if CacheControl is not None:
            put["CacheControl"] = CacheControl
        self.puts.append(put)
        self.calls.append(("put_object", Key))
        self.objects[(Bucket, Key)] = Body
        self.cache_controls[(Bucket, Key)] = CacheControl

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.deletes.append({"Bucket": Bucket, "Key": Key})
        self.calls.append(("delete_object", Key))
        if (Bucket, Key) in self.delete_errors:
            raise FakeClientError(self.delete_errors[(Bucket, Key)])
        self.objects.pop((Bucket, Key), None)
        self.cache_controls.pop((Bucket, Key), None)

    def list_objects_v2(
        self,
        *,
        Bucket: str,
        Prefix: str,
        ContinuationToken: str | None = None,
    ) -> dict[str, object]:
        request: dict[str, object] = {"Bucket": Bucket, "Prefix": Prefix}
        if ContinuationToken is not None:
            request["ContinuationToken"] = ContinuationToken
        self.lists.append(request)
        self.calls.append(("list_objects_v2", Prefix))
        contents = [
            {"Key": key}
            for bucket, key in sorted(self.objects)
            if bucket == Bucket and key.startswith(Prefix)
        ]
        return {"Contents": contents, "IsTruncated": False}


class UploadFileTests(unittest.TestCase):
    def test_s3_client_requires_upload_extra(self) -> None:
        config = S3Config("https://s3.example.com", "bucket")
        with patch.dict("sys.modules", {"boto3": None}):
            with self.assertRaisesRegex(
                ConfigError,
                r"install ludos\[images\] or ludos\[flatpaks\]",
            ):
                _create_s3_client(config, {})

    def test_upload_file_parser(self) -> None:
        args = build_parser().parse_args(
            [
                "registry",
                "file",
                "upload",
                "cache/iso/anatase.iso",
                "isos/anatase.iso",
                "anatase-44.20260627.iso",
            ]
        )

        self.assertEqual(args.registry_action, "file")
        self.assertEqual(args.registry_file_action, "upload")
        self.assertEqual(args.path, Path("cache/iso/anatase.iso"))
        self.assertEqual(args.output_path, "isos/anatase.iso")
        self.assertEqual(args.download_name, "anatase-44.20260627.iso")
        self.assertFalse(args.sign)

    def test_upload_file_parser_accepts_sign(self) -> None:
        args = build_parser().parse_args(
            [
                "registry",
                "file",
                "upload",
                "--sign",
                "cache/iso/anatase.iso",
                "isos/anatase.iso",
                "anatase-44.20260627.iso",
            ]
        )

        self.assertTrue(args.sign)

    def test_upload_file_parser_allows_missing_download_name(self) -> None:
        args = build_parser().parse_args(
            [
                "registry",
                "file",
                "upload",
                "cache/iso/anatase.iso",
                "isos/anatase.iso",
            ]
        )

        self.assertEqual(args.registry_action, "file")
        self.assertEqual(args.registry_file_action, "upload")
        self.assertEqual(args.path, Path("cache/iso/anatase.iso"))
        self.assertEqual(args.output_path, "isos/anatase.iso")
        self.assertIsNone(args.download_name)

    def test_upload_file_delete_parser(self) -> None:
        args = build_parser().parse_args(
            ["registry", "file", "delete", "isos/anatase.iso"]
        )

        self.assertEqual(args.registry_action, "file")
        self.assertEqual(args.registry_file_action, "delete")
        self.assertEqual(args.output_path, "isos/anatase.iso")

    def test_upload_file_command_dispatches_upload(self) -> None:
        args = build_parser().parse_args(
            [
                "registry",
                "file",
                "upload",
                "cache/iso/anatase.iso",
                "isos/anatase.iso",
                "anatase-44.20260627.iso",
            ]
        )

        with patch("ludos.__main__.upload_file", return_value=0) as upload:
            self.assertEqual(args.func(args), 0)

        upload.assert_called_once_with(
            Path("cache/iso/anatase.iso"),
            "isos/anatase.iso",
            "anatase-44.20260627.iso",
            sign=False,
        )

    def test_upload_file_command_dispatches_upload_without_download_name(self) -> None:
        args = build_parser().parse_args(
            [
                "registry",
                "file",
                "upload",
                "cache/iso/anatase.iso",
                "isos/anatase.iso",
            ]
        )

        with patch("ludos.__main__.upload_file", return_value=0) as upload:
            self.assertEqual(args.func(args), 0)

        upload.assert_called_once_with(
            Path("cache/iso/anatase.iso"),
            "isos/anatase.iso",
            None,
            sign=False,
        )

    def test_upload_file_command_dispatches_sign(self) -> None:
        args = build_parser().parse_args(
            [
                "registry",
                "file",
                "upload",
                "--sign",
                "cache/iso/anatase.iso",
                "isos/anatase.iso",
            ]
        )

        with patch("ludos.__main__.upload_file", return_value=0) as upload:
            self.assertEqual(args.func(args), 0)

        upload.assert_called_once_with(
            Path("cache/iso/anatase.iso"),
            "isos/anatase.iso",
            None,
            sign=True,
        )

    def test_upload_file_command_dispatches_delete(self) -> None:
        args = build_parser().parse_args(
            ["registry", "file", "delete", "isos/anatase.iso"]
        )

        with patch("ludos.__main__.delete_file", return_value=0) as delete:
            self.assertEqual(args.func(args), 0)

        delete.assert_called_once_with("isos/anatase.iso")

    def test_s3_api_parses_endpoint_and_bucket(self) -> None:
        config = _s3_config_from_env(ENV)

        self.assertEqual(
            config,
            S3Config(
                endpoint_url="https://s3.example.com",
                bucket="anatase-artifacts",
            ),
        )

    def test_upload_sets_content_disposition_and_creates_sha256sums(self) -> None:
        client = FakeS3Client()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "anatase.iso"
            path.write_bytes(b"installer")
            digest = hashlib.sha256(b"installer").hexdigest()

            upload_file(
                path,
                "isos/anatase.iso",
                "anatase-44.20260627.iso",
                environ=ENV,
                client=client,
            )

        self.assertEqual(client.uploads[0]["Bucket"], "anatase-artifacts")
        self.assertEqual(client.uploads[0]["Key"], "isos/anatase.iso")
        self.assertEqual(
            client.uploads[0]["ExtraArgs"],
            {
                "ContentDisposition": 'attachment; filename="anatase-44.20260627.iso"',
                "CacheControl": REGISTRY_SHORT_CACHE_CONTROL,
            },
        )
        self.assertIsNotNone(client.uploads[0]["Callback"])
        self.assertEqual(client.gets[0]["Key"], "isos/SHA256SUMS")
        self.assertEqual(
            client.objects[("anatase-artifacts", "isos/SHA256SUMS")].decode("utf-8"),
            f"{digest} anatase-44.20260627.iso\n",
        )

    def test_upload_without_download_name_omits_content_disposition_and_uses_filename(
        self,
    ) -> None:
        client = FakeS3Client()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "anatase.iso"
            path.write_bytes(b"installer")
            digest = hashlib.sha256(b"installer").hexdigest()

            upload_file(
                path,
                "isos/anatase.iso",
                environ=ENV,
                client=client,
            )

        self.assertEqual(client.uploads[0]["Bucket"], "anatase-artifacts")
        self.assertEqual(client.uploads[0]["Key"], "isos/anatase.iso")
        self.assertEqual(
            client.uploads[0]["ExtraArgs"],
            {"CacheControl": REGISTRY_SHORT_CACHE_CONTROL},
        )
        self.assertIsNotNone(client.uploads[0]["Callback"])
        self.assertEqual(client.gets[0]["Key"], "isos/SHA256SUMS")
        self.assertEqual(client.puts[0]["CacheControl"], REGISTRY_SHORT_CACHE_CONTROL)
        self.assertEqual(
            client.objects[("anatase-artifacts", "isos/SHA256SUMS")].decode("utf-8"),
            f"{digest} anatase.iso\n",
        )

    def test_upload_replaces_download_name_and_preserves_other_entries(self) -> None:
        checksum_key = ("anatase-artifacts", "isos/SHA256SUMS")
        client = FakeS3Client(
            {
                checksum_key: (
                    b"old111 other.iso\n"
                    b"old222 anatase-44.20260627.iso\n"
                    b"old333 older.iso\n"
                )
            }
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "anatase.iso"
            path.write_bytes(b"new iso")
            digest = hashlib.sha256(b"new iso").hexdigest()

            upload_file(
                path,
                "isos/anatase.iso",
                "anatase-44.20260627.iso",
                environ=ENV,
                client=client,
            )

        self.assertEqual(
            client.objects[checksum_key].decode("utf-8"),
            (
                f"{digest} anatase-44.20260627.iso\n"
                "old111 other.iso\n"
                "old333 older.iso\n"
            ),
        )

    def test_upload_truncates_sha256sums_to_twenty_entries(self) -> None:
        checksum_key = ("anatase-artifacts", "isos/SHA256SUMS")
        old_entries = "".join(f"{index:064x} old-{index}.iso\n" for index in range(25))
        client = FakeS3Client({checksum_key: old_entries.encode("utf-8")})
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "anatase.iso"
            path.write_bytes(b"new iso")
            digest = hashlib.sha256(b"new iso").hexdigest()

            upload_file(
                path,
                "isos/anatase.iso",
                "anatase-44.20260627.iso",
                environ=ENV,
                client=client,
            )

        lines = client.objects[checksum_key].decode("utf-8").splitlines()
        self.assertEqual(len(lines), 20)
        self.assertEqual(lines[0], f"{digest} anatase-44.20260627.iso")
        self.assertEqual(lines[-1], f"{18:064x} old-18.iso")

    def test_upload_sign_uploads_detached_signatures_and_reuses_digest(self) -> None:
        client = FakeS3Client()

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "anatase.iso"
            path.write_bytes(b"installer")
            digest = hashlib.sha256(b"installer").hexdigest()

            with (
                patch("ludos.upload.file.gpg_config_from_env", return_value=object()),
                patch(
                    "ludos.upload.file.sign_detached_digest",
                    return_value=b"detached signature",
                ) as sign_detached,
            ):
                upload_file(
                    path,
                    "isos/anatase.iso",
                    "anatase-44.20260627.iso",
                    sign=True,
                    environ=ENV,
                    client=client,
                )

        signed_digest = sign_detached.call_args.args[0]
        self.assertEqual(signed_digest.hexdigest(), digest)
        self.assertEqual(
            [
                (put["Key"], put["ContentType"], put["Body"])
                for put in client.puts
                if str(put["Key"]).endswith(".sig")
            ],
            [
                (
                    "isos/anatase.iso.sig",
                    "application/octet-stream",
                    b"detached signature",
                ),
                (
                    "isos/anatase-44.20260627.iso.sig",
                    "application/octet-stream",
                    b"detached signature",
                ),
            ],
        )

    def test_upload_sign_without_download_name_uploads_one_signature(self) -> None:
        client = FakeS3Client()

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "anatase.iso"
            path.write_bytes(b"installer")

            with (
                patch("ludos.upload.file.gpg_config_from_env", return_value=object()),
                patch(
                    "ludos.upload.file.sign_detached_digest",
                    return_value=b"detached signature",
                ),
            ):
                upload_file(
                    path,
                    "isos/anatase.iso",
                    sign=True,
                    environ=ENV,
                    client=client,
                )

        self.assertEqual(
            [
                put["Key"]
                for put in client.puts
                if str(put["Key"]).endswith(".sig")
            ],
            ["isos/anatase.iso.sig"],
        )

    def test_delete_file_does_not_touch_sha256sums(self) -> None:
        client = FakeS3Client(
            {
                ("anatase-artifacts", "isos/anatase.iso"): b"installer",
                ("anatase-artifacts", "isos/SHA256SUMS"): b"old111 old.iso\n",
            }
        )

        delete_file("isos/anatase.iso", environ=ENV, client=client)

        self.assertEqual(
            client.deletes,
            [{"Bucket": "anatase-artifacts", "Key": "isos/anatase.iso"}],
        )
        self.assertEqual(client.gets, [])
        self.assertEqual(client.puts, [])
        self.assertIn(("anatase-artifacts", "isos/SHA256SUMS"), client.objects)


if __name__ == "__main__":
    unittest.main()
