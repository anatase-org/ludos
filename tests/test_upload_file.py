from __future__ import annotations

import hashlib
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from ludos.__main__ import build_parser
from ludos.upload.file import (
    S3Config,
    _s3_config_from_env,
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
    def __init__(self, objects: dict[tuple[str, str], bytes] | None = None) -> None:
        self.objects = {} if objects is None else dict(objects)
        self.uploads: list[dict[str, object]] = []
        self.puts: list[dict[str, object]] = []
        self.deletes: list[dict[str, object]] = []
        self.gets: list[dict[str, object]] = []

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
        if Callback is not None:
            Callback(len(data))  # type: ignore[operator]
        self.objects[(bucket, key)] = data

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, BytesIO]:
        self.gets.append({"Bucket": Bucket, "Key": Key})
        try:
            body = self.objects[(Bucket, Key)]
        except KeyError as exc:
            raise FakeClientError("NoSuchKey") from exc
        return {"Body": BytesIO(body)}

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
    ) -> None:
        self.puts.append(
            {
                "Bucket": Bucket,
                "Key": Key,
                "Body": Body,
                "ContentType": ContentType,
            }
        )
        self.objects[(Bucket, Key)] = Body

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.deletes.append({"Bucket": Bucket, "Key": Key})
        self.objects.pop((Bucket, Key), None)


class UploadFileTests(unittest.TestCase):
    def test_upload_file_parser(self) -> None:
        args = build_parser().parse_args(
            [
                "upload",
                "file",
                "cache/iso/anatase.iso",
                "isos/anatase.iso",
                "anatase-44.20260627.iso",
            ]
        )

        self.assertEqual(args.upload_action, "file")
        self.assertEqual(args.path, Path("cache/iso/anatase.iso"))
        self.assertEqual(args.output_path, "isos/anatase.iso")
        self.assertEqual(args.download_name, "anatase-44.20260627.iso")

    def test_upload_delete_parser(self) -> None:
        args = build_parser().parse_args(["upload", "delete", "isos/anatase.iso"])

        self.assertEqual(args.upload_action, "delete")
        self.assertEqual(args.output_path, "isos/anatase.iso")

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
            },
        )
        self.assertIsNotNone(client.uploads[0]["Callback"])
        self.assertEqual(client.gets[0]["Key"], "isos/SHA256SUMS")
        self.assertEqual(
            client.objects[("anatase-artifacts", "isos/SHA256SUMS")].decode("utf-8"),
            f"{digest} anatase-44.20260627.iso\n",
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
