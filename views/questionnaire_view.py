"""Public entry button; all form answers and confirmations stay private."""

import logging

import discord

from config import BRAND_NAME, BRAND_COLOR
from core.questionnaire import validate_answers

logger = logging.getLogger(__name__)


def questionnaire_embed():
    return discord.Embed(
        title=f"Welcome to {BRAND_NAME}",
        description=(
            "Click **Start onboarding** to complete your member questionnaire.\n\n"
            "1. What's your TikTok handle?\n"
            "2. What's your email?\n"
            "3. What's your phone number?\n"
            "4. What's your TikTok GMV for the last 30 days?\n"
            "5. What's your birthday? **Optional** — share it if you'd like us to know when to say happy birthday!\n\n"
            "Your answers are saved by the bot and shared with our team in a private Slack channel. "
            "They won't be posted in this Discord channel. Only the birthday is optional; no birth year is needed."
        ),
        color=BRAND_COLOR,
    )


class QuestionnaireModal(discord.ui.Modal):
    def __init__(self, user, defaults=None):
        super().__init__(title=f"{BRAND_NAME} onboarding"[:45], timeout=900)
        self.user_id = user.id
        defaults = defaults or {}
        fields = (
            ("tt_handle", "What's your TT handle?", "@yourhandle", 100, True),
            ("email", "What's your email?", "name@example.com", 254, True),
            ("phone", "What's your phone number?", "Include your country code, e.g. +1 555 123 4567", 30, True),
            ("gmv", "What's your TT last 30 days GMV?", "Include currency, e.g. USD 5,000 (or USD 0)", 100, True),
            ("birthday", "What's your birthday? (optional)", "MM/DD — so we know when to say happy birthday!", 5, False),
        )
        self.inputs = {}
        for key, label, placeholder, limit, required in fields:
            field = discord.ui.TextInput(
                label=label, placeholder=placeholder, max_length=limit, required=required,
                default=defaults.get(key, ""),
            )
            self.inputs[key] = field
            self.add_item(field)

    async def on_submit(self, interaction):
        # Copies of the questionnaire can live in any channel of the server.
        if interaction.user.id != self.user_id or not interaction.guild:
            await interaction.response.send_message("Please open the questionnaire from the server.", ephemeral=True)
            return
        raw = {key: field.value for key, field in self.inputs.items()}
        try:
            answers = validate_answers(raw)
        except ValueError as error:
            await interaction.response.send_message(
                str(error), view=CorrectAnswersView(interaction.user.id, raw, self.reopen), ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        cog = interaction.client.get_cog("QuestionnaireCog")
        if cog is None:
            await interaction.followup.send("Onboarding is temporarily unavailable. Please try again later.", ephemeral=True)
            return
        try:
            saved = await cog.save_answers(interaction, answers)
        except Exception as error:
            logger.error("Questionnaire save failed (%s)", type(error).__name__)
            await interaction.followup.send(
                "Your answers could not be saved. Please try again.",
                view=CorrectAnswersView(interaction.user.id, raw, self.reopen), ephemeral=True,
            )
            return
        await self.after_save(interaction, saved)

    def reopen(self, user, answers):
        """A fresh, pre-filled copy of this form (used by "Edit answers")."""
        return type(self)(user, answers)

    async def after_save(self, interaction, saved):
        """Final reply once the answers are stored and queued for Slack."""
        await interaction.followup.send(
            "Thank you! Your answers are saved and queued for our team in Slack."
            if saved else "You've already submitted your onboarding answers. Contact a moderator if you need to correct them.",
            ephemeral=True,
        )


class CorrectAnswersView(discord.ui.View):
    def __init__(self, user_id, answers, form=None):
        super().__init__(timeout=900)
        self.user_id, self.answers = user_id, answers
        self.form = form or QuestionnaireModal

    @discord.ui.button(label="Edit answers", style=discord.ButtonStyle.primary)
    async def correct(self, interaction, button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This form belongs to another member.", ephemeral=True)
            return
        await interaction.response.send_modal(self.form(interaction.user, self.answers))


class MemberQuestionnaireView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Start onboarding", style=discord.ButtonStyle.primary,
                       custom_id="member_questionnaire:start:v1")
    async def start(self, interaction, button):
        if not interaction.guild or interaction.user.bot:
            await interaction.response.send_message("Use the questionnaire in the server.", ephemeral=True)
            return
        # No database/network work before showing the modal (Discord's 3-second deadline).
        await interaction.response.send_modal(QuestionnaireModal(interaction.user))
