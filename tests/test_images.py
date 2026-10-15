"""
Unit tests for the shared picture handling in `core.images`.

The point of the module is that a picture posted by the bot is *rendered* by
Discord rather than offered as a download, so most of what is checked here is
that the filename follows the bytes and that anything Discord cannot show is
turned away with a sentence saying why.
"""

from types import SimpleNamespace
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aiohttp

from core import images

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"pretend-png-body"
JPG_BYTES = b"\xff\xd8\xff\xe0" + b"pretend-jpeg-body"
GIF_BYTES = b"GIF89a" + b"pretend-gif-body"
WEBP_BYTES = b"RIFF" + b"\x20\x00\x00\x00" + b"WEBP" + b"pretend-webp-body"

URL = "https://cdn.example.com/banner.png"


def run(coroutine):
    return asyncio.run(coroutine)


class FakeResponse:
    """The slice of `aiohttp.ClientResponse` the downloader actually touches."""

    def __init__(self, *, status=200, body=PNG_BYTES, content_length=None):
        self.status = status
        self.content_length = len(body) if content_length is None else content_length
        self.content = SimpleNamespace(read=self._read)
        self._body = body

    async def _read(self, limit):
        return self._body[:limit]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class FakeSession:
    """Stands in for a `ClientSession`, one queued outcome per URL."""

    def __init__(self, responses):
        self.responses = responses
        self.requested = []
        self.closed = False

    async def close(self):
        self.closed = True

    def get(self, url):
        self.requested.append(url)
        outcome = self.responses[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeAttachment:
    """The slice of `discord.Attachment` a modal upload is read through."""

    def __init__(self, filename, body=PNG_BYTES, size=None, error=None):
        self.filename = filename
        self.size = len(body) if size is None else size
        self._body = body
        self._error = error

    async def read(self):
        if self._error is not None:
            raise self._error
        return self._body


class TestFormatDetection(unittest.TestCase):
    def test_every_format_discord_renders_is_recognised(self):
        self.assertEqual(images.detect_format(PNG_BYTES), "png")
        self.assertEqual(images.detect_format(JPG_BYTES), "jpg")
        self.assertEqual(images.detect_format(GIF_BYTES), "gif")
        self.assertEqual(images.detect_format(WEBP_BYTES), "webp")

    def test_a_riff_file_that_is_not_webp_is_not_an_image(self):
        self.assertIsNone(images.detect_format(b"RIFF" + b"\x00" * 4 + b"WAVE"))

    def test_anything_else_is_refused(self):
        self.assertIsNone(images.detect_format(b"<svg xmlns='...'>"))
        self.assertIsNone(images.detect_format(b""))

    def test_a_rejected_file_is_named_so_the_message_is_useful(self):
        self.assertEqual(images.describe_payload(b"<svg viewBox='0'>"), "an SVG")
        self.assertEqual(images.describe_payload(b"%PDF-1.7"), "a PDF")
        self.assertEqual(images.describe_payload(b"BM" + b"\x00" * 20), "a BMP")
        self.assertEqual(
            images.describe_payload(b"\x00\x00\x00\x18ftypheic"), "a HEIC photo"
        )
        self.assertEqual(
            images.describe_payload(b"\x00\x00\x00\x18ftypavif"), "an AVIF"
        )
        self.assertIn("web page", images.describe_payload(b"<!DOCTYPE html>"))
        self.assertIn("cannot show", images.describe_payload(b"\x01\x02\x03\x04"))


class TestFilenames(unittest.TestCase):
    def test_the_extension_comes_from_the_bytes_not_the_link(self):
        # The exact failure this whole module exists for: a WebP served under
        # a `.png` name is not rendered by Discord at all.
        self.assertEqual(images.safe_filename("banner.png", "webp", "x"), "banner.webp")

    def test_a_query_string_is_left_off(self):
        self.assertEqual(
            images.safe_filename("banner.png?width=600", "png", "x"), "banner.png"
        )

    def test_path_separators_cannot_escape_the_filename(self):
        name = images.safe_filename("%2e%2e%2f%2e%2e%2fsecret.png", "png", "x")
        self.assertNotIn("/", name)
        self.assertNotIn("..", name)

    def test_a_nameless_source_falls_back(self):
        self.assertEqual(images.safe_filename("", "gif", "image-3"), "image-3.gif")

    def test_the_label_prefers_the_filename_then_the_host(self):
        self.assertEqual(images.name_from_url(URL), "banner.png")
        self.assertEqual(
            images.name_from_url("https://cdn.example.com"), "cdn.example.com"
        )


class TestDownloading(unittest.TestCase):
    def test_a_good_link_is_read_and_typed_from_its_bytes(self):
        session = FakeSession({URL: FakeResponse(body=JPG_BYTES)})
        picture = run(images.load_from_url(URL, session=session))
        self.assertEqual(picture.format, "jpg")
        self.assertEqual(picture.payload, JPG_BYTES)
        self.assertEqual(picture.source_name, "banner.png")

    def test_a_dead_link_says_so_and_points_at_uploading(self):
        session = FakeSession({URL: FakeResponse(status=404)})
        with self.assertRaises(images.ImageRejected) as caught:
            run(images.load_from_url(URL, session=session))
        self.assertIn("HTTP 404", caught.exception.reason)
        self.assertIn("upload", caught.exception.reason)

    def test_a_web_page_is_refused_by_what_it_contains(self):
        session = FakeSession({URL: FakeResponse(body=b"<!DOCTYPE html><html>")})
        with self.assertRaises(images.ImageRejected) as caught:
            run(images.load_from_url(URL, session=session))
        self.assertIn("web page", caught.exception.reason)

    def test_an_oversized_file_is_refused_before_it_is_read(self):
        session = FakeSession(
            {
                URL: FakeResponse(
                    body=PNG_BYTES, content_length=images.MAX_IMAGE_BYTES + 1
                )
            }
        )
        with self.assertRaises(images.ImageRejected) as caught:
            run(images.load_from_url(URL, session=session))
        self.assertIn("upload limit", caught.exception.reason)

    def test_an_understated_length_is_caught_on_the_way_in(self):
        oversized = PNG_BYTES + b"x" * images.MAX_IMAGE_BYTES
        session = FakeSession({URL: FakeResponse(body=oversized, content_length=10)})
        with self.assertRaises(images.ImageRejected) as caught:
            run(images.load_from_url(URL, session=session))
        self.assertIn("upload limit", caught.exception.reason)

    def test_an_empty_body_is_refused(self):
        session = FakeSession({URL: FakeResponse(body=b"")})
        with self.assertRaises(images.ImageRejected) as caught:
            run(images.load_from_url(URL, session=session))
        self.assertIn("empty", caught.exception.reason)

    def test_a_connection_failure_is_turned_into_a_sentence(self):
        session = FakeSession({URL: aiohttp.ClientError("boom")})
        with self.assertRaises(images.ImageRejected) as caught:
            run(images.load_from_url(URL, session=session))
        self.assertIn("could not be opened", caught.exception.reason)

    def test_a_timeout_is_turned_into_a_sentence(self):
        session = FakeSession({URL: TimeoutError()})
        with self.assertRaises(images.ImageRejected) as caught:
            run(images.load_from_url(URL, session=session))
        self.assertIn("could not be opened", caught.exception.reason)


class TestUploads(unittest.TestCase):
    def test_an_uploaded_file_keeps_its_name_and_gains_its_real_format(self):
        picture = run(images.load_from_attachment(FakeAttachment("logo.png")))
        self.assertEqual(picture.source_name, "logo.png")
        self.assertEqual(picture.format, "png")

    def test_an_oversized_upload_is_refused_without_reading_it(self):
        attachment = FakeAttachment(
            "huge.png", size=images.MAX_IMAGE_BYTES + 1, error=AssertionError("read")
        )
        with self.assertRaises(images.ImageRejected) as caught:
            run(images.load_from_attachment(attachment))
        self.assertIn("upload limit", caught.exception.reason)

    def test_an_upload_discord_cannot_show_is_refused_by_name(self):
        attachment = FakeAttachment("logo.svg", body=b"<svg xmlns='x'>")
        with self.assertRaises(images.ImageRejected) as caught:
            run(images.load_from_attachment(attachment))
        self.assertIn("SVG", caught.exception.reason)


class TestAssetStore(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name) / "banners"
        patcher = patch.object(images, "BANNER_ASSET_PATH", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._directory.cleanup)

    def _stored(self, payload=PNG_BYTES, name="logo.png"):
        image_format = images.detect_format(payload)
        return images.store(
            images.LoadedImage(
                payload=payload, format=image_format, source_name=name
            )
        )

    def test_a_stored_picture_can_be_read_back(self):
        asset_id = self._stored()
        self.assertTrue(asset_id.endswith(".png"))
        self.assertEqual(images.path_for(asset_id).read_bytes(), PNG_BYTES)

    def test_the_id_follows_the_bytes_so_the_same_picture_costs_nothing_twice(self):
        self.assertEqual(self._stored(name="a.png"), self._stored(name="b.png"))

    def test_different_pictures_get_different_ids(self):
        self.assertNotEqual(self._stored(PNG_BYTES), self._stored(JPG_BYTES))

    def test_nothing_is_left_behind_by_a_write(self):
        self._stored()
        self.assertEqual([path.suffix for path in self.root.iterdir()], [".png"])

    def test_a_stored_row_cannot_name_a_file_outside_the_folder(self):
        for hostile in ("../../config.py", "/etc/passwd", "logo.png", None, 7):
            self.assertFalse(images.is_asset_id(hostile), hostile)
            self.assertIsNone(images.path_for(hostile), hostile)

    def test_a_missing_file_reads_back_as_nothing(self):
        asset_id = self._stored()
        images.path_for(asset_id).unlink()
        self.assertIsNone(images.path_for(asset_id))
        self.assertIsNone(images.file_for(asset_id))

    def test_the_embed_reference_names_the_upload(self):
        self.assertEqual(images.reference("abc.png"), "attachment://abc.png")


if __name__ == "__main__":
    unittest.main()
