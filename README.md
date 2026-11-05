# Blastats

Match tracking and stats for [Blaston](https://www.resolutiongames.com/blaston), a VR dueling game. Blastats records ranked matches as they happen and turns them into leaderboards, player profiles, round by round replays and weapon meta stats.

The same client also runs a 24/7 YouTube livestream of the matches, and every recorded match on the site links to when it happened on the stream.

Live at [blastats.com](https://blastats.com). Stream at [YouTube](https://www.youtube.com/@xyfume/streams).

![Blastats opening page](docs/screenshot.png)

## How it works

```mermaid
flowchart LR
    game["Blaston Spectator<br/>+ MelonLoader mod"] -->|matches.json| poller["BlastonPoller.py"]
    game -->|24/7 stream| yt["YouTube"]
    site -.->|watch links| yt
    yt -->|chat commands| chat["chat_camera.py"]
    chat -->|camera_command.txt| game
    playfab["PlayFab +<br/>match server"] --> poller
    poller -->|players, ladders,<br/>matches| db[("SQLite<br/>data/blastats.db")]
    poller --> rectify["rectify.py"]
    rectify -->|corrections| db
    db --> api["api.py<br/>(FastAPI)"]
    api --> site["static/<br/>(vanilla JS)"]
```

There are two sources of match data, and the poller pairs them as they arrive.

- The mod runs inside the Blaston Spectator client, which is captured and streamed to YouTube around the clock. It spectates competitive queue, and records everything the spectator view can see: loadouts, health at the end of each round, damage taken per weapon, a per hit damage timeline, grabs, ping and character portraits.
- The poller talks to the game's backend directly. It tracks the season leaderboards and player stats, and picks up every ranked match announcement, not just the ones the mod is spectating. It works out the winner by watching each player's lifetime rounds won counter change.
- Whichever copy of a game arrives second is paired with the first (same players, within 3 minutes), so each game is stored once. The poller also picks up new spectated matches from the mod's `matches.json`, and keeps every ranked announcement at any rank for the activity heatmap, even when the result isn't tracked.
- The API reads from the database, with result corrections applied by SQL views, and serves the site.

## Database

Everything is stored in one SQLite database, `data/blastats.db`, built from [`schema.sql`](schema.sql). Each game is a row in `matches`, with its players, loadouts, rounds, per round stats and damage events in their own tables. Corrections sit in a separate table and are applied by views, so recorded results are never edited. Schema changes ship as numbered files in [`migrations/`](migrations) and are applied automatically the next time the poller or API starts.

![Database diagram](docs/blastats_erd.drawio.svg)

This replaced a set of JSON files that the API re-read and re-merged whenever a match was added. With about 19,000 matches, the first profile load after a new match went from around 6 seconds to about 50 ms, or 0.6 seconds when the match was spectated and weapon stats need rebuilding.

## Components

| Path | What it is |
| --- | --- |
| `mod/` | C# MelonLoader mod using Harmony patches on the IL2CPP game. Records matches, directs the stream camera, keeps the spectate queue alive and restarts the client if it gets stuck. |
| `BlastonPoller.py` | Polls PlayFab for leaderboards and stats, the match server for new matches, and the mod's output for spectated matches. Saves everything to the database and runs the rectifier after each stats cycle. |
| `rectify.py` | Finds matches where the recorded winner is probably wrong, using ELO movement around the match. |
| `api.py` | FastAPI backend reading from the database. Streaks, head to head, and weapon stats filtered by ELO range. |
| `schema.sql` | Tables, indexes and the views the API reads through. |
| `db.py` | Opens the database with the right settings and creates it from the schema on first run. |
| `store.py` | Saves matches and pairs spectated copies with announcements. Shared by the poller and the import. |
| `import_json.py` | One off import of the old JSON files into the database. |
| `add_tournament.py` | Adds or updates tournament results. |
| `backup_db.py` | Backs up the database and keeps the newest copies. |
| `chat_camera.py` | Reads the stream's live chat and passes camera commands to the mod. Uses the parts of YouTube's chat stream format in `youtube_chat.proto`. |
| `static/` | Frontend. Plain ES modules, no build step or framework. |

## Running it

Needs Python 3.10 or newer.

```
pip install -r requirements.txt
```

The poller logs in with a PlayFab custom ID, read from an environment variable:

```
setx BLASTATS_CUSTOM_ID <id>
```

If you're upgrading from the JSON version, load the old data once before starting anything. It won't overwrite an existing database unless given `--replace`.

```
python import_json.py
```

Then, from the project folder (open a new terminal after `setx`):

```
python BlastonPoller.py
python -m uvicorn api:app --port 8000
```

The site is at `http://localhost:8000`. Everything is stored in `data/`, which is created on first run and isn't part of the repo.

### Tournaments

Tournament results are added by hand from a JSON file holding one tournament or a list of them:

```json
{"name": "Halloween Cup 2026", "placement": [{"displayName": "Xyfume", "place": 1, "prizeMoney": 75}]}
```

```
python add_tournament.py cup.json
python add_tournament.py --list
python add_tournament.py --remove "Halloween Cup 2026"
```

A tournament with the same name is replaced, so a file can be edited and loaded again.

### Backups

`data/` isn't part of the repo, so the database is backed up separately. The backup is safe to run while the poller and API are running, and keeps the newest 7 copies.

```
python backup_db.py --dest "C:/path/to/OneDrive/Blastats backups"
```

To run it daily, add it to Task Scheduler with the full path to `python.exe`. To restore, stop the poller and API, delete `blastats.db-wal` and `blastats.db-shm` from `data/`, and copy a backup in as `data/blastats.db`.

### Chat camera

Viewers can change the stream camera from the live chat, with a 3 second cooldown between changes.

| Command | Camera |
| --- | --- |
| `/auto` or `/autocamera` | The client's auto camera, cycling through the fixed views |
| `/thirdperson` | Third person, following each round's winner |
| `/firstperson` | First person, following each round's winner |
| `/thirdperson pink` or `/firstperson blue` | Stays on that player |

It needs a YouTube Data API key from a Google Cloud project with the YouTube Data API v3 enabled, and reads the live video from `videoId` in `data/stream_info.json`. Run it on the PC running the spectator client, since commands reach the mod through the `data/` folder.

```
setx YOUTUBE_API_KEY <key>
python chat_camera.py
```

### Building the mod

1. Install [MelonLoader](https://melonwiki.xyz) into Blaston Spectator and launch it once so it generates `MelonLoader/Il2CppAssemblies`.
2. Set `GamePath` near the top of `mod/InternalMenuPatch/InternalMenuPatch.csproj` to your install folder.
3. Build, and copy `InternalMenuPatch.dll` into the game's `Mods` folder.
4. Point the mod at the project's `data/` folder in `UserData/MelonPreferences.cfg`, with the game closed. `PovFov` is the first person camera's field of view, and 0 leaves the game's own.

   ```toml
   [BlastonTracker]
   DataDir = "C:/path/to/blastats/data"
   PovFov = 90.0
   ```

## License

[MIT](LICENSE)
