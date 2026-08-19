"""Process 1 (PLAN.md Section 2/3): the #verify-here channel, the Verify
button, DMs, and role assignment. Also owns the Discord-side half of the
verdict hand-off (see "Discord webhook relay" below).

Since DEVVIT_PIVOT_SPEC.md v5: fully stateless. There is no database and no
verify.db. Every piece of in-flight state lives in the plain dicts below,
in this process's memory, for the few minutes a verification is actually in
flight -- see the module docstring in Claude/DEVVIT_PIVOT_SPEC.md for why
(Devvit Rules' SOC2/pen-test requirement for services that persist a link
between a Reddit account and an external account). Durable state lives
where it already belonged: Discord's own role membership ("verified"),
Discord's own channel history (the verification log), and Devvit's own
Redis/KV store (the cross-Discord-account anti-duplicate check).
"""

import base64
import json
import re
import secrets
import string
import time
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

import config
import verdict

intents = discord.Intents.default()
intents.members = True  # needed to look up members and assign roles
intents.message_content = True  # needed to read DMs and the relay webhook's messages

bot = commands.Bot(command_prefix="!", intents=intents)

# Set once at startup by ensure_relay_webhook() -- lets on_message() confirm an
# incoming message actually came from *our* webhook, not just any message
# dropped into the relay channel.
_relay_webhook_id: int | None = None

# ---------------------------------------------------------------------------
# In-memory verification state (DEVVIT_PIVOT_SPEC.md v5 -- no database)
# ---------------------------------------------------------------------------
# SESSIONS is keyed by discord_user_id (str). One entry per in-flight
# verification attempt:
#   stage: "awaiting_username" -- bot is waiting for the user's DM reply
#          "awaiting_verdict"  -- code issued, waiting on the relay webhook
#   claimed_username: the Reddit username the user typed into their DM --
#       Discord-side, self-reported data (see the compliance note in
#       DEVVIT_PIVOT_SPEC.md's "Log-scope compliance boundary" section).
#   short_id: the code half actually looked up on webhook arrival (None
#       while awaiting_username).
#   mismatch_count: how many times Devvit has told us this claim didn't
#       match the real account.
#   updated_at: epoch seconds, used by sweep_stale_sessions() below.
SESSIONS: dict[str, dict] = {}

# Reverse index for O(1) webhook lookups: short_id -> discord_user_id.
# Only holds entries for sessions currently in the "awaiting_verdict" stage.
CODE_TO_USER: dict[str, str] = {}

USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,20}$")

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
    "reddit_account_already_linked": "🔗 That Reddit account is already linked to a different Discord account.",
    "username_mismatch": "🙅 Couldn't confirm the claimed Reddit account matched after 3 attempts.",
    "reddit_identity_unavailable": (
        "🚫 Reddit wouldn't confirm your identity — this can happen if the account is "
        "suspended/banned, or from a temporary Reddit issue."
    ),
}

NO_VISIBLE_ACTIVITY_NOTE = (
    "We didn't find any posts or comments from you in this subreddit — we've flagged it for a "
    "mod to double check."
)


def build_verify_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🔒 Verify your Reddit account",
        description=(
            "Unlock the rest of the server by proving you're an active, real Reddit account — "
            "not brand new, not a burner.\n\n"
            "Click **Verify Reddit Account** below. We'll DM you to ask which Reddit account is "
            "yours, then send you a one-tap link — tap it, hit **Verify** on Reddit, and you're "
            "verified automatically within about a minute."
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


def _requirement_lines(row: dict) -> list[str]:
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


async def mention_with_name(discord_user_id) -> str:
    """<@id> mention plus a plaintext username fallback, e.g. "<@123> (name)".
    Discord clients sometimes render a mention as the raw numeric ID instead
    of resolving it (client-side cache lag) -- a mod reading the log/mod
    channel then has no way to tell who that actually is. The plaintext name
    means they can still identify the user even when that happens.
    """
    uid = int(discord_user_id)
    user = bot.get_user(uid)  # cache-only, no API call
    if user is None:
        try:
            user = await bot.fetch_user(uid)
        except discord.HTTPException:
            user = None
    return f"<@{uid}> ({user.name})" if user else f"<@{uid}>"


# ---------------------------------------------------------------------------
# Username claim normalization + code<->claim encoding
# ---------------------------------------------------------------------------

def normalize_username(raw: str) -> str | None:
    """Strips an optional u/ or /u/ prefix and whitespace, lowercases, and
    validates against Reddit's username character rules. Returns None for
    anything that doesn't look like a plausible username.
    """
    candidate = raw.strip()
    for prefix in ("/u/", "u/"):
        if candidate.lower().startswith(prefix):
            candidate = candidate[len(prefix):]
            break
    if not USERNAME_RE.match(candidate):
        return None
    return candidate.lower()


def generate_short_id(length: int = 6) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def build_code(short_id: str, claimed_username: str, discord_user_id: str) -> str:
    """Packs the claim into the code the user pastes into the Devvit form --
    this is the *only* channel that exists for Discord-side data to reach
    the Devvit app at all (it can't fetch our VPS, and we can't call into
    it -- see DEVVIT_PIVOT_SPEC.md v5's "inbound-callback problem"). Not
    encryption, just packing -- Devvit unpacks it locally to compare against
    the real identity it resolves, and never echoes the claim back to us.
    """
    blob = json.dumps({"u": claimed_username, "d": discord_user_id}, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(blob).decode().rstrip("=")
    return f"{short_id}.{encoded}"


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
    member = interaction.user if isinstance(interaction.user, discord.Member) else None

    if member and config.VERIFIED_ROLE_ID and any(r.id == config.VERIFIED_ROLE_ID for r in member.roles):
        await interaction.response.send_message("You're already verified! ✅", ephemeral=True)
        return

    now = time.time()
    session = SESSIONS.get(discord_user_id)
    if session and now - session["updated_at"] < config.CODE_EXPIRY_MINUTES * 60:
        if session["stage"] == "awaiting_username":
            await interaction.response.send_message(
                "Check your DMs — reply there with your Reddit username to continue.",
                ephemeral=True,
            )
            return
        if now - session["updated_at"] < config.CODE_COOLDOWN_SECONDS:
            await interaction.response.send_message(
                "You've already got a code waiting — check your DMs for it.",
                ephemeral=True,
            )
            return

        # Past cooldown but the code is still valid (stage == "awaiting_verdict").
        # Re-send the SAME code instead of falling through to start a brand new
        # session -- that used to silently abandon the in-flight one (and its
        # short_id), so a verdict for the old code would arrive to find no
        # matching session at all and get discarded, with no result DM ever
        # sent. Re-sending costs nothing and can't orphan anything.
        session["updated_at"] = now
        try:
            await send_code_dm(interaction.user, discord_user_id, session)
            await interaction.response.send_message("Sent you the code again — check your DMs! 📬", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                "I can't DM you — enable DMs from server members (Privacy Settings) and click Verify again.",
                ephemeral=True,
            )
        return

    SESSIONS[discord_user_id] = {
        "stage": "awaiting_username",
        "claimed_username": None,
        "short_id": None,
        "mismatch_count": 0,
        "updated_at": now,
    }

    try:
        embed = discord.Embed(
            title="👋 Which Reddit account is yours?",
            description=(
                "Reply to this DM with your Reddit username (e.g. `u/yourname` or just "
                "`yourname`) and we'll send you a one-tap link to finish verifying."
            ),
            color=COLOR_INFO,
        )
        await interaction.user.send(embed=embed)
        await interaction.response.send_message("Check your DMs! 📬", ephemeral=True)
    except discord.Forbidden:
        del SESSIONS[discord_user_id]
        await interaction.response.send_message(
            "I can't DM you — enable DMs from server members (Privacy Settings) and click Verify again.",
            ephemeral=True,
        )


async def handle_username_reply(message: discord.Message, session: dict) -> None:
    discord_user_id = str(message.author.id)
    normalized = normalize_username(message.content)
    if not normalized:
        await message.channel.send(
            "That doesn't look like a Reddit username — usually 3-20 letters/numbers/underscores/hyphens, "
            "optionally starting with `u/`. Try again?"
        )
        return

    short_id = generate_short_id()
    session.update(
        stage="awaiting_verdict",
        claimed_username=normalized,
        short_id=short_id,
        updated_at=time.time(),
    )
    CODE_TO_USER[short_id] = discord_user_id

    await send_code_dm(message.channel, discord_user_id, session)


async def send_code_dm(destination, discord_user_id: str, session: dict) -> None:
    """Builds and sends the code+link DM. Shared by handle_username_reply
    (issuing a fresh code) and handle_verify_click (re-sending an existing,
    still-valid one -- see the fallthrough note there for why that matters).
    """
    code = build_code(session["short_id"], session["claimed_username"], discord_user_id)
    embed = discord.Embed(
        title="📋 Almost there!",
        description=(
            "**1.** Tap and hold the code in the message right below this one, then **Copy**\n"
            "**2.** Tap **Open Verification Post**\n"
            "**3.** Paste the code into the form and hit **Verify** on Reddit"
        ),
        color=COLOR_INFO,
    )
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
    await destination.send(embed=embed, view=link_view)
    # A short caption as its OWN message, separate from the code -- gives
    # the code delivery some visual polish without touching the code
    # message's own text (any styling there, even an emoji prefix, would
    # get copied right along with the code -- see the note below).
    await destination.send("👇 Your code:")
    # The code is its own plain message, not an embed field -- on mobile,
    # long-pressing a message copies the message's whole rendered text, so
    # a code sitting inside an embed field would copy the entire embed
    # (title + description + field) instead of just the code. A standalone
    # message means "copy text" grabs exactly and only the code -- and
    # deliberately no fenced-code-block backticks here either, since those
    # are literal characters in the raw message text and would get copied
    # right along with the code otherwise. No need for that styling anyway
    # when the code is already the message's only content.
    await destination.send(code)


# ---------------------------------------------------------------------------
# Manual review request (from a fail DM)
# ---------------------------------------------------------------------------

class ManualReviewModal(discord.ui.Modal, title="Request Manual Review"):
    """Shown when the user clicks Request Manual Review -- collects a short
    note on why they think the result was wrong, since a mod reading the
    flag with no context has to guess. original_embed/original_message are
    captured at button-click time (before the modal opens) since a modal
    submission is its own separate interaction and doesn't carry a reference
    back to the message that spawned it.
    """

    note = discord.ui.TextInput(
        label="Why do you think this is a mistake?",
        style=discord.TextStyle.paragraph,
        placeholder="e.g. My profile is set to private, or I've been more active recently.",
        required=True,
        max_length=500,
    )

    def __init__(self, original_embed: discord.Embed | None, original_message: discord.Message | None):
        super().__init__()
        self._original_embed = original_embed
        self._original_message = original_message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Discord requires a response within 3 seconds or shows "didn't
        # respond in time" -- defer immediately and do the real work via a
        # followup so a slow mod-channel send can't blow that window.
        await interaction.response.defer(ephemeral=True)

        mod_channel = bot.get_channel(config.MOD_REVIEW_CHANNEL_ID)
        posted_to_mods = False
        if mod_channel:
            embed = discord.Embed(
                title="🔎 Manual Review Requested",
                description=await mention_with_name(interaction.user.id),
                color=COLOR_SOFT_FAIL,
            )
            if self._original_embed:
                if self._original_embed.description:
                    embed.description += f"\n{self._original_embed.description}"
                for field in self._original_embed.fields:
                    embed.add_field(name=field.name, value=field.value, inline=field.inline)
            if self.note.value:
                embed.add_field(name="User's note", value=self.note.value, inline=False)

            try:
                # A private thread -- not a public one -- so the requesting
                # user (who generally can't see the mod-review channel at
                # all) can still be added and actually participate. Needs
                # the bot's role to have Create Private Threads granted in
                # this channel; Manage Threads too, so mods automatically see
                # it without needing to be individually added.
                thread = await mod_channel.create_thread(
                    name=f"Review — {interaction.user.display_name}",
                    type=discord.ChannelType.private_thread,
                    reason="Manual review requested",
                )
                await thread.add_user(interaction.user)
                ping = " ".join(f"<@&{role_id}>" for role_id in config.MOD_PING_ROLE_IDS) or None
                await thread.send(content=ping, embed=embed, view=CloseThreadView())
                posted_to_mods = True
            except discord.Forbidden:
                # No thread permission -- fall back to the old flat-embed
                # post so the request isn't silently lost, same defensive
                # pattern used everywhere else in this file.
                try:
                    await mod_channel.send(embed=embed)
                    posted_to_mods = True
                except discord.Forbidden:
                    print(f"[discord_bot] missing permission to create thread or post in MOD_REVIEW_CHANNEL_ID (manual review, user={interaction.user.id})")

        await interaction.followup.send(
            "Sent to the mod team — someone will follow up soon."
            if posted_to_mods
            else "Couldn't reach the mod-review channel — let a mod know directly for now.",
            ephemeral=True,
        )
        if self._original_message:
            try:
                await self._original_message.edit(view=None)
            except discord.HTTPException:
                pass


class CloseThreadView(discord.ui.View):
    """Persistent -- posted inside a manual-review thread. Handled by the raw
    on_interaction listener below (same pattern as request_review/verify_button),
    not a bound callback, since it needs to survive restarts without being
    re-attached to a specific message.
    """

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Close Thread",
                style=discord.ButtonStyle.success,
                custom_id="close_review_thread",
            )
        )


class ManualReviewView(discord.ui.View):
    """Persistent (survives restarts) -- the button's custom_id carries no
    per-message data on purpose. There's nothing left to look up by the time
    it's clicked (v5 has no DB, and the in-memory session is long gone by
    then), so the handler below just re-reads whatever this exact message's
    own embed already shows -- Discord's own message history is already the
    durable copy of that data, nothing needs to be re-fetched from anywhere.
    """

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Request Manual Review",
                style=discord.ButtonStyle.secondary,
                custom_id="request_review",
            )
        )


@bot.event
async def on_interaction(interaction: discord.Interaction) -> None:
    if interaction.type != discord.InteractionType.component:
        return
    custom_id = interaction.data.get("custom_id", "") if interaction.data else ""

    if custom_id == "request_review":
        # Opening a modal must be the direct, immediate response to the click
        # -- no defer() first. The modal's own on_submit handles the rest of
        # the flow (including its own 3-second-response timing) once submitted.
        original_embed = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else None
        await interaction.response.send_modal(ManualReviewModal(original_embed, interaction.message))
        return

    if custom_id == "close_review_thread":
        channel = interaction.channel
        # Live permission check against the parent channel, not a stored mod
        # role list -- the requesting user is a thread member too by now, and
        # this is what actually stops them from closing their own thread.
        perms = (
            channel.parent.permissions_for(interaction.user)
            if isinstance(channel, discord.Thread) and channel.parent
            else None
        )
        if not perms or not perms.manage_threads:
            await interaction.response.send_message("Only mods can close this.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await channel.send(f"✅ Resolved by {await mention_with_name(interaction.user.id)}")
            await channel.edit(archived=True, locked=True)
            await interaction.followup.send("Closed.", ephemeral=True)
        except discord.HTTPException:
            await interaction.followup.send(
                "Couldn't close the thread — try again or close it manually.", ephemeral=True
            )
        return


# ---------------------------------------------------------------------------
# Unlink
# ---------------------------------------------------------------------------

@bot.tree.command(name="unlink", description="Remove your Verified role so you can re-verify.")
async def unlink(interaction: discord.Interaction) -> None:
    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    if not member or not config.VERIFIED_ROLE_ID:
        await interaction.response.send_message("Nothing to unlink.", ephemeral=True)
        return

    role = interaction.guild.get_role(config.VERIFIED_ROLE_ID) if interaction.guild else None
    if not role or role not in member.roles:
        await interaction.response.send_message("You're not currently verified.", ephemeral=True)
        return

    try:
        await member.remove_roles(role, reason="Self-service unlink")
        if config.UNVERIFIED_ROLE_ID:
            unverified_role = interaction.guild.get_role(config.UNVERIFIED_ROLE_ID)
            if unverified_role:
                await member.add_roles(unverified_role, reason="Self-service unlink")
    except discord.Forbidden:
        await interaction.response.send_message(
            "I don't have permission to change your roles — let a mod know.", ephemeral=True
        )
        return

    # Nothing to delete on our side -- there's no database. Devvit's own
    # anti-duplicate KV entry expires on its own TTL (30 days); re-verifying
    # under this same Discord account is not blocked by that at all, only
    # moving the Reddit account to a *different* Discord account is (see
    # DEVVIT_PIVOT_SPEC.md v5's unlink section).
    await interaction.response.send_message(
        "Unlinked — click **Verify Reddit Account** any time to link a Reddit account again.",
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# Discord webhook relay (replaces webhook_receiver.py, DEVVIT_PIVOT_SPEC.md v4/v5)
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
        "get_relay_webhook_url.py to retrieve its URL for the Devvit app's discordWebhookUrl setting"
    )


@bot.event
async def on_message(message: discord.Message) -> None:
    # Overriding on_message on a commands.Bot normally requires calling
    # bot.process_commands(message) to keep prefix commands working -- not
    # needed here since this bot defines none (everything is buttons/slash
    # commands/interactions). If that changes, add the call back.
    if message.author.id == bot.user.id:
        return

    if isinstance(message.channel, discord.DMChannel):
        session = SESSIONS.get(str(message.author.id))
        if session and session["stage"] == "awaiting_username":
            await handle_username_reply(message, session)
        return

    if message.channel.id != config.VERIFY_RELAY_CHANNEL_ID:
        return
    if message.webhook_id is None or message.webhook_id != _relay_webhook_id:
        # Not from the webhook we created for this exact purpose -- ignore.
        # (Guards against someone posting arbitrary text in the relay channel.)
        # Logged (IDs only, never message content) since this exact silence
        # is otherwise indistinguishable from the webhook message never
        # arriving at all -- e.g. Devvit's discordWebhookUrl setting pointing
        # at a different webhook than the one this process is actually using.
        if message.webhook_id is not None:
            print(f"[discord_bot] relay: ignoring message from webhook_id={message.webhook_id} (expected {_relay_webhook_id}) -- check Devvit's discordWebhookUrl setting")
        return

    try:
        payload = json.loads(message.content)
    except (json.JSONDecodeError, TypeError):
        # Never log message.content here -- it's untrusted relay traffic and
        # could echo back a claimed username on a malformed payload. A bare
        # tag is enough to notice this is happening; body content stays out
        # of any persistent log (DEVVIT_PIVOT_SPEC.md v5's logging note).
        print("[discord_bot] relay message wasn't valid JSON")
        return

    short_id = str(payload.get("code", ""))
    try:
        verdict_payload = verdict.parse_verdict(payload)
    except verdict.VerdictError as exc:
        print(f"[discord_bot] relay: rejected malformed verdict for code {short_id!r}: {exc}")
        return

    await handle_verdict(short_id, verdict_payload)


async def handle_verdict(short_id: str, payload: "verdict.VerdictPayload") -> None:
    discord_user_id = CODE_TO_USER.get(short_id)
    session = SESSIONS.get(discord_user_id) if discord_user_id else None
    if not discord_user_id or not session or session.get("short_id") != short_id:
        # Expired, already resolved, or the bot restarted mid-flow -- this is
        # the natural idempotency guard in a stateless design. If someone
        # reports "I verified and got nothing," this line plus the short_id
        # (no username, no Discord ID) is the diagnostic trail; the fallback
        # is a mod manually granting the role, a plain Discord action.
        print(f"[discord_bot] relay: no session for code {short_id!r} — discarding")
        return

    CODE_TO_USER.pop(short_id, None)

    if not payload.username_ok:
        session["mismatch_count"] += 1
        session["short_id"] = None
        if session["mismatch_count"] >= 3:
            row = {
                "discord_user_id": discord_user_id,
                "reddit_username": session["claimed_username"],
                "status": "failed",
                "fail_reason": "username_mismatch",
                "account_age_days": None,
                "total_karma": None,
                "subreddit_activity_count": None,
                "subreddit_karma": None,
                "verified_at": None,
            }
            SESSIONS.pop(discord_user_id, None)
            await flag_username_mismatch_for_mods(row)
            try:
                await post_verification_log(row)
            except Exception as exc:  # noqa: BLE001 - don't let a log-post failure block the user's DM below
                print(f"[discord_bot] error posting verification log for username-mismatch exhaustion (user={discord_user_id}): {exc}")
            try:
                user = await bot.fetch_user(int(discord_user_id))
                embed = discord.Embed(
                    title="🔎 Sent to the mod team",
                    description=(
                        "We couldn't confirm that Reddit account matches after a few tries — "
                        "we've flagged this for a mod to take a look."
                    ),
                    color=COLOR_SOFT_FAIL,
                )
                await user.send(embed=embed)
            except discord.Forbidden:
                pass
        else:
            session["stage"] = "awaiting_username"
            session["updated_at"] = time.time()
            try:
                user = await bot.fetch_user(int(discord_user_id))
                embed = discord.Embed(
                    title="❌ That didn't match",
                    description=(
                        "That didn't match the Reddit account you told us about "
                        f"**({session['mismatch_count']}/3)**. Reply here with your Reddit "
                        "username to try again."
                    ),
                    color=COLOR_FAIL,
                )
                await user.send(embed=embed)
            except discord.Forbidden:
                pass
        return

    row = {
        "discord_user_id": discord_user_id,
        "reddit_username": session["claimed_username"],
        "status": payload.status,
        "fail_reason": payload.fail_reason,
        "account_age_days": payload.account_age_days,
        "total_karma": payload.total_karma,
        "subreddit_activity_count": payload.subreddit_activity_count,
        "subreddit_karma": payload.subreddit_karma,
        "verified_at": time.time() if payload.status == "verified" else None,
    }
    SESSIONS.pop(discord_user_id, None)

    try:
        await handle_result(row)
    except Exception as exc:  # noqa: BLE001 - one bad verdict shouldn't crash the listener
        print(f"[discord_bot] error handling verdict for code {short_id!r}: {exc}")
        return

    try:
        await post_verification_log(row)
    except Exception as exc:  # noqa: BLE001 - log-posting failures shouldn't lose the role/DM work above
        print(f"[discord_bot] error posting verification log for code {short_id!r}: {exc}")


async def flag_username_mismatch_for_mods(row: dict) -> None:
    """Proactive auto-flag after 3 failed username-match attempts -- same
    pattern as the no_visible_activity soft-fail flag in handle_result(),
    just for a different failure mode. row["fail_reason"] == "username_mismatch"
    always here, so the FAIL_REASON_TEXT lookup is really just for the shared
    copy, not branching.
    """
    mod_channel = bot.get_channel(config.MOD_REVIEW_CHANNEL_ID)
    if not mod_channel:
        return
    who = await mention_with_name(row["discord_user_id"])
    embed = discord.Embed(
        title="🔎 Manual Review Requested — Username Mismatch",
        description=f"{who} — last claimed u/{row['reddit_username']}",
        color=COLOR_SOFT_FAIL,
    )
    embed.add_field(name="Reason", value=FAIL_REASON_TEXT[row["fail_reason"]], inline=False)
    try:
        await mod_channel.send(embed=embed)
    except discord.Forbidden:
        print(f"[discord_bot] missing permission to post in MOD_REVIEW_CHANNEL_ID (username mismatch, user={row['discord_user_id']})")


# ---------------------------------------------------------------------------
# Role assignment, DMs, mod alerts
# ---------------------------------------------------------------------------

async def handle_result(row: dict) -> None:
    guild = bot.get_guild(config.DISCORD_GUILD_ID)
    member = guild.get_member(int(row["discord_user_id"])) if guild else None

    if row["status"] == "verified":
        if member:
            try:
                if config.VERIFIED_ROLE_ID:
                    role = guild.get_role(config.VERIFIED_ROLE_ID)
                    if role:
                        await member.add_roles(role, reason="Passed Reddit verification")
                if config.UNVERIFIED_ROLE_ID:
                    unverified_role = guild.get_role(config.UNVERIFIED_ROLE_ID)
                    if unverified_role and unverified_role in member.roles:
                        await member.remove_roles(unverified_role, reason="Passed Reddit verification")
            except discord.Forbidden:
                print(f"[discord_bot] missing permission to update roles for {row['discord_user_id']}")
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
                embed.description = (
                    f"{FAIL_REASON_TEXT[row['fail_reason']]}\n"
                    f"Attempted: u/{row['reddit_username']}"
                )
            else:
                embed.add_field(name="Requirements", value="\n".join(_requirement_lines(row)), inline=False)
                if row["fail_reason"] == "no_visible_activity":
                    embed.color = COLOR_SOFT_FAIL
                    embed.description = NO_VISIBLE_ACTIVITY_NOTE

            embed.set_footer(text="Think this is a mistake? Request a manual review below.")
            await user.send(embed=embed, view=ManualReviewView())
        except discord.Forbidden:
            pass

        # Soft-fail (Section 11): proactively flag no-activity cases for a mod
        # even before the user asks. NOT necessarily a hidden/curated profile
        # -- the app runs with moderator scope and already sees through those
        # (28-day visibility window), so this genuinely means no detected
        # activity, full stop. Could still be a hidden profile *older* than
        # that window, or simply someone new to the subreddit -- a mod needs
        # to look either way, hence still routed here rather than hard-failed.
        if row["fail_reason"] == "no_visible_activity":
            mod_channel = bot.get_channel(config.MOD_REVIEW_CHANNEL_ID)
            if mod_channel:
                who = await mention_with_name(row["discord_user_id"])
                embed = discord.Embed(
                    title="⚠️ No Subreddit Activity Found",
                    description=(
                        f"{who} verified as **u/{row['reddit_username']}** "
                        f"but no r/{config.SUBREDDIT_NAME} activity was found."
                    ),
                    color=COLOR_SOFT_FAIL,
                )
                embed.add_field(name="Requirements", value="\n".join(_requirement_lines(row)), inline=False)
                try:
                    await mod_channel.send(embed=embed)
                except discord.Forbidden:
                    # Not fatal here (the user's own fail DM with a manual-review
                    # button already went out above) -- but worth a clear log
                    # line rather than a bare exception bubbling up to the
                    # relay handler's generic catch-all.
                    print(f"[discord_bot] missing permission to post soft-fail flag in MOD_REVIEW_CHANNEL_ID for {row['discord_user_id']}")


# ---------------------------------------------------------------------------
# Verification log channel (VerificationLogChannel.md)
# ---------------------------------------------------------------------------

async def post_verification_log(row: dict) -> None:
    channel = bot.get_channel(config.VERIFICATION_LOG_CHANNEL_ID)
    if not channel:
        return  # Not configured — logging is optional.

    mention = await mention_with_name(row["discord_user_id"])
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
                lines.append("🔎 No subreddit activity found — routed to mod review.")
            description = f"{mention}\n" + "\n".join(lines)

        embed = discord.Embed(
            title=f"❌ Failed — u/{reddit_username}",
            description=description,
            color=color,
        )

    await channel.send(embed=embed)


# ---------------------------------------------------------------------------
# Session housekeeping
# ---------------------------------------------------------------------------

@tasks.loop(seconds=config.SESSION_SWEEP_INTERVAL_SECONDS)
async def sweep_stale_sessions() -> None:
    cutoff = time.time() - config.CODE_EXPIRY_MINUTES * 60
    stale = [uid for uid, session in SESSIONS.items() if session["updated_at"] < cutoff]
    for uid in stale:
        session = SESSIONS.pop(uid, None)
        if session and session.get("short_id"):
            CODE_TO_USER.pop(session["short_id"], None)


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


_views_registered = False


@bot.event
async def on_ready() -> None:
    print(f"[discord_bot] logged in as {bot.user}")
    global _views_registered
    if not _views_registered:
        # on_ready can fire more than once per process (e.g. after a gateway
        # reconnect) -- re-registering these every time adds a duplicate
        # listener for the same custom_id, which makes a single button click
        # dispatch twice (the second dispatch then crashes trying to ack an
        # already-acknowledged interaction). Register exactly once.
        bot.add_view(VerifyView())
        bot.add_view(ManualReviewView())
        bot.add_view(CloseThreadView())
        _views_registered = True
    # Slash commands persist server-side once registered -- they don't need
    # re-syncing on every restart, and Discord's command-sync endpoint has a
    # much stricter rate limit than normal API calls (hit it for real during
    # a run of frequent restarts, which then blocked ensure_verify_message()
    # below since sync() used to run first and retry-with-backoff on 429).
    # Opt-in via env var, and run it last so a slow/rate-limited sync can
    # never block the startup steps that actually matter every time.
    await ensure_verify_message()
    await ensure_relay_webhook()
    if config.SYNC_SLASH_COMMANDS:
        await bot.tree.sync()
        print("[discord_bot] synced slash commands")
    if not sweep_stale_sessions.is_running():
        sweep_stale_sessions.start()


def main() -> None:
    config.validate(require_discord=True)
    bot.run(config.DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
