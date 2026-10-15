"""
Unit tests for the Normal Message composer.
"""

from types import SimpleNamespace
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import images
from tests.test_images import (
    GIF_BYTES,
    JPG_BYTES,
    PNG_BYTES,
    FakeAttachment,
    FakeResponse,
    FakeSession,
)
from views.plain_message_view import (
    MAX_IMAGES,
    PlainMessageState,
    PlainMessageView,
    StagedImage,
    build_composer_embeds,
    composer_files,
    missing_assets,
    stage_images,
)

PNG = "https://cdn.example.com/banner.png"
JPG = "https://cdn.example.com/photo.jpg"

# Discord's own ceilings for the parts of an embed the composer fills in.
EMBED_FIELD_LIMIT = 1024
EMBED_TITLE_LIMIT = 256
SELECT_DESCRIPTION_LIMIT = 100


def run(coroutine):
    return asyncio.run(coroutine)


def _names(files):
    """The upload names, closing the handles the test opened to read them."""
    try:
        return [file.filename for file in files]
    finally:
        for file in files:
            file.close()


class AssetFolder(unittest.TestCase):
    """Base class that points the asset store at a throwaway folder."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        patcher = patch.object(
            images, "BANNER_ASSET_PATH", Path(directory.name) / "banners"
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(directory.cleanup)

    def staged(self, label="banner.png", payload=PNG_BYTES):
        """One picture already on a draft, stored the way the composer does."""
        image_format = images.detect_format(payload)
        asset_id = images.store(
            images.LoadedImage(
                payload=payload, format=image_format, source_name=label
            )
        )
        return StagedImage(
            asset_id=asset_id,
            label=label,
            format=image_format,
            description=f"{label} - 0.0 MB {images.FORMAT_NAMES[image_format]}",
        )


class TestDraftState(AssetFolder):
    def test_a_new_draft_is_blank(self):
        self.assertTrue(PlainMessageState(user_id=1).is_blank)

    def test_text_alone_is_enough_to_post(self):
        self.assertFalse(PlainMessageState(user_id=1, content="hello").is_blank)

    def test_a_picture_alone_is_enough_to_post(self):
        state = PlainMessageState(user_id=1, images=[self.staged()])
        self.assertFalse(state.is_blank)

    def test_free_slots_track_the_staged_pictures(self):
        state = PlainMessageState(user_id=1)
        self.assertEqual(state.free_slots, MAX_IMAGES)
        state.images.append(self.staged())
        self.assertEqual(state.free_slots, MAX_IMAGES - 1)

    def test_placeholders_are_resolved_for_the_preview(self):
        state = PlainMessageState(user_id=1, content="Welcome to {server_name}!")
        guild = SimpleNamespace(name="SafeRest", member_count=10, icon=None)
        self.assertEqual(state.render_content(guild), "Welcome to SafeRest!")

    def test_a_notice_is_shown_once(self):
        state = PlainMessageState(user_id=1)
        state.inform("Saved.", "success")
        self.assertEqual(state.take_notice(), ("Saved.", "success"))
        self.assertEqual(state.take_notice(), (None, "info"))


class TestStaging(AssetFolder):
    def _session(self, responses):
        return patch(
            "views.plain_message_view.aiohttp.ClientSession",
            return_value=_AsyncContext(FakeSession(responses)),
        )

    def test_uploads_are_stored_and_kept_in_order(self):
        notice, level = run(
            stage_images(
                state := PlainMessageState(user_id=1),
                uploads=[
                    FakeAttachment("logo.png", PNG_BYTES),
                    FakeAttachment("shot.jpg", JPG_BYTES),
                ],
            )
        )
        self.assertEqual([image.label for image in state.images], ["logo.png", "shot.jpg"])
        self.assertEqual([image.format for image in state.images], ["png", "jpg"])
        self.assertEqual(level, "success")
        self.assertIn("**2**", notice)

    def test_a_link_is_downloaded_and_stored_while_the_draft_is_open(self):
        state = PlainMessageState(user_id=1)
        with self._session({PNG: FakeResponse(body=PNG_BYTES)}):
            notice, level = run(stage_images(state, raw=PNG))
        self.assertEqual(level, "success")
        self.assertEqual(len(state.images), 1)
        self.assertEqual(images.path_for(state.images[0].asset_id).read_bytes(), PNG_BYTES)

    def test_blank_lines_and_padding_are_ignored(self):
        state = PlainMessageState(user_id=1)
        with self._session({PNG: FakeResponse(body=PNG_BYTES)}):
            run(stage_images(state, raw=f"\n  {PNG}  \n\n"))
        self.assertEqual(len(state.images), 1)

    def test_non_http_links_are_refused_and_named(self):
        state = PlainMessageState(user_id=1)
        notice, level = run(
            stage_images(state, raw="javascript:alert(1)\nfile:///etc/passwd")
        )
        self.assertEqual(state.images, [])
        self.assertEqual(level, "warning")
        self.assertIn("javascript:alert(1)", notice)

    def test_a_dead_link_is_reported_against_its_own_name(self):
        state = PlainMessageState(user_id=1)
        with self._session({PNG: FakeResponse(status=404), JPG: FakeResponse(body=JPG_BYTES)}):
            notice, level = run(stage_images(state, raw=f"{PNG}\n{JPG}"))
        self.assertEqual(level, "warning")
        self.assertIn("banner.png", notice)
        self.assertIn("HTTP 404", notice)
        # The one that worked is still staged - a bad link spoils only itself.
        self.assertEqual([image.label for image in state.images], ["photo.jpg"])

    def test_a_file_discord_cannot_render_is_turned_away(self):
        state = PlainMessageState(user_id=1)
        notice, level = run(
            stage_images(state, uploads=[FakeAttachment("art.svg", b"<svg xmlns='x'>")])
        )
        self.assertEqual(state.images, [])
        self.assertEqual(level, "warning")
        self.assertIn("SVG", notice)

    def test_the_same_picture_is_not_staged_twice(self):
        state = PlainMessageState(user_id=1)
        run(stage_images(state, uploads=[FakeAttachment("logo.png", PNG_BYTES)]))
        notice, level = run(
            # A different name, the same bytes: still the same picture.
            stage_images(state, uploads=[FakeAttachment("copy.png", PNG_BYTES)])
        )
        self.assertEqual(len(state.images), 1)
        self.assertEqual(level, "warning")
        self.assertIn("already on the draft", notice)

    def test_staging_stops_at_the_picture_limit(self):
        state = PlainMessageState(user_id=1)
        uploads = [
            FakeAttachment(f"{index}.png", PNG_BYTES + bytes([index]))
            for index in range(MAX_IMAGES + 2)
        ]
        notice, level = run(stage_images(state, uploads=uploads))
        self.assertEqual(len(state.images), MAX_IMAGES)
        self.assertEqual(level, "warning")
        self.assertIn("**2** did not fit", notice)

    def test_an_empty_box_says_so(self):
        notice, level = run(stage_images(PlainMessageState(user_id=1), raw="   \n  "))
        self.assertEqual(level, "warning")
        self.assertIn("Nothing was added", notice)


class TestUploadNames(AssetFolder):
    def test_a_picture_is_posted_under_the_extension_its_bytes_call_for(self):
        # The bug this guards: a WebP fetched from a `.png` link used to be
        # uploaded as `.png`, and Discord showed a download card instead.
        webp = b"RIFF" + b"\x20\x00\x00\x00" + b"WEBP" + b"body"
        state = PlainMessageState(user_id=1)
        with patch(
            "views.plain_message_view.aiohttp.ClientSession",
            return_value=_AsyncContext(FakeSession({PNG: FakeResponse(body=webp)})),
        ):
            run(stage_images(state, raw=PNG))
        self.assertEqual(_names(composer_files(state.images)), ["banner.webp"])

    def test_two_pictures_with_one_name_stay_apart(self):
        state = PlainMessageState(
            user_id=1,
            images=[
                self.staged("logo.png", PNG_BYTES),
                self.staged("logo.png", GIF_BYTES),
            ],
        )
        self.assertEqual(len(set(_names(composer_files(state.images)))), 2)

    def test_a_vanished_asset_is_named_rather_than_posted(self):
        state = PlainMessageState(user_id=1, images=[self.staged()])
        images.path_for(state.images[0].asset_id).unlink()
        self.assertEqual(composer_files(state.images), [])
        self.assertEqual(missing_assets(state.images), ["**banner.png**"])


class TestComposerScreen(AssetFolder):
    guild = SimpleNamespace(name="SafeRest", member_count=1200, icon=None)

    def test_an_empty_draft_prompts_for_text(self):
        embeds = build_composer_embeds(PlainMessageState(user_id=1), self.guild)
        self.assertIn("Edit message", embeds[0].fields[0].value)

    def test_the_card_measures_the_written_text(self):
        state = PlainMessageState(user_id=1, content="line one\nline two")
        card = build_composer_embeds(state, self.guild)[0]
        self.assertIn("across **2** lines", card.fields[0].value)

    def test_every_staged_picture_previews_the_file_that_will_be_posted(self):
        state = PlainMessageState(
            user_id=1,
            content="hi",
            images=[self.staged("banner.png", PNG_BYTES), self.staged("photo.jpg", JPG_BYTES)],
        )
        previews = [
            embed for embed in build_composer_embeds(state, self.guild) if embed.image
        ]
        # Pointed at the message's own uploads, not at a link that might
        # answer differently later.
        self.assertEqual(
            [embed.image.url for embed in previews],
            ["attachment://banner.png", "attachment://photo.jpg"],
        )

    def test_a_notice_is_rendered_above_the_previews(self):
        state = PlainMessageState(user_id=1, content="hi", images=[self.staged()])
        state.inform("Added **1** picture(s).", "success")
        embeds = build_composer_embeds(state, self.guild)
        self.assertEqual(len(embeds), 3)
        self.assertIsNone(embeds[1].image.url)
        self.assertEqual(embeds[2].image.url, "attachment://banner.png")

    def test_an_everyone_mention_is_flagged_as_silent(self):
        state = PlainMessageState(user_id=1, content="@everyone sale today")
        card = build_composer_embeds(state, self.guild)[0]
        self.assertIn("not** ping", " ".join(field.value for field in card.fields))

    def test_a_draft_without_a_ping_carries_no_warning(self):
        state = PlainMessageState(user_id=1, content="sale today")
        card = build_composer_embeds(state, self.guild)[0]
        self.assertEqual(len(card.fields), 2)

    def test_a_vanished_asset_is_flagged_on_the_card(self):
        state = PlainMessageState(user_id=1, images=[self.staged()])
        images.path_for(state.images[0].asset_id).unlink()
        card = build_composer_embeds(state, self.guild)[0]
        self.assertIn("went missing", card.fields[1].value)

    def test_a_full_screen_stays_inside_discords_limits(self):
        state = PlainMessageState(
            user_id=1,
            content="@everyone " + "word " * 300,
            images=[
                self.staged(f"{'n' * 80}-{index}.png", PNG_BYTES + bytes([index]))
                for index in range(MAX_IMAGES)
            ],
        )
        state.inform("Added **4** picture(s).", "success")
        embeds = build_composer_embeds(state, self.guild)
        # Ten embeds per message; the composer never comes close.
        self.assertLessEqual(len(embeds), 10)
        for embed in embeds:
            self.assertLessEqual(len(embed.title or ""), EMBED_TITLE_LIMIT)
            for embed_field in embed.fields:
                self.assertLessEqual(len(embed_field.value), EMBED_FIELD_LIMIT)


class TestComposerControls(AssetFolder):
    def test_a_text_only_draft_has_no_remove_control(self):
        view = PlainMessageView(PlainMessageState(user_id=1, content="hi"))
        names = [item.__class__.__name__ for item in view.children]
        self.assertNotIn("RemoveImageSelect", names)
        self.assertIn("PublishChannelSelect", names)

    def test_the_remove_control_appears_with_the_first_picture(self):
        state = PlainMessageState(
            user_id=1,
            content="hi",
            images=[self.staged("banner.png", PNG_BYTES), self.staged("photo.jpg", JPG_BYTES)],
        )
        select = next(
            item
            for item in PlainMessageView(state).children
            if item.__class__.__name__ == "RemoveImageSelect"
        )
        self.assertEqual([option.value for option in select.options], ["0", "1"])
        self.assertEqual(select.max_values, 2)
        for option in select.options:
            self.assertLessEqual(len(option.description), SELECT_DESCRIPTION_LIMIT)

    def test_adding_is_blocked_once_the_draft_is_full(self):
        state = PlainMessageState(
            user_id=1,
            images=[
                self.staged(f"{index}.png", PNG_BYTES + bytes([index]))
                for index in range(MAX_IMAGES)
            ],
        )
        self.assertTrue(PlainMessageView(state).add_images.disabled)

    def test_adding_is_open_while_there_is_room(self):
        view = PlainMessageView(PlainMessageState(user_id=1, images=[self.staged()]))
        self.assertFalse(view.add_images.disabled)

    def test_the_controls_fit_discords_five_action_rows(self):
        state = PlainMessageState(user_id=1, content="hi", images=[self.staged()])
        view = PlainMessageView(state)
        self.assertLessEqual(len({item.row for item in view.children}), 5)


class _AsyncContext:
    """Wraps a fake session so `async with ClientSession(...)` works."""

    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc_info):
        return False


if __name__ == "__main__":
    unittest.main()
