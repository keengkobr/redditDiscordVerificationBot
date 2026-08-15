# Verify for Discord

Checks whether someone's Reddit account is real and active enough to join your Discord server —
then tells your Discord bot whether to let them in.

## What it does

This app adds one pinned post to your subreddit, titled "Verify for Discord." When someone wants
to join your Discord server, your Discord bot sends them here with a one-time code. They paste
that code into the post and click Verify.

Behind the scenes, the app then:

1. Checks how old their Reddit account is.
2. Checks their total karma.
3. Checks how much they've actually posted or commented in *your* subreddit, and how much karma
   they've earned here specifically.
4. Compares all of that against the minimums you've set (see "Settings" below).
5. Sends a pass/fail result back to your Discord server, which handles everything from there —
   giving them a role, sending them a message, and logging the attempt for your mods.

Nobody has to log into anything separately or share a password — since they're already on
Reddit, the app can see who they are the moment they open the post.

## Setting it up on your subreddit

1. Install the app on your subreddit and make it a moderator. It needs moderator access to see
   someone's karma and activity within your subreddit specifically (not just their public Reddit
   history), and this also lets it catch people who've hidden their post/comment history from
   their profile.
2. Installing it automatically creates the pinned "Verify for Discord" post. If you ever need a
   fresh one, moderators can recreate it from the subreddit's mod menu ("Create Verify for
   Discord post").
3. In the app's settings, paste in the Discord webhook URL your Discord bot gives you. This is
   the one piece of setup that connects the two — without it, the app has nowhere to send results.
4. Optionally adjust the minimum requirements (account age, karma, etc.) to whatever fits your
   community. Sensible defaults are already filled in.

That's it — from here, everything runs on its own.

## Settings

Find these under the app's settings on your subreddit (not something you'd edit in code):

- **Discord webhook URL** — where results get sent. Get this from whoever set up your Discord
  bot; keep it private, since anyone with this URL could post fake results.
- **Minimum account age** — how old (in days) a Reddit account has to be. Default: 30 days.
- **Minimum total karma** — combined karma across all of Reddit. Default: 50.
- **Minimum activity in this subreddit** — how many posts/comments they need here specifically.
  Default: 1.
- **Minimum karma in this subreddit** — karma earned here specifically. Default: 50.

These same numbers apply across every subreddit that installs this app — there's no per-server
copy to keep in sync.

## A note on privacy

This app never stores anything. It checks someone's Reddit account at the moment they click
Verify, sends a plain "yes, this checks out" or "no, it doesn't" back to Discord, and forgets
about it. It doesn't keep a list of who's tried, and it never hands your Discord server anyone's
actual Reddit username — only whether the account they claimed matches and whether it passed.
