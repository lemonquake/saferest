"""
Unit tests for the embed banner: the shared modal and how a draft renders it.

The failure these guard against is the one moderators actually reported - a
picture that arrives as a download card underneath the post instead of inside
the embed. That happens when the file and the `attachment://` reference the
embed uses to name it ever disagree, so most of what is checked here is that
they are decided together and stay in step.
"""

import asyncio
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

from core import images
from core.embed_script import EmbedScript
from tests.test_images import (
    JPG_BYTES,
    PNG_BYTES,
    FakeAttachment,
    FakeResponse,
    FakeSession,
)
from views.banner import apply_banner, describe_banner

LINK = "https://cdn.example.com/banner.png"
ICON = "https://cdn.example.com/icon.png"


def run(coroutine):
    return asyncio.run(coroutine)


class BannerCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        patcher = patch.object(
            images, "BANNER_ASSET_PATH", Path(directory.name) / "banners"
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(directory.cleanup)
        self.script = EmbedScript(user_id=1)
        self.script.title = "New arrivals"

    def stored(self, payload=PNG_BYTES, name="logo.png"):
        image_format = images.detect_format(payload)
        return images.store(
            images.LoadedImage(
                payload=payload, format=image_format, source_name=name
            )
        )

    def apply(self, *, uploads=(), typed=None, remove=None, responses=None):
        if responses is None:
            return run(
                apply_banner(
                    self.script, uploads=list(uploads), typed=typed, remove=remove
                )
            )
        with patch(
            "core.images.aiohttp.ClientSession",
            return_value=FakeSession(responses),
        ):
            return run(
                apply_banner(
                    self.script, uploads=list(uploads), typed=typed, remove=remove
                )
            )


class TestRendering(BannerCase):
    def test_an_uploaded_banner_is_named_by_the_embed_and_sent_with_it(self):
        self.script.set_image_upload(asset_id := self.stored())
        embed = self.script.render_embed()
        files = self.script.render_files()
        self.addCleanup(lambda: [file.close() for file in files])
        # The two halves have to agree or the picture becomes a download.
        self.assertEqual(embed.image.url, f"attachment://{asset_id}")
        self.assertEqual([file.filename for file in files], [asset_id])

    def test_a_linked_banner_needs_no_upload(self):
        self.script.set_image_link(LINK)
        self.assertEqual(self.script.render_embed().image.url, LINK)
        self.assertEqual(self.script.render_files(), [])

    def test_a_draft_with_no_banner_attaches_nothing(self):
        self.assertIsNone(self.script.render_embed().image.url)
        self.assertEqual(self.script.render_files(), [])

    def test_an_upload_whose_file_vanished_never_leaves_a_dangling_reference(self):
        # Discord rejects the whole message when an embed names a file that
        # is not attached, so the banner has to drop out cleanly instead.
        self.script.set_image_upload(asset_id := self.stored())
        images.path_for(asset_id).unlink()
        self.assertIsNone(self.script.render_embed().image.url)
        self.assertEqual(self.script.render_files(), [])

    def test_a_vanished_upload_falls_back_to_the_link_it_had(self):
        self.script.set_image_upload(asset_id := self.stored())
        self.script.image_url = LINK
        images.path_for(asset_id).unlink()
        self.assertEqual(self.script.render_embed().image.url, LINK)

    def test_a_banner_alone_is_enough_to_post(self):
        self.script.title = None
        self.assertTrue(self.script.is_blank)
        self.script.set_image_upload(self.stored())
        self.assertFalse(self.script.is_blank)
        self.assertTrue(self.script.has_embed)


class TestPersistence(BannerCase):
    def test_an_uploaded_banner_survives_a_save_and_reload(self):
        self.script.set_image_upload(asset_id := self.stored())
        restored = EmbedScript.from_payload(self.script.to_payload(), user_id=2)
        self.assertEqual(restored.image_asset, asset_id)
        self.assertEqual(restored.render_embed().image.url, f"attachment://{asset_id}")

    def test_a_stored_row_cannot_point_at_a_file_outside_the_folder(self):
        restored = EmbedScript.from_payload(
            {"image_asset": "../../config.py", "title": "x"}, user_id=1
        )
        self.assertIsNone(restored.image_asset)

    def test_a_template_saved_before_uploads_existed_still_loads(self):
        restored = EmbedScript.from_payload(
            {"version": 2, "title": "x", "image_url": LINK}, user_id=1
        )
        self.assertIsNone(restored.image_asset)
        self.assertEqual(restored.image_url, LINK)

    def test_a_clone_carries_the_banner(self):
        self.script.set_image_upload(asset_id := self.stored())
        self.assertEqual(self.script.clone().image_asset, asset_id)


class TestApplyingAChoice(BannerCase):
    def test_an_upload_becomes_the_banner(self):
        notice, level = self.apply(uploads=[FakeAttachment("logo.png", PNG_BYTES)])
        self.assertEqual(level, "success")
        self.assertIsNotNone(self.script.image_asset)
        self.assertIn("not as a download", notice)

    def test_a_link_is_copied_so_it_cannot_expire_out_from_under_the_post(self):
        notice, level = self.apply(
            typed=LINK, responses={LINK: FakeResponse(body=JPG_BYTES)}
        )
        self.assertEqual(level, "success")
        self.assertIsNone(self.script.image_url)
        self.assertTrue(self.script.image_asset.endswith(".jpg"))
        self.assertIn("saved with the bot", notice)

    def test_an_upload_replaces_a_link_that_was_there(self):
        self.script.set_image_link(LINK)
        self.apply(uploads=[FakeAttachment("logo.png", PNG_BYTES)])
        self.assertIsNone(self.script.image_url)
        self.assertIsNotNone(self.script.image_asset)

    def test_a_refused_picture_leaves_the_draft_exactly_as_it_was(self):
        self.script.set_image_upload(original := self.stored())
        notice, level = self.apply(
            uploads=[FakeAttachment("art.svg", b"<svg xmlns='x'>")]
        )
        self.assertEqual(level, "error")
        self.assertIn("SVG", notice)
        self.assertEqual(self.script.image_asset, original)

    def test_a_dead_link_leaves_the_draft_exactly_as_it_was(self):
        self.script.set_image_upload(original := self.stored())
        notice, level = self.apply(
            typed=LINK, responses={LINK: FakeResponse(status=403)}
        )
        self.assertEqual(level, "error")
        self.assertIn("HTTP 403", notice)
        self.assertEqual(self.script.image_asset, original)

    def test_a_link_that_is_not_a_link_is_refused(self):
        notice, level = self.apply(typed="cdn.example.com/banner.png")
        self.assertEqual(level, "error")
        self.assertFalse(self.script.has_image)

    def test_removing_takes_the_banner_off(self):
        self.script.set_image_upload(self.stored())
        notice, level = self.apply(remove="yes")
        self.assertEqual(level, "success")
        self.assertFalse(self.script.has_image)

    def test_removing_and_replacing_at_once_changes_nothing(self):
        self.script.set_image_upload(original := self.stored())
        notice, level = self.apply(
            uploads=[FakeAttachment("logo.png", JPG_BYTES)], remove="yes"
        )
        self.assertEqual(level, "warning")
        self.assertIn("Pick one", notice)
        self.assertEqual(self.script.image_asset, original)

    def test_an_empty_submit_never_wipes_an_upload_it_could_not_show(self):
        # The text box cannot display an uploaded picture, so leaving it blank
        # must not be read as "delete it".
        self.script.set_image_upload(original := self.stored())
        notice, level = self.apply()
        self.assertEqual(level, "info")
        self.assertEqual(self.script.image_asset, original)


class TestSummaryLine(BannerCase):
    def test_an_upload_is_described_as_one(self):
        self.script.set_image_upload(self.stored())
        self.assertIn("Uploaded picture", describe_banner(self.script, missing="none"))

    def test_a_link_is_shown_as_a_link(self):
        self.script.set_image_link(LINK)
        self.assertEqual(describe_banner(self.script, missing="none"), f"[Linked image]({LINK})")

    def test_a_missing_file_is_called_out_rather_than_claimed_to_work(self):
        self.script.set_image_upload(asset_id := self.stored())
        images.path_for(asset_id).unlink()
        self.assertIn("missing", describe_banner(self.script, missing="none"))

    def test_no_banner_uses_the_caller_s_wording(self):
        self.assertEqual(describe_banner(self.script, missing="none yet"), "none yet")



class TestCleanPictures(BannerCase):
    """A post with a picture carries no server icon, footer or timestamp."""

    def setUp(self):
        super().setUp()
        self.guild = SimpleNamespace(
            name="Server", member_count=10, icon=SimpleNamespace(url=ICON)
        )

    def test_a_picture_with_text_has_no_icon_footer_or_timestamp(self):
        self.script.set_image_upload(self.stored())
        embed = self.script.render_embed(guild=self.guild)
        self.assertIsNone(embed.thumbnail.url)
        self.assertIsNone(embed.footer.text)
        self.assertIsNone(embed.timestamp)
        self.assertEqual(embed.title, "New arrivals")

    def test_an_uploaded_picture_alone_posts_as_the_picture_itself(self):
        self.script.title = None
        self.script.set_image_upload(self.stored())
        self.assertIsNone(self.script.render_embed(guild=self.guild))
        files = self.script.render_files()
        self.addCleanup(lambda: [file.close() for file in files])
        self.assertEqual(len(files), 1)
        self.assertFalse(self.script.is_blank)

    def test_a_linked_picture_alone_keeps_a_bare_frame(self):
        self.script.title = None
        self.script.set_image_link(LINK)
        embed = self.script.render_embed(guild=self.guild)
        self.assertEqual(embed.image.url, LINK)
        self.assertIsNone(embed.thumbnail.url)
        self.assertIsNone(embed.footer.text)

    def test_a_text_only_embed_keeps_its_branding(self):
        embed = self.script.render_embed(guild=self.guild)
        self.assertEqual(embed.thumbnail.url, ICON)
        self.assertIsNotNone(embed.footer.text)
        self.assertTrue(self.script.icon_visible)

    def test_the_icon_is_reported_hidden_once_a_picture_is_added(self):
        self.script.set_image_link(LINK)
        self.assertTrue(self.script.show_icon)
        self.assertFalse(self.script.icon_visible)


if __name__ == "__main__":
    unittest.main()
