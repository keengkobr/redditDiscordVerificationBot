"""Process 1 (PLAN.md Section 2/3): the #verify-here channel, the Verify
button, DMs, and role assignment. Also polls the shared DB for results the
Devvit app's verdicts (relayed via a Discord webhook -- see the "Discord
webhook relay" section below) have written and acts on them (assign role /
DM / mod alert).

Since DEVVIT_PIVOT_SPEC.md v4: also owns the Discord-side half of the
verdict hand-off. Reddit's HTTP Fetch Policy never approves personal
domains, only a fixed global allowlist (which includes discord.com) --
so the Devvit app POSTs its verdict to a Discord Incoming Webhook on a
hidden relay channel instead of a self-hosted HTTP endpoint, and this
process reads that channel directly and writes to verify.db itself
(verdict.py) -- no separate webhook_receiver.py process needed.
"""

import asyncio
import json
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

import config
import db
import verdict

intents = discord.Intents.default()
intents.members = True  # needed to look up members and assign roles
intents.message_content = True  # needed to read the relay webhook's message content

bot = commands.Bot(command_prefix="!", intents=intents)

# Set once at startup by ensure_relay_webhook() -- lets on_message() confirm an
# incoming message actually came from *our* webhook, not just any message
# dropped into the relay channel.
_relay_webhook_id: int | None = None

VERIFY_MESSAGE = (
    "**Verify your Reddit account to unlock the rest of the server.**\n\n"
    f"Requirements: a Reddit account that's been active in r/{config.SUBREDDIT_NAME} for a while — "
    "not brand new, not a burner.\n\n"
    "Click the button below. We'll DM you a one-tap Reddit link — tap it, hit **Send**, and you're "
    "verified automatically within about a minute. No codes to type, nothing to post publicly."
)

FAIL_REASON_TEXT = {
    "reddit_account_not_found": "we couldn't find that Reddit account.",
    "code_expired": "the verification code expired before we received it — click Verify again to get a new one.",
    "reddit_account_already_linked": "that Reddit account is already linked to a different Discord account.",
    "no_visible_activity": (
        f"we couldn't find visible activity in r/{config.SUBREDDIT_NAME} on that account. "
        "This can happen if your profile is set to private/curated — we've flagged it for a mod to double check."
    ),
}


def describe_fail_reason(fail_reason: str) -> str:
    if fail_reason in FAIL_REASON_TEXT:
        return FAIL_REASON_TEXT[fail_reason]
    return f"your account didn't meet our activity requirements ({fail_reason})."


# ---------------------------------------------------------------------------
# DB helpers (sqlite3 is sync; run it off the event loop thread)
# ---------------------------------------------------------------------------

def _run_db_sync(fn, args):
    conn = db.connect(config.DB_PATH)
    try:
        result = fn(conn, *args)
        conn.commit()
        return result
    finally:
        conn.close()


async def run_db(fn, *args):
    return await asyncio.to_thread(_run_db_sync, fn, args)


# ---------------------------------------------------------------------------
# Verify button
# ---------------------------------------------------------------------------

class VerifyView(discord.ui.View):
    """Persistent view (survives bot restarts via custom_id)."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Verify Reddit Account",
        style=discord.ButtonStyle.primary,
        custom_id="verify_button",
    )
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_verify_click(interaction)


async def handle_verify_click(interaction: discord.Interaction) -> None:
    discord_user_id = str(interaction.user.id)
    result = await run_db(
        db.create_or_get_pending,
        discord_user_id,
        config.CODE_EXPIRY_MINUTES * 60,
        config.CODE_COOLDOWN_SECONDS,
    )

    if result["state"] == "already_verified":
        await interaction.response.send_message("You're already verified! ✅", ephemeral=True)
        return

    if result["state"] == "reused" and result.get("rate_limited"):
        await interaction.response.send_message(
            "You've already got a code waiting — check your DMs for it.",
            ephemeral=True,
        )
        return

    code = result["code"]

    try:
        await interaction.user.send(
            "**Almost there!** Open the pinned verification post on Reddit, click **Verify**, "
            "and paste in this code:\n\n"
            f"`{code}`\n\n"
            f"{config.DEVVIT_POST_URL}\n\n"
            f"This code expires in {config.CODE_EXPIRY_MINUTES} minutes. "
            "You'll get a DM here as soon as it's checked (usually under a minute)."
        )
        await interaction.response.send_message("Check your DMs! 📬", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(
            "I can't DM you — enable DMs from server members (Privacy Settings) and click Verify again.",
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Manual review request (from a fail DM)
# ---------------------------------------------------------------------------

class ManualReviewView(discord.ui.View):
    """Not registered persistently on purpose: the button has no bound callback,
    so clicks are handled by the raw on_interaction listener below via custom_id
    parsing. That works even after a bot restart, since it needs no view cache.
    """

    def __init__(self, verification_id: int):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Request Manual Review",
                style=discord.ButtonStyle.secondary,
                custom_id=f"request_review:{verification_id}",
            )
        )


@bot.event
async def on_interaction(interaction: discord.Interaction) -> None:
    if interaction.type != discord.InteractionType.component:
        return
    custom_id = interaction.data.get("custom_id", "") if interaction.data else ""
    if not custom_id.startswith("request_review:"):
        return

    verification_id = int(custom_id.split(":", 1)[1])
    row = await run_db(db.get_verification_by_id, verification_id)
    if not row:
        await interaction.response.send_message(
            "Couldn't find that verification record — try clicking Verify again.", ephemeral=True
        )
        return

    mod_channel = bot.get_channel(config.MOD_REVIEW_CHANNEL_ID)
    if mod_channel:
        await mod_channel.send(
            f"🔎 Manual review requested by <@{row['discord_user_id']}>\n"
            f"Reddit username: u/{row['reddit_username'] or 'unknown'}\n"
            f"Fail reason: `{row['fail_reason']}`"
        )

    await interaction.response.send_message("Sent to the mod team — someone will follow up soon.", ephemeral=True)
    if interaction.message:
        try:
            await interaction.message.edit(view=None)
        except discord.HTTPException:
            pass


# ---------------------------------------------------------------------------
# Discord webhook relay (replaces webhook_receiver.py, DEVVIT_PIVOT_SPEC.md v4)
# ---------------------------------------------------------------------------

RELAY_WEBHOOK_NAME = "Verification Relay"  # Discord rejects webhook names containing "discord"


async def ensure_relay_webhook() -> None:
    """Finds or creates the Incoming Webhook the Devvit app POSTs verdicts to.
    Only logs the full URL (which is itself the secret -- anyone with it can
    post to this channel) the one time it's actually created; on later
    restarts it just confirms the webhook still exists, without re-logging
    the token into journalctl history repeatedly.
    """
    global _relay_webhook_id

    channel = bot.get_channel(config.VERIFY_RELAY_CHANNEL_ID)
    if not channel:
        print("[discord_bot] VERIFY_RELAY_CHANNEL_ID not found — skipping relay webhook setup")
        return

    webhooks = await channel.webhooks()
    existing = next((w for w in webhooks if w.name == RELAY_WEBHOOK_NAME), None)
    if existing:
        _relay_webhook_id = existing.id
        print(f"[discord_bot] using existing relay webhook (id={existing.id})")
        return

    created = await channel.create_webhook(name=RELAY_WEBHOOK_NAME)
    _relay_webhook_id = created.id
    print(
        "[discord_bot] created relay webhook — copy this URL into the Devvit app's "
        f"webhookUrl setting NOW, it will not be logged again:\n{created.url}"
    )


@bot.event
async def on_message(message: discord.Message) -> None:
    # Overriding on_message on a commands.Bot normally requires calling
    # bot.process_commands(message) to keep prefix commands working -- not
    # needed here since this bot defines none (everything is buttons/
    # interactions). If that changes, add the call back.
    if message.channel.id != config.VERIFY_RELAY_CHANNEL_ID:
        return
    if message.webhook_id is None or message.webhook_id != _relay_webhook_id:
        # Not from the webhook we created for this exact purpose -- ignore.
        # (Guards against someone posting arbitrary text in the relay channel.)
        return

    try:
        payload = json.loads(message.content)
    except (json.JSONDecodeError, TypeError):
        print(f"[discord_bot] relay message wasn't valid JSON: {message.content!r}")
        return

    try:
        await run_db_verdict(payload)
        print(f"[discord_bot] relay: processed verdict for code {payload.get('code')!r}")
    except verdict.VerdictError as exc:
        print(f"[discord_bot] relay: rejected verdict for code {payload.get('code')!r}: {exc}")


async def run_db_verdict(payload: dict) -> None:
    await asyncio.to_thread(verdict.process_verdict, payload)


# ---------------------------------------------------------------------------
# Background: pick up results the poller has written
# ---------------------------------------------------------------------------

@tasks.loop(seconds=config.POLL_INTERVAL_SECONDS)
async def process_results() -> None:
    rows = await run_db(db.get_unprocessed_results)
    for row in rows:
        try:
            await handle_result(row)
        except Exception as exc:  # noqa: BLE001 - one bad row shouldn't stall the loop
            print(f"[discord_bot] error handling result id={row['id']}: {exc}")
        finally:
            await run_db(db.mark_processed, row["id"])

    # Tracked via its own logged_to_discord flag rather than processed_at, so a
    # crash between role/DM handling and log-posting still gets the log entry
    # posted on the next pass or after a restart (VerificationLogChannel.md).
    log_rows = await run_db(db.get_unlogged_results)
    for row in log_rows:
        try:
            await post_verification_log(row)
        except Exception as exc:  # noqa: BLE001 - retry next loop instead of losing the entry
            print(f"[discord_bot] error posting verification log id={row['id']}: {exc}")
        else:
            await run_db(db.mark_logged, row["id"])


async def handle_result(row) -> None:
    guild = bot.get_guild(config.DISCORD_GUILD_ID)
    member = guild.get_member(int(row["discord_user_id"])) if guild else None

    if row["status"] == "verified":
        if member and config.VERIFIED_ROLE_ID:
            role = guild.get_role(config.VERIFIED_ROLE_ID)
            if role:
                try:
                    await member.add_roles(role, reason="Passed Reddit verification")
                except discord.Forbidden:
                    print(f"[discord_bot] missing permission to assign role to {row['discord_user_id']}")
        try:
            user = member or await bot.fetch_user(int(row["discord_user_id"]))
            await user.send(f"✅ You're verified as u/{row['reddit_username']}! Welcome in.")
        except discord.Forbidden:
            pass

    elif row["status"] == "failed":
        reason_text = describe_fail_reason(row["fail_reason"])
        try:
            user = member or await bot.fetch_user(int(row["discord_user_id"]))
            await user.send(
                f"❌ Verification didn't pass: {reason_text}\n\n"
                "If you think this is a mistake, request a manual review below.",
                view=ManualReviewView(row["id"]),
            )
        except discord.Forbidden:
            pass

        # Soft-fail (Section 11): proactively flag possible hidden-profile cases
        # for a mod even before the user asks.
        if row["fail_reason"] == "no_visible_activity":
            mod_channel = bot.get_channel(config.MOD_REVIEW_CHANNEL_ID)
            if mod_channel:
                await mod_channel.send(
                    f"⚠️ Soft-fail (possible curated/hidden profile): <@{row['discord_user_id']}> "
                    f"verified as u/{row['reddit_username']} but no visible "
                    f"r/{config.SUBREDDIT_NAME} activity was found."
                )


# ---------------------------------------------------------------------------
# Verification log channel (VerificationLogChannel.md)
# ---------------------------------------------------------------------------

LOG_COLOR_PASS = 0x2ECC71
LOG_COLOR_FAIL = 0xE74C3C


def _metric_line(value, threshold, label: str, unit: str = "") -> str:
    """One line of the log embed. Bolds the whole line when this specific
    check is the one that failed, so a mod can tell at a glance which
    threshold(s) tripped without doing the math themselves.
    """
    shown = "N/A" if value is None else value
    failed = value is not None and value < threshold
    line = f"{label}: {shown}{unit} (needs {threshold}{unit}+)"
    return f"**{line}**" if failed else line


async def post_verification_log(row) -> None:
    channel = bot.get_channel(config.VERIFICATION_LOG_CHANNEL_ID)
    if not channel:
        return  # Not configured — logging is optional.

    mention = f"<@{row['discord_user_id']}>"
    reddit_username = row["reddit_username"] or "unknown"

    if row["status"] == "verified":
        verified_at = (
            datetime.fromtimestamp(row["verified_at"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            if row["verified_at"]
            else "N/A"
        )
        embed = discord.Embed(
            title=f"✅ Verified — u/{reddit_username}",
            description=f"{mention}",
            color=LOG_COLOR_PASS,
        )
        embed.add_field(name="Account age", value=f"{row['account_age_days']} days", inline=False)
        embed.add_field(name="Total karma", value=str(row["total_karma"]), inline=False)
        embed.add_field(
            name=f"r/{config.SUBREDDIT_NAME} activity",
            value=f"{row['subreddit_activity_count']} posts/comments, {row['subreddit_karma']} karma",
            inline=False,
        )
        embed.add_field(name="Verified at", value=verified_at, inline=False)
    else:
        lines = [
            _metric_line(row["account_age_days"], config.MIN_ACCOUNT_AGE_DAYS, "Account age", " days"),
            _metric_line(row["total_karma"], config.MIN_TOTAL_KARMA, "Total karma"),
            _metric_line(
                row["subreddit_activity_count"],
                config.MIN_SUBREDDIT_ACTIVITY_COUNT,
                f"r/{config.SUBREDDIT_NAME} activity",
                " posts/comments",
            ),
            _metric_line(row["subreddit_karma"], config.MIN_SUBREDDIT_KARMA, f"r/{config.SUBREDDIT_NAME} karma"),
        ]
        if row["fail_reason"] in ("reddit_account_not_found", "code_expired", "reddit_account_already_linked"):
            # No threshold check ran at all — the metric lines would just be all-N/A noise.
            lines = [f"Reason: `{row['fail_reason']}`"]
        if row["fail_reason"] == "no_visible_activity" and config.MOD_REVIEW_CHANNEL_ID:
            mod_channel = bot.get_channel(config.MOD_REVIEW_CHANNEL_ID)
            if mod_channel:
                lines.append(f"Routed to: #{mod_channel.name}")

        embed = discord.Embed(
            title=f"❌ Failed — u/{reddit_username}",
            description=f"{mention}\n" + "\n".join(lines),
            color=LOG_COLOR_FAIL,
        )

    await channel.send(embed=embed)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

async def ensure_verify_message() -> None:
    channel = bot.get_channel(config.VERIFY_CHANNEL_ID)
    if not channel:
        print("[discord_bot] VERIFY_CHANNEL_ID not found — skipping pinned message setup")
        return

    pins = await channel.pins()
    if any(msg.author == bot.user for msg in pins):
        return  # Already posted on a previous run.

    msg = await channel.send(VERIFY_MESSAGE, view=VerifyView())
    await msg.pin()


@bot.event
async def on_ready() -> None:
    print(f"[discord_bot] logged in as {bot.user}")
    bot.add_view(VerifyView())
    await ensure_verify_message()
    await ensure_relay_webhook()
    if not process_results.is_running():
        process_results.start()


def main() -> None:
    config.validate(require_discord=True)
    db.init_db(config.DB_PATH)
    bot.run(config.DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
