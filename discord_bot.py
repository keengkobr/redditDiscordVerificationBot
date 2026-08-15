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

# --- Colors + shared formatting (used by the verify-here embed, DMs, and the
# verification log channel alike, so all three read as one consistent style) ---
COLOR_INFO = 0x5865F2  # Discord blurple -- the pre-verification info embed
COLOR_PASS = 0x2ECC71
COLOR_FAIL = 0xE74C3C
COLOR_SOFT_FAIL = 0xF1C40F  # amber -- "no visible activity", routed to mod review, not a hard reject

# Fail reasons with no metrics behind them at all (nothing ran far enough to
# produce numbers) -- shown as plain text instead of an empty requirements list.
FAIL_REASON_TEXT = {
    "reddit_account_not_found": "🔍 We couldn't find that Reddit account.",
    "code_expired": "⏰ The verification code expired before we received it — click Verify again to get a new one.",
    "reddit_account_already_linked": "🔗 That Reddit account is already linked to a different Discord account.",
}

NO_VISIBLE_ACTIVITY_NOTE = (
    "We couldn't find visible activity in this subreddit on your account — this can happen if "
    "your profile is set to private/curated. We've flagged it for a mod to double check."
)


def build_verify_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🔒 Verify your Reddit account",
        description=(
            "Unlock the rest of the server by proving you're an active, real Reddit account — "
            "not brand new, not a burner.\n\n"
            "Click **Verify Reddit Account** below. We'll DM you a one-tap Reddit link — tap it, "
            "hit **Send**, and you're verified automatically within about a minute. No codes to "
            "type, nothing to post publicly."
        ),
        color=COLOR_INFO,
    )
    embed.add_field(
        name="Requirements",
        value=(
            f"📅 Account age: **{config.MIN_ACCOUNT_AGE_DAYS}+ days**\n"
            f"⭐ Total karma: **{config.MIN_TOTAL_KARMA}+**\n"
            f"💬 Activity in r/{config.SUBREDDIT_NAME}: **{config.MIN_SUBREDDIT_ACTIVITY_COUNT}+ post/comment**\n"
            f"🏆 Karma in r/{config.SUBREDDIT_NAME}: **{config.MIN_SUBREDDIT_KARMA}+**"
        ),
        inline=False,
    )
    return embed


def _requirement_lines(row) -> list[str]:
    """One bold, checkmarked line per requirement -- the shared building block
    behind the verify DM, pass/fail DMs, and the log channel embed, so all of
    them read as one consistent style instead of three different formats.
    """

    def line(value, threshold, label: str, unit: str = "") -> str:
        if value is None:
            return f"◽ **{label}** — N/A _(need {threshold}{unit}+)_"
        emoji = "✅" if value >= threshold else "❌"
        return f"{emoji} **{label}** — {value}{unit} _(need {threshold}{unit}+)_"

    return [
        line(row["account_age_days"], config.MIN_ACCOUNT_AGE_DAYS, "Account age", " days"),
        line(row["total_karma"], config.MIN_TOTAL_KARMA, "Total karma"),
        line(
            row["subreddit_activity_count"],
            config.MIN_SUBREDDIT_ACTIVITY_COUNT,
            f"r/{config.SUBREDDIT_NAME} activity",
            " posts/comments",
        ),
        line(row["subreddit_karma"], config.MIN_SUBREDDIT_KARMA, f"r/{config.SUBREDDIT_NAME} karma"),
    ]


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
        embed = discord.Embed(
            title="📋 Almost there!",
            description=(
                "**1.** Copy the code below\n"
                "**2.** Click **Open Verification Post**\n"
                "**3.** Paste the code into the form and hit **Verify** on Reddit"
            ),
            color=COLOR_INFO,
        )
        # A fenced code block, not inline backticks -- renders as its own
        # tappable/selectable block on both desktop and mobile, easier to
        # grab in one motion than text buried inline in a sentence.
        embed.add_field(name="Your code", value=f"```{code}```", inline=False)
        embed.set_footer(
            text=f"Expires in {config.CODE_EXPIRY_MINUTES} minutes — you'll get a DM here once it's checked."
        )
        link_view = discord.ui.View()
        link_view.add_item(
            discord.ui.Button(
                label="Open Verification Post",
                style=discord.ButtonStyle.link,
                url=config.DEVVIT_POST_URL,
                emoji="🔗",
            )
        )
        await interaction.user.send(embed=embed, view=link_view)
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
        embed = discord.Embed(
            title="🔎 Manual Review Requested",
            description=f"<@{row['discord_user_id']}> — u/{row['reddit_username'] or 'unknown'}",
            color=COLOR_SOFT_FAIL,
        )
        if row["fail_reason"] in FAIL_REASON_TEXT:
            embed.add_field(name="Reason", value=FAIL_REASON_TEXT[row["fail_reason"]], inline=False)
        else:
            embed.add_field(name="Requirements", value="\n".join(_requirement_lines(row)), inline=False)
        await mod_channel.send(embed=embed)

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
    Never logs the full URL -- it's a live credential, and journalctl history
    persists (backups, log shipping, etc. could see it even though reading it
    directly needs sudo). Logs the webhook ID only; run get_relay_webhook_url.py
    on demand to actually retrieve the URL when you need it.
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
        f"[discord_bot] created relay webhook (id={created.id}) — run "
        "get_relay_webhook_url.py to retrieve its URL for the Devvit app's webhookUrl setting"
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
            embed = discord.Embed(
                title="✅ You're verified!",
                description=f"Welcome in — verified as **u/{row['reddit_username']}**.",
                color=COLOR_PASS,
            )
            embed.add_field(name="Requirements", value="\n".join(_requirement_lines(row)), inline=False)
            await user.send(embed=embed)
        except discord.Forbidden:
            pass

    elif row["status"] == "failed":
        try:
            user = member or await bot.fetch_user(int(row["discord_user_id"]))
            embed = discord.Embed(title="❌ Verification didn't pass", color=COLOR_FAIL)

            if row["fail_reason"] in FAIL_REASON_TEXT:
                embed.description = FAIL_REASON_TEXT[row["fail_reason"]]
            else:
                embed.add_field(name="Requirements", value="\n".join(_requirement_lines(row)), inline=False)
                if row["fail_reason"] == "no_visible_activity":
                    embed.color = COLOR_SOFT_FAIL
                    embed.description = NO_VISIBLE_ACTIVITY_NOTE

            embed.set_footer(text="Think this is a mistake? Request a manual review below.")
            await user.send(embed=embed, view=ManualReviewView(row["id"]))
        except discord.Forbidden:
            pass

        # Soft-fail (Section 11): proactively flag possible hidden-profile cases
        # for a mod even before the user asks.
        if row["fail_reason"] == "no_visible_activity":
            mod_channel = bot.get_channel(config.MOD_REVIEW_CHANNEL_ID)
            if mod_channel:
                embed = discord.Embed(
                    title="⚠️ Possible Hidden Profile",
                    description=(
                        f"<@{row['discord_user_id']}> verified as **u/{row['reddit_username']}** "
                        f"but no visible r/{config.SUBREDDIT_NAME} activity was found."
                    ),
                    color=COLOR_SOFT_FAIL,
                )
                embed.add_field(name="Requirements", value="\n".join(_requirement_lines(row)), inline=False)
                await mod_channel.send(embed=embed)


# ---------------------------------------------------------------------------
# Verification log channel (VerificationLogChannel.md)
# ---------------------------------------------------------------------------


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
            description=mention,
            color=COLOR_PASS,
        )
        embed.add_field(name="Requirements", value="\n".join(_requirement_lines(row)), inline=False)
        embed.set_footer(text=f"Verified at {verified_at}")
    else:
        color = COLOR_FAIL
        if row["fail_reason"] in FAIL_REASON_TEXT:
            description = f"{mention}\n{FAIL_REASON_TEXT[row['fail_reason']]}"
        else:
            lines = _requirement_lines(row)
            if row["fail_reason"] == "no_visible_activity":
                color = COLOR_SOFT_FAIL
                lines.append("")
                lines.append("🔎 Possible curated/hidden profile — routed to mod review.")
            description = f"{mention}\n" + "\n".join(lines)

        embed = discord.Embed(
            title=f"❌ Failed — u/{reddit_username}",
            description=description,
            color=color,
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

    msg = await channel.send(embed=build_verify_embed(), view=VerifyView())
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
